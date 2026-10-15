"""第 10 步（场景规划）的 AI 生成要知道的章：哪几场是一章、哪一场收章、这一章给读者什么、先别揭什么（2026-10-16 规划层收拢第 3 步）。

场景规划逐场写三拍，可章是读者一口气读完、在章末决定要不要翻页的单位：一章最后一场的挫折 / 决定与钩子就是整章的
结尾。以前第 10 步的模型只从上游场景列表里看得到每场的章名，不知道哪一场收章，也看不到作者写的章的说明
（``chapter_intent``：给读者什么 / 先别揭）。这里按故事序把场归进章，交给提示载荷的 ``chapter_context``
（``snowflake_prompt_budget.PROTECTED_KEYS``：作者意图，降载不削）。章的说明没写就不带——不替作者补。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SnowflakeChapterPlan
from novel_system.services.chapter_drama import chapter_intent
from novel_system.services.snowflake_scene_order import live_scene_plans_in_story_order

CHAPTER_CONTEXT_KEY = "chapter_context"
CHAPTER_CONTEXT_HOW_TO_USE = (
    "章是读者一口气读完、在章末决定要不要翻页的单位。chapters 按故事序列出这些场所在的章：scene_ids 是这一章的几场，"
    "closing_scene_id 是收章的那一场——它的挫败（主动场）/ 决定（反应场）与钩子就是整章的结尾，要扣得住读者，"
    "其余场的钩子只需接住下一场。reader_payoff 是作者写的「这一章给读者什么」：这一章的几场合起来要交付它。"
    "not_yet 是作者写的「先别揭」：任何一场都不要写出、也不要写到揭晓的程度。没有 reader_payoff / not_yet 的章是作者"
    "还没写，不要替作者补。"
)


def chapter_context_for_scene_planning(
    session: Session, project_id: str, scene_ids: Iterable[str] | None = None
) -> dict[str, Any] | None:
    """``scene_ids`` 给了就只列这些场所在的章（定向深化的一批），否则全书的章；一章都没有返回 ``None``。"""
    wanted = {str(item) for item in (scene_ids or []) if str(item or "").strip()}
    chapters = {
        row.chapter_plan_id: row
        for row in session.execute(
            select(SnowflakeChapterPlan).where(
                SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None)
            )
        ).scalars()
    }
    members: dict[str, list[str]] = {}
    for plan in live_scene_plans_in_story_order(session, project_id):
        if plan.chapter_plan_id in chapters and plan.scene_id:
            members.setdefault(plan.chapter_plan_id, []).append(str(plan.scene_id))
    items: list[dict[str, Any]] = []
    for chapter_plan_id, scene_list in members.items():
        if wanted and not wanted.intersection(scene_list):
            continue
        chapter = chapters[chapter_plan_id]
        item: dict[str, Any] = {
            "chapter": chapter.title or f"第 {int(chapter.chapter_seq or 1)} 章",
            "scene_ids": scene_list,
            "closing_scene_id": scene_list[-1],
        }
        catalog = session.get(ChapterGoal, chapter.catalog_chapter_id) if chapter.catalog_chapter_id else None
        if catalog is not None and not catalog.trashed_flag:
            intent = chapter_intent(catalog)
            if intent["promise"]:
                item["reader_payoff"] = intent["promise"]
            not_yet = [line.strip() for line in intent["forbidden"].splitlines() if line.strip()]
            if not_yet:
                item["not_yet"] = not_yet
        items.append(item)
    if not items:
        return None
    return {"chapters": items, "how_to_use": CHAPTER_CONTEXT_HOW_TO_USE}


__all__ = ["CHAPTER_CONTEXT_HOW_TO_USE", "CHAPTER_CONTEXT_KEY", "chapter_context_for_scene_planning"]
