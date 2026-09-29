from __future__ import annotations


def structured_target(
    target_type: str | None,
    target_id: str | None,
    target_ref: str | None = None,
) -> dict[str, str] | None:
    if not target_type or not target_id:
        return None
    resolved_target_ref = target_ref or f"{target_type}:{target_id}"
    return {
        "target_type": target_type,
        "target_id": target_id,
        "target_ref": resolved_target_ref,
    }
