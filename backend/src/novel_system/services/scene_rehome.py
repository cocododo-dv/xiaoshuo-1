"""场景卡换章时，把挂在这一场上的运行时行一起带走（阶段 Y，2026-09-20）。

场景的运行时产物——草稿、QC、定稿、执行契约、蓝图、bundle、attempt、人工复核、场景记忆、修订候选、
写手评审、正史提交 / 事实候选 / 叙事事件、场景级连续性快照——每一行都冗余存了一份 ``chapter_id``。
重新分章把场景卡搬到另一章时，过去只改 ``SceneCard.chapter_id``，这些行还指着旧章：

- 起草上下文按 ``FinalScene.chapter_id == scene.chapter_id`` 找本章已写的场、上一章的末场
  （新鲜度预算、声音锚点）——搬过的场查不到；
- 正史层拿 ``final.chapter_id`` 与 ``scene.chapter_id`` 互相核对——搬过的场直接报「不属于这一章」；
- 章级连续性快照是按章内的场重建的投影——两头的章都过期了；
- 这些表的 ``chapter_id`` 多数是真外键：旧章空了进回收站之后，作者清空回收站会撞上外键（500），
  因为搬走的场的草稿 / 定稿还指着它；待办卡（``ReviewItem``）按章筛选时也还挂在旧章下面。

章 id 钉住之后（``SnowflakeChapterPlan.catalog_chapter_id``）跨章搬动已经少得多（只有真的换了章的场才搬），
但挪章界、拆章、并章仍然会搬——搬的时候这些行必须跟着走。

刻意不动的表见 ``NOT_REHOMED_TABLES``。新加一张同时带 ``scene_id`` 与 ``chapter_id`` 的表时，
``tests/test_scene_rehome.py`` 的守卫要求在两个清单里选一个——不许默默漏掉。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    CanonCommit,
    ChapterRollingNote,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    NarrativeEvent,
    PassagePatchCandidate,
    QcReport,
    ReviewItem,
    RevisionCandidate,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneExecutionContract,
    SceneMemory,
    WriterEvaluation,
)
from novel_system.services.canon_continuity import CanonContinuityService

#: 跟着场景走的表：行上同时有 scene_id 与 chapter_id，且 chapter_id 的含义就是「这一场所在的章」
REHOMED_MODELS = (
    SceneBundle,
    SceneBlueprint,
    SceneExecutionContract,
    SceneDraft,
    QcReport,
    FinalScene,
    AttemptTracker,
    HumanReviewEvent,
    SceneMemory,
    RevisionCandidate,
    PassagePatchCandidate,
    WriterEvaluation,
    GenerationPlanningArtifact,
    CanonCommit,
    FactCandidate,
    NarrativeEvent,
    ReviewItem,
    ChapterRollingNote,  # 名字带「章」，其实一场一条（scene_id 唯一），归档这一场时写下
)

#: 同样带着 scene_id + chapter_id、但刻意不跟着搬的表，以及为什么
NOT_REHOMED_TABLES = {
    "scene_cards": "场景卡自己：搬章的就是它，由物化 / 回流直接改",
    "snowflake_scene_plans": "构思侧的分章自己管（save / propose 时给每一场重盖章戳）",
    "continuity_snapshots": "场景级快照在 rehome_scenes 里单独处理（scope_type = scene），章级快照是重建出来的投影",
    "llm_calls": "记账流水：记的是调用发生时这一场挂在哪一章，不改写历史；没有外键",
    "chapter_run_jobs": "章级任务，场只是它跑到哪一场的指针",
}


def rehome_scenes(session: Session, project_id: str, moves: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """``moves``：``{scene_id: (旧章, 新章)}``。返回搬了多少行、重建了哪几章的连续性快照。"""
    moved_rows = 0
    snapshot_chapters: set[str] = set()
    for scene_id, (old_chapter_id, new_chapter_id) in moves.items():
        if not scene_id or not new_chapter_id or old_chapter_id == new_chapter_id:
            continue
        for model in REHOMED_MODELS:
            rows = session.execute(
                select(model).where(model.scene_id == scene_id, model.chapter_id != new_chapter_id)
            ).scalars().all()
            for row in rows:
                row.chapter_id = new_chapter_id
            moved_rows += len(rows)
        snapshots = session.execute(
            select(ContinuitySnapshot).where(
                ContinuitySnapshot.scene_id == scene_id, ContinuitySnapshot.scope_type == "scene"
            )
        ).scalars().all()
        for snapshot in snapshots:
            if snapshot.chapter_id != new_chapter_id:
                snapshot.chapter_id = new_chapter_id
                moved_rows += 1
        if snapshots:
            # 这一场有正史：两头的章级快照都要按新的章内成员重算
            snapshot_chapters.update({old_chapter_id, new_chapter_id})
    rebuilt: list[str] = []
    dropped: list[str] = []
    if snapshot_chapters:
        session.flush()
        canon = CanonContinuityService(session)
        for chapter_id in sorted(chapter for chapter in snapshot_chapters if chapter):
            still_holds_a_scene = session.execute(
                select(SceneCard.scene_id).where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0).limit(1)
            ).first()
            if still_holds_a_scene is None:
                # 场全搬走了：空章没有连续性可投影。留着这一行，章进回收站后清空回收站会撞上它的外键。
                for stale in session.execute(
                    select(ContinuitySnapshot).where(
                        ContinuitySnapshot.project_id == project_id,
                        ContinuitySnapshot.scope_type == "chapter",
                        ContinuitySnapshot.scope_id == chapter_id,
                    )
                ).scalars().all():
                    session.delete(stale)
                dropped.append(chapter_id)
                continue
            canon.rebuild_chapter_snapshot(project_id, chapter_id)
            rebuilt.append(chapter_id)
    if moved_rows or dropped:
        session.flush()
    return {"moved_row_count": moved_rows, "rebuilt_chapter_snapshots": rebuilt, "dropped_chapter_snapshots": dropped}
