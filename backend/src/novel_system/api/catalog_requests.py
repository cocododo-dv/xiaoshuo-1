from __future__ import annotations

from pydantic import Field

from novel_system.api.request_types import BoundedJsonObject, StrictRequestModel


class _CatalogChapterNarrativeRequest(StrictRequestModel):
    # 章级的张力 / 视角 / 时间 / 地点 / 入口 / 出口 / 衔接 / 线索已退役（批准 #17a）：带着它们的请求照常 422
    title: str | None = Field(default=None, max_length=500)
    act: str | None = Field(default=None, max_length=128)
    promise: str | None = Field(default=None, max_length=20_000)
    drama: BoundedJsonObject | None = None
    notes: str | None = Field(default=None, max_length=100_000)


class CatalogChapterCreateRequest(_CatalogChapterNarrativeRequest):
    state: str | None = Field(default=None, max_length=64)
    words_target: int | None = Field(default=None, ge=0, le=2_147_483_647)
    current: bool | None = None
    with_scene: bool | None = None
    scene_title: str | None = Field(default=None, max_length=500)


class CatalogChapterUpdateRequest(_CatalogChapterNarrativeRequest):
    state: str | None = Field(default=None, max_length=64)
    words_target: int | None = Field(default=None, ge=0, le=2_147_483_647)
    current: bool | None = None


class CatalogSceneBriefRequest(StrictRequestModel):
    goal: str | None = Field(default=None, max_length=20_000)
    conflict: str | None = Field(default=None, max_length=20_000)
    setback: str | None = Field(default=None, max_length=20_000)
    reaction: str | None = Field(default=None, max_length=20_000)
    dilemma: str | None = Field(default=None, max_length=20_000)
    decision: str | None = Field(default=None, max_length=20_000)


class _CatalogSceneFieldsRequest(StrictRequestModel):
    title: str | None = Field(default=None, max_length=500)
    kind: str | None = Field(default=None, max_length=64)
    state: str | None = Field(default=None, max_length=64)
    goal: str | None = Field(default=None, max_length=20_000)
    conflict: str | None = Field(default=None, max_length=20_000)
    setback: str | None = Field(default=None, max_length=20_000)
    reaction: str | None = Field(default=None, max_length=20_000)
    dilemma: str | None = Field(default=None, max_length=20_000)
    decision: str | None = Field(default=None, max_length=20_000)
    brief: CatalogSceneBriefRequest | None = None
    exit_change: str | None = Field(default=None, max_length=20_000)
    hook: str | None = Field(default=None, max_length=20_000)


class CatalogSceneCreateRequest(_CatalogSceneFieldsRequest):
    at: int | None = Field(default=None, ge=0, le=2_147_483_647)
    pov_character_id: str | None = Field(default=None, max_length=255)
    pov_character_name: str | None = Field(default=None, max_length=500)


class CatalogSceneUpdateRequest(_CatalogSceneFieldsRequest):
    pov_character_id: str | None = Field(default=None, max_length=255)
    pov_character_name: str | None = Field(default=None, max_length=500)

