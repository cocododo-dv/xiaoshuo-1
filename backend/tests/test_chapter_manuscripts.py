from __future__ import annotations

from sqlalchemy import event

from novel_system.db.models import (
    ChapterMemory,
    ChapterState,
    FinalScene,
    RevisionCandidate,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.chapter_manuscripts import ChapterManuscriptService


def _create_chapter(client, chapter_id: str, *, goal: str = "Draft a chapter") -> None:
    response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": chapter_id,
            "planned_scene_count": 3,
            "chapter_goal": goal,
            "main_plot_push": f"push {chapter_id}",
            "emotional_target": f"emotion {chapter_id}",
            "ending_effect": f"ending {chapter_id}",
            "must_not": f"avoid {chapter_id}",
            "notes": f"notes {chapter_id}",
        },
        headers={"X-Idempotency-Key": f"create-{chapter_id}"},
    )
    assert response.status_code == 200


def _create_scene(
    client,
    scene_id: str,
    *,
    chapter_id: str,
    scene_seq: int,
    is_chapter_last: int = 0,
) -> None:
    response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "scene_seq": scene_seq,
            "pov_character_id": "CHAR_A",
            "onstage_chars_json": ["CHAR_A"],
            "location": f"Location {scene_id}",
            "scene_goal": f"goal for {scene_id}",
            "beats_json": [f"beat {scene_id}"],
            "must_include_text": "",
            "forbidden_text": "",
            "exit_change": "",
            "hook": "",
            "target_length_band": "medium",
            "scene_type": "reunion",
            "is_chapter_last": is_chapter_last,
        },
        headers={"X-Idempotency-Key": f"create-{scene_id}"},
    )
    assert response.status_code == 200


def _finalize_scene(session, scene_id: str, chapter_id: str, content: str, *, suffix: str = "v1") -> str:
    row_id = f"final_scene_{scene_id}_{suffix}"
    final = FinalScene(
        row_id=row_id,
        scene_id=scene_id,
        chapter_id=chapter_id,
        content=content,
        status="approved",
        source_bundle_id=f"bundle_{scene_id}",
        source_bundle_hash=f"hash_{scene_id}",
    )
    state = session.get(SceneRunState, scene_id)
    assert state is not None
    state.scene_status = "archived"
    state.current_final_scene_row_id = row_id
    session.add(final)
    session.commit()
    return row_id


def _set_final_aggregate(session, chapter_id: str, content: str, *, row_id: str | None = None) -> str:
    memory_row_id = row_id or f"chapter_memory_final_{chapter_id}_v1"
    memory = ChapterMemory(
        row_id=memory_row_id,
        chapter_id=chapter_id,
        aggregate_stage="final",
        content=content,
        active_flag=1,
        runtime_eligible=1,
        runtime_eligibility_basis="direct_read",
    )
    state = session.get(ChapterState, chapter_id)
    assert state is not None
    state.last_final_memory_row_id = memory_row_id
    session.add(memory)
    session.commit()
    return memory_row_id


def _statement_count(session, action) -> int:
    engine = session.get_bind()
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement)

    session.expire_all()
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    return len(statements)


def test_manuscript_detail_query_count_does_not_grow_with_scene_count(client, session) -> None:
    _create_chapter(client, "CHM_QUERY_ONE")
    _create_scene(client, "CHM_QUERY_ONE_SC01", chapter_id="CHM_QUERY_ONE", scene_seq=1)
    _finalize_scene(session, "CHM_QUERY_ONE_SC01", "CHM_QUERY_ONE", "one")

    _create_chapter(client, "CHM_QUERY_MANY")
    for scene_seq in range(1, 9):
        scene_id = f"CHM_QUERY_MANY_SC{scene_seq:02d}"
        _create_scene(client, scene_id, chapter_id="CHM_QUERY_MANY", scene_seq=scene_seq)
        _finalize_scene(session, scene_id, "CHM_QUERY_MANY", f"scene {scene_seq}")

    one_scene_queries = _statement_count(
        session,
        lambda: ChapterManuscriptService(session).manuscript_detail("CHM_QUERY_ONE"),
    )
    many_scene_queries = _statement_count(
        session,
        lambda: ChapterManuscriptService(session).manuscript_detail("CHM_QUERY_MANY"),
    )

    assert many_scene_queries <= one_scene_queries + 1


