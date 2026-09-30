"""风格参考 v3 — 绑定解析（从 ``InjectionService`` 搬出，叶子模块：只依赖 ORM）。

优先级单点 :data:`SCOPE_RANK`（``runtime_contract.contract_layer`` 也引用它），排序单点 :func:`rank_bindings`
（冻结路径与 ``style_policy`` 的轻量现解析共用，不各写一份）：scene（0）> character（1，POV 在前、其余台上人物
按出场顺序）> project（2）> global（3）；同级取最新创建的一条。v3 起**只冻结最具体的一层**（:func:`most_specific_binding`）——旧的多层合并按层序把样例 /
声音取自「最后一层」，而角色层是按 POV 优先排的，最后一层恰恰是最不重要的配角（J7）。

``resolve_binding_layers`` 仍返回由泛到具体的全部命中层：bundle 需要它们的 profile id 做来源登记；渲染与契约只用
最具体的一层。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
)

SCOPE_RANK: dict[str, int] = {"scene": 0, "character": 1, "project": 2, "global": 3}
_UNMATCHED = 99


def ordered_character_ids(pov_id: Any, onstage_ids: Iterable[Any] | None) -> list[str]:
    """角色匹配集：POV 排首 + 台上人物去重（POV 可能不在台上名单里）。"""
    ordered: list[str] = []
    if pov_id:
        ordered.append(str(pov_id))
    for cid in onstage_ids or []:
        if cid and str(cid) not in ordered:
            ordered.append(str(cid))
    return ordered


def ts_to_int(ts: str | None) -> int:
    """ISO 时间串 → 可比 int（只用于排序，失败回 0；取到微秒，同秒创建的多条绑定也能确定地决平）。"""
    if not ts:
        return 0
    cleaned = "".join(ch for ch in str(ts) if ch.isdigit())
    if not cleaned:
        return 0
    try:
        return int(cleaned[:20].ljust(20, "0"))
    except ValueError:
        return 0


def binding_rank(
    binding: Any,
    *,
    project_id: str | None,
    character_ids: Sequence[str] | None,
    scene_id: str | None,
) -> int:
    """:data:`SCOPE_RANK`：scene=0 > character=1 > project=2 > global=3；不匹配 99。"""
    scope = getattr(binding, "scope", None)
    ref = getattr(binding, "scope_ref_id", None)
    if scene_id and scope == "scene" and ref == scene_id:
        return SCOPE_RANK["scene"]
    if character_ids and scope == "character" and ref in character_ids:
        return SCOPE_RANK["character"]
    if project_id and scope == "project" and ref == project_id:
        return SCOPE_RANK["project"]
    if scope == "global":
        return SCOPE_RANK["global"]
    return _UNMATCHED


def _char_order(binding: Any, character_ids: Sequence[str] | None) -> int:
    if getattr(binding, "scope", None) == "character" and character_ids:
        ref = getattr(binding, "scope_ref_id", None)
        if ref in character_ids:
            return list(character_ids).index(ref)
    return 0


@dataclass(frozen=True)
class RankedBinding:
    """命中作用域的一条活动绑定，带它的排序位置与所指画像的状态 / 书（只查列）。"""

    binding: StyleReferenceInjectionBinding
    rank: int
    profile_status: str
    book_id: str | None

    @property
    def usable(self) -> bool:
        """所指画像是 active 的——只有这样的绑定能生效（画像归档 / 草稿 / 已删的绑定不生效）。"""
        return self.profile_status == "active"


def _sort_key(binding: Any, *, rank: int, character_ids: Sequence[str] | None) -> tuple[int, int, int]:
    return (rank, _char_order(binding, character_ids), -ts_to_int(getattr(binding, "created_at", None)))


def rank_bindings(
    session: Session,
    project_id: str | None,
    task_type: str,
    *,
    character_ids: Sequence[str] | None = None,
    scene_id: str | None = None,
) -> list[RankedBinding]:
    """命中这个作用域的全部活动绑定（不论画像状态），按生效顺序排好：scene > 角色（POV 在前、其余台上人物按出场
    顺序）> project > global，同级取最新——第一条 ``usable`` 的就是生效的那条。

    冻结路径（:func:`resolve_active_binding` / :func:`resolve_binding_layers`）只看 ``usable`` 的；
    ``style_policy`` 的轻量现解析也用这一份：命中的绑定全部不 usable 时降级（C7），而不是当作未绑定。"""
    if not project_id and not character_ids and not scene_id:
        return []
    matched: list[tuple[int, StyleReferenceInjectionBinding]] = []
    for binding in session.scalars(
        select(StyleReferenceInjectionBinding).where(
            StyleReferenceInjectionBinding.task_type == str(task_type),
            StyleReferenceInjectionBinding.status == "active",
        )
    ).all():
        rank = binding_rank(binding, project_id=project_id, character_ids=character_ids, scene_id=scene_id)
        if rank < _UNMATCHED:
            matched.append((rank, binding))
    if not matched:
        return []
    profile_rows = {
        str(pid): (str(status or ""), str(book_id or "") or None)
        for pid, status, book_id in session.execute(
            select(
                StyleReferenceProfile.profile_id,
                StyleReferenceProfile.status,
                StyleReferenceProfile.book_id,
            ).where(StyleReferenceProfile.profile_id.in_(sorted({str(b.profile_id) for _rank, b in matched})))
        )
    }
    matched.sort(key=lambda item: _sort_key(item[1], rank=item[0], character_ids=character_ids))
    return [
        RankedBinding(
            binding=binding,
            rank=rank,
            profile_status=profile_rows.get(str(binding.profile_id), ("", None))[0],
            book_id=profile_rows.get(str(binding.profile_id), ("", None))[1],
        )
        for rank, binding in matched
    ]


def resolve_active_binding(
    session: Session,
    project_id: str | None,
    task_type: str,
    *,
    character_ids: Sequence[str] | None = None,
    scene_id: str | None = None,
) -> StyleReferenceInjectionBinding | None:
    """最具体的一条活动绑定（scene > POV 角色 > 其余台上角色 > project > global，同级取最新；画像须 active）。"""
    for ranked in rank_bindings(session, project_id, task_type, character_ids=character_ids, scene_id=scene_id):
        if ranked.usable:
            return ranked.binding
    return None


def resolve_binding_layers(
    session: Session,
    project_id: str | None,
    task_type: str,
    *,
    character_ids: Sequence[str] | None = None,
    scene_id: str | None = None,
) -> list[StyleReferenceInjectionBinding]:
    """由泛到具体的全部命中层：base（project > global，单）+ 台上角色（每角色一层，POV 在前）+ scene（单）。

    只用于登记来源与列给作者看；生效的是 :func:`most_specific_binding`。
    """
    # rank_bindings 已按生效顺序排好：每一档里第一条就是这一档最该生效的，角色档里 POV 在前、同一角色取最新
    ranked = [
        entry
        for entry in rank_bindings(session, project_id, task_type, character_ids=character_ids, scene_id=scene_id)
        if entry.usable
    ]
    if not ranked:
        return []

    def _best(*scopes: str) -> Any | None:
        allowed = {SCOPE_RANK[scope] for scope in scopes}
        return next((entry.binding for entry in ranked if entry.rank in allowed), None)

    seen: set[str] = set()
    character_layers = []
    for entry in ranked:
        ref = str(entry.binding.scope_ref_id)
        if entry.rank == SCOPE_RANK["character"] and ref not in seen:
            seen.add(ref)
            character_layers.append(entry.binding)
    layers: list[Any] = []
    base = _best("project", "global")
    if base is not None:
        layers.append(base)
    layers.extend(character_layers)
    scene_binding = _best("scene")
    if scene_binding is not None:
        layers.append(scene_binding)
    return layers


def most_specific_binding(layers: Sequence[Any]) -> Any | None:
    """一组命中层里真正生效的一层：scene > character（列表里越靠前越优先——POV 在前）> project > global。

    ``resolve_binding_layers`` 的顺序是「由泛到具体」，但角色层内部是 POV 优先，所以不能简单取最后一层。
    """
    best: tuple[tuple[int, int], Any] | None = None
    for index, binding in enumerate(layers or ()):
        rank = SCOPE_RANK.get(str(getattr(binding, "scope", "") or ""), _UNMATCHED)
        key = (rank, index)
        if best is None or key < best[0]:
            best = (key, binding)
    return best[1] if best is not None else None


__all__ = [
    "SCOPE_RANK",
    "RankedBinding",
    "binding_rank",
    "most_specific_binding",
    "ordered_character_ids",
    "rank_bindings",
    "resolve_active_binding",
    "resolve_binding_layers",
    "ts_to_int",
]
