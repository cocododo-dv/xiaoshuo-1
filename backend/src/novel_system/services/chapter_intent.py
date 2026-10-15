"""写章的说明：「这一章给读者什么 / 先别揭 / 备注」（2026-10-16 规划层收拢第 2 步）。

三格存在目录章的 ``narrative_json.drama``（``promise`` / ``forbidden`` / ``notes``，读法在 ``chapter_drama``）——确认写入
重新物化时目录章的 ``narrative_json`` 原样留着，拆章 / 并章按「还是不是同一章」认领，三格跟着章走，不需要新列。

两个写入口，各管各的章：

- 雪花整理出来的章：构思的分章面板（``snowflake_chaptering.save`` 收载荷里每章的 ``promise`` / ``forbidden_text`` /
  ``notes``；目录里还没有这一章时面板不让写，确认写入之后再写）；章节编排里只读。
- 手建的章：章节编排（``catalog.update_chapter`` 的 ``drama``）。

「给读者什么」与「先别揭」进章节蓝图：改了它们，这一章的章节蓝图让位（AI 写的下一次起草重做；作者写的留着，
提示「设计改过」——``scene_planning_staleness``）。以前改了戏剧卡、蓝图照旧，作者得自己记得去重新生成。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.chapter_drama import INTENT_KEYS, chapter_intent
from novel_system.services.scene_planning_staleness import supersede_scene_planning_artifacts

#: 进章节蓝图的两格：它们变了，章节蓝图就过时了
_BLUEPRINT_INTENT_KEYS = frozenset({"promise", "forbidden"})
CHAPTER_INTENT_CHANGED = "chapter_intent_changed"


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def write_chapter_intent(
    session: Session,
    chapter: ChapterGoal,
    values: Mapping[str, Any],
    *,
    operation: str,
) -> list[str]:
    """把给了的几格写进这一章（只认 :data:`chapter_drama.INTENT_KEYS`）；返回真的变了的键。批准过的章 409。"""
    current = chapter_intent(chapter)
    incoming = {key: _clean(values[key]) for key in INTENT_KEYS if key in values}
    changed = [key for key, value in incoming.items() if value != current.get(key, "")]
    if not changed:
        return []
    require_chapter_mutation_allowed(
        session, chapter, changed_fields=[f"drama.{key}" for key in changed], operation=operation
    )
    narrative = dict(chapter.narrative_json or {})
    drama = dict(narrative.get("drama") or {})
    drama.update({key: incoming[key] for key in changed})
    narrative["drama"] = drama
    chapter.narrative_json = narrative
    note_intent_changed(session, chapter.chapter_id, changed)
    return changed


def intent_changes(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """两份章的说明之间变了的键（章节编排整份替换 ``drama`` 时用）。"""
    return [key for key in INTENT_KEYS if (before.get(key) or "") != (after.get(key) or "")]


def note_intent_changed(session: Session, chapter_id: str, changed_keys: Iterable[str]) -> None:
    """进蓝图的那两格变了：这一章的章节蓝图让位（AI 写的作废、作者写的留着并记一条「设计改过」）。"""
    if _BLUEPRINT_INTENT_KEYS & set(changed_keys):
        supersede_scene_planning_artifacts(
            session, scene_ids=[], chapter_ids=[chapter_id], reason=CHAPTER_INTENT_CHANGED
        )


__all__ = ["CHAPTER_INTENT_CHANGED", "intent_changes", "note_intent_changed", "write_chapter_intent"]
