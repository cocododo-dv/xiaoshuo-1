from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from novel_system.contracts.bundle import BundleSnapshotHashProjection


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").rstrip())
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    return value


def normalize_string(text: str) -> str:
    """:func:`normalize` for a value that must stay a string (``ValueError`` otherwise)."""
    normalized = normalize(text)
    if not isinstance(normalized, str):
        raise ValueError("text must normalize to a string")
    return normalized


def canonical_json(payload: Any) -> str:
    normalized = normalize(payload)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(text: str | None) -> str:
    """SHA-256 hex of ``str(text or "")`` as UTF-8 (``None`` hashes like ``""``)."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def json_plain(payload: Any) -> str:
    """``json.dumps`` with sorted keys, compact separators and raw UTF-8 — no text normalisation."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json_plain(payload: Any) -> str:
    """SHA-256 of :func:`json_plain` — the checkpoint / archive / audit flavour."""
    return sha256_text(json_plain(payload))


def sha256_json_normalized(payload: Any) -> str:
    """SHA-256 of :func:`canonical_json` (NFC, CRLF→LF, trailing whitespace stripped)."""
    return sha256_text(canonical_json(payload))


def compute_bundle_hash_projection(payload: BundleSnapshotHashProjection) -> str:
    return sha256_json_normalized(payload.model_dump(mode="json"))


def verify_bundle_snapshot_hash(
    snapshot: Any,
    *,
    expected_hash: str | None,
) -> dict[str, Any]:
    """Recompute a persisted bundle hash with the published BSHASH_v1 projection.

    ``SceneBundle.frozen_snapshot_json`` contains non-hashed envelope fields such
    as ``scene_id`` and ``chapter_id``.  Reconstructing the same typed projection
    used by :class:`BundleBuilder` avoids both accepting those fields as hash
    inputs and accidentally inventing a second, incompatible hash contract.
    """

    normalized_expected = str(expected_hash or "")
    try:
        if not isinstance(snapshot, dict):
            raise TypeError("bundle snapshot must be an object")
        projection = BundleSnapshotHashProjection(
            contract_version=snapshot.get("contract_version"),
            stage_allowlist_name=snapshot.get("stage_allowlist_name"),
            source_version_refs=snapshot.get("source_version_refs"),
            resolved_ref_ids=snapshot.get("resolved_ref_ids"),
            ordered_injections=snapshot.get("ordered_injections"),
            inline_digests=snapshot.get("inline_digests"),
        )
        computed_hash = compute_bundle_hash_projection(projection)
    except Exception as exc:  # noqa: BLE001 - integrity validation must fail closed
        return {
            "valid": False,
            "error_code": "bundle_projection_invalid",
            "expected_hash": normalized_expected or None,
            "computed_hash": None,
            "error_type": type(exc).__name__,
        }
    valid = bool(normalized_expected) and computed_hash == normalized_expected
    return {
        "valid": valid,
        "error_code": None if valid else "bundle_hash_mismatch",
        "expected_hash": normalized_expected or None,
        "computed_hash": computed_hash,
    }
