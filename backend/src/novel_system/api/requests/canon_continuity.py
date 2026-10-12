"""正史核对接口的请求体：手记事实候选、候选裁决、一场的正史核对完成；故事记忆里作者改过的章摘要、线索标记与编辑意见的决定。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from novel_system.api.requests.common import StrictRequestModel


class ManualFactCandidateRequest(StrictRequestModel):
    event_type: str = Field(min_length=1, max_length=64)
    raw_entity_ref: str = Field(min_length=1, max_length=200)
    fact_key: str = Field(min_length=1, max_length=120)
    fact_value: str = Field(min_length=1, max_length=2000)
    evidence_text: str = Field(min_length=1, max_length=2000)
    entity_type: str | None = Field(default=None, max_length=64)
    planned_timeline_event_id: str | None = Field(default=None, max_length=255)

    @field_validator(
        "event_type",
        "raw_entity_ref",
        "fact_key",
        "fact_value",
        "evidence_text",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("value must not be blank")
        return clean


class FactCandidateDecisionRequest(StrictRequestModel):
    action: Literal["accept", "reject"]
    selected_entity_id: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=1000)
    expected_final_scene_row_id: str | None = Field(default=None, max_length=255)


class SceneCanonVerificationRequest(StrictRequestModel):
    note: str = Field(min_length=1, max_length=1000)
    expected_final_scene_row_id: str | None = Field(default=None, max_length=255)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("note must not be blank")
        return clean


class ChapterDigestChange(StrictRequestModel):
    character: str = Field(min_length=1, max_length=40)
    change: str = Field(min_length=1, max_length=160)


class ChapterDigestBody(StrictRequestModel):
    """作者改的是文字；这一章对线索台账的更新不在这里改（原样带过来，线索用标记管）。"""

    summary: str = Field(min_length=1, max_length=600)
    ending_state: str = Field(default="", max_length=240)
    character_changes: list[ChapterDigestChange] = Field(default_factory=list, max_length=30)

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("value must not be blank")
        return clean


class ChapterDigestSaveRequest(StrictRequestModel):
    """作者改过的章摘要：``base_row_id`` 是打开编辑时的那一份（还没有摘要时为空）。"""

    base_row_id: str | None = Field(default=None, max_length=255)
    digest: ChapterDigestBody



class StoryThreadMarkRequest(StrictRequestModel):
    """作者标记台账里的一条线：放下 / 已收 / 取消标记（``open``）。"""

    status: Literal["dropped", "resolved", "open"]
    note: str = Field(default="", max_length=200)


class EditorialGuidanceDecisionRequest(StrictRequestModel):
    """作者对一条编辑意见：采纳（可以带改过的字）/ 不采纳（已采纳的 = 撤销）。"""

    decision: Literal["accept", "dismiss"]
    text: str | None = Field(default=None, max_length=400)
