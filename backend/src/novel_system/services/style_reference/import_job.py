"""参考书段落分类作业(2026-09-23 风格参考 v3:跑在统一作业表上,kind=classify)。

严格 LLM(2026-09-15):每一段都由 LLM 分类,没有启发式兜底。导入 / 重新分类 / 就地重分类的请求只做
准备工作(导入:解码、切段、安全扫描、批量落书与段落行),然后建一个 ``classify`` 作业;这里的处理器
(``register_job_handler("classify", ...)``,本模块导入时注册)在作业工人线程里逐批分类。

**作业表取代了旧的书上 JSON 游标状态机**(``stats_json["classification"]``、自己的单线程执行器、心跳
线程、``recover_classification_jobs``):

- 游标在 ``job.cursor_json``(见 ``_new_cursor``):锚定集(全书分层抽样的段落位置)、当前阶段、每阶段
  已完成的位置区间、快模型在锚定集上的结果、一致率与余段节点、批数 / 调用 / 重试 / 每批耗时;
- 进度写 ``job.progress_json``(``StyleJobService.progress``),活动面板读 ``job_activity_entry``;
- 取消:``request_cancel``——排队中或工人已死(心跳过期)的作业在请求里直接收尾,运行中的由处理器在
  下一个检查点(``check_continue``)看到后收尾;
- 恢复:心跳过期的 running 由常驻清扫线程放回 queued 再派发(``jobs.start_job_sweeper``),从游标续跑;
- 所有权:工人的每一次写(游标、段落类型、进度、结束)都以「owner_token 仍是我」为条件——游标的条件写
  是每批事务里的第一条写,之后才写段落行,提交是原子的;删书(``cancel_all_for_book``)、清扫重排、取消
  之后,旧工人的写全部落空,工人据此停下(v3 I2:删书重导入不会再让旧线程把「仅本机」的书发到云端);
- 每批派发前重查:作业仍归我、没被取消、书还在、书的云策略仍允许这个节点的实际路由(v3 I2 / I7)。

**分批与并行**(v3 I13 / I16):按字数自适应分批(≤6,000 字且 ≤100 段,见 ``segmentation.llm``),
最多 ``PARALLEL_BATCHES`` 批并行——LLM 调用在工人线程里(各用各的记账会话),写库只在作业线程里;
每批失败(调用失败 / 输出对不上)按 ``BATCH_RETRY_BACKOFF_SECONDS`` 退避重试两次,仍失败才让作业失败
(``STYLE_REFERENCE_CLASSIFICATION_FAILED`` 502,游标保留,可「继续分类」)。路由与提示词模板每个作业
只载一次;LLM 客户端在作业开始时按当前配置取(v3 I6,不在请求里捕获)。

**三种模式**(``params.mode``):

- ``import``   新导入的书(段落行初始类型 ``unclassified``);书 ``ingesting`` → 成功 ``ready`` / 失败或取消 ``failed``;
- ``reclassify`` 破坏式重新分类:请求里先清掉派生数据(抽取 / 画像 / 绑定 / 窗口 / 作业);状态同 import;
- ``retype``   就地重分类(v3 I5 / L7):正文不变、只重标段落类型,不删抽取 / 画像 / 绑定;书全程保持
               ``ready``(绑定照常用),失败 / 取消也回到 ``ready``(已写的新类型保留,可继续)。

每次成功:``stats_json["paragraph_types_revision"]`` +1、``classification_provenance`` 记来源
(``llm`` + 份额 + 提示词版本 + 时间),并从段落行重算指标 / 段型分布(导入与破坏式重分类还重算声音签名)。
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from novel_system.db.models import (
    LlmCall,
    StyleReferenceBook,
    StyleReferenceJob,
    StyleReferenceParagraph,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import (
    ClassificationFailedError,
    EmptyBookError,
    LLMRequiredError,
)
from novel_system.services.style_reference.jobs import (
    ACTIVE_STATES,
    JOB_FAILED_CODE,
    JOB_KIND_CLASSIFY,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    ClaimedJob,
    JobCancelled,
    JobLost,
    StyleJobService,
    heartbeat_is_stale,
    job_activity_entry,
    register_job_handler,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.segmentation import llm as seg
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)

logger = logging.getLogger(__name__)

MODE_IMPORT = "import"
MODE_RECLASSIFY = "reclassify"
MODE_RETYPE = "retype"
CLASSIFY_MODES: tuple[str, ...] = (MODE_IMPORT, MODE_RECLASSIFY, MODE_RETYPE)

UNCLASSIFIED_PARAGRAPH_TYPE = "unclassified"
CURSOR_VERSION = 1

PHASE_ANCHOR_STRONG = "anchor_strong"
PHASE_ANCHOR_FAST = "anchor_fast"
PHASE_REST = "rest"
PHASE_FINALIZE = "finalize"
_PHASE_ORDER: tuple[str, ...] = (PHASE_ANCHOR_STRONG, PHASE_ANCHOR_FAST, PHASE_REST)
_PHASE_LABELS: dict[str, str] = {
    PHASE_ANCHOR_STRONG: "锚定集 · 强模型",
    PHASE_ANCHOR_FAST: "锚定集 · 快模型对照",
    PHASE_REST: "余段分类",
    PHASE_FINALIZE: "统计与写入",
}
_MODE_LABELS: dict[str, str] = {
    MODE_IMPORT: "导入",
    MODE_RECLASSIFY: "重新分类",
    MODE_RETYPE: "重标段落类型",
}

PARALLEL_BATCHES = 3
BATCH_ATTEMPTS = 3  # 1 次 + 2 次重试
BATCH_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0)
WAIT_POLL_SECONDS = 2.0
BATCH_SECONDS_KEPT = 64

_RETRYABLE_BATCH_CODES = frozenset(
    {"STYLE_REFERENCE_CLASSIFY_LLM_CALL_FAILED", "STYLE_REFERENCE_CLASSIFY_OUTPUT_MISMATCH"}
)

CLASSIFICATION_ALREADY_ACTIVE_CODE = "STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE"
NOTHING_TO_RESUME_CODE = "STYLE_REFERENCE_CLASSIFICATION_NOTHING_TO_RESUME"

# 费用预估的默认值(本地账本里没有分类节点的记录时用;有记录时换成账本里的中位数):
# - 输入:每个请求字符 ≈ 0.52 token(2026-09-15 实库账本 162 次分类调用的中位数 0.518);
# - 输出:一段的分类结果(一条 JSON)≈ 100–110 字符 ≈ 32 token(关推理后几乎只有这些);
# - 吞吐:输出 ≈ 190 token/s(同一账本的完成 token / 延迟中位数 196);每次调用固定开销 3 s。
DEFAULT_INPUT_TOKENS_PER_CHAR = 0.52
DEFAULT_OUTPUT_TOKENS_PER_PARAGRAPH = 32.0
DEFAULT_OUTPUT_TOKENS_PER_SECOND = 190.0
DEFAULT_CALL_OVERHEAD_SECONDS = 3.0
ESTIMATE_LEDGER_SAMPLE = 200


def resolve_classification_client() -> tuple[Any | None, bool]:
    """作业开始时按**当前**运行时配置取 LLM 客户端(v3 I6:不在请求里捕获);测试在这里打桩。"""
    from novel_system.services.system_config import build_runtime_llm_client
    from novel_system.settings import get_settings

    return build_runtime_llm_client(settings=get_settings())


# ---------------------------------------------------------------- 建 / 续 / 取消


def _job_mode(job: Any) -> str:
    params = getattr(job, "params_json", None)
    if params is None:
        params = getattr(job, "params", None)
    mode = str((params or {}).get("mode") or MODE_IMPORT)
    return mode if mode in CLASSIFY_MODES else MODE_IMPORT


def _status_after_failure(mode: str) -> str:
    """失败 / 取消后书的状态:就地重分类回到 ready(书本来就完整可用),其余 failed。"""
    return "ready" if mode == MODE_RETYPE else "failed"


def latest_classification_job(session: Session, book_id: str) -> StyleReferenceJob | None:
    return StyleJobService(session).latest_for_book(book_id, kind=JOB_KIND_CLASSIFY)


def active_classification_job(session: Session, book_id: str) -> StyleReferenceJob | None:
    active = StyleJobService(session).active_for_book(book_id, kind=JOB_KIND_CLASSIFY)
    return active[0] if active else None


def _raise_already_active(job: StyleReferenceJob, book_id: str) -> None:
    raise DomainError(
        CLASSIFICATION_ALREADY_ACTIVE_CODE,
        "这本书正在分类:等它完成,或先取消。",
        status_code=409,
        details={"book_id": book_id, "job_id": job.job_id, "op_key": job.op_key, "state": job.state},
    )


def create_classification_job(
    session: Session,
    book: StyleReferenceBook,
    *,
    mode: str,
    op_key: str | None = None,
) -> StyleReferenceJob:
    """给一本书建分类作业(queued)。已有排队 / 运行中的分类作业 → 409。调用方提交后派发。"""
    if mode not in CLASSIFY_MODES:
        raise ValueError(f"unknown classification mode {mode!r}")
    existing = active_classification_job(session, book.book_id)
    if existing is not None:
        _raise_already_active(existing, book.book_id)
    job = StyleJobService(session).create(
        JOB_KIND_CLASSIFY,
        book_id=book.book_id,
        op_key=op_key or None,
        params={"mode": mode},
        phase="queued",
    )
    job.progress_json = {"phase": "queued", "phase_label": "排队中", "mode": mode}
    if mode != MODE_RETYPE:
        book.status = "ingesting"
    stats = dict(book.stats_json or {})
    if "classification" in stats:
        # 2026-09-15 的书上 JSON 游标状态机已退役,作业表是唯一的进度真源。
        stats.pop("classification", None)
        book.stats_json = stats
    session.flush()
    return job


def resume_classification(
    session: Session,
    book: StyleReferenceBook,
    *,
    op_key: str | None = None,
) -> StyleReferenceJob:
    """「继续分类」:把这本书最近一次失败 / 取消 / 工人已死的分类作业放回队列,从游标续跑。

    进程重启之后也能续(游标在作业行上)。老版本留下的「分类没完成」的书(没有作业行,书还是
    failed / ingesting / cancelling)会新建一个从头分类的作业(不清派生数据——那些书也没有派生数据)。
    """
    service = StyleJobService(session)
    latest = service.latest_for_book(book.book_id, kind=JOB_KIND_CLASSIFY)
    if latest is None or latest.state == STATE_SUCCEEDED:
        if str(book.status or "") == "ready":
            raise DomainError(
                NOTHING_TO_RESUME_CODE,
                "这本书的段落分类已经完成,没有可以继续的分类。",
                status_code=409,
                details={"book_id": book.book_id},
            )
        return create_classification_job(session, book, mode=MODE_IMPORT, op_key=op_key)
    if latest.state in ACTIVE_STATES and not (
        latest.state == STATE_RUNNING and heartbeat_is_stale(latest.heartbeat_at)
    ):
        if latest.state == STATE_RUNNING:
            _raise_already_active(latest, book.book_id)
        job = latest  # 已经在排队:再派发一次即可
    else:
        job = service.requeue(latest.job_id)
    if op_key:
        job.op_key = op_key
    progress = dict(job.progress_json or {})
    progress.update({"phase_label": "排队中", "resumed_at": utcnow()})
    job.progress_json = progress
    if _job_mode(job) != MODE_RETYPE:
        book.status = "ingesting"
    session.flush()
    return job


def cancel_classification(session: Session, book_id: str) -> StyleReferenceJob | None:
    """取消这本书正在排队 / 运行的分类作业。排队中或工人已死的作业在这里直接收尾(书的状态一并
    落定);运行中的由工人在下一个检查点收尾。没有活动作业时返回 None。"""
    job = active_classification_job(session, book_id)
    if job is None:
        return None
    job = StyleJobService(session).request_cancel(job.job_id)
    if job.state == STATE_CANCELLED:
        book = session.get(StyleReferenceBook, book_id)
        if book is not None:
            book.status = _status_after_failure(_job_mode(job))
        session.flush()
    return job


def count_paragraphs(session: Session, book_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(StyleReferenceParagraph)
            .where(StyleReferenceParagraph.book_id == book_id)
        )
        or 0
    )


# ---------------------------------------------------------------- cursor helpers


def _add_ranges(ranges: Sequence[Sequence[int]], positions: Sequence[int]) -> list[list[int]]:
    """把位置并入闭区间列表(合并相邻 / 重叠)。"""
    points = sorted({int(p) for p in positions})
    merged: list[list[int]] = [[int(a), int(b)] for a, b in ranges]
    for point in points:
        merged.append([point, point])
    merged.sort()
    out: list[list[int]] = []
    for start, end in merged:
        if out and start <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def _in_ranges(ranges: Sequence[Sequence[int]]) -> set[int]:
    covered: set[int] = set()
    for start, end in ranges:
        covered.update(range(int(start), int(end) + 1))
    return covered


def _new_cursor(*, anchors: list[int], total: int, same_route: bool) -> dict[str, Any]:
    return {
        "version": CURSOR_VERSION,
        "paragraphs_total": int(total),
        "anchor_positions": list(anchors),
        "phase": PHASE_ANCHOR_STRONG,
        "done": {phase: [] for phase in _PHASE_ORDER},
        "anchor_fast_types": {},
        "same_route": bool(same_route),
        "agreement": None,
        "rest_node": None,
        "calibration_skipped": None,
        "batches_done": 0,
        "batches_total": 0,
        "llm_calls": 0,
        "retries": 0,
        "batch_seconds": [],
        "started_at": utcnow(),
    }


@dataclass
class _BatchOutcome:
    results: dict[int, tuple[str, float]]
    seconds: float
    attempts: int


class _Stopped(Exception):
    """作业已停(取消 / 失败 / 丢了所有权),工人线程里还没开始的重试不再发。"""


# ---------------------------------------------------------------- the handler


class _ClassificationRun:
    def __init__(self, session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
        self.session = session
        self.claimed = claimed
        self.service = service
        self.book_id = str(claimed.book_id or "")
        self.mode = _job_mode(claimed)
        self.client: Any = None
        self.runtimes: dict[str, seg.NodeRuntime] = {}
        self.ids: list[str] = []
        self.indexes: list[int] = []
        self.texts: list[str] = []
        self.spans: list[tuple[int, int]] = []
        self.cursor: dict[str, Any] = {}

    # ---- lifecycle ------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except JobLost:
            self.session.rollback()
            raise
        except JobCancelled:
            self._finish_cancelled()
        except DomainError as exc:
            self._finish_failed(
                code=exc.code,
                message=str(exc.message),
                retryable=bool(getattr(exc, "retryable", False) or (exc.details or {}).get("retryable")),
                details=exc.details if isinstance(exc.details, Mapping) else None,
            )
        except Exception as exc:  # noqa: BLE001 — 作业边界:记失败,书的状态一并落定
            logger.exception("classification job %s failed", self.claimed.job_id)
            self._finish_failed(
                code=str(getattr(exc, "code", None) or JOB_FAILED_CODE),
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )

    def _set_book_status(self, status: str) -> None:
        self.session.execute(
            update(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == self.book_id)
            .values(status=status, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )

    def _finish_cancelled(self) -> None:
        self.session.rollback()
        if not self.service.finish_cancelled(self.claimed):
            self.session.rollback()
            return
        self._set_book_status(_status_after_failure(self.mode))
        self.session.commit()

    def _finish_failed(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.session.rollback()
        if not self.service.fail(
            self.claimed, code=code, message=message, retryable=retryable, details=details
        ):
            self.session.rollback()
            return
        self._set_book_status(_status_after_failure(self.mode))
        self.session.commit()

    def _fresh(self) -> None:
        """结束当前读事务,之后的检查读到别的连接刚提交的取消 / 删书。"""
        self.session.commit()

    def _check_continue(self) -> None:
        self._fresh()
        self.service.check_continue(self.claimed)

    def _pre_call_check(self, runtime: seg.NodeRuntime) -> None:
        """每批派发前:作业仍归我且没被取消、书还在、书的云策略仍允许这个节点的实际路由。"""
        self._check_continue()
        book = self.session.execute(
            select(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == self.book_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if book is None:
            raise JobLost(self.claimed.job_id)
        ensure_cloud_llm_allowed(
            book,
            operation="classify_book",
            routes={runtime.node_id: runtime.route},
            llm_client=self.client,
        )

    # ---- main -----------------------------------------------------------
    def _run(self) -> None:
        self._check_continue()
        book = self.session.get(StyleReferenceBook, self.book_id)
        if book is None:
            raise JobLost(self.claimed.job_id)
        self.service.progress(self.claimed, phase="prepare", phase_label="准备分类")
        self.session.commit()

        client, enabled = resolve_classification_client()
        if not enabled or client is None:
            raise LLMRequiredError(operation="classify_book")
        self.client = client
        try:
            self.runtimes = seg.load_classification_runtimes()
        except seg.SegmentationLLMError as exc:
            raise ClassificationFailedError(
                code=exc.code, message=exc.message, book_id=self.book_id, details=exc.details
            ) from exc

        rows = self.session.execute(
            select(
                StyleReferenceParagraph.paragraph_id,
                StyleReferenceParagraph.paragraph_index,
                StyleReferenceParagraph.text,
                StyleReferenceParagraph.start_offset,
                StyleReferenceParagraph.end_offset,
            )
            .where(StyleReferenceParagraph.book_id == self.book_id)
            .order_by(StyleReferenceParagraph.paragraph_index)
        ).all()
        if not rows:
            raise EmptyBookError("classification")
        self.ids = [str(row.paragraph_id) for row in rows]
        self.indexes = [int(row.paragraph_index) for row in rows]
        self.texts = [str(row.text or "") for row in rows]
        self.spans = [(int(row.start_offset or 0), int(row.end_offset or 0)) for row in rows]

        anchor_rt = self.runtimes[seg.NODE_ANCHOR]
        bulk_rt = self.runtimes[seg.NODE_BULK]
        cursor = dict(self.claimed.cursor or {})
        if int(cursor.get("version") or 0) != CURSOR_VERSION or int(
            cursor.get("paragraphs_total") or -1
        ) != len(self.texts):
            anchors = seg.select_anchor_positions(self.texts, seed=self.book_id)
            cursor = _new_cursor(
                anchors=anchors, total=len(self.texts), same_route=seg.same_model(anchor_rt, bulk_rt)
            )
        self.cursor = cursor
        anchors = [int(p) for p in cursor["anchor_positions"]]
        anchor_set = set(anchors)
        rest = [pos for pos in range(len(self.texts)) if pos not in anchor_set]

        # 批数总量 = 已完成 + 各阶段剩余(分批只看字数,与余段走哪个节点无关)
        plans: dict[str, list[list[int]]] = {
            PHASE_ANCHOR_STRONG: self._remaining_plan(PHASE_ANCHOR_STRONG, anchors),
            PHASE_ANCHOR_FAST: (
                self._remaining_plan(PHASE_ANCHOR_FAST, anchors)
                if rest and not cursor.get("same_route") and cursor.get("rest_node") is None
                else []
            ),
            PHASE_REST: self._remaining_plan(PHASE_REST, rest),
        }
        cursor["batches_total"] = int(cursor.get("batches_done") or 0) + sum(len(p) for p in plans.values())
        self._save_cursor(
            progress=dict(
                phase="classify",
                phase_label=self._phase_label(),
                done=int(cursor["batches_done"]),
                total=int(cursor["batches_total"]),
                extra={
                    "phase_started_at": utcnow(),
                    "phase_done_at_start": int(cursor["batches_done"]),
                    "paragraphs_total": len(self.texts),
                    "mode": self.mode,
                    "attempt": self.claimed.attempt,
                },
            )
        )

        # 1. 锚定集 × 强模型(结果直接落段落行)
        self._set_phase(PHASE_ANCHOR_STRONG)
        self._run_batches(PHASE_ANCHOR_STRONG, anchor_rt, plans[PHASE_ANCHOR_STRONG])

        # 2. 锚定集 × 快模型对照(两个节点是同一个模型 / 没有余段时跳过)
        if rest and cursor.get("rest_node") is None:
            if cursor.get("same_route"):
                cursor["calibration_skipped"] = "same_route"
                cursor["rest_node"] = seg.NODE_BULK
            else:
                self._set_phase(PHASE_ANCHOR_FAST)
                self._run_batches(PHASE_ANCHOR_FAST, bulk_rt, plans[PHASE_ANCHOR_FAST])
                strong = self._current_types(anchors)
                fast = {int(k): str(v) for k, v in (cursor.get("anchor_fast_types") or {}).items()}
                agreement = seg.agreement(strong, fast)
                cursor["agreement"] = agreement
                cursor["rest_node"] = (
                    seg.NODE_BULK
                    if agreement is not None and agreement >= seg.AGREEMENT_THRESHOLD
                    else seg.NODE_ANCHOR
                )
            self._save_cursor()
        elif not rest:
            cursor["calibration_skipped"] = cursor.get("calibration_skipped") or "no_rest"

        # 3. 余段
        if rest:
            self._set_phase(PHASE_REST)
            rest_rt = self.runtimes[str(cursor.get("rest_node") or seg.NODE_BULK)]
            self._run_batches(PHASE_REST, rest_rt, plans[PHASE_REST])

        self._finalize(anchors=anchors, rest=rest)

    # ---- planning / cursor ---------------------------------------------
    def _remaining_plan(self, phase: str, positions: Sequence[int]) -> list[list[int]]:
        done = _in_ranges(self.cursor.get("done", {}).get(phase) or [])
        remaining = [pos for pos in positions if pos not in done]
        return seg.plan_batches(remaining, self.texts)

    def _phase_label(self) -> str:
        phase = str(self.cursor.get("phase") or PHASE_ANCHOR_STRONG)
        return f"段落分类 · {_PHASE_LABELS.get(phase, phase)}"

    def _set_phase(self, phase: str) -> None:
        """进入一个阶段(只前进不后退:续跑时已完成的阶段不再改写进度文案)。"""
        order = (*_PHASE_ORDER, PHASE_FINALIZE)
        current = str(self.cursor.get("phase") or PHASE_ANCHOR_STRONG)
        if current == phase or (current in order and order.index(current) > order.index(phase)):
            return
        self.cursor["phase"] = phase
        self._save_cursor(progress=dict(phase_label=self._phase_label()))

    def _save_cursor(self, *, progress: Mapping[str, Any] | None = None) -> None:
        self._fresh()
        if not self.service.save_cursor(self.claimed, self.cursor):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        if progress is not None:
            self.service.progress(self.claimed, **dict(progress))
        self.session.commit()

    def _current_types(self, positions: Sequence[int]) -> dict[int, str]:
        wanted = {self.ids[pos]: self.indexes[pos] for pos in positions}
        rows = self.session.execute(
            select(StyleReferenceParagraph.paragraph_id, StyleReferenceParagraph.paragraph_type).where(
                StyleReferenceParagraph.paragraph_id.in_(list(wanted))
            )
        ).all()
        return {wanted[str(row.paragraph_id)]: str(row.paragraph_type or "") for row in rows}

    # ---- batches --------------------------------------------------------
    def _run_batches(self, phase: str, runtime: seg.NodeRuntime, batches: list[list[int]]) -> None:
        if not batches:
            return
        pending: deque[list[int]] = deque(batches)
        in_flight: dict[Future, list[int]] = {}
        stop = threading.Event()
        pool = ThreadPoolExecutor(
            max_workers=PARALLEL_BATCHES, thread_name_prefix=f"sr_classify_{self.claimed.job_id[-6:]}"
        )
        try:
            while pending or in_flight:
                while pending and len(in_flight) < PARALLEL_BATCHES:
                    positions = pending.popleft()
                    self._pre_call_check(runtime)
                    future = pool.submit(self._call_with_retries, phase, runtime, positions, stop)
                    in_flight[future] = positions
                done, _ = wait(list(in_flight), timeout=WAIT_POLL_SECONDS, return_when=FIRST_COMPLETED)
                if not done:
                    self._check_continue()
                    continue
                failure: BaseException | None = None
                for future in sorted(done, key=lambda item: in_flight[item][0]):
                    positions = in_flight.pop(future)
                    error = future.exception()
                    if error is not None:
                        failure = failure or error
                        continue
                    self._apply(phase, positions, future.result())
                if failure is not None:
                    raise failure
        finally:
            stop.set()
            pool.shutdown(wait=False, cancel_futures=True)

    def _call_with_retries(
        self,
        phase: str,
        runtime: seg.NodeRuntime,
        positions: list[int],
        stop: threading.Event,
    ) -> _BatchOutcome:
        """工人线程:一批最多 ``BATCH_ATTEMPTS`` 次(退避重试);记账用自己的会话。"""
        from novel_system.db.session import SessionLocal

        problems: list[str] = []
        last: seg.SegmentationLLMError | None = None
        first_index = self.indexes[positions[0]]
        for attempt in range(BATCH_ATTEMPTS):
            if stop.is_set():
                raise _Stopped()
            if attempt:
                delay = BATCH_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(BATCH_RETRY_BACKOFF_SECONDS) - 1)]
                if stop.wait(max(0.0, float(delay))):
                    raise _Stopped()
            began = time.monotonic()
            try:
                with SessionLocal() as ledger_session:
                    results = seg.classify_batch(
                        runtime,
                        positions,
                        self.texts,
                        self.indexes,
                        self.client,
                        session=ledger_session,
                        scope_id=self.book_id,
                        step=f"paragraph_classification:{phase}:{first_index}:{len(positions)}",
                    )
                return _BatchOutcome(
                    results=results, seconds=round(time.monotonic() - began, 2), attempts=attempt + 1
                )
            except seg.SegmentationLLMError as exc:
                last = exc
                problems.append(f"attempt {attempt + 1}: {exc.code}: {exc.message[:300]}")
                if exc.code not in _RETRYABLE_BATCH_CODES:
                    break
                logger.warning(
                    "classification batch %s:%s (node %s) attempt %d failed: %s",
                    phase,
                    first_index,
                    runtime.node_id,
                    attempt + 1,
                    exc.code,
                )
        assert last is not None
        raise ClassificationFailedError(
            code=last.code,
            message=last.message,
            book_id=self.book_id,
            details={
                "phase": phase,
                "node_id": runtime.node_id,
                "first_paragraph_index": first_index,
                "paragraphs": len(positions),
                "attempts": len(problems),
                "problems": problems,
                **{k: v for k, v in last.details.items() if k in ("problem_count", "expected", "received")},
            },
        )

    def _apply(self, phase: str, positions: list[int], outcome: _BatchOutcome) -> None:
        """作业线程:游标(条件写,本事务第一条写)→ 段落类型 → 进度,一次提交。"""
        cursor = self.cursor
        done = dict(cursor.get("done") or {})
        done[phase] = _add_ranges(done.get(phase) or [], positions)
        cursor["done"] = done
        cursor["batches_done"] = int(cursor.get("batches_done") or 0) + 1
        cursor["llm_calls"] = int(cursor.get("llm_calls") or 0) + outcome.attempts
        cursor["retries"] = int(cursor.get("retries") or 0) + max(0, outcome.attempts - 1)
        cursor["batch_seconds"] = (list(cursor.get("batch_seconds") or []) + [outcome.seconds])[
            -BATCH_SECONDS_KEPT:
        ]
        if phase == PHASE_ANCHOR_FAST:
            fast = dict(cursor.get("anchor_fast_types") or {})
            fast.update({str(index): ptype for index, (ptype, _conf) in outcome.results.items()})
            cursor["anchor_fast_types"] = fast
        self._fresh()
        if not self.service.save_cursor(self.claimed, cursor):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        if phase != PHASE_ANCHOR_FAST:
            by_index = {self.indexes[pos]: pos for pos in positions}
            try:
                self.session.execute(
                    update(StyleReferenceParagraph),
                    [
                        {
                            "paragraph_id": self.ids[by_index[index]],
                            "paragraph_type": ptype,
                            "classifier_confidence": float(conf),
                        }
                        for index, (ptype, conf) in outcome.results.items()
                    ],
                )
            except StaleDataError as exc:
                # 段落行没了 = 书在这一批期间被删(删书同事务里也取消了作业,正常情况下走不到这里)
                self.session.rollback()
                raise JobLost(self.claimed.job_id) from exc
        seconds = [float(s) for s in cursor["batch_seconds"]]
        self.service.progress(
            self.claimed,
            done=int(cursor["batches_done"]),
            total=int(cursor["batches_total"]),
            detail=f"第 {cursor['batches_done']}/{cursor['batches_total']} 批",
            llm_calls_delta=outcome.attempts,
            extra={
                "retries": int(cursor["retries"]),
                "batch_seconds_mean": round(sum(seconds) / len(seconds), 2) if seconds else None,
            },
        )
        self.session.commit()

    # ---- finalize -------------------------------------------------------
    def _finalize(self, *, anchors: list[int], rest: list[int]) -> None:
        from novel_system.services.style_reference.classification_stats import (
            compute_classification_stats,
        )

        cursor = self.cursor
        self._set_phase(PHASE_FINALIZE)
        self._check_continue()
        covered = _in_ranges(cursor["done"].get(PHASE_ANCHOR_STRONG) or []) | _in_ranges(
            cursor["done"].get(PHASE_REST) or []
        )
        missing = [pos for pos in range(len(self.texts)) if pos not in covered]
        if missing:
            raise ClassificationFailedError(
                code="STYLE_REFERENCE_CLASSIFY_INCOMPLETE",
                message=f"{len(missing)} paragraph(s) were never classified",
                book_id=self.book_id,
                details={"missing": len(missing), "first_missing_index": self.indexes[missing[0]]},
            )
        rows = self.session.execute(
            select(
                StyleReferenceParagraph.paragraph_index,
                StyleReferenceParagraph.paragraph_type,
                StyleReferenceParagraph.classifier_confidence,
            )
            .where(StyleReferenceParagraph.book_id == self.book_id)
            .order_by(StyleReferenceParagraph.paragraph_index)
        ).all()
        if len(rows) != len(self.texts) or any(
            str(row.paragraph_type or "") not in seg.VALID_PARAGRAPH_TYPES for row in rows
        ):
            raise ClassificationFailedError(
                code="STYLE_REFERENCE_CLASSIFY_INCOMPLETE",
                message="paragraph table changed or still holds unclassified rows",
                book_id=self.book_id,
            )
        rest_node = cursor.get("rest_node")
        calibration = seg.build_calibration(
            anchor_size=len(anchors),
            total=len(rows),
            fast_model_agreement=cursor.get("agreement"),
            fallback_to_strong=bool(rest and rest_node == seg.NODE_ANCHOR),
            rest_classifier=(
                None if not rest else ("strong_llm" if rest_node == seg.NODE_ANCHOR else "fast_llm")
            ),
            calibration_skipped=cursor.get("calibration_skipped"),
        )
        classifications = [
            ParagraphClassification(
                paragraph_index=int(row.paragraph_index),
                paragraph_type=str(row.paragraph_type),
                confidence=float(row.classifier_confidence or 0.0),
                classifier_confidence_level=seg.confidence_level(float(row.classifier_confidence or 0.0)),
            )
            for row in rows
        ]
        spans = [(start, end, text) for (start, end), text in zip(self.spans, self.texts)]
        stats_update = compute_classification_stats(
            spans, SegmentationResult(classifications=classifications, calibration=calibration)
        )
        voice_signature = None
        if self.mode != MODE_RETYPE:
            # 正文不变时(就地重分类)声音签名不变:只在导入 / 破坏式重分类时重算
            from novel_system.services.style_reference.voice_signature import compute_voice_signature

            voice_signature = compute_voice_signature(self.texts)

        now = utcnow()
        anchor_rt = self.runtimes[seg.NODE_ANCHOR]
        used_nodes = [seg.NODE_ANCHOR] + ([str(rest_node)] if rest and rest_node else [])
        if rest and not cursor.get("same_route") and cursor.get("agreement") is not None:
            used_nodes.append(seg.NODE_BULK)
        provenance = {
            "source": "llm",
            "llm_paragraphs": len(rows),
            "heuristic_paragraphs": 0,
            "prompt_version": anchor_rt.prompt_version,
            "prompt_versions": {
                node: self.runtimes[node].prompt_version for node in dict.fromkeys(used_nodes)
            },
            "models": {node: self.runtimes[node].route_key[1] for node in dict.fromkeys(used_nodes)},
            "classified_at": now,
            "mode": self.mode,
            "job_id": self.claimed.job_id,
            "anchor_size": len(anchors),
            "anchor_sampling": "stratified",
            "agreement": cursor.get("agreement"),
            "rest_node": rest_node if rest else None,
            "calibration_skipped": cursor.get("calibration_skipped"),
            "batches": int(cursor.get("batches_done") or 0),
            "llm_calls": int(cursor.get("llm_calls") or 0),
            "retries": int(cursor.get("retries") or 0),
        }

        self._fresh()
        book = self.session.execute(
            select(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == self.book_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if book is None:
            raise JobLost(self.claimed.job_id)
        stats = dict(book.stats_json or {})
        revision = int(stats.get("paragraph_types_revision") or 0) + 1
        result = {
            "book_id": self.book_id,
            "mode": self.mode,
            "paragraphs": len(rows),
            "paragraph_types_revision": revision,
            "batches": provenance["batches"],
            "llm_calls": provenance["llm_calls"],
            "retries": provenance["retries"],
            "agreement": provenance["agreement"],
            "rest_node": provenance["rest_node"],
        }
        if not self.service.succeed(self.claimed, result):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        stats.update(stats_update)
        if voice_signature is not None:
            stats["voice_signature"] = voice_signature
        stats["paragraph_types_revision"] = revision
        stats["classification_provenance"] = provenance
        stats.pop("classification", None)
        book.stats_json = stats
        book.status = "ready"
        self.session.flush()
        self.session.commit()


def run_classification_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``classify`` 作业处理器(工人线程里运行;终态由这里连同书的状态一起写)。"""
    _ClassificationRun(session, claimed, service).run()


