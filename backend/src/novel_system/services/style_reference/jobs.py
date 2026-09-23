"""风格参考 v3（2026-09-23）— 统一的持久作业：分类 / 学习文风 / 对照检查。

取代进程内登记簿（``import_progress``）、书上的 JSON 游标与 run / report 行各自的心跳。一个作业一行
（``style_reference_jobs``），生命周期：

    queued --claim--> running --succeed/fail/cancel--> succeeded / failed / cancelled
                         |
                         +-- 心跳过期（工人死了：重启、``--reload``、崩溃）--sweep--> queued（再派发）

**所有权**：认领时 ``attempt`` +1 并换一枚新的 ``owner_token``；之后的每一次写（心跳 / 进度 / 游标 /
结束）都是「owner_token 仍是我、state 仍是 running」的条件 UPDATE。作业被清扫重排、被取消、所属的书被删，
旧工人的写全部落空（返回 False），工人据此停下——这就是作业的身份，不靠进程内状态。

**恢复**：常驻清扫线程每 ``SWEEP_INTERVAL_SECONDS`` 秒把心跳过期的 running 放回 queued 并把所有 queued
派发出去；启动时先清扫一次。重复派发无害——认领是条件写，只有一个工人能拿到。

**取消**：``request_cancel`` 置 ``cancel_requested``；排队中或工人已死（心跳过期）的作业在请求里直接收尾为
cancelled，运行中的由工人在下一个检查点（``check_continue``）看到后收尾。

处理器约定（``register_job_handler(kind, handler)``）：``handler(session, claimed, service)`` 在工人线程里
运行，自己负责周期性调用 ``service.check_continue(claimed)``（取消 → ``JobCancelled``，丢了所有权 →
``JobLost``）、``service.progress`` / ``service.save_cursor``，最后 ``service.succeed``；抛出的
``DomainError`` 由框架记为失败（错误码原样保留），其余异常记为 ``STYLE_REFERENCE_JOB_FAILED``。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceJob, utcnow
from novel_system.services.errors import DomainError

logger = logging.getLogger(__name__)

JOB_KIND_CLASSIFY = "classify"
JOB_KIND_LEARN = "learn"
JOB_KIND_CHECK = "check"
JOB_KINDS = (JOB_KIND_CLASSIFY, JOB_KIND_LEARN, JOB_KIND_CHECK)

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
ACTIVE_STATES = (STATE_QUEUED, STATE_RUNNING)
TERMINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED)

HEARTBEAT_INTERVAL_SECONDS = 15.0
STALE_AFTER_SECONDS = 60.0
SWEEP_INTERVAL_SECONDS = 30.0
EXECUTOR_MAX_WORKERS = 2

JOB_FAILED_CODE = "STYLE_REFERENCE_JOB_FAILED"
JOB_CANCELLED_CODE = "STYLE_REFERENCE_JOB_CANCELLED"
JOB_ALREADY_ACTIVE_CODE = "STYLE_REFERENCE_JOB_ALREADY_ACTIVE"
JOB_NOT_FOUND_CODE = "STYLE_REFERENCE_JOB_NOT_FOUND"


class JobCancelled(Exception):
    """处理器在检查点看到取消请求。"""


class JobLost(Exception):
    """处理器发现自己已不是这个作业的主人（被清扫重排 / 被删 / 被别的工人接手）。"""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    kind: str
    owner_token: str
    attempt: int
    book_id: str | None
    profile_id: str | None
    op_key: str | None
    params: dict[str, Any] = field(default_factory=dict)
    cursor: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def heartbeat_is_stale(heartbeat_at: str | None, *, now: datetime | None = None) -> bool:
    beat = _parse(heartbeat_at)
    if beat is None:
        return True
    return (now or _now()) - beat > timedelta(seconds=STALE_AFTER_SECONDS)


class StyleJobService:
    """作业表的全部读写。方法只 flush 不 commit（与调用方同一事务）——工人框架负责提交。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ 建 / 读
    def create(
        self,
        kind: str,
        *,
        book_id: str | None = None,
        profile_id: str | None = None,
        op_key: str | None = None,
        params: Mapping[str, Any] | None = None,
        phase: str | None = None,
        allow_parallel: bool = False,
    ) -> StyleReferenceJob:
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown style job kind: {kind!r}")
        if book_id and not allow_parallel:
            active = self.active_for_book(book_id, kind=kind)
            if active:
                raise DomainError(
                    JOB_ALREADY_ACTIVE_CODE,
                    "这本书已有同类作业在排队或运行，请等它完成或先取消",
                    status_code=409,
                    details={"job_id": active[0].job_id, "kind": kind, "book_id": book_id},
                )
        now = utcnow()
        job = StyleReferenceJob(
            job_id=f"sr_job_{uuid.uuid4().hex[:12]}",
            kind=kind,
            book_id=book_id,
            profile_id=profile_id,
            op_key=op_key,
            state=STATE_QUEUED,
            phase=phase,
            cancel_requested=0,
            attempt=0,
            owner_token=None,
            heartbeat_at=None,
            params_json=dict(params or {}),
            cursor_json={},
            progress_json={"phase": phase} if phase else {},
            result_json=None,
            error_json=None,
            created_at=now,
            updated_at=now,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get(self, job_id: str, *, fresh: bool = False) -> StyleReferenceJob | None:
        if fresh:
            return self.session.execute(
                select(StyleReferenceJob)
                .where(StyleReferenceJob.job_id == job_id)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
        return self.session.get(StyleReferenceJob, job_id)

    def active_for_book(self, book_id: str, *, kind: str | None = None) -> list[StyleReferenceJob]:
        stmt = select(StyleReferenceJob).where(
            StyleReferenceJob.book_id == book_id,
            StyleReferenceJob.state.in_(ACTIVE_STATES),
        )
        if kind:
            stmt = stmt.where(StyleReferenceJob.kind == kind)
        return list(
            self.session.execute(
                stmt.order_by(StyleReferenceJob.created_at).execution_options(populate_existing=True)
            ).scalars()
        )

    def latest_for_book(self, book_id: str, *, kind: str | None = None) -> StyleReferenceJob | None:
        stmt = select(StyleReferenceJob).where(StyleReferenceJob.book_id == book_id)
        if kind:
            stmt = stmt.where(StyleReferenceJob.kind == kind)
        return self.session.execute(
            stmt.order_by(StyleReferenceJob.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    def list_recent(self, *, finished_within_seconds: float = 600.0, limit: int = 100) -> list[StyleReferenceJob]:
        """活动面板：所有活动作业 + 最近结束的作业（默认 10 分钟内）。"""
        cutoff = _iso(_now() - timedelta(seconds=finished_within_seconds))
        stmt = (
            select(StyleReferenceJob)
            .where(
                (StyleReferenceJob.state.in_(ACTIVE_STATES))
                | (StyleReferenceJob.finished_at >= cutoff)
            )
            .order_by(StyleReferenceJob.created_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    # ------------------------------------------------------------------ 认领 / 所有权
    def claim(self, job_id: str) -> ClaimedJob | None:
        """queued → running（attempt +1、新 owner_token）。排队中已被取消的作业在这里收尾为 cancelled。"""
        job = self.get(job_id, fresh=True)
        if job is None or job.state != STATE_QUEUED:
            return None
        now = utcnow()
        if int(job.cancel_requested or 0):
            self.session.execute(
                update(StyleReferenceJob)
                .where(StyleReferenceJob.job_id == job_id, StyleReferenceJob.state == STATE_QUEUED)
                .values(
                    state=STATE_CANCELLED,
                    finished_at=now,
                    updated_at=now,
                    error_json={"code": JOB_CANCELLED_CODE, "message": "cancelled before start"},
                )
            )
            self.session.flush()
            return None
        token = uuid.uuid4().hex
        result = self.session.execute(
            update(StyleReferenceJob)
            .where(
                StyleReferenceJob.job_id == job_id,
                StyleReferenceJob.state == STATE_QUEUED,
                StyleReferenceJob.attempt == int(job.attempt or 0),
            )
            .values(
                state=STATE_RUNNING,
                attempt=int(job.attempt or 0) + 1,
                owner_token=token,
                heartbeat_at=now,
                started_at=job.started_at or now,
                updated_at=now,
                error_json=None,
            )
        )
        self.session.flush()
        if int(result.rowcount or 0) != 1:
            return None
        job = self.get(job_id, fresh=True)
        assert job is not None
        return ClaimedJob(
            job_id=job.job_id,
            kind=job.kind,
            owner_token=token,
            attempt=int(job.attempt or 0),
            book_id=job.book_id,
            profile_id=job.profile_id,
            op_key=job.op_key,
            params=dict(job.params_json or {}),
            cursor=dict(job.cursor_json or {}),
            progress=dict(job.progress_json or {}),
        )

    def _owned(self, claimed: ClaimedJob):
        return and_(
            StyleReferenceJob.job_id == claimed.job_id,
            StyleReferenceJob.owner_token == claimed.owner_token,
            StyleReferenceJob.state == STATE_RUNNING,
        )

    def _owned_update(self, claimed: ClaimedJob, **values: Any) -> bool:
        values.setdefault("updated_at", utcnow())
        result = self.session.execute(update(StyleReferenceJob).where(self._owned(claimed)).values(**values))
        self.session.flush()
        return int(result.rowcount or 0) == 1

    def heartbeat(self, claimed: ClaimedJob) -> bool:
        return self._owned_update(claimed, heartbeat_at=utcnow())

    def still_owner(self, claimed: ClaimedJob) -> bool:
        job = self.get(claimed.job_id, fresh=True)
        return bool(job is not None and job.state == STATE_RUNNING and job.owner_token == claimed.owner_token)

    def check_continue(self, claimed: ClaimedJob) -> None:
        """处理器的检查点：取消 → ``JobCancelled``；不再是主人 → ``JobLost``。"""
        job = self.get(claimed.job_id, fresh=True)
        if job is None or job.state != STATE_RUNNING or job.owner_token != claimed.owner_token:
            raise JobLost(claimed.job_id)
        if int(job.cancel_requested or 0):
            raise JobCancelled(claimed.job_id)

    # ------------------------------------------------------------------ 进度 / 游标
    def progress(
        self,
        claimed: ClaimedJob,
        *,
        phase: str | None = None,
        phase_label: str | None = None,
        done: int | None = None,
        total: int | None = None,
        detail: str | None = None,
        llm_calls_delta: int = 0,
        extra: Mapping[str, Any] | None = None,
    ) -> bool:
        job = self.get(claimed.job_id, fresh=True)
        if job is None:
            return False
        progress = dict(job.progress_json or {})
        if phase is not None:
            if progress.get("phase") != phase:
                progress["phase_started_at"] = utcnow()
            progress["phase"] = phase
        if phase_label is not None:
            progress["phase_label"] = phase_label
        if done is not None:
            progress["done"] = int(done)
        if total is not None:
            progress["total"] = int(total)
        if detail is not None:
            progress["detail"] = str(detail)
        if llm_calls_delta:
            progress["llm_calls"] = int(progress.get("llm_calls") or 0) + int(llm_calls_delta)
        if extra:
            progress.update(dict(extra))
        progress["updated_at"] = utcnow()
        values: dict[str, Any] = {"progress_json": progress, "heartbeat_at": utcnow()}
        if phase is not None:
            values["phase"] = phase
        return self._owned_update(claimed, **values)

    def save_cursor(self, claimed: ClaimedJob, cursor: Mapping[str, Any]) -> bool:
        return self._owned_update(claimed, cursor_json=dict(cursor), heartbeat_at=utcnow())

    # ------------------------------------------------------------------ 结束
    def succeed(self, claimed: ClaimedJob, result: Mapping[str, Any] | None = None) -> bool:
        now = utcnow()
        return self._owned_update(
            claimed,
            state=STATE_SUCCEEDED,
            result_json=dict(result or {}),
            error_json=None,
            finished_at=now,
            heartbeat_at=now,
            owner_token=None,
        )

    def fail(
        self,
        claimed: ClaimedJob,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        now = utcnow()
        error = {"code": str(code), "message": str(message)[:2000], "retryable": bool(retryable)}
        if details:
            error["details"] = dict(details)
        return self._owned_update(
            claimed,
            state=STATE_FAILED,
            error_json=error,
            finished_at=now,
            heartbeat_at=now,
            owner_token=None,
        )

    def finish_cancelled(self, claimed: ClaimedJob) -> bool:
        now = utcnow()
        return self._owned_update(
            claimed,
            state=STATE_CANCELLED,
            error_json={"code": JOB_CANCELLED_CODE, "message": "cancelled"},
            finished_at=now,
            heartbeat_at=now,
            owner_token=None,
        )

    # ------------------------------------------------------------------ 取消 / 重排 / 清扫
    def request_cancel(self, job_id: str) -> StyleReferenceJob:
        """请求取消。排队中 / 工人已死（心跳过期）的作业直接收尾；运行中的由工人在检查点收尾。"""
        job = self.get(job_id, fresh=True)
        if job is None:
            raise DomainError(JOB_NOT_FOUND_CODE, f"style job {job_id!r} not found", status_code=404)
        if job.state in TERMINAL_STATES:
            return job
        now = utcnow()
        finish_now = job.state == STATE_QUEUED or heartbeat_is_stale(job.heartbeat_at)
        values: dict[str, Any] = {"cancel_requested": 1, "updated_at": now}
        if finish_now:
            values.update(
                state=STATE_CANCELLED,
                finished_at=now,
                owner_token=None,
                error_json={"code": JOB_CANCELLED_CODE, "message": "cancelled"},
            )
        self.session.execute(
            update(StyleReferenceJob)
            .where(StyleReferenceJob.job_id == job_id, StyleReferenceJob.state.in_(ACTIVE_STATES))
            .values(**values)
        )
        self.session.flush()
        refreshed = self.get(job_id, fresh=True)
        assert refreshed is not None
        return refreshed

    def cancel_all_for_book(self, book_id: str) -> list[str]:
        """删书前调用：该书所有活动作业直接收尾为 cancelled（旧工人的条件写随之落空）。"""
        now = utcnow()
        ids = [job.job_id for job in self.active_for_book(book_id)]
        if ids:
            self.session.execute(
                update(StyleReferenceJob)
                .where(StyleReferenceJob.job_id.in_(ids), StyleReferenceJob.state.in_(ACTIVE_STATES))
                .values(
                    state=STATE_CANCELLED,
                    cancel_requested=1,
                    owner_token=None,
                    finished_at=now,
                    updated_at=now,
                    error_json={"code": JOB_CANCELLED_CODE, "message": "book deleted"},
                )
            )
            self.session.flush()
        return ids

    def requeue(self, job_id: str, *, params_update: Mapping[str, Any] | None = None) -> StyleReferenceJob:
        """失败 / 取消的作业从游标处续跑：放回 queued（游标保留、错误清空）。"""
        job = self.get(job_id, fresh=True)
        if job is None:
            raise DomainError(JOB_NOT_FOUND_CODE, f"style job {job_id!r} not found", status_code=404)
        if job.state in ACTIVE_STATES:
            if job.state == STATE_RUNNING and not heartbeat_is_stale(job.heartbeat_at):
                raise DomainError(
                    JOB_ALREADY_ACTIVE_CODE,
                    "这个作业正在运行",
                    status_code=409,
                    details={"job_id": job_id},
                )
        params = dict(job.params_json or {})
        if params_update:
            params.update(dict(params_update))
        now = utcnow()
        self.session.execute(
            update(StyleReferenceJob)
            .where(StyleReferenceJob.job_id == job_id)
            .values(
                state=STATE_QUEUED,
                cancel_requested=0,
                owner_token=None,
                heartbeat_at=None,
                finished_at=None,
                error_json=None,
                params_json=params,
                updated_at=now,
            )
        )
        self.session.flush()
        refreshed = self.get(job_id, fresh=True)
        assert refreshed is not None
        return refreshed

    def sweep(self, *, now: datetime | None = None) -> list[str]:
        """心跳过期的 running → queued；返回所有 queued 作业 id（调用方派发）。"""
        current = now or _now()
        cutoff = _iso(current - timedelta(seconds=STALE_AFTER_SECONDS))
        stale = list(
            self.session.execute(
                select(StyleReferenceJob.job_id).where(
                    StyleReferenceJob.state == STATE_RUNNING,
                    (StyleReferenceJob.heartbeat_at.is_(None)) | (StyleReferenceJob.heartbeat_at < cutoff),
                )
            ).scalars()
        )
        for job_id in stale:
            self.session.execute(
                update(StyleReferenceJob)
                .where(
                    StyleReferenceJob.job_id == job_id,
                    StyleReferenceJob.state == STATE_RUNNING,
                    (StyleReferenceJob.heartbeat_at.is_(None)) | (StyleReferenceJob.heartbeat_at < cutoff),
                )
                .values(state=STATE_QUEUED, owner_token=None, updated_at=_iso(current))
            )
        if stale:
            self.session.flush()
            logger.info("style job sweep requeued %d stale job(s): %s", len(stale), stale)
        return list(
            self.session.execute(
                select(StyleReferenceJob.job_id)
                .where(StyleReferenceJob.state == STATE_QUEUED)
                .order_by(StyleReferenceJob.created_at)
            ).scalars()
        )


# ---------------------------------------------------------------------- 活动条目
_KIND_LABELS = {JOB_KIND_CLASSIFY: "段落分类", JOB_KIND_LEARN: "学习文风", JOB_KIND_CHECK: "对照检查"}


def job_activity_entry(job: StyleReferenceJob, *, now: datetime | None = None) -> dict[str, Any]:
    """活动面板的统一条目形状（前端 ``SR_ACTIVITY`` 消费）。"""
    current = now or _now()
    progress = dict(job.progress_json or {})
    done = int(progress.get("done") or 0)
    total = int(progress.get("total") or 0)
    percent = round(100.0 * done / total, 1) if total > 0 else None
    started = _parse(job.started_at)
    finished = _parse(job.finished_at)
    elapsed = ((finished or current) - started).total_seconds() if started else None
    eta = None
    phase_started = _parse(progress.get("phase_started_at"))
    if job.state == STATE_RUNNING and phase_started and 0 < done < total:
        spent = (current - phase_started).total_seconds()
        rate_done = float(progress.get("phase_done_at_start") or 0)
        effective = max(1e-6, done - rate_done)
        eta = max(0.0, spent / effective * (total - done))
    stalled = job.state == STATE_RUNNING and heartbeat_is_stale(job.heartbeat_at, now=current)
    return {
        "key": f"job:{job.job_id}",
        "job_id": job.job_id,
        "kind": job.kind,
        "kind_label": _KIND_LABELS.get(job.kind, job.kind),
        "status": job.state,
        "book_id": job.book_id,
        "profile_id": job.profile_id,
        "phase": job.phase,
        "phase_label": progress.get("phase_label") or job.phase,
        "detail": progress.get("detail"),
        "percent": percent,
        "steps": {"done": done, "total": total} if total else None,
        "llm_calls": int(progress.get("llm_calls") or 0),
        "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
        "eta_seconds": round(eta, 1) if eta is not None else None,
        "stalled": stalled,
        "cancellable": job.state in ACTIVE_STATES,
        "resumable": job.state in (STATE_FAILED, STATE_CANCELLED),
        "cancel_requested": bool(job.cancel_requested),
        "error": dict(job.error_json) if job.error_json else None,
        "result": dict(job.result_json) if job.result_json else None,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


# ---------------------------------------------------------------------- 工人框架
JobHandler = Callable[[Session, ClaimedJob, StyleJobService], None]
_HANDLERS: dict[str, JobHandler] = {}
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_DISPATCHED: set[str] = set()
_SWEEPER: threading.Thread | None = None
_SWEEPER_STOP = threading.Event()


def register_job_handler(kind: str, handler: JobHandler) -> None:
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown style job kind: {kind!r}")
    _HANDLERS[kind] = handler


def registered_job_handler(kind: str) -> JobHandler | None:
    return _HANDLERS.get(kind)


def _session_factory():
    from novel_system.db.session import SessionLocal

    return SessionLocal()


def dispatch_job(job_id: str) -> bool:
    """把作业投到有界线程池（同一进程里同一作业只投一次）。认领是条件写，重复投递无害。"""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if job_id in _DISPATCHED:
            return False
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=EXECUTOR_MAX_WORKERS, thread_name_prefix="sr_job")
        _DISPATCHED.add(job_id)
        _EXECUTOR.submit(_run_job, job_id)
    return True


def run_job_inline(job_id: str) -> None:
    """测试 / 工具用：在当前线程里跑完一个作业（与工人线程同一路径）。"""
    with _EXECUTOR_LOCK:
        _DISPATCHED.add(job_id)
    _run_job(job_id)


def _run_job(job_id: str) -> None:
    from novel_system.services.style_reference.background_heartbeat import periodic_heartbeat

    try:
        with _session_factory() as session:
            service = StyleJobService(session)
            claimed = service.claim(job_id)
            session.commit()
            if claimed is None:
                return
            handler = _HANDLERS.get(claimed.kind)
            if handler is None:
                service.fail(claimed, code=JOB_FAILED_CODE, message=f"no handler for kind {claimed.kind!r}")
                session.commit()
                return

            def _beat() -> None:
                with _session_factory() as beat_session:
                    StyleJobService(beat_session).heartbeat(claimed)
                    beat_session.commit()

            with periodic_heartbeat(
                _beat, interval_seconds=HEARTBEAT_INTERVAL_SECONDS, thread_name=f"sr_job_hb_{job_id}"
            ):
                try:
                    handler(session, claimed, service)
                    session.commit()
                except JobCancelled:
                    session.rollback()
                    service.finish_cancelled(claimed)
                    session.commit()
                except JobLost:
                    session.rollback()
                    logger.info("style job %s lost ownership; worker stops", job_id)
                except DomainError as exc:
                    session.rollback()
                    service.fail(
                        claimed,
                        code=exc.code,
                        message=str(exc.message),
                        retryable=bool(getattr(exc, "retryable", False)),
                        details=exc.details if isinstance(exc.details, Mapping) else None,
                    )
                    session.commit()
                except Exception as exc:  # noqa: BLE001 — 作业边界：记失败，不让线程静默死
                    session.rollback()
                    logger.exception("style job %s failed", job_id)
                    service.fail(
                        claimed,
                        code=getattr(exc, "code", None) or JOB_FAILED_CODE,
                        message=f"{type(exc).__name__}: {exc}",
                        retryable=True,
                    )
                    session.commit()
    except Exception:  # noqa: BLE001 — 最外层边界（连库失败等）
        logger.exception("style job %s crashed before it could record a result", job_id)
    finally:
        with _EXECUTOR_LOCK:
            _DISPATCHED.discard(job_id)


def sweep_and_dispatch() -> list[str]:
    """清扫一次并派发所有排队作业（启动恢复与常驻清扫共用）。"""
    try:
        with _session_factory() as session:
            ids = StyleJobService(session).sweep()
            session.commit()
    except Exception:  # noqa: BLE001 — 清扫失败留给下一轮
        logger.exception("style job sweep failed")
        return []
    dispatched = [job_id for job_id in ids if _HANDLERS.get(_job_kind(job_id) or "") and dispatch_job(job_id)]
    return dispatched


def _job_kind(job_id: str) -> str | None:
    try:
        with _session_factory() as session:
            job = session.get(StyleReferenceJob, job_id)
            return job.kind if job is not None else None
    except Exception:  # noqa: BLE001
        return None


def start_job_sweeper(*, interval_seconds: float = SWEEP_INTERVAL_SECONDS) -> None:
    """常驻清扫线程（FastAPI lifespan 启动时调用一次；重复调用无害）。"""
    global _SWEEPER
    with _EXECUTOR_LOCK:
        if _SWEEPER is not None and _SWEEPER.is_alive():
            return
        _SWEEPER_STOP.clear()

        def _loop() -> None:
            sweep_and_dispatch()
            while not _SWEEPER_STOP.wait(max(1.0, float(interval_seconds))):
                sweep_and_dispatch()

        _SWEEPER = threading.Thread(target=_loop, name="sr_job_sweeper", daemon=True)
        _SWEEPER.start()


def shutdown_job_workers(*, wait: bool = False) -> None:
    global _EXECUTOR, _SWEEPER
    _SWEEPER_STOP.set()
    with _EXECUTOR_LOCK:
        executor = _EXECUTOR
        _EXECUTOR = None
        _SWEEPER = None
        _DISPATCHED.clear()
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=not wait)


__all__ = [
    "ACTIVE_STATES",
    "ClaimedJob",
    "JOB_ALREADY_ACTIVE_CODE",
    "JOB_CANCELLED_CODE",
    "JOB_FAILED_CODE",
    "JOB_KINDS",
    "JOB_KIND_CHECK",
    "JOB_KIND_CLASSIFY",
    "JOB_KIND_LEARN",
    "JOB_NOT_FOUND_CODE",
    "JobCancelled",
    "JobLost",
    "STALE_AFTER_SECONDS",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "StyleJobService",
    "TERMINAL_STATES",
    "dispatch_job",
    "heartbeat_is_stale",
    "job_activity_entry",
    "register_job_handler",
    "registered_job_handler",
    "run_job_inline",
    "shutdown_job_workers",
    "start_job_sweeper",
    "sweep_and_dispatch",
]
