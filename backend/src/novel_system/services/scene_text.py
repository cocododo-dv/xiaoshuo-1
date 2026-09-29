"""Shared DB lookups for a scene's / chapter's current text rows (a leaf: models + SQLAlchemy only).

One home for the copies that were byte-identical (X02-18 / X01-14). Rules that differ stay where they
are, under their own names — they are NOT this module's contract:

- current author draft *without* the ``updated_at`` / ``draft_id`` ordering
  (``writer_deep_review`` passage patch apply, ``style_reference.check_job`` scene text,
  ``api/routes/scenes`` adopt-current, ``project_overview`` batch by scene ids);
- the final-scene pointer rule of ``canonical_manuscripts`` / ``project_overview`` / the run pipeline.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import AuthorDraft, ChapterMemory, ChapterState, FinalScene, SceneRunState


def current_author_draft(session: Session, object_type: str, object_id: str) -> AuthorDraft | None:
    """The ``current`` author draft of an object; newest ``updated_at`` then ``draft_id`` wins."""
    return session.execute(
        select(AuthorDraft)
        .where(
            AuthorDraft.object_type == object_type,
            AuthorDraft.object_id == object_id,
            AuthorDraft.status == "current",
        )
        .order_by(AuthorDraft.updated_at.desc(), AuthorDraft.draft_id.desc())
    ).scalars().first()


def pointed_final_scene(session: Session, scene_id: str) -> FinalScene | None:
    """The run state's ``current_final_scene_row_id`` when it points at this scene, else the newest row."""
    state = session.get(SceneRunState, scene_id)
    if state is not None and state.current_final_scene_row_id:
        pointed = session.get(FinalScene, state.current_final_scene_row_id)
        if pointed is not None and pointed.scene_id == scene_id:
            return pointed
    return session.execute(
        select(FinalScene)
        .where(FinalScene.scene_id == scene_id)
        .order_by(FinalScene.created_at.desc(), FinalScene.row_id.desc())
    ).scalars().first()


def final_chapter_memory(session: Session, chapter_id: str) -> ChapterMemory | None:
    """The chapter's final aggregate: the state's pointer when valid, else the newest active final row."""
    state = session.get(ChapterState, chapter_id)
    if state is not None and state.last_final_memory_row_id:
        pointed = session.get(ChapterMemory, state.last_final_memory_row_id)
        if pointed is not None and pointed.chapter_id == chapter_id and pointed.aggregate_stage == "final":
            return pointed
    return session.execute(
        select(ChapterMemory)
        .where(
            ChapterMemory.chapter_id == chapter_id,
            ChapterMemory.aggregate_stage == "final",
            ChapterMemory.active_flag == 1,
        )
        .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
    ).scalars().first()
