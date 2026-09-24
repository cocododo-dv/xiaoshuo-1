"""风格参考 v3 — 绑定解析（从 ``InjectionService`` 搬出，叶子模块：只依赖 ORM）。

优先级单点 :data:`SCOPE_RANK`（``runtime_contract.contract_layer`` 与 ``style_policy`` 的轻量现解析都引用它，
不各写一份）：scene（0）> character（1，POV 在前、其余台上人物按出场顺序）> project（2）> global（3）；同级取
最新创建的一条。v3 起**只冻结最具体的一层**（:func:`most_specific_binding`）——旧的多层合并按层序把样例 /
声音取自「最后一层」，而角色层是按 POV 优先排的，最后一层恰恰是最不重要的配角（J7）。

``resolve_binding_layers`` 仍返回由泛到具体的全部命中层：bundle 需要它们的 profile id 做来源登记，
``/injection/layers`` 要把「哪几层命中、哪一层生效」列给作者看；渲染与契约只用最具体的一层。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceWindow,
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
    """scene=0 > character=1 > project=2 > global=3；不匹配 99。"""
    scope = getattr(binding, "scope", None)
    ref = getattr(binding, "scope_ref_id", None)
    if scene_id and scope == "scene" and ref == scene_id:
        return 0
    if character_ids and scope == "character" and ref in character_ids:
        return 1
    if project_id and scope == "project" and ref == project_id:
        return 2
    if scope == "global":
        return 3
    return _UNMATCHED


def _char_order(binding: Any, character_ids: Sequence[str] | None) -> int:
    if getattr(binding, "scope", None) == "character" and character_ids:
        ref = getattr(binding, "scope_ref_id", None)
        if ref in character_ids:
            return list(character_ids).index(ref)
    return 0


def active_bindings(session: Session, task_type: str) -> list[StyleReferenceInjectionBinding]:
    """``status=active`` 且画像也 ``active`` 的绑定（画像状态只查一列，不加载整份 profile_json）。"""
    bindings = list(
        session.scalars(
            select(StyleReferenceInjectionBinding).where(
                StyleReferenceInjectionBinding.task_type == str(task_type),
                StyleReferenceInjectionBinding.status == "active",
            )
        ).all()
    )
    if not bindings:
        return []
    profile_ids = sorted({str(b.profile_id) for b in bindings})
    active_profiles = {
        str(pid)
        for pid, status in session.execute(
            select(StyleReferenceProfile.profile_id, StyleReferenceProfile.status).where(
                StyleReferenceProfile.profile_id.in_(profile_ids)
            )
        )
        if status == "active"
    }
    return [b for b in bindings if str(b.profile_id) in active_profiles]


def _sort_key(binding: Any, *, rank: int, character_ids: Sequence[str] | None) -> tuple[int, int, int]:
    return (rank, _char_order(binding, character_ids), -ts_to_int(getattr(binding, "created_at", None)))


def resolve_active_binding(
    session: Session,
    project_id: str | None,
    task_type: str,
    *,
    character_ids: Sequence[str] | None = None,
    scene_id: str | None = None,
) -> StyleReferenceInjectionBinding | None:
    """最具体的一条活动绑定（scene > POV 角色 > 其余台上角色 > project > global，同级取最新）。"""
    if not project_id and not character_ids and not scene_id:
        return None
    candidates: list[tuple[tuple[int, int, int], Any]] = []
    for binding in active_bindings(session, task_type):
        rank = binding_rank(binding, project_id=project_id, character_ids=character_ids, scene_id=scene_id)
        if rank < _UNMATCHED:
            candidates.append((_sort_key(binding, rank=rank, character_ids=character_ids), binding))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


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
    if not project_id and not character_ids and not scene_id:
        return []
    ranked: list[tuple[int, Any]] = []
    for binding in active_bindings(session, task_type):
        rank = binding_rank(binding, project_id=project_id, character_ids=character_ids, scene_id=scene_id)
        if rank < _UNMATCHED:
            ranked.append((rank, binding))
    if not ranked:
        return []

    def _best(allowed: set[int]) -> Any | None:
        pool = [(rank, b) for rank, b in ranked if rank in allowed]
        if not pool:
            return None
        pool.sort(key=lambda item: _sort_key(item[1], rank=item[0], character_ids=character_ids))
        return pool[0][1]

    characters = sorted(
        (b for rank, b in ranked if rank == 1),
        key=lambda b: (_char_order(b, character_ids), -ts_to_int(getattr(b, "created_at", None))),
    )
    seen: set[str] = set()
    character_layers = []
    for binding in characters:
        ref = str(binding.scope_ref_id)
        if ref not in seen:
            seen.add(ref)
            character_layers.append(binding)
    layers: list[Any] = []
    base = _best({2, 3})
    if base is not None:
        layers.append(base)
    layers.extend(character_layers)
    scene_binding = _best({0})
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


def describe_binding_layers(
    session: Session,
    project_id: str | None,
    task_type: str,
    *,
    character_ids: Sequence[str] | None = None,
    scene_id: str | None = None,
) -> dict[str, Any]:
    """只读：命中了哪几层、哪一层生效（U10：不再为了列出 profile id 全量渲染 12 窗）。

    返回 ``{layers, merged, budget_total, deduplicated}``（旧形状保留，前端「叠加层」页签直接可读）；每层带
    ``applied``（v3 只有最具体的一层生效）、画像标题与状态（只查列），生效层带绑定的 v3 配置与按窗口索引估算的
    样例字数（``estimated_sample_chars`` = 本书窗口平均字数 × 样例窗数；样例是提示的主体）。
    """
    from novel_system.services.style_reference.binding_config import (
        effective_reference_mode,
        normalize_binding_config,
        sends_samples,
    )

    layers = resolve_binding_layers(
        session, project_id, task_type, character_ids=character_ids, scene_id=scene_id
    )
    if not layers:
        return {"layers": [], "merged": None, "budget_total": 0, "deduplicated": []}
    applied = most_specific_binding(layers)
    profile_ids = sorted({str(b.profile_id) for b in layers})
    profile_rows = {
        str(pid): (title, status, book_id)
        for pid, title, status, book_id in session.execute(
            select(
                StyleReferenceProfile.profile_id,
                StyleReferenceProfile.title,
                StyleReferenceProfile.status,
                StyleReferenceProfile.book_id,
            ).where(StyleReferenceProfile.profile_id.in_(profile_ids))
        )
    }
    out_layers: list[dict[str, Any]] = []
    applied_summary: dict[str, Any] | None = None
    for binding in layers:
        title, status, book_id = profile_rows.get(str(binding.profile_id), (None, None, None))
        config = normalize_binding_config(binding.config_json or {})
        is_applied = binding is applied
        entry: dict[str, Any] = {
            "rank": SCOPE_RANK.get(str(binding.scope), 9),
            "scope": binding.scope,
            "scope_ref_id": binding.scope_ref_id,
            "binding_id": binding.binding_id,
            "profile_id": binding.profile_id,
            "profile_title": title,
            "profile_status": status,
            "strategy": binding.strategy,
            "reference_mode": config["reference_mode"],
            "sample_windows": config["sample_windows"],
            "applied": is_applied,
            # 旧字段：v3 不再按层分配预算——生效层权重 1，其余 0
            "weight": 1 if is_applied else 0,
            "budget_chars": 0,
            "block_chars": {},
            "fragment_count": 0,
        }
        if is_applied:
            cloud_policy = None
            window_chars = 0.0
            if book_id:
                cloud_policy = session.scalar(
                    select(StyleReferenceBook.cloud_policy).where(StyleReferenceBook.book_id == str(book_id))
                )
                window_chars = float(
                    session.scalar(
                        select(func.avg(StyleReferenceWindow.chars)).where(
                            StyleReferenceWindow.book_id == str(book_id)
                        )
                    )
                    or 0.0
                )
            mode = effective_reference_mode(config["reference_mode"], cloud_policy=cloud_policy)
            estimated = int(round(window_chars * int(config["sample_windows"]))) if sends_samples(mode) else 0
            entry["reference_mode"] = mode
            entry["estimated_sample_chars"] = estimated
            entry["budget_chars"] = estimated
            applied_summary = {
                "layer_count": 1,
                "strategy": binding.strategy,
                "reference_mode": mode,
                "sample_windows": config["sample_windows"],
                "dimension_states": config["dimension_states"],
                "binding_id": binding.binding_id,
                "profile_id": binding.profile_id,
                "prefix_chars": estimated,
                "estimated": True,
            }
        out_layers.append(entry)
    shadowed = [
        {
            "binding_id": b.binding_id,
            "profile_id": b.profile_id,
            "scope": b.scope,
            "scope_ref_id": b.scope_ref_id,
            "rank": SCOPE_RANK.get(str(b.scope), 9),
        }
        for b in layers
        if b is not applied
    ]
    return {
        "layers": out_layers,
        "merged": applied_summary,
        "budget_total": int((applied_summary or {}).get("prefix_chars") or 0),
        # v3：没有合并，其余命中层都被最具体的一层遮住
        "deduplicated": shadowed,
    }


__all__ = [
    "SCOPE_RANK",
    "active_bindings",
    "binding_rank",
    "describe_binding_layers",
    "most_specific_binding",
    "ordered_character_ids",
    "resolve_active_binding",
    "resolve_binding_layers",
    "ts_to_int",
]
