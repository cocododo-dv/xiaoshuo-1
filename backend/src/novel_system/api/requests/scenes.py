"""场景接口的请求体：v1 场景卡建 / 改、作者笔记、运行（同步 / 任务 / 取消）、候选终选、预算追加、采纳归档、删场。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from novel_system.api.requests.common import (
    INT64_MAX,
    BoundedIdentifier,
    StrictRequestModel,
    WriterBriefJsonInput,
)


class SceneUpsertRequest(StrictRequestModel):
    """Whitelist author-editable scene-card fields.

    Run state, trash state, word rollups, and timestamps remain server-owned.
    """

    scene_id: str = Field(min_length=1, max_length=255)
    chapter_id: str = Field(min_length=1, max_length=255)
    scene_goal: str = Field(max_length=100_000)
    project_id: str | None = Field(default=None, max_length=255)
    outline_plan_id: str | None = Field(default=None, max_length=255)
    scene_seq: int | None = Field(default=None, ge=1, le=INT64_MAX)
    pov_character_id: str | None = Field(default=None, max_length=255)
    onstage_chars_json: list[Annotated[str, Field(min_length=1, max_length=255)]] = (
        Field(default_factory=list, max_length=256)
    )
    resolved_relation_id: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=10_000)
    beats_json: list[Annotated[str, Field(max_length=20_000)]] = Field(
        default_factory=list, max_length=256
    )
    must_include_text: str | None = Field(default=None, max_length=100_000)
    forbidden_text: str | None = Field(default=None, max_length=100_000)
    exit_change: str | None = Field(default=None, max_length=20_000)
    hook: str | None = Field(default=None, max_length=20_000)
    # Keep the established domain-error contract for malformed writer briefs:
    # normalize_scene_writer_brief() validates the JSON shape and returns the
    # stable WRITER_BRIEF_INVALID / HTTP 400 response used by API clients.
    writer_brief_json: WriterBriefJsonInput = None
    target_length_band: str | None = Field(default=None, max_length=64)
    scene_type: str | None = Field(default=None, max_length=64)
    is_chapter_last: int = Field(default=0, ge=0, le=1)
    state: str = Field(default="todo", min_length=1, max_length=64)
    constraint_intensity: float | None = Field(default=None, ge=0.0, le=1.0)


class ExactAuthorDraftAdoptionRequest(StrictRequestModel):
    """One exact browser manuscript revision to save and publish atomically."""

    draft_id: str = Field(min_length=1, max_length=255)
    base_revision_no: int = Field(ge=1, le=INT64_MAX)
    # Required but nullable: null is the CAS value when no canonical scene exists.
    expected_current_final_scene_row_id: str | None = Field(max_length=255)
    content: str = Field(max_length=2_000_000)


class AdoptCurrentRequest(StrictRequestModel):
    """Only exact server-issued content-safety finding codes may be acknowledged."""

    accepted_warning_codes: list[
        Annotated[str, Field(min_length=1, max_length=128)]
    ] = Field(default_factory=list, max_length=64)
    exact_author_draft: ExactAuthorDraftAdoptionRequest | None = None


class SceneIdsRequest(StrictRequestModel):
    scene_ids: list[BoundedIdentifier] = Field(max_length=10_000)


class SceneRunCommandRequest(StrictRequestModel):
    # These values retain their domain validators and stable error codes.
    author_note: Any | None = None
    run_policy: Any | None = None
    from_step: Any | None = None
    resume: Any | None = None


class SceneRunJobRequest(SceneRunCommandRequest):
    resume_budget: bool | None = None


class SceneRunCancelRequest(StrictRequestModel):
    reason: Any | None = None


class StyleCandidateSelectRequest(StrictRequestModel):
    no_clear_difference: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=INT64_MAX)
    preference_tags: list[
        Literal[
            "style_match",
            "rhythm",
            "voice",
            "imagery",
            "dialogue",
            "overall_quality",
            "plot_fidelity",
        ]
    ] = Field(default_factory=list, max_length=7)


class SceneBudgetTopupRequest(StrictRequestModel):
    # The endpoint deliberately reports all invalid dimensions together through
    # INVALID_BUDGET_TOPUP, so retain raw scalar types for that domain check.
    extra_tokens: Any = 0
    extra_attempts: Any = 0
    extra_provider_attempts: Any = 0
    reason: str | None = Field(default=None, max_length=300)


class SceneAuthorNotesSaveRequest(StrictRequestModel):
    notes: str = Field(max_length=100_000)
    base_revision_no: int = Field(ge=0, le=INT64_MAX)
