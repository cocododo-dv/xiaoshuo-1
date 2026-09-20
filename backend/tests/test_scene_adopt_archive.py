"""Wave 1（结果闭环治理 §5.2）：作者采纳归档单入口 + 归档状态词表统一。

完成门可复算证明：前端置 done 的唯一合法路径是本端点的成功响应——
归档后必须存在可回放的后端 FinalScene（status=archived），章节聚合
（chapter_manuscripts，以 FinalScene 为源）能取到全文；缓存清除不丢稿。
"""

from __future__ import annotations

import hashlib

import pytest

from novel_system.db.models import (
    AttemptTracker,
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneMemory,
    SceneBundle,
    SceneDraft,
    SceneRunState,
    StyleReferenceBook,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.archiver import Archiver
from novel_system.services.errors import DomainError
from tests.test_chapter_manuscripts import _create_chapter, _create_scene


def _seed_style_draft(session, scene_id: str, chapter_id: str, *, content: str, row_id: str | None = None) -> str:
    draft_row_id = row_id or f"draft_style_{scene_id}_v1"
    session.add(
        SceneDraft(
            row_id=draft_row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            stage="style",
            content=content,
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash=f"hash_{scene_id}",
        )
    )
    state = session.get(SceneRunState, scene_id)
    assert state is not None, "scenes POST 应已建运行态行"
    state.scene_status = "human_review_required"
    state.current_style_draft_row_id = draft_row_id
    session.commit()
    return draft_row_id


def test_adopt_promotes_style_draft_to_archived_final(client, session):
    _create_chapter(client, "chapter_adopt_1")
    _create_scene(client, "scene_adopt_1", chapter_id="chapter_adopt_1", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_1", "chapter_adopt_1", content="潮水退去，她看清了闸门上的名字。")

    response = client.post(
        "/api/v1/scenes/scene_adopt_1/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-1"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scene_status"] == "archived"
    final_row_id = data["final_scene_row_id"]
    assert final_row_id

    final = session.get(FinalScene, final_row_id)
    assert final is not None
    # 状态词表统一：归档事务写入的权威态是 archived，不再依赖 approved 的字符串巧合
    assert final.status == "archived"
    assert final.content == "潮水退去，她看清了闸门上的名字。"

    # 完成门：归档稿可从 workbench 回放
    workbench = client.get("/api/v1/scenes/scene_adopt_1/workbench").json()["data"]
    assert workbench["final_scene"]["content"] == "潮水退去，她看清了闸门上的名字。"
    assert workbench["author_state"]["author_state"] == "archived"

    # 完成门：章节聚合（FinalScene 为源）取到全文——清除任何前端缓存都不影响
    detail = client.get("/api/v1/chapter-manuscripts/chapter_adopt_1").json()["data"]
    assert "潮水退去" in detail["assembled"]["content"]


def test_adopt_exact_author_revision_archives_the_submitted_text_not_stale_pipeline_draft(
    client,
    session,
):
    """浏览器当前稿必须与权威 FinalScene 是同一份修订，不能由服务端另选旧管线稿。"""

    _create_chapter(client, "chapter_adopt_exact")
    _create_scene(
        client,
        "scene_adopt_exact",
        chapter_id="chapter_adopt_exact",
        scene_seq=1,
    )
    _seed_style_draft(
        session,
        "scene_adopt_exact",
        "chapter_adopt_exact",
        content="这是服务端仍指向的旧管线稿。",
    )
    ensured = client.post(
        "/api/v1/author-drafts/scene/scene_adopt_exact/ensure",
        json={},
        headers={"X-Idempotency-Key": "adopt-exact-ensure"},
    )
    assert ensured.status_code == 200, ensured.text
    author_draft = ensured.json()["data"]["draft"]

    response = client.post(
        "/api/v1/scenes/scene_adopt_exact/adopt-current",
        json={
            "accepted_warning_codes": [],
            "exact_author_draft": {
                "draft_id": author_draft["draft_id"],
                "base_revision_no": author_draft["revision_no"],
                "expected_current_final_scene_row_id": None,
                "content": "<p>这是作者在浏览器中明确选中的正文。</p>",
            },
        },
        headers={"X-Idempotency-Key": "adopt-exact"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    final = session.get(FinalScene, data["final_scene_row_id"])
    saved = session.get(AuthorDraft, author_draft["draft_id"])
    assert final is not None
    assert final.content == "这是作者在浏览器中明确选中的正文。"
    assert final.content != "这是服务端仍指向的旧管线稿。"
    assert final.source_kind == "author_draft"
    assert final.source_author_draft_id == author_draft["draft_id"]
    assert final.source_author_draft_revision_no == data["draft_revision_no"]
    assert saved is not None and saved.content == "<p>这是作者在浏览器中明确选中的正文。</p>"
    assert saved.revision_no == data["draft_revision_no"]
    assert data["author_draft"]["revision_no"] == data["draft_revision_no"]
    assert data["author_draft"]["canonical_dirty"] is False
    assert data["canon_continuity"] == {
        "status": "unavailable",
        "complete": False,
        "reason": "projectless_legacy_scene",
    }


def test_adopt_without_any_draft_409(client, session):
    _create_chapter(client, "chapter_adopt_2")
    _create_scene(client, "scene_adopt_2", chapter_id="chapter_adopt_2", scene_seq=1)

    response = client.post(
        "/api/v1/scenes/scene_adopt_2/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-2"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_VALID_DRAFT"


def test_adopt_idempotent_replay_and_already_archived(client, session):
    _create_chapter(client, "chapter_adopt_3")
    _create_scene(client, "scene_adopt_3", chapter_id="chapter_adopt_3", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_3", "chapter_adopt_3", content="第一次归档正文。")

    first = client.post(
        "/api/v1/scenes/scene_adopt_3/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-3"},
    )
    assert first.status_code == 200
    first_row = first.json()["data"]["final_scene_row_id"]

    # 同幂等键重放：同结果，不建第二行
    replay = client.post(
        "/api/v1/scenes/scene_adopt_3/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-3"},
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["final_scene_row_id"] == first_row

    # 已归档后换新幂等键再采纳：幂等返回已归档结果，不重复归档
    again = client.post(
        "/api/v1/scenes/scene_adopt_3/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-3-bis"},
    )
    assert again.status_code == 200
    assert again.json()["data"]["scene_status"] == "archived"
    assert again.json()["data"]["final_scene_row_id"] == first_row
    assert again.json()["data"]["safe_to_archive"] is True
    assert again.json()["data"]["literary_warnings_unresolved"] is False
    assert again.json()["data"]["author_confirmed_final"] is True
    finals = session.query(FinalScene).filter(FinalScene.scene_id == "scene_adopt_3").all()
    assert len(finals) == 1


def test_adopt_source_safety_blocked_keeps_draft(client, session, monkeypatch):
    """设计红线 8：来源安全未通过时草稿可保存，但不能标记为已安全归档。"""
    monkeypatch.setenv("NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON", '["路明非"]')
    _create_chapter(client, "chapter_adopt_4")
    _create_scene(client, "scene_adopt_4", chapter_id="chapter_adopt_4", scene_seq=1)
    draft_row_id = _seed_style_draft(
        session, "scene_adopt_4", "chapter_adopt_4",
        content="他抬起头，看见路明非站在门口。",  # PROTECTED_SOURCE_TERMS 保护词
    )

    response = client.post(
        "/api/v1/scenes/scene_adopt_4/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-4"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_SAFETY_BLOCKED"

    # 草稿保留、未归档
    session.expire_all()
    assert session.get(SceneDraft, draft_row_id) is not None
    state = session.get(SceneRunState, "scene_adopt_4")
    assert state.scene_status != "archived"
    finals = session.query(FinalScene).filter(FinalScene.scene_id == "scene_adopt_4").all()
    assert not [f for f in finals if f.status == "archived"]


def test_adopt_content_safety_requires_exact_acknowledgement_and_audits_it(
    client,
    session,
    monkeypatch,
):
    _create_chapter(client, "chapter_adopt_content_safety")
    _create_scene(
        client,
        "scene_adopt_content_safety",
        chapter_id="chapter_adopt_content_safety",
        scene_seq=1,
    )
    _seed_style_draft(
        session,
        "scene_adopt_content_safety",
        "chapter_adopt_content_safety",
        content="角色只有16岁，段落明确描写两人的性行为。",
    )
    monkeypatch.setattr(
        "novel_system.services.final_text_gate.ReferenceSafetyService.scan_runtime_text",
        lambda *args, **kwargs: {"safe": True, "matches": []},
    )

    blocked = client.post(
        "/api/v1/scenes/scene_adopt_content_safety/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-content-safety-blocked"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CONTENT_SAFETY_REVIEW_REQUIRED"

    accepted = client.post(
        "/api/v1/scenes/scene_adopt_content_safety/adopt-current",
        json={
            "accepted_warning_codes": [
                "sexual_content_with_minor_indicators",
            ]
        },
        headers={"X-Idempotency-Key": "adopt-content-safety-accepted"},
    )
    assert accepted.status_code == 200
    final_id = accepted.json()["data"]["final_scene_row_id"]
    attempt = session.query(AttemptTracker).filter(
        AttemptTracker.scene_id == "scene_adopt_content_safety",
        AttemptTracker.step == "archive",
    ).one()
    gate = attempt.details_json["final_text_gate"]
    assert gate["content_safety"]["acknowledged_codes"] == [
        "sexual_content_with_minor_indicators"
    ]
    assert gate["content_hash"] == session.get(FinalScene, final_id).content_hash


def test_adopt_rejects_unbounded_or_unknown_request_fields(client):
    _create_chapter(client, "chapter_adopt_request_contract")
    _create_scene(
        client,
        "scene_adopt_request_contract",
        chapter_id="chapter_adopt_request_contract",
        scene_seq=1,
    )

    unknown = client.post(
        "/api/v1/scenes/scene_adopt_request_contract/adopt-current",
        json={"accepted_warning_codes": [], "bypass": True},
        headers={"X-Idempotency-Key": "adopt-request-unknown"},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"

    oversized = client.post(
        "/api/v1/scenes/scene_adopt_request_contract/adopt-current",
        json={"accepted_warning_codes": ["x" * 129]},
        headers={"X-Idempotency-Key": "adopt-request-oversized"},
    )
    assert oversized.status_code == 422
    assert oversized.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_adopt_blocks_dynamic_term_from_bound_reference_profile(client, session):
    _create_chapter(client, "chapter_adopt_dynamic")
    _create_scene(client, "scene_adopt_dynamic", chapter_id="chapter_adopt_dynamic", scene_seq=1)
    draft_row_id = _seed_style_draft(
        session,
        "scene_adopt_dynamic",
        "chapter_adopt_dynamic",
        content="Professor Meridian arrived with a different archive key.",
    )
    session.add(
        StyleReferenceBook(
            book_id="refbook_dynamic_adopt",
            title="Public source",
            source_kind="path",
            cloud_policy="local_only",
            text_checksum="dynamic-adopt-checksum",
        )
    )
    session.flush()
    session.add(
        StyleReferenceRun(
            run_id="run_dynamic_adopt",
            book_id="refbook_dynamic_adopt",
            status="done",
            phase="done",
        )
    )
    session.flush()
    session.add(
        StyleReferenceProfile(
            profile_id="refprofile_dynamic_adopt",
            book_id="refbook_dynamic_adopt",
            run_id="run_dynamic_adopt",
            title="Dynamic safety",
            status="active",
            profile_json={
                "source_safety": {
                    "ready": True,
                    "profile_id": "refprofile_dynamic_adopt",
                    "protected_terms": ["Professor Meridian"],
                    "distinctive_phrases": [],
                    "scene_bridges": [],
                }
            },
        )
    )
    bundle = SceneBundle(
        bundle_id="bundle_scene_adopt_dynamic",
        scene_id="scene_adopt_dynamic",
        chapter_id="chapter_adopt_dynamic",
        bundle_snapshot_hash="hash_scene_adopt_dynamic",
        frozen_snapshot_json={
            "source_version_refs": {"reference_profile_ids": ["refprofile_dynamic_adopt"]},
        },
    )
    state = session.get(SceneRunState, "scene_adopt_dynamic")
    state.current_bundle_id = bundle.bundle_id
    state.current_bundle_hash = bundle.bundle_snapshot_hash
    session.add(bundle)
    session.commit()

    response = client.post(
        "/api/v1/scenes/scene_adopt_dynamic/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-dynamic"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_SAFETY_BLOCKED"
    session.expire_all()
    assert session.get(SceneDraft, draft_row_id) is not None
    assert session.get(SceneRunState, "scene_adopt_dynamic").scene_status != "archived"


def test_adopt_promotes_existing_unarchived_final_scene(client, session):
    """管线停在 near_final_ready 的既有 FinalScene：adopt 提升归档它，不建新行。"""
    _create_chapter(client, "chapter_adopt_5")
    _create_scene(client, "scene_adopt_5", chapter_id="chapter_adopt_5", scene_seq=1)
    session.add(
        FinalScene(
            row_id="final_adopt_5_v1",
            scene_id="scene_adopt_5",
            chapter_id="chapter_adopt_5",
            content="近终稿正文。",
            status="near_final_ready",
            source_bundle_id="bundle_adopt_5",
            source_bundle_hash="hash_adopt_5",
        )
    )
    state = session.get(SceneRunState, "scene_adopt_5")
    state.scene_status = "human_review_required"
    state.current_final_scene_row_id = "final_adopt_5_v1"
    session.commit()

    response = client.post(
        "/api/v1/scenes/scene_adopt_5/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-5"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["final_scene_row_id"] == "final_adopt_5_v1"
    session.expire_all()
    assert session.get(FinalScene, "final_adopt_5_v1").status == "archived"
    finals = session.query(FinalScene).filter(FinalScene.scene_id == "scene_adopt_5").all()
    assert len(finals) == 1


def test_adopt_falls_back_to_author_draft(client, session):
    """人工手写场（无管线稿，只有 author-draft 正文）也能走同一归档入口。"""
    _create_chapter(client, "chapter_adopt_6")
    _create_scene(client, "scene_adopt_6", chapter_id="chapter_adopt_6", scene_seq=1)
    ensure = client.post("/api/v1/author-drafts/scene/scene_adopt_6/ensure", json={})
    assert ensure.status_code == 200
    draft_id = ensure.json()["data"]["draft"]["draft_id"]
    patched = client.patch(
        f"/api/v1/author-drafts/{draft_id}",
        json={"content": "<p>手写的正文段落。</p>", "base_revision_no": ensure.json()["data"]["draft"]["revision_no"]},
    )
    assert patched.status_code == 200

    response = client.post(
        "/api/v1/scenes/scene_adopt_6/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-6"},
    )
    assert response.status_code == 200
    final_row_id = response.json()["data"]["final_scene_row_id"]
    final = session.get(FinalScene, final_row_id)
    assert final is not None
    assert final.status == "archived"
    assert "手写的正文段落" in final.content


def test_archiver_marks_final_scene_archived(session):
    """单元级：归档事务统一写 FinalScene.status=archived（词表统一）。"""
    session.add(
        ChapterGoal(
            chapter_id="chapter_unit_1",
            planned_scene_count=1,
            chapter_goal="Archive one valid scene",
        )
    )
    session.flush()
    session.add(
        SceneCard(
            scene_id="scene_unit_1",
            chapter_id="chapter_unit_1",
            scene_seq=1,
            scene_goal="Archive the final manuscript",
        )
    )
    session.flush()
    session.add(
        FinalScene(
            row_id="final_unit_1",
            scene_id="scene_unit_1",
            chapter_id="chapter_unit_1",
            content="正文",
            status="near_final_ready",
            source_bundle_id="bundle_u",
            source_bundle_hash="hash_u",
        )
    )
    session.add(SceneRunState(scene_id="scene_unit_1"))
    session.flush()

    result = Archiver(session).archive_final_scene("scene_unit_1", "final_unit_1")
    assert result["scene_status"] == "archived"
    assert session.get(FinalScene, "final_unit_1").status == "archived"


def test_archiver_blocks_unsafe_actual_final_text(client, session, monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON", '["路明非"]')
    _create_chapter(client, "chapter_archive_gate_source")
    _create_scene(
        client,
        "scene_archive_gate_source",
        chapter_id="chapter_archive_gate_source",
        scene_seq=1,
    )
    final = FinalScene(
        row_id="final_archive_gate_source_v1",
        scene_id="scene_archive_gate_source",
        chapter_id="chapter_archive_gate_source",
        content="他抬头看见路明非站在门口。",
        status="near_final_ready",
        source_bundle_id="bundle_archive_gate_source",
        source_bundle_hash="hash_archive_gate_source",
    )
    session.add(final)
    session.flush()

    with pytest.raises(DomainError) as exc_info:
        Archiver(session).archive_final_scene(final.scene_id, final.row_id)

    assert exc_info.value.code == "SOURCE_SAFETY_BLOCKED"
    assert final.status == "near_final_ready"
    assert session.query(SceneMemory).filter(SceneMemory.scene_id == final.scene_id).count() == 0
    assert session.query(AttemptTracker).filter(
        AttemptTracker.scene_id == final.scene_id,
        AttemptTracker.step == "archive",
    ).count() == 0


def test_archiver_fails_closed_when_source_safety_is_unavailable(client, session, monkeypatch):
    _create_chapter(client, "chapter_archive_gate_unavailable")
    _create_scene(
        client,
        "scene_archive_gate_unavailable",
        chapter_id="chapter_archive_gate_unavailable",
        scene_seq=1,
    )
    final = FinalScene(
        row_id="final_archive_gate_unavailable_v1",
        scene_id="scene_archive_gate_unavailable",
        chapter_id="chapter_archive_gate_unavailable",
        content="她在雨里收起信封，转身走向码头。",
        status="near_final_ready",
        source_bundle_id="bundle_archive_gate_unavailable",
        source_bundle_hash="hash_archive_gate_unavailable",
    )
    session.add(final)
    session.flush()

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("scanner offline")

    monkeypatch.setattr(
        "novel_system.services.final_text_gate.ReferenceSafetyService.scan_runtime_text",
        unavailable,
    )

    with pytest.raises(DomainError) as exc_info:
        Archiver(session).archive_final_scene(final.scene_id, final.row_id)

    assert exc_info.value.code == "SOURCE_SAFETY_UNAVAILABLE"
    assert final.content_hash is None
    assert final.status == "near_final_ready"
    assert session.query(SceneMemory).filter(SceneMemory.scene_id == final.scene_id).count() == 0
    assert session.query(AttemptTracker).filter(
        AttemptTracker.scene_id == final.scene_id,
        AttemptTracker.step == "archive",
    ).count() == 0


def test_archiver_blocks_verified_continuity_issue(client, session):
    _create_chapter(client, "chapter_archive_gate_continuity")
    _create_scene(
        client,
        "scene_archive_gate_continuity",
        chapter_id="chapter_archive_gate_continuity",
        scene_seq=1,
    )
    scene = session.get(SceneCard, "scene_archive_gate_continuity")
    scene.forbidden_text = "不可出现的暗号"
    final = FinalScene(
        row_id="final_archive_gate_continuity_v1",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        content="她在墙上写下不可出现的暗号。",
        status="near_final_ready",
        source_bundle_id="bundle_archive_gate_continuity",
        source_bundle_hash="hash_archive_gate_continuity",
    )
    session.add(final)
    session.flush()

    with pytest.raises(DomainError) as exc_info:
        Archiver(session).archive_final_scene(final.scene_id, final.row_id)

    assert exc_info.value.code == "FINAL_TEXT_CONTINUITY_BLOCKED"
    blockers = exc_info.value.details["final_text_gate"]["archive_blockers"]
    assert "continuity:forbidden_text" in blockers
    assert final.status == "near_final_ready"


def test_archiver_is_not_blocked_by_the_legacy_reference_policy_sentence(client, session):
    """2026-09-20 真实故障的后半截：物化写进每张场景卡 forbidden_text 的防抄袭政策句被按顿号拆成禁用词，

    正文里出现「人物」两个字就是 continuity:forbidden_text，归档被拦。政策句不是禁用词表。
    """
    _create_chapter(client, "chapter_archive_gate_policy")
    _create_scene(
        client,
        "scene_archive_gate_policy",
        chapter_id="chapter_archive_gate_policy",
        scene_seq=1,
    )
    scene = session.get(SceneCard, "scene_archive_gate_policy")
    scene.forbidden_text = "不得复制参考书原文表达、人物、设定或桥段。"
    final = FinalScene(
        row_id="final_archive_gate_policy_v1",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        content="这号人物他见得多了。她推开门，雨声从院子里涌了进来。",
        status="near_final_ready",
        source_bundle_id="bundle_archive_gate_policy",
        source_bundle_hash="hash_archive_gate_policy",
    )
    session.add(final)
    session.flush()

    result = Archiver(session).archive_final_scene(final.scene_id, final.row_id)

    gate = result["final_text_gate"]
    assert gate["archivable"] is True
    assert "continuity:forbidden_text" not in gate["archive_blockers"]
    assert final.status == "archived"


def test_archiver_keeps_literary_findings_advisory(client, session):
    _create_chapter(client, "chapter_archive_gate_literary")
    _create_scene(
        client,
        "scene_archive_gate_literary",
        chapter_id="chapter_archive_gate_literary",
        scene_seq=1,
    )
    final = FinalScene(
        row_id="final_archive_gate_literary_v1",
        scene_id="scene_archive_gate_literary",
        chapter_id="chapter_archive_gate_literary",
        content="她看着门。她看着灯。她看着空椅子。最后，一切都变得不同了。",
        status="near_final_ready",
        source_bundle_id="bundle_archive_gate_literary",
        source_bundle_hash="hash_archive_gate_literary",
    )
    session.add(final)
    session.flush()

    result = Archiver(session).archive_final_scene(final.scene_id, final.row_id)

    gate = result["final_text_gate"]
    assert gate["archivable"] is True
    assert gate["auto_promotable"] is False
    assert gate["literary_quality"]["risky_dimensions"]
    assert gate["content_hash"] == hashlib.sha256(final.content.encode("utf-8")).hexdigest()
    assert final.content_hash == gate["content_hash"]
    assert final.status == "archived"
    attempt = session.get(AttemptTracker, result["archive_attempt_id"])
    assert attempt.details_json["final_text_gate"]["content_hash"] == gate["content_hash"]


def test_archiver_blocks_persisted_content_hash_mismatch_before_side_effects(client, session):
    _create_chapter(client, "chapter_archive_gate_hash")
    _create_scene(
        client,
        "scene_archive_gate_hash",
        chapter_id="chapter_archive_gate_hash",
        scene_seq=1,
    )
    final = FinalScene(
        row_id="final_archive_gate_hash_v1",
        scene_id="scene_archive_gate_hash",
        chapter_id="chapter_archive_gate_hash",
        content="她推开门，雨声从院子里涌了进来。",
        content_hash="0" * 64,
        status="near_final_ready",
        source_bundle_id="bundle_archive_gate_hash",
        source_bundle_hash="hash_archive_gate_hash",
    )
    session.add(final)
    session.flush()

    with pytest.raises(DomainError) as exc_info:
        Archiver(session).archive_final_scene(final.scene_id, final.row_id)

    assert exc_info.value.code == "FINAL_SCENE_CONTENT_HASH_MISMATCH"
    assert exc_info.value.details["stored_content_hash"] == "0" * 64
    assert exc_info.value.details["actual_content_hash"] == hashlib.sha256(
        final.content.encode("utf-8")
    ).hexdigest()
    assert final.content_hash == "0" * 64
    assert final.status == "near_final_ready"
    assert session.query(SceneMemory).filter(SceneMemory.scene_id == final.scene_id).count() == 0
    assert session.query(AttemptTracker).filter(
        AttemptTracker.scene_id == final.scene_id,
        AttemptTracker.step == "archive",
    ).count() == 0


def test_adopt_updates_latest_valid_pointer(client, session):
    """归档路径同样维护 latest_valid_draft 指针（§4.3）。"""
    _create_chapter(client, "chapter_adopt_7")
    _create_scene(client, "scene_adopt_7", chapter_id="chapter_adopt_7", scene_seq=1)
    draft_row_id = _seed_style_draft(session, "scene_adopt_7", "chapter_adopt_7", content="指针维护正文。")

    response = client.post(
        "/api/v1/scenes/scene_adopt_7/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-7"},
    )
    assert response.status_code == 200
    session.expire_all()
    state = session.get(SceneRunState, "scene_adopt_7")
    assert state.latest_valid_draft_row_id == draft_row_id


def test_manuscript_detail_scene_entry_carries_content(client, session):
    """FE 换源数据面：detail 的 scenes[].final_scene 必须带 content 全文。"""
    _create_chapter(client, "chapter_adopt_8")
    _create_scene(client, "scene_adopt_8", chapter_id="chapter_adopt_8", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_8", "chapter_adopt_8", content="逐场正文全文。")
    adopted = client.post(
        "/api/v1/scenes/scene_adopt_8/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-8"},
    )
    assert adopted.status_code == 200

    detail = client.get("/api/v1/chapter-manuscripts/chapter_adopt_8").json()["data"]
    entries = [s for s in detail["scenes"] if s["scene_id"] == "scene_adopt_8"]
    assert entries and entries[0]["final_scene"]["content"] == "逐场正文全文。"


# ---------------------------------------------------------------------------
# C2 状态一致性债务（评估 §0）：归档后无主执行残留必须在同一事务收敛，
# 会计安全栅栏与活跃执行不得被覆盖；job 视图层收敛为 archived。
# ---------------------------------------------------------------------------


def _seed_run_residue(session, scene_id: str, *, status: str, checkpoint: str | None = "soft_qc_ready", execution_id: str = "exec_residue_1") -> None:
    state = session.get(SceneRunState, scene_id)
    state.active_execution_id = execution_id
    state.run_execution_status = status
    state.run_checkpoint = checkpoint
    state.run_checkpoint_json = (
        {"execution_id": execution_id, "node_key": checkpoint}
        if checkpoint
        else {"execution_id": execution_id}
    )
    session.commit()


def test_adopt_finalizes_failed_run_residue(client, session):
    """failed@soft_qc_ready 残留在归档事务内收敛为 completed/archived。"""
    _create_chapter(client, "chapter_adopt_9")
    _create_scene(client, "scene_adopt_9", chapter_id="chapter_adopt_9", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_9", "chapter_adopt_9", content="残留收敛正文。")
    _seed_run_residue(session, "scene_adopt_9", status="failed")

    response = client.post(
        "/api/v1/scenes/scene_adopt_9/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-9"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["run_residue_finalized"] is True

    session.expire_all()
    state = session.get(SceneRunState, "scene_adopt_9")
    assert state.run_execution_status == "completed"
    assert state.run_checkpoint == "archived"
    payload = state.run_checkpoint_json
    # 原状态/断点保留在审计字段，历史不被抹除
    assert payload["finalized_by"] == "author_adoption"
    assert payload["finalized_from_status"] == "failed"
    assert payload["finalized_from_node"] == "soft_qc_ready"
    assert payload["node_key"] == "archived"
    assert payload["execution_id"] == "exec_residue_1"

    # 收敛后的终态是可接管的：新执行 claim 不会撞 RUN_EXECUTION_IN_PROGRESS
    from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

    checkpoint = SceneRunCheckpointService(session).acquire_execution("scene_adopt_9", "exec_residue_2")
    assert checkpoint.execution_id == "exec_residue_2"
    assert checkpoint.resumed is False


def test_adopt_keeps_accounting_fence_untouched(client, session):
    """会计安全栅栏（usage_exceeds_reservation）在事故修复前不得被归档抹除。"""
    _create_chapter(client, "chapter_adopt_10")
    _create_scene(client, "scene_adopt_10", chapter_id="chapter_adopt_10", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_10", "chapter_adopt_10", content="栅栏保留正文。")
    _seed_run_residue(session, "scene_adopt_10", status="usage_exceeds_reservation")

    response = client.post(
        "/api/v1/scenes/scene_adopt_10/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-10"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["run_residue_finalized"] is False

    session.expire_all()
    state = session.get(SceneRunState, "scene_adopt_10")
    assert state.run_execution_status == "usage_exceeds_reservation"
    assert state.run_checkpoint == "soft_qc_ready"


def test_adopt_leaves_live_execution_untouched(client, session):
    """活跃执行有 owner：归档不得抢占其执行栅栏。"""
    _create_chapter(client, "chapter_adopt_11")
    _create_scene(client, "scene_adopt_11", chapter_id="chapter_adopt_11", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_11", "chapter_adopt_11", content="活跃执行正文。")
    _seed_run_residue(session, "scene_adopt_11", status="active")

    response = client.post(
        "/api/v1/scenes/scene_adopt_11/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-11"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["run_residue_finalized"] is False

    session.expire_all()
    state = session.get(SceneRunState, "scene_adopt_11")
    assert state.run_execution_status == "active"
    assert state.run_checkpoint == "soft_qc_ready"


def test_adopt_replay_heals_legacy_residue_on_archived_scene(client, session):
    """修复前已归档但残留 failed 的历史场景：幂等重放 adopt 即自愈。"""
    _create_chapter(client, "chapter_adopt_13")
    _create_scene(client, "scene_adopt_13", chapter_id="chapter_adopt_13", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_13", "chapter_adopt_13", content="历史残留自愈正文。")

    first = client.post(
        "/api/v1/scenes/scene_adopt_13/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-13"},
    )
    assert first.status_code == 200
    # 模拟修复前的历史库形态：归档后残留 failed@soft_qc_ready
    _seed_run_residue(session, "scene_adopt_13", status="failed", execution_id="exec_legacy_13")

    replay = client.post(
        "/api/v1/scenes/scene_adopt_13/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-13-heal"},
    )
    assert replay.status_code == 200
    data = replay.json()["data"]
    assert data["already_archived"] is True
    assert data["run_residue_finalized"] is True

    session.expire_all()
    state = session.get(SceneRunState, "scene_adopt_13")
    assert state.run_execution_status == "completed"
    assert state.run_checkpoint == "archived"


def test_latest_job_view_converges_to_archived(client, session):
    """job 视图层收敛：场景归档后 latest 不再展示旧 awaiting_candidate_selection。"""
    from novel_system.db.models import ChapterRunJob

    _create_chapter(client, "chapter_adopt_12")
    _create_scene(client, "scene_adopt_12", chapter_id="chapter_adopt_12", scene_seq=1)
    _seed_style_draft(session, "scene_adopt_12", "chapter_adopt_12", content="视图收敛正文。")
    session.add(
        ChapterRunJob(
            job_id="job_adopt_12",
            chapter_id="chapter_adopt_12",
            scene_id="scene_adopt_12",
            status="completed",
            job_type="scene_run_full",
            payload_json={"scene_id": "scene_adopt_12", "current_step": "awaiting_candidate_selection"},
        )
    )
    session.commit()

    before = client.get("/api/v1/scenes/scene_adopt_12/run/jobs/latest").json()["data"]
    assert before["current_step"] == "awaiting_candidate_selection"
    assert before["scene_status"] != "archived"

    adopted = client.post(
        "/api/v1/scenes/scene_adopt_12/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "adopt-12"},
    )
    assert adopted.status_code == 200

    after = client.get("/api/v1/scenes/scene_adopt_12/run/jobs/latest").json()["data"]
    assert after["scene_status"] == "archived"
    assert after["current_step"] == "archived"
    # 历史真值不被改写：job 行本身的 payload 仍保留原暂停点
    session.expire_all()
    job = session.get(ChapterRunJob, "job_adopt_12")
    assert job.payload_json["current_step"] == "awaiting_candidate_selection"
    assert job.status == "completed"
