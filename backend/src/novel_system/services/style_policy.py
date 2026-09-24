"""风格参考 v3（2026-09-23）— 每个 bundle 解析一次的风格策略。

管线里「有没有风格绑定、要不要让位、怎么送参考」过去散在约 60 处判断里，用了 5 种不同定义（冻结契约 +
style_first / 正在建的契约 / 实时重解析的契约 / 任一活动绑定 / 任一冻结契约），节点之间已经互相矛盾。
v3 起所有节点只看 :class:`StylePolicy`：

- :func:`style_policy_for_bundle`：场景管线（bundle 里冻结的契约），按**契约载荷的内容指纹**（+ 冻结状态）记忆，
  同一 bundle 只校验一次——不按载荷自报的 ``contract_hash`` 记（L2：改过内容、留着原哈希的载荷不能拿回校验过的
  那份策略）；
- :func:`style_policy_live`：写作台等没有 bundle 的节点（按当前活动绑定现解析，并记下契约哈希）。

``bound``：有一份可用的风格契约（冻结或现解析）且画像可用——渲染参考、抄袭门、读数都以它为准。
``style_first``：``bound`` 且起草方式是作者手笔直起——房风规则让位、首稿直起、风格步按读数决定。
中立模块：只依赖风格参考包的叶子（runtime_contract / binding_config / inject.bindings），不依赖任何管线模块。
"""

from __future__ import annotations

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
from novel_system.services.style_reference.inject.bindings import SCOPE_RANK
from novel_system.services.style_reference.runtime_contract import (
    contract_layer,
    contract_payload_fingerprint,
    inline_contract_payload,
    resolve_style_runtime_contract_state,
    style_runtime_contract_status_from_bundle,
)

logger = logging.getLogger(__name__)

MODE_NONE = "none"  # 没有绑定、也没有冻结记录（旧 bundle / 无 scope）
MODE_ABSENT = "absent"  # bundle 明确冻结了「没有绑定」
MODE_FROZEN = "frozen"
MODE_FROZEN_LEGACY = "frozen_legacy"
MODE_LIVE = "live"
MODE_DEGRADED = "degraded"
BOUND_MODES = frozenset({MODE_FROZEN, MODE_FROZEN_LEGACY, MODE_LIVE})
# 2026-09-24（契约文档 §8.1 C7）：轻量现解析里，作者的绑定指向的画像不是 active（归档 / 草稿）——不是「没有绑定」，
# 是「绑了、暂时用不了」：策略降级并带上画像 / 绑定 / 书的 id，抄袭门由此报 unavailable（不是 0 本书通过）
PROFILE_NOT_ACTIVE_CODE = "STYLE_REFERENCE_PROFILE_NOT_ACTIVE"
_UNMATCHED_RANK = 99

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
    config = normalize_binding_config(binding.get("config_json"))
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


def _policy_cache_key(bundle_or_snapshot: Mapping[str, Any] | None, task_type: str) -> str | None:
    """记忆键：冻结状态标记 + 契约**原样载荷**的内容指纹（不校验、不信任载荷自报的 ``contract_hash``）。

    状态也进键：同一份契约挂在「absent」标记下是冲突（degraded），不能拿回挂在「frozen」下校验过的策略。"""
    fingerprint = contract_payload_fingerprint(inline_contract_payload(bundle_or_snapshot, task_type=task_type))
    if fingerprint is None:
        return None
    status = style_runtime_contract_status_from_bundle(bundle_or_snapshot, task_type=task_type) or ""
    return f"{task_type}:{status}:{fingerprint}"


def style_policy_for_bundle(
    bundle_or_snapshot: Mapping[str, Any] | None,
    *,
    task_type: str = "scene_generation",
) -> StylePolicy:
    """场景管线的风格策略（冻结契约为准；同一份契约载荷只校验一次）。"""
    cache_key = _policy_cache_key(bundle_or_snapshot, task_type)
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


