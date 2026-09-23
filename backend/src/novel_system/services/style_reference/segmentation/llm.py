"""LLM 段落分类的原子单元(2026-09-23 风格参考 v3)。

整本书的分类是分类作业(``import_job``,作业表 kind=classify)逐批执行的——没有启发式兜底,
也没有同步的整本分类路径。这里只放作业用到的纯零件:

- **节点运行时**(路由 + 提示词模板)每个作业载一次(``load_classification_runtimes``);
  旧实现每一批重新解析一次路由与模板(实测 447 ms / 批);
- **锚定集**在全书上分层抽样(``select_anchor_positions``):按位置等分成 ``ANCHOR_SIZE`` 层,
  每层按书的种子确定性地挑一段,跳过章题、书前的书名页 / 简介块、副文本与场分隔行——不再是「前 200 段」;
- **按字数自适应分批**(``plan_batches``):一批 ≤ ``BATCH_MAX_CHARS`` 字且 ≤ ``BATCH_MAX_PARAGRAPHS`` 段;
- **一批一次记账调用**(``classify_batch``):每段带批外的前 / 后一段作只读上下文;
- **严格解析**(``parse_batch_output``):按 ``paragraph_index`` 对齐,``paragraph_type`` 按
  ``schemas.ParagraphType`` 校验;缺段、多出、重复、非法类型一律 ``ClassificationBatchMismatch``
  (作业整批重试),绝不按位置对齐、绝不补「叙述 0.3」。

``positions`` 指段落在按 ``paragraph_index`` 升序排好的列表里的位置(0..n-1);发给模型、从模型
收回的是真实的 ``paragraph_index``(老书刷新过可能不连续)。
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from novel_system.services.style_reference.schemas import ParagraphType
from novel_system.services.style_reference.segmentation.heuristic import is_title_paragraph
from novel_system.services.style_reference.text_utils import (
    compact_ws,
    is_paratext_paragraph,
    is_scene_break_paragraph,
)
from novel_system.services.style_reference.untrusted_data import (
    UntrustedPayload,
    render_untrusted_system_prompt,
    render_untrusted_user_prompt,
)

logger = logging.getLogger(__name__)

NODE_ANCHOR = "style_ref_paragraph_classify_anchor"
NODE_BULK = "style_ref_paragraph_classify_bulk"
CLASSIFY_NODE_IDS: tuple[str, ...] = (NODE_ANCHOR, NODE_BULK)

ANCHOR_SIZE = 200
AGREEMENT_THRESHOLD = 0.85

# 一批的上限(v3 I16):按字数自适应——旧的固定 25 段 / 批让 26,616 段的书要 1,065 次调用;
# ≤6,000 字且 ≤100 段时同一本书约 314 批。一段超长时只送前 PARAGRAPH_TEXT_MAX_CHARS 字(判主导类型足够)。
BATCH_MAX_CHARS = 6000
BATCH_MAX_PARAGRAPHS = 100
PARAGRAPH_TEXT_MAX_CHARS = 1500
# 批外相邻段的只读上下文:前一段取末尾、后一段取开头各这么多字。
CONTEXT_MAX_CHARS = 120
# 估算用:一段在 JSON 载荷里的固定开销(键名、引号、缩进)。
ITEM_OVERHEAD_CHARS = 48

VALID_PARAGRAPH_TYPES: frozenset[str] = frozenset(item.value for item in ParagraphType)
_CONFIDENCE_MAP = {"high": 0.9, "medium": 0.6, "low": 0.3}
_UNKNOWN_CONFIDENCE = 0.5

# 书前块(第一个章题之前)只在它很短时当书名页 / 简介跳过:不超过全书的这个比例。
_FRONT_MATTER_MAX_SHARE = 0.05


class SegmentationLLMError(Exception):
    """段落分类的一次 LLM 调用失败(路由 / 模板缺失、渲染失败、调用失败、输出不合格)。"""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class ClassificationBatchMismatch(SegmentationLLMError):
    """模型的输出与这一批对不上(缺段 / 多出 / 重复 / 非法类型 / 缺字段):整批重试。"""

    def __init__(self, problems: Sequence[str], *, expected: int, received: int) -> None:
        shown = list(problems)[:8]
        super().__init__(
            "STYLE_REFERENCE_CLASSIFY_OUTPUT_MISMATCH",
            f"classification output does not match the batch ({len(problems)} problem(s)): "
            + "; ".join(shown),
            details={
                "problems": shown,
                "problem_count": len(problems),
                "expected": expected,
                "received": received,
            },
        )
        self.problems = list(problems)


@dataclass(frozen=True)
class NodeRuntime:
    """一个分类节点在这个作业里的路由与模板(作业开始时载一次)。"""

    node_id: str
    route: Any
    template: Any

    @property
    def prompt_version(self) -> str:
        return str(getattr(self.template, "version", "") or "")

    @property
    def route_key(self) -> tuple[str, str]:
        """(provider_id 或 provider, model):两个节点的这一对相同 = 同一个模型。"""
        provider = getattr(self.route, "provider_id", None) or getattr(self.route, "provider", None) or ""
        return (str(provider), str(getattr(self.route, "model", "") or ""))


def load_classification_runtimes(
    node_ids: Sequence[str] = CLASSIFY_NODE_IDS,
) -> dict[str, NodeRuntime]:
    """载入分类节点的路由(DB node_routing 优先、yaml 兜底)与提示词模板——每个作业一次。"""
    try:
        routing = load_model_routing_config()
        templates = load_prompt_templates()
    except Exception as exc:  # pylint: disable=broad-except
        raise SegmentationLLMError(
            "STYLE_REFERENCE_CLASSIFY_CONFIG_LOAD_FAILED",
            f"failed to load model routing or prompt templates: {exc}",
        ) from exc
    runtimes: dict[str, NodeRuntime] = {}
    for node_id in node_ids:
        try:
            route = resolve_node_route(routing, node_id)
        except KeyError:
            raise SegmentationLLMError(
                "STYLE_REFERENCE_CLASSIFY_ROUTE_MISSING",
                f"task routing not configured for node {node_id!r}",
                details={"node_id": node_id},
            ) from None
        template = templates.get(node_id)
        if template is None:
            raise SegmentationLLMError(
                "STYLE_REFERENCE_CLASSIFY_PROMPT_MISSING",
                f"prompt template not configured for node {node_id!r}",
                details={"node_id": node_id},
            )
        runtimes[node_id] = NodeRuntime(node_id=node_id, route=route, template=template)
    return runtimes


def same_model(first: NodeRuntime, second: NodeRuntime) -> bool:
    """两个节点解析到同一个 provider + model:锚定集的快模型对照没有意义,跳过。"""
    return first.route_key == second.route_key


# ---------------------------------------------------------------- anchors / batches


def _anchor_candidate(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if is_title_paragraph(stripped) or is_paratext_paragraph(stripped) or is_scene_break_paragraph(stripped):
        return False
    return True


def select_anchor_positions(
    texts: Sequence[str],
    *,
    seed: str,
    anchor_size: int | None = None,
) -> list[int]:
    """锚定集:在全书上按位置分层抽样(升序的段落位置)。

    书不超过锚定集时全书都是锚定段(没有余段,也就没有快模型对照)。否则先跳过书前的书名页 / 简介块
    (第一个章题之前、不超过全书 5% 的块),把剩下的位置等分成 ``anchor_size`` 层,每层按书的种子打乱后
    取第一个「可当锚定段」的段(不是章题 / 副文本 / 场分隔行 / 空段;一层里一个都没有时取打乱后的第一段)
    ——续跑时同一本书挑出同一组。只检查被试到的段,26,000 段的书也只看几百段。
    """
    size = ANCHOR_SIZE if anchor_size is None else int(anchor_size)
    total = len(texts)
    if total <= size:
        return list(range(total))
    front_limit = max(3, int(total * _FRONT_MATTER_MAX_SHARE))
    first_title = next(
        (pos for pos in range(min(total, front_limit + 1)) if is_title_paragraph(str(texts[pos] or "").strip())),
        None,
    )
    skip_before = first_title if first_title is not None and 0 < first_title <= front_limit else 0
    span = total - skip_before
    layers = min(size, span)
    rng = random.Random(f"style-reference-anchors:{seed}")
    picks: list[int] = []
    for layer in range(layers):
        lo = skip_before + (layer * span) // layers
        hi = skip_before + ((layer + 1) * span) // layers
        order = list(range(lo, max(hi, lo + 1)))
        rng.shuffle(order)
        picks.append(next((pos for pos in order if _anchor_candidate(texts[pos])), order[0]))
    return sorted(set(picks))


def _clipped_length(text: str) -> int:
    return min(len(str(text or "")), PARAGRAPH_TEXT_MAX_CHARS)


def plan_batches(
    positions: Sequence[int],
    texts: Sequence[str],
    *,
    max_chars: int | None = None,
    max_paragraphs: int | None = None,
) -> list[list[int]]:
    """按字数自适应分批(输入顺序保持):一批 ≤ ``max_chars`` 字(超长段按送出的截断长度算)
    且 ≤ ``max_paragraphs`` 段;单段超过字数上限时自成一批。缺省取模块常量(调用时读)。"""
    max_chars = BATCH_MAX_CHARS if max_chars is None else int(max_chars)
    max_paragraphs = BATCH_MAX_PARAGRAPHS if max_paragraphs is None else int(max_paragraphs)
    batches: list[list[int]] = []
    current: list[int] = []
    current_chars = 0
    for pos in positions:
        size = _clipped_length(texts[pos])
        if current and (current_chars + size > max_chars or len(current) >= max_paragraphs):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(int(pos))
        current_chars += size
    if current:
        batches.append(current)
    return batches


def _clip(text: str, limit: int) -> str:
    return compact_ws(text)[:limit]


def _tail(text: str, limit: int) -> str:
    compact = compact_ws(text)
    return compact if len(compact) <= limit else "…" + compact[-limit:]


def _head(text: str, limit: int) -> str:
    compact = compact_ws(text)
    return compact if len(compact) <= limit else compact[:limit] + "…"


def batch_items(
    positions: Sequence[int],
    texts: Sequence[str],
    indexes: Sequence[int],
) -> list[dict[str, Any]]:
    """一批的载荷条目:``paragraph_index`` + 正文;前 / 后一段不在本批里时带只读上下文。"""
    in_batch = set(positions)
    items: list[dict[str, Any]] = []
    for pos in positions:
        item: dict[str, Any] = {
            "paragraph_index": int(indexes[pos]),
            "text": _clip(texts[pos], PARAGRAPH_TEXT_MAX_CHARS),
        }
        if pos - 1 >= 0 and (pos - 1) not in in_batch:
            item["context_before"] = _tail(texts[pos - 1], CONTEXT_MAX_CHARS)
        if pos + 1 < len(texts) and (pos + 1) not in in_batch:
            item["context_after"] = _head(texts[pos + 1], CONTEXT_MAX_CHARS)
        items.append(item)
    return items


def estimated_message_chars(
    runtime: NodeRuntime,
    positions: Sequence[int],
    texts: Sequence[str],
) -> int:
    """一批请求的大致字数(系统提示 + 任务提示 + 载荷),费用预估用。"""
    template = runtime.template
    fixed = len(str(getattr(template, "system_prompt", "") or "")) + len(
        str(getattr(template, "task_prompt", "") or "")
    )
    fixed += 600  # 不可信数据边界与系统约束
    in_batch = set(positions)
    chars = 0
    for pos in positions:
        chars += _clipped_length(texts[pos]) + ITEM_OVERHEAD_CHARS
        if pos - 1 >= 0 and (pos - 1) not in in_batch:
            chars += min(len(str(texts[pos - 1] or "")), CONTEXT_MAX_CHARS) + 24
        if pos + 1 < len(texts) and (pos + 1) not in in_batch:
            chars += min(len(str(texts[pos + 1] or "")), CONTEXT_MAX_CHARS) + 24
    return fixed + chars


# ---------------------------------------------------------------- one batch = one call


def build_batch_request(runtime: NodeRuntime, items: list[dict[str, Any]]) -> Any:
    """把一批条目渲染成记账调用的 ``LLMRequest``(载荷在唯一的不可信数据边界里)。"""
    template = runtime.template
    try:
        task_prompt = str(template.task_prompt).replace("{paragraphs}", "See the bounded payload below.")
        user_prompt = render_untrusted_user_prompt(
            task_prompt,
            UntrustedPayload({"paragraphs": items}),
            kind=runtime.node_id,
        )
        system_prompt = render_untrusted_system_prompt(template.system_prompt)
    except Exception:  # pylint: disable=broad-except
        raise SegmentationLLMError(
            "STYLE_REFERENCE_CLASSIFY_PROMPT_RENDER_FAILED",
            f"failed to render the classification prompt for node {runtime.node_id!r}",
            details={"node_id": runtime.node_id},
        ) from None
    return build_llm_request(
        runtime.route,
        node_id=runtime.node_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_schema=template.structured_schema,
    )


def _call_batch(
    runtime: NodeRuntime,
    positions: Sequence[int],
    texts: Sequence[str],
    indexes: Sequence[int],
    llm_client: Any,
    *,
    session: Session,
    scope_id: str,
    step: str,
) -> Any:
    items = batch_items(positions, texts, indexes)
    request = build_batch_request(runtime, items)
    try:
        response = execute_accounted_call(
            session,
            llm_client,
            request,
            LLMCallContext(
                scope_type="style_reference_book",
                scope_id=scope_id,
                node_id=runtime.node_id,
                step=step,
            ),
            llm_call_id=f"llm_style_segment_{uuid.uuid4().hex}",
        )
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, LLMAccountingError) or is_llm_control_plane_failure(exc):
            raise
        raise SegmentationLLMError(
            "STYLE_REFERENCE_CLASSIFY_LLM_CALL_FAILED",
            f"accounted LLM execution failed for node {runtime.node_id!r}: {exc}",
            details={"node_id": runtime.node_id, "error_type": type(exc).__name__},
        ) from exc
    return getattr(response, "structured_output", None)


def classify_batch(
    runtime: NodeRuntime,
    positions: Sequence[int],
    texts: Sequence[str],
    indexes: Sequence[int],
    llm_client: Any,
    *,
    session: Session,
    scope_id: str,
    step: str,
) -> dict[int, tuple[str, float]]:
    """一次记账 LLM 调用分类一批段落,返回 ``{paragraph_index: (type, confidence)}``(与本批一一对应)。

    ``session`` 只给记账用(``execute_accounted_call`` 会自己提交):并行的批次各用各的会话。
    记账 / 控制面失败原样抛出(不重试、不降级);其余调用失败是 ``SegmentationLLMError``,
    输出对不上是 ``ClassificationBatchMismatch``——两者由作业整批重试。
    """
    if not positions:
        return {}
    structured = _call_batch(
        runtime, positions, texts, indexes, llm_client, session=session, scope_id=scope_id, step=step
    )
    return parse_batch_output(structured, [int(indexes[pos]) for pos in positions])


def classify_batch_partial(
    runtime: NodeRuntime,
    positions: Sequence[int],
    texts: Sequence[str],
    indexes: Sequence[int],
    llm_client: Any,
    *,
    session: Session,
    scope_id: str,
    step: str,
) -> tuple[dict[int, tuple[str, float]], list[str]]:
    """同 :func:`classify_batch`,但输出按条收:返回(自身合格的每一条,问题清单)。

    分类作业用它:一批 100 段里模型漏了一段 / 给了一个非法类型,合格的 99 段照收,只把缺的段再发一次
    (整批重发同样的 100 段,模型常常在同一处再漏)。调用失败仍抛 ``SegmentationLLMError``。
    """
    if not positions:
        return {}, []
    structured = _call_batch(
        runtime, positions, texts, indexes, llm_client, session=session, scope_id=scope_id, step=step
    )
    results, problems, _received = _parse_batch_items(structured, [int(indexes[pos]) for pos in positions])
    return results, problems


def _as_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _parse_batch_items(
    structured: Any,
    expected_indexes: Sequence[int],
) -> tuple[dict[int, tuple[str, float]], list[str], int]:
    """逐条核对一批的输出:返回(自身合格的条目,问题清单,收到的条数)。

    合格 = 段号属于本批、类型是 8 类之一、这个段号只出现一次;出现不止一次的段号一条都不收(不知道该信哪条)。
    置信度标签缺失 / 不认识时记 0.5(不算问题)。
    """
    expected = [int(index) for index in expected_indexes]
    expected_set = set(expected)
    classifications = structured.get("classifications") if isinstance(structured, Mapping) else None
    if not isinstance(classifications, list):
        return {}, ["output has no classifications list"], 0
    problems: list[str] = []
    results: dict[int, tuple[str, float]] = {}
    duplicated: set[int] = set()
    for position, item in enumerate(classifications):
        if not isinstance(item, Mapping):
            problems.append(f"item {position} is not an object")
            continue
        index = _as_index(item.get("paragraph_index"))
        if index is None:
            problems.append(f"item {position} has no integer paragraph_index")
            continue
        if index not in expected_set:
            problems.append(f"paragraph_index {index} is not in this batch")
            continue
        if index in results or index in duplicated:
            problems.append(f"paragraph_index {index} appears more than once")
            duplicated.add(index)
            results.pop(index, None)
            continue
        ptype = str(item.get("paragraph_type") or "").strip()
        if ptype not in VALID_PARAGRAPH_TYPES:
            problems.append(f"paragraph_index {index} has invalid paragraph_type {ptype!r}")
            continue
        label = str(item.get("confidence") or "").strip().lower()
        results[index] = (ptype, _CONFIDENCE_MAP.get(label, _UNKNOWN_CONFIDENCE))
    missing = [index for index in expected if index not in results]
    if missing:
        preview = ", ".join(str(index) for index in missing[:6])
        problems.append(f"{len(missing)} paragraph(s) missing (e.g. {preview})")
    return results, problems, len(classifications)


def parse_batch_output(
    structured: Any,
    expected_indexes: Sequence[int],
) -> dict[int, tuple[str, float]]:
    """严格解析一批的输出:按 ``paragraph_index`` 对齐,类型必须是 8 类之一,每段恰好一次。

    置信度标签缺失 / 不认识时记 0.5(不为它重试);其余任何不一致都抛
    ``ClassificationBatchMismatch``——调用方整批重试,绝不按位置对齐或补默认类型。
    """
    results, problems, received = _parse_batch_items(structured, expected_indexes)
    if problems:
        raise ClassificationBatchMismatch(problems, expected=len(list(expected_indexes)), received=received)
    return results


# ---------------------------------------------------------------- calibration


def agreement(strong: Mapping[int, str], fast: Mapping[int, str]) -> float | None:
    """锚定集上强 / 快模型结果的一致率(两边都有的段);没有共同段返回 None。"""
    common = [index for index in strong if index in fast]
    if not common:
        return None
    return sum(1 for index in common if strong[index] == fast[index]) / len(common)


def build_calibration(
    *,
    anchor_size: int,
    total: int,
    fast_model_agreement: float | None,
    fallback_to_strong: bool,
    rest_classifier: str | None,
    calibration_skipped: str | None = None,
) -> dict[str, Any]:
    """``stats_json.classifier_calibration``:每一段都是 LLM 分的,没有启发式份额。"""
    return {
        "anchor_size": int(anchor_size),
        "anchor_sampling": "stratified",
        "fast_model_agreement": fast_model_agreement,
        "fallback_to_strong": bool(fallback_to_strong),
        "fallback_to_heuristic": False,
        "rest_classifier": rest_classifier,
        "calibration_skipped": calibration_skipped,
        "llm_classified_paragraphs": int(total),
        "heuristic_classified_paragraphs": 0,
    }


def confidence_level(conf: float) -> str:
    if conf >= 0.8:
        return "high"
    if conf >= 0.5:
        return "medium"
    return "low"


__all__ = [
    "AGREEMENT_THRESHOLD",
    "ANCHOR_SIZE",
    "BATCH_MAX_CHARS",
    "BATCH_MAX_PARAGRAPHS",
    "CLASSIFY_NODE_IDS",
    "CONTEXT_MAX_CHARS",
    "ClassificationBatchMismatch",
    "NODE_ANCHOR",
    "NODE_BULK",
    "NodeRuntime",
    "PARAGRAPH_TEXT_MAX_CHARS",
    "SegmentationLLMError",
    "VALID_PARAGRAPH_TYPES",
    "agreement",
    "batch_items",
    "build_batch_request",
    "build_calibration",
    "classify_batch",
    "classify_batch_partial",
    "confidence_level",
    "estimated_message_chars",
    "load_classification_runtimes",
    "parse_batch_output",
    "plan_batches",
    "same_model",
    "select_anchor_positions",
]
