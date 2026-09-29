"""Crash-safe recovery for in-process background workers.

Workers still own the authoritative CAS.  The scans only discover durable
candidates and submit them, which makes duplicate scans from multiple ASGI
workers harmless: at most one worker can move a row into an owned running
state.

- 启动恢复（``run_startup_recovery``，lifespan 同步调用一次）：排队的任务、租约已过期 / 没有租约的 running
  任务再派发；过期的取消请求收尾；旧版遗留的状态收拾一次。
- 周期恢复（B03-01，``start_run_job_sweeper``，每分钟一拍，启动那一拍不跑）：进程还活着时，租约过期、本进程
  里又没有它的工人的 running 任务再派发；主人已死的取消请求收尾。
- 进程退出（``shutdown_run_job_workers``）：工人代 +1（在跑的工人不再续租）、关工人车道、把本进程工人持有的
  租约就地到期——重启后的启动恢复立刻接着跑，不必等 600 秒的租约自然过期。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novel_system.db.models import (
    BackgroundRecoveryLease,
    ChapterGoal,
    ChapterRunJob,
    StyleReferenceRun,
    utcnow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.background_jobs import (
    PeriodicThread,
    bump_worker_generation,
    close_daemon_lanes,
    daemon_lane,
)
from novel_system.services.errors import DomainError
from novel_system.services.run_job_leases import (
    CHAPTER_RUN_LANE,
    CHAPTER_RUN_LANE_WORKERS,
    JOB_TYPE_CHAPTER_FULL,
    JOB_TYPE_SCENE_FULL,
    RUN_JOB_TYPES,
    SCENE_RUN_LANE,
    STATUS_PENDING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    busy_job_ids,
    lease_is_active,
    mark_dispatched,
    release_held_leases,
    unmark_dispatched,
)


logger = logging.getLogger(__name__)

STARTUP_RECOVERY_LEASE_SECONDS = 30
RUN_JOB_SWEEPER_THREAD_NAME = "run_job_sweeper"
RUN_JOB_SWEEP_INTERVAL_SECONDS = 60.0
_RUN_JOB_SWEEPER: PeriodicThread | None = None
_RUN_JOB_SWEEPER_LOCK = threading.Lock()

SceneDispatch = Callable[[str], None]
ChapterDispatch = Callable[[str, str, str | None], None]


def recover_run_job_dispatches(
    session: Session,
    *,
    now: datetime | None = None,
    scene_dispatch: SceneDispatch,
    chapter_dispatch: ChapterDispatch,
    orphans_only: bool = False,
    skip_job_ids: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Re-submit queued/pending jobs and abandoned RUNNING leases.

    ``orphans_only``（周期恢复）：只收 running 且租约已过期 / 没有租约的任务——排队中的归启动恢复与建任务的请求
    管。``skip_job_ids``：本进程里还排着或在跑的任务（它们的工人还活着），不重复派发。
    """

    current = now or datetime.now(UTC)
    skip = set(skip_job_ids)
    rows = list(
        session.scalars(
            select(ChapterRunJob).where(
                ChapterRunJob.job_type.in_(RUN_JOB_TYPES),
                ChapterRunJob.status.in_((STATUS_QUEUED, STATUS_PENDING, STATUS_RUNNING)),
            )
        )
    )
    candidates: list[tuple[str, str, str | None, str | None]] = []
    skipped_active: list[str] = []
    for job in rows:
        if job.job_id in skip:
            continue
        if job.status == STATUS_RUNNING and lease_is_active(job.lease_expires_at, now=current):
            skipped_active.append(job.job_id)
            continue
        if orphans_only and job.status != STATUS_RUNNING:
            continue
        if job.job_type == JOB_TYPE_SCENE_FULL and job.status not in {STATUS_QUEUED, STATUS_RUNNING}:
            continue
        if job.job_type == JOB_TYPE_CHAPTER_FULL and job.status not in {STATUS_PENDING, STATUS_RUNNING}:
            continue
        project_id: str | None = None
        if job.chapter_id:
            chapter = session.get(ChapterGoal, job.chapter_id)
            project_id = str(chapter.project_id) if chapter and chapter.project_id else None
        candidates.append((job.job_type, job.job_id, job.chapter_id, project_id))
    session.rollback()

    dispatched_scene: list[str] = []
    dispatched_chapter: list[str] = []
    for job_type, job_id, chapter_id, project_id in candidates:
        if job_type == JOB_TYPE_SCENE_FULL:
            scene_dispatch(job_id)
            dispatched_scene.append(job_id)
        elif chapter_id:
            chapter_dispatch(job_id, str(chapter_id), project_id)
            dispatched_chapter.append(job_id)
    return {
        "scene_dispatched": dispatched_scene,
        "chapter_dispatched": dispatched_chapter,
        "active_lease_skipped": skipped_active,
    }


