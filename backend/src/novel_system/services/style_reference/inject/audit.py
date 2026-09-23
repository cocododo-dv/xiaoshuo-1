"""风格参考 v3 — 渲染审计（不含任何正文：只有规模、哈希、窗号与策略）。

审计随提示字典的 ``_style_reference_runtime_audit`` 进 LLM 审计摘要与 AttemptTracker；字段名沿用旧审计里消费方
在读的那些（``outcome`` / ``contract_hash`` / ``profile_ids`` / ``render_stats`` / ``few_shot_window_refs`` /
``prefix_chars`` / ``prefix_sha256`` / ``placement`` …），再加 v3 的 ``role`` / ``reference_mode`` / ``selection`` /
``blocks`` / ``notices`` / ``legacy_profile``。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

AUDIT_VERSION = "style_render_audit_v3"


def block_digest(text: str) -> dict[str, Any]:
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
    }


def _layer(contract: Any) -> Mapping[str, Any]:
    from novel_system.services.style_reference.runtime_contract import contract_layer

    return contract_layer(contract if isinstance(contract, Mapping) else None)


def build_audit(
    *,
    policy: Any,
    request: Any,
    selection: Any,
    system_prefix: str,
    user_tail: str,
    blocks: Mapping[str, Mapping[str, Any]],
    window_refs: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    notices: Sequence[str],
    legacy_profile: bool,
    legacy_digit_lines_dropped: int = 0,
    card_examples: int = 0,
    samples_blocked: str | None = None,
    reference_mode: str | None = None,
    route: Mapping[str, Any] | None = None,
    no_samples_note: bool = False,
) -> dict[str, Any]:
    rendered = system_prefix + user_tail
    layer = _layer(getattr(policy, "contract", None))
    binding = layer.get("binding") if isinstance(layer.get("binding"), Mapping) else {}
    profile_id = getattr(policy, "profile_id", None)
    binding_id = getattr(policy, "binding_id", None)
    return {
        "version": AUDIT_VERSION,
        "outcome": "hit" if rendered else "miss",
        "role": getattr(request, "role", None),
        "placement": getattr(request, "placement", None),
        # 这一次实际的参考方式（绑定的参考方式按书冻结时 / 现在的云策略压过之后）
        "reference_mode": reference_mode if reference_mode is not None else getattr(policy, "reference_mode", None),
        # 云策略按接收提示的节点判（节点、路由是否本机、送不送书 / 原文、原因；不含正文）
        "route": dict(route or {}),
        # 一窗样例都没带时，样例位置写了「本次没有附原文样例」（M5）
        "no_samples_note": bool(no_samples_note),
        # 旧字段：绑定行上的旧策略值（v3 语义看 reference_mode）
        "strategy": str(binding.get("strategy") or "mixed"),
        "contract_hash": getattr(policy, "contract_hash", None),
        "profile_ids": [profile_id] if profile_id else [],
        "binding_ids": [binding_id] if binding_id else [],
        "layer_count": 1 if profile_id else 0,
        "policy": policy.audit() if hasattr(policy, "audit") else {},
        "request": request.audit() if hasattr(request, "audit") else {},
        "selection": selection.audit() if hasattr(selection, "audit") else {},
        "legacy_profile": bool(legacy_profile),
        "legacy_digit_lines_dropped": int(legacy_digit_lines_dropped),
        "card_examples": int(card_examples),
        "samples_blocked": samples_blocked,
        "notices": list(dict.fromkeys(str(n) for n in notices if n)),
        "blocks": {name: dict(value) for name, value in blocks.items()},
        "few_shot_window_refs": [dict(item) for item in window_refs],
        "render_stats": dict(stats),
        "prefix_chars": len(rendered),
        "prefix_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }


__all__ = ["AUDIT_VERSION", "block_digest", "build_audit"]
