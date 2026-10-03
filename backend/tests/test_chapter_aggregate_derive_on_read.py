"""章汇总读时现拼（重评 R13 + 主管补充，[批准#21]）：章级读者一律读各场当前终稿现拼，存下来的章汇总（ChapterMemory）
只是一份派生缓存——

- 回收站里的场不在这一章里：它的场景记忆不算进章汇总，也就不再挡住同一章别的场晋升（探针 a）；
- 流水线先归档章末一场、后归档前面的场时，汇总漏掉前面那场；场序重排后汇总还是旧顺序——章级读者（文学质量的
  章源、章级准终稿评审）都不读它（探针 b、复核的重排探针）；
- 晋升照旧重建章汇总，重建不成只记日志、不再 409；同一修订的重放判断不再要求章汇总逐字一致。

探针原稿：``scratch/reeval/r13/*.py`` 与 ``scratch/reeval/g5_critic/probe_reorder_stale_aggregate.py``。
"""

from __future__ import annotations

import hashlib
from itertools import permutations
import logging
import time

import pytest

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    OperationLog,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryProject,
)
from novel_system.services.aggregator import Aggregator, is_chapter_aggregate_of
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
    session.add(ChapterState(chapter_id=chapter_id))
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
    # 第三场（章末）还没有正文：不在汇总里，也不挡汇总
    _project_id, chapter_id, (first, second, _last) = _seed_chapter(session, "TRASH_AGG", 3)
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

    # 章汇总读时现拼（重建存下来的那一份、归档时都按它）：这一章此刻归档过的各场记忆，按场序
    derived = Aggregator(session).derive_final_aggregate(chapter_id)
    assert derived.status == "derived"
    assert derived.content == "第一场后起草、后归档。\n第二场（本章最后一场）先起草、先归档。"


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
    assert Aggregator(session).derive_final_aggregate(chapter_id).content == "第二场正文。\n第一场正文。"


# ---------------------------------------------------------------------------------------------- 晋升：汇总重建不成只记日志，重放不看汇总


def _seed_promotable_scene(session, key: str) -> dict[str, str]:
    """两场的一章：第一场有待晋升的作者稿（第 2 版），当前权威正文已归档、事实已核对。"""
    project_id, chapter_id, (first, second) = _seed_chapter(session, key, 2)
    final_id = f"final_{first}_v1"
    text = "旧权威正文。"
    session.add(
        SceneRunState(
            scene_id=first,
            scene_status="archived",
            current_final_scene_row_id=final_id,
            narrative_sync_status="synced",
            narrative_sync_final_scene_row_id=final_id,
        )
    )
    session.add(
        FinalScene(
            row_id=final_id,
            scene_id=first,
            chapter_id=chapter_id,
            content=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{first}",
            source_bundle_hash=f"hash_{first}",
            source_kind="generation",
        )
    )
    draft_id = f"author_draft_scene_{first}"
    session.add(
        AuthorDraft(
            draft_id=draft_id,
            object_type="scene",
            object_id=first,
            source_text_ref=f"final_scene:{final_id}",
            content="<p>林昭在雨城的案卷里夹了一张便条。</p>",
            revision_no=2,
            status="current",
        )
    )
    session.flush()
    Archiver(session).archive_final_scene(first, final_id)
    CanonContinuityService(session).verify_scene_complete(
        project_id, first, actor_ref="author", note="核对过。", expected_final_scene_row_id=final_id
    )
    assert Aggregator(session).run_final_aggregate(chapter_id)["status"] == "created"
    session.commit()
    return {
        "project_id": project_id,
        "chapter_id": chapter_id,
        "scene_id": first,
        "sibling_id": second,
        "final_id": final_id,
        "draft_id": draft_id,
    }


def _promote(client, seeded: dict[str, str], key: str, expected_final_id: str | None = None):
    return client.post(
        f"/api/v1/author-drafts/{seeded['draft_id']}/promote-canonical",
        json={
            "base_revision_no": 2,
            "expected_current_final_scene_row_id": expected_final_id or seeded["final_id"],
            "narrative_effect": "facts_unchanged",
            "accepted_warning_codes": [],
        },
        headers={"X-Idempotency-Key": key},
    )


