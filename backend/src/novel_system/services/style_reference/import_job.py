"""参考书段落分类作业(2026-09-23 风格参考 v3:跑在统一作业表上,kind=classify)——对外接口与读模型。

严格 LLM(2026-09-15):每一段都由 LLM 分类,没有启发式兜底。导入 / 重新分类 / 就地重分类的请求只做
准备工作(导入:解码、切段、安全扫描、批量落书与段落行),然后建一个 ``classify`` 作业(:func:`create_classification_job`;
「继续分类」:func:`resume_classification`、取消 :func:`cancel_classification`);处理器 ``classify_run.run_classification_job``
(由 ``workers.install_workers`` 登记)在作业工人线程里逐批分类。这里还有书卡 / 活动面板读的摘要
(:func:`classification_payload` / :func:`classification_activity_entry`)、段落类型的来源与整本分类的费用估算。

**三种模式**(``params.mode``):

- ``import``   新导入的书(段落行初始类型 ``unclassified``);书 ``ingesting`` → 成功 ``ready`` / 失败或取消 ``failed``;
- ``reclassify`` 破坏式重新分类:请求里先清掉派生数据(抽取 / 画像 / 绑定 / 窗口 / 作业);状态同 import;
- ``retype``   就地重分类(v3 I5 / L7):正文不变、只重标段落类型,不删抽取 / 画像 / 绑定;书全程保持
               ``ready``(绑定照常用),失败 / 取消也回到 ``ready``(已写的新类型保留,可继续)。

同一本书上分类与学习互斥(``ensure_not_learning``)。最多 :data:`PARALLEL_BATCHES` 批并行——处理器与估算读的是同一个数。
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    LlmCall,
    StyleReferenceBook,
    StyleReferenceJob,
    StyleReferenceParagraph,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import ClassificationFailedError
from novel_system.services.style_reference.job_runtime import conflict_error_by_kind
from novel_system.services.style_reference.jobs import (
    ACTIVE_STATES,
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    StyleJobService,
    heartbeat_is_stale,
    job_activity_entry,
)
from novel_system.services.style_reference.segmentation import llm as seg


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
PHASE_ORDER: tuple[str, ...] = (PHASE_ANCHOR_STRONG, PHASE_ANCHOR_FAST, PHASE_REST)
PHASE_LABELS: dict[str, str] = {
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


# ---------------------------------------------------------------- 建 / 续 / 取消


def job_mode(job: Any) -> str:
    params = getattr(job, "params_json", None)
    if params is None:
        params = getattr(job, "params", None)
    mode = str((params or {}).get("mode") or MODE_IMPORT)
    return mode if mode in CLASSIFY_MODES else MODE_IMPORT


def status_after_failure(mode: str) -> str:
    """失败 / 取消后书的状态:就地重分类回到 ready(书本来就完整可用),其余 failed。"""
    return "ready" if mode == MODE_RETYPE else "failed"


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


BOOK_LEARNING_CODE = "STYLE_REFERENCE_BOOK_LEARNING"
PARAGRAPHS_CHANGED_CODE = "STYLE_REFERENCE_PARAGRAPHS_CHANGED"


def _learning_error(job: StyleReferenceJob, book_id: str) -> DomainError:
    return DomainError(
        BOOK_LEARNING_CODE,
        "这本书正在学习文风：等它完成，或先取消，再重新分类。",
        status_code=409,
        details={"book_id": book_id, "job_id": job.job_id, "state": job.state},
    )


def _already_classifying_error(other: StyleReferenceJob, book_id: str) -> DomainError:
    return DomainError(
        CLASSIFICATION_ALREADY_ACTIVE_CODE,
        "这本书正在分类:等它完成,或先取消。",
        status_code=409,
        details={"book_id": book_id, "job_id": other.job_id, "op_key": other.op_key, "state": other.state},
    )


def ensure_not_learning(session: Session, book_id: str) -> None:
    """学习文风作业在读这本书的段落类型（选样本、窗口段型构成、标签）：它在跑时不许重分类 / 就地重标，
    否则学到一半的画像建立在两套类型上。"""
    active = StyleJobService(session).active_for_book(book_id, kind=JOB_KIND_LEARN)
    if active:
        raise _learning_error(active[0], book_id)


def create_classification_job(
    session: Session,
    book: StyleReferenceBook,
    *,
    mode: str,
    op_key: str | None = None,
) -> StyleReferenceJob:
    """给一本书建分类作业(queued)。已有排队 / 运行中的分类作业、或学习文风作业在跑 → 409。调用方提交后派发。"""
    if mode not in CLASSIFY_MODES:
        raise ValueError(f"unknown classification mode {mode!r}")
    ensure_not_learning(session, book.book_id)
    existing = active_classification_job(session, book.book_id)
    if existing is not None:
        _raise_already_active(existing, book.book_id)
    job = StyleJobService(session).create(
        JOB_KIND_CLASSIFY,
        book_id=book.book_id,
        op_key=op_key or None,
        params={"mode": mode},
        phase="queued",
        exclusive_with=(JOB_KIND_LEARN,),
        conflict_error=lambda other: conflict_error_by_kind(
            other, book.book_id, by_kind={JOB_KIND_LEARN: _learning_error}, default=_already_classifying_error
        ),
    )
    job.progress_json = {"phase": "queued", "phase_label": "排队中", "mode": mode}
    if mode != MODE_RETYPE:
        book.status = "ingesting"
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
    ensure_not_learning(session, book.book_id)
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
        # 放回队列这条 UPDATE 已拿到写锁:再查一次学习作业(与建作业同一个「先写后查」,见 jobs 模块文档)
        conflict = service.first_conflict(book.book_id, kinds=(JOB_KIND_LEARN,), excluding=job.job_id)
        if conflict is not None:
            raise _learning_error(conflict, book.book_id)
    if op_key:
        job.op_key = op_key
    progress = dict(job.progress_json or {})
    progress.update({"phase_label": "排队中", "resumed_at": utcnow()})
    job.progress_json = progress
    if job_mode(job) != MODE_RETYPE:
        book.status = "ingesting"
    session.flush()
    return job


def cancel_classification(session: Session, book_id: str) -> StyleReferenceJob | None:
    """取消这本书正在排队 / 运行的分类作业。排队中或工人已死的作业在这里直接收尾(书的状态一并
    落定);运行中的由工人在下一个检查点收尾。没有活动作业时返回 None。"""
    job = active_classification_job(session, book_id)
    if job is None:
        return None
    # 排队中 / 工人已死的作业在请求里直接收尾;书的状态由登记的收尾钩子(on_classification_cancelled)一并落定
    return StyleJobService(session).request_cancel(job.job_id)


def count_paragraphs(session: Session, book_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(StyleReferenceParagraph)
            .where(StyleReferenceParagraph.book_id == book_id)
        )
        or 0
    )


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
        "mode": job_mode(job),
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


def classification_activity_entry(
    job: StyleReferenceJob,
    *,
    title: str | None,
    total_chars: int | None,
) -> dict[str, Any]:
    """一个分类作业的活动条目:作业表的统一条目(``job:<id>``),加上书名、分类方式与段数 / 字数。"""
    entry = job_activity_entry(job)
    mode = job_mode(job)
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
    return entry


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
    "BOOK_LEARNING_CODE",
    "CLASSIFICATION_ALREADY_ACTIVE_CODE",
    "CLASSIFY_MODES",
    "CURSOR_VERSION",
    "MODE_IMPORT",
    "MODE_RECLASSIFY",
    "MODE_RETYPE",
    "NOTHING_TO_RESUME_CODE",
    "PARAGRAPHS_CHANGED_CODE",
    "PARALLEL_BATCHES",
    "PHASE_ANCHOR_FAST",
    "PHASE_ANCHOR_STRONG",
    "PHASE_FINALIZE",
    "PHASE_LABELS",
    "PHASE_ORDER",
    "PHASE_REST",
    "UNCLASSIFIED_PARAGRAPH_TYPE",
    "active_classification_job",
    "cancel_classification",
    "classification_activity_entry",
    "classification_payload",
    "classification_provenance",
    "count_paragraphs",
    "create_classification_job",
    "ensure_not_learning",
    "estimate_classification",
    "job_mode",
    "resume_classification",
    "status_after_failure",
]
