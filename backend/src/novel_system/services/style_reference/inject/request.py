"""风格参考 v3（2026-09-23）— 一次渲染的请求（取代 ``InjectionService`` 的改属性传参，J16）。

过去调用方先 ``svc.few_shot_seed = …``、``svc.scene_position = …``、``svc.few_shot_k_cap = …``、
``svc.drift_ptype_priority = …``、``svc.context_text = …`` 再调渲染，漏设一个就悄悄变成别的行为，评审 /
规划节点还拿到起草口径的标题。现在所有输入都在这一个冻结的数据类里：

- ``role``：``draft`` 起草 / ``revise`` 改稿 / ``review`` 评审 / ``plan`` 规划——决定各块的标题口径、样例落点
  与窗数（评审取冻结选窗的前 4 窗、规划前 3 窗，:data:`ROLE_K_CAPS`）；
- ``placement``：``system`` 整块进 system 提示 / ``user_tail`` 样例进 user 消息末尾（起草 / 改稿）；
  评审与规划一律 ``system``；
- ``k_cap``：调用方再压的窗数上限（``None`` 不压）；
- ``scene_id`` / ``bundle_id``：选窗的种子与冻结键；
- ``position`` / ``situation_tags`` / ``dialogue_heavy`` / ``rendering_mode``：按本场设计挑样例的输入
  （不看任何草稿）；
- ``revise_dimensions``：定向修改要改的维（改稿可把至多 2 窗换成示范这些维手法的窗）；
- ``recent_gaps``：近期常见偏差的白话短语（文风卡末尾补充强调，N7）；
- ``node_ids``：接收这份提示的模型节点（H1：书的云策略按这些节点的实际路由判；模板可能在几个节点下派发时
  全部列上，每一个都要满足；空 = 说不出，「仅本机」的书按不许送处理）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from novel_system.services.style_reference.policy import normalize_node_ids
from novel_system.services.style_reference.tags import normalize_situation_tags

ROLE_DRAFT = "draft"
ROLE_REVISE = "revise"
ROLE_REVIEW = "review"
ROLE_PLAN = "plan"
ROLES: tuple[str, ...] = (ROLE_DRAFT, ROLE_REVISE, ROLE_REVIEW, ROLE_PLAN)

PLACEMENT_SYSTEM = "system"
PLACEMENT_USER_TAIL = "user_tail"
PLACEMENTS: tuple[str, ...] = (PLACEMENT_SYSTEM, PLACEMENT_USER_TAIL)

POSITION_OPENING = "opening"
POSITION_CLOSING = "closing"
POSITION_WHOLE = "whole"
SCENE_POSITIONS: tuple[str, ...] = (POSITION_OPENING, POSITION_CLOSING, POSITION_WHOLE)

# 评审 / 规划节点只看冻结选窗的前几窗（选窗顺序里位置 / 场面匹配的窗排在最前）——L4
REVIEW_K = 4
PLAN_K = 3
ROLE_K_CAPS: dict[str, int] = {ROLE_REVIEW: REVIEW_K, ROLE_PLAN: PLAN_K}
MAX_RECENT_GAPS = 3
RECENT_GAP_MAX_CHARS = 60


def _texts(values: Iterable[Any] | None, *, limit: int, max_chars: int | None = None) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = " ".join(str(value or "").split())
        if max_chars is not None:
            text = text[:max_chars]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


@dataclass(frozen=True)
class StyleRenderRequest:
    role: str = ROLE_DRAFT
    placement: str = PLACEMENT_SYSTEM
    k_cap: int | None = None
    scene_id: str | None = None
    bundle_id: str | None = None
    position: str | None = None
    situation_tags: tuple[str, ...] = field(default_factory=tuple)
    dialogue_heavy: bool = False
    rendering_mode: str | None = None
    revise_dimensions: tuple[str, ...] = field(default_factory=tuple)
    recent_gaps: tuple[str, ...] = field(default_factory=tuple)
    node_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        role = str(self.role or ROLE_DRAFT).strip().lower()
        if role not in ROLES:
            role = ROLE_DRAFT
        placement = str(self.placement or PLACEMENT_SYSTEM).strip().lower()
        if placement not in PLACEMENTS or role in (ROLE_REVIEW, ROLE_PLAN):
            # 评审 / 规划节点的样例留在 system 里（它们不写正文，没有「紧挨输出」的收口）
            placement = PLACEMENT_SYSTEM
        k_cap = None
        if self.k_cap is not None:
            try:
                k_cap = max(0, int(self.k_cap))
            except (TypeError, ValueError):
                k_cap = None
        position = str(self.position or "").strip().lower() or None
        if position not in SCENE_POSITIONS:
            position = None
        rendering_mode = str(self.rendering_mode or "").strip().lower() or None
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "placement", placement)
        object.__setattr__(self, "k_cap", k_cap)
        object.__setattr__(self, "scene_id", str(self.scene_id) if self.scene_id else None)
        object.__setattr__(self, "bundle_id", str(self.bundle_id) if self.bundle_id else None)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "situation_tags", tuple(normalize_situation_tags(list(self.situation_tags or ()))))
        object.__setattr__(self, "dialogue_heavy", bool(self.dialogue_heavy))
        object.__setattr__(self, "rendering_mode", rendering_mode)
        object.__setattr__(
            self,
            "revise_dimensions",
            tuple(dim for dim in _texts(self.revise_dimensions, limit=16) if dim in ALL_DIMENSIONS),
        )
        object.__setattr__(
            self, "recent_gaps", _texts(self.recent_gaps, limit=MAX_RECENT_GAPS, max_chars=RECENT_GAP_MAX_CHARS)
        )
        object.__setattr__(self, "node_ids", normalize_node_ids(self.node_ids))

    def effective_k(self, sample_windows: int) -> int:
        """这一次渲染实际送几窗：绑定的样例窗数 → 角色上限（评审 4 / 规划 3）→ 调用方上限。"""
        try:
            k = max(0, int(sample_windows))
        except (TypeError, ValueError):
            k = 0
        role_cap = ROLE_K_CAPS.get(self.role)
        if role_cap is not None:
            k = min(k, role_cap)
        if self.k_cap is not None:
            k = min(k, self.k_cap)
        return k

    def audit(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "placement": self.placement,
            "k_cap": self.k_cap,
            "scene_id": self.scene_id,
            "bundle_id": self.bundle_id,
            "position": self.position,
            "situation_tags": list(self.situation_tags),
            "dialogue_heavy": self.dialogue_heavy,
            "rendering_mode": self.rendering_mode,
            "revise_dimensions": list(self.revise_dimensions),
            "recent_gap_count": len(self.recent_gaps),
            "node_ids": list(self.node_ids),
        }


def infer_role(placement: str | None, few_shot_k_cap: int | None) -> str:
    """调用方没说角色时按旧参数推断：样例进 user 尾部 → 起草；窗数上限 ≤3 → 规划；=4 → 评审；其余 → 起草。"""
    if str(placement or "") == PLACEMENT_USER_TAIL:
        return ROLE_DRAFT
    if few_shot_k_cap is not None:
        try:
            cap = int(few_shot_k_cap)
        except (TypeError, ValueError):
            return ROLE_DRAFT
        if cap <= PLAN_K:
            return ROLE_PLAN
        if cap == REVIEW_K:
            return ROLE_REVIEW
    return ROLE_DRAFT


__all__ = [
    "MAX_RECENT_GAPS",
    "PLACEMENTS",
    "PLACEMENT_SYSTEM",
    "PLACEMENT_USER_TAIL",
    "PLAN_K",
    "POSITION_CLOSING",
    "POSITION_OPENING",
    "POSITION_WHOLE",
    "REVIEW_K",
    "ROLES",
    "ROLE_DRAFT",
    "ROLE_K_CAPS",
    "ROLE_PLAN",
    "ROLE_REVIEW",
    "ROLE_REVISE",
    "SCENE_POSITIONS",
    "StyleRenderRequest",
    "infer_role",
]
