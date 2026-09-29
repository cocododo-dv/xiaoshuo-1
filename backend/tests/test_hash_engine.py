from __future__ import annotations

import json
from pathlib import Path

from novel_system.contracts.bundle import BundleSnapshotHashProjection
from novel_system.services.hash_engine import (
    compute_bundle_hash_projection,
    verify_bundle_snapshot_hash,
)


def test_bundle_hash_matches_golden_vector() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "bundle_hash_projection.json"
    payload = BundleSnapshotHashProjection.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )

    assert compute_bundle_hash_projection(payload) == (
        "34a0a289e7ed8a45b185568f4871b4ff888f9a7ea687a4fae2067fe400f5607b"
    )


def test_bundle_snapshot_verifier_uses_the_published_projection() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "bundle_hash_projection.json"
    snapshot = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected_hash = "34a0a289e7ed8a45b185568f4871b4ff888f9a7ea687a4fae2067fe400f5607b"
    snapshot["scene_id"] = "envelope-only-scene"
    snapshot["chapter_id"] = "envelope-only-chapter"

    valid = verify_bundle_snapshot_hash(snapshot, expected_hash=expected_hash)
    snapshot["inline_digests"] = {
        **snapshot["inline_digests"],
        "author_instruction": "tampered",
    }
    invalid = verify_bundle_snapshot_hash(snapshot, expected_hash=expected_hash)

    assert valid == {
        "valid": True,
        "error_code": None,
        "expected_hash": expected_hash,
        "computed_hash": expected_hash,
    }
    assert invalid["valid"] is False
    assert invalid["error_code"] == "bundle_hash_mismatch"
