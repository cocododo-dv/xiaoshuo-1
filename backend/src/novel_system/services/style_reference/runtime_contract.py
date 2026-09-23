"""Frozen runtime contract shared by style injection, scoring and feedback.

The contract stores abstract profile inputs and hashes of any referenced prose;
raw reference quotes stay in the StyleReference repository.  This lets a scene
bundle freeze exactly which binding/configuration was selected without copying a
book into every bundle.

风格参考 v3（2026-09-23）— 契约 v2（``style_reference_runtime_contract_v2``）：

- **只冻结一层**：最具体的活动绑定（scene > character（POV 在前）> project > global）。旧契约按层序多层合并，
  样例与声音取「最后一层」，而角色层是 POV 优先排的——最后一层恰恰是最不重要的配角（J7）。调用方仍可传入整组
  命中层（``bundle_builder`` / ``scene_blueprint`` / 写作台），这里挑出最具体的一层冻结。
- **瘦身**：画像键按 :data:`FROZEN_PROFILE_JSON_KEYS` 白名单（v3 键 + 旧画像的兼容键；结构画像去掉逐章列表）；
  **不再冻结** ``sample_quote_refs`` / ``sample_paragraph_refs``（约 9.1 万字，只服务「根哈希失配时退回证据引文」
  的兜底路径，那条路径已删——失配时按当前窗口索引渲染并在审计里记 ``STYLE_REFERENCE_BOOK_CHANGED``，J6）。
- **书快照**：``book_id`` / ``text_checksum`` / ``cloud_policy`` / ``cloud_llm_allowed_at_freeze`` /
  ``paragraph_root_sha256`` / ``paragraph_count`` / ``window_index_version``；根哈希读 ``stats_json`` 里存好的值
  （``paragraph_root.ensure_paragraph_root``，缺失才用两列快速路径现算并写回），不再每次加载全部 ORM 段落（J1）。
- **绑定快照**存规范化后的 v3 配置（``binding_config.normalize_binding_config``：参考方式 / 样例窗数 / 维度状态 /
  起草方式）；顶层 ``draft_mode`` 不变。
- v1 契约（旧 bundle 里冻结的）照旧能校验、能用（``style_policy.policy_from_contract`` 读最后一层）。
- 校验按内容指纹记忆（J1：一场里 40 多次深拷贝校验），每次返回新的对象（调用方改了也不污染缓存）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from novel_system.services.hash_engine import canonical_json
from novel_system.services.style_reference.binding_config import normalize_binding_config
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.inject.bindings import most_specific_binding
from novel_system.services.style_reference.paragraph_root import ensure_paragraph_root
from novel_system.services.style_reference.policy import cloud_llm_allowed

logger = logging.getLogger(__name__)


STYLE_RUNTIME_CONTRACT_VERSION_V1 = "style_reference_runtime_contract_v1"
STYLE_RUNTIME_CONTRACT_VERSION_V2 = "style_reference_runtime_contract_v2"
STYLE_RUNTIME_CONTRACT_VERSION = STYLE_RUNTIME_CONTRACT_VERSION_V2
SUPPORTED_CONTRACT_VERSIONS = frozenset({STYLE_RUNTIME_CONTRACT_VERSION_V1, STYLE_RUNTIME_CONTRACT_VERSION_V2})
STYLE_CONTEXT_VERSION = "style_reference_generation_context_v1"
# v1 契约的画像白名单（只用于校验旧 bundle 里冻结的 v1 契约）
_FROZEN_PROFILE_JSON_KEYS_V1 = frozenset(
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
# v2：v3 画像键（文风卡、行状态、声音、结构画像、规划手法、叙事机制、概述、来源、学习标记、版本）
V3_PROFILE_JSON_KEYS = frozenset(
    {
        "dimension_card",
        "card_line_states",
        "voice",
        "voice_signature",
        "structure_card",
        "planning_guidance",
        "narrative_guidance",
        "qualitative_summary",
        "reference_basis",
        "learned_from",
        "profile_version",
        "protected_terms_version",
    }
)
# v2：学习作业跑之前的旧画像还要用到的键（卡替身的正向 / 禁忌行、量化基线的旧读者）
LEGACY_PROFILE_JSON_KEYS = frozenset(
    {
        "style_features",
        "narrative_patterns",
        "calibration_guidance",
        "banned_replication_rules",
        "metrics_baseline",
    }
)
FROZEN_PROFILE_JSON_KEYS = V3_PROFILE_JSON_KEYS | LEGACY_PROFILE_JSON_KEYS
_FROZEN_PROFILE_JSON_KEYS = FROZEN_PROFILE_JSON_KEYS
# 声音块只冻结渲染与旧读者要用的小键（v3 的 ``voice`` 可能带作者自身分布，不进契约）
_VOICE_SIGNATURE_KEYS = ("version", "habits", "deliberate_repetition", "features", "top_words", "stats")
_VOICE_KEYS = ("version", "habits", "deliberate_repetition", "features")
_STRUCTURE_CARD_DROPPED_KEYS = frozenset({"chapters"})
_V2_FORBIDDEN_LAYER_KEYS = ("sample_quote_refs", "sample_paragraph_refs")
_ALLOWED_STRATEGIES = frozenset({"A", "B", "C", "mixed"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _json_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _profile_value(profile: Any, name: str, default: Any = None) -> Any:
    if isinstance(profile, Mapping):
        return profile.get(name, default)
    return getattr(profile, name, default)


DRAFT_MODE_STYLE_FIRST = "style_first"
DRAFT_MODE_NEUTRAL_FIRST = "neutral_first"
_ALLOWED_DRAFT_MODES = frozenset({DRAFT_MODE_STYLE_FIRST, DRAFT_MODE_NEUTRAL_FIRST})


def _default_draft_mode() -> str:
    """``injection_budget.yaml`` 的 ``draft_mode_default``(缺省 style_first)。"""
    try:
        budget = load_yaml_config("injection_budget")
    except FileNotFoundError:
        budget = {}
    value = str(budget.get("draft_mode_default") or "").strip().lower()
    return value if value in _ALLOWED_DRAFT_MODES else DRAFT_MODE_STYLE_FIRST


def resolve_draft_mode(config_json: Mapping[str, Any] | None) -> str:
    """一个绑定的生效起草方式:``config_json.draft_mode`` 合法即用,否则取 yaml 缺省。"""
    raw = ""
    if isinstance(config_json, Mapping):
        raw = str(config_json.get("draft_mode") or "").strip().lower()
    return raw if raw in _ALLOWED_DRAFT_MODES else _default_draft_mode()


def compute_paragraph_root(repo: Any, book_id: str) -> tuple[str, int]:
    """整本书段落的根哈希与段落数(按 paragraph_index 升序;只含哈希,不含原文)。

    root = sha256(Σ ``f"{index}\\x1f" + sha256(text) + "\\x1e"``)。段落被改动 / 增删 / 换序都会
    改变根哈希;书没有段落时返回 ("", 0)。v3 起契约构建走 ``paragraph_root.ensure_paragraph_root``
    (存在 stats_json、两列快速路径,逐位相同);这里只留给没有会话的仓储与口径对照测试。
    """
    paragraphs = repo.list_paragraphs(str(book_id))
    digest = hashlib.sha256()
    count = 0
    for paragraph in paragraphs:
        try:
            index = int(getattr(paragraph, "paragraph_index", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        text = str(getattr(paragraph, "text", "") or "")
        digest.update(f"{index}\x1f".encode("utf-8"))
        digest.update(_text_hash(text).encode("utf-8"))
        digest.update(b"\x1e")
        count += 1
    if count == 0:
        return "", 0
    return digest.hexdigest(), count


def _paragraph_root(repo: Any, book_id: str) -> tuple[str, int]:
    session = getattr(repo, "session", None)
    try:
        if session is not None:
            return ensure_paragraph_root(session, str(book_id))
        return compute_paragraph_root(repo, str(book_id))
    except Exception:  # noqa: BLE001 — 算不出根哈希:契约照建,渲染期按当前索引走
        logger.warning("style contract paragraph root unavailable", exc_info=True)
        return "", 0


def frozen_profile_json(raw_profile_json: Mapping[str, Any] | None) -> dict[str, Any]:
    """画像 → 契约里冻结的那部分（白名单；结构画像去掉逐章列表；声音只留小键）。"""
    raw = raw_profile_json if isinstance(raw_profile_json, Mapping) else {}
    frozen: dict[str, Any] = {}
    for key in sorted(FROZEN_PROFILE_JSON_KEYS):
        if key not in raw:
            continue
        value = copy.deepcopy(raw[key])
        if key == "structure_card" and isinstance(value, Mapping):
            value = {k: v for k, v in value.items() if k not in _STRUCTURE_CARD_DROPPED_KEYS}
        elif key == "voice_signature" and isinstance(value, Mapping):
            value = {k: value[k] for k in _VOICE_SIGNATURE_KEYS if k in value}
        elif key == "voice" and isinstance(value, Mapping):
            value = {k: value[k] for k in _VOICE_KEYS if k in value}
        frozen[key] = value
    return frozen


def legacy_forbidden_findings(repo: Any, profile: Any, raw_profile_json: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """旧画像（没有文风卡）的禁忌陈述：合成期的 ``generation_safe_forbidden_findings``，更旧的画像回退查 finding 表。

    有文风卡的 v3 画像返回 ``[]``——「作者不这么写」已经在卡里。
    """
    raw = raw_profile_json if isinstance(raw_profile_json, Mapping) else {}
    if isinstance(raw.get("dimension_card"), Mapping):
        return []
    safe_forbidden = raw.get("generation_safe_forbidden_findings")
    if isinstance(safe_forbidden, list):
        return [
            {
                "finding_id": str(item.get("finding_id") or ""),
                "sub_dimension": str(item.get("sub_dimension") or ""),
                "statement": str(item.get("statement") or ""),
                "status": str(item.get("status") or ""),
            }
            for item in copy.deepcopy(safe_forbidden)
            if isinstance(item, Mapping) and str(item.get("finding_id") or "") and isinstance(item.get("statement"), str)
        ]
    findings: list[dict[str, Any]] = []
    for finding_id in list(_profile_value(profile, "source_finding_ids_json", None) or []):
        finding = repo.get_finding(str(finding_id))
        if finding is None or getattr(finding, "finding_kind", None) != "forbidden_pattern":
            continue
        findings.append(
            {
                "finding_id": str(finding.finding_id),
                "sub_dimension": str(finding.sub_dimension or ""),
                "statement": str(finding.statement or ""),
                "status": str(finding.status or ""),
            }
        )
    return findings


def build_style_runtime_contract(
    repo: Any,
    layers: Sequence[Any],
    *,
    task_type: str,
) -> dict[str, Any] | None:
    """冻结一份 v2 契约：从命中层里挑最具体的一层（``inject.bindings.most_specific_binding``）。

    ``layers`` 可以是整组命中层（由泛到具体，角色层 POV 在前）或只有一层；无层 → ``None``。
    """
    if not layers:
        return None
    binding = most_specific_binding(list(layers))
    if binding is None:
        return None
    if (
        getattr(binding, "status", None) != "active"
        or str(getattr(binding, "task_type", "") or "") != str(task_type)
    ):
        raise ValueError("style binding is not active for the requested task")
    profile = repo.get_profile(str(binding.profile_id))
    if profile is None or getattr(profile, "status", None) != "active":
        raise ValueError(f"active style profile missing: {binding.profile_id}")
    raw_profile_json = getattr(profile, "profile_json", None) or {}
    if not isinstance(raw_profile_json, Mapping):
        raise ValueError("style profile payload must be an object")
    raw_finding_ids = getattr(profile, "source_finding_ids_json", None) or []
    if not isinstance(raw_finding_ids, Sequence) or isinstance(raw_finding_ids, (str, bytes, bytearray)):
        raise ValueError("style profile finding ids must be a list")
    banned_terms = sorted(
        {
            str(getattr(term, "term", "") or "").strip()
            for term in repo.list_banned_terms(str(profile.profile_id), scope="generation")
            if str(getattr(term, "term", "") or "").strip()
        }
    )
    book = repo.get_book(str(profile.book_id))
    paragraph_root, paragraph_count = _paragraph_root(repo, str(profile.book_id))
    stats = getattr(book, "stats_json", None) if book is not None else None
    marker = stats.get("window_index") if isinstance(stats, Mapping) else None
    book_snapshot: dict[str, Any] = {
        "book_id": str(profile.book_id),
        "text_checksum": str(getattr(book, "text_checksum", "") or ""),
        "cloud_policy": str(getattr(book, "cloud_policy", "") or ""),
        "cloud_llm_allowed_at_freeze": bool(book is not None and cloud_llm_allowed(book)),
        "window_index_version": str(marker.get("version") or "") or None if isinstance(marker, Mapping) else None,
    }
    if paragraph_root:
        book_snapshot["paragraph_root_sha256"] = paragraph_root
        book_snapshot["paragraph_count"] = int(paragraph_count)
    raw_config = getattr(binding, "config_json", None) or {}
    if not isinstance(raw_config, Mapping):
        raise ValueError("style binding config must be an object")
    draft_mode = resolve_draft_mode(raw_config)
    config = normalize_binding_config(str(binding.strategy or "mixed"), raw_config)
    config["draft_mode"] = draft_mode
    layer: dict[str, Any] = {
        "order": 0,
        "binding": {
            "binding_id": str(binding.binding_id),
            "profile_id": str(binding.profile_id),
            "scope": str(binding.scope),
            "scope_ref_id": str(binding.scope_ref_id or ""),
            "task_type": str(binding.task_type),
            "strategy": str(binding.strategy),
            "status": str(binding.status),
            "config_json": config,
        },
        "profile": {
            "profile_id": str(profile.profile_id),
            "book_id": str(profile.book_id),
            "run_id": str(getattr(profile, "run_id", "") or ""),
            "version_tag": str(getattr(profile, "version_tag", "") or ""),
            "status": str(profile.status),
            "profile_json": frozen_profile_json(raw_profile_json),
            "source_finding_ids_json": [str(item) for item in raw_finding_ids if str(item or "")],
        },
        "forbidden_findings": legacy_forbidden_findings(repo, profile, raw_profile_json),
        "banned_terms": banned_terms,
        "book": book_snapshot,
    }
    layer["layer_hash"] = _json_hash(layer)
    contract: dict[str, Any] = {
        "schema_version": 2,
        "contract_version": STYLE_RUNTIME_CONTRACT_VERSION_V2,
        "task_type": str(task_type),
        "profile_ids": [layer["profile"]["profile_id"]],
        "binding_ids": [layer["binding"]["binding_id"]],
        "layer_count": 1,
        "layers": [layer],
        # 起草方式随契约冻结(生效层说了算,缺省取 yaml);重放旧 bundle 时不再看今天的配置。
        "draft_mode": draft_mode,
    }
    contract["contract_hash"] = _json_hash(contract)
    return validate_style_runtime_contract(contract)


# ---------------------------------------------------------------------------
# 校验（按内容指纹记忆；每次返回新对象）
# ---------------------------------------------------------------------------

_VALIDATED_MAX = 128
_VALIDATED: "OrderedDict[str, str]" = OrderedDict()
_VALIDATED_LOCK = threading.Lock()


def reset_contract_memo() -> None:
    with _VALIDATED_LOCK:
        _VALIDATED.clear()


def _memo_get(key: str) -> dict[str, Any] | None:
    with _VALIDATED_LOCK:
        cached = _VALIDATED.get(key)
        if cached is None:
            return None
        _VALIDATED.move_to_end(key)
    return json.loads(cached)


def _memo_put(key: str, contract: Mapping[str, Any]) -> None:
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _VALIDATED_LOCK:
        _VALIDATED[key] = encoded
        while len(_VALIDATED) > _VALIDATED_MAX:
            _VALIDATED.popitem(last=False)


def _fingerprint(payload: Mapping[str, Any]) -> str | None:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return "p:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_style_runtime_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验 v1 / v2 契约并返回一份新的副本；同一内容只真正校验一次（进程内按内容指纹记忆）。"""
    if not isinstance(payload, Mapping):
        raise ValueError("style runtime contract must be an object")
    key = _fingerprint(payload)
    if key is not None:
        cached = _memo_get(key)
        if cached is not None:
            return cached
    version = payload.get("contract_version")
    if version == STYLE_RUNTIME_CONTRACT_VERSION_V2:
        contract = _validate_v2(payload)
    elif version == STYLE_RUNTIME_CONTRACT_VERSION_V1:
        contract = _validate_v1(payload)
    else:
        raise ValueError("unsupported style runtime contract")
    if key is not None:
        _memo_put(key, contract)
        return json.loads(json.dumps(contract, ensure_ascii=False))
    return contract


