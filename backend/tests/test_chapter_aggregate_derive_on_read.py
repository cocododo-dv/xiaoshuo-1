"""章汇总读时现拼（重评 R13 + 主管补充，[批准#21]）：章级读者一律读各场当前终稿现拼，存下来的章汇总（ChapterMemory）
只是一份派生缓存——

- 回收站里的场不在这一章里：它的场景记忆不算进章汇总，也就不再挡住同一章别的场晋升（探针 a）；
- 流水线先归档章末一场、后归档前面的场时，汇总漏掉前面那场；场序重排后汇总还是旧顺序——章级读者（文学质量的
  章源、章级准终稿评审）都不读它（探针 b、复核的重排探针）。

探针原稿：``scratch/reeval/r13/*.py`` 与 ``scratch/reeval/g5_critic/probe_reorder_stale_aggregate.py``。
"""

from __future__ import annotations

import hashlib

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryProject,
)
from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.catalog import CatalogService
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.literary_quality.service import LiteraryQualityService
from novel_system.services.near_final import NearFinalAcceptanceService


def _seed_chapter(session, key: str, scene_count: int) -> tuple[str, str, list[str]]:
    """一部作品、一章、``scene_count`` 场（最后一场是章末场），都还没有终稿。"""
    project_id = f"R13_{key}"
    chapter_id = f"{project_id}_CH01"
    session.add(
        StoryProject(
            project_id=project_id,
            title="雨城旧信",
            outline_text="林昭重读案卷。",
            current_chapter_id=chapter_id,
            approved_chapter_ids_json=[],
        )
    )
    session.flush()
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            planned_scene_count=scene_count,
            display_order=1,
            chapter_goal="林昭在案卷里找到旧信",
        )
    )
    session.flush()
    session.add(ChapterState(chapter_id=chapter_id, aggregate_block_reason="none"))
    scene_ids = [f"{chapter_id}_SC0{seq}" for seq in range(1, scene_count + 1)]
    for seq, scene_id in enumerate(scene_ids, start=1):
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                project_id=project_id,
                scene_seq=seq,
                scene_goal="林昭读旧信",
                is_chapter_last=1 if seq == scene_count else 0,
            )
        )
    session.commit()
    return project_id, chapter_id, scene_ids


def _pipeline_archive(session, scene_id: str, text: str, *, chapter_last_rebuilds: bool = True) -> str:
    """与流水线归档段相同的两步：归档这一场（每场都做），章末那一场再重建章汇总（归档第 8 步只在章末做）。
    ``chapter_last_rebuilds=False``：只归档（不是章末那一场时流水线就是这样）。"""
    scene = session.get(SceneCard, scene_id)
    final_id = f"final_{scene_id}_v1"
    session.add(SceneRunState(scene_id=scene_id, scene_status="ready", current_final_scene_row_id=final_id))
    session.add(
        FinalScene(
            row_id=final_id,
            scene_id=scene_id,
            chapter_id=scene.chapter_id,
            content=text,
            status="draft",
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash=f"hash_{scene_id}",
        )
    )
    session.flush()
    Archiver(session).archive_final_scene(scene_id, final_id, carry_notes_json=[], author_confirmed_final=True)
    if chapter_last_rebuilds and scene.is_chapter_last == 1:
        Aggregator(session).run_final_aggregate(scene.chapter_id)
    session.commit()
    return final_id


def _stored_aggregate(session, chapter_id: str) -> ChapterMemory | None:
    return (
        session.query(ChapterMemory)
        .filter_by(chapter_id=chapter_id, aggregate_stage="final", active_flag=1)
        .one_or_none()
    )


# ---------------------------------------------------------------------------------------------- 回收站（探针 a）