def test_promotion_logs_a_chapter_aggregate_it_cannot_rebuild_instead_of_refusing(client, session, caplog) -> None:
    seeded = _seed_promotable_scene(session, "AGG_BLOCKED")
    # 同一章另一场有两条都「有效」的场景记忆（旧库里才有的不一致）：章汇总拼不出来
    for suffix in ("a", "b"):
        session.add(
            SceneMemory(
                row_id=f"scene_memory_{seeded['sibling_id']}_{suffix}",
                scene_id=seeded["sibling_id"],
                chapter_id=seeded["chapter_id"],
                content=f"重复的记忆 {suffix}",
                source_bundle_id=f"bundle_{suffix}",
                final_scene_row_id=f"final_{suffix}",
                active_flag=1,
            )
        )
    session.commit()
    stale = _stored_aggregate(session, seeded["chapter_id"])

    with caplog.at_level(logging.WARNING, logger="novel_system.services.canonical_manuscripts"):
        response = _promote(client, seeded, "r13-aggregate-blocked")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["chapter_memory_row_id"] is None
    assert any("active_scene_memory_ambiguous" in record.getMessage() for record in caplog.records)
    session.expire_all()
    # 正文照常发布：新一版权威正文、指针、草稿的晋升记录
    new_final = session.get(FinalScene, data["final_scene_row_id"])
    assert new_final is not None and new_final.content == "林昭在雨城的案卷里夹了一张便条。"
    assert session.get(SceneRunState, seeded["scene_id"]).current_final_scene_row_id == new_final.row_id
    assert session.get(AuthorDraft, seeded["draft_id"]).last_promoted_revision_no == 2
    # 汇总没动（拼不出来就不拼），卷汇总跟着章汇总走、也不做；审计里记着这两件事
    assert _stored_aggregate(session, seeded["chapter_id"]).row_id == stale.row_id
    log = session.query(OperationLog).filter_by(
        event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
    ).one()
    assert log.payload_json["chapter_aggregate"] == "blocked"
    assert log.payload_json["volume_aggregate"] == "skipped"


def test_same_revision_replay_does_not_require_an_exact_chapter_aggregate(client, session) -> None:
    seeded = _seed_promotable_scene(session, "REPLAY")
    first = _promote(client, seeded, "r13-replay-first")
    assert first.status_code == 200, first.text
    author_final_id = first.json()["data"]["final_scene_row_id"]

    # 同一章的另一场随后归档、没有重建章汇总（流水线归档不是章末的场就是这样）：存下来的汇总落后于逐场终稿
    _pipeline_archive(session, seeded["sibling_id"], "第二场：林昭把便条折好。", chapter_last_rebuilds=False)
    assert _stored_aggregate(session, seeded["chapter_id"]).content == "林昭在雨城的案卷里夹了一张便条。"
    session.expire_all()
    counts_before = {
        "finals": session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memories": session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count(),
        "chapter_memories": session.query(ChapterMemory).filter_by(chapter_id=seeded["chapter_id"]).count(),
        "logs": session.query(OperationLog).filter_by(
            event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
        ).count(),
    }

    again = _promote(client, seeded, "r13-replay-second-key", expected_final_id=author_final_id)

    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert data["already_current"] is True
    assert data["derivation_reused"] is True
    assert data["final_scene_row_id"] == author_final_id
    session.expire_all()
    assert {
        "finals": session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memories": session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count(),
        "chapter_memories": session.query(ChapterMemory).filter_by(chapter_id=seeded["chapter_id"]).count(),
        "logs": session.query(OperationLog).filter_by(
            event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
        ).count(),
    } == counts_before


# ------------------------------------------------------------ 章汇总的复验：恰好是这几场各一次、用换行拼起来（复核 A1-R1）


