from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.requests.common import BoundedJsonObject, StrictRequestModel


LibraryTextItem = Annotated[str, Field(max_length=2000)]
LibraryReference = Annotated[str, Field(min_length=1, max_length=512)]


class _LibraryEntityFieldsRequest(StrictRequestModel):
    name: str | None = Field(default=None, max_length=500)
    kind: str | None = Field(default=None, max_length=64)
    aliases: list[LibraryTextItem] | None = Field(default=None, max_length=1000)
    summary: str | None = Field(default=None, max_length=100_000)
    details: BoundedJsonObject | None = None
    tags: list[LibraryTextItem] | None = Field(default=None, max_length=1000)


class LibraryEntityCreateRequest(_LibraryEntityFieldsRequest):
    pass


class LibraryEntityUpdateRequest(_LibraryEntityFieldsRequest):
    status: str | None = Field(default=None, max_length=64)


class LibraryRelationCreateRequest(StrictRequestModel):
    from_ref: str | None = Field(default=None, max_length=512)
    to_ref: str | None = Field(default=None, max_length=512)
    kind: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=100_000)


class LibraryTimelineEventRequest(StrictRequestModel):
    label: str | None = Field(default=None, max_length=1000)
    time_label: str | None = Field(default=None, max_length=1000)
    chapter_ref: str | None = Field(default=None, max_length=512)
    entity_refs: list[LibraryReference] | None = Field(default=None, max_length=10_000)
    note: str | None = Field(default=None, max_length=100_000)
    display_order: int | None = Field(
        default=None,
        ge=-2_147_483_648,
        le=2_147_483_647,
    )


class LibraryCharacterRequest(StrictRequestModel):
    name: str | None = Field(default=None, max_length=500)
    role: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=100_000)
    details: BoundedJsonObject | None = None