def test_a_trashed_archived_scene_is_outside_the_chapter_aggregate(session) -> None:
    _project_id, chapter_id, (first, second, third) = _seed_chapter(session, "TRASH_AGG", 3)
    _pipeline_archive(session, first, "林昭拆开第一封旧信。")
    _pipeline_archive(session, second, "第二场：雨夜里她把信纸摊在案卷上。")
    created = Aggregator(session).run_final_aggregate(chapter_id)
    session.commit()
    assert created["status"] == "created"

    trashed = AuthorLifecycleService(session).trash_scenes([second], "author")
    session.commit()
    assert [item["scene_id"] for item in trashed["processed"]] == [second]

    # 回收站里那一场的场景记忆还在（清除之前都能恢复），但它已经不在这一章里：汇总照常重建，只是没有它
    assert session.query(SceneMemory).filter_by(scene_id=second, active_flag=1).count() == 1
    rebuilt = Aggregator(session).run_final_aggregate(chapter_id)
    session.commit()
    assert rebuilt["status"] == "created", rebuilt
    assert _stored_aggregate(session, chapter_id).content == "林昭拆开第一封旧信。"

    # 恢复之后它又回到这一章
    AuthorLifecycleService(session).restore_scenes([second])
    session.commit()
    restored = Aggregator(session).run_final_aggregate(chapter_id)
    session.commit()
    assert restored["status"] == "created"
    assert _stored_aggregate(session, chapter_id).content == "林昭拆开第一封旧信。\n第二场：雨夜里她把信纸摊在案卷上。"
    assert third  # 第三场（章末）没有正文：不在汇总里，也不挡汇总