def _validate_v2(payload: Mapping[str, Any]) -> dict[str, Any]:
    contract = copy.deepcopy(dict(payload))
    supplied_hash = str(contract.pop("contract_hash", "") or "")
    if (
        type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != 2
        or contract.get("contract_version") != STYLE_RUNTIME_CONTRACT_VERSION_V2
    ):
        raise ValueError("unsupported style runtime contract")
    layers = contract.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise ValueError("style runtime contract v2 must freeze exactly one layer")
    if type(contract.get("layer_count")) is not int or contract.get("layer_count") != 1:
        raise ValueError("style runtime contract layer count mismatch")
    task_type = str(contract.get("task_type") or "")
    if not task_type:
        raise ValueError("style runtime contract task type is missing")
    layer = layers[0]
    if not isinstance(layer, Mapping) or type(layer.get("order")) is not int or layer.get("order") != 0:
        raise ValueError("style runtime contract layer order is invalid")
    if any(key in layer for key in _V2_FORBIDDEN_LAYER_KEYS):
        raise ValueError("style runtime contract v2 must not carry sample references")
    layer_copy = copy.deepcopy(dict(layer))
    layer_hash = str(layer_copy.pop("layer_hash", "") or "")
    if not layer_hash or _json_hash(layer_copy) != layer_hash:
        raise ValueError("style runtime contract layer hash mismatch")
    binding = layer.get("binding")
    profile = layer.get("profile")
    book = layer.get("book")
    if not isinstance(binding, Mapping) or not isinstance(profile, Mapping) or not isinstance(book, Mapping):
        raise ValueError("style runtime contract layer snapshot is invalid")
    if not isinstance(binding.get("config_json"), Mapping) or not isinstance(profile.get("profile_json"), Mapping):
        raise ValueError("style runtime contract payload shape is invalid")
    if not set(profile["profile_json"]).issubset(FROZEN_PROFILE_JSON_KEYS):
        raise ValueError("style runtime contract profile payload is not allow-listed")
    finding_ids = profile.get("source_finding_ids_json", [])
    if not isinstance(finding_ids, list) or any(not isinstance(item, str) or not item for item in finding_ids):
        raise ValueError("style runtime contract finding ids are malformed")
    if not isinstance(layer.get("forbidden_findings"), list) or not isinstance(layer.get("banned_terms"), list):
        raise ValueError("style runtime contract safety inputs are invalid")
    if any(
        not isinstance(item, Mapping)
        or not str(item.get("finding_id") or "")
        or not isinstance(item.get("statement"), str)
        for item in layer["forbidden_findings"]
    ):
        raise ValueError("style runtime contract forbidden findings are malformed")
    if any(not isinstance(term, str) or not term for term in layer["banned_terms"]):
        raise ValueError("style runtime contract banned terms are malformed")
    profile_id = str(profile.get("profile_id") or "")
    binding_id = str(binding.get("binding_id") or "")
    book_id = str(profile.get("book_id") or "")
    if (
        not profile_id
        or not binding_id
        or not book_id
        or str(binding.get("profile_id") or "") != profile_id
        or str(binding.get("task_type") or "") != task_type
        or str(binding.get("status") or "") != "active"
        or str(binding.get("strategy") or "") not in _ALLOWED_STRATEGIES
        or not str(binding.get("scope") or "")
        or str(profile.get("status") or "") != "active"
        or str(book.get("book_id") or "") != book_id
        or type(book.get("cloud_llm_allowed_at_freeze")) is not bool
        or not isinstance(book.get("cloud_policy", ""), str)
    ):
        raise ValueError("style runtime contract layer lineage is invalid")
    paragraph_root = book.get("paragraph_root_sha256")
    if paragraph_root is not None and (
        not isinstance(paragraph_root, str)
        or _SHA256_RE.fullmatch(paragraph_root) is None
        or type(book.get("paragraph_count")) is not int
        or int(book.get("paragraph_count")) < 0
    ):
        raise ValueError("style runtime contract book paragraph root is malformed")
    if contract.get("draft_mode") not in _ALLOWED_DRAFT_MODES:
        raise ValueError("style runtime contract draft mode is invalid")
    if contract.get("profile_ids") != [profile_id]:
        raise ValueError("style runtime contract profile ids mismatch")
    if contract.get("binding_ids") != [binding_id]:
        raise ValueError("style runtime contract binding ids mismatch")
    computed_hash = _json_hash(contract)
    if not supplied_hash or supplied_hash != computed_hash:
        raise ValueError("style runtime contract hash mismatch")
    contract["contract_hash"] = supplied_hash
    return contract


