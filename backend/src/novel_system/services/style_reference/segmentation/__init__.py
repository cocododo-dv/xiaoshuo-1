"""Style Reference 段落分类。

- **产品路径**(2026-09-15 严格 LLM,2026-09-23 v3 起跑在作业表上):整本书由分类作业
  (``import_job``,kind=classify)用 LLM 逐批分类,原子单元在 ``segmentation.llm``;没有启发式兜底。
- **离线夹具模式**:``classify_paragraphs`` 只跑启发式(``segmentation.heuristic``),供测试与本地
  语料工具建书用。产品路由在 LLM 未启用时已经 409 ``STYLE_REFERENCE_LLM_REQUIRED``,永远不会走到这里;
  带着 ``llm_enabled=True`` 调用它是编程错误(LLM 分类必须走作业)。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.style_reference.segmentation.heuristic import classify_heuristic
from novel_system.services.style_reference.segmentation.llm import SegmentationLLMError
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)


def classify_paragraphs(
    paragraphs: list[tuple[int, int, str]],
    *,
    llm_enabled: bool = False,
    llm_client: Any | None = None,  # noqa: ARG001 — 离线夹具模式不用模型,保留签名兼容
) -> SegmentationResult:
    """离线夹具模式的段落分类(启发式)。``paragraphs`` 元素是 ``(start_offset, end_offset, body)``。"""
    if llm_enabled:
        raise ValueError(
            "LLM paragraph classification runs as a classify job (style_reference.import_job); "
            "classify_paragraphs is the offline fixture mode only"
        )
    if not paragraphs:
        return SegmentationResult(classifications=[], calibration={"input_empty": True})
    return classify_heuristic(paragraphs)


__all__ = [
    "ParagraphClassification",
    "SegmentationLLMError",
    "SegmentationResult",
    "classify_paragraphs",
]
