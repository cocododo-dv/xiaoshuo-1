"""阶段 Y（2026-09-20）：场景卡换章时，挂在这一场上的运行时行跟着走（``services/scene_rehome.py``）。

场景的草稿 / QC / 定稿 / 契约 / 正史……每一行都冗余存了一份 ``chapter_id``，多数还是指向目录章的真外键。
重新分章把场景卡搬到另一章时过去只改卡：

- 起草上下文、正史核对按「这一行的章 == 这一场的章」找东西——搬过的场查不到自己写过的稿；
- 旧章空了进回收站之后，清空回收站会撞上这些外键（500），因为搬走的场的稿子还指着它。
"""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, select

from novel_system.db.models import (
    Base,
    ChapterGoal,
    ChapterRollingNote,
    ContinuitySnapshot,
    FinalScene,
    ReviewItem,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.projects import AUTO_TRASHED_EMPTY_CHAPTER
from novel_system.services.scene_rehome import NOT_REHOMED_TABLES, REHOMED_MODELS, rehome_scenes
from tests.test_catalog_book_spine import _catalog, _materialized, _preview
from tests.test_snowflake_chaptering_story_order import _confirm


#: 带 CHECK 约束的必填列给一个合法值（其余必填列随便填）
_LEGAL_VALUES = {
    "generation_planning_artifacts": {"artifact_type": "character_pressure_blueprint", "object_type": "scene"},
}


def _minimal(model, **values):
    """只填必填列的一行：测的是 chapter_id 跟不跟着走，不是这张表的业务语义。"""
    columns = model.__table__.columns
    values = {**_LEGAL_VALUES.get(model.__tablename__, {}), **values}
    kwargs = {key: value for key, value in values.items() if key in columns.keys()}
    for column in columns:
        if column.name in kwargs or column.nullable or column.computed is not None:
            continue
        if column.default is not None or column.server_default is not None:
            continue
        if column.primary_key and isinstance(column.type, Integer):
            continue
        kind = column.type.python_type
        kwargs[column.name] = f"{column.name}-{uuid.uuid4().hex[:8]}" if kind is str else kind()
    return model(**kwargs)


def _runtime_rows(session, project_id: str, scene_id: str, chapter_id: str, summary: str) -> FinalScene:
    final = _minimal(FinalScene, scene_id=scene_id, chapter_id=chapter_id, project_id=project_id, content=summary)
    session.add(final)
    session.flush()
    for model in REHOMED_MODELS:
        if model is FinalScene:
            continue
        session.add(_minimal(
            model, scene_id=scene_id, chapter_id=chapter_id, project_id=project_id, final_scene_row_id=final.row_id,
        ))
    session.add(_minimal(
        ContinuitySnapshot, snapshot_id=f"continuity_scene_{final.row_id}", project_id=project_id, scope_type="scene",
        scope_id=scene_id, chapter_id=chapter_id, scene_id=scene_id, final_scene_row_id=final.row_id,
        status="complete", summary_text=summary,
    ))
    state = session.get(SceneRunState, scene_id) or _minimal(SceneRunState, scene_id=scene_id)
    state.current_final_scene_row_id = final.row_id
    session.add(state)
    session.flush()
    return final


def test_every_table_that_carries_a_scene_and_a_chapter_has_made_a_choice() -> None:
    """新加一张同时带 scene_id 与 chapter_id 的表：要么跟着场走，要么写明为什么不跟——不许默默漏掉。"""
    rehomed = {model.__tablename__ for model in REHOMED_MODELS}
    assert rehomed.isdisjoint(NOT_REHOMED_TABLES)
    carriers = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if {"scene_id", "chapter_id"} <= set(mapper.local_table.columns.keys())
    }
    assert carriers == rehomed | set(NOT_REHOMED_TABLES), sorted(carriers ^ (rehomed | set(NOT_REHOMED_TABLES)))