def test_promotion_is_not_blocked_after_an_archived_sibling_went_to_the_trash(client, session) -> None:
    project_id, chapter_id, (first, second) = _seed_chapter(session, "TRASH_PROMOTE", 2)
    drafts: dict[str, tuple[str, str]] = {}
    for scene_id in (first, second):
        final_id = f"final_{scene_id}_v1"
        text = f"旧权威正文：{scene_id[-4:]}。"
        session.add(
            SceneRunState(
                scene_id=scene_id,
                scene_status="archived",
                current_final_scene_row_id=final_id,
                narrative_sync_status="synced",
                narrative_sync_final_scene_row_id=final_id,
            )
        )
        session.add(
            FinalScene(
                row_id=final_id,
                scene_id=scene_id,
                chapter_id=chapter_id,
                content=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                status="archived",
                source_bundle_id=f"bundle_{scene_id}",
                source_bundle_hash=f"hash_{scene_id}",
                source_kind="generation",
            )
        )
        draft_id = f"author_draft_scene_{scene_id}"
        session.add(
            AuthorDraft(
                draft_id=draft_id,
                object_type="scene",
                object_id=scene_id,
                source_text_ref=f"final_scene:{final_id}",
                content="<p>林昭把旧信塞回案卷，雨声停了。</p>",
                revision_no=2,
                status="current",
            )
        )
        session.flush()
        Archiver(session).archive_final_scene(scene_id, final_id)
        CanonContinuityService(session).verify_scene_complete(
            project_id, scene_id, actor_ref="author", note="核对过。", expected_final_scene_row_id=final_id
        )
        drafts[scene_id] = (draft_id, final_id)
    assert Aggregator(session).run_final_aggregate(chapter_id)["status"] == "created"
    session.commit()

    AuthorLifecycleService(session).trash_scenes([second], "author")
    session.commit()

    draft_id, final_id = drafts[first]
    response = client.post(
        f"/api/v1/author-drafts/{draft_id}/promote-canonical",
        json={
            "base_revision_no": 2,
            "expected_current_final_scene_row_id": final_id,
            "narrative_effect": "facts_unchanged",
            "accepted_warning_codes": [],
        },
        headers={"X-Idempotency-Key": "r13-promote-after-trash"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["final_scene_row_id"] != final_id
    session.expire_all()
    aggregate = _stored_aggregate(session, chapter_id)
    assert aggregate is not None and aggregate.row_id == data["chapter_memory_row_id"]
    assert aggregate.content == "林昭把旧信塞回案卷，雨声停了。"


# ---------------------------------------------------------------------------------------------- 章级读者（探针 b、重排）


def _quality_chapter_source(session, chapter_id: str, text_layer: str = "author_draft_preferred") -> dict | None:
    """文学质量巡检拼给一章的那份正文（与 overview / chapter-set-review 同一条取法）。"""
    service = LiteraryQualityService(session)
    rows = service._text_rows(
        text_layer,
        chapter_ids=[chapter_id],
        scenes=service._scenes(project_id=None, chapter_id=chapter_id),
    )
    return service._chapter_source(chapter_id, text_layer=text_layer, rows=rows)


def _overview_chapter_item(client, chapter_id: str, text_layer: str = "author_draft_preferred") -> dict:
    response = client.get(f"/api/v1/literary-quality/overview?text_layer={text_layer}&chapter_id={chapter_id}")
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["data"]["items"] if item["object_type"] == "chapter")


def test_chapter_readers_include_scenes_archived_after_the_chapter_last_one(client, session) -> None:
    _project_id, chapter_id, (first, second) = _seed_chapter(session, "PIPELINE_ORDER", 2)
    _pipeline_archive(session, second, "第二场（本章最后一场）先起草、先归档。")
    _pipeline_archive(session, first, "第一场后起草、后归档。")

    # 存下来的汇总只在章末那一场归档时建过：漏了后归档的第一场（这份缓存不再被任何章级读者读）
    assert _stored_aggregate(session, chapter_id).content == "第二场（本章最后一场）先起草、先归档。"
    detail = ChapterManuscriptService(session).manuscript_detail(chapter_id)
    assert detail["assembled"]["content"] == "第一场后起草、后归档。\n第二场（本章最后一场）先起草、先归档。"

    quality = _quality_chapter_source(session, chapter_id)
    assert quality["text_layer"] == "chapter_assembled"
    assert quality["content"] == "第一场后起草、后归档。\n\n第二场（本章最后一场）先起草、先归档。"
    item = _overview_chapter_item(client, chapter_id)
    assert item["text_layer"] == "chapter_assembled"
    assert item["source_ref"] == f"chapter_assembled:{chapter_id}"

    near_final = NearFinalAcceptanceService(session)._chapter_source(session.get(ChapterGoal, chapter_id))
    assert near_final["source_text_ref"] == f"chapter_assembled:{chapter_id}"
    assert near_final["content"] == "第一场后起草、后归档。\n\n第二场（本章最后一场）先起草、先归档。"

    # 显式挑「章记忆终稿」这一层也是读时现拼：这一章此刻归档过的各场记忆，按场序
    memory_layer = _quality_chapter_source(session, chapter_id, "chapter_memory_final")
    assert memory_layer["text_layer"] == "chapter_memory_final"
    assert memory_layer["content"] == "第一场后起草、后归档。\n第二场（本章最后一场）先起草、先归档。"


def test_chapter_readers_follow_a_scene_reorder(client, session) -> None:
    _project_id, chapter_id, (first, second) = _seed_chapter(session, "REORDER", 2)
    _pipeline_archive(session, first, "第一场正文。")
    _pipeline_archive(session, second, "第二场正文。")
    assert _stored_aggregate(session, chapter_id).content == "第一场正文。\n第二场正文。"

    CatalogService(session).reorder_scenes(chapter_id, {"scene_ids": [second, first], "last_scene_id": first})
    session.commit()

    detail = ChapterManuscriptService(session).manuscript_detail(chapter_id)
    assert detail["assembled"]["content"] == "第二场正文。\n第一场正文。"
    assert _quality_chapter_source(session, chapter_id)["content"] == "第二场正文。\n\n第一场正文。"
    near_final = NearFinalAcceptanceService(session)._chapter_source(session.get(ChapterGoal, chapter_id))
    assert near_final["content"] == "第二场正文。\n\n第一场正文。"
    assert _quality_chapter_source(session, chapter_id, "chapter_memory_final")["content"] == "第二场正文。\n第一场正文。"
    assert _overview_chapter_item(client, chapter_id, "chapter_memory_final")["text_layer"] == "chapter_memory_final"
