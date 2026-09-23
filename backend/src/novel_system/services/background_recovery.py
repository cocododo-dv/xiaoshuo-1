"""Crash-safe startup recovery for in-process background workers.

Workers still own the authoritative CAS.  The startup scan only discovers
durable candidates and submits them, which makes duplicate scans from multiple
ASGI workers harmless: at most one worker can move a row into an owned running
state.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
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
from novel_system.services.errors import DomainError


logger = logging.getLogger(__name__)

STARTUP_RECOVERY_LEASE_SECONDS = 30

SceneDispatch = Callable[[str], None]
ChapterDispatch = Callable[[str, str, str | None], None]


def recover_run_job_dispatches(
    session: Session,
    *,
    now: datetime | None = None,
    scene_dispatch: SceneDispatch,
    chapter_dispatch: ChapterDispatch,
) -> dict[str, list[str]]:
    """Re-submit queued/pending jobs and abandoned RUNNING leases."""

    current = now or datetime.now(UTC)
    current_iso = current.isoformat()
    rows = list(
        session.scalars(
            select(ChapterRunJob).where(
                ChapterRunJob.job_type.in_(("scene_run_full", "chapter_run_full")),
                ChapterRunJob.status.in_(("queued", "pending", "running")),
            )
        )
    )
    candidates: list[tuple[str, str, str | None, str | None]] = []
    skipped_active: list[str] = []
    for job in rows:
        if job.status == "running" and _lease_is_active(job.lease_expires_at, current_iso):
            skipped_active.append(job.job_id)
            continue
        if job.job_type == "scene_run_full" and job.status not in {"queued", "running"}:
            continue
        if job.job_type == "chapter_run_full" and job.status not in {"pending", "running"}:
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
        if job_type == "scene_run_full":
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
    if _lease_is_active(current_row.lease_expires_at, current.isoformat()):
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


def _dispatch_scene(job_id: str) -> None:
    from novel_system.services.scene_run_jobs import start_scene_run_job_worker

    start_scene_run_job_worker(job_id)


def _dispatch_chapter(job_id: str, chapter_id: str, project_id: str | None) -> None:
    if project_id:
        from novel_system.services.projects import start_project_chapter_run_job_worker

        start_project_chapter_run_job_worker(project_id, chapter_id, job_id)
        return
    thread = threading.Thread(
        target=_run_unscoped_chapter_job,
        args=(job_id, chapter_id),
        daemon=True,
        name=f"chapter-recovery:{job_id}",
    )
    thread.start()


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


def _lease_is_active(value: str | None, now_iso: str) -> bool:
    # ISO-8601 timestamps produced by the service are fixed-width UTC values;
    # parse malformed/legacy values as abandoned rather than immortal.
    parsed = _parse_iso(value)
    if parsed is None:
        return False
    now = _parse_iso(now_iso)
    return bool(now is not None and parsed > now)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
