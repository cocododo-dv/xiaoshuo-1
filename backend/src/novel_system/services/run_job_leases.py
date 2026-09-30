"""场景 / 章节运行任务的共用词表与租约内核（叶子模块：只依赖 ``db`` 与 SQLAlchemy、``background_jobs``）。

两种任务都是 ``chapter_run_jobs`` 表里的一行（``job_type`` 区分），用同一套租约字段：
``worker_id`` / ``attempt_no`` / ``lease_expires_at``。认领是对「看到的那一行」（状态、attempt、worker、租约）
的条件更新，之后每一次写都以 ``worker_id + attempt_no`` 为 fence——丢了租约的旧工人的写全部落空。
每种任务「哪些状态可认领、失败能不能重领」的策略留在各自的服务里（B03-12 / B03-24）。

**进程退出**（B03-01）：本进程工人持有的租约登记在这里；lifespan 结束时工人代 +1（``background_jobs``），
之后这些租约不再续，并被就地到期（``release_held_leases``）——重启后的恢复立刻把任务接着跑，不必等 600 秒
的租约自然过期。进程里还在跑或排着队的任务（``busy_job_ids``）不会被周期恢复再派发一次。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import update
from sqlalchemy.orm import Session

from novel_system.cache_registry import register_cache_reset
from novel_system.db.models import ChapterRunJob
from novel_system.services.background_jobs import current_worker_generation, generation_changed
from novel_system.services.errors import DomainError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------- 任务类型 / 状态
JOB_TYPE_SCENE_FULL = "scene_run_full"
JOB_TYPE_CHAPTER_FULL = "chapter_run_full"
RUN_JOB_TYPES: tuple[str, ...] = (JOB_TYPE_SCENE_FULL, JOB_TYPE_CHAPTER_FULL)

# 等待中的状态两种任务叫法不同（场景 queued，章节 pending）；前端把 queued 归一成 pending。
STATUS_QUEUED = "queued"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_CANCEL_REQUESTED = "cancel_requested"
STATUS_CANCELLED = "cancelled"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
# 有工人正持有租约的状态（续约、进程退出时就地到期都认这两个）
OWNED_STATUSES: tuple[str, ...] = (STATUS_RUNNING, STATUS_CANCEL_REQUESTED)

# 工人车道（background_jobs.DaemonLane）：场景任务一次最多两条管线；恢复派发的无项目章节任务一次一章
SCENE_RUN_LANE = "scene_run"
SCENE_RUN_LANE_WORKERS = 2
CHAPTER_RUN_LANE = "chapter_run"
CHAPTER_RUN_LANE_WORKERS = 1

# ---------------------------------------------------------------------- 场景任务的步位词表（B03-03）
# ``current_step`` 对作者只说一套词：管线阶段（进行中）+ 几个停点。前端 ``ws-scene-derive.js`` 的
# RUN_JOB_STEP_LABELS 按这套词给中文标签；检查点节点名（budget_ready …）不出现在任务视图里。
SCENE_RUN_STAGE_ORDER: tuple[str, ...] = (
    "planning_running",
    "bundle_built",
    "neutral_running",
    "hard_qc_running",
    "style_running",
    "soft_qc_running",
    "rewrite_running",
    "acceptance_review_running",
    "near_final",
    "archived",
)
SCENE_STEP_CLAIMED = "planning_running"

# 检查点节点（scene_run_checkpoint.RUN_CHECKPOINT_ORDER）→ 存下这个节点之后正在进行的阶段。
# 风格稿的逐稿进度存在 hard_qc_ready 的 sub_index 上；归档尾巴的 8 个子步都存在 near_final_ready 上。
_CHECKPOINT_STAGES: dict[str, str] = {
    "budget_ready": "planning_running",
    "planning_ready": "bundle_built",
    "bundle_ready": "neutral_running",
    "neutral_ready": "hard_qc_running",
    "hard_qc_ready": "style_running",
    "style_ready": "soft_qc_running",
    "selection_wait": "awaiting_candidate_selection",
    "soft_qc_ready": "acceptance_review_running",
    "near_final_ready": "near_final",
    "archived": "archived",
}


def scene_job_step(token: str) -> str:
    """任务里记下的步位 → 作者词表：检查点节点换成进行中的阶段，其余（阶段词、停点、状态词）原样。"""
    return _CHECKPOINT_STAGES.get(token, token)


# ---------------------------------------------------------------------- 时间
def iso_now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def lease_expiry(lease_seconds: int, *, now: datetime | None = None) -> str:
    """从 ``now`` 起 ``lease_seconds`` 秒（至少 1 秒）后到期的租约时刻。"""
    return ((now or datetime.now(UTC)) + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()


def parse_iso(value: Any) -> datetime | None:
    """ISO-8601 → 带时区的 UTC 时刻；空值 / 格式不对 → None（缺时区按 UTC）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def lease_is_active(value: Any, *, now: datetime | None = None) -> bool:
    """租约还没到期。空值、格式坏掉的租约按「主人已死」算（不是永生的主人）。"""
    expires = parse_iso(value)
    return expires is not None and expires > (now or datetime.now(UTC))