def retire_legacy_style_reference_runs(session: Session) -> list[str]:
    """旧抽取流程(``RunOrchestrator``,2026-09-23 v3 P3 删除)留下的「运行中」run 标 failed。

    学习文风作业的血缘 run(``dispatch_state="learn_job"``)不动:它们的状态由作业写。旧 run 不能续跑,
    作者对这本书「学习文风」即可。
    """

    rows = list(
        session.scalars(
            select(StyleReferenceRun).where(
                StyleReferenceRun.status == "running",
                StyleReferenceRun.dispatch_state.in_(("queued", "running")),
            )
        )
    )
    failed: list[str] = []
    for run in rows:
        if _fail_style_run(
            session,
            run,
            code="STYLE_REFERENCE_RUN_RETIRED",
            message="旧的抽取流程已下线:请对这本书「学习文风」",
        ):
            failed.append(run.run_id)
    session.commit()
    return failed


def run_startup_recovery() -> dict[str, Any]:
    """FastAPI lifespan entry point; failures are isolated by job family."""

    owner_id = f"startup:{uuid4().hex}"
    try:
        with SessionLocal() as session:
            if not acquire_startup_recovery_lease(session, owner_id=owner_id):
                return {"skipped": "another_startup_worker_owns_recovery"}
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery lease acquisition failed")
        return {"skipped": "recovery_lease_unavailable"}

    summary: dict[str, Any] = {}
    try:
        from novel_system.services.llm_accounting import (
            recover_stale_legacy_reservations,
        )

        with SessionLocal() as session:
            summary["llm_legacy_reservations"] = recover_stale_legacy_reservations(
                session
            )
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while reconciling legacy LLM reservations")
        summary["llm_legacy_reservations"] = {"error": "scan_failed"}

    try:
        with SessionLocal() as session:
            summary["run_jobs"] = recover_run_job_dispatches(
                session,
                scene_dispatch=_dispatch_scene,
                chapter_dispatch=_dispatch_chapter,
                skip_job_ids=busy_job_ids(),
            )
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while scanning scene/chapter jobs")
        summary["run_jobs"] = {"error": "scan_failed"}

    try:
        from novel_system.services.scene_run_jobs import recover_expired_cancel_requested_jobs

        with SessionLocal() as session:
            summary["runtime_sweep"] = {
                "expired_cancel_requests": recover_expired_cancel_requested_jobs(
                    session, worker_id="startup_recovery"
                )
            }
            session.commit()
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while sweeping expired cancel requests")
        summary["runtime_sweep"] = {"error": "scan_failed"}

    try:
        with SessionLocal() as session:
            summary["style_reference_legacy_runs_retired"] = retire_legacy_style_reference_runs(session)
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while retiring legacy style-reference runs")
        summary["style_reference_legacy_runs_retired"] = {"error": "scan_failed"}

    try:
        # 风格参考 v3:分类作业的续跑由作业表的常驻清扫线程负责(lifespan 里启动);这里只收拾
        # 旧的书上 JSON 游标状态机留下、没有作业行可续的书(标 failed,作者「继续分类」建新作业)。
        from novel_system.services.style_reference.import_job import fail_orphaned_classifications

        with SessionLocal() as session:
            summary["style_reference_orphaned_classifications"] = fail_orphaned_classifications(session)
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while scanning orphaned style-reference classifications")
        summary["style_reference_orphaned_classifications"] = {"error": "scan_failed"}

    # 风格参考 v3（P5b）：旧回测（异步校验报告）随旧校验层删除，不再有要收拾的回测 worker；对照检查在作业表上，
    # 由作业表的常驻清扫线程续跑。
    logger.info("startup background recovery summary=%s", summary)
    return summary


