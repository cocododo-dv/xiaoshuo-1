"""编辑意见进起草（2026-10-12）：作者采纳的编辑通读意见（``EditorialGuidance``，见 ``story_editorial``）作为
``editorial_guidance`` 段进那一章各场的起草 bundle 与场景蓝图的来源快照。

一条意见针对一章（``chapter_id``），或针对那份通读的整段后面几章（``chapter_id`` 为空、这一章在通读的 window 里）。
只有 ``accepted`` 的算；作者改过字的用作者的字。没有就不挂这一段，快照与哈希逐字不变。只读叶子（只依赖 ORM）：
``bundle_builder`` / ``scene_blueprint`` / ``context_budget`` 都引它。
"""

from __future__ import annotations

from collections.abc import MutableMapping, MutableSequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import EditorialGuidance, EditorialReview, SceneCard

EDITORIAL_GUIDANCE_SECTION_KEY = "editorial_guidance"
EDITORIAL_GUIDANCE_SECTION_LABEL = (
    "Editor Guidance (author-approved notes for this chapter from the editorial read-through — act on them where this "
    "scene gives the chance; never against the Scene Structure or the Scene Card)"
)


def accepted_guidance_for_chapter(session: Session, project_id: str, chapter_id: str) -> list[EditorialGuidance]:
    """这一章采纳了的编辑意见，按通读的先后与意见的次序。"""
    if not project_id or not chapter_id:
        return []
    rows = session.execute(
        select(EditorialGuidance, EditorialReview.window_chapter_ids_json)
        .join(EditorialReview, EditorialReview.review_id == EditorialGuidance.review_id)
        .where(EditorialGuidance.project_id == project_id, EditorialGuidance.status == "accepted")
        .order_by(EditorialReview.created_at.asc(), EditorialReview.review_id.asc(), EditorialGuidance.position.asc())
    ).all()
    return [
        guidance
        for guidance, window in rows
        if guidance.chapter_id == chapter_id
        or (guidance.chapter_id is None and isinstance(window, list) and chapter_id in window)
    ]


def guidance_text(rows: list[EditorialGuidance]) -> str:
    return "\n".join(f"- {(row.author_text or row.text).strip()}" for row in rows if (row.author_text or row.text).strip())


def attach_editorial_guidance(
    scene: SceneCard,
    session: Session,
    *,
    refs: MutableMapping[str, Any],
    injections: MutableSequence[dict[str, Any]],
    digests: MutableMapping[str, Any],
) -> None:
    """这一章有采纳的意见就挂上（来源记意见的 id，正文进 ``inline_digests``）；没有什么都不写。"""
    rows = accepted_guidance_for_chapter(session, str(scene.project_id or ""), str(scene.chapter_id or ""))
    text = guidance_text(rows)
    if not text:
        return
    refs[EDITORIAL_GUIDANCE_SECTION_KEY] = [row.guidance_id for row in rows]
    injections.append(
        {"slot": EDITORIAL_GUIDANCE_SECTION_KEY, "ref_id": scene.chapter_id, "digest_key": EDITORIAL_GUIDANCE_SECTION_KEY}
    )
    digests[EDITORIAL_GUIDANCE_SECTION_KEY] = text