@pytest.mark.parametrize(
    ("content", "parts"),
    [
        pytest.param("林昭先读了旧信。\n雨城的钟敲过三下。", ["雨城的钟敲过三下。", "林昭先读了旧信。"], id="order-differs"),
        pytest.param(
            "林昭说：走吧。雨停了。\n第二场，她把旧信收进案卷。\n雨停了。",
            ["林昭说：走吧。雨停了。", "第二场，她把旧信收进案卷。", "雨停了。"],
            id="later-scene-inside-earlier",
        ),
        pytest.param("林昭拆开旧信。雨一直下。\n林昭拆开旧信。", ["林昭拆开旧信。", "林昭拆开旧信。雨一直下。"], id="later-scene-opens-earlier"),
        pytest.param("第一段。\n第二段。\n第一段。", ["第一段。", "第一段。\n第二段。"], id="multi-line-prefix"),
        pytest.param("旧信。\n旧信。", ["旧信。", "旧信。"], id="identical-scenes"),
        # 先试最长的一段会走进死路，得退回来换一段
        pytest.param("甲\n乙\n丙\n甲\n乙", ["甲", "甲\n乙", "乙\n丙"], id="backtracks"),
    ],
)
def test_the_aggregate_check_accepts_every_exact_join_of_its_inputs(content: str, parts: list[str]) -> None:
    assert is_chapter_aggregate_of(content, parts)


@pytest.mark.parametrize(
    ("content", "parts"),
    [
        pytest.param(
            "林昭先读了旧信。\n雨城的钟敲过三下。\n案卷里没有的一段。",
            ["雨城的钟敲过三下。", "林昭先读了旧信。"],
            id="extra-text",
        ),
        pytest.param("林昭先读了旧信。", ["雨城的钟敲过三下。", "林昭先读了旧信。"], id="missing-scene"),
        pytest.param("林昭先读了旧信。\n雨城的钟敲过四下。", ["雨城的钟敲过三下。", "林昭先读了旧信。"], id="same-length-edit"),
        pytest.param("林昭先读了旧信。 雨城的钟敲过三下。", ["雨城的钟敲过三下。", "林昭先读了旧信。"], id="wrong-separator"),
        pytest.param("旧信。\n旧信。", ["旧信。", "案卷。"], id="one-scene-twice"),
        pytest.param("甲\n甲\n甲\n丁", ["甲", "甲\n甲", "乙"], id="dead-end-everywhere"),
    ],
)
def test_the_aggregate_check_rejects_anything_else(content: str, parts: list[str]) -> None:
    assert not is_chapter_aggregate_of(content, parts)


def test_the_aggregate_check_is_order_free_even_when_scenes_contain_each_other() -> None:
    """同一组场（互相含着、互为开头、有一场重复）按任何次序拼都认；改掉最后一个字就不认。"""
    parts = ["雨停了。", "林昭说：走吧。雨停了。", "雨停了。\n她把旧信收进案卷。", "雨停了。"]
    for order in permutations(parts):
        joined = "\n".join(order)
        assert is_chapter_aggregate_of(joined, parts), order
        assert not is_chapter_aggregate_of(f"{joined[:-1]}！", parts), order


def test_the_aggregate_check_compares_the_given_order_before_searching() -> None:
    """复核 I4-R1：先按给的次序拼一次比（第 8 步自检按现在的场序给），比上了就不必逐段对；逐段对走满步数就当不是
    （fail closed）。"""
    parts = ["林昭先读了旧信。", "雨城的钟敲过三下。", "她把旧信收进案卷。"]
    joined = "\n".join(parts)
    assert is_chapter_aggregate_of(joined, parts, max_steps=0)
    assert is_chapter_aggregate_of(joined, parts[::-1])
    assert not is_chapter_aggregate_of(joined, parts[::-1], max_steps=0)


def test_the_aggregate_check_gives_up_quickly_on_a_forged_chain_of_openings() -> None:
    """复核 I4-R1：各段本身带换行、一段正是另一段用换行接着写下去的开头（「雨」「雨\\n雨」……共 18 段），汇总改掉最后
    一个字。拼得出同一个开头的组合随段数成倍增长，以前要把它们试遍才答「不是」（18 段约 22 秒，每多两段慢 4 倍多）；
    现在逐段对有步数上限，走满就当不是。对的汇总不论给的次序，照样一下认出。"""
    parts = ["\n".join(["雨"] * count) for count in range(1, 19)]
    joined = "\n".join(parts)

    started = time.perf_counter()
    assert not is_chapter_aggregate_of(f"{joined[:-1]}晴", parts)
    assert time.perf_counter() - started < 3
    assert is_chapter_aggregate_of(joined, parts[::-1])