def test_chapter_manuscript_detail_assembles_current_final_scenes_and_marks_missing(client, session) -> None:
    _create_chapter(client, "CHM100", goal="Read the current manuscript")
    _create_scene(client, "CHM100_SC02", chapter_id="CHM100", scene_seq=2, is_chapter_last=1)
    _create_scene(client, "CHM100_SC01", chapter_id="CHM100", scene_seq=1)
    _create_scene(client, "CHM100_SC03", chapter_id="CHM100", scene_seq=3)
    _finalize_scene(session, "CHM100_SC02", "CHM100", "second scene text")
    _finalize_scene(session, "CHM100_SC01", "CHM100", "first scene text")

    response = client.get("/api/v1/chapter-manuscripts/CHM100")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["chapter"]["chapter_id"] == "CHM100"
    assert data["completion_status"] == "partial"
    assert data["comparison_status"] == "aggregate_missing"
    assert data["assembled"] == {
        "content": "first scene text\nsecond scene text",
        "char_count": len("first scene text\nsecond scene text"),
        "scene_count": 3,
        "generated_scene_count": 2,
        "missing_scene_ids": ["CHM100_SC03"],
    }
    assert data["aggregate"] is None
    assert [scene["scene_id"] for scene in data["scenes"]] == ["CHM100_SC01", "CHM100_SC02", "CHM100_SC03"]
    assert data["scenes"][0]["final_scene"] == {
        "row_id": "final_scene_CHM100_SC01_v1",
        # Wave 1 前端换源：detail 逐场携带归档正文全文
        "content": "first scene text",
        "char_count": len("first scene text"),
        "created_at": data["scenes"][0]["final_scene"]["created_at"],
    }
    assert data["scenes"][2]["final_scene"] is None


def test_chapter_manuscript_detail_compares_aggregate_with_current_assembled_text(client, session) -> None:
    _create_chapter(client, "CHM200", goal="Compare final aggregate")
    _create_scene(client, "CHM200_SC01", chapter_id="CHM200", scene_seq=1)
    _create_scene(client, "CHM200_SC02", chapter_id="CHM200", scene_seq=2, is_chapter_last=1)
    _finalize_scene(session, "CHM200_SC01", "CHM200", "alpha")
    _finalize_scene(session, "CHM200_SC02", "CHM200", "beta")
    aggregate_row_id = _set_final_aggregate(session, "CHM200", "alpha\nbeta")

    response = client.get("/api/v1/chapter-manuscripts/CHM200")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["completion_status"] == "complete"
    assert data["comparison_status"] == "aggregate_matches_current"
    assert data["aggregate"] == {
        "row_id": aggregate_row_id,
        "content": "alpha\nbeta",
        "char_count": len("alpha\nbeta"),
        "created_at": data["aggregate"]["created_at"],
    }

    session.get(FinalScene, "final_scene_CHM200_SC02_v1").content = "beta changed"
    session.commit()
    response = client.get("/api/v1/chapter-manuscripts/CHM200")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["assembled"]["content"] == "alpha\nbeta changed"
    assert data["comparison_status"] == "aggregate_differs_current"


def test_chapter_manuscript_detail_scans_current_manuscript_for_protected_source_terms(
    client,
    session,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON",
        '["盐湾学院", "欧文·灰港"]',
    )
    _create_chapter(client, "CHM250", goal="Scan protected terms")
    _create_scene(client, "CHM250_SC01", chapter_id="CHM250", scene_seq=1)
    _create_scene(client, "CHM250_SC02", chapter_id="CHM250", scene_seq=2, is_chapter_last=1)
    _finalize_scene(session, "CHM250_SC01", "CHM250", "第一场是干净的原创线索。")
    _finalize_scene(session, "CHM250_SC02", "CHM250", "第二场错误出现了盐湾学院与欧文·灰港。")

    response = client.get("/api/v1/chapter-manuscripts/CHM250")

    assert response.status_code == 200
    scan = response.json()["data"]["source_safety_scan"]
    # 风格参考 v3：全局受保护词也过唯一抄袭门，只报位置、来源与哈希
    assert scan["safe"] is False
    assert scan["protected_hit_count"] == 2
    assert {item["source"] for item in scan["protected_hits"]} == {"environment"}
    assert scan["checked_books"] == []
    assert "盐湾" not in str(scan)


