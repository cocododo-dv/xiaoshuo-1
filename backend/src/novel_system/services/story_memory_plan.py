"""故事记忆用的「后面几章的计划」（只读）：章摘要对照它给线索标「计划在哪章兑现」，编辑通读对照它给后面几章写意见。

计划 = 目录章的章目标（作者规划过的才算，``story_slots.planned_chapter_goal``）与各活场的设计：章摘要只要每场一行
（场目标、挫折或决定、必须揭示什么），编辑通读要整段场景结构（``scene_structure_brief``，与起草看到的同一份）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.scene_form import scene_form, text, writer_brief
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.story_memory_digest import chapter_display_title
from novel_system.services.story_slots import planned_chapter_goal

# 章摘要的输入里最多带后面这么多章的计划
UPCOMING_DIGEST_CHAPTERS = 12
_SCENE_LINE_MAX_CHARS = 160


def chapter_scenes(session: Session, chapter_id: str) -> list[SceneCard]:
    """一章的活场，按章内顺序。"""
    return list(
        session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars()
    )


def scene_outline(scene: SceneCard, chapter: ChapterGoal) -> str:
    """一场的计划一行：场目标；挫折（主动场）或决定（反应场）；必须揭示什么（与前面重复的不写）。没规划 → 空串。"""
    brief = writer_brief(scene)
    parts: list[str] = []
    goal = text(planned_chapter_goal(scene.scene_goal, chapter))
    if goal:
        parts.append(goal)
    if scene_form(scene) == "reactive":
        decision = text(brief.get("decision"))
        if decision:
            parts.append(f"决定：{decision}")
    else:
        setback = text(brief.get("setback"))
        if setback:
            parts.append(f"挫折：{setback}")
    reveal = text(brief.get("must_reveal"))
    if reveal and not any(reveal in part for part in parts):
        parts.append(f"揭示：{reveal}")
    line = "；".join(parts)
    return line if len(line) <= _SCENE_LINE_MAX_CHARS else line[:_SCENE_LINE_MAX_CHARS].rstrip() + "…"


def upcoming_plan(
    session: Session,
    chapters: list[ChapterGoal],
    start_index: int,
    *,
    max_chapters: int = UPCOMING_DIGEST_CHAPTERS,
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """``chapters[start_index:]`` 里最多 ``max_chapters`` 章的计划（一场一行）；什么都没规划的章不列。另给章号 → 章 id。"""
    rows: list[dict[str, Any]] = []
    numbers: dict[int, str] = {}
    for index in range(max(0, start_index), min(len(chapters), max(0, start_index) + max_chapters)):
        chapter = chapters[index]
        goal = text(planned_chapter_goal(chapter.chapter_goal, chapter))
        scenes = [line for line in (scene_outline(scene, chapter) for scene in chapter_scenes(session, chapter.chapter_id)) if line]
        if not goal and not scenes:
            continue
        rows.append(
            {"chapter_number": index + 1, "title": chapter_display_title(chapter), "goal": goal, "scenes": scenes}
        )
        numbers[index + 1] = chapter.chapter_id
    return rows, numbers


def window_plan(session: Session, chapters: list[ChapterGoal], indexes: list[int]) -> list[dict[str, Any]]:
    """编辑通读针对的后面几章：章目标与每场的整段场景结构（起草看到的同一份；没有结构的场退回一行计划）。"""
    rows: list[dict[str, Any]] = []
    for index in indexes:
        chapter = chapters[index]
        scenes: list[str] = []
        for scene in chapter_scenes(session, chapter.chapter_id):
            brief = render_scene_structure_brief(scene, session) or scene_outline(scene, chapter)
            if brief:
                scenes.append(brief)
        rows.append(
            {
                "chapter_number": index + 1,
                "title": chapter_display_title(chapter),
                "goal": text(planned_chapter_goal(chapter.chapter_goal, chapter)),
                "scenes": scenes,
            }
        )
    return rows
