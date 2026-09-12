"""Frozen runtime contract shared by style injection, scoring and feedback.

The contract stores abstract profile inputs and hashes of any referenced prose;
raw reference quotes stay in the StyleReference repository.  This lets a scene
bundle freeze exactly which layers/configuration were selected without copying a
book into every bundle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from novel_system.db.models import StyleReferenceParagraph
from novel_system.services.hash_engine import canonical_json
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.policy import cloud_llm_allowed

logger = logging.getLogger(__name__)


STYLE_RUNTIME_CONTRACT_VERSION = "style_reference_runtime_contract_v1"
STYLE_CONTEXT_VERSION = "style_reference_generation_context_v1"
_FROZEN_PROFILE_JSON_KEYS = frozenset(
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
        # 2026-09 风格模仿 v2(W1 合成期写入):确定性声音签名 + habits、叙事机制指引。
        # 旧画像没有这些键时,下游一律不渲染对应块(优雅退化)。
        "voice_signature",
        "narrative_guidance",
        # 2026-09-12 结构跟随(Step 2 Track B):结构画像(章 / 场尺度、开合方式、段型比重、
        # 章首章尾样例)与规划层指引(scene.* / theme.* 观察陈述)。旧画像没有时不渲染。
        "structure_card",
        "planning_guidance",
    }
)
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


def _quote_ids(profile_json: Mapping[str, Any]) -> list[str]:
    index = profile_json.get("scene_samples_index")
    if not isinstance(index, Mapping):
        return []
    values: list[str] = []
    for raw_ids in index.values():
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, Sequence):
            continue
        values.extend(str(value).strip() for value in raw_ids if str(value).strip())
    return list(dict.fromkeys(values))


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


def _few_shot_window_span() -> int:
    """每侧要冻结哈希的相邻段数(``few_shot_contract_neighbour_span``,默认 2)。

    2026-09-09 样例优先:窗口可达数十段,不再逐段冻结相邻段——契约改冻整本书的段落根哈希
    (``book.paragraph_root_sha256``,:func:`compute_paragraph_root`),根哈希一致时全书段落
    都可进窗口;这里冻结的少量相邻段只是根哈希失配时的兜底路径(窗口退化到这些段落之内)。
    """
    try:
        budget = load_yaml_config("injection_budget")
    except FileNotFoundError:
        budget = {}
    try:
        span = int(budget.get("few_shot_contract_neighbour_span", 2))
    except (TypeError, ValueError):
        span = 2
    return max(0, span)


def compute_paragraph_root(repo: Any, book_id: str) -> tuple[str, int]:
    """整本书段落的根哈希与段落数(按 paragraph_index 升序;只含哈希,不含原文)。

    root = sha256(Σ ``f"{index}\\x1f" + sha256(text) + "\\x1e"``)。段落被改动 / 增删 / 换序都会
    改变根哈希;书没有段落时返回 ("", 0)。
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


def _contiguous_neighbours(repo: Any, paragraph: Any, span: int) -> list[Any]:
    """``paragraph`` 同书、paragraph_index 连续(遇缺口 / 空段即停)的 ±``span`` 相邻段。

    与 ``injection._build_sample_window`` 的展开规则一致(不含中心段,按 index 升序);
    读取失败退化为不冻结相邻段(窗口随之退化为单段,与旧契约行为相同)。
    """
    if span <= 0:
        return []
    session = getattr(repo, "session", None)
    book_id = str(getattr(paragraph, "book_id", "") or "")
    try:
        center = int(getattr(paragraph, "paragraph_index", 0) or 0)
    except (TypeError, ValueError):
        return []
    if session is None or not book_id:
        return []
    stmt = (
        select(StyleReferenceParagraph)
        .where(
            StyleReferenceParagraph.book_id == book_id,
            StyleReferenceParagraph.paragraph_index >= center - span,
            StyleReferenceParagraph.paragraph_index <= center + span,
        )
        .order_by(StyleReferenceParagraph.paragraph_index)
    )
    try:
        rows = list(session.scalars(stmt).all())
    except Exception:  # noqa: BLE001 — 相邻段读取失败退化为只冻结父段
        logger.warning("style contract neighbour paragraph lookup failed", exc_info=True)
        return []
    by_index: dict[int, Any] = {}
    for row in rows:
        try:
            by_index[int(row.paragraph_index)] = row
        except (TypeError, ValueError):
            continue

    def _usable(row: Any) -> bool:
        return bool(str(getattr(row, "text", "") or "").strip())

    left: list[Any] = []
    index = center - 1
    while len(left) < span and index in by_index and _usable(by_index[index]):
        left.insert(0, by_index[index])
        index -= 1
    right: list[Any] = []
    index = center + 1
    while len(right) < span and index in by_index and _usable(by_index[index]):
        right.append(by_index[index])
        index += 1
    return [*left, *right]