def test_chapter_manuscript_scans_the_chapter_against_the_bound_reference(client, session) -> None:
    """风格参考 v3：成稿中心的整章读数走唯一抄袭门——每场绑定的书 + 画像的受保护专名，只报位置与哈希。"""
    from tests.reference_copy_fixtures import PROTECTED_NAME, seed_bound_reference

    _create_chapter(client, "CHM251")
    _create_scene(client, "CHM251_SC01", chapter_id="CHM251", scene_seq=1, is_chapter_last=1)
    _finalize_scene(session, "CHM251_SC01", "CHM251", f"{PROTECTED_NAME}打开了档案柜。")
    refs = seed_bound_reference(
        session,
        seed="chapter_manuscript",
        scope="scene",
        scope_ref_id="CHM251_SC01",
        protected_terms=(PROTECTED_NAME,),
    )

    scan = client.get("/api/v1/chapter-manuscripts/CHM251").json()["data"]["source_safety_scan"]

    assert scan["safe"] is False
    assert scan["checked_books"] == [refs["book_id"]]
    assert scan["profile_ids"] == [refs["profile_id"]]
    assert scan["protected_hit_count"] >= 1
    assert PROTECTED_NAME not in str(scan)


def test_chapter_manuscript_list_reports_statuses_and_excludes_trashed_records(client, session) -> None:
    _create_chapter(client, "CHM300", goal="Visible chapter")
    _create_scene(client, "CHM300_SC01", chapter_id="CHM300", scene_seq=1)
    _create_scene(client, "CHM300_SC02", chapter_id="CHM300", scene_seq=2, is_chapter_last=1)
    _finalize_scene(session, "CHM300_SC01", "CHM300", "visible text")

    _create_chapter(client, "CHM301", goal="Trashed chapter")
    _create_scene(client, "CHM301_SC01", chapter_id="CHM301", scene_seq=1, is_chapter_last=1)
    trash_chapter = client.post(
        "/api/v1/chapters/trash",
        json={"chapter_ids": ["CHM301"]},
        headers={"X-Idempotency-Key": "trash-chm301"},
    )
    assert trash_chapter.status_code == 200

    _create_scene(client, "CHM300_SC03", chapter_id="CHM300", scene_seq=3)
    trash_scene = client.post(
        "/api/v1/scenes/trash",
        json={"scene_ids": ["CHM300_SC03"]},
        headers={"X-Idempotency-Key": "trash-chm300-sc03"},
    )
    assert trash_scene.status_code == 200

    state = session.get(ChapterState, "CHM300")
    assert state is not None
    state.chapter_backfill_pending_count = 2
    session.commit()

    response = client.get("/api/v1/chapter-manuscripts")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["chapter_id"] for item in items] == ["CHM300"]
    assert items[0]["scene_count"] == 2
    assert items[0]["generated_scene_count"] == 1
    assert items[0]["missing_scene_ids"] == ["CHM300_SC02"]
    assert items[0]["completion_status"] == "partial"
    assert items[0]["comparison_status"] == "aggregate_missing"
    assert items[0]["chapter_backfill_pending_count"] == 2


def test_chapter_manuscript_empty_chapter_and_active_aggregate_fallback(client, session) -> None:
    _create_chapter(client, "CHM400", goal="No scenes yet")
    _set_final_aggregate(session, "CHM400", "legacy aggregate", row_id="chapter_memory_final_CHM400_v2")
    state = session.get(ChapterState, "CHM400")
    assert state is not None
    state.last_final_memory_row_id = None
    session.commit()

    response = client.get("/api/v1/chapter-manuscripts/CHM400")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["completion_status"] == "empty"
    assert data["comparison_status"] == "aggregate_differs_current"
    assert data["assembled"]["scene_count"] == 0
    assert data["assembled"]["content"] == ""
    assert data["aggregate"]["row_id"] == "chapter_memory_final_CHM400_v2"


