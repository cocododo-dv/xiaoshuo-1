"""风格参考 · 运行时契约 v1（``style_reference_runtime_contract_v1``）的读取器——**冻结的历史，不要扩展**。

v3 之前冻结进旧 bundle 的多层契约。已有安装的旧 bundle 里还带着它（审计 B10-24：一份真实安装的 3 个 scene bundle
都是 v1 契约），``style_policy_for_scene`` 退回到 ``SceneRunState.current_bundle_id`` 时门禁照旧读它们，所以读取器
留着；新契约只写 v2（``runtime_contract``，按 ``contract_version`` 分派到这里的 :func:`validate_v1`）。

这里的校验逐字保留原样：旧 bundle 的层哈希、契约哈希按当时的规则算，改一个字就会让它们失效。不写新契约、不加
新键；要退役这条路径，先确认没有 bundle 还带 v1 契约（数一数内联契约的 ``contract_version``）。

v2 的读侧也用这里的几样旧口径：绑定快照的旧策略值 A / B / C（:data:`LEGACY_STRATEGIES`，迁移 0092 之前冻结的 v2
契约还带着）、v1 画像键（:data:`FROZEN_PROFILE_JSON_KEYS_V1`，旧 v2 契约也可能带）、契约哈希
（:func:`contract_json_hash`）与 sha256 形状（:data:`SHA256_RE`）——两个版本必须按同一把尺子算。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from novel_system.services.hash_engine import sha256_json_normalized

STYLE_RUNTIME_CONTRACT_VERSION_V1 = "style_reference_runtime_contract_v1"
# v1 契约的画像白名单（只用于校验旧 bundle 里冻结的 v1 契约；v2 的读侧白名单也放行它们）
FROZEN_PROFILE_JSON_KEYS_V1 = frozenset(
    {
        "reference_basis",
        "narrative_summary",
        "qualitative_summary",
        "metrics_baseline",
        "scene_samples_index",
        "sub_dimensions",
        "style_features",
        "narrative_patterns",
        "banned_replication_rules",
        "calibration_guidance",
        "generation_safe_forbidden_findings",
        "source_overlap_filter",
        "voice_signature",
        "narrative_guidance",
        "structure_card",
        "planning_guidance",
    }
)
# 绑定快照的策略值：v1 契约与迁移 0092 之前冻结的 v2 契约还带 A / B / C（新契约恒写 mixed）
LEGACY_STRATEGIES = frozenset({"A", "B", "C", "mixed"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# v1 契约可能写的起草方式（写了就必须是其中之一）
V1_DRAFT_MODES = frozenset({"style_first", "neutral_first"})


def contract_json_hash(payload: Mapping[str, Any]) -> str:
    """契约与每一层的哈希（v1 / v2 同一个规则）。"""
    return sha256_json_normalized(dict(payload))


def validate_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    """v1 契约（v3 之前冻结进旧 bundle 的多层契约）的原校验，逐字保留。"""
    contract = copy.deepcopy(dict(payload))
    supplied_hash = str(contract.pop("contract_hash", "") or "")
    if (
        type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != 1
        or contract.get("contract_version") != STYLE_RUNTIME_CONTRACT_VERSION_V1
    ):
        raise ValueError("unsupported style runtime contract")
    layers = contract.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ValueError("style runtime contract has no layers")
    if (
        type(contract.get("layer_count")) is not int
        or contract.get("layer_count") != len(layers)
    ):
        raise ValueError("style runtime contract layer count mismatch")
    task_type = str(contract.get("task_type") or "")
    if not task_type:
        raise ValueError("style runtime contract task type is missing")
    expected_profile_ids: list[str] = []
    expected_binding_ids: list[str] = []
    for expected_order, layer in enumerate(layers):
        if (
            not isinstance(layer, Mapping)
            or type(layer.get("order")) is not int
            or layer.get("order") != expected_order
        ):
            raise ValueError("style runtime contract layer order is invalid")
        layer_copy = copy.deepcopy(dict(layer))
        layer_hash = str(layer_copy.pop("layer_hash", "") or "")
        if not layer_hash or contract_json_hash(layer_copy) != layer_hash:
            raise ValueError("style runtime contract layer hash mismatch")
        binding = layer.get("binding")
        profile = layer.get("profile")
        if not isinstance(binding, Mapping) or not isinstance(profile, Mapping):
            raise ValueError("style runtime contract layer snapshot is invalid")
        if not isinstance(binding.get("config_json"), Mapping) or not isinstance(
            profile.get("profile_json"), Mapping
        ):
            raise ValueError("style runtime contract payload shape is invalid")
        if not set(profile["profile_json"]).issubset(FROZEN_PROFILE_JSON_KEYS_V1):
            raise ValueError("style runtime contract profile payload is not allow-listed")
        if not isinstance(profile.get("source_finding_ids_json"), list):
            raise ValueError("style runtime contract finding ids are invalid")
        if not isinstance(layer.get("forbidden_findings"), list) or not isinstance(
            layer.get("banned_terms"), list
        ):
            raise ValueError("style runtime contract safety inputs are invalid")
        if (
            not isinstance(layer.get("sample_quote_refs"), list)
            or not isinstance(layer.get("sample_paragraph_refs", []), list)
            or not isinstance(layer.get("book"), Mapping)
        ):
            raise ValueError("style runtime contract source references are invalid")
        if any(
            not isinstance(finding_id, str) or not finding_id
            for finding_id in profile["source_finding_ids_json"]
        ):
            raise ValueError("style runtime contract finding ids are malformed")
        if any(
            not isinstance(item, Mapping)
            or not str(item.get("finding_id") or "")
            or not isinstance(item.get("statement"), str)
            for item in layer["forbidden_findings"]
        ):
            raise ValueError("style runtime contract forbidden findings are malformed")
        if any(
            not isinstance(term, str) or not term for term in layer["banned_terms"]
        ):
            raise ValueError("style runtime contract banned terms are malformed")
        paragraph_ids: list[str] = []
        for paragraph_ref in layer.get("sample_paragraph_refs", []):
            if not isinstance(paragraph_ref, Mapping):
                raise ValueError("style runtime contract paragraph reference is malformed")
            paragraph_id = str(paragraph_ref.get("paragraph_id") or "")
            paragraph_sha256 = str(paragraph_ref.get("paragraph_sha256") or "")
            if (
                not paragraph_id
                or paragraph_id in paragraph_ids
                or SHA256_RE.fullmatch(paragraph_sha256) is None
            ):
                raise ValueError("style runtime contract paragraph reference is malformed")
            paragraph_ids.append(paragraph_id)

        quote_ids: list[str] = []
        for quote_ref in layer["sample_quote_refs"]:
            if not isinstance(quote_ref, Mapping):
                raise ValueError("style runtime contract quote reference is malformed")
            quote_id = str(quote_ref.get("quote_id") or "")
            quote_sha256 = str(quote_ref.get("quote_sha256") or "")
            paragraph_id = str(quote_ref.get("paragraph_id") or "")
            if (
                not quote_id
                or quote_id in quote_ids
                or SHA256_RE.fullmatch(quote_sha256) is None
                or (paragraph_id and paragraph_id not in paragraph_ids)
            ):
                raise ValueError("style runtime contract quote reference is malformed")
            quote_ids.append(quote_id)
        profile_id = str(profile.get("profile_id") or "")
        binding_id = str(binding.get("binding_id") or "")
        book_id = str(profile.get("book_id") or "")
        book = layer["book"]
        if (
            not profile_id
            or not binding_id
            or not book_id
            or not str(profile.get("run_id") or "")
            or str(binding.get("profile_id") or "") != profile_id
            or str(binding.get("task_type") or "") != task_type
            or str(binding.get("status") or "") != "active"
            or str(binding.get("strategy") or "") not in LEGACY_STRATEGIES
            or not str(binding.get("scope") or "")
            or str(profile.get("status") or "") != "active"
            or str(book.get("book_id") or "") != book_id
            or type(book.get("cloud_llm_allowed_at_freeze")) is not bool
        ):
            raise ValueError("style runtime contract layer lineage is invalid")
        paragraph_root = book.get("paragraph_root_sha256")
        if paragraph_root is not None and (
            not isinstance(paragraph_root, str)
            or SHA256_RE.fullmatch(paragraph_root) is None
            or type(book.get("paragraph_count")) is not int
            or int(book.get("paragraph_count")) < 0
        ):
            raise ValueError("style runtime contract book paragraph root is malformed")
        if profile_id not in expected_profile_ids:
            expected_profile_ids.append(profile_id)
        expected_binding_ids.append(binding_id)
    if "draft_mode" in contract and (
        not isinstance(contract.get("draft_mode"), str)
        or contract.get("draft_mode") not in V1_DRAFT_MODES
    ):
        raise ValueError("style runtime contract draft mode is invalid")
    if contract.get("profile_ids") != expected_profile_ids:
        raise ValueError("style runtime contract profile ids mismatch")
    if contract.get("binding_ids") != expected_binding_ids:
        raise ValueError("style runtime contract binding ids mismatch")
    computed_hash = contract_json_hash(contract)
    if not supplied_hash or supplied_hash != computed_hash:
        raise ValueError("style runtime contract hash mismatch")
    contract["contract_hash"] = supplied_hash
    return contract


__all__ = [
    "FROZEN_PROFILE_JSON_KEYS_V1",
    "LEGACY_STRATEGIES",
    "SHA256_RE",
    "STYLE_RUNTIME_CONTRACT_VERSION_V1",
    "contract_json_hash",
    "validate_v1",
]
