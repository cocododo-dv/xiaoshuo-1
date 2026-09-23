"""风格参考 v3（2026-09-23）— 每个 bundle 解析一次的风格策略。

管线里「有没有风格绑定、要不要让位、怎么送参考」过去散在约 60 处判断里，用了 5 种不同定义（冻结契约 +
style_first / 正在建的契约 / 实时重解析的契约 / 任一活动绑定 / 任一冻结契约），节点之间已经互相矛盾。
v3 起所有节点只看 :class:`StylePolicy`：

- :func:`style_policy_for_bundle`：场景管线（bundle 里冻结的契约），按契约哈希记忆，同一 bundle 只校验一次；
- :func:`style_policy_live`：写作台等没有 bundle 的节点（按当前活动绑定现解析，并记下契约哈希）。

``bound``：有一份可用的风格契约（冻结或现解析）且画像可用——渲染参考、抄袭门、读数都以它为准。
``style_first``：``bound`` 且起草方式是作者手笔直起——房风规则让位、首稿直起、风格步按读数决定。
中立模块：只依赖风格参考包的叶子（runtime_contract / binding_config），不依赖任何管线模块。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novel_system.services.style_reference.binding_config import (
    DEFAULT_SAMPLE_WINDOWS,
    DRAFT_MODE_NEUTRAL_FIRST,
    DRAFT_MODE_STYLE_FIRST,
    REFERENCE_MODE_FULL,
    effective_reference_mode,
    normalize_binding_config,
    normalize_dimension_states,
    sends_card,
    sends_samples,
)
from novel_system.services.style_reference.runtime_contract import (
    contract_layer,
    resolve_style_runtime_contract_state,
)

logger = logging.getLogger(__name__)

MODE_NONE = "none"  # 没有绑定、也没有冻结记录（旧 bundle / 无 scope）
MODE_ABSENT = "absent"  # bundle 明确冻结了「没有绑定」
MODE_FROZEN = "frozen"
MODE_FROZEN_LEGACY = "frozen_legacy"
MODE_LIVE = "live"
MODE_DEGRADED = "degraded"
BOUND_MODES = frozenset({MODE_FROZEN, MODE_FROZEN_LEGACY, MODE_LIVE})

_CACHE_MAX = 64
_CACHE: "OrderedDict[str, StylePolicy]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class StylePolicy:
    bound: bool = False
    style_first: bool = False
    mode: str = MODE_NONE
    contract: Mapping[str, Any] | None = None
    contract_hash: str | None = None
    profile_id: str | None = None
    binding_id: str | None = None
    book_id: str | None = None
    reference_mode: str = REFERENCE_MODE_FULL
    sample_windows: int = DEFAULT_SAMPLE_WINDOWS
    dimension_states: Mapping[str, str] = field(default_factory=dict)
    draft_mode: str = DRAFT_MODE_NEUTRAL_FIRST
    error_code: str | None = None

    def defers_house_taste(self) -> bool:
        """房风规则（反模板、收尾动作、字数形状、新鲜度构式清单……）是否让位给参考。"""
        return self.bound and self.style_first

    @property
    def sends_samples(self) -> bool:
        return self.bound and sends_samples(self.reference_mode)

    @property
    def sends_card(self) -> bool:
        return self.bound and sends_card(self.reference_mode)

    def audit(self) -> dict[str, Any]:
        return {
            "bound": self.bound,
            "style_first": self.style_first,
            "mode": self.mode,
            "contract_hash": self.contract_hash,
            "profile_id": self.profile_id,
            "binding_id": self.binding_id,
            "reference_mode": self.reference_mode,
            "sample_windows": self.sample_windows,
            "draft_mode": self.draft_mode,
            "error_code": self.error_code,
        }


UNBOUND = StylePolicy()


def policy_from_contract(contract: Mapping[str, Any], *, mode: str) -> StylePolicy:
    """已校验的契约 → 策略。v2 契约只有一层；v1 的多层契约取最具体的一层（``runtime_contract.contract_layer``：
    scene > POV 角色 > 其余角色 > project > global——不是层序最后一层，J7）。"""
    layer = contract_layer(contract)
    if not layer:
        return StylePolicy(mode=MODE_DEGRADED, error_code="runtime_contract_invalid")
    binding = layer.get("binding") if isinstance(layer.get("binding"), Mapping) else {}
    profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
    book = layer.get("book") if isinstance(layer.get("book"), Mapping) else {}
    config = normalize_binding_config(binding.get("strategy"), binding.get("config_json"))
    # 起草方式以契约顶层冻结值为准；v1 契约缺键时按「先中性」处理（与旧 effective_draft_mode 一致）
    draft_mode = str(contract.get("draft_mode") or "") or DRAFT_MODE_NEUTRAL_FIRST
    if draft_mode not in (DRAFT_MODE_STYLE_FIRST, DRAFT_MODE_NEUTRAL_FIRST):
        draft_mode = DRAFT_MODE_NEUTRAL_FIRST
    reference_mode = effective_reference_mode(
        config["reference_mode"], cloud_policy=str(book.get("cloud_policy") or "") or None
    )
    return StylePolicy(
        bound=True,
        style_first=draft_mode == DRAFT_MODE_STYLE_FIRST,
        mode=mode,
        contract=contract,
        contract_hash=str(contract.get("contract_hash") or "") or None,
        profile_id=str(profile.get("profile_id") or "") or None,
        binding_id=str(binding.get("binding_id") or "") or None,
        book_id=str(book.get("book_id") or profile.get("book_id") or "") or None,
        reference_mode=reference_mode,
        sample_windows=int(config["sample_windows"]),
        dimension_states=normalize_dimension_states(config["dimension_states"]),
        draft_mode=draft_mode,
    )


def _peek_contract_hash(bundle_or_snapshot: Mapping[str, Any] | None, task_type: str) -> str | None:
    """不校验地读出 bundle 里契约自带的哈希，只用作记忆键（第一次仍完整校验）。"""
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
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, Mapping):
        value = str(raw.get("contract_hash") or "")
        return value or None
    return None


def style_policy_for_bundle(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> StylePolicy:
    """场景管线的风格策略（冻结契约为准；同一契约只校验一次）。"""
    peeked = _peek_contract_hash(bundle_or_snapshot, task_type)
    cache_key = f"{task_type}:{peeked}" if peeked else None
    if cache_key:
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None:
                _CACHE.move_to_end(cache_key)
                return cached
    state = resolve_style_runtime_contract_state(bundle_or_snapshot, task_type=task_type)
    if state.error_code is not None:
        return StylePolicy(mode=MODE_DEGRADED, error_code=state.error_code)
    if state.mode in (MODE_FROZEN, MODE_FROZEN_LEGACY) and isinstance(state.contract, Mapping):
        policy = policy_from_contract(state.contract, mode=state.mode)
        if cache_key and policy.bound:
            with _CACHE_LOCK:
                _CACHE[cache_key] = policy
                while len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
        return policy
    if state.mode == MODE_ABSENT:
        return StylePolicy(mode=MODE_ABSENT)
    return UNBOUND


def style_policy_live(session: Any, scope: Any, *, task_type: str = "scene_generation") -> StylePolicy:
    """没有 bundle 的节点（写作台续写 / 段落补丁 / 深评 / 章级评审）：按当前活动绑定现解析契约。

    ``scope`` 只按属性读（``project_id`` / ``scene_id`` / ``pov_character_id`` / ``onstage_chars_json``），
    与 ``style_prompt_injection.resolve_style_scope`` 的返回值同形。解析失败按未绑定处理并记错误码。
    """
    if scope is None:
        return UNBOUND
    try:
        from novel_system.services.style_reference.inject.bindings import (
            ordered_character_ids,
            resolve_binding_layers,
        )
        from novel_system.services.style_reference.repository import StyleReferenceRepository
        from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract

        layers = resolve_binding_layers(
            session,
            getattr(scope, "project_id", None),
            task_type,
            character_ids=ordered_character_ids(
                getattr(scope, "pov_character_id", None), getattr(scope, "onstage_chars_json", None)
            ),
            scene_id=getattr(scope, "scene_id", None),
        )
        if not layers:
            return UNBOUND
        # 契约构建从整组命中层里挑最具体的一层（scene > POV 角色 > 其余角色 > project > global）——
        # 不能取 layers[-1]：角色层是 POV 优先排的，最后一层是最不重要的配角（J7）
        contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type=task_type)
    except Exception as exc:  # noqa: BLE001 — 实时解析失败：未绑定 + 错误码（不阻断写作台）
        logger.warning("live style policy resolution failed: %s", exc)
        return StylePolicy(mode=MODE_DEGRADED, error_code=getattr(exc, "code", type(exc).__name__))
    if not contract:
        return UNBOUND
    return policy_from_contract(contract, mode=MODE_LIVE)


def reset_style_policy_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "BOUND_MODES",
    "MODE_ABSENT",
    "MODE_DEGRADED",
    "MODE_FROZEN",
    "MODE_FROZEN_LEGACY",
    "MODE_LIVE",
    "MODE_NONE",
    "StylePolicy",
    "UNBOUND",
    "policy_from_contract",
    "reset_style_policy_cache",
    "style_policy_for_bundle",
    "style_policy_live",
]