def acquire_startup_recovery_lease(
    session: Session,
    *,
    owner_id: str,
    now: datetime | None = None,
    lease_seconds: int = STARTUP_RECOVERY_LEASE_SECONDS,
) -> bool:
    """Elect one scanner across processes using insert-or-expired-CAS."""

    current = now or datetime.now(UTC)
    expires_at = (current + timedelta(seconds=max(1, lease_seconds))).isoformat()
    values = {
        "lease_key": "application_startup_recovery",
        "owner_id": owner_id,
        "lease_expires_at": expires_at,
        "created_at": current.isoformat(),
        "updated_at": current.isoformat(),
    }
    try:
        session.execute(insert(BackgroundRecoveryLease).values(**values))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()

    current_row = session.get(BackgroundRecoveryLease, values["lease_key"])
    if current_row is None:
        session.rollback()
        return False
    if lease_is_active(current_row.lease_expires_at, now=current):
        session.rollback()
        return False
    previous_owner = current_row.owner_id
    previous_expiry = current_row.lease_expires_at
    session.rollback()
    won = session.execute(
        update(BackgroundRecoveryLease)
        .where(
            BackgroundRecoveryLease.lease_key == values["lease_key"],
            BackgroundRecoveryLease.owner_id == previous_owner,
            BackgroundRecoveryLease.lease_expires_at == previous_expiry,
        )
        .values(
            owner_id=owner_id,
            lease_expires_at=expires_at,
            updated_at=current.isoformat(),
        )
        .execution_options(synchronize_session=False)
    )
    if won.rowcount == 1:
        session.commit()
        return True
    session.rollback()
    return False


def sweep_orphaned_run_jobs(*, now: datetime | None = None) -> dict[str, Any]:
    """周期恢复的一拍（B03-01）：租约已过期、本进程里又没有它的工人的 running 任务再派发一次；主人已死的
    取消请求收尾为 cancelled。两件事各自隔离失败，下一拍再试。"""
    summary: dict[str, Any] = {}
    try:
        with SessionLocal() as session:
            summary["run_jobs"] = recover_run_job_dispatches(
                session,
                now=now,
                scene_dispatch=_dispatch_scene,
                chapter_dispatch=_dispatch_chapter,
                orphans_only=True,
                skip_job_ids=busy_job_ids(),
            )
    except Exception:  # noqa: BLE001 — 周期线程边界
        logger.exception("run job sweep failed while scanning orphaned jobs")
        summary["run_jobs"] = {"error": "scan_failed"}
    try:
        from novel_system.services.scene_run_jobs import recover_expired_cancel_requested_jobs

        with SessionLocal() as session:
            summary["expired_cancel_requests"] = recover_expired_cancel_requested_jobs(
                session, worker_id="recovery_sweep"
            )
            session.commit()
    except Exception:  # noqa: BLE001 — 周期线程边界
        logger.exception("run job sweep failed while confirming expired cancellations")
        summary["expired_cancel_requests"] = {"error": "scan_failed"}
    return summary