def test_chapter_manuscript_detail_returns_editorial_workspace_for_writer_desk(client, session) -> None:
    _create_chapter(client, "CHM500", goal="Read with editorial diagnostics")
    _create_scene(client, "CHM500_SC01", chapter_id="CHM500", scene_seq=1, is_chapter_last=1)
    _finalize_scene(session, "CHM500_SC01", "CHM500", "scene text with a weak ending")
    chapter_eval = WriterEvaluation(
        evaluation_id="writer_eval_chapter_CHM500",
        object_type="chapter",
        object_id="CHM500",
        chapter_id="CHM500",
        rubric_id="drama_effectiveness_v1",
        source_text_ref="assembled:CHM500",
        overall_score=0.54,
        scores_json={"ending_drive": 0.42},
        findings_json=[
            {
                "dimension": "ending_drive",
                "severity": "major",
                "issue": "chapter ending stalls",
                "recommendation": "end on a sharper irreversible choice",
                "evidence_excerpt": "weak ending",
                "evidence_location": "chapter final paragraph",
                "why_it_matters": "the next chapter needs a live question",
            }
        ],
        revision_brief_json=[{"dimension": "ending_drive", "action": "sharpen the close", "priority": "high"}],
        requires_human_review=1,
    )
    scene_eval = WriterEvaluation(
        evaluation_id="writer_eval_scene_CHM500_SC01",
        object_type="scene",
        object_id="CHM500_SC01",
        chapter_id="CHM500",
        scene_id="CHM500_SC01",
        rubric_id="drama_effectiveness_v1",
        source_text_ref="final_scene:final_scene_CHM500_SC01_v1",
        overall_score=0.62,
        scores_json={"dialogue_edge": 0.48},
        findings_json=[
            {
                "dimension": "dialogue_edge",
                "severity": "major",
                "issue": "dialogue softens the confrontation",
                "recommendation": "let the reply carry a cost",
                "evidence_excerpt": "scene text",
                "evidence_location": "scene 1",
                "why_it_matters": "the power shift is otherwise invisible",
            }
        ],
        revision_brief_json=[{"dimension": "dialogue_edge", "action": "cut polite filler", "priority": "high"}],
        requires_human_review=0,
    )
    chapter_candidate = RevisionCandidate(
        revision_id="revision_chapter_CHM500",
        evaluation_id=chapter_eval.evaluation_id,
        object_type="chapter",
        object_id="CHM500",
        chapter_id="CHM500",
        revision_type="chapter_revision",
        source_text_ref="assembled:CHM500",
        proposed_text="chapter revision plan",
        diff_summary_json={
            "summary": "reshape the last beat",
            "changed_dimensions": ["ending_drive"],
            "rewrite_strategy": "revision_plan",
            "source_text_ref": "assembled:CHM500",
            "candidate_kind": "revision_plan",
        },
    )
    scene_candidate = RevisionCandidate(
        revision_id="revision_scene_CHM500_SC01",
        evaluation_id=scene_eval.evaluation_id,
        object_type="scene",
        object_id="CHM500_SC01",
        chapter_id="CHM500",
        scene_id="CHM500_SC01",
        revision_type="scene_revision",
        source_text_ref="final_scene:final_scene_CHM500_SC01_v1",
        proposed_text="rewritten scene text",
        diff_summary_json={
            "summary": "make the answer sharper",
            "changed_dimensions": ["dialogue_edge"],
            "rewrite_strategy": "full_scene_rewrite",
            "source_text_ref": "final_scene:final_scene_CHM500_SC01_v1",
            "candidate_kind": "full_scene_rewrite",
        },
    )
    session.add_all([chapter_eval, scene_eval, chapter_candidate, scene_candidate])
    session.commit()

    response = client.get("/api/v1/chapter-manuscripts/CHM500")

    assert response.status_code == 200
    workspace = response.json()["data"]["editorial_workspace"]
    assert workspace["reading_source"] == "assembled"
    assert workspace["chapter_review"]["latest_evaluation"]["evaluation_id"] == "writer_eval_chapter_CHM500"
    assert workspace["chapter_review"]["latest_evaluation"]["findings"][0]["evidence_excerpt"] == "weak ending"
    assert workspace["scene_reviews"][0]["scene_id"] == "CHM500_SC01"
    assert workspace["scene_reviews"][0]["review"]["latest_evaluation"]["findings"][0]["why_it_matters"] == "the power shift is otherwise invisible"
    assert [candidate["revision_id"] for candidate in workspace["revision_candidates"]] == [
        "revision_chapter_CHM500",
        "revision_scene_CHM500_SC01",
    ]
    assert workspace["open_issue_counts"] == {
        "open_candidates": 2,
        "findings": 2,
        "requires_human_review": 1,
        "reviewed_objects": 2,
    }
