"""参考书分类任务(2026-09-15 严格 LLM):整本段落分类由可续跑的后台任务逐批执行。

背景:LLM 可用时每一段都必须由 LLM 分类——没有启发式兜底、没有余段上限。一本 26,000 段的书
= 1,000 多次串行分类调用,在慢中转上是几个小时,同步 HTTP 请求扛不住(前端 15 分钟变更超时,
后端重启就丢)。于是导入 / 重新分类的请求只做准备工作(解码、切段、安全扫描、落书与段落行,
书的状态是 ``ingesting``),真正的分类交给这里的单线程执行器:

- **durable 游标**在 ``book.stats_json["classification"]``(JSON 列,无需迁移):阶段
  (``anchor_strong → anchor_fast → rest → done``)、阶段内偏移、批次计数、每批耗时、快模型一致率、
  余段选用的节点、心跳、错误、取消请求。每批的段落类型直接写回 ``style_reference_paragraphs``
  行并提交,进程崩溃后从游标续跑(``recover_classification_jobs``),不重复已落库的批次;
- 心跳线程每 30 s 续 ``heartbeat_at``,启动恢复把超过 ``CLASSIFICATION_STALE_SECONDS`` 没心跳的
  running 任务视为中断并重新派发;
- 取消:``request_classification_cancel`` 把书的 ``status`` 置 ``cancelling``(普通列,不碰 JSON,
  worker 是 ``stats_json.classification`` 唯一的写者,没有丢更新的竞态),worker 在下一批边界
  退出,书标 ``failed`` + ``STYLE_REFERENCE_IMPORT_CANCELLED``;作者可「继续分类」(resume,保留
  游标)或删书;心跳同理只写 ``updated_at`` 列;
- 进度:任务在进程内登记簿上接着请求登记的那条(同一个幂等键),``GET …/activity`` 同时读
  durable 游标,页面刷新后也能续接。

分类完成后才算 ``ready``:统计指标、段型分布、声音签名都在最后一步从段落行计算并写入,
抽取 / 合成对非 ready 的书一律 409。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph, utcnow
from novel_system.services.llm_accounting import LLMAccountingError, is_llm_control_plane_failure
from novel_system.services.style_reference.background_heartbeat import periodic_heartbeat
from novel_system.services.style_reference.import_progress import (
    ImportProgressReporter,
    NullImportProgress,
    attach_import_progress,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.segmentation.llm import (
    AGREEMENT_THRESHOLD,
    ANCHOR_SIZE,
    BATCH_SIZE,
    NODE_ANCHOR,
    NODE_BULK,
    SegmentationLLMError,
    build_calibration,
    classify_paragraph_batch,
    planned_batches,
)
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)

logger = logging.getLogger(__name__)

CLASSIFICATION_STATE_KEY = "classification"
UNCLASSIFIED_PARAGRAPH_TYPE = "unclassified"
CLASSIFICATION_HEARTBEAT_INTERVAL_SECONDS = 30
# 心跳线程每 30 s 续一次,与 LLM 调用时长无关;超过这个窗口没有心跳 = worker 已死。
CLASSIFICATION_STALE_SECONDS = 300
CANCEL_ERROR_CODE = "STYLE_REFERENCE_IMPORT_CANCELLED"
INTERRUPTED_ERROR_CODE = "STYLE_REFERENCE_IMPORT_INTERRUPTED"
LLM_REQUIRED_AFTER_RESTART_CODE = "STYLE_REFERENCE_LLM_REQUIRED_AFTER_RESTART"

_PHASES: tuple[str, ...] = ("anchor_strong", "anchor_fast", "rest", "done")

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


class ClassificationCancelled(Exception):
    """作者取消(或书已被删除):worker 在批边界退出。"""


def _now_iso() -> str:
    return utcnow()


def classification_op_key(book_id: str) -> str:
    return f"classify:{book_id}"


def classification_state(book: Any) -> dict[str, Any] | None:
    stats = getattr(book, "stats_json", None)
    if not isinstance(stats, dict):
        return None
    state = stats.get(CLASSIFICATION_STATE_KEY)
    return dict(state) if isinstance(state, dict) else None


def build_classification_state(
    *,
    kind: str,
    op_key: str | None,
    total_paragraphs: int,
) -> dict[str, Any]:
    """新任务的 durable 状态(``queued``);``kind`` ∈ {import, reclassify}。"""
    total = max(0, int(total_paragraphs))
    anchor_size = min(ANCHOR_SIZE, total)
    return {
        "state": "queued",
        "kind": kind,
        "op_key": op_key or None,
        "attempt": 1,
        "total_paragraphs": total,
        "anchor_size": anchor_size,
        "batch_size": BATCH_SIZE,
        "batches_total": planned_batches(total, anchor_size),
        "batches_done": 0,
        "llm_calls": 0,
        "batch_seconds": [],
        "cursor": {"phase": "anchor_strong", "offset": 0},
        "anchor_fast": [],
        "fast_model_agreement": None,
        "fallback_to_strong": None,
        "rest_node": None,
        "created_at": _now_iso(),
        "started_at": None,
        "heartbeat_at": None,
        "finished_at": None,
        "error": None,
    }


def _write_state(session: Session, book_id: str, state: dict[str, Any], **book_columns: Any) -> bool:
    """把状态写回书行(整列替换,JSON 列不能就地改);返回是否命中行。"""
    book = session.get(StyleReferenceBook, book_id)
    if book is None:
        return False
    stats = dict(book.stats_json or {})
    stats[CLASSIFICATION_STATE_KEY] = dict(state)
    book.stats_json = stats
    for column, value in book_columns.items():
        setattr(book, column, value)
    session.flush()
    return True


# ---------------------------------------------------------------- dispatch


def start_style_reference_classification_worker(
    *,
    book_id: str,
    llm_client: Any,
    op_key: str | None = None,
) -> None:
    """提交一次分类任务;worker 自己做 queued→running 的认领,重复提交无害。"""

    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sr_classify")
        _EXECUTOR.submit(
            _classification_worker, book_id=str(book_id), llm_client=llm_client, op_key=op_key
        )


def shutdown_style_reference_classification_executor(*, wait: bool = False) -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor = _EXECUTOR
        _EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=False)


def _claim(session: Session, book_id: str) -> dict[str, Any] | None:
    """认领任务:queued(或恢复时重新排队)→ running。running 且心跳新鲜的不认领;
    排队期间就被取消的(status=cancelling)直接收尾成 cancelled。"""
    book = session.get(StyleReferenceBook, book_id)
    if book is None:
        return None
    state = classification_state(book)
    if state is None:
        return None
    if book.status == "cancelling" and state.get("state") in ("queued", "running"):
        _finish_failed(
            session,
            book_id,
            state,
            code=CANCEL_ERROR_CODE,
            message="classification cancelled before it started",
            terminal_state="cancelled",
        )
        return None
    if book.status != "ingesting":
        return None
    if state.get("state") == "running" and not _heartbeat_stale(book.updated_at):
        return None
    if state.get("state") not in ("queued", "running"):
        return None
    now = _now_iso()
    state["state"] = "running"
    state["started_at"] = state.get("started_at") or now
    state["heartbeat_at"] = now
    state["error"] = None
    _write_state(session, book_id, state)
    session.commit()
    return state


def _heartbeat_stale(heartbeat_at: str | None, *, now: datetime | None = None) -> bool:
    if not heartbeat_at:
        return True
    try:
        seen = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return seen < current - timedelta(seconds=CLASSIFICATION_STALE_SECONDS)


def _renew_heartbeat(book_id: str) -> None:
    """只续 ``updated_at`` 列(worker 是 stats_json 唯一的写者,心跳不能碰 JSON)。"""
    from novel_system.db.session import SessionLocal

    with SessionLocal() as session:
        touched = session.execute(
            update(StyleReferenceBook)
            .where(
                StyleReferenceBook.book_id == book_id,
                StyleReferenceBook.status.in_(("ingesting", "cancelling")),
            )
            .values(updated_at=_now_iso())
            .execution_options(synchronize_session=False)
        )
        if touched.rowcount == 1:
            session.commit()
        else:
            session.rollback()


def _classification_worker(*, book_id: str, llm_client: Any, op_key: str | None) -> None:
    from novel_system.db.session import SessionLocal

    progress: ImportProgressReporter | NullImportProgress = NullImportProgress()
    try:
        with SessionLocal() as session:
            state = _claim(session, book_id)
            if state is None:
                logger.info("classification job for %s was not claimable", book_id)
                return
            book = session.get(StyleReferenceBook, book_id)
            key = op_key or state.get("op_key") or classification_op_key(book_id)
            progress = attach_import_progress(
                key,
                kind=str(state.get("kind") or "import"),
                title=book.title if book is not None else None,
                source="job",
                book_id=book_id,
            )
            progress.set_totals(
                chars_total=int(getattr(book, "total_chars", 0) or 0) or None,
                paragraphs_total=int(state.get("total_paragraphs") or 0),
            )
            progress.phase("classify")
            progress.classify_plan(
                int(state.get("batches_total") or 0), "llm", done=int(state.get("batches_done") or 0)
            )
            with periodic_heartbeat(
                lambda: _renew_heartbeat(book_id),
                interval_seconds=CLASSIFICATION_HEARTBEAT_INTERVAL_SECONDS,
                thread_name=f"sr_classify_heartbeat:{book_id}",
            ):
                try:
                    run_classification(session, book_id, state, llm_client, progress=progress)
                except ClassificationCancelled:
                    _finish_failed(
                        session,
                        book_id,
                        state,
                        code=CANCEL_ERROR_CODE,
                        message="classification cancelled by the author",
                        terminal_state="cancelled",
                    )
                    progress.fail(code=CANCEL_ERROR_CODE, message="已取消")
                    return
                except Exception as exc:  # pylint: disable=broad-except
                    code = str(getattr(exc, "code", None) or exc.__class__.__name__)
                    message = str(getattr(exc, "message", None) or exc)
                    logger.exception("classification job for %s failed", book_id)
                    _finish_failed(session, book_id, state, code=code, message=message)
                    progress.fail(code=code, message=message)
                    return
            progress.succeed(
                book_id=book_id, paragraphs_count=int(state.get("total_paragraphs") or 0)
            )
    except Exception as exc:  # pragma: no cover - final worker boundary
        logger.exception("classification worker for %s crashed outside the job", book_id)
        progress.fail(code=exc.__class__.__name__, message=str(exc))


def _finish_failed(
    session: Session,
    book_id: str,
    state: dict[str, Any],
    *,
    code: str,
    message: str,
    terminal_state: str = "failed",
) -> None:
    try:
        session.rollback()
        state["state"] = terminal_state
        state["error"] = {"code": code, "message": str(message)[:500]}
        state["finished_at"] = _now_iso()
        if _write_state(session, book_id, state, status="failed"):
            session.commit()
    except Exception:  # pragma: no cover - failure bookkeeping must not raise
        logger.exception("failed to record classification failure for %s", book_id)
        session.rollback()


# ---------------------------------------------------------------- the batch loop


def _check_cancel(book_id: str) -> None:
    """用独立短事务看取消标志 / 书是否还在(worker 自己的 session 可能读不到别人的提交)。"""
    from novel_system.db.session import SessionLocal

    with SessionLocal() as session:
        book = session.get(StyleReferenceBook, book_id)
        if book is None:
            raise ClassificationCancelled("book deleted")
        if book.status == "cancelling":
            raise ClassificationCancelled("cancel requested")


def run_classification(
    session: Session,
    book_id: str,
    state: dict[str, Any],
    llm_client: Any,
    *,
    progress: ImportProgressReporter | NullImportProgress | None = None,
) -> dict[str, Any]:
    """从游标开始逐批分类直到 ``done``,再算统计、把书置 ready。每批提交一次。"""
    reporter = progress if progress is not None else NullImportProgress()
    repo = StyleReferenceRepository(session)
    rows = repo.list_paragraphs(book_id)
    spans = [(row.start_offset, row.end_offset, row.text or "") for row in rows]
    paragraph_ids = [row.paragraph_id for row in rows]
    # 强模型对锚定段的结果:续跑时从行里读(anchor_strong 阶段已落库的行)
    strong_types: list[str] = [str(row.paragraph_type or "") for row in rows]
    session.expunge_all()
    total = len(spans)
    anchor_size = min(int(state.get("anchor_size") or 0) or ANCHOR_SIZE, total)
    state["anchor_size"] = anchor_size
    state["total_paragraphs"] = total
    rest_size = total - anchor_size
    if total == 0:
        raise SegmentationLLMError("STYLE_REF_EMPTY_BOOK", "book has no paragraphs to classify")

    while True:
        cursor = dict(state.get("cursor") or {"phase": "anchor_strong", "offset": 0})
        phase = str(cursor.get("phase") or "anchor_strong")
        offset = int(cursor.get("offset") or 0)
        if phase == "done":
            break
        if phase == "anchor_strong":
            node, lo, hi = NODE_ANCHOR, 0, anchor_size
        elif phase == "anchor_fast":
            node, lo, hi = NODE_BULK, 0, anchor_size
        elif phase == "rest":
            node = str(state.get("rest_node") or NODE_BULK)
            lo, hi = anchor_size, total
        else:
            raise SegmentationLLMError("STYLE_REF_BAD_CURSOR", f"unknown classification phase {phase!r}")

        if lo + offset >= hi:
            # 阶段完成:推进到下一阶段
            if phase == "anchor_strong":
                next_phase = "anchor_fast" if rest_size else "done"
            elif phase == "anchor_fast":
                fast = [str(x) for x in (state.get("anchor_fast") or [])]
                agreement = _agreement(strong_types[:anchor_size], fast)
                state["fast_model_agreement"] = agreement
                state["fallback_to_strong"] = agreement < AGREEMENT_THRESHOLD
                state["rest_node"] = NODE_ANCHOR if state["fallback_to_strong"] else NODE_BULK
                next_phase = "rest"
            else:
                next_phase = "done"
            state["cursor"] = {"phase": next_phase, "offset": 0}
            _write_state(session, book_id, state)
            session.commit()
            continue

        _check_cancel(book_id)
        start = lo + offset
        end = min(hi, start + BATCH_SIZE)
        batch = spans[start:end]
        began = time.monotonic()
        results = classify_paragraph_batch(
            batch,
            node,
            llm_client,
            session=session,
            scope_id=book_id,
            batch_start_index=start,
        )
        elapsed = round(time.monotonic() - began, 1)
        if phase == "anchor_fast":
            fast = list(state.get("anchor_fast") or [])
            fast.extend(ptype for ptype, _conf in results)
            state["anchor_fast"] = fast
        else:
            try:
                session.execute(
                    update(StyleReferenceParagraph),
                    [
                        {
                            "paragraph_id": paragraph_ids[start + i],
                            "paragraph_type": ptype,
                            "classifier_confidence": float(conf),
                        }
                        for i, (ptype, conf) in enumerate(results)
                    ],
                )
            except StaleDataError as exc:
                # 段落行没了 = 书在这一批期间被删除
                session.rollback()
                raise ClassificationCancelled("paragraphs deleted") from exc
            for i, (ptype, _conf) in enumerate(results):
                strong_types[start + i] = ptype
        state["cursor"] = {"phase": phase, "offset": offset + len(batch)}
        state["batches_done"] = int(state.get("batches_done") or 0) + 1
        state["llm_calls"] = int(state.get("llm_calls") or 0) + 1
        seconds = list(state.get("batch_seconds") or [])
        seconds.append(elapsed)
        state["batch_seconds"] = seconds[-64:]
        state["heartbeat_at"] = _now_iso()
        _write_state(session, book_id, state)
        session.commit()
        reporter.classify_batch_done(node)

    return _finalize(session, book_id, state, spans, progress=reporter)


def _agreement(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    pairs = min(len(a), len(b))
    return sum(1 for i in range(pairs) if a[i] == b[i]) / pairs


def _finalize(
    session: Session,
    book_id: str,
    state: dict[str, Any],
    spans: list[tuple[int, int, str]],
    *,
    progress: ImportProgressReporter | NullImportProgress,
) -> dict[str, Any]:
    """全书分类完成:从段落行算统计与声音签名,写入 stats_json,书置 ready。"""
    from novel_system.services.style_reference.classification_stats import (
        compute_classification_stats,
    )
    from novel_system.services.style_reference.voice_signature import compute_voice_signature

    progress.phase("metrics")
    repo = StyleReferenceRepository(session)
    rows = repo.list_paragraphs(book_id)
    classifications = [
        ParagraphClassification(
            paragraph_index=int(row.paragraph_index),
            paragraph_type=str(row.paragraph_type or "narration"),
            confidence=float(row.classifier_confidence or 0.0),
            classifier_confidence_level=_confidence_level(float(row.classifier_confidence or 0.0)),
        )
        for row in rows
    ]
    calibration = build_calibration(
        anchor_size=int(state.get("anchor_size") or 0),
        total=len(rows),
        fast_model_agreement=state.get("fast_model_agreement"),
        fallback_to_strong=bool(state.get("fallback_to_strong")),
        rest_classifier=(
            None
            if state.get("rest_node") is None
            else ("strong_llm" if state.get("rest_node") == NODE_ANCHOR else "fast_llm")
        ),
    )
    seg_result = SegmentationResult(classifications=classifications, calibration=calibration)
    stats_update = compute_classification_stats(spans, seg_result)
    voice_signature = compute_voice_signature([body for _s, _e, body in spans])

    progress.phase("persist")
    book = session.get(StyleReferenceBook, book_id)
    if book is None:
        raise ClassificationCancelled("book deleted")
    state["state"] = "done"
    state["cursor"] = {"phase": "done", "offset": 0}
    state["finished_at"] = _now_iso()
    state["heartbeat_at"] = state["finished_at"]
    state["error"] = None
    stats = dict(book.stats_json or {})
    stats.update(stats_update)
    stats["voice_signature"] = voice_signature
    stats[CLASSIFICATION_STATE_KEY] = dict(state)
    book.stats_json = stats
    book.status = "ready"
    session.flush()
    session.commit()
    return state


def _confidence_level(conf: float) -> str:
    if conf >= 0.8:
        return "high"
    if conf >= 0.5:
        return "medium"
    return "low"


# ---------------------------------------------------------------- author controls


def request_classification_cancel(session: Session, book_id: str) -> dict[str, Any] | None:
    """请求取消(queued / running 才有意义):书 status → ``cancelling``,worker 在下一批边界退出。
    返回状态快照(带 ``cancel_requested``),书不存在返回 None。"""
    book = session.get(StyleReferenceBook, book_id)
    if book is None:
        return None
    state = classification_state(book)
    if state is None:
        return None
    if book.status == "ingesting" and state.get("state") in ("queued", "running"):
        book.status = "cancelling"
        session.flush()
    state["cancel_requested"] = book.status == "cancelling"
    return state


def requeue_classification(
    session: Session,
    book_id: str,
    *,
    kind: str | None = None,
    op_key: str | None = None,
    resume: bool,
) -> dict[str, Any]:
    """重新排队:``resume=True`` 保留游标(失败 / 取消 / 中断后继续),否则从头分类。"""
    book = session.get(StyleReferenceBook, book_id)
    if book is None:
        raise ValueError(f"book {book_id!r} not found")
    previous = classification_state(book)
    total = len(StyleReferenceRepository(session).list_paragraphs(book_id))
    if resume and previous is not None and previous.get("cursor"):
        state = dict(previous)
        state["state"] = "queued"
        state["attempt"] = int(previous.get("attempt") or 0) + 1
        state["error"] = None
        state["finished_at"] = None
        state["heartbeat_at"] = None
        if kind:
            state["kind"] = kind
        if op_key:
            state["op_key"] = op_key
    else:
        state = build_classification_state(
            kind=kind or (previous or {}).get("kind") or "reclassify",
            op_key=op_key or (previous or {}).get("op_key"),
            total_paragraphs=total,
        )
    _write_state(session, book_id, state, status="ingesting")
    return state


# ---------------------------------------------------------------- startup recovery


def recover_classification_jobs(
    session: Session,
    *,
    llm_client: Any | None,
    llm_enabled: bool,
    dispatch: Any,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """启动恢复:``ingesting`` 的书——queued 直接重派;running 但心跳过期视为中断,重新排队续跑;
    LLM 未启用时标 failed(游标保留,启用后可「继续分类」)。"""
    current = now or datetime.now(UTC)
    repo = StyleReferenceRepository(session)
    dispatched: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    to_dispatch: list[tuple[str, str | None]] = []
    # 取消到一半 worker 就死了的:直接收尾成 cancelled(游标保留,可续跑)
    for book in repo.list_books(status="cancelling"):
        state = classification_state(book)
        if state is None or state.get("state") not in ("queued", "running"):
            continue
        state["state"] = "cancelled"
        state["error"] = {"code": CANCEL_ERROR_CODE, "message": "cancelled; worker did not survive"}
        state["finished_at"] = _now_iso()
        _write_state(session, book.book_id, state, status="failed")
        failed.append(book.book_id)
    for book in repo.list_books(status="ingesting"):
        state = classification_state(book)
        if state is None:
            continue
        current_state = str(state.get("state") or "")
        if current_state == "running" and not _heartbeat_stale(book.updated_at, now=current):
            skipped.append(book.book_id)
            continue
        if current_state not in ("queued", "running"):
            continue
        if not llm_enabled or llm_client is None:
            state["state"] = "failed"
            state["error"] = {
                "code": LLM_REQUIRED_AFTER_RESTART_CODE,
                "message": "classification could not restart because no LLM is configured",
            }
            state["finished_at"] = _now_iso()
            _write_state(session, book.book_id, state, status="failed")
            failed.append(book.book_id)
            continue
        if current_state == "running":
            state["state"] = "queued"
            state["attempt"] = int(state.get("attempt") or 0) + 1
            state["heartbeat_at"] = None
            state["error"] = {"code": INTERRUPTED_ERROR_CODE, "message": "resumed after restart"}
            _write_state(session, book.book_id, state)
        to_dispatch.append((book.book_id, state.get("op_key")))
    session.commit()
    for book_id, op_key in to_dispatch:
        dispatch(book_id, llm_client, op_key)
        dispatched.append(book_id)
    return {"dispatched": dispatched, "failed": failed, "active_skipped": skipped}


__all__ = [
    "CANCEL_ERROR_CODE",
    "CLASSIFICATION_STALE_SECONDS",
    "CLASSIFICATION_STATE_KEY",
    "INTERRUPTED_ERROR_CODE",
    "LLM_REQUIRED_AFTER_RESTART_CODE",
    "UNCLASSIFIED_PARAGRAPH_TYPE",
    "ClassificationCancelled",
    "build_classification_state",
    "classification_op_key",
    "classification_state",
    "recover_classification_jobs",
    "request_classification_cancel",
    "requeue_classification",
    "run_classification",
    "shutdown_style_reference_classification_executor",
    "start_style_reference_classification_worker",
]