def start_run_job_sweeper(*, interval_seconds: float = RUN_JOB_SWEEP_INTERVAL_SECONDS) -> None:
    """运行任务的周期恢复线程（lifespan 启动时调用；重复调用无害）。启动那一拍不跑——启动恢复刚同步扫过。"""
    global _RUN_JOB_SWEEPER
    with _RUN_JOB_SWEEPER_LOCK:
        if _RUN_JOB_SWEEPER is not None and _RUN_JOB_SWEEPER.is_alive():
            return
        _RUN_JOB_SWEEPER = PeriodicThread(
            RUN_JOB_SWEEPER_THREAD_NAME,
            lambda: sweep_orphaned_run_jobs(),
            interval_seconds=interval_seconds,
            run_first=False,
        ).start()


def shutdown_run_job_workers() -> dict[str, Any]:
    """lifespan 结束（B03-01）：停周期恢复、工人代 +1（在跑的工人不再续租）、关工人车道（丢下还没开始的派发——
    任务行还是 queued，下次启动接着派发）、把本进程工人持有的租约就地到期。"""
    global _RUN_JOB_SWEEPER
    with _RUN_JOB_SWEEPER_LOCK:
        sweeper, _RUN_JOB_SWEEPER = _RUN_JOB_SWEEPER, None
    if sweeper is not None:
        sweeper.stop(join_timeout=5.0)
    bump_worker_generation()
    dropped = close_daemon_lanes(SCENE_RUN_LANE, CHAPTER_RUN_LANE)
    for items in dropped.values():
        for args in items:
            if args:
                unmark_dispatched(str(args[0]))
    released = release_held_leases()
    if released:
        logger.info("run job leases released on shutdown: %s", released)
    return {
        "released_leases": released,
        "dropped_dispatches": {name: [args[0] for args in items if args] for name, items in dropped.items()},
    }


def _dispatch_scene(job_id: str) -> None:
    from novel_system.services.scene_run_jobs import start_scene_run_job_worker

    start_scene_run_job_worker(job_id)


def _dispatch_chapter(job_id: str, chapter_id: str, project_id: str | None) -> None:
    if project_id:
        from novel_system.services.projects import start_project_chapter_run_job_worker

        start_project_chapter_run_job_worker(project_id, chapter_id, job_id)
        return
    if not mark_dispatched(job_id):
        return
    lane = daemon_lane(CHAPTER_RUN_LANE, max_workers=CHAPTER_RUN_LANE_WORKERS)
    if not lane.submit(_run_unscoped_chapter_job, job_id, chapter_id):
        unmark_dispatched(job_id)


def _run_unscoped_chapter_job(job_id: str, chapter_id: str) -> None:
    from novel_system.services.chapter_runner import ChapterRunnerService

    try:
        with SessionLocal() as session:
            ChapterRunnerService(session).run_full(chapter_id)
            session.commit()
    except DomainError as exc:
        if exc.code not in {"RUN_JOB_IN_PROGRESS", "RUN_JOB_NOT_CLAIMABLE"}:
            logger.exception("recovered chapter job %s failed: %s", job_id, exc.code)
    except Exception:  # pragma: no cover - worker boundary
        logger.exception("recovered chapter job %s failed", job_id)
    finally:
        unmark_dispatched(job_id)


def _fail_style_run(
    session: Session,
    run: StyleReferenceRun,
    *,
    code: str,
    message: str,
) -> bool:
    coverage = dict(run.coverage_json or {})
    coverage["failure_reason"] = code
    coverage["retryable"] = True
    finished_at = utcnow()
    changed = session.execute(
        update(StyleReferenceRun)
        .where(
            StyleReferenceRun.run_id == run.run_id,
            StyleReferenceRun.status == "running",
            StyleReferenceRun.dispatch_state == run.dispatch_state,
            (
                StyleReferenceRun.heartbeat_at.is_(None)
                if run.heartbeat_at is None
                else StyleReferenceRun.heartbeat_at == run.heartbeat_at
            ),
        )
        .values(
            status="failed",
            dispatch_state="failed",
            coverage_json=coverage,
            heartbeat_at=finished_at,
            finished_at=finished_at,
            error_code=code,
            error_text=message,
            retryable=True,
        )
        .execution_options(synchronize_session=False)
    )
    return changed.rowcount == 1