def build_style_runtime_contract(
    repo: Any,
    layers: Sequence[Any],
    *,
    task_type: str,
) -> dict[str, Any] | None:
    """Freeze ordered binding layers and every abstract input used for rendering."""
    if not layers:
        return None
    window_span = _few_shot_window_span()
    frozen_layers: list[dict[str, Any]] = []
    for order, binding in enumerate(layers):
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
        # Keep this an explicit allow-list.  A future profile schema may gain
        # raw excerpts or private annotations; those must not silently become
        # part of every immutable SceneBundle.
        profile_json = {
            key: copy.deepcopy(raw_profile_json[key])
            for key in _FROZEN_PROFILE_JSON_KEYS
            if key in raw_profile_json
        }
        raw_finding_ids = getattr(profile, "source_finding_ids_json", None) or []
        if not isinstance(raw_finding_ids, Sequence) or isinstance(
            raw_finding_ids, (str, bytes, bytearray)
        ):
            raise ValueError("style profile finding ids must be a list")
        source_finding_ids = list(copy.deepcopy(raw_finding_ids))
        safe_forbidden = raw_profile_json.get(
            "generation_safe_forbidden_findings"
        )
        if isinstance(safe_forbidden, list):
            forbidden_findings = copy.deepcopy(safe_forbidden)
        else:
            # 旧 Profile 没有确定性原文重合过滤审计时保留兼容路径；新 Profile
            # 一律冻结合成阶段产出的 generation_safe 列表。
            forbidden_findings: list[dict[str, Any]] = []
            for finding_id in source_finding_ids:
                finding = repo.get_finding(str(finding_id))
                if (
                    finding is None
                    or getattr(finding, "finding_kind", None)
                    != "forbidden_pattern"
                ):
                    continue
                forbidden_findings.append(
                    {
                        "finding_id": str(finding.finding_id),
                        "sub_dimension": str(finding.sub_dimension or ""),
                        "statement": str(finding.statement or ""),
                        "status": str(finding.status or ""),
                    }
                )

        quote_refs: list[dict[str, str]] = []
        paragraph_refs: dict[str, dict[str, str]] = {}
        for quote_id in _quote_ids(profile_json):
            quote = repo.get_quote(quote_id)
            quote_text = str(getattr(quote, "quote_text", "") or "")
            if quote_text:
                quote_ref = {
                    "quote_id": quote_id,
                    "quote_sha256": _text_hash(quote_text),
                }
                paragraph_id = str(getattr(quote, "paragraph_id", "") or "")
                paragraph = repo.get_paragraph(paragraph_id) if paragraph_id else None
                paragraph_text = str(getattr(paragraph, "text", "") or "")
                if paragraph_id and paragraph_text:
                    quote_ref["paragraph_id"] = paragraph_id
                    paragraph_refs.setdefault(
                        paragraph_id,
                        {
                            "paragraph_id": paragraph_id,
                            "paragraph_sha256": _text_hash(paragraph_text),
                        },
                    )
                    # v2(W4.5):few-shot 是以 quote 父段为中心的连续 1–3 段窗口,冻结
                    # 路径只允许契约里有 sha256 的段落——所以把父段两侧连续的相邻段一并
                    # 冻结(只冻哈希,不冻原文),否则生产场景运行的窗口全部退化为单段。
                    # 相邻段事后被改动仍会 sha256 失配 → 该侧退化,回放不可变性不变。
                    for neighbour in _contiguous_neighbours(repo, paragraph, window_span):
                        neighbour_id = str(getattr(neighbour, "paragraph_id", "") or "")
                        neighbour_text = str(getattr(neighbour, "text", "") or "")
                        if not neighbour_id or not neighbour_text:
                            continue
                        paragraph_refs.setdefault(
                            neighbour_id,
                            {
                                "paragraph_id": neighbour_id,
                                "paragraph_sha256": _text_hash(neighbour_text),
                            },
                        )
                quote_refs.append(quote_ref)

        banned_terms = sorted(
            {
                str(getattr(term, "term", "") or "").strip()
                for term in repo.list_banned_terms(
                    str(profile.profile_id), scope="generation"
                )
                if str(getattr(term, "term", "") or "").strip()
            }
        )
        book = repo.get_book(str(profile.book_id))
        try:
            paragraph_root, paragraph_count = compute_paragraph_root(
                repo, str(profile.book_id)
            )
        except Exception:  # noqa: BLE001 — 算不出根哈希时契约退回逐段哈希兜底路径
            logger.warning("style contract paragraph root unavailable", exc_info=True)
            paragraph_root, paragraph_count = "", 0
        book_snapshot: dict[str, Any] = {
            "book_id": str(profile.book_id),
            "text_checksum": str(getattr(book, "text_checksum", "") or ""),
            "cloud_llm_allowed_at_freeze": bool(
                book is not None and cloud_llm_allowed(book)
            ),
        }
        if paragraph_root:
            # 2026-09-09 样例优先:冻结整本书段落根哈希(不冻原文),渲染期根哈希一致 → 全书
            # 段落都可进 few-shot 窗口;失配 → 只用下面逐段冻结的相邻段(兜底)。
            book_snapshot["paragraph_root_sha256"] = paragraph_root
            book_snapshot["paragraph_count"] = int(paragraph_count)
        profile_snapshot = {
            "profile_id": str(profile.profile_id),
            "book_id": str(profile.book_id),
            "run_id": str(profile.run_id),
            "version_tag": str(getattr(profile, "version_tag", "") or ""),
            "status": str(profile.status),
            "profile_json": profile_json,
            "source_finding_ids_json": source_finding_ids,
        }
        raw_config = getattr(binding, "config_json", None) or {}
        if not isinstance(raw_config, Mapping):
            raise ValueError("style binding config must be an object")
        binding_snapshot = {
            "binding_id": str(binding.binding_id),
            "profile_id": str(binding.profile_id),
            "scope": str(binding.scope),
            "scope_ref_id": str(binding.scope_ref_id or ""),
            "task_type": str(binding.task_type),
            "strategy": str(binding.strategy),
            "status": str(binding.status),
            "config_json": copy.deepcopy(dict(raw_config)),
        }
        layer = {
            "order": order,
            "binding": binding_snapshot,
            "profile": profile_snapshot,
            "forbidden_findings": forbidden_findings,
            "banned_terms": banned_terms,
            "sample_quote_refs": quote_refs,
            "sample_paragraph_refs": list(paragraph_refs.values()),
            "book": book_snapshot,
        }
        layer["layer_hash"] = _json_hash(layer)
        frozen_layers.append(layer)

    profile_ids = list(
        dict.fromkeys(layer["profile"]["profile_id"] for layer in frozen_layers)
    )
    contract: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": STYLE_RUNTIME_CONTRACT_VERSION,
        "task_type": str(task_type),
        "profile_ids": profile_ids,
        "binding_ids": [layer["binding"]["binding_id"] for layer in frozen_layers],
        "layer_count": len(frozen_layers),
        "layers": frozen_layers,
        # 2026-09-12 风格直起(Step 2):起草方式随契约冻结——最具体的绑定层说了算,缺省
        # 取 yaml;重放旧 bundle 时不再看今天的配置。旧契约没有这个键 → neutral_first。
        "draft_mode": resolve_draft_mode(frozen_layers[-1]["binding"]["config_json"]),
    }
    contract["contract_hash"] = _json_hash(contract)
    return validate_style_runtime_contract(contract)


def validate_style_runtime_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    contract = copy.deepcopy(dict(payload))
    supplied_hash = str(contract.pop("contract_hash", "") or "")
    if (
        type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != 1
        or contract.get("contract_version") != STYLE_RUNTIME_CONTRACT_VERSION
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
        if not set(profile["profile_json"]).issubset(_FROZEN_PROFILE_JSON_KEYS):
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
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("style runtime contract JSON is invalid") from exc
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
    if refs.get(version_key) == STYLE_RUNTIME_CONTRACT_VERSION:
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
    "STYLE_CONTEXT_VERSION",
    "DRAFT_MODE_NEUTRAL_FIRST",
    "DRAFT_MODE_STYLE_FIRST",
    "STYLE_RUNTIME_CONTRACT_VERSION",
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