def style_policy_live(
    session: Any,
    scope: Any,
    *,
    task_type: str = "scene_generation",
    freeze_contract: bool = True,
) -> StylePolicy:
    """没有 bundle 的节点（写作台续写 / 段落补丁 / 深评 / 章级评审）：按当前活动绑定现解析契约。

    ``scope`` 只按属性读（``project_id`` / ``scene_id`` / ``pov_character_id`` / ``onstage_chars_json``），
    与 ``style_prompt_injection.resolve_style_scope`` 的返回值同形。解析失败按未绑定处理并记错误码。

    ``freeze_contract=False``（P5a）：只读绑定 / 画像状态 / 书的云策略三处的几列，不冻结契约（``contract`` /
    ``contract_hash`` 为 None）——给只需要「绑没绑、是否让位、参考的是哪本书 / 哪份画像」的节点用
    （场景诊断逐场判定、抄袭门、写作台采纳）。冻结一份契约要读全书段落算根哈希（真实库约 0.8 s），
    逐场做不起；要渲染参考的节点仍用默认的冻结路径。选层规则与注入单选一致：scene > character（POV
    优先）> project > global，同层取最新——最具体的一层说了算。命中的绑定里没有一条指向 active 画像时
    **降级**（``error_code=STYLE_REFERENCE_PROFILE_NOT_ACTIVE``，带最具体那条的 profile / binding / book id），
    不当作未绑定（C7）。
    """
    if scope is None:
        return UNBOUND
    if not freeze_contract:
        return _live_policy_without_contract(session, scope, task_type=task_type)
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


def _scope_character_ids(scope: Any) -> list[str]:
    ids: list[str] = []
    for value in [getattr(scope, "pov_character_id", None), *(getattr(scope, "onstage_chars_json", None) or [])]:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def _live_policy_without_contract(session: Any, scope: Any, *, task_type: str) -> StylePolicy:
    """``style_policy_live(..., freeze_contract=False)`` 的实现：只查几列，不加载画像 JSON、不冻结契约。

    与冻结路径同一条选层规则（``inject.bindings.SCOPE_RANK``：scene > character（POV 在前）> project > global，
    同层取最新），且同样只在指向 active 画像的绑定里选；命中的绑定**全部**指向非 active 画像时不再回答
    「未绑定」，而是降级（C7）——冻结路径在这种情形下冻不出契约，这里给出同样的降级形状。"""
    project_id = str(getattr(scope, "project_id", None) or "") or None
    scene_id = str(getattr(scope, "scene_id", None) or "") or None
    character_ids = _scope_character_ids(scope)
    if not project_id and not scene_id and not character_ids:
        return UNBOUND
    try:
        from sqlalchemy import select

        from novel_system.db.models import (
            StyleReferenceBook,
            StyleReferenceInjectionBinding,
            StyleReferenceProfile,
        )
        from novel_system.services.style_reference.runtime_contract import resolve_draft_mode

        bindings = list(
            session.execute(
                select(StyleReferenceInjectionBinding).where(
                    StyleReferenceInjectionBinding.task_type == task_type,
                    StyleReferenceInjectionBinding.status == "active",
                )
            ).scalars()
        )

        def rank(binding: Any) -> tuple[int, int]:
            ref = str(binding.scope_ref_id or "")
            scope_name = str(binding.scope or "")
            if scene_id and scope_name == "scene" and ref == scene_id:
                return SCOPE_RANK["scene"], 0
            if scope_name == "character" and ref in character_ids:
                return SCOPE_RANK["character"], character_ids.index(ref)
            if project_id and scope_name == "project" and ref == project_id:
                return SCOPE_RANK["project"], 0
            if scope_name == "global":
                return SCOPE_RANK["global"], 0
            return _UNMATCHED_RANK, 0

        candidates = [binding for binding in bindings if rank(binding)[0] < _UNMATCHED_RANK]
        if not candidates:
            return UNBOUND
        profile_rows = {
            str(row[0]): (str(row[1] or ""), str(row[2] or ""))
            for row in session.execute(
                select(
                    StyleReferenceProfile.profile_id,
                    StyleReferenceProfile.status,
                    StyleReferenceProfile.book_id,
                ).where(StyleReferenceProfile.profile_id.in_({b.profile_id for b in candidates}))
            ).all()
        }
        # 同层同序取最新（created_at 是 ISO 字符串，字典序即时间序）
        candidates.sort(key=lambda b: str(b.created_at or ""), reverse=True)
        usable = [b for b in candidates if profile_rows.get(str(b.profile_id), ("", ""))[0] == "active"]
        if not usable:
            # 绑了、但画像都不是 active（归档 / 草稿 / 已删）：降级，不是未绑定
            stuck = min(candidates, key=rank)
            return StylePolicy(
                mode=MODE_DEGRADED,
                error_code=PROFILE_NOT_ACTIVE_CODE,
                profile_id=str(stuck.profile_id),
                binding_id=str(stuck.binding_id),
                book_id=profile_rows.get(str(stuck.profile_id), ("", ""))[1] or None,
            )
        best = min(usable, key=rank)
        book_id = profile_rows[str(best.profile_id)][1] or None
        cloud_policy = None
        if book_id:
            cloud_policy = session.execute(
                select(StyleReferenceBook.cloud_policy).where(StyleReferenceBook.book_id == book_id)
            ).scalar_one_or_none()
        raw_config = best.config_json if isinstance(best.config_json, Mapping) else {}
        config = normalize_binding_config(raw_config)
        draft_mode = resolve_draft_mode(raw_config)
    except Exception as exc:  # noqa: BLE001 — 实时解析失败：未绑定 + 错误码（不阻断调用方）
        logger.warning("light live style policy resolution failed: %s", exc)
        return StylePolicy(mode=MODE_DEGRADED, error_code=getattr(exc, "code", type(exc).__name__))
    return StylePolicy(
        bound=True,
        style_first=draft_mode == DRAFT_MODE_STYLE_FIRST,
        mode=MODE_LIVE,
        contract=None,
        contract_hash=None,
        profile_id=str(best.profile_id),
        binding_id=str(best.binding_id),
        book_id=book_id,
        reference_mode=effective_reference_mode(config["reference_mode"], cloud_policy=cloud_policy),
        sample_windows=int(config["sample_windows"]),
        dimension_states=normalize_dimension_states(config["dimension_states"]),
        draft_mode=draft_mode,
    )


