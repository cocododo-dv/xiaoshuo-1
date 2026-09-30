"""正史核对接口的请求体：手记事实候选、候选裁决、一场的正史核对完成。"""

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
