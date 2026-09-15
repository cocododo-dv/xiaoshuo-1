"""LLM 段落分类器(锚定集校准)。

依据《风格参考模块重构执行手册 v1.1》§6.2:
- 前 200 段走 `style_ref_paragraph_classify_anchor`(quality_balanced)做锚定
- 锚定集再走 `style_ref_paragraph_classify_bulk`(local_fast)对照,agreement >= 0.85 余段用快模型,
  < 0.85 余段整本走强模型
- 2026-09-15 严格 LLM:没有任何启发式兜底。LLM 可用时每一段都由 LLM 分类(不再有余段上限),
  LLM 调用失败就是失败(``ClassificationFailedError``);大书的分类由可续跑的后台任务逐批执行
  (``import_job.py``),这里的 ``classify_paragraph_batch`` 是任务每一批调用的原子单元。

分类器经共享的 `build_llm_request` 构造请求,再经 `execute_accounted_call` 统一落父调用和物理 attempt;
调用方必须显式提供持久 session 与稳定的参考书 scope。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMCallContext,
    execute_accounted_call,
    is_llm_control_plane_failure,
)
from novel_system.services.llm_client import (
    build_llm_request,
    load_model_routing_config,
    resolve_node_route,
)
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.style_reference.import_progress import ClassificationProgress
from novel_system.services.style_reference.text_utils import compact_ws
from novel_system.services.style_reference.untrusted_data import (
    UntrustedPayload,
    render_untrusted_system_prompt,
    render_untrusted_user_prompt,
)
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)

logger = logging.getLogger(__name__)

NODE_ANCHOR = "style_ref_paragraph_classify_anchor"
NODE_BULK = "style_ref_paragraph_classify_bulk"

ANCHOR_SIZE = 200
AGREEMENT_THRESHOLD = 0.85

# 一次 LLM 调用分类多少段(避免 prompt 过长)
BATCH_SIZE = 25


class SegmentationLLMError(Exception):
    """段落分类 LLM 调用失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _batch_count(paragraph_count: int) -> int:
    return (paragraph_count + BATCH_SIZE - 1) // BATCH_SIZE if paragraph_count > 0 else 0


def planned_batches(total_paragraphs: int, anchor_size: int | None = None) -> int:
    """整本 LLM 分类的批次数:锚定集×强模型 + (有余段时)锚定集×快模型 + 余段。"""
    total = max(0, int(total_paragraphs))
    anchors = min(ANCHOR_SIZE if anchor_size is None else int(anchor_size), total)
    rest = total - anchors
    anchor_batches = _batch_count(anchors)
    return anchor_batches + (anchor_batches + _batch_count(rest) if rest else 0)


def classify_with_llm(
    paragraphs: list[tuple[int, int, str]],
    llm_client: Any,
    *,
    session: Session,
    scope_id: str,
    progress: ClassificationProgress | None = None,
) -> SegmentationResult:
    """锚定集校准的 LLM 段落分类——整本都由 LLM 分类(2026-09-15 起没有余段上限)。

    ``progress`` 是操作进度登记簿(见 ``import_progress``):先按三步的批次数申报总量
    (锚定集×强模型 + 锚定集×快模型 + 余段;书不超过锚定集时只有第一项),每批完成汇报一次。
    这是同步整本路径(小书 / 离线夹具);大书走 ``import_job`` 的可续跑批次循环,逐批调用
    ``classify_paragraph_batch``。
    """
    total = len(paragraphs)
    anchor_size = min(ANCHOR_SIZE, total)
    anchor_paras = paragraphs[:anchor_size]
    rest = paragraphs[anchor_size:]
    if progress is not None:
        progress.classify_plan(planned_batches(total), "llm")
    on_batch = progress.classify_batch_done if progress is not None else None

    # Step 1: 锚定集走强模型
    anchor_strong = _classify_via_node(
        anchor_paras,
        NODE_ANCHOR,
        llm_client,
        session=session,
        scope_id=scope_id,
        on_batch=on_batch,
    )

    agreement: float | None = None
    fallback_to_strong = False
    rest_classifier: str | None = None
    rest_classified: list[tuple[str, float]] = []
    if rest:
        # Step 2: 锚定集再走快模型,校准一致性
        anchor_fast = _classify_via_node(
            anchor_paras,
            NODE_BULK,
            llm_client,
            session=session,
            scope_id=scope_id,
            on_batch=on_batch,
        )
        agreement = _compute_agreement(anchor_strong, anchor_fast)
        fallback_to_strong = agreement < AGREEMENT_THRESHOLD

        # Step 3: 余下段落按选定路径
        rest_classified = _classify_via_node(
            rest,
            NODE_ANCHOR if fallback_to_strong else NODE_BULK,
            llm_client,
            session=session,
            scope_id=scope_id,
            on_batch=on_batch,
        )
        rest_classifier = "strong_llm" if fallback_to_strong else "fast_llm"

    merged = merge_classifications(anchor_strong, rest_classified)
    calibration = build_calibration(
        anchor_size=anchor_size,
        total=total,
        fast_model_agreement=agreement,
        fallback_to_strong=fallback_to_strong,
        rest_classifier=rest_classifier,
    )
    return SegmentationResult(classifications=merged, calibration=calibration)