def _validate_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
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
        if not layer_hash or _json_hash(layer_copy) != layer_hash:
            raise ValueError("style runtime contract layer hash mismatch")
        binding = layer.get("binding")
        profile = layer.get("profile")
        if not isinstance(binding, Mapping) or not isinstance(profile, Mapping):
            raise ValueError("style runtime contract layer snapshot is invalid")
        if not isinstance(binding.get("config_json"), Mapping) or not isinstance(
            profile.get("profile_json"), Mapping
        ):
            raise ValueError("style runtime contract payload shape is invalid")
        if not set(profile["profile_json"]).issubset(_FROZEN_PROFILE_JSON_KEYS_V1):
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
                or _SHA256_RE.fullmatch(paragraph_sha256) is None
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
                or _SHA256_RE.fullmatch(quote_sha256) is None
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
            or str(binding.get("strategy") or "") not in _ALLOWED_STRATEGIES
            or not str(binding.get("scope") or "")
            or str(profile.get("status") or "") != "active"
            or str(book.get("book_id") or "") != book_id
            or type(book.get("cloud_llm_allowed_at_freeze")) is not bool
        ):
            raise ValueError("style runtime contract layer lineage is invalid")
        paragraph_root = book.get("paragraph_root_sha256")
        if paragraph_root is not None and (
            not isinstance(paragraph_root, str)
            or _SHA256_RE.fullmatch(paragraph_root) is None
            or type(book.get("paragraph_count")) is not int
            or int(book.get("paragraph_count")) < 0
        ):
            raise ValueError("style runtime contract book paragraph root is malformed")
        if profile_id not in expected_profile_ids:
            expected_profile_ids.append(profile_id)
        expected_binding_ids.append(binding_id)
    if "draft_mode" in contract and (
        not isinstance(contract.get("draft_mode"), str)
        or contract.get("draft_mode") not in _ALLOWED_DRAFT_MODES
    ):
        raise ValueError("style runtime contract draft mode is invalid")
    if contract.get("profile_ids") != expected_profile_ids:
        raise ValueError("style runtime contract profile ids mismatch")
    if contract.get("binding_ids") != expected_binding_ids:
        raise ValueError("style runtime contract binding ids mismatch")
    computed_hash = _json_hash(contract)
    if not supplied_hash or supplied_hash != computed_hash:
        raise ValueError("style runtime contract hash mismatch")
    contract["contract_hash"] = supplied_hash
    return contract


