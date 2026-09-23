from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QCIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    issue_key: str = "ok"
    message: str = ""


class HardQCOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_code: str
    pass_flag: bool
    next_action: str
    issues: list[QCIssue]
    rewrite_brief: list[str]


class StyleDimensionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    score: float = Field(ge=0, le=1)
    evidence: str = ""


class StyleDeviation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    severity: str = ""
    patch_brief: str = ""
    evidence: str | None = None


class SoftQCOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_code: str
    pass_flag: bool
    next_action: str
    issues: list[QCIssue]
    rewrite_brief: list[str] = Field(default_factory=list)
    carry_forward_note: bool = False
    note_scope: str | None = None
    carry_note_text: str | None = None
    style_score: float | None = Field(default=None, ge=0, le=1)
    style_dimensions: list[StyleDimensionScore] = Field(default_factory=list)
    style_deviations: list[StyleDeviation] = Field(default_factory=list)
    # 风格参考 v3（V7）：参考评审按 16 维打的「像不像」分（换算到 0–1；落库时再换回 10 分制）
    dimension_scores: dict[str, float] = Field(default_factory=dict)

    @field_validator("dimension_scores")
    @classmethod
    def _unit_dimension_scores(cls, value: dict[str, float]) -> dict[str, float]:
        for key, score in value.items():
            if not 0.0 <= float(score) <= 1.0:
                raise ValueError(f"dimension score out of range: {key}")
        return value
