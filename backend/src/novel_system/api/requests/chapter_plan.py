from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from novel_system.api.requests.common import BoundedJsonObject, StrictRequestModel


class ChapterPlanCandidatesRequest(StrictRequestModel):
    direction_hint: str | None = Field(default=None, max_length=300)


class ChapterPlanFillRequest(StrictRequestModel):
    # Keep domain vocabulary validation in the service for CHAPTER_PLAN_MODE_INVALID.
    mode: str | None = Field(default=None, max_length=32)
    candidate: BoundedJsonObject | None = None


class ChapterPlanApplyRequest(StrictRequestModel):
    # The patch document is versioned and sanitized against the live catalog;
    # only its command envelope is closed here.
    patch: BoundedJsonObject | None = None


# ---- 分章面板（…/snowflake-workspace/chapter-plan/*，B07-18）----
# 信封是封闭的（不认识的顶层键 422）；策略等词表仍由服务校验，好保住它们的领域错误码
# （SNOWFLAKE_CHAPTER_STRATEGY_INVALID 等）。章 / 归属条目的形状由服务逐项清洗，这里允许多余键——
# 旧调用方还会带 scene_seq（服务早已不读）等字段，不能因此 422。


class ChapterPlanPreviewRequest(StrictRequestModel):
    strategy: str | None = Field(default=None, max_length=64)
    scenes_per_chapter: int | None = None
    target_chapter_count: int | None = None


class ChapterPlanProposeRequest(StrictRequestModel):
    target_chapter_count: int | None = None
    scenes_per_chapter: int | None = None
    replace: bool | None = None


class ChapterPlanSuggestRequest(StrictRequestModel):
    base_strategy: str | None = Field(default=None, max_length=64)


class _ChapterPlanItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    row_uid: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=1_000)
    act: int | str | None = None
    spine: str | None = Field(default=None, max_length=32)


class ChapterTitlesChapter(_ChapterPlanItem):
    scene_plan_ids: list[str] | None = Field(default=None, max_length=10_000)


class ChapterTitlesRequest(StrictRequestModel):
    chapters: list[ChapterTitlesChapter] | None = Field(default=None, max_length=2_000)
    rename_all: bool | None = None


class ChapterPlanChapter(_ChapterPlanItem):
    chapter_goal: str | None = Field(default=None, max_length=10_000)
    summary: str | None = Field(default=None, max_length=10_000)


class ChapterPlanAssignment(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene_plan_id: str | None = Field(default=None, max_length=255)
    chapter_row_uid: str | None = Field(default=None, max_length=255)


class ChapterPlanSaveRequest(StrictRequestModel):
    replace_chapters: bool | None = None
    chapters: list[ChapterPlanChapter] | None = Field(default=None, max_length=2_000)
    assignments: list[ChapterPlanAssignment] | None = Field(default=None, max_length=20_000)