def style_runtime_contract_from_bundle(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> dict[str, Any] | None:
    if not isinstance(bundle_or_snapshot, Mapping):
        return None
    snapshot = bundle_or_snapshot.get("snapshot")
    if not isinstance(snapshot, Mapping):
        snapshot = bundle_or_snapshot
    inline = snapshot.get("inline_digests")
    if not isinstance(inline, Mapping):
        return None
    key = (
        "_style_reference_runtime_contract"
        if task_type == "scene_generation"
        else f"_style_reference_runtime_contract_{task_type}"
    )
    raw = inline.get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        # bundle 里冻结的是 JSON 字符串:按字符串本身记忆,命中时连解析都省了
        raw_key = "s:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cached = _memo_get(raw_key)
        if cached is not None:
            return cached
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("style runtime contract JSON is invalid") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("style runtime contract must be an object")
        contract = validate_style_runtime_contract(parsed)
        _memo_put(raw_key, contract)
        return contract
    if not isinstance(raw, Mapping):
        raise ValueError("style runtime contract must be an object")
    return validate_style_runtime_contract(raw)


def style_runtime_contract_status_from_bundle(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> str | None:
    """Return the bundle's freeze status, or ``None`` for a legacy bundle."""

    if not isinstance(bundle_or_snapshot, Mapping):
        return None
    snapshot = bundle_or_snapshot.get("snapshot")
    if not isinstance(snapshot, Mapping):
        snapshot = bundle_or_snapshot
    refs = snapshot.get("source_version_refs")
    if not isinstance(refs, Mapping):
        return None
    prefix = "" if task_type == "scene_generation" else f"{task_type}_"
    status_key = f"{prefix}style_reference_runtime_contract_status"
    version_key = f"{prefix}style_reference_runtime_contract_version"
    status = str(refs.get(status_key) or "").strip().lower()
    if status:
        return status
    # Transitional bundles may carry the version/hash but predate the explicit
    # status field. Treat them as contract-aware so they cannot fall through to
    # live binding resolution if their embedded contract goes missing.
    if refs.get(version_key) in SUPPORTED_CONTRACT_VERSIONS:
        return "expected"
    return None


def contract_profile_objects(
    contract: Mapping[str, Any],
    *,
    per_layer: bool = True,
) -> list[Any]:
    validated = validate_style_runtime_contract(contract)
    snapshots = []
    for layer in validated["layers"]:
        snapshot = dict(layer["profile"])
        # These runtime-only attributes let validation consume the same frozen
        # safety inputs as generation without changing the persisted profile
        # payload or consulting mutable rows by profile_id.
        snapshot["runtime_contract_banned_terms"] = copy.deepcopy(
            list(layer["banned_terms"])
        )
        snapshot["runtime_contract_forbidden_findings"] = copy.deepcopy(
            list(layer["forbidden_findings"])
        )
        snapshot["runtime_contract_book"] = copy.deepcopy(dict(layer["book"]))
        snapshots.append(snapshot)
    if not per_layer:
        snapshots = list(
            {snapshot["profile_id"]: snapshot for snapshot in snapshots}.values()
        )
    return [SimpleNamespace(**copy.deepcopy(snapshot)) for snapshot in snapshots]


def blend_profile_metric_baselines(
    profiles: Sequence[Any],
) -> dict[str, dict[str, float | int]]:
    """Blend ordered generic→specific baselines with total variance."""
    if not profiles:
        return {}
    weighted_profiles = list(zip(profiles, range(1, len(profiles) + 1), strict=True))
    metric_names: set[str] = set()
    for profile, _weight in weighted_profiles:
        profile_json = _profile_value(profile, "profile_json", {}) or {}
        baseline = (
            profile_json.get("metrics_baseline")
            if isinstance(profile_json, Mapping)
            else {}
        )
        if isinstance(baseline, Mapping):
            metric_names.update(str(name) for name in baseline)

    blended: dict[str, dict[str, float | int]] = {}
    for metric in sorted(metric_names):
        components: list[tuple[float, float, float]] = []
        for profile, weight in weighted_profiles:
            profile_json = _profile_value(profile, "profile_json", {}) or {}
            baseline = (
                profile_json.get("metrics_baseline")
                if isinstance(profile_json, Mapping)
                else {}
            )
            raw = baseline.get(metric) if isinstance(baseline, Mapping) else None
            if isinstance(raw, Mapping):
                raw_mean = raw.get("mean")
                raw_std = raw.get("std", 0.0)
            else:
                raw_mean = raw
                raw_std = 0.0
            try:
                mean = float(raw_mean)
                std = float(raw_std)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(mean) or not math.isfinite(std) or std < 0:
                continue
            components.append((float(weight), mean, std))
        if not components:
            continue
        weight_sum = sum(weight for weight, _mean, _std in components)
        target_mean = (
            sum(weight * mean for weight, mean, _std in components) / weight_sum
        )
        target_variance = (
            sum(
                weight * (std**2 + (mean - target_mean) ** 2)
                for weight, mean, std in components
            )
            / weight_sum
        )
        blended[metric] = {
            "mean": target_mean,
            "std": math.sqrt(max(0.0, target_variance)),
            "component_count": len(components),
        }
    return blended


def contract_metric_mean_map(contract: Mapping[str, Any]) -> dict[str, float]:
    baseline = blend_profile_metric_baselines(contract_profile_objects(contract))
    return {
        metric: float(stats["mean"])
        for metric, stats in baseline.items()
        if isinstance(stats, Mapping) and "mean" in stats
    }


@dataclass(frozen=True, slots=True)
class StyleGenerationContext:
    query_text: str
    source_kind: str
    query_sha256: str
    char_count: int
    version: str = STYLE_CONTEXT_VERSION

    def audit_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_kind": self.source_kind,
            "query_sha256": self.query_sha256,
            "char_count": self.char_count,
        }


@dataclass(frozen=True, slots=True)
class StyleRuntimeContractState:
    """One normalized interpretation of a bundle's style-contract state.

    ``error_code`` is deliberately data, not an exception, so optional consumers
    can degrade in their own way without ever treating an inconsistent new bundle
    as permission to resolve today's live bindings.
    """

    status: str | None
    mode: str
    contract: dict[str, Any] | None = None
    error_code: str | None = None


def resolve_style_runtime_contract_state(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> StyleRuntimeContractState:
    """Normalize legacy/frozen/absent/degraded states for every runtime consumer."""

    status = style_runtime_contract_status_from_bundle(
        bundle_or_snapshot,
        task_type=task_type,
    )
    try:
        contract = style_runtime_contract_from_bundle(
            bundle_or_snapshot,
            task_type=task_type,
        )
    except (TypeError, ValueError):
        return StyleRuntimeContractState(
            status=status,
            mode="degraded",
            error_code="runtime_contract_invalid",
        )

    if status == "absent":
        if contract is not None:
            return StyleRuntimeContractState(
                status=status,
                mode="degraded",
                error_code="runtime_contract_status_conflict",
            )
        return StyleRuntimeContractState(status=status, mode="absent")
    if status in {"frozen", "expected"}:
        if contract is None:
            return StyleRuntimeContractState(
                status=status,
                mode="degraded",
                error_code="runtime_contract_missing",
            )
        return StyleRuntimeContractState(
            status=status,
            mode="frozen",
            contract=contract,
        )
    if status == "degraded":
        return StyleRuntimeContractState(
            status=status,
            mode="degraded",
            error_code="runtime_contract_degraded",
        )
    if status is not None:
        return StyleRuntimeContractState(
            status=status,
            mode="degraded",
            error_code="runtime_contract_status_invalid",
        )
    if contract is not None:
        # Early contract prototypes had an embedded contract but no status marker.
        return StyleRuntimeContractState(
            status=None,
            mode="frozen_legacy",
            contract=contract,
        )
    return StyleRuntimeContractState(status=None, mode="legacy_live")


_STYLE_BOUND_MODES = frozenset({"frozen", "frozen_legacy"})


def effective_draft_mode(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> str:
    """这份 bundle 的起草方式。

    只有冻结的契约(``frozen`` / ``frozen_legacy``)且契约写了 ``style_first`` 才是
    style_first;无绑定 / absent / degraded / 旧契约缺键一律 ``neutral_first``——让位与
    首稿直起都以此为准,无绑定的项目行为逐字不变。
    """
    state = resolve_style_runtime_contract_state(bundle_or_snapshot, task_type=task_type)
    if state.mode not in _STYLE_BOUND_MODES or not isinstance(state.contract, Mapping):
        return DRAFT_MODE_NEUTRAL_FIRST
    mode = str(state.contract.get("draft_mode") or "")
    return mode if mode in _ALLOWED_DRAFT_MODES else DRAFT_MODE_NEUTRAL_FIRST


def is_style_bound(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> bool:
    """``effective_draft_mode(...) == "style_first"``:房风门让位与首稿直起的统一条件。"""
    return effective_draft_mode(bundle_or_snapshot, task_type=task_type) == DRAFT_MODE_STYLE_FIRST


def extract_style_generation_context(
    text: str | None,
    *,
    source_kind: str,
    max_chars: int = 2000,
) -> StyleGenerationContext:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", normalized).strip()
    query = normalized[-max(1, int(max_chars)) :] if normalized else ""
    return StyleGenerationContext(
        query_text=query,
        source_kind=str(source_kind),
        query_sha256=_text_hash(query),
        char_count=len(query),
    )


__all__ = [
    "FROZEN_PROFILE_JSON_KEYS",
    "LEGACY_PROFILE_JSON_KEYS",
    "STYLE_CONTEXT_VERSION",
    "DRAFT_MODE_NEUTRAL_FIRST",
    "DRAFT_MODE_STYLE_FIRST",
    "STYLE_RUNTIME_CONTRACT_VERSION",
    "STYLE_RUNTIME_CONTRACT_VERSION_V1",
    "STYLE_RUNTIME_CONTRACT_VERSION_V2",
    "SUPPORTED_CONTRACT_VERSIONS",
    "V3_PROFILE_JSON_KEYS",
    "compute_paragraph_root",
    "frozen_profile_json",
    "legacy_forbidden_findings",
    "reset_contract_memo",
    "StyleGenerationContext",
    "StyleRuntimeContractState",
    "blend_profile_metric_baselines",
    "build_style_runtime_contract",
    "contract_metric_mean_map",
    "contract_profile_objects",
    "effective_draft_mode",
    "extract_style_generation_context",
    "is_style_bound",
    "resolve_draft_mode",
    "resolve_style_runtime_contract_state",
    "style_runtime_contract_from_bundle",
    "style_runtime_contract_status_from_bundle",
    "validate_style_runtime_contract",
]