def merge_classifications(
    anchor_strong: list[tuple[str, float]],
    rest_classified: list[tuple[str, float]],
) -> list[ParagraphClassification]:
    """锚定段以强模型结果为准,余段接在后面,按段落索引编号。"""
    merged: list[ParagraphClassification] = []
    anchor_size = len(anchor_strong)
    for idx, (ptype, conf) in enumerate(anchor_strong):
        merged.append(
            ParagraphClassification(
                paragraph_index=idx,
                paragraph_type=ptype,
                confidence=conf,
                classifier_confidence_level=_confidence_level(conf),
            )
        )
    for offset, (ptype, conf) in enumerate(rest_classified):
        merged.append(
            ParagraphClassification(
                paragraph_index=anchor_size + offset,
                paragraph_type=ptype,
                confidence=conf,
                classifier_confidence_level=_confidence_level(conf),
            )
        )
    return merged


def build_calibration(
    *,
    anchor_size: int,
    total: int,
    fast_model_agreement: float | None,
    fallback_to_strong: bool,
    rest_classifier: str | None,
) -> dict[str, Any]:
    """``stats_json.classifier_calibration``:每一段都是 LLM 分的,没有启发式份额。"""
    return {
        "anchor_size": int(anchor_size),
        "fast_model_agreement": fast_model_agreement,
        "fallback_to_strong": bool(fallback_to_strong),
        "fallback_to_heuristic": False,
        "rest_classifier": rest_classifier,
        "llm_classified_paragraphs": int(total),
        "heuristic_classified_paragraphs": 0,
    }


def _confidence_level(conf: float) -> str:
    if conf >= 0.8:
        return "high"
    if conf >= 0.5:
        return "medium"
    return "low"


def _compute_agreement(
    a: list[tuple[str, float]], b: list[tuple[str, float]]
) -> float:
    """两个 paragraph_type 序列的一致性比例。"""
    if not a or not b:
        return 0.0
    pairs = min(len(a), len(b))
    matched = sum(1 for i in range(pairs) if a[i][0] == b[i][0])
    return matched / pairs


def _classify_via_node(
    paragraphs: list[tuple[int, int, str]],
    node_id: str,
    llm_client: Any,
    *,
    session: Session,
    scope_id: str,
    on_batch: Callable[[str], None] | None = None,
) -> list[tuple[str, float]]:
    """对一批段落调指定 LLM 节点,返回 [(paragraph_type, confidence), ...]。

    内部按 BATCH_SIZE 分批调用 ``classify_paragraph_batch``;每批完成后调用 ``on_batch(node_id)``
    (操作进度)。
    """
    if not paragraphs:
        return []
    results: list[tuple[str, float]] = []
    for batch_start in range(0, len(paragraphs), BATCH_SIZE):
        batch = paragraphs[batch_start : batch_start + BATCH_SIZE]
        results.extend(
            classify_paragraph_batch(
                batch,
                node_id,
                llm_client,
                session=session,
                scope_id=scope_id,
                batch_start_index=batch_start,
            )
        )
        if on_batch is not None:
            on_batch(node_id)
    return results


