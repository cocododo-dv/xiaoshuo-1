"""bundle 的两个摘要来源：这一场、这一章最新的一份经过复核的记忆（B11-21：以前包在一个无状态的 ``Resolver`` 类里）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterMemory,
    SceneCard,
    SceneMemory,
)


def resolve_scene_summary(session: Session, scene: SceneCard) -> SceneMemory | None:
    return session.execute(
        select(SceneMemory)
        .where(
            SceneMemory.scene_id == scene.scene_id,
            SceneMemory.active_flag == 1,
            SceneMemory.source_review_id.is_not(None),
        )
        .order_by(SceneMemory.created_at.desc(), SceneMemory.row_id.desc())
    ).scalars().first()


def resolve_chapter_summary(session: Session, scene: SceneCard) -> ChapterMemory | None:
    return session.execute(
        select(ChapterMemory)
        .where(
            ChapterMemory.chapter_id == scene.chapter_id,
            ChapterMemory.active_flag == 1,
            ChapterMemory.source_review_id.is_not(None),
        )
        .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
    ).scalars().first()
