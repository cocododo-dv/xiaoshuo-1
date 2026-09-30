"""Shared DB lookups for a scene's / chapter's current text rows (a leaf: models + SQLAlchemy only).

One home for the copies that were byte-identical (X02-18 / X01-14). Rules that differ stay where they
are, under their own names — they are NOT this module's contract:

- current author draft *without* the ``updated_at`` / ``draft_id`` ordering
  (``writer_deep_review`` passage patch apply, ``style_reference.check_job`` scene text,
  ``api/routes/scenes`` adopt-current, ``project_overview`` batch by scene ids);
- the final-scene pointer rule of ``canonical_manuscripts`` / ``project_overview`` / the run pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable

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


# ---- 批量版本：规则与上面的单行查询逐条相同，行数多少都是固定几次查询（文学质量巡检一次看全书） ----


def _unique_ids(ids: Iterable[str]) -> list[str]:
    return [value for value in dict.fromkeys(ids) if value]


def current_author_drafts(session: Session, object_type: str, object_ids: Iterable[str]) -> dict[str, AuthorDraft]:
    """:func:`current_author_draft` for many objects of one type: object id → draft (objects without one are absent)."""
    ids = _unique_ids(object_ids)
    drafts: dict[str, AuthorDraft] = {}
    if not ids:
        return drafts
    for draft in session.execute(
        select(AuthorDraft)
        .where(
            AuthorDraft.object_type == object_type,
            AuthorDraft.object_id.in_(ids),
            AuthorDraft.status == "current",
        )
        .order_by(AuthorDraft.object_id, AuthorDraft.updated_at.desc(), AuthorDraft.draft_id.desc())
    ).scalars():
        drafts.setdefault(draft.object_id, draft)
    return drafts


def pointed_final_scenes(session: Session, scene_ids: Iterable[str]) -> dict[str, FinalScene]:
    """:func:`pointed_final_scene` for many scenes: scene id → final row (scenes without one are absent)."""
    ids = _unique_ids(scene_ids)
    finals: dict[str, FinalScene] = {}
    if not ids:
        return finals
    pointers = {
        scene_id: row_id
        for scene_id, row_id in session.execute(
            select(SceneRunState.scene_id, SceneRunState.current_final_scene_row_id).where(
                SceneRunState.scene_id.in_(ids)
            )
        ).all()
        if row_id
    }
    if pointers:
        rows = {
            row.row_id: row
            for row in session.execute(
                select(FinalScene).where(FinalScene.row_id.in_(list(dict.fromkeys(pointers.values()))))
            ).scalars()
        }
        for scene_id, row_id in pointers.items():
            pointed = rows.get(row_id)
            if pointed is not None and pointed.scene_id == scene_id:
                finals[scene_id] = pointed
    missing = [scene_id for scene_id in ids if scene_id not in finals]
    if missing:
        for row in session.execute(
            select(FinalScene)
            .where(FinalScene.scene_id.in_(missing))
            .order_by(FinalScene.scene_id, FinalScene.created_at.desc(), FinalScene.row_id.desc())
        ).scalars():
            finals.setdefault(row.scene_id, row)
    return finals
