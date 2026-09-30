from __future__ import annotations


from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterMemory,
    SceneCard,
    SceneMemory,
)


class Resolver:
    def resolve_scene_summary(self, session: Session, scene: SceneCard) -> SceneMemory | None:
        return session.execute(
            select(SceneMemory)
            .where(
                SceneMemory.scene_id == scene.scene_id,
                SceneMemory.active_flag == 1,
                SceneMemory.source_review_id.is_not(None),
            )
            .order_by(SceneMemory.created_at.desc(), SceneMemory.row_id.desc())
        ).scalars().first()

    def resolve_chapter_summary(self, session: Session, scene: SceneCard) -> ChapterMemory | None:
        return session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id == scene.chapter_id,
                ChapterMemory.active_flag == 1,
                ChapterMemory.source_review_id.is_not(None),
            )
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()
