"""风格参考 v3（2026-09-23）— 统一的持久作业：分类 / 学习文风 / 对照检查。

一个作业一行（``style_reference_jobs``；进度、游标、心跳都在这一行上，没有别的进程内状态），生命周期：

    queued --claim--> running --succeed/fail/cancel--> succeeded / failed / cancelled
                         |
                         +-- 心跳过期（工人死了：重启、``--reload``、崩溃）--sweep--> queued（再派发）

**所有权**：认领时 ``attempt`` +1 并换一枚新的 ``owner_token``；之后的每一次写（心跳 / 进度 / 游标 /
结束）都是「owner_token 仍是我、state 仍是 running」的条件 UPDATE。作业被清扫重排、被取消、所属的书被删，
旧工人的写全部落空（返回 False），工人据此停下——这就是作业的身份，不靠进程内状态。

**恢复**：常驻清扫线程每 ``SWEEP_INTERVAL_SECONDS`` 秒把心跳过期的 running 放回 queued 并把所有 queued
派发出去；启动时先清扫一次。重复派发无害——认领是条件写，只有一个工人能拿到。

**进程退出 ≠ 作业失败**：``shutdown_job_workers``（lifespan 结束：``--reload``、停服）把「工人代」+1；处理器在下一个
检查点看到代变了就抛 ``JobInterrupted``，框架把作业**放回 queued**（``release``，游标保留），下次启动的清扫接着跑。
Ctrl-C（``KeyboardInterrupt``）同样放回队列再往上抛。LLM 调用跑在守护线程里（``DaemonCallPool``），进程退出不等
在飞的网络请求（它们的记账预留由记账层按 TTL 回收）。

**取消**：``request_cancel`` 置 ``cancel_requested``；排队中或工人已死（心跳过期）的作业在请求里直接收尾为
cancelled，运行中的由工人在下一个检查点（``check_continue``）看到后收尾；带着取消标记被清扫到的过期作业由清扫
直接收尾。在请求 / 认领 / 清扫里收尾的取消都调用这类作业登记的 ``on_cancelled`` 钩子（书的状态、学习的 run 行）。

**互斥**：同一本书的分类与学习互斥（各自也只能有一个活动作业）。建作业是「先插入、再查」：这条 INSERT 已拿到
SQLite 的写锁，两个几乎同时的请求在这里串行化，后到的一定看得见先到的那一行（先查后插时两个都查不到）。

处理器约定（``register_job_handler(kind, handler)``）：``handler(session, claimed, service)`` 在工人线程里
运行，自己负责周期性调用 ``service.check_continue(claimed)``（取消 → ``JobCancelled``，丢了所有权 →
``JobLost``，进程要退出 → ``JobInterrupted``）、``service.progress`` / ``service.save_cursor``，最后
``service.succeed``；抛出的 ``DomainError`` 由框架记为失败（错误码原样保留），其余异常记为
``STYLE_REFERENCE_JOB_FAILED``——进程退出期间冒出来的异常（代已变 / 线程池已关）一律按中断放回队列。
三种处理器共用的脚手架（检查点、并行调用循环、终态映射）在 ``job_runtime.JobRun``。

**维护任务**（``register_maintenance_task``）：随清扫线程跑的定期任务（例：``cleanup`` 登记的遥测 90 天留存
清理）——清扫线程启动时先跑一次，之后每隔登记的间隔再跑；任务自己开会话，异常只记日志、不影响清扫。
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
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
# 对照检查一次一个评审调用：单独一条车道，不在几十分钟的分类 / 学习后面排队
CHECK_EXECUTOR_MAX_WORKERS = 2
# 同一本书上互斥的作业种类（分类改段落类型、学习读段落类型）
BOOK_EXCLUSIVE_KINDS = (JOB_KIND_CLASSIFY, JOB_KIND_LEARN)

JOB_FAILED_CODE = "STYLE_REFERENCE_JOB_FAILED"
JOB_CANCELLED_CODE = "STYLE_REFERENCE_JOB_CANCELLED"
JOB_ALREADY_ACTIVE_CODE = "STYLE_REFERENCE_JOB_ALREADY_ACTIVE"
JOB_NOT_FOUND_CODE = "STYLE_REFERENCE_JOB_NOT_FOUND"
JOB_NOT_RESUMABLE_CODE = "STYLE_REFERENCE_JOB_NOT_RESUMABLE"


class JobCancelled(Exception):
    """处理器在检查点看到取消请求。"""


class JobLost(Exception):
    """处理器发现自己已不是这个作业的主人（被清扫重排 / 被删 / 被别的工人接手）。"""


class JobInterrupted(Exception):
    """工人所在的进程要退出（lifespan 结束 / ``--reload`` / Ctrl-C）：作业放回队列，游标保留，下次启动续跑。"""


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
    # 认领它的工人所属的「工人代」（框架填；None = 测试 / 工具直接拿的认领，不受进程退出影响）
    generation: int | None = None


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


def already_active_error(job: StyleReferenceJob) -> DomainError:
    return DomainError(
        JOB_ALREADY_ACTIVE_CODE,
        "这本书已有同类作业在排队或运行，请等它完成或先取消",
        status_code=409,
        details={"job_id": job.job_id, "kind": job.kind, "book_id": job.book_id, "state": job.state},
    )


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
        exclusive_with: Sequence[str] = (),
        conflict_error: Callable[[StyleReferenceJob], DomainError] | None = None,
    ) -> StyleReferenceJob:
        """建一个 queued 作业。``book_id`` 且非 ``allow_parallel``：同一本书上同类（加 ``exclusive_with`` 里的种类）
        已有活动作业 → ``conflict_error(那个作业)``（缺省 409 ``JOB_ALREADY_ACTIVE``）。插入之后再查一次（见模块文档
        「互斥」）；抛错时本事务的插入由调用方回滚（路由的幂等包装在 DomainError 上整体回滚）。"""
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown style job kind: {kind!r}")
        conflict_kinds = (kind, *tuple(exclusive_with))
        if book_id and not allow_parallel:
            conflict = self.first_conflict(book_id, kinds=conflict_kinds)
            if conflict is not None:
                raise (conflict_error or already_active_error)(conflict)
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
        if book_id and not allow_parallel:
            conflict = self.first_conflict(book_id, kinds=conflict_kinds, excluding=job.job_id)
            if conflict is not None:
                raise (conflict_error or already_active_error)(conflict)
        return job

    def first_conflict(
        self, book_id: str, *, kinds: Sequence[str], excluding: str | None = None
    ) -> StyleReferenceJob | None:
        """这本书上第一个种类在 ``kinds`` 里的活动作业（排除 ``excluding``）；续跑放回队列之后也用它复查。"""
        for other in self.active_for_book(book_id):
            if other.job_id != excluding and other.kind in kinds:
                return other
        return None

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
        """活动面板：**全部**活动作业 + 最近结束的作业（默认 10 分钟内，至多 ``limit`` 条）。

        活动作业不受 ``limit`` 限制：界面把 ``/activity`` 当完整清单，清单里不再有的在跑条目会被收掉——十分钟里结束的
        作业再多，也不能把一个还在跑的老作业挤出清单。"""
        cutoff = _iso(_now() - timedelta(seconds=finished_within_seconds))
        active = list(
            self.session.execute(
                select(StyleReferenceJob)
                .where(StyleReferenceJob.state.in_(ACTIVE_STATES))
                .order_by(StyleReferenceJob.created_at.desc())
            ).scalars()
        )
        finished = list(
            self.session.execute(
                select(StyleReferenceJob)
                .where(
                    StyleReferenceJob.state.in_(TERMINAL_STATES),
                    StyleReferenceJob.finished_at >= cutoff,
                )
                .order_by(StyleReferenceJob.created_at.desc())
                .limit(limit)
            ).scalars()
        )
        return sorted(active + finished, key=lambda job: str(job.created_at or ""), reverse=True)

    # ------------------------------------------------------------------ 认领 / 所有权
    def claim(self, job_id: str) -> ClaimedJob | None:
        """queued → running（attempt +1、新 owner_token）。排队中已被取消的作业在这里收尾为 cancelled。"""
        job = self.get(job_id, fresh=True)
        if job is None or job.state != STATE_QUEUED:
            return None
        now = utcnow()
        if int(job.cancel_requested or 0):
            result = self.session.execute(
                update(StyleReferenceJob)
                .where(StyleReferenceJob.job_id == job_id, StyleReferenceJob.state == STATE_QUEUED)
                .values(
                    state=STATE_CANCELLED,
                    owner_token=None,
                    finished_at=now,
                    updated_at=now,
                    error_json={"code": JOB_CANCELLED_CODE, "message": "cancelled before start"},
                )
            )
            self.session.flush()
            if int(result.rowcount or 0) == 1:
                run_cancel_hook(self.session, job_id)
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
        """处理器的检查点：进程要退出 → ``JobInterrupted``；取消 → ``JobCancelled``；不再是主人 → ``JobLost``。"""
        if worker_generation_changed(claimed):
            raise JobInterrupted(claimed.job_id)
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

    def release(self, claimed: ClaimedJob) -> bool:
        """进程退出时把自己的 running 作业放回 queued（条件写；游标、进度、attempt 保留，下次启动的清扫派发）。"""
        return self._owned_update(claimed, state=STATE_QUEUED, owner_token=None, heartbeat_at=None)

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
        result = self.session.execute(
            update(StyleReferenceJob)
            .where(StyleReferenceJob.job_id == job_id, StyleReferenceJob.state.in_(ACTIVE_STATES))
            .values(**values)
        )
        self.session.flush()
        if finish_now and int(result.rowcount or 0) == 1:
            run_cancel_hook(self.session, job_id)
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
        cutoff = _iso(_now() - timedelta(seconds=STALE_AFTER_SECONDS))
        # 条件写：只有「失败 / 取消 / 排队中 / 心跳过期的 running」能放回队列——路由看见过期心跳的同一瞬间工人刚好
        # 收尾成功时，不能把 succeeded 的作业又拉回 queued（再收尾一遍会把版本号、类型修订号各加一次）
        result = self.session.execute(
            update(StyleReferenceJob)
            .where(
                StyleReferenceJob.job_id == job_id,
                or_(
                    StyleReferenceJob.state.in_((STATE_FAILED, STATE_CANCELLED, STATE_QUEUED)),
                    and_(
                        StyleReferenceJob.state == STATE_RUNNING,
                        or_(StyleReferenceJob.heartbeat_at.is_(None), StyleReferenceJob.heartbeat_at < cutoff),
                    ),
                ),
            )
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
        if int(result.rowcount or 0) != 1:
            raise DomainError(
                JOB_ALREADY_ACTIVE_CODE if refreshed.state in ACTIVE_STATES else JOB_NOT_RESUMABLE_CODE,
                "这个作业的状态刚刚变了（正在运行或已经完成），刷新后再试",
                status_code=409,
                details={"job_id": job_id, "state": refreshed.state},
            )
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
            job = self.get(job_id, fresh=True)
            if job is None:
                continue
            stale_where = and_(
                StyleReferenceJob.job_id == job_id,
                StyleReferenceJob.state == STATE_RUNNING,
                (StyleReferenceJob.heartbeat_at.is_(None)) | (StyleReferenceJob.heartbeat_at < cutoff),
            )
            if int(job.cancel_requested or 0):
                # 工人死前已被要求取消：直接收尾（不放回队列再让认领去收，那样会跳过这类作业的收尾钩子）
                result = self.session.execute(
                    update(StyleReferenceJob)
                    .where(stale_where)
                    .values(
                        state=STATE_CANCELLED,
                        owner_token=None,
                        finished_at=_iso(current),
                        updated_at=_iso(current),
                        error_json={"code": JOB_CANCELLED_CODE, "message": "cancelled"},
                    )
                )
                self.session.flush()
                if int(result.rowcount or 0) == 1:
                    run_cancel_hook(self.session, job_id)
                continue
            self.session.execute(
                update(StyleReferenceJob)
                .where(stale_where)
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
CancelHook = Callable[[Session, StyleReferenceJob], None]
_HANDLERS: dict[str, JobHandler] = {}
_CANCEL_HOOKS: dict[str, CancelHook] = {}
_EXECUTORS: dict[str, ThreadPoolExecutor] = {}
_EXECUTOR_LOCK = threading.Lock()
_DISPATCHED: set[str] = set()
_SWEEPER: threading.Thread | None = None
_SWEEPER_STOP = threading.Event()
# 工人代：``shutdown_job_workers`` 每次 +1；认领时记下当时的代，检查点发现代变了 = 进程要退出
_GENERATION = 0

_LANE_LONG = "long"
_LANE_CHECK = "check"
_LANE_WORKERS = {_LANE_LONG: EXECUTOR_MAX_WORKERS, _LANE_CHECK: CHECK_EXECUTOR_MAX_WORKERS}


def register_job_handler(kind: str, handler: JobHandler, *, on_cancelled: CancelHook | None = None) -> None:
    """登记一类作业的处理器；``on_cancelled(session, job)`` 在请求 / 认领 / 清扫里直接收尾取消时调用（同一事务）。"""
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown style job kind: {kind!r}")
    _HANDLERS[kind] = handler
    if on_cancelled is not None:
        _CANCEL_HOOKS[kind] = on_cancelled


def registered_job_handler(kind: str) -> JobHandler | None:
    return _HANDLERS.get(kind)


def run_cancel_hook(session: Session, job_id: str) -> None:
    """一个刚在框架里被收尾为 cancelled 的作业：跑这类作业的收尾钩子（书的状态 / run 行）；钩子失败只记日志。"""
    job = session.get(StyleReferenceJob, job_id)
    if job is None:
        return
    hook = _CANCEL_HOOKS.get(str(job.kind))
    if hook is None:
        return
    try:
        with session.begin_nested():
            hook(session, job)
    except Exception:  # noqa: BLE001 — 收尾钩子是附带的状态整理，不让它拖垮取消本身
        logger.exception("style job %s cancel hook failed", job_id)


def current_worker_generation() -> int:
    return _GENERATION


def worker_generation_changed(claimed: ClaimedJob) -> bool:
    return claimed.generation is not None and claimed.generation != _GENERATION


def is_worker_interruption(exc: BaseException, claimed: ClaimedJob | None = None) -> bool:
    """进程退出期间冒出来的异常：代已变，或线程池已关（``cannot schedule new futures after [interpreter] shutdown``）。"""
    if claimed is not None and worker_generation_changed(claimed):
        return True
    return isinstance(exc, RuntimeError) and "after" in str(exc) and "shutdown" in str(exc)


class DaemonCallPool:
    """给处理器的 LLM 调用用的「线程池」：每个调用一条守护线程，并发上限由调用方控制（在飞数）。

    ``concurrent.futures.ThreadPoolExecutor`` 的线程在解释器退出时会被逐个 join——``--reload`` / 停服要等在飞的
    网络请求回来（最长到 LLM 超时）。这里的线程是守护线程：进程退出时直接丢下，作业由框架放回队列，下次续跑；
    丢下的调用的记账预留由记账层按 TTL 回收。接口是处理器用到的那部分 ``Executor``（``submit`` / ``shutdown``）。
    """

    def __init__(self, max_workers: int | None = None, thread_name_prefix: str = "sr_call") -> None:
        del max_workers  # 并发由调用方控制
        self._prefix = thread_name_prefix
        self._closed = False
        self._lock = threading.Lock()
        self._count = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._count += 1
            name = f"{self._prefix}_{self._count}"
        future: Future = Future()

        def runner() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — 原样交给 Future
                future.set_exception(exc)
            else:
                future.set_result(result)

        threading.Thread(target=runner, name=name, daemon=True).start()
        return future

    def shutdown(self, wait: bool = False, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures  # 守护线程不等；已开始的调用无法撤回（结果被丢弃）
        with self._lock:
            self._closed = True


def _session_factory():
    from novel_system.db.session import SessionLocal

    return SessionLocal()


def _lane_for(kind: str | None) -> str:
    return _LANE_CHECK if kind == JOB_KIND_CHECK else _LANE_LONG


def dispatch_job(job_id: str, *, kind: str | None = None) -> bool:
    """把作业投到有界线程池（同一进程里同一作业只投一次）。认领是条件写，重复投递无害。

    对照检查走自己的车道（``CHECK_EXECUTOR_MAX_WORKERS``），不在分类 / 学习后面排队。"""
    lane = _lane_for(kind if kind is not None else _job_kind(job_id))
    with _EXECUTOR_LOCK:
        if job_id in _DISPATCHED:
            return False
        executor = _EXECUTORS.get(lane)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=_LANE_WORKERS[lane], thread_name_prefix=f"sr_job_{lane}")
            _EXECUTORS[lane] = executor
        _DISPATCHED.add(job_id)
        try:
            executor.submit(_run_job, job_id)
        except RuntimeError:
            # 线程池在关（进程在退出）：不派发，作业留在队列里等下次启动
            _DISPATCHED.discard(job_id)
            return False
    return True


def run_job_inline(job_id: str) -> None:
    """测试 / 工具用：在当前线程里跑完一个作业（与工人线程同一路径）。"""
    with _EXECUTOR_LOCK:
        _DISPATCHED.add(job_id)
    _run_job(job_id)


def _release_interrupted(session: Session, service: StyleJobService, claimed: ClaimedJob) -> None:
    try:
        session.rollback()
        released = service.release(claimed)
        session.commit()
    except Exception:  # noqa: BLE001 — 放不回去就等清扫（心跳过期后重排）
        logger.exception("style job %s could not be released on interruption", claimed.job_id)
        return
    logger.info(
        "style job %s interrupted by worker shutdown; %s",
        claimed.job_id,
        "back in the queue (cursor kept)" if released else "no longer owned",
    )


def _run_job(job_id: str) -> None:
    from novel_system.services.style_reference.background_heartbeat import periodic_heartbeat

    generation = current_worker_generation()
    try:
        with _session_factory() as session:
            service = StyleJobService(session)
            claimed = service.claim(job_id)
            session.commit()
            if claimed is None:
                return
            claimed = dataclasses.replace(claimed, generation=generation)
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
                except JobInterrupted:
                    _release_interrupted(session, service, claimed)
                except JobCancelled:
                    session.rollback()
                    service.finish_cancelled(claimed)
                    session.commit()
                except JobLost:
                    session.rollback()
                    logger.info("style job %s lost ownership; worker stops", job_id)
                except DomainError as exc:
                    if is_worker_interruption(exc, claimed):
                        _release_interrupted(session, service, claimed)
                        return
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
                    if is_worker_interruption(exc, claimed):
                        _release_interrupted(session, service, claimed)
                        return
                    session.rollback()
                    logger.exception("style job %s failed", job_id)
                    service.fail(
                        claimed,
                        code=getattr(exc, "code", None) or JOB_FAILED_CODE,
                        message=f"{type(exc).__name__}: {exc}",
                        retryable=True,
                    )
                    session.commit()
                except (KeyboardInterrupt, SystemExit):
                    # Ctrl-C / 正常退出：进程要走了——作业放回队列（游标保留），再往上抛。被 SIGKILL 的进程什么也
                    # 做不了：作业留在 running，心跳过期后由清扫放回队列
                    _release_interrupted(session, service, claimed)
                    raise
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
    dispatched: list[str] = []
    for job_id in ids:
        kind = _job_kind(job_id)
        if kind and _HANDLERS.get(kind) and dispatch_job(job_id, kind=kind):
            dispatched.append(job_id)
    return dispatched


def _job_kind(job_id: str) -> str | None:
    try:
        with _session_factory() as session:
            job = session.get(StyleReferenceJob, job_id)
            return job.kind if job is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------- 维护任务（随清扫线程跑）
MaintenanceTask = Callable[[], Any]
_MAINTENANCE: dict[str, tuple[MaintenanceTask, float]] = {}
_MAINTENANCE_LAST_RUN: dict[str, float] = {}


def register_maintenance_task(name: str, task: MaintenanceTask, *, interval_seconds: float) -> None:
    """登记一项随清扫线程跑的定期维护任务（模块导入时登记，与处理器同一种约定）：清扫线程启动时先跑一次，之后每
    ``interval_seconds`` 秒跑一次。任务自己开会话、自己提交；抛出的异常由清扫线程记日志，不影响清扫与别的任务。
    同名重复登记覆盖（模块被重新导入时无害）。"""
    _MAINTENANCE[str(name)] = (task, max(1.0, float(interval_seconds)))


def run_due_maintenance(*, now: float | None = None) -> list[str]:
    """跑一遍到期的维护任务（``now`` 是单调时钟秒数，缺省当前）；返回这一轮跑了的任务名（失败的不算）。"""
    current = time.monotonic() if now is None else float(now)
    ran: list[str] = []
    for name, (task, interval) in list(_MAINTENANCE.items()):
        last = _MAINTENANCE_LAST_RUN.get(name)
        if last is not None and current - last < interval:
            continue
        _MAINTENANCE_LAST_RUN[name] = current
        try:
            task()
        except Exception:  # noqa: BLE001 — 维护任务失败只记日志，下一个间隔再试
            logger.exception("style job maintenance task %s failed", name)
            continue
        ran.append(name)
    return ran


def sweeper_tick(*, now: float | None = None) -> None:
    """清扫线程的一拍：清扫并派发，再跑到期的维护任务。"""
    sweep_and_dispatch()
    run_due_maintenance(now=now)


def start_job_sweeper(*, interval_seconds: float = SWEEP_INTERVAL_SECONDS) -> None:
    """常驻清扫线程（FastAPI lifespan 启动时调用一次；重复调用无害）。启动时先跑一拍（清扫 + 全部维护任务），
    之后每 ``interval_seconds`` 秒一拍（维护任务只在各自的间隔到期时才跑）。"""
    global _SWEEPER
    with _EXECUTOR_LOCK:
        if _SWEEPER is not None and _SWEEPER.is_alive():
            return
        _SWEEPER_STOP.clear()
        _MAINTENANCE_LAST_RUN.clear()

        def _loop() -> None:
            sweeper_tick()
            while not _SWEEPER_STOP.wait(max(1.0, float(interval_seconds))):
                sweeper_tick()

        _SWEEPER = threading.Thread(target=_loop, name="sr_job_sweeper", daemon=True)
        _SWEEPER.start()


def shutdown_job_workers(*, wait: bool = False) -> None:
    """lifespan 结束：停清扫、工人代 +1（在跑的处理器在下一个检查点把作业放回队列）、关线程池。

    ``wait=True`` 等在跑的处理器放回作业再返回（测试用）；缺省不等——它们在几秒内自己放回。"""
    global _SWEEPER, _GENERATION
    _SWEEPER_STOP.set()
    with _EXECUTOR_LOCK:
        _GENERATION += 1
        executors = list(_EXECUTORS.values())
        _EXECUTORS.clear()
        _SWEEPER = None
        _DISPATCHED.clear()
    for executor in executors:
        executor.shutdown(wait=wait, cancel_futures=True)


__all__ = [
    "ACTIVE_STATES",
    "BOOK_EXCLUSIVE_KINDS",
    "ClaimedJob",
    "DaemonCallPool",
    "JOB_ALREADY_ACTIVE_CODE",
    "JOB_CANCELLED_CODE",
    "JOB_FAILED_CODE",
    "JOB_KINDS",
    "JOB_KIND_CHECK",
    "JOB_KIND_CLASSIFY",
    "JOB_KIND_LEARN",
    "JOB_NOT_FOUND_CODE",
    "JOB_NOT_RESUMABLE_CODE",
    "JobCancelled",
    "JobInterrupted",
    "JobLost",
    "STALE_AFTER_SECONDS",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "StyleJobService",
    "TERMINAL_STATES",
    "already_active_error",
    "current_worker_generation",
    "dispatch_job",
    "heartbeat_is_stale",
    "is_worker_interruption",
    "job_activity_entry",
    "register_job_handler",
    "register_maintenance_task",
    "registered_job_handler",
    "run_cancel_hook",
    "run_due_maintenance",
    "run_job_inline",
    "shutdown_job_workers",
    "start_job_sweeper",
    "sweep_and_dispatch",
    "sweeper_tick",
    "worker_generation_changed",
]
