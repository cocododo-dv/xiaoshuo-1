"""叙事事件的分类表（叶子：不 import ``novel_system`` 的任何东西）。

事件类型：
  character_state    — 身体 / 情绪状态变化
  character_learns   — 角色获知某件事
  location_change    — 角色移动
  relation_change    — 关系变化
  item_change        — 物品得失
  foreshadow_plant / foreshadow_reinforce / foreshadow_resolve — 伏笔埋设 / 加固 / 回收

实体类型：character, location, item, relation, foreshadow
"""
from __future__ import annotations

EVENT_TYPES = (
    "character_state",
    "character_learns",
    "location_change",
    "relation_change",
    "item_change",
    "foreshadow_plant",
    "foreshadow_reinforce",
    "foreshadow_resolve",
)

ENTITY_TYPES = ("character", "location", "item", "relation", "foreshadow")

# 信息差相关的事实键：POV 投影按知情范围过滤它们，其余事实在场即可观察（公共）。
INFORMATION_ASYMMETRY_FACT_KEYS = frozenset(
    {
        "secret_held_by",
        "believes_false",
        "revealed_to",
        "scene_revelation",
    }
)

# 信息差键里「内容」本身是秘密的两个：非持有者 POV 看不到正文，只拿到盲区提示。
SECRET_CONTENT_KEYS = ("secret_held_by", "believes_false")

# 硬事实检查认得的事实键（其余事实只进提示词，不做关键词矛盾检查）。
CHECKABLE_FACT_KEYS = frozenset(
    {
        "alive",
        "location",
        "physical_state",
        "has_item",
        "missing_limb",
        "appearance",
        "ability",
    }
)

# 归正史管理的事件来源：运行时重放只在它们带着有效的、与终稿哈希绑定的提交时才认。
CANON_MANAGED_SOURCE_KINDS = ("canon_candidate_accepted", "canon_acceptance", "facts_unchanged")

# 一场正史「核对完成」的提交种类：作者确认本场，或声明事实不变沿用上一版。
SCENE_COMPLETION_COMMIT_KINDS = ("author_verification", "facts_unchanged")


def entity_type_for_event(event_type: str) -> str:
    """事件类型对应的实体类型（作者手填候选没写实体类型、正文抽取出的事件都按这张表归类）。"""
    if event_type == "item_change":
        return "item"
    if event_type.startswith("foreshadow_"):
        return "foreshadow"
    if event_type == "relation_change":
        return "relation"
    return "character"