def test_every_runtime_row_follows_the_scene_and_nothing_else_moves(client, session) -> None:
    project_id = _materialized(client, "rehome-rows", scenes_per_chapter=3)
    chapters = _catalog(client, project_id)
    old, new = chapters[0]["chapter_id"], chapters[1]["chapter_id"]
    moving, staying = chapters[0]["scenes"][2]["scene_id"], chapters[0]["scenes"][0]["scene_id"]
    _runtime_rows(session, project_id, moving, old, "搬走的这一场的连续性摘要。")
    _runtime_rows(session, project_id, staying, old, "留下的这一场的连续性摘要。")
    canon = CanonContinuityService(session)
    before = canon.rebuild_chapter_snapshot(project_id, old)
    assert "搬走的这一场" in before.summary_text and "留下的这一场" in before.summary_text

    card = session.get(SceneCard, moving)
    card.chapter_id, card.scene_seq, card.is_chapter_last = new, 99, 0
    session.flush()
    result = rehome_scenes(session, project_id, {moving: (old, new)})
    session.commit()

    assert result["moved_row_count"] == len(REHOMED_MODELS) + 1, "每张表一行 + 场景级连续性快照"
    assert result["rebuilt_chapter_snapshots"] == sorted([old, new]) and result["dropped_chapter_snapshots"] == []
    for model in REHOMED_MODELS:
        rows = {row.scene_id: row.chapter_id for row in session.execute(
            select(model).where(model.scene_id.in_([moving, staying]))
        ).scalars()}
        assert rows == {moving: new, staying: old}, model.__tablename__
    scene_snapshots = {row.scene_id: row.chapter_id for row in session.execute(
        select(ContinuitySnapshot).where(ContinuitySnapshot.scope_type == "scene")
    ).scalars()}
    assert scene_snapshots == {moving: new, staying: old}
    # 章级快照是按章内成员重建的投影：两头都按新的成员重算
    chapter_snapshots = {row.scope_id: row.summary_text for row in session.execute(
        select(ContinuitySnapshot).where(ContinuitySnapshot.scope_type == "chapter")
    ).scalars()}
    assert chapter_snapshots[old] == "留下的这一场的连续性摘要。"
    assert chapter_snapshots[new] == "搬走的这一场的连续性摘要。"

    # 再跑一次是空操作
    assert rehome_scenes(session, project_id, {moving: (old, new)})["moved_row_count"] == 0


def test_rechaptering_takes_the_written_scene_along_and_the_emptied_chapter_can_leave_the_trash(client, session) -> None:
    """6 章收成 4 章：第 1 场从它自己的一章并进第 2、3 场的章。它写过的稿跟着走；空了的旧章进回收站，
    清空回收站不撞外键（过去：稿子还指着旧章 → 500）。"""
    project_id = _materialized(client, "rehome-rechapter", scenes_per_chapter=2)
    chapters = _catalog(client, project_id)
    old = chapters[0]["chapter_id"]
    assert len(chapters[0]["scenes"]) == 1
    scene_id = chapters[0]["scenes"][0]["scene_id"]
    final = _runtime_rows(session, project_id, scene_id, old, "他第一次怀疑那本日志被人动过。")
    CanonContinuityService(session).rebuild_chapter_snapshot(project_id, old)
    session.commit()

    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "rehome-rechapter-2")
    assert old in {item["chapter_id"] for item in result["trashed_empty_chapters"]}
    session.expire_all()
    new = session.get(SceneCard, scene_id).chapter_id
    assert new != old and new == chapters[1]["chapter_id"], "并进了第 2、3 场的那一章（同一行目录章）"
    assert session.get(FinalScene, final.row_id).chapter_id == new
    for model in (SceneDraft, ReviewItem, ChapterRollingNote):
        assert {row.chapter_id for row in session.execute(select(model).where(model.scene_id == scene_id)).scalars()} == {new}
    snapshots = {(row.scope_type, row.scope_id) for row in session.execute(
        select(ContinuitySnapshot).where(ContinuitySnapshot.project_id == project_id)
    ).scalars()}
    assert ("chapter", old) not in snapshots, "空章没有连续性可投影——这一行留着，清空回收站会撞外键"
    assert ("chapter", new) in snapshots and ("scene", scene_id) in snapshots

    row = session.get(ChapterGoal, old)
    assert row.trashed_flag == 1 and row.trashed_by == AUTO_TRASHED_EMPTY_CHAPTER
    purged = client.delete(f"/api/v2/trash/chapter:{old}", headers={"X-Idempotency-Key": "rehome-purge"})
    assert purged.status_code == 200, purged.text
    session.expire_all()
    assert session.get(ChapterGoal, old) is None
    assert session.get(FinalScene, final.row_id).chapter_id == new, "稿子还在，跟着场"
