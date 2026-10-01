"""写作台深改的请求体：局部补丁（建 / 采纳 / 放弃）、深评偏好、AI 看这一处、AI 通读本章。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.requests.common import StrictRequestModel

OptionalIdentifier = Annotated[str, Field(max_length=255)]
PreferenceTag = Annotated[str, Field(min_length=1, max_length=128)]


class PassageTargetRangeRequest(StrictRequestModel):
    start: int | None = Field(default=None, ge=0, le=2_000_000)
    end: int | None = Field(default=None, ge=0, le=2_000_000)
    unit: str | None = Field(default=None, max_length=32)


class PassagePatchCreateRequest(StrictRequestModel):
    # Required domain fields remain optional here so PASSAGE_PATCH_INVALID is
    # retained for omissions; present values are still type/size checked.
    object_type: str | None = Field(default=None, max_length=64)
    object_id: OptionalIdentifier | None = None
    chapter_id: OptionalIdentifier | None = None
    scene_id: OptionalIdentifier | None = None
    source_text_ref: str | None = Field(default=None, max_length=1000)
    target_text_ref: str | None = Field(default=None, max_length=1000)
    source_draft_id: OptionalIdentifier | None = None
    quality_signal_id: OptionalIdentifier | None = None
    source_excerpt: str | None = Field(default=None, max_length=100_000)
    issue_dimension: str | None = Field(default=None, max_length=128)
    candidate_category: str | None = Field(default=None, max_length=64)
    target_range: PassageTargetRangeRequest | None = None
    revision_strategy: str | None = Field(default=None, max_length=4000)
    preference_tags: list[PreferenceTag] = Field(default_factory=list, max_length=64)
    # 2026-09-22 场景诊断统一：作者 / 诊断给的改法（工具条指令、发现的建议）与发现的问题句。
    # issue_dimension 从此只放维度键（发现的 dimension，或自由改写的 author_instruction）。
    instruction: str | None = Field(default=None, max_length=4000)
    issue_note: str | None = Field(default=None, max_length=2000)


class PassagePatchAcceptRequest(StrictRequestModel):
    selected_option_id: OptionalIdentifier | None = None
    note: str | None = Field(default=None, max_length=4000)


class PassagePatchRejectRequest(StrictRequestModel):
    note: str | None = Field(default=None, max_length=4000)


class DeepReviewDecisionRequest(StrictRequestModel):
    at: int = Field(ge=0, le=(1 << 63) - 1)
    text: str = Field(min_length=1, max_length=1000)


class PassageReviewRequest(StrictRequestModel):
    """「AI 看这一处」：复核一条发现（signal_id），或独立地看一段（paragraph_index / 选中的原话）/ 一段范围
    （paragraph_start–paragraph_end，含两端）；模型同时看到整场正文，跨段的矛盾也能指出。"""

    signal_id: str | None = Field(default=None, max_length=255)
    paragraph_index: int | None = Field(default=None, ge=0, le=100_000)
    paragraph_start: int | None = Field(default=None, ge=0, le=100_000)
    paragraph_end: int | None = Field(default=None, ge=0, le=100_000)
    excerpt: str | None = Field(default=None, max_length=2000)
    question: str | None = Field(default=None, max_length=2000)


class ChapterReviewRequest(StrictRequestModel):
    """「AI 通读本章」：``all`` 整章一次；``changed`` 只把上次通读之后改过字的场全文送审（未改的场沿用上次的发现）。"""

    scope: str | None = Field(default=None, pattern="^(all|changed)$")


class SceneDeepReviewPreferencesSaveRequest(StrictRequestModel):
    decision_log: list[DeepReviewDecisionRequest] = Field(default_factory=list, max_length=30)
    ignored_issue_keys: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=200,
    )
    base_revision_no: int = Field(ge=0, le=(1 << 63) - 1)
