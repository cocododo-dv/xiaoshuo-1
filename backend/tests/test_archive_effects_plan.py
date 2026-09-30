"""三条归档路径的确定性收尾（B03-25，[批准#22]）：``services/archive_effects_plan.py`` 说明里的每一条都在这里钉住。

- 晋升（成稿中心；带作者稿的采纳也走它）重建章汇总之后，卷汇总也跟着做；
- 不带作者稿的两种采纳（已归档的重放、流水线稿）既不重建章汇总，也不做卷汇总（章级读者读时现拼）。

流水线那一边（章末那一场的归档第 8 / 9 步）由 ``test_scene_run_checkpoint_archive.py`` 的
``test_chapter_last_sub9_volume_boundary_crash_reuses_same_summary`` 与
``test_chapter_last_volume_rolls_up_the_stored_aggregate_when_stage_8_cannot_rebuild_it``（第 8 步拼不出章汇总，第 9 步
照样卷存着的那份）钉住；晋升这一次章汇总没重建成、卷汇总记 ``skipped`` 的那一条在 ``test_chapter_aggregate_derive_on_read.py``。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    OperationLog,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    VolumeSummary,
)
from novel_system.services.aggregator import VOLUME_CHAPTER_SPAN, Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.canonical_manuscripts import CanonicalSceneService

PROJECT_ID = "EFFECTS_PLAN"
CHAPTER_IDS = [f"{PROJECT_ID}_CH{index:02d}" for index in range(1, VOLUME_CHAPTER_SPAN + 1)]
LAST_CHAPTER = CHAPTER_IDS[-1]
SCENE_ID = f"{LAST_CHAPTER}_SC01"


def _seed_volume_boundary_chapters(session) -> None:
    """卷边界上的那一章（第 VOLUME_CHAPTER_SPAN 章）与前面各章：前面各章各有一份章汇总；这一章只有一场，是章末场。"""
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="雨城旧信",
            outline_text="archive effects plan",
            current_chapter_id=LAST_CHAPTER,
            approved_chapter_ids_json=[],
        )
    )
    session.flush()
    for order, chapter_id in enumerate(CHAPTER_IDS, start=1):
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
    for order, chapter_id in enumerate(CHAPTER_IDS[:-1], start=1):
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
    session.add(ChapterState(chapter_id=LAST_CHAPTER, aggregate_block_reason="none"))
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=LAST_CHAPTER,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="把案卷交出去",
            is_chapter_last=1,
        )
    )
    session.flush()


def _seed_archived_scene_with_author_revision(session) -> dict[str, str]:
    """章末场已归档（章汇总也建过一次、卷汇总还没有），另有一版作者改过的稿。"""
    _seed_volume_boundary_chapters(session)
    old_final_id = f"final_scene_{SCENE_ID}_v1"
    draft_id = f"author_draft_scene_{SCENE_ID}"
    session.add(
        SceneRunState(
            scene_id=SCENE_ID,
            scene_status="archived",
            current_final_scene_row_id=old_final_id,
            narrative_sync_status="synced",
            narrative_sync_final_scene_row_id=old_final_id,
        )
    )
    session.add(
        FinalScene(
            row_id=old_final_id,
            scene_id=SCENE_ID,
            chapter_id=LAST_CHAPTER,
            content="雨城的钟敲过三下。",
            content_hash=hashlib.sha256("雨城的钟敲过三下。".encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{SCENE_ID}",
            source_bundle_hash=f"bundle_hash_{SCENE_ID}",
            source_kind="generation",
        )
    )
    session.add(
        AuthorDraft(
            draft_id=draft_id,
            object_type="scene",
            object_id=SCENE_ID,
            source_text_ref=f"final_scene:{old_final_id}",
            content="<p>雨城的钟敲过三下，林昭把案卷交了出去。</p>",
            revision_no=2,
            status="current",
        )
    )
    session.flush()
    Archiver(session).archive_final_scene(SCENE_ID, old_final_id)
    CanonContinuityService(session).verify_scene_complete(
        PROJECT_ID,
        SCENE_ID,
        actor_ref="test",
        note="fixture verifies the previous final before carry-forward",
        expected_final_scene_row_id=old_final_id,
    )
    aggregate = Aggregator(session).run_final_aggregate(LAST_CHAPTER)
    assert aggregate and aggregate["status"] == "created"
    session.commit()
    return {"old_final_id": old_final_id, "draft_id": draft_id}


def _counts(session) -> tuple[int, int]:
    """(卷汇总行数, 章末那一章的章汇总行数)。"""
    session.expire_all()
    volumes = session.scalar(select(func.count()).select_from(VolumeSummary))
    chapter_memories = session.scalar(
        select(func.count()).select_from(ChapterMemory).where(ChapterMemory.chapter_id == LAST_CHAPTER)
    )
    return int(volumes or 0), int(chapter_memories or 0)


def _assert_volume_rolled_up_with(session, text: str) -> None:
    volumes = session.scalars(select(VolumeSummary).where(VolumeSummary.project_id == PROJECT_ID)).all()
    assert len(volumes) == 1
    assert (volumes[0].volume_seq, volumes[0].chapter_count) == (1, VOLUME_CHAPTER_SPAN)
    assert (volumes[0].chapter_id_start, volumes[0].chapter_id_end) == (CHAPTER_IDS[0], CHAPTER_IDS[-1])
    assert text in volumes[0].atmosphere_summary


def test_promotion_rebuilds_the_volume_summary_after_the_chapter_aggregate(session) -> None:
    """以前卷汇总只有流水线做（章末那一场归档第 9 步）：同一章由作者在成稿中心晋升，卷边界上的远景氛围就不跟着
    更新。现在晋升重建章汇总之后接着卷汇总。"""
    seeded = _seed_archived_scene_with_author_revision(session)
    assert _counts(session) == (0, 1)

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

    assert _counts(session) == (1, 2)
    _assert_volume_rolled_up_with(session, "林昭把案卷交了出去")
    log = session.scalars(
        select(OperationLog).where(OperationLog.event_type == "author_draft_promoted_canonical")
    ).all()[-1]
    assert log.payload_json["volume_aggregate"] == "created"


def test_adopt_with_the_exact_author_draft_goes_through_promotion_and_rolls_up_the_volume(client, session) -> None:
    """React 的「采纳并归档」总带作者稿（exact_author_draft）：走的是晋升，章汇总与卷汇总都跟着做。"""
    seeded = _seed_archived_scene_with_author_revision(session)
    assert _counts(session) == (0, 1)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={
            "accepted_warning_codes": [],
            "exact_author_draft": {
                "draft_id": seeded["draft_id"],
                "base_revision_no": 2,
                "expected_current_final_scene_row_id": seeded["old_final_id"],
                "content": "<p>雨城的钟敲过三下，林昭把案卷交给了守夜人。</p>",
            },
        },
        headers={"X-Idempotency-Key": "effects-plan-adopt-exact"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["exact_author_draft"] is True
    assert _counts(session) == (1, 2)
    _assert_volume_rolled_up_with(session, "林昭把案卷交给了守夜人")


def test_adopt_replay_of_an_archived_scene_rebuilds_neither_aggregate(client, session) -> None:
    """不带作者稿、场景已归档：重放归档确认，不重建章汇总、不做卷汇总（章级读者读时现拼，卷汇总跟着章汇总走）。"""
    _seed_archived_scene_with_author_revision(session)
    assert _counts(session) == (0, 1)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "effects-plan-adopt-replay"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["already_archived"] is True
    assert _counts(session) == (0, 1)


def test_adopt_of_a_pipeline_draft_rebuilds_neither_aggregate(client, session) -> None:
    """不带作者稿、采纳流水线停下的稿子（章末场、卷边界上）：归档了，但不重建章汇总、不做卷汇总——不照搬流水线
    「章末那一场做第 8 / 9 步」的规则。"""
    _seed_volume_boundary_chapters(session)
    draft_row_id = f"draft_style_{SCENE_ID}_v1"
    session.add(
        SceneDraft(
            row_id=draft_row_id,
            scene_id=SCENE_ID,
            chapter_id=LAST_CHAPTER,
            stage="style",
            content="雨城的钟敲过三下，林昭把案卷交了出去。",
            source_bundle_id=f"bundle_{SCENE_ID}",
            source_bundle_hash=f"bundle_hash_{SCENE_ID}",
        )
    )
    session.add(
        SceneRunState(
            scene_id=SCENE_ID,
            scene_status="quality_warning_pending_acceptance",
            current_style_draft_row_id=draft_row_id,
        )
    )
    session.commit()
    assert _counts(session) == (0, 0)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "effects-plan-adopt-pipeline-draft"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["scene_status"] == "archived"
    assert _counts(session) == (0, 0)