# ---------------------------------------------------------------------- SQLite 写锁
def begin_immediate(session: Session) -> None:
    """SQLite 上立刻拿写锁（BEGIN IMMEDIATE）：先读后写的认领不会在 WAL 里把读升级成写时撞 SQLITE_BUSY。"""
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------- 认领 / 续约 / fence
def _is_or_eq(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def update_observed(session: Session, job: ChapterRunJob, *, job_type: str, values: dict[str, Any]) -> bool:
    """条件更新「看到的那一行」：状态、attempt、worker、租约都还与读到的一样才写；别人先改过 → False。"""
    changed = session.execute(
        update(ChapterRunJob)
        .where(
            ChapterRunJob.job_id == job.job_id,
            ChapterRunJob.job_type == job_type,
            ChapterRunJob.status == job.status,
            ChapterRunJob.attempt_no == int(job.attempt_no or 0),
            _is_or_eq(ChapterRunJob.worker_id, job.worker_id),
            _is_or_eq(ChapterRunJob.lease_expires_at, job.lease_expires_at),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return changed.rowcount == 1


def cas_claim(
    session: Session,
    job: ChapterRunJob,
    *,
    job_type: str,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
    clear_outcome: bool = False,
) -> str | None:
    """把看到的那一行改成 running / 归 ``worker_id`` / attempt +1（``update_observed``）。

    成功返回新租约的到期时刻，别人抢先改过这一行则返回 ``None``（调用方按自己的口径报错）。
    ``clear_outcome``：同时清掉上一次的结束时刻与错误（场景任务：失败可重领）。
    """
    current = now or datetime.now(UTC)
    now_iso = current.isoformat()
    expires = lease_expiry(lease_seconds, now=current)
    values: dict[str, Any] = {
        "status": STATUS_RUNNING,
        "worker_id": worker_id,
        "attempt_no": int(job.attempt_no or 0) + 1,
        "started_at": job.started_at or now_iso,
        "heartbeat_at": now_iso,
        "lease_expires_at": expires,
    }
    if clear_outcome:
        values.update(finished_at=None, error_code=None, error_text=None)
    return expires if update_observed(session, job, job_type=job_type, values=values) else None


@dataclass
class RunJobLease:
    """一次认领拿到的租约；``worker_id + attempt_no`` 是之后每一次写的 fence。

    ``renew`` 用认领时的会话；``renew_detached`` 自己开会话（长时间的模型调用期间由心跳线程调用，
    ``llm_task_runner`` 经绑定方法的 ``__self__`` 找到它）。进程要退出之后两者都不再续（B03-01）。
    每种任务的子类给出 ``job_type`` / 可续约的状态 / 丢租约时的说法。
    """

    job_id: str
    worker_id: str
    attempt_no: int
    lease_expires_at: str
    _session: Session = field(repr=False, compare=False)
    # 认领时的工人代；lifespan 结束（工人代 +1）后不再续约
    generation: int | None = field(default_factory=current_worker_generation, repr=False, compare=False)

    job_type: ClassVar[str] = ""
    renewable_statuses: ClassVar[tuple[str, ...]] = (STATUS_RUNNING,)
    lost_message: ClassVar[str] = "run job owner lease was lost"

    def renew(self, *, lease_seconds: int) -> str:
        if generation_changed(self.generation):
            return self.lease_expires_at
        self.lease_expires_at = self._renew_in(self._session, lease_seconds)
        return self.lease_expires_at

    def renew_detached(self, *, lease_seconds: int) -> str:
        """Renew with an independent session for long provider calls."""
        if generation_changed(self.generation):
            return self.lease_expires_at
        from novel_system.db.session import SessionLocal

        with SessionLocal() as session:
            expires = self._renew_in(session, lease_seconds)
            session.commit()
            return expires

    def lost(self, message: str | None = None, **details: Any) -> DomainError:
        return DomainError(
            "RUN_OWNER_LEASE_LOST",
            message or self.lost_message,
            status_code=409,
            details={"job_id": self.job_id, "worker_id": self.worker_id, "attempt_no": self.attempt_no, **details},
        )

    def owned_by_me(self, *statuses: str) -> tuple[Any, ...]:
        """「这一行还归我、状态是 ``statuses`` 之一」的条件（条件更新用）。"""
        return (
            ChapterRunJob.job_id == self.job_id,
            ChapterRunJob.job_type == self.job_type,
            ChapterRunJob.status.in_(statuses),
            ChapterRunJob.worker_id == self.worker_id,
            ChapterRunJob.attempt_no == self.attempt_no,
        )

    def update_owned(self, session: Session, *, statuses: tuple[str, ...], values: dict[str, Any]) -> bool:
        """条件更新自己的那一行；别的工人已接手 / 状态变了 → False（不抛）。"""
        changed = session.execute(
            update(ChapterRunJob)
            .where(*self.owned_by_me(*statuses))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return changed.rowcount == 1

    def _renew_in(self, session: Session, lease_seconds: int) -> str:
        now = datetime.now(UTC)
        expires = lease_expiry(lease_seconds, now=now)
        if not self.update_owned(
            session,
            statuses=self.renewable_statuses,
            values={"heartbeat_at": now.isoformat(), "lease_expires_at": expires},
        ):
            session.rollback()
            raise self.lost()
        session.flush()
        return expires


# ---------------------------------------------------------------------- 本进程持有的租约（B03-01）
_HELD: dict[str, RunJobLease] = {}
_DISPATCHED: set[str] = set()
_REGISTRY_LOCK = threading.Lock()


def _reset_registry() -> None:
    with _REGISTRY_LOCK:
        _HELD.clear()
        _DISPATCHED.clear()


# 任务 id 按库而定：测试每个用例一个新库、反复用同样的 id
register_cache_reset("run_job_leases.held", _reset_registry)


def hold_lease(lease: RunJobLease) -> None:
    """工人拿到租约：登记，进程退出时就地到期。"""
    with _REGISTRY_LOCK:
        _HELD[lease.job_id] = lease


def drop_lease(lease: RunJobLease | None) -> None:
    """工人结束：注销（只注销自己那一次认领的登记）。"""
    if lease is None:
        return
    with _REGISTRY_LOCK:
        held = _HELD.get(lease.job_id)
        if held is not None and (held.worker_id, held.attempt_no) == (lease.worker_id, lease.attempt_no):
            del _HELD[lease.job_id]


def mark_dispatched(job_id: str) -> bool:
    """任务交给本进程的工人车道（排着队或在跑）；已经交过 → False（不重复派发）。"""
    with _REGISTRY_LOCK:
        if job_id in _DISPATCHED:
            return False
        _DISPATCHED.add(job_id)
        return True


def unmark_dispatched(job_id: str) -> None:
    with _REGISTRY_LOCK:
        _DISPATCHED.discard(job_id)


def busy_job_ids() -> set[str]:
    """本进程里排着队或正持有租约的任务：周期恢复不再派发它们。"""
    with _REGISTRY_LOCK:
        return set(_DISPATCHED) | set(_HELD)


def release_held_leases() -> list[str]:
    """进程要退出：本进程工人持有的租约就地到期（条件在 worker_id / attempt_no 上），返回到期了的任务。

    先把工人代 +1（``background_jobs.bump_worker_generation``）再调它：这之后工人不再续约，到期的租约不会被
    心跳线程又续上。登记不清：工人线程还活着时（同一进程里下一个 lifespan，测试里会有）它们仍算「本进程在跑」，
    不会被再派发一次；工人结束时自己注销。
    """
    from novel_system.db.session import SessionLocal

    with _REGISTRY_LOCK:
        leases = list(_HELD.values())
    released: list[str] = []
    for lease in leases:
        try:
            with SessionLocal() as session:
                if lease.update_owned(session, statuses=OWNED_STATUSES, values={"lease_expires_at": iso_now()}):
                    released.append(lease.job_id)
                session.commit()
        except Exception:  # noqa: BLE001 — 进程退出路径：到期不了就等租约自然过期
            logger.exception("run job %s lease could not be released on shutdown", lease.job_id)
    return released
