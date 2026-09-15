"""Style Reference v1.1 段落分类调度入口。

参见《风格参考模块重构执行手册 v1.1》§6.2(段落分类锚定集校准)。

调度逻辑(2026-09-15 严格 LLM):
- `llm_enabled=True` 且 `llm_client` 非空 → 整本走 LLM 锚定校准;LLM 调用失败就是失败
  (``ClassificationFailedError``,502 + author_action),**不再降级到启发式**;
- `llm_enabled=False` → 启发式,只是**离线夹具模式**(测试与本地语料工具):产品路由在 LLM
  未启用时已经 409 ``STYLE_REFERENCE_LLM_REQUIRED``,永远不会带着 ``llm_enabled=False`` 走到这里。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.llm_accounting import (
    LLMAccountingError,
    is_llm_control_plane_failure,
)
from novel_system.services.style_reference.errors import ClassificationFailedError
from novel_system.services.style_reference.import_progress import ClassificationProgress
from novel_system.services.style_reference.segmentation.heuristic import classify_heuristic
from novel_system.services.style_reference.segmentation.llm import (
    SegmentationLLMError,
    classify_with_llm,
)
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)

logger = logging.getLogger(__name__)


def classify_paragraphs(
    paragraphs: list[tuple[int, int, str]],
    *,
    llm_enabled: bool,
    llm_client: Any | None = None,
    session: Session | None = None,
    scope_id: str | None = None,
    progress: ClassificationProgress | None = None,
) -> SegmentationResult:
    """调度段落分类。`paragraphs` 元素是 `(start_offset, end_offset, body)`。

    ``progress``(操作进度登记簿)只在 LLM 路径按批次推进;离线夹具路径申报 0 批即完成。
    """
    if not paragraphs:
        return SegmentationResult(classifications=[], calibration={"input_empty": True})

    if not llm_enabled or llm_client is None:
        # 离线夹具模式(见模块 docstring):产品路由不会带着 llm_enabled=False 到这里。
        if progress is not None:
            progress.classify_plan(0, "heuristic")
        return classify_heuristic(paragraphs)
    if session is None or not str(scope_id or "").strip():
        raise SegmentationLLMError(
            "STYLE_REF_ACCOUNTING_CONTEXT_REQUIRED",
            "LLM segmentation requires a durable session and style-reference scope",
        )

    try:
        return classify_with_llm(
            paragraphs,
            llm_client,
            session=session,
            scope_id=str(scope_id),
            progress=progress,
        )
    except SegmentationLLMError as exc:
        if is_llm_control_plane_failure(exc):
            raise
        # 2026-09-15 严格 LLM:不降级。作者看到的是 502 + 原因 + 「检查模型接入后重试」。
        raise ClassificationFailedError(
            code=exc.code, message=exc.message, book_id=str(scope_id)
        ) from exc
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, LLMAccountingError) or is_llm_control_plane_failure(exc):
            raise
        raise ClassificationFailedError(
            code="STYLE_REF_LLM_UNEXPECTED_FAILURE", message=str(exc), book_id=str(scope_id)
        ) from exc


__all__ = [
    "ParagraphClassification",
    "SegmentationResult",
    "classify_paragraphs",
    "ClassificationFailedError",
    "SegmentationLLMError",
]
