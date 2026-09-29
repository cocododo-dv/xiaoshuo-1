"""作者稿接口的请求体（保存、晋升权威正文、AI 续写一组）。"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.request_types import StrictRequestModel

INT64_MAX = (1 << 63) - 1
MAX_DRAFT_CONTENT_CHARS = 2_000_000
MAX_INSTRUCTION_CHARS = 8_000
MAX_NOTE_CHARS = 4_000
MAX_WARNING_CODES = 64

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
OptionalIdentifier = Annotated[str, Field(max_length=255)]
NoteText = Annotated[str, Field(max_length=MAX_NOTE_CHARS)]
WarningCode = Annotated[str, Field(min_length=1, max_length=128)]


class AuthorDraftSaveRequest(StrictRequestModel):
    content: str = Field(max_length=MAX_DRAFT_CONTENT_CHARS)
    base_revision_no: int = Field(ge=1, le=INT64_MAX)
    patch_id: OptionalIdentifier | None = None
    revision_id: OptionalIdentifier | None = None
    option_id: OptionalIdentifier | None = None
    note: NoteText | None = None


class CanonicalPromotionRequest(StrictRequestModel):
    # Keep these optional at the transport boundary so the domain service can
    # apply the fail-closed ``requires_reconcile`` default.
    base_revision_no: int | None = Field(default=None, ge=1, le=INT64_MAX)
    expected_current_final_scene_row_id: Identifier | None = None
    narrative_effect: str | None = Field(default=None, max_length=64)
    accepted_warning_codes: list[WarningCode] = Field(
        default_factory=list,
        max_length=MAX_WARNING_CODES,
    )


class ProposalGenerateSetRequest(StrictRequestModel):
    # 只剩「AI 续写」一种（continuation_variants，缺省也是它）：局部段落范围与评估来源随采纳 / 对比接口一起删了（批准 #7）
    mode: str | None = Field(default=None, max_length=64)
    instruction: str | None = Field(default=None, max_length=MAX_INSTRUCTION_CHARS)
