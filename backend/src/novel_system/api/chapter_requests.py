"""v1 章接口的请求体（建 / 改章、批量删章、章内场景排序）。"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.request_types import BoundedJsonObject, StrictRequestModel, WriterBriefJsonInput

INT64_MAX = (1 << 63) - 1


class ChapterUpsertRequest(StrictRequestModel):
    """Whitelist the chapter fields an author is allowed to edit."""

    chapter_id: str = Field(min_length=1, max_length=255)
    chapter_goal: str = Field(max_length=100_000)
    project_id: str | None = Field(default=None, max_length=255)
    outline_plan_id: str | None = Field(default=None, max_length=255)
    planned_scene_count: int | None = Field(default=None, ge=0, le=INT64_MAX)
    mid_aggregate_enabled: int = Field(default=0, ge=0, le=1)
    narrative_json: BoundedJsonObject | None = None
    state: str = Field(default="planned", min_length=1, max_length=64)
    words_target: int | None = Field(default=None, ge=0, le=INT64_MAX)
    display_order: int | None = Field(default=None, ge=0, le=INT64_MAX)
    main_plot_push: str | None = Field(default=None, max_length=100_000)
    emotional_target: str | None = Field(default=None, max_length=100_000)
    ending_effect: str | None = Field(default=None, max_length=100_000)
    must_not: str | None = Field(default=None, max_length=100_000)
    notes: str | None = Field(default=None, max_length=100_000)
    # Shape validation belongs to normalize_chapter_writer_brief() so chapter
    # and scene endpoints share WRITER_BRIEF_INVALID / HTTP 400 semantics.
    writer_brief_json: WriterBriefJsonInput = None


BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=255)]


class ChapterIdsRequest(StrictRequestModel):
    chapter_ids: list[BoundedIdentifier] = Field(max_length=10_000)


class ChapterSceneOrderRequest(StrictRequestModel):
    scene_ids: list[BoundedIdentifier] = Field(max_length=10_000)
    last_scene_id: BoundedIdentifier
