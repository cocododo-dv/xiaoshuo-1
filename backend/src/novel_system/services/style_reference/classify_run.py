"""参考书段落分类作业的处理器(kind=classify;从 ``import_job`` 拆出,``workers.install_workers`` 登记)。

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
最多 ``import_job.PARALLEL_BATCHES`` 批并行(估算读的是同一个数,所以留在 ``import_job``、在这里按模块取)——LLM
调用在工人线程里(各用各的记账会话),写库只在作业线程里;每批失败(调用失败 / 输出对不上)按
:data:`BATCH_RETRY_BACKOFF_SECONDS` 退避重试两次,仍失败才让作业失败(``STYLE_REFERENCE_CLASSIFICATION_FAILED``
502,游标保留,可「继续分类」)。路由与提示词模板每个作业只载一次;LLM 客户端在作业开始时按当前配置取
(:func:`resolve_classification_client`,v3 I6,不在请求里捕获;测试在这里打桩)。

每次成功:``stats_json["paragraph_types_revision"]`` +1、``classification_provenance`` 记来源
(``llm`` + 份额 + 提示词版本 + 时间),并从段落行重算分类器校准与段型分布(导入与破坏式重分类还重算声音签名)。

处理器的脚手架(检查点、并行调用循环、退避重试、终态收尾)在 ``job_runtime.JobRun``,与学习 / 对照检查共用。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from novel_system.db.models import (
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
from novel_system.services.style_reference.job_runtime import (
    JobRun,
    retry_attempts,
)
from novel_system.services.style_reference.jobs import (
    ClaimedJob,
    DaemonCallPool,
    JobLost,
    StyleJobService,
)
from novel_system.services.style_reference.segmentation import llm as seg
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)

from novel_system.services.style_reference import import_job
from novel_system.services.style_reference.import_job import (
    CURSOR_VERSION,
    MODE_RETYPE,
    PARAGRAPHS_CHANGED_CODE,
    PHASE_ANCHOR_FAST,
    PHASE_ANCHOR_STRONG,
    PHASE_FINALIZE,
    PHASE_LABELS,
    PHASE_ORDER,
    PHASE_REST,
    job_mode,
    status_after_failure,
)

logger = logging.getLogger(__name__)

# 每批的重试与节拍(测试把退避 / 等待调成 0);同时在飞的批数是 import_job.PARALLEL_BATCHES
BATCH_ATTEMPTS = 3  # 1 次 + 2 次重试
BATCH_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0)
WAIT_POLL_SECONDS = 2.0
BATCH_SECONDS_KEPT = 64

_RETRYABLE_BATCH_CODES = frozenset(
    {"STYLE_REFERENCE_CLASSIFY_LLM_CALL_FAILED", "STYLE_REFERENCE_CLASSIFY_OUTPUT_MISMATCH"}
)


def resolve_classification_client() -> tuple[Any | None, bool]:
    """作业开始时按**当前**运行时配置取 LLM 客户端(v3 I6:不在请求里捕获);测试在这里打桩。"""
    from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

    return runtime_llm_client_and_enabled()


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
        "done": {phase: [] for phase in PHASE_ORDER},
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
    # 重试用尽仍没分出来的段(位置);非空时 ``failure`` 是这一批的失败——合格的部分照样落库,
    # 游标只记分出来的段,「继续分类」只重发没分出来的段
    unresolved: tuple[int, ...] = ()
    failure: Exception | None = None


# ---------------------------------------------------------------- the handler


class _ClassificationRun(JobRun):
    operation = "classify_book"

    def __init__(self, session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
        super().__init__(session, claimed, service)
        self.mode = job_mode(claimed)
        self.runtimes: dict[str, seg.NodeRuntime] = {}
        self.ids: list[str] = []
        self.indexes: list[int] = []
        self.texts: list[str] = []
        self.spans: list[tuple[int, int]] = []
        self.cursor: dict[str, Any] = {}

    # ---- lifecycle (终态的附带写:书的状态) ------------------------------------
    def _set_book_status(self, status: str) -> None:
        self.session.execute(
            update(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == self.book_id)
            .values(status=status, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )

    def finish_cancelled(self) -> None:
        self.session.rollback()
        if not self.service.finish_cancelled(self.claimed):
            self.session.rollback()
            return
        self._set_book_status(status_after_failure(self.mode))
        self.session.commit()

    def finish_failed(
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
        self._set_book_status(status_after_failure(self.mode))
        self.session.commit()

    # ---- main -----------------------------------------------------------
    def _run(self) -> None:
        self.check_continue()
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
        return f"段落分类 · {PHASE_LABELS.get(phase, phase)}"

    def _set_phase(self, phase: str) -> None:
        """进入一个阶段(只前进不后退:续跑时已完成的阶段不再改写进度文案)。"""
        order = (*PHASE_ORDER, PHASE_FINALIZE)
        current = str(self.cursor.get("phase") or PHASE_ANCHOR_STRONG)
        if current == phase or (current in order and order.index(current) > order.index(phase)):
            return
        self.cursor["phase"] = phase
        self._save_cursor(progress=dict(phase_label=self._phase_label()))

    def _save_cursor(self, *, progress: Mapping[str, Any] | None = None) -> None:
        self.save_cursor(self.cursor, progress=progress)

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
        """最多 ``PARALLEL_BATCHES`` 批同时在飞;每批的合格结果一回来就落库(作业线程;同一轮回来的按首段位置落)。

        某一批失败:不再派发新批,已经在飞的批(已经花了钱)等它们回来、合格的照常落库,然后作业失败——
        游标里已分出来的段保留,「继续分类」只重发没分出来的段。取消 / 丢了所有权 / 进程退出:立即停,
        在飞的调用在守护线程里自生自灭(结果丢弃)。循环本身在 ``JobRun.run_parallel``(与学习作业共用)。"""
        if not batches:
            return

        def submit(pool: DaemonCallPool, positions: list[int], stop: threading.Event) -> Future:
            self.pre_call_check({runtime.node_id: runtime.route})
            return pool.submit(self._call_with_retries, phase, runtime, positions, stop)

        def apply(positions: list[int], outcome: _BatchOutcome) -> Exception | None:
            self._apply(phase, positions, outcome)
            return outcome.failure

        self.run_parallel(
            [list(batch) for batch in batches],
            submit,
            apply,
            max_inflight=import_job.PARALLEL_BATCHES,
            poll_seconds=WAIT_POLL_SECONDS,
            thread_prefix="sr_classify",
            order_key=lambda positions: positions[0],
        )

    def _call_with_retries(
        self,
        phase: str,
        runtime: seg.NodeRuntime,
        positions: list[int],
        stop: threading.Event,
    ) -> _BatchOutcome:
        """工人线程:一批最多 ``BATCH_ATTEMPTS`` 次调用(退避重试);记账用自己的会话。

        输出按条收(``seg.classify_batch_partial``):合格的段先收下,下一次只重发还没分出来的段。重试用尽
        仍有段没分出来 → 返回带 ``unresolved`` / ``failure`` 的结果(合格部分照样落库);调用本身全失败同理。"""
        from novel_system.db.session import SessionLocal

        problems: list[str] = []
        last_code = "STYLE_REFERENCE_CLASSIFY_OUTPUT_MISMATCH"
        last_message = ""
        last_details: dict[str, Any] = {}
        first_index = self.indexes[positions[0]]
        remaining = list(positions)
        results: dict[int, tuple[str, float]] = {}
        calls = 0
        began = time.monotonic()
        for attempt in retry_attempts(BATCH_ATTEMPTS, BATCH_RETRY_BACKOFF_SECONDS, stop):
            calls += 1
            try:
                with SessionLocal() as ledger_session:
                    got, batch_problems = seg.classify_batch_partial(
                        runtime,
                        remaining,
                        self.texts,
                        self.indexes,
                        self.client,
                        session=ledger_session,
                        scope_id=self.book_id,
                        step=f"paragraph_classification:{phase}:{self.indexes[remaining[0]]}:{len(remaining)}",
                    )
            except seg.SegmentationLLMError as exc:
                last_code, last_message, last_details = exc.code, exc.message, dict(exc.details)
                problems.append(f"attempt {attempt + 1}: {exc.code}: {exc.message[:300]}")
                logger.warning(
                    "classification batch %s:%s (node %s) attempt %d failed: %s",
                    phase,
                    first_index,
                    runtime.node_id,
                    attempt + 1,
                    exc.code,
                )
                if exc.code not in _RETRYABLE_BATCH_CODES:
                    break
                continue
            results.update(got)
            remaining = [pos for pos in remaining if self.indexes[pos] not in results]
            if not remaining:
                return _BatchOutcome(
                    results=results, seconds=round(time.monotonic() - began, 2), attempts=calls
                )
            last_code = "STYLE_REFERENCE_CLASSIFY_OUTPUT_MISMATCH"
            last_message = (
                f"classification output does not match the batch ({len(batch_problems)} problem(s)): "
                + "; ".join(batch_problems[:8])
            )
            last_details = {"problem_count": len(batch_problems), "expected": len(positions), "received": len(got)}
            problems.append(
                f"attempt {attempt + 1}: accepted {len(got)}, still missing {len(remaining)}: "
                + "; ".join(batch_problems[:3])
            )
            logger.warning(
                "classification batch %s:%s (node %s) attempt %d left %d paragraph(s) unclassified",
                phase,
                first_index,
                runtime.node_id,
                attempt + 1,
                len(remaining),
            )
        failure = ClassificationFailedError(
            code=last_code,
            message=last_message or "classification batch failed",
            book_id=self.book_id,
            details={
                "phase": phase,
                "node_id": runtime.node_id,
                "first_paragraph_index": first_index,
                "paragraphs": len(positions),
                "unresolved": len(remaining),
                "first_unresolved_index": self.indexes[remaining[0]] if remaining else None,
                "attempts": calls,
                "problems": problems,
                **{k: v for k, v in last_details.items() if k in ("problem_count", "expected", "received")},
            },
        )
        return _BatchOutcome(
            results=results,
            seconds=round(time.monotonic() - began, 2),
            attempts=calls,
            unresolved=tuple(remaining),
            failure=failure,
        )

    def _apply(self, phase: str, positions: list[int], outcome: _BatchOutcome) -> None:
        """作业线程:游标(条件写,本事务第一条写)→ 段落类型 → 进度,一次提交。

        只有分出来的段记为完成;整批都分出来才算「完成一批」(没分出来的段续跑时重新成批)。"""
        cursor = self.cursor
        classified = [pos for pos in positions if self.indexes[pos] in outcome.results]
        if not classified and outcome.failure is not None:
            return
        done = dict(cursor.get("done") or {})
        done[phase] = _add_ranges(done.get(phase) or [], classified)
        cursor["done"] = done
        if not outcome.unresolved:
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
        self.fresh()
        if not self.service.save_cursor(self.claimed, cursor):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        if phase != PHASE_ANCHOR_FAST:
            by_index = {self.indexes[pos]: pos for pos in classified}
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
                # 段落行没了:书在这一批期间被删(删书同事务里也取消了作业 → 丢了所有权),或段落表被别的写者改了
                # (作业还归我)。后者直接让作业失败、说清原因——悄悄停下会在心跳过期后被清扫重排,段数变了游标
                # 作废、整本重新计费
                self.session.rollback()
                if not self.service.still_owner(self.claimed):
                    raise JobLost(self.claimed.job_id) from exc
                raise DomainError(
                    PARAGRAPHS_CHANGED_CODE,
                    "段落表在分类期间被改动了(段落行被删或重编号):等改动的操作结束后再「继续分类」。",
                    status_code=409,
                    details={
                        "book_id": self.book_id,
                        "phase": phase,
                        "first_paragraph_index": self.indexes[classified[0]],
                        "retryable": True,
                        "author_action": {
                            "action": "resume_classification",
                            "view": "styleref",
                            "book_id": self.book_id,
                            "label": "继续分类",
                        },
                    },
                ) from exc
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
        self.check_continue()
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

        # 先做作业行的条件写(拿到 SQLite 的写锁),再在同一事务里重读书的 stats_json 合并写回:
        # 这之间别的连接提交不了写,并发写 stats 的人(窗口索引等)的键不会被覆盖。
        self.fresh()
        result = {
            "book_id": self.book_id,
            "mode": self.mode,
            "paragraphs": len(rows),
            "batches": provenance["batches"],
            "llm_calls": provenance["llm_calls"],
            "retries": provenance["retries"],
            "agreement": provenance["agreement"],
            "rest_node": provenance["rest_node"],
        }
        if not self.service.succeed(self.claimed, result):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        book = self.session.execute(
            select(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == self.book_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if book is None:
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        stats = dict(book.stats_json or {})
        revision = int(stats.get("paragraph_types_revision") or 0) + 1
        self.session.execute(
            update(StyleReferenceJob)
            .where(StyleReferenceJob.job_id == self.claimed.job_id)
            .values(result_json={**result, "paragraph_types_revision": revision})
            .execution_options(synchronize_session=False)
        )
        stats.update(stats_update)
        if voice_signature is not None:
            stats["voice_signature"] = voice_signature
        stats["paragraph_types_revision"] = revision
        stats["classification_provenance"] = provenance
        book.stats_json = stats
        book.status = "ready"
        self.session.flush()
        self.session.commit()


def run_classification_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``classify`` 作业处理器(工人线程里运行;终态由这里连同书的状态一起写)。"""
    _ClassificationRun(session, claimed, service).run()


def on_classification_cancelled(session: Session, job: StyleReferenceJob) -> None:
    """请求 / 认领 / 清扫里直接收尾的取消:书的状态一并落定(就地重标回 ready,其余 failed)。"""
    if not job.book_id:
        return
    session.execute(
        update(StyleReferenceBook)
        .where(StyleReferenceBook.book_id == job.book_id)
        .values(status=status_after_failure(job_mode(job)), updated_at=utcnow())
        .execution_options(synchronize_session="fetch")
    )


__all__ = [
    "BATCH_ATTEMPTS",
    "BATCH_RETRY_BACKOFF_SECONDS",
    "WAIT_POLL_SECONDS",
    "on_classification_cancelled",
    "resolve_classification_client",
    "run_classification_job",
]
