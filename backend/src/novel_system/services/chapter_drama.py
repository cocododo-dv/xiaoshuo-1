"""章节戏剧卡（``ChapterGoal.narrative_json.drama``）在场景运行里的读法。

戏剧卡归章节编排（不在构思里）：六格写法（核心承诺 / 章节问题 / 主线推进 / 人物变化 / 结尾余味 / 结尾效果，
叫法见 ``catalog_labels.DRAMA_SLOT_LABELS``）外加两条护栏（禁止包含 / 备注）。卡片以前说它会进起草、「禁止包含」
会拦，可场景运行从不读它（round 2 · B2-5）。现在：

- 填了的六格与「禁止包含」进场景运行现做的章节蓝图：``near_final_planning`` 规划快照里的 ``chapter_drama`` 段
  （渲染位在 ``context_budget.SECTION_SPECS``）；起草读的是蓝图。章节编排的「AI 生成蓝图」本来就读整张卡。
- 「禁止包含」的词与场景卡 ``forbidden_text`` 一起按字面查（``qc_constraints.literal_forbidden_sources``）。
- 备注是作者留给自己的话，不进场景运行。

一格都没写（或只有空白）就什么都不加：规划快照、哈希与提示词逐字与以前相同。
"""

from __future__ import annotations

import json
from typing import Any

from novel_system.services.catalog_labels import DRAMA_SLOT_LABELS

#: 规划快照里戏剧卡那一段的键（``inline_digests`` / 注入槽 / ``SECTION_SPECS`` 同名）
CHAPTER_DRAMA_SECTION_KEY = "chapter_drama"
#: 进章节蓝图的格子：六格写法 + 禁止包含（备注不进）
_BLUEPRINT_KEYS: tuple[str, ...] = (*DRAMA_SLOT_LABELS, "forbidden")


def chapter_drama(chapter: Any) -> dict[str, Any]:
    """这一章的戏剧卡（没有 / 形状不对就是空表）。"""
    narrative = getattr(chapter, "narrative_json", None) if chapter is not None else None
    drama = narrative.get("drama") if isinstance(narrative, dict) else None
    return drama if isinstance(drama, dict) else {}


def _slot_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def chapter_forbidden_text(chapter: Any) -> str:
    """戏剧卡「禁止包含」那一格（一行一条；空白算没写）。"""
    return _slot_text(chapter_drama(chapter).get("forbidden"))


def chapter_drama_digest(chapter: Any) -> str:
    """规划快照的 ``chapter_drama`` 段：填了的格子（键序固定的 JSON）；一格都没填是空串——调用方据此不加这一段。"""
    drama = chapter_drama(chapter)
    filled = {key: _slot_text(drama.get(key)) for key in _BLUEPRINT_KEYS if _slot_text(drama.get(key))}
    return json.dumps(filled, ensure_ascii=False, sort_keys=True) if filled else ""