register_job_handler(JOB_KIND_CLASSIFY, run_classification_job)


# ---------------------------------------------------------------- read models


def classification_provenance(stats: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """段落类型的来源:新分类作业写的 ``classification_provenance``;老书按校准信息推出来(``derived``)。

    - 2026-09-15 前的有界导入:锚定集之外整段交给启发式 → ``legacy_heuristic``(份额 + 锚定集上
      启发式与模型的一致率);
    - 2026-09-15 起的严格 LLM 导入(还没有这个键):``llm``(一致率 = 快模型在锚定集上的一致率)。
    """
    if not isinstance(stats, Mapping):
        return None
    recorded = stats.get("classification_provenance")
    if isinstance(recorded, Mapping):
        return dict(recorded)
    calibration = stats.get("classifier_calibration")
    if not isinstance(calibration, Mapping):
        return None
    heuristic = int(calibration.get("heuristic_classified_paragraphs") or 0)
    llm = int(calibration.get("llm_classified_paragraphs") or 0)
    if calibration.get("rest_classifier") == "heuristic" or (
        calibration.get("fallback_to_heuristic") is True and heuristic
    ):
        return {
            "source": "legacy_heuristic",
            "derived": True,
            "llm_paragraphs": llm,
            "heuristic_paragraphs": heuristic,
            "heuristic_anchor_agreement": calibration.get("heuristic_anchor_agreement"),
            "agreement": calibration.get("heuristic_anchor_agreement"),
        }
    if calibration.get("fallback_to_heuristic") is True:
        # 离线夹具建的书(测试 / 本地语料工具):整本启发式
        return {
            "source": "offline_heuristic",
            "derived": True,
            "llm_paragraphs": 0,
            "heuristic_paragraphs": heuristic or None,
            "agreement": None,
        }
    return {
        "source": "llm",
        "derived": True,
        "llm_paragraphs": llm,
        "heuristic_paragraphs": heuristic,
        "agreement": calibration.get("fast_model_agreement"),
        "prompt_version": None,
    }


def classification_payload(job: StyleReferenceJob | None) -> dict[str, Any] | None:
    """书的 ``classification`` 字段:最近一个分类作业的摘要(没有作业返回 None)。"""
    if job is None:
        return None
    cursor = dict(job.cursor_json or {})
    progress = dict(job.progress_json or {})
    stalled = job.state == STATE_RUNNING and heartbeat_is_stale(job.heartbeat_at)
    return {
        "job_id": job.job_id,
        "state": job.state,
        "mode": _job_mode(job),
        "kind": _legacy_kind(job),
        "op_key": job.op_key,
        "phase": cursor.get("phase") or job.phase,
        "phase_label": progress.get("phase_label"),
        "batches_done": int(cursor.get("batches_done") or progress.get("done") or 0),
        "batches_total": int(cursor.get("batches_total") or progress.get("total") or 0),
        "llm_calls": int(cursor.get("llm_calls") or progress.get("llm_calls") or 0),
        "retries": int(cursor.get("retries") or 0),
        "agreement": cursor.get("agreement"),
        "rest_node": cursor.get("rest_node"),
        "attempt": int(job.attempt or 0),
        "error": dict(job.error_json) if job.error_json else None,
        "cancel_requested": bool(job.cancel_requested),
        "stalled": stalled,
        "resumable": job.state in (STATE_FAILED, STATE_CANCELLED) or stalled,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _legacy_kind(job: StyleReferenceJob) -> str:
    """旧活动面板的 kind(导入 / 重新分类);就地重分类也算「重新分类」。"""
    return "import" if _job_mode(job) == MODE_IMPORT else "reclassify"


_LEGACY_STATUS = {
    STATE_QUEUED: "running",
    STATE_RUNNING: "running",
    STATE_SUCCEEDED: "succeeded",
    STATE_FAILED: "failed",
    STATE_CANCELLED: "cancelled",
}


def classification_activity_entries(
    job: StyleReferenceJob,
    *,
    title: str | None,
    total_chars: int | None,
) -> list[dict[str, Any]]:
    """一个分类作业的活动条目:作业表的统一条目(``job:<id>``),外加一条兼容旧前端的别名条目
    (键 = 请求的幂等键、kind = import / reclassify、带旧的 ``classify`` 批次块),别名带
    ``compat_alias_of``——新界面应只认 ``job:`` 条目;P7 随 ``/imports/{key}/progress`` 一起删别名。"""
    entry = job_activity_entry(job)
    mode = _job_mode(job)
    cursor = dict(job.cursor_json or {})
    progress = dict(job.progress_json or {})
    entry.update(
        {
            "title": title,
            "mode": mode,
            "mode_label": _MODE_LABELS.get(mode, mode),
            "op_key": job.op_key,
            "target_id": job.book_id,
            "paragraphs_total": progress.get("paragraphs_total") or cursor.get("paragraphs_total"),
            "chars_total": total_chars,
        }
    )
    entries = [entry]
    if job.op_key:
        entries.append(_legacy_alias(job, entry, title=title, total_chars=total_chars))
    return entries


def _legacy_alias(
    job: StyleReferenceJob,
    entry: Mapping[str, Any],
    *,
    title: str | None,
    total_chars: int | None,
) -> dict[str, Any]:
    status = _LEGACY_STATUS.get(job.state, job.state)
    cursor = dict(job.cursor_json or {})
    done = int(cursor.get("batches_done") or 0)
    total = int(cursor.get("batches_total") or 0)
    percent = entry.get("percent")
    if status == "succeeded":
        percent = 100
    elif percent is None:
        percent = 0
    else:
        percent = min(99, int(percent))
    paragraphs = cursor.get("paragraphs_total")
    kind = _legacy_kind(job)
    return {
        "key": job.op_key,
        "compat_alias_of": entry.get("key"),
        "job_id": job.job_id,
        "kind": kind,
        "kind_label": "导入" if kind == "import" else "重新分类",
        "source": "job",
        "title": title,
        "book_id": job.book_id,
        "target_id": job.book_id,
        "status": status,
        "phase": "classify" if status == "running" else ("done" if status == "succeeded" else status),
        "phase_label": (
            entry.get("phase_label") if status == "running" else {"succeeded": "完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)
        ),
        "percent": percent,
        "steps": {"done": done, "total": total, "label": "批"} if total else None,
        "classify": {
            "mode": "llm",
            "batches_done": done,
            "batches_total": total,
            "llm_calls": int(cursor.get("llm_calls") or 0),
            "node_id": cursor.get("rest_node") if cursor.get("phase") == PHASE_REST else seg.NODE_ANCHOR,
        },
        "llm_calls": int(cursor.get("llm_calls") or 0),
        "started_at": job.started_at or job.created_at,
        "updated_at": job.updated_at,
        "elapsed_seconds": entry.get("elapsed_seconds"),
        "eta_seconds": entry.get("eta_seconds"),
        "error": dict(job.error_json) if job.error_json else None,
        "result": (
            {"book_id": job.book_id, "paragraphs_count": paragraphs} if status == "succeeded" else None
        ),
        "cancel_requested": bool(job.cancel_requested),
        "cancellable": status == "running" and not bool(job.cancel_requested),
        "retryable": status in ("failed", "cancelled"),
        "resumable": status in ("failed", "cancelled") or bool(entry.get("stalled")),
        "chars_total": total_chars,
        "paragraphs_total": paragraphs,
        "paragraphs_count": paragraphs if status == "succeeded" else None,
    }


def find_job_by_op_key(session: Session, op_key: str) -> StyleReferenceJob | None:
    return session.execute(
        select(StyleReferenceJob)
        .where(StyleReferenceJob.op_key == op_key)
        .order_by(StyleReferenceJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def legacy_progress_snapshot(
    job: StyleReferenceJob,
    *,
    title: str | None,
    total_chars: int | None,
) -> dict[str, Any]:
    """``GET /imports/{key}/progress`` 的兼容快照(旧前端导入轮询读的形状)。"""
    entry = job_activity_entry(job)
    alias = _legacy_alias(job, entry, title=title, total_chars=total_chars)
    alias["import_key"] = job.op_key
    alias["op_key"] = job.op_key
    return alias


# ---------------------------------------------------------------- estimate


def _median(values: Sequence[float]) -> float | None:
    clean = [float(v) for v in values if v and v > 0]
    return float(statistics.median(clean)) if clean else None


def _ledger_basis(session: Session) -> dict[str, Any]:
    """本地账本里分类节点最近的调用:每请求字符的输入 token、输出吞吐、每段输出 token(新格式的
    ``step`` 记了段数,且关推理)。取不到的项用默认值,并如实标出来源。"""
    rows = session.execute(
        select(
            LlmCall.prompt_tokens,
            LlmCall.completion_tokens,
            LlmCall.latency_ms,
            LlmCall.request_payload_summary,
            LlmCall.step,
            LlmCall.reasoning_level,
        )
        .where(
            LlmCall.node_id.in_(list(seg.CLASSIFY_NODE_IDS)),
            LlmCall.accounting_status == "settled",
        )
        .order_by(LlmCall.created_at.desc())
        .limit(ESTIMATE_LEDGER_SAMPLE)
    ).all()
    per_char: list[float] = []
    per_second: list[float] = []
    per_paragraph: list[float] = []
    for row in rows:
        summary = row.request_payload_summary if isinstance(row.request_payload_summary, Mapping) else {}
        message_chars = summary.get("message_chars")
        if row.prompt_tokens and isinstance(message_chars, (int, float)) and message_chars > 0:
            per_char.append(float(row.prompt_tokens) / float(message_chars))
        if row.completion_tokens and row.latency_ms:
            per_second.append(float(row.completion_tokens) / (float(row.latency_ms) / 1000.0))
        parts = str(row.step or "").split(":")
        if len(parts) == 4 and parts[3].isdigit() and int(parts[3]) > 0 and row.reasoning_level == "off":
            if row.completion_tokens:
                per_paragraph.append(float(row.completion_tokens) / int(parts[3]))
    input_per_char = _median(per_char)
    output_per_second = _median(per_second)
    output_per_paragraph = _median(per_paragraph)
    return {
        "calls_sampled": len(rows),
        "input_tokens_per_char": round(input_per_char or DEFAULT_INPUT_TOKENS_PER_CHAR, 4),
        "input_tokens_per_char_source": "ledger" if input_per_char else "default",
        "output_tokens_per_second": round(output_per_second or DEFAULT_OUTPUT_TOKENS_PER_SECOND, 1),
        "output_tokens_per_second_source": "ledger" if output_per_second else "default",
        "output_tokens_per_paragraph": round(output_per_paragraph or DEFAULT_OUTPUT_TOKENS_PER_PARAGRAPH, 1),
        "output_tokens_per_paragraph_source": "ledger" if output_per_paragraph else "default",
        "call_overhead_seconds": DEFAULT_CALL_OVERHEAD_SECONDS,
    }


def estimate_classification(session: Session, book: StyleReferenceBook) -> dict[str, Any]:
    """整本(重新)分类要多少批 / 调用 / token / 分钟(作者重标段落类型前看费用)。

    与作业同一套计划:同样的锚定集、同样的按字数分批;两个分类节点是同一个模型时没有快模型对照。
    输入 token 按每批请求字数 × 每字符 token,输出按每段 token,时间按输出吞吐 + 每次固定开销、
    ``PARALLEL_BATCHES`` 路并行;均值优先取本地账本,取不到用默认值(见 ``basis``)。
    """
    rows = session.execute(
        select(StyleReferenceParagraph.text)
        .where(StyleReferenceParagraph.book_id == book.book_id)
        .order_by(StyleReferenceParagraph.paragraph_index)
    ).all()
    texts = [str(row.text or "") for row in rows]
    try:
        runtimes = seg.load_classification_runtimes()
    except seg.SegmentationLLMError as exc:
        raise ClassificationFailedError(
            code=exc.code, message=exc.message, book_id=book.book_id, details=exc.details
        ) from exc
    anchor_rt = runtimes[seg.NODE_ANCHOR]
    bulk_rt = runtimes[seg.NODE_BULK]
    anchors = seg.select_anchor_positions(texts, seed=book.book_id)
    anchor_set = set(anchors)
    rest = [pos for pos in range(len(texts)) if pos not in anchor_set]
    same_route = seg.same_model(anchor_rt, bulk_rt)
    phases: list[tuple[str, seg.NodeRuntime, list[list[int]]]] = [
        (PHASE_ANCHOR_STRONG, anchor_rt, seg.plan_batches(anchors, texts))
    ]
    if rest and not same_route:
        phases.append((PHASE_ANCHOR_FAST, bulk_rt, seg.plan_batches(anchors, texts)))
    if rest:
        phases.append((PHASE_REST, bulk_rt, seg.plan_batches(rest, texts)))
    basis = _ledger_basis(session)
    input_tokens = 0.0
    output_tokens = 0.0
    seconds = 0.0
    batches = 0
    phase_batches: dict[str, int] = {}
    for phase, runtime, plan in phases:
        phase_batches[phase] = len(plan)
        for positions in plan:
            batches += 1
            input_tokens += seg.estimated_message_chars(runtime, positions, texts) * basis["input_tokens_per_char"]
            batch_output = len(positions) * basis["output_tokens_per_paragraph"]
            output_tokens += batch_output
            seconds += basis["call_overhead_seconds"] + batch_output / max(1.0, basis["output_tokens_per_second"])
    minutes = seconds / max(1, PARALLEL_BATCHES) / 60.0
    return {
        "book_id": book.book_id,
        "paragraphs": len(texts),
        "batches": batches,
        "est_calls": batches,
        "est_input_tokens": int(round(input_tokens)),
        "est_output_tokens": int(round(output_tokens)),
        "est_minutes": round(minutes, 1),
        "parallel": PARALLEL_BATCHES,
        "anchor_size": len(anchors),
        "same_route": same_route,
        "phase_batches": phase_batches,
        "batch_limits": {"max_chars": seg.BATCH_MAX_CHARS, "max_paragraphs": seg.BATCH_MAX_PARAGRAPHS},
        "retries_not_included": True,
        "basis": basis,
    }


__all__ = [
    "BATCH_ATTEMPTS",
    "BATCH_RETRY_BACKOFF_SECONDS",
    "CLASSIFICATION_ALREADY_ACTIVE_CODE",
    "CLASSIFY_MODES",
    "MODE_IMPORT",
    "MODE_RECLASSIFY",
    "MODE_RETYPE",
    "NOTHING_TO_RESUME_CODE",
    "PARALLEL_BATCHES",
    "UNCLASSIFIED_PARAGRAPH_TYPE",
    "active_classification_job",
    "cancel_classification",
    "classification_activity_entries",
    "classification_payload",
    "classification_provenance",
    "count_paragraphs",
    "create_classification_job",
    "estimate_classification",
    "find_job_by_op_key",
    "latest_classification_job",
    "legacy_progress_snapshot",
    "resolve_classification_client",
    "resume_classification",
    "run_classification_job",
]
