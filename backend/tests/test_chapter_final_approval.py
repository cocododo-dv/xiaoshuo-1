"""终稿定稿：作者亲手写 / 逐场采纳的章也能定稿（B08-01），定稿绑定的是当前各场终稿现拼的正文（R13），
「已通读」可以随「确认定稿」一次提交并绑定读到的正文哈希（B08-20，批准 #10）。

过去通读包只在项目停在「本章终审」（只有「运行本章」会置这个状态）时才给：作者在写作台写完、逐场晋升的章，
点「批准为终稿」永远 409 CHAPTER_FINAL_READ_CONFIRM_UNAVAILABLE；旧测试是手工把项目状态改成终审才走通的。
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    OperationLog,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.chapter_final_flow import ProjectChapterFlowService
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.errors import DomainError


_sequence = 0


def _post(client, path: str, payload: dict | None = None, *, expect: int = 200):
    global _sequence
    _sequence += 1
    response = client.post(path, json=payload or {}, headers={"X-Idempotency-Key": f"final-approval-{_sequence}"})
    assert response.status_code == expect, response.text
    return response


def _author_written_chapter(client, session, *, scene_texts: list[str]) -> dict:
    """作者路径：建作品 → 目录建章建场 → 写作台保存正文 → 晋升为权威正文 → 核对正史。项目状态始终不是「本章终审」。"""
    project_id = _post(
        client, "/api/v2/projects", {"title": "雨城旧信", "outline_text": "林昭回到雨城，翻开一卷旧案。"}
    ).json()["data"]["project"]["project_id"]
    chapter = _post(
        client,
        f"/api/v2/projects/{project_id}/catalog/chapters",
        {"title": "旧信", "current": True, "with_scene": False},
    ).json()["data"]["chapter"]
    chapter_id = chapter["chapter_id"]
    scene_ids = []
    for index, _text in enumerate(scene_texts, start=1):
        scene = _post(
            client, f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes", {"title": f"第{index}场"}
        ).json()["data"]["scene"]
        scene_ids.append(scene["scene_id"])
    for scene_id, text in zip(scene_ids, scene_texts):
        draft = _post(client, f"/api/v1/author-drafts/scene/{scene_id}/ensure").json()["data"]["draft"]
        saved = client.patch(
            f"/api/v1/author-drafts/{draft['draft_id']}",
            json={"content": f"<p>{text}</p>", "base_revision_no": draft["revision_no"]},
        )
        assert saved.status_code == 200, saved.text
        promoted = _post(
            client,
            f"/api/v1/author-drafts/{draft['draft_id']}/promote-canonical",
            {
                "base_revision_no": saved.json()["data"]["draft"]["revision_no"],
                "expected_current_final_scene_row_id": None,
                "narrative_effect": "requires_reconcile",
            },
        )
        assert promoted.json()["data"]["final_scene_row_id"]
    canon = CanonContinuityService(session)
    for scene_id in scene_ids:
        canon.verify_scene_complete(project_id, scene_id, actor_ref="author", note="作者核对过本场正史。")
    session.commit()
    project = session.get(StoryProject, project_id)
    assert project is not None and project.status != "chapter_final_review"
    return {"project_id": project_id, "chapter_id": chapter_id, "scene_ids": scene_ids}


def _assembled_hash(session, chapter_id: str) -> str:
    content = ChapterManuscriptService(session).manuscript_detail(chapter_id)["assembled"]["content"]
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_author_written_chapter_can_be_read_confirmed_and_approved_without_a_chapter_run(client, session) -> None:
    seeded = _author_written_chapter(client, session, scene_texts=["林昭在雨里拆开旧信。", "案卷的最后一页被人撕走了。"])
    base = f"/api/v1/projects/{seeded['project_id']}/chapters/{seeded['chapter_id']}"

    confirmed = _post(client, f"{base}/read-confirm", {"note": "通读过整章。"}).json()["data"]
    assert confirmed["body_hash"] == _assembled_hash(session, seeded["chapter_id"])

    approved = _post(client, f"{base}/approve-final", {"revision_notes": "定稿。"}).json()["data"]
    assert approved["approved_chapter_id"] == seeded["chapter_id"]
    assert approved["approval_note"]["body_hash"] == confirmed["body_hash"]
    session.expire_all()
    assert session.get(ChapterGoal, seeded["chapter_id"]).state == "approved"
    assert session.get(StoryProject, seeded["project_id"]).status == "completed"


def test_approval_keeps_the_linear_rule_for_author_written_chapters(client, session) -> None:
    seeded = _author_written_chapter(client, session, scene_texts=["林昭把旧信压在案卷下面。"])
    later = _post(
        client,
        f"/api/v2/projects/{seeded['project_id']}/catalog/chapters",
        {"title": "后一章", "current": False, "with_scene": False},
    ).json()["data"]["chapter"]
    response = _post(
        client,
        f"/api/v1/projects/{seeded['project_id']}/chapters/{later['chapter_id']}/approve-final",
        {"read_confirmation": {"body_hash": "0" * 64}},
        expect=409,
    )
    assert response.json()["error"]["code"] == "PROJECT_CHAPTER_NOT_CURRENT"


def test_read_confirmation_binds_the_assembled_finals_not_a_stale_aggregate(client, session) -> None:
    """R13 探针 c：流水线只在章末一场归档时重建章汇总。最后一场先归档、前面一场后归档，汇总就漏了前面那场；
    定稿绑定的必须是成稿中心读到的逐场终稿，而不是这份过期汇总。"""
    project_id, chapter_id = "PRJ_FINAL_R13", "PRJ_FINAL_R13_CH01"
    session.add(
        StoryProject(
            project_id=project_id,
            title="雨城案卷",
            outline_text="旧案。",
            current_chapter_id=chapter_id,
            approved_chapter_ids_json=[],
            # 流水线（运行本章）走到终审时的状态：这里验的是正文取哪一份，与状态闸门无关
            status="chapter_final_review",
        )
    )
    session.flush()
    session.add(ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=2, display_order=1, chapter_goal="旧案"))
    session.flush()
    session.add(ChapterState(chapter_id=chapter_id, aggregate_block_reason="none"))
    for seq in (1, 2):
        session.add(
            SceneCard(
                scene_id=f"{chapter_id}_SC0{seq}",
                chapter_id=chapter_id,
                project_id=project_id,
                scene_seq=seq,
                scene_goal="旧案",
                is_chapter_last=1 if seq == 2 else 0,
            )
        )
    session.flush()

    def pipeline_archive(seq: int, text: str) -> None:
        scene_id, final_id = f"{chapter_id}_SC0{seq}", f"final_{chapter_id}_SC0{seq}_v1"
        session.add(SceneRunState(scene_id=scene_id, scene_status="ready", current_final_scene_row_id=final_id))
        session.add(
            FinalScene(
                row_id=final_id,
                scene_id=scene_id,
                chapter_id=chapter_id,
                content=text,
                status="draft",
                source_bundle_id="bundle_r13",
                source_bundle_hash="hash_r13",
            )
        )
        session.flush()
        Archiver(session).archive_final_scene(scene_id, final_id, carry_notes_json=[])
        if session.get(SceneCard, scene_id).is_chapter_last == 1:
            Aggregator(session).run_final_aggregate(chapter_id)
        CanonContinuityService(session).verify_scene_complete(project_id, scene_id, actor_ref="author", note="核对过。")
        session.commit()

    pipeline_archive(2, "第二场（本章最后一场）先起草、先归档。")
    pipeline_archive(1, "第一场后起草、后归档。")
    detail = ChapterManuscriptService(session).manuscript_detail(chapter_id)
    assert detail["comparison_status"] == "aggregate_differs_current"
    assert "第一场" in detail["assembled"]["content"]

    confirmed = _post(client, f"/api/v1/projects/{project_id}/chapters/{chapter_id}/read-confirm").json()["data"]
    assert confirmed["body_hash"] == _assembled_hash(session, chapter_id)
    assert confirmed["body_source"] == "assembled"
    assert detail["body_hash"] == confirmed["body_hash"]
    packet = client.get(f"/api/v1/projects/{project_id}/dashboard").json()["data"]["review_packet"]
    assert "第一场" in packet["body"]


def test_approve_final_folds_the_read_confirmation_and_binds_the_body_hash(client, session) -> None:
    seeded = _author_written_chapter(client, session, scene_texts=["林昭在旧信背面找到一行铅笔字。"])
    base = f"/api/v1/projects/{seeded['project_id']}/chapters/{seeded['chapter_id']}"
    detail = client.get(f"/api/v1/chapter-manuscripts/{seeded['chapter_id']}").json()["data"]
    assert detail["body_hash"] == _assembled_hash(session, seeded["chapter_id"])

    stale = _post(
        client,
        f"{base}/approve-final",
        {"read_confirmation": {"body_hash": "f" * 64, "note": "读的是旧版。"}},
        expect=409,
    )
    error = stale.json()["error"]
    assert error["code"] == "CHAPTER_FINAL_BODY_CHANGED"
    assert error["details"]["body_hash"] == detail["body_hash"]
    session.expire_all()
    assert session.get(ChapterGoal, seeded["chapter_id"]).state != "approved"

    approved = _post(
        client,
        f"{base}/approve-final",
        {"revision_notes": "定稿。", "read_confirmation": {"body_hash": detail["body_hash"], "note": "通读过整章。"}},
    ).json()["data"]
    assert approved["approval_note"]["body_hash"] == detail["body_hash"]
    events = session.execute(
        select(OperationLog.event_type, OperationLog.payload_json)
        .where(OperationLog.object_ref == seeded["chapter_id"], OperationLog.object_type == "chapter")
        .order_by(OperationLog.operation_id.asc())
    ).all()
    kinds = [event_type for event_type, _ in events]
    assert kinds == ["chapter_final_read_confirmed", "chapter_final_approval"]
    read_payload = events[0][1]
    assert read_payload["body_hash"] == detail["body_hash"]
    assert read_payload["note"] == "通读过整章。"
    assert events[1][1]["read_confirmed_at"] == read_payload["confirmed_at"]


def test_approve_final_refuses_a_folded_read_confirmation_without_the_read_body_hash(client, session) -> None:
    """「已通读」随「确认定稿」一次提交时必须带上作者读到的那一份的哈希：不带就什么都没绑，等于没读过。

    HTTP 层的请求模型早就 422；这里守的是服务层本身——过去 ``{"read_confirmation": {}}`` 直接调服务会
    记一条「已通读」并把一章从没通读确认过的正文定稿。
    """
    seeded = _author_written_chapter(client, session, scene_texts=["林昭把旧信压回案卷最底下。"])
    project_id, chapter_id = seeded["project_id"], seeded["chapter_id"]
    http = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/approve-final",
        json={"read_confirmation": {}},
        headers={"X-Idempotency-Key": "final-approval-folded-without-hash"},
    )
    assert http.status_code == 422, http.text

    service = ProjectChapterFlowService(session)
    for folded in ({}, {"note": "读过了。"}, {"body_hash": "   ", "note": "读过了。"}, {"body_hash": None}):
        with pytest.raises(DomainError) as refused:
            service.approve_final(project_id, chapter_id, {"read_confirmation": folded})
        assert (refused.value.code, refused.value.status_code) == ("CHAPTER_READ_CONFIRM_INVALID", 400), folded
    session.flush()
    session.expire_all()
    assert session.get(ChapterGoal, chapter_id).state != "approved"
    project = session.get(StoryProject, project_id)
    assert project.current_chapter_id == chapter_id
    assert list(project.approved_chapter_ids_json or []) == []
    recorded = session.execute(
        select(OperationLog.event_type).where(OperationLog.object_type == "chapter", OperationLog.object_ref == chapter_id)
    ).scalars().all()
    assert "chapter_final_read_confirmed" not in recorded and "chapter_final_approval" not in recorded
