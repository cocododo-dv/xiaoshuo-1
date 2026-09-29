from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from novel_system.api.request_types import StrictRequestModel


class _ProjectFieldsRequest(StrictRequestModel):
    title: str | None = Field(default=None, max_length=500)
    genre: str | None = Field(default=None, max_length=255)
    mark: str | None = Field(default=None, max_length=32)
    accent: str | None = Field(default=None, max_length=128)
    synopsis_line: str | None = Field(default=None, max_length=4000)
    # HTML numeric inputs arrive as strings in the current client.  Accept that
    # representation deliberately, but reject malformed, non-positive, or
    # unreasonably large values before they reach the lenient legacy parser.
    target_word_count: int | str | None = None
    target_chapter_count: int | str | None = None
    words_target_daily: int | str | None = None

    @field_validator(
        "target_word_count",
        "target_chapter_count",
        "words_target_daily",
    )
    @classmethod
    def validate_positive_integer_input(cls, value: Any) -> Any:
        if value is None or value == "":
            return value
        if isinstance(value, bool):
            raise ValueError("value must be a positive integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be a positive integer") from exc
        if str(value).strip() != str(number) or not 1 <= number <= 2_147_483_647:
            raise ValueError("value must be a positive integer")
        return value


class ProjectCreateRequest(_ProjectFieldsRequest):
    # Omission remains a domain error so PROJECT_OUTLINE_REQUIRED is stable.
    outline_text: str | None = Field(default=None, max_length=2_000_000)
    planning_mode: str | None = Field(default=None, max_length=64)
    snowflake_workflow_mode: str | None = Field(default=None, max_length=64)


class ProjectProfileUpdateRequest(_ProjectFieldsRequest):
    pass


# ---- 本章流程：运行本章、已通读、确认定稿、重开定稿 ----


class ProjectChapterRunJobRequest(StrictRequestModel):
    """运行本章没有可调的选项（离线演示已退役，``offline_demo`` 一并删去）；空对象即可。"""


class ProjectChapterReadConfirmRequest(StrictRequestModel):
    note: str | None = Field(default=None, max_length=1000)


class ProjectChapterReadConfirmationRequest(StrictRequestModel):
    body_hash: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class ProjectChapterApproveFinalRequest(StrictRequestModel):
    revision_notes: str | None = Field(default=None, max_length=2000)
    # 批准 #10：「已通读」随「确认定稿」一次提交，绑定作者读到的那一份正文（GET chapter-manuscripts 的 body_hash）
    read_confirmation: ProjectChapterReadConfirmationRequest | None = None


class ProjectChapterReopenFinalRequest(StrictRequestModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("reason must not be blank")
        return reason
