"""手建的空白占位章（阶段 X，2026-09-19）——叶子模块，只依赖 ORM 模型与 ``chapter_approval``。

作者常常在雪花做完之前先进过一次写作台，点了「创建第一章 · 开场」——目录里于是躺着一章
「第 1 章 / 开场」，一个字没写。雪花的章物化进来只能排在它后面（不挪动计划之外的章）：
雪花的「第 1 章」显示成第 02 章、目录里两章同名，写作台默认落在那张空白占位场上，
雪花整理出来的章像是挂在书的外面（真实故障）。

物化（``projects.approve_outline_plan``）在落位之前把这样的章移入回收站；分章面板
（``snowflake_chaptering``）用同一条判定提前告诉作者。两边都引用这里，所以它必须是叶子。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    LlmCall,
    OperationLog,
    SceneCard,
    SceneRunState,
    utcnow,
)
from novel_system.services.chapter_approval import is_chapter_approved

#: 「雪花整理进目录时，被系统移走的手建空白占位章」的标记（``trashed_by``）
AUTO_TRASHED_PLACEHOLDER_CHAPTER = "snowflake_placeholder"
_AUTO_CHAPTER_TITLE = re.compile(r"^第\s*\d+\s*章$")
_PLACEHOLDER_SCENE_TITLES = frozenset({"开场", "新场景"})
_PLACEHOLDER_SCENE_GOALS = frozenset({"", "（本场目标待规划）"})
#: 作者稿里由系统生成的脚手架行（旧版 ensure 会把场景卡抄成这样的正文）
_DRAFT_SCAFFOLD_LINE = re.compile(r"^【(章节目标|场景目标|地点|节拍|结尾变化|读者钩子)】")


def _draft_is_untouched(draft: AuthorDraft) -> bool:
    """作者稿是不是一个字都没动过：第 1 版、从没提升过，内容为空或只有系统脚手架行。"""
    if int(draft.revision_no or 1) > 1 or draft.last_promoted_revision_no is not None:
        return False
    text = re.sub(r"<[^>]+>", "\n", str(draft.content or ""))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return all(_DRAFT_SCAFFOLD_LINE.match(line) for line in lines)


def _placeholder_scene_is_pristine(session: Session, chapter: ChapterGoal, scene: SceneCard) -> bool:
    brief = dict(scene.writer_brief_json or {})
    if str(brief.get("source") or "") != "catalog_api":
        return False
    title = str(brief.get("title") or scene.scene_goal or "").strip()
    if title not in _PLACEHOLDER_SCENE_TITLES:
        return False
    chapter_name = str((chapter.narrative_json or {}).get("title") or chapter.chapter_goal or "").strip()
    goal = str(brief.get("goal") or "").strip()
    if goal not in _PLACEHOLDER_SCENE_GOALS and goal != chapter_name:
        return False
    if any(str(brief.get(key) or "").strip() for key in ("conflict", "setback", "reaction", "dilemma", "decision")):
        return False
    if int(scene.words_current or 0) > 0 or str(scene.author_notes or "").strip():
        return False
    state = session.get(SceneRunState, scene.scene_id)
    if state is not None and (
        str(state.scene_status or "ready") != "ready" or state.current_final_scene_row_id or state.current_bundle_id
    ):
        return False
    for model in (FinalScene, AttemptTracker, LlmCall):
        if session.execute(select(model).where(model.scene_id == scene.scene_id).limit(1)).first() is not None:
            return False
    drafts = session.execute(
        select(AuthorDraft).where(AuthorDraft.object_type == "scene", AuthorDraft.object_id == scene.scene_id)
    ).scalars().all()
    return all(_draft_is_untouched(draft) for draft in drafts)


def pristine_placeholder_chapters(
    session: Session, project_id: str, *, keep_chapter_ids: set[str] | None = None
) -> list[tuple[ChapterGoal, list[SceneCard]]]:
    """目录里的空白占位章（连同章下的卡）。判定见 :func:`trash_pristine_placeholder_chapters`。"""
    keep = keep_chapter_ids or set()
    found: list[tuple[ChapterGoal, list[SceneCard]]] = []
    chapters = session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
    ).scalars().all()
    for chapter in chapters:
        if chapter.chapter_id in keep:
            continue
        if str((chapter.writer_brief_json or {}).get("source") or "") != "catalog_api":
            continue
        title = str((chapter.narrative_json or {}).get("title") or chapter.chapter_goal or "").strip()
        if not _AUTO_CHAPTER_TITLE.match(title) or is_chapter_approved(session, chapter):
            continue
        narrative = dict(chapter.narrative_json or {})
        if any(str(narrative.get(key) or "").strip() for key in ("pov", "time_label", "place", "entry", "exit", "promise")):
            continue
        if any(str(value or "").strip() for value in dict(narrative.get("drama") or {}).values()) or narrative.get("threads"):
            continue
        cards = list(session.execute(select(SceneCard).where(SceneCard.chapter_id == chapter.chapter_id)).scalars())
        if any(int(card.trashed_flag or 0) == 1 for card in cards):
            continue
        if not all(_placeholder_scene_is_pristine(session, chapter, card) for card in cards):
            continue
        found.append((chapter, cards))
    return found


def trash_pristine_placeholder_chapters(
    session: Session,
    project_id: str,
    *,
    keep_chapter_ids: set[str],
    actor_ref: str = AUTO_TRASHED_PLACEHOLDER_CHAPTER,
) -> list[dict[str, Any]]:
    """雪花整理进目录之前，把**手建的空白占位章**移入回收站（可恢复），返回被移走的章。

    只动同时满足这几条的章：目录 API 手建、章名还是系统起的「第 N 章」、不在这一版分章里、没有终审通过、
    章下每一张卡都是没动过的占位场（系统题名、三拍没填、零字、没有笔记、管线没跑过、没有定稿 / LLM 调用，
    作者稿为空或只有系统脚手架）、回收站里没有它的卡（删过场 = 作者在这一章里干过活）。
    作者起过名、写过一个字、填过一拍的章原样保留。
    """
    trashed: list[dict[str, Any]] = []
    now = utcnow()
    for chapter, cards in pristine_placeholder_chapters(session, project_id, keep_chapter_ids=keep_chapter_ids):
        title = str((chapter.narrative_json or {}).get("title") or chapter.chapter_goal or "").strip()
        chapter.trashed_flag = 1
        chapter.trashed_at = now
        chapter.trashed_by = actor_ref
        for card in cards:
            # 与章同一个时间戳进回收站：恢复这一章时，卡跟着回来（trash.py 按 trashed_at 配对）
            card.trashed_flag = 1
            card.trashed_at = now
            card.trashed_by = actor_ref
        trashed.append({"chapter_id": chapter.chapter_id, "title": title})
        session.add(
            OperationLog(
                event_type="snowflake_placeholder_chapter_trashed",
                object_type="chapter_goal",
                object_ref=chapter.chapter_id,
                payload_json={
                    "project_id": project_id,
                    "title": title,
                    "trashed_at": now,
                    "scene_count": len(cards),
                    "reason": "pristine_placeholder_before_snowflake_chapters",
                },
            )
        )
    if trashed:
        session.flush()
    return trashed
