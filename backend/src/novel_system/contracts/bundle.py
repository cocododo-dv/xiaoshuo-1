from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OrderedInjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str
    ref_id: str
    digest_key: str


class BundleSnapshotHashProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    stage_allowlist_name: str
    source_version_refs: dict[str, Any]
    resolved_ref_ids: dict[str, list[str]]
    ordered_injections: list[OrderedInjection]
    inline_digests: dict[str, str]