def _load_node_template(node_id: str) -> tuple[Any, Any]:
    try:
        routing = load_model_routing_config()
        try:
            # DB node_routing 优先、yaml task_routing 兜底(教训见 resolve_node_route)。
            task_config = resolve_node_route(routing, node_id)
        except KeyError:
            raise SegmentationLLMError(
                "STYLE_REF_LLM_ROUTE_MISSING",
                f"task routing not configured for node {node_id!r}",
            ) from None
        templates = load_prompt_templates()
        if node_id not in templates:
            raise SegmentationLLMError(
                "STYLE_REF_LLM_PROMPT_MISSING",
                f"prompt template not configured for node {node_id!r}",
            )
        return task_config, templates[node_id]
    except SegmentationLLMError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise SegmentationLLMError(
            "STYLE_REF_LLM_CONFIG_LOAD_FAILED",
            f"failed to load routing or prompt: {exc}",
        ) from exc


def classify_paragraph_batch(
    batch: list[tuple[int, int, str]],
    node_id: str,
    llm_client: Any,
    *,
    session: Session,
    scope_id: str,
    batch_start_index: int = 0,
) -> list[tuple[str, float]]:
    """一次 LLM 调用分类一批段落(≤ BATCH_SIZE),返回与 ``batch`` 等长的 [(type, confidence)]。

    后台导入任务的原子单元:每批的结果由调用方落库并推进游标,进程崩溃后从游标续跑。
    """
    if not batch:
        return []
    task_config, template = _load_node_template(node_id)
    batch_payload = {
        "paragraphs": [
            {"paragraph_index": batch_start_index + i, "text": compact_ws(body)[:600]}
            for i, (_s, _e, body) in enumerate(batch)
        ]
    }
    try:
        task_prompt = template.task_prompt.replace(
            "{paragraphs}", "See the bounded payload below."
        )
        user_prompt = render_untrusted_user_prompt(
            task_prompt,
            UntrustedPayload(batch_payload),
            kind=node_id,
        )
        system_prompt = render_untrusted_system_prompt(template.system_prompt)
    except Exception:  # pylint: disable=broad-except
        raise SegmentationLLMError(
            "STYLE_REF_LLM_PROMPT_RENDER_FAILED",
            f"failed to render segmentation prompt for node {node_id!r}",
        ) from None
    request = build_llm_request(
        task_config,
        node_id=node_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_schema=template.structured_schema,
    )
    try:
        response = execute_accounted_call(
            session,
            llm_client,
            request,
            LLMCallContext(
                scope_type="style_reference_book",
                scope_id=scope_id,
                node_id=node_id,
                step=f"paragraph_classification:{batch_start_index}",
            ),
            llm_call_id=f"llm_style_segment_{uuid.uuid4().hex}",
        )
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, LLMAccountingError) or is_llm_control_plane_failure(exc):
            raise
        raise SegmentationLLMError(
            "STYLE_REF_LLM_GENERATE_FAILED",
            f"accounted LLM execution failed for node {node_id!r}: {exc}",
        ) from exc

    parsed = _parse_response(response)
    if len(parsed) != len(batch):
        logger.warning(
            "segmentation LLM returned %d classifications for batch of %d; padding with narration",
            len(parsed),
            len(batch),
        )
        while len(parsed) < len(batch):
            parsed.append(("narration", 0.3))
        parsed = parsed[: len(batch)]
    return parsed


def _parse_response(response: Any) -> list[tuple[str, float]]:
    """从 LLMResponse.structured_output 解析 [(paragraph_type, confidence), ...]。"""
    structured = getattr(response, "structured_output", None) or {}
    classifications = structured.get("classifications") or []
    if not isinstance(classifications, list):
        return []
    parsed: list[tuple[str, float]] = []
    confidence_map = {"high": 0.9, "medium": 0.6, "low": 0.3}
    for item in classifications:
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("paragraph_type") or "narration")
        conf_label = str(item.get("confidence") or "medium")
        conf = confidence_map.get(conf_label, 0.5)
        parsed.append((ptype, conf))
    return parsed
