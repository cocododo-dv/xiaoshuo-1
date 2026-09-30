"""雪花草稿的三种合并：各有各的用处，名字把用处说清楚（B06-17）。

- :func:`merge_default` —— 步骤默认稿 + 存档 / 请求里的草稿：嵌套字典逐层覆盖，其余值（含列表）整个替换。
  每一步草稿的形状都从这里来（``merge_step_draft``）。
- :func:`overlay_keeping_members` —— ``draft_override``（前端刚编辑、还没自动保存上行的内容）盖在存档上：
  集合按成员身份对位合并，override 没带的成员留在尾部。它是叠加，不是删除指令——删除走 PATCH 保存，那里整表替换。
- :func:`apply_member_patch` —— 模型产出的补丁并回底稿：角色按 ``character_id``、场景按 ``scene_id`` 就地合并，
  保持底稿的成员顺序（作者的语序），新成员追加在尾部；没有身份键的列表整个替换。

2026-09-30 从 ``snowflake_steps`` / ``snowflake_workspace`` / ``snowflake_workspace_llm`` 三处收拢到这里；
旧名字（``_merge_dicts`` / ``_merge_dicts_keeping_members`` / ``_merge_member_lists`` / ``_merge_patch``）在原模块照旧可用。
叶子模块。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from novel_system.services.hash_engine import normalize

#: overlay 合并时集合成员的身份键：按它们对位合并，而不是整表替换。
OVERLAY_ID_KEYS: dict[str, tuple[str, ...]] = {
    "scenes": ("scene_id", "row_uid"),
    "characters": ("character_id", "display_name"),
    # 章表同理：前端少带几章不能把存档里的章整片抹掉，那会连带解绑全书的场景归属。
    "chapters": ("row_uid",),
}


def merge_default(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """默认稿 + 草稿：嵌套字典逐层覆盖，其余值整个替换（深拷贝，不改两边的原件）。"""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_default(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def overlay_keeping_members(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``draft_override`` 专用合并：集合按成员 id 对位，不整表替换。

    draft_override 的语义是「补上作者刚编辑、还没自动保存上行的内容」，是叠加，不是删除指令——
    真正的删除走 PATCH 保存，那里仍然整表替换。朴素的整表替换会让前端少带几个成员就把存档里的成员
    整片抹掉：真实故障里一部作品的场景规划就这样从 12 场无声掉回 5 场，而且发生在调 LLM 之前。
    """
    merged = deepcopy(base)
    for key, value in override.items():
        id_keys = OVERLAY_ID_KEYS.get(str(key))
        if id_keys and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = merge_member_lists(merged[key], value, id_keys=id_keys)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = overlay_keeping_members(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_member_lists(
    base_items: list[Any],
    override_items: list[Any],
    *,
    id_keys: tuple[str, ...],
) -> list[Any]:
    """按成员 id 对位合并两份集合，保持 override 的顺序，base 独有的成员追加在尾部。

    身份匹配对 id_keys 里**任一**键取交集：同一场景在 base 里带 scene_id、在 override 里
    只带 row_uid（前端刚编辑、后端 id 还没回填的成员，两边键不同）也能对上，不会被当成两个
    成员重复留下。只有当两份成员完全没有共享任一 id 值时才视为不同成员——那本就是不同成员。
    """

    def id_values(item: Any) -> list[str]:
        if not isinstance(item, dict):
            return []
        values: list[str] = []
        for id_key in id_keys:
            value = str(item.get(id_key) or "").strip()
            if value:
                values.append(f"{id_key}:{value}")
        return values

    # 一个 base 成员按它携带的每个 id 值都建索引，这样 override 用其中任一键都能命中同一成员。
    base_by_id_value: dict[str, Any] = {}
    for item in base_items:
        for value in id_values(item):
            base_by_id_value.setdefault(value, item)

    merged: list[Any] = []
    matched_base: set[int] = set()
    for item in override_items:
        existing = None
        for value in id_values(item):
            if value in base_by_id_value:
                existing = base_by_id_value[value]
                break
        if isinstance(existing, dict) and isinstance(item, dict):
            merged.append(overlay_keeping_members(existing, item))
            matched_base.add(id(existing))
        else:
            merged.append(deepcopy(item))
    # 前端这次没带上来的成员不代表作者删了它——留在尾部，等真正的 PATCH 来决定去留。
    merged.extend(
        deepcopy(item)
        for item in base_items
        if id_values(item) and id(item) not in matched_base
    )
    return merged


def apply_member_patch(base: Any, patch: Any, *, collection_key: str | None = None) -> Any:
    """模型补丁并回底稿：字典逐键合并；角色 / 场景集合按身份键就地合并、底稿顺序不变、新成员追加在尾部。"""
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(normalize(base))
        for key, value in patch.items():
            merged[key] = apply_member_patch(merged.get(key), value, collection_key=key)
        return merged
    if isinstance(base, list) and isinstance(patch, list):
        id_key = patch_id_key(collection_key)
        if not id_key:
            return normalize(patch)
        base_items = [normalize(item) for item in base if isinstance(item, dict)]
        patch_items = [normalize(item) for item in patch if isinstance(item, dict)]
        patch_by_id = {
            str(item.get(id_key) or ""): item
            for item in patch_items
            if str(item.get(id_key) or "")
        }
        # 保持底稿成员顺序（名册/场景序即作者语序）：patch 命中的按 id 就地合并，
        # 模型新增的成员追加在尾部——定向补全不打乱其余成员的排列。
        merged_items = []
        seen_ids: set[str] = set()
        for item in base_items:
            item_id = str(item.get(id_key) or "")
            if not item_id:
                continue
            merged_items.append(apply_member_patch(item, patch_by_id[item_id]) if item_id in patch_by_id else item)
            seen_ids.add(item_id)
        for item in patch_items:
            item_id = str(item.get(id_key) or "")
            if item_id and item_id not in seen_ids:
                merged_items.append(apply_member_patch({}, item))
                seen_ids.add(item_id)
        return merged_items
    return normalize(patch)


def patch_id_key(collection_key: str | None) -> str | None:
    """模型补丁里集合成员的身份键（角色 ``character_id``、场景 ``scene_id``；其余集合没有，整表替换）。"""
    if collection_key == "characters":
        return "character_id"
    if collection_key == "scenes":
        return "scene_id"
    return None
