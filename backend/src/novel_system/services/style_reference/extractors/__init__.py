"""Style Reference v1.1 extractors。

参见《风格参考模块重构执行手册 v1.1》§6.5 / §6.6 / §6.7 与
plans/style-reference-v1-1-fancy-shannon.md。
"""

from __future__ import annotations

from novel_system.services.style_reference.extractors.base import (
    BaseExtractor,
    ExtractionProgress,
    ExtractionRetryPolicy,
    ExtractionRunResult,
)
from novel_system.services.style_reference.extractors.language import LanguageExtractor
from novel_system.services.style_reference.extractors.narrative import NarrativeExtractor
from novel_system.services.style_reference.extractors.scene import SceneExtractor
from novel_system.services.style_reference.extractors.theme import ThemeExtractor

__all__ = [
    "BaseExtractor",
    "ExtractionProgress",
    "ExtractionRetryPolicy",
    "ExtractionRunResult",
    "LanguageExtractor",
    "NarrativeExtractor",
    "SceneExtractor",
    "ThemeExtractor",
]
