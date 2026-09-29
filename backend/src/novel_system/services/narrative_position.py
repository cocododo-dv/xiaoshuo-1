"""Canonical cross-chapter narrative ordering.

``SceneCard.scene_seq`` is chapter-local and therefore cannot be used as a
project-wide cursor.  This module derives a scene's position from the two
authoritative, mutable catalog fields instead of persisting another order
cache that would have to be kept in sync after every reorder::

    (chapter.display_order, chapter_id, scene.scene_seq, scene_id)

Missing/duplicate chapter orders are made deterministic with the same
``NULLS LAST + chapter_id`` fallback used by ``CatalogService.chapter_rows``.
Narrative events join back to their scene/chapter so a catalog reorder is
immediately reflected by replay without mutating the append-only event rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, NarrativeEvent, SceneCard
from novel_system.services.errors import DomainError


@dataclass(frozen=True, slots=True)
class NarrativeCursor:
    chapter_missing_order: int
    chapter_order: int
    chapter_id: str
    scene_seq: int
    scene_id: str


class NarrativePositionService:
    """Resolve catalog positions and apply them to scene/event queries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def chapter_missing_expr() -> Any:
        return case((ChapterGoal.display_order.is_(None), 1), else_=0)

    @staticmethod
    def chapter_order_expr() -> Any:
        return func.coalesce(ChapterGoal.display_order, 0)

    def cursor_for_scene(self, project_id: str, scene_id: str) -> NarrativeCursor:
        # Scene creation and event recording often share one transaction.  Flush
        # pending catalog rows so ``Session.get`` can resolve their position.
        self.session.flush()
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError(
                "NARRATIVE_CURSOR_SCENE_NOT_FOUND",
                f"scene '{scene_id}' was not found",
                status_code=404,
            )
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if chapter is None:
            raise DomainError(
                "NARRATIVE_CURSOR_CHAPTER_NOT_FOUND",
                f"chapter '{scene.chapter_id}' was not found",
                status_code=409,
            )
        known_projects = {value for value in (scene.project_id, chapter.project_id) if value}
        if known_projects and project_id not in known_projects:
            raise DomainError(
                "NARRATIVE_CURSOR_PROJECT_MISMATCH",
                f"scene '{scene_id}' does not belong to project '{project_id}'",
                status_code=409,
            )
        if len(known_projects) > 1:
            raise DomainError(
                "NARRATIVE_CURSOR_PROJECT_MISMATCH",
                f"scene '{scene_id}' and chapter '{chapter.chapter_id}' disagree on project",
                status_code=409,
            )
        return NarrativeCursor(
            chapter_missing_order=1 if chapter.display_order is None else 0,
            chapter_order=int(chapter.display_order or 0),
            chapter_id=chapter.chapter_id,
            scene_seq=int(scene.scene_seq or 0),
            scene_id=scene.scene_id,
        )

    def event_statement(self, project_id: str):
        """Return active project events joined to their canonical positions."""
        return (
            select(NarrativeEvent)
            .join(
                SceneCard,
                and_(
                    SceneCard.scene_id == NarrativeEvent.scene_id,
                    SceneCard.chapter_id == NarrativeEvent.chapter_id,
                ),
            )
            .join(ChapterGoal, ChapterGoal.chapter_id == SceneCard.chapter_id)
            .where(
                NarrativeEvent.project_id == project_id,
                SceneCard.trashed_flag == 0,
                ChapterGoal.trashed_flag == 0,
            )
        )

    def ordered_events(self, statement, *, descending: bool = False):
        fields = (
            self.chapter_missing_expr(),
            self.chapter_order_expr(),
            ChapterGoal.chapter_id,
            SceneCard.scene_seq,
            SceneCard.scene_id,
            NarrativeEvent.created_at,
            NarrativeEvent.event_id,
        )
        if descending:
            return statement.order_by(*(field.desc() for field in fields))
        return statement.order_by(*(field.asc() for field in fields))

    def before(self, statement, cursor: NarrativeCursor, *, inclusive: bool = False):
        """Restrict a positioned scene/event statement to a narrative boundary."""
        missing = self.chapter_missing_expr()
        order = self.chapter_order_expr()
        final_scene_comparison = (
            SceneCard.scene_id <= cursor.scene_id
            if inclusive
            else SceneCard.scene_id < cursor.scene_id
        )
        return statement.where(
            or_(
                missing < cursor.chapter_missing_order,
                and_(
                    missing == cursor.chapter_missing_order,
                    order < cursor.chapter_order,
                ),
                and_(
                    missing == cursor.chapter_missing_order,
                    order == cursor.chapter_order,
                    ChapterGoal.chapter_id < cursor.chapter_id,
                ),
                and_(
                    missing == cursor.chapter_missing_order,
                    order == cursor.chapter_order,
                    ChapterGoal.chapter_id == cursor.chapter_id,
                    SceneCard.scene_seq < cursor.scene_seq,
                ),
                and_(
                    missing == cursor.chapter_missing_order,
                    order == cursor.chapter_order,
                    ChapterGoal.chapter_id == cursor.chapter_id,
                    SceneCard.scene_seq == cursor.scene_seq,
                    final_scene_comparison,
                ),
            )
        )

    def scene_statement(self, project_id: str):
        return (
            select(SceneCard)
            .join(ChapterGoal, ChapterGoal.chapter_id == SceneCard.chapter_id)
            .where(
                or_(
                    SceneCard.project_id == project_id,
                    and_(
                        SceneCard.project_id.is_(None),
                        ChapterGoal.project_id == project_id,
                    ),
                ),
                SceneCard.trashed_flag == 0,
                ChapterGoal.trashed_flag == 0,
            )
        )

    def scenes_before(self, project_id: str, scene_id: str) -> list[SceneCard]:
        cursor = self.cursor_for_scene(project_id, scene_id)
        statement = self.before(self.scene_statement(project_id), cursor)
        statement = statement.order_by(
            self.chapter_missing_expr().asc(),
            self.chapter_order_expr().asc(),
            ChapterGoal.chapter_id.asc(),
            SceneCard.scene_seq.asc(),
            SceneCard.scene_id.asc(),
        )
        return list(self.session.execute(statement).scalars().all())
