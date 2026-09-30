"""三条归档路径的确定性收尾对齐（B03-25，[批准#22]）：晋升（含带作者稿的采纳）重建章汇总之后，卷汇总也跟着做。"""

from __future__ import annotations

import hashlib

from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    OperationLog,
    SceneCard,
    SceneRunState,
    StoryProject,
    VolumeSummary,
)
from novel_system.services.aggregator import VOLUME_CHAPTER_SPAN, Aggregator
from novel_system.services.archive_effects_plan import AUTHOR_ADOPT, MANUSCRIPT_PROMOTE, PIPELINE
from novel_system.services.archiver import Archiver
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.canonical_manuscripts import CanonicalSceneService

PROJECT_ID = "EFFECTS_PLAN"


def _seed_volume_boundary_scene(session) -> dict[str, str]:
    """卷边界上的那一章（第 VOLUME_CHAPTER_SPAN 章）：前面各章各有一份章汇总；这一章一场已归档、有一版作者改过的稿。"""
    chapter_ids = [f"{PROJECT_ID}_CH{index:02d}" for index in range(1, VOLUME_CHAPTER_SPAN + 1)]
    last_chapter = chapter_ids[-1]
    scene_id = f"{last_chapter}_SC01"
    old_final_id = f"final_scene_{scene_id}_v1"
    draft_id = f"author_draft_scene_{scene_id}"
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="雨城旧信",
            outline_text="archive effects plan",
            current_chapter_id=last_chapter,
            approved_chapter_ids_json=[],
        )
    )
    session.flush()
    for order, chapter_id in enumerate(chapter_ids, start=1):
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=PROJECT_ID,
                planned_scene_count=1,
                display_order=order,
                chapter_goal=f"第 {order} 章",
            )
        )
    session.flush()
    for order, chapter_id in enumerate(chapter_ids[:-1], start=1):
        session.add(
            ChapterMemory(
                row_id=f"chapter_memory_{chapter_id}",
                chapter_id=chapter_id,
                aggregate_stage="final",
                content=f"第 {order} 章：林昭把旧信塞回案卷。",
                active_flag=1,
                runtime_eligible=1,
            )
        )
    session.add(ChapterState(chapter_id=last_chapter, aggregate_block_reason="none"))
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=last_chapter,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="把案卷交出去",
            is_chapter_last=1,
        )
    )
    session.flush()
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_status="archived",
            current_final_scene_row_id=old_final_id,
            narrative_sync_status="synced",
            narrative_sync_final_scene_row_id=old_final_id,
        )
    )
    session.add(
        FinalScene(
            row_id=old_final_id,
            scene_id=scene_id,
            chapter_id=last_chapter,
            content="雨城的钟敲过三下。",
            content_hash=hashlib.sha256("雨城的钟敲过三下。".encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash=f"bundle_hash_{scene_id}",
            source_kind="generation",
        )
    )
    session.add(
        AuthorDraft(
            draft_id=draft_id,
            object_type="scene",
            object_id=scene_id,
            source_text_ref=f"final_scene:{old_final_id}",
            content="<p>雨城的钟敲过三下，林昭把案卷交了出去。</p>",
            revision_no=2,
            status="current",
        )
    )
    session.flush()
    Archiver(session).archive_final_scene(scene_id, old_final_id)
    CanonContinuityService(session).verify_scene_complete(
        PROJECT_ID,
        scene_id,
        actor_ref="test",
        note="fixture verifies the previous final before carry-forward",
        expected_final_scene_row_id=old_final_id,
    )
    aggregate = Aggregator(session).run_final_aggregate(last_chapter)
    assert aggregate and aggregate["status"] == "created"
    session.commit()
    return {"scene_id": scene_id, "old_final_id": old_final_id, "draft_id": draft_id, "chapters": chapter_ids}


def test_promotion_rebuilds_the_volume_summary_after_the_chapter_aggregate(session) -> None:
    """以前卷汇总只有流水线做（章末那一场归档第 9 步）：同一章由作者在成稿中心晋升（或带作者稿采纳），
    卷边界上的远景氛围就不跟着更新。现在晋升重建章汇总之后接着卷汇总。"""
    seeded = _seed_volume_boundary_scene(session)
    assert session.scalars(select(VolumeSummary)).all() == []

    CanonicalSceneService(session).promote_author_draft(
        seeded["draft_id"],
        {
            "base_revision_no": 2,
            "expected_current_final_scene_row_id": seeded["old_final_id"],
            "narrative_effect": "facts_unchanged",
            "accepted_warning_codes": [],
        },
        actor_ref="test",
    )
    session.commit()

    volumes = session.scalars(select(VolumeSummary).where(VolumeSummary.project_id == PROJECT_ID)).all()
    assert len(volumes) == 1
    assert (volumes[0].volume_seq, volumes[0].chapter_count) == (1, VOLUME_CHAPTER_SPAN)
    assert (volumes[0].chapter_id_start, volumes[0].chapter_id_end) == (seeded["chapters"][0], seeded["chapters"][-1])
    assert "林昭把案卷交了出去" in volumes[0].atmosphere_summary
    log = session.scalars(
        select(OperationLog).where(OperationLog.event_type == "author_draft_promoted_canonical")
    ).all()[-1]
    assert log.payload_json["volume_aggregate"] == "created"


def test_the_plan_names_what_each_archive_path_does() -> None:
    """三条路径的差别写在一张表里：卷汇总跟着章汇总走；不带作者稿的采纳不重建章汇总（章级读者读时现拼）。"""
    assert PIPELINE.chapter_aggregate == "chapter_last_stage" and PIPELINE.volume_after_chapter_aggregate
    assert MANUSCRIPT_PROMOTE.chapter_aggregate == "every_archive" and MANUSCRIPT_PROMOTE.volume_after_chapter_aggregate
    assert AUTHOR_ADOPT.chapter_aggregate == "derive_on_read" and not AUTHOR_ADOPT.volume_after_chapter_aggregate
    assert all(plan.fidelity_reading for plan in (PIPELINE, MANUSCRIPT_PROMOTE, AUTHOR_ADOPT))
    assert PIPELINE.llm_effects and not MANUSCRIPT_PROMOTE.llm_effects and not AUTHOR_ADOPT.llm_effects
