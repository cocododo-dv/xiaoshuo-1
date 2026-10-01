"""待办卡片的请求体：建卡、处理、稍后提醒 / 取消提醒。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.requests.common import BoundedJsonObject, StrictRequestModel

OptionalIdentifier = Annotated[str, Field(max_length=255)]
CardListItem = Annotated[str, Field(max_length=4000)]


class ReviewCardCreateRequest(StrictRequestModel):
    project_id: OptionalIdentifier | None = None
    scene_id: OptionalIdentifier | None = None
    chapter_id: OptionalIdentifier | None = None
    # Values remain domain-validated for REVIEW_CARD_KIND_INVALID.
    kind: str = Field(max_length=64)
    priority: int | None = Field(default=None, ge=1, le=10)
    title: str | None = Field(default=None, max_length=10_000)
    source: str | None = Field(default=None, max_length=255)
    where: str | None = Field(default=None, max_length=1000)
    occurred_at: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=100_000)
    preview: str | None = Field(default=None, max_length=100_000)
    checklist: list[CardListItem] | None = Field(default=None, max_length=500)
    options: list[CardListItem] | None = Field(default=None, max_length=500)
    actions: list[BoundedJsonObject] | None = Field(default=None, max_length=100)
    dedupe_key: str | None = Field(default=None, max_length=512)


class ReviewCardResolveRequest(StrictRequestModel):
    action_index: int | None = Field(default=None, ge=0, le=10_000)
    project_id: OptionalIdentifier | None = None


class ReviewCardProjectRequest(StrictRequestModel):
    project_id: OptionalIdentifier | None = None
