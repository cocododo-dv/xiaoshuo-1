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


def test_shared_hash_helpers_match_the_inline_formulas_they_replaced() -> None:
    """X02-15: every hash site now goes through hash_engine; the bytes must not move."""

    import hashlib
    import json

    from novel_system.services.hash_engine import (
        canonical_json,
        json_plain,
        sha256_json_normalized,
        sha256_json_plain,
        sha256_text,
    )

    texts = ["", "林昭在雨城读旧信。\r\n", "trailing  ", "é"]
    for text in texts:
        assert sha256_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert sha256_text(text) == hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
    assert sha256_text(None) == hashlib.sha256(b"").hexdigest()

    payloads = [
        {},
        {"b": 1, "a": ["案卷", {"z": "x\r\n", "y": None}]},
        {"text": "é ", "n": 1.5, "flag": True},
    ]
    for payload in payloads:
        plain = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert json_plain(payload) == plain
        assert sha256_json_plain(payload) == hashlib.sha256(plain.encode("utf-8")).hexdigest()
        assert sha256_json_normalized(payload) == hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
    # The two JSON flavours really differ once text needs normalising.
    assert sha256_json_plain(payloads[2]) != sha256_json_normalized(payloads[2])