def style_policy_for_scene(
    session: Any,
    scene: Any,
    bundle_snapshot: Mapping[str, Any] | None = None,
    *,
    task_type: str = "scene_generation",
) -> StylePolicy:
    """手里没有 bundle 外壳、但要判这一场的管线节点（成稿门、各道抄袭门）：给定的 bundle 快照 → 场景当前
    ``SceneRunState.current_bundle_id`` 的冻结快照 → 旧 bundle（契约前的快照）/ 没有 bundle 时按当前活动绑定
    轻量现解析（``freeze_contract=False``）。bundle 冻结了「无绑定」（absent）或契约损坏（degraded）时不回退
    到现解析——不一致的新 bundle 不是读今天绑定的许可。"""
    policy: StylePolicy | None = None
    if isinstance(bundle_snapshot, Mapping):
        policy = style_policy_for_bundle(bundle_snapshot, task_type=task_type)
    if (policy is None or policy.mode == MODE_NONE) and scene is not None:
        scene_id = getattr(scene, "scene_id", None)
        if scene_id:
            try:
                from novel_system.db.models import SceneBundle, SceneRunState

                state = session.get(SceneRunState, scene_id)
                bundle_id = getattr(state, "current_bundle_id", None) if state is not None else None
                row = session.get(SceneBundle, bundle_id) if bundle_id else None
                if row is not None and row.scene_id == scene_id:
                    policy = style_policy_for_bundle(row.frozen_snapshot_json, task_type=task_type)
            except Exception as exc:  # noqa: BLE001 — 读不到当前 bundle：按现解析处理
                logger.warning("scene bundle lookup for style policy failed: %s", exc)
    if policy is None or policy.mode == MODE_NONE:
        policy = style_policy_live(session, scene, task_type=task_type, freeze_contract=False)
    return policy


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
    "PROFILE_NOT_ACTIVE_CODE",
    "StylePolicy",
    "UNBOUND",
    "policy_from_contract",
    "reset_style_policy_cache",
    "style_policy_for_bundle",
    "style_policy_for_scene",
    "style_policy_live",
]
