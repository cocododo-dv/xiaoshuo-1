"""文学质量视图的请求体：单段分析、章组复审。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.requests.common import BoundedIdentifier, StrictRequestModel

ProtectedTerm = Annotated[str, Field(min_length=1, max_length=512)]


class LiteraryQualityAnalyzeTextRequest(StrictRequestModel):
    # Omission remains a domain error (LITERARY_QUALITY_TEXT_REQUIRED).
    content: str | None = Field(default=None, max_length=2_000_000)
    object_type: str | None = Field(default=None, max_length=64)
    object_id: str | None = Field(default=None, max_length=255)
    chapter_id: str | None = Field(default=None, max_length=255)
    scene_id: str | None = Field(default=None, max_length=255)
    source_ref: str | None = Field(default=None, max_length=1000)


class LiteraryQualityChapterSetRequest(StrictRequestModel):
    # Empty/missing lists retain LITERARY_QUALITY_CHAPTER_SET_REQUIRED.
    chapter_ids: list[BoundedIdentifier] = Field(default_factory=list, max_length=500)
    protected_terms: list[ProtectedTerm] = Field(default_factory=list, max_length=500)
    # Keep vocabulary validation in the service for LITERARY_QUALITY_LAYER_INVALID.
    text_layer: str | None = Field(default=None, max_length=64)
