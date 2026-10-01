"""Wave 1（结果闭环治理 §5.3）：author_state 投影服务。

设计红线：枚举必须能表示「无稿」——空稿三态（not_started / generating /
generation_failed）是 G-01「跑完但无稿」的可表示状态；旧实现里
human_review_required 且无任何稿的场在 API 上与「有稿待审」不可区分。
投影判定先分「有稿性」：无稿 → 空稿三态；有稿 → draft_ready /
quality_warning / awaiting_author_choice / hard_blocked / archived。
"""

from __future__ import annotations

from novel_system.db.models import (
    ChapterRunJob,
    FinalScene,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.author_state import compute_author_state

CONTRACT_FIELDS = (
    "author_state",
    "latest_valid_draft_row_id",
    "current_final_scene_row_id",
    "blocking_findings",
    "quality_warnings",
    "recommended_actions",
    "can_edit",
    "can_archive",
    "recovery_action",
)


def _make_scene(session, scene_id: str = "scene_as_1", chapter_id: str = "chapter_as_1") -> SceneCard:
    scene = SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="loc",
        scene_goal="goal",
        beats_json=["beat"],
        must_include_text="",
        forbidden_text="",
        exit_change="",
        hook="",
        target_length_band="medium",
        scene_type="reunion",
    )
    session.add(scene)
    session.flush()
    return scene


def _make_state(session, scene_id: str, **kwargs) -> SceneRunState:
    state = SceneRunState(scene_id=scene_id, **kwargs)
    session.add(state)
    session.flush()
    return state


def _make_draft(session, scene_id: str, row_id: str, *, content: str = "有效正文段落。") -> SceneDraft:
    draft = SceneDraft(
        row_id=row_id,
        scene_id=scene_id,
        chapter_id="chapter_as_1",
        stage="style",
        content=content,
        source_bundle_id="bundle_as",
        source_bundle_hash="hash_as",
    )
    session.add(draft)
    session.flush()
    return draft


def _make_job(session, scene_id: str, chapter_id: str, status: str, *, error_code: str | None = None, suffix: str = "j1") -> ChapterRunJob:
    job = ChapterRunJob(
        job_id=f"scene_run_{scene_id}_{suffix}",
        chapter_id=chapter_id,
        scene_id=scene_id,
        status=status,
        job_type="scene_run_full",
        payload_json={"scene_id": scene_id},
        error_code=error_code,
    )
    session.add(job)
    session.flush()
    return job


def test_contract_fields_always_present(session):
    scene = _make_scene(session)
    payload = compute_author_state(session, scene.scene_id)
    for field in CONTRACT_FIELDS:
        assert field in payload, f"author_state 投影缺契约字段 {field}"


def test_not_started_without_state_row(session):
    scene = _make_scene(session)
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "not_started"
    assert payload["can_edit"] is False
    assert payload["can_archive"] is False
    assert payload["latest_valid_draft_row_id"] is None


def test_not_started_with_ready_state_row(session):
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="ready")
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "not_started"


def test_generating_with_active_job(session):
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="bundle_built")
    _make_job(session, scene.scene_id, scene.chapter_id, "running")
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "generating"
    assert payload["can_archive"] is False


def test_cancel_requested_job_remains_generating_until_durable_confirmation(session):
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="bundle_built")
    _make_job(session, scene.scene_id, scene.chapter_id, "cancel_requested")

    payload = compute_author_state(session, scene.scene_id)

    assert payload["author_state"] == "generating"
    assert payload["can_edit"] is False
    assert payload["can_archive"] is False


def test_generation_failed_with_failed_job_and_no_draft(session):
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="bundle_built")
    _make_job(session, scene.scene_id, scene.chapter_id, "failed", error_code="SCENE_RUN_FAILED")
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "generation_failed"
    assert payload["recovery_action"] == "retry"
    assert payload["can_archive"] is False


def test_generation_failed_recovery_action_llm(session):
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="bundle_built")
    _make_job(session, scene.scene_id, scene.chapter_id, "failed", error_code="LLM_NOT_ENABLED")
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "generation_failed"
    assert payload["recovery_action"] == "configure_llm"


def test_generation_failed_recovery_action_scene_card(session):
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="bundle_built")
    _make_job(
        session, scene.scene_id, scene.chapter_id, "blocked",
        error_code="SCENE_EXECUTION_CONTRACT_BLOCKED",
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "generation_failed"
    assert payload["recovery_action"] == "complete_scene_card"


def test_human_review_required_without_draft_is_generation_failed(session):
    """G-01 核心回归：human_review_required 但库里没有任何稿 → 空稿态，
    不得投影成「有稿待审」的 hard_blocked。"""
    scene = _make_scene(session)
    _make_state(session, scene.scene_id, scene_status="human_review_required")
    _make_job(session, scene.scene_id, scene.chapter_id, "blocked", error_code="HUMAN_REVIEW")
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "generation_failed"
    assert payload["recovery_action"] is not None


def test_draft_ready_with_style_draft(session):
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_1")
    _make_state(
        session, scene.scene_id,
        scene_status="bundle_built",
        current_style_draft_row_id=draft.row_id,
        latest_valid_draft_row_id=draft.row_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "draft_ready"
    assert payload["latest_valid_draft_row_id"] == draft.row_id
    assert payload["can_edit"] is True
    assert payload["can_archive"] is True


def _make_blocking_report(session, scene_id: str, report_id: str) -> QcReport:
    """Wave 2（§5.4/§6.1）：verified Q1 分级条目——hard_blocked 的证据载体。"""
    report = QcReport(
        qc_report_id=report_id,
        scene_id=scene_id,
        chapter_id="chapter_as_1",
        qc_type="hard_qc",
        source_draft_row_id="draft_src",
        source_bundle_id="bundle_as",
        resolution_code="hard_fail_partial",
        pass_flag=0,
        next_action="partial_rewrite",
        issues_json=[
            {
                "issue_key": "missing_required_text",
                "message": "必备元素缺失",
                "quality_level": "Q1",
                "blocking": True,
                "verified_by": "scene_card_required_text",
                "source": "llm_advisory",
                "recommended_action": "confirm_or_revise",
            }
        ],
        rewrite_brief_json=[],
    )
    session.add(report)
    session.flush()
    return report


def test_hard_blocked_keeps_latest_valid_draft(session):
    """Wave 2 语义：hard_blocked = 阻断状态词 + 报告里有 verified Q0/Q1 条目。"""
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_2")
    report = _make_blocking_report(session, scene.scene_id, "qc_as_block_1")
    _make_state(
        session, scene.scene_id,
        scene_status="human_review_required",
        latest_valid_draft_row_id=draft.row_id,
        current_qc_report_id=report.qc_report_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "hard_blocked"
    assert payload["latest_valid_draft_row_id"] == draft.row_id
    assert payload["can_edit"] is True
    assert payload["can_archive"] is False
    assert payload["blocking_findings"][0]["issue_key"] == "missing_required_text"
    assert payload["blocking_findings"][0]["quality_level"] == "Q1"


def test_hard_status_without_verified_findings_downgrades_to_quality_warning(session):
    """Wave 2（§5.4）：阻断状态词残留但无 verified Q0/Q1 → 有稿可接管。
    只有真实 Q0/Q1 能阻断归档——历史 LLM-only 阻断行不再锁死采纳。"""
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_2b")
    _make_state(
        session, scene.scene_id,
        scene_status="human_review_required",
        latest_valid_draft_row_id=draft.row_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "quality_warning"
    assert payload["can_archive"] is True
    assert payload["latest_valid_draft_row_id"] == draft.row_id


def test_latest_valid_survives_pointer_clear(session):
    """§4.3：重写路径清掉 current_* 指针后，latest_valid 仍在 → 仍算有稿。"""
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_3")
    report = _make_blocking_report(session, scene.scene_id, "qc_as_block_2")
    _make_state(
        session, scene.scene_id,
        scene_status="hard_qc_full_rewrite_required",
        current_style_draft_row_id=None,
        current_neutral_draft_row_id=None,
        latest_valid_draft_row_id=draft.row_id,
        current_qc_report_id=report.qc_report_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "hard_blocked"
    assert payload["latest_valid_draft_row_id"] == draft.row_id


def test_quality_warning_pending_acceptance_status(session):
    """Wave 2 严格模式停点：quality_warning_pending_acceptance → 可归档的 quality_warning。"""
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_3b")
    _make_state(
        session, scene.scene_id,
        scene_status="quality_warning_pending_acceptance",
        latest_valid_draft_row_id=draft.row_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "quality_warning"
    assert payload["can_archive"] is True


def test_quality_warning_soft_patch(session):
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_4")
    _make_state(
        session, scene.scene_id,
        scene_status="soft_qc_patch_required",
        current_style_draft_row_id=draft.row_id,
        latest_valid_draft_row_id=draft.row_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "quality_warning"
    assert payload["can_archive"] is True


def test_awaiting_author_choice_on_critical_gate(session):
    scene = _make_scene(session)
    draft = _make_draft(session, scene.scene_id, "draft_as_style_5")
    _make_state(
        session, scene.scene_id,
        scene_status="critical_scene_human_gate",
        current_style_draft_row_id=draft.row_id,
        latest_valid_draft_row_id=draft.row_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "awaiting_author_choice"
    assert payload["can_archive"] is False


def test_archived_with_final_scene(session):
    scene = _make_scene(session)
    final = FinalScene(
        row_id="final_as_1",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        content="归档正文。",
        status="archived",
        source_bundle_id="bundle_as",
        source_bundle_hash="hash_as",
    )
    session.add(final)
    _make_state(
        session, scene.scene_id,
        scene_status="archived",
        current_final_scene_row_id=final.row_id,
    )
    payload = compute_author_state(session, scene.scene_id)
    assert payload["author_state"] == "archived"
    assert payload["current_final_scene_row_id"] == final.row_id
    assert payload["can_archive"] is False


def test_status_endpoint_exposes_projection(client, session):
    """API 挂载点 1：GET /scenes/{id}/status 必须带 author_state 契约字段。"""
    from tests.support.catalog import create_v1_chapter as _create_chapter, create_v1_scene as _create_scene

    _create_chapter(client, "chapter_as_api")
    _create_scene(client, "scene_as_api", chapter_id="chapter_as_api", scene_seq=1)
    response = client.get("/api/v1/scenes/scene_as_api/status")
    assert response.status_code == 200
    data = response.json()["data"]
    for field in CONTRACT_FIELDS:
        assert field in data, f"status 端点缺 author_state 契约字段 {field}"
    assert data["author_state"] == "not_started"


def test_scene_run_states_list_exposes_author_state(client, session):
    """API 挂载点 2：GET /scene-run-states 列表项带 author_state。"""
    from novel_system.db.models import StoryProject
    from tests.support.catalog import create_v1_chapter as _create_chapter, create_v1_scene as _create_scene

    session.add(
        StoryProject(
            project_id="proj_as_list",
            title="投影列表",
            outline_text="outline",
            planning_mode="snowflake",
        )
    )
    session.commit()
    _create_chapter(client, "chapter_as_list")
    _create_scene(client, "scene_as_list", chapter_id="chapter_as_list", scene_seq=1)
    scene = session.get(SceneCard, "scene_as_list")
    scene.project_id = "proj_as_list"
    state = session.get(SceneRunState, "scene_as_list")
    assert state is not None
    state.scene_status = "human_review_required"
    session.commit()

    response = client.get("/api/v1/scene-run-states?project_id=proj_as_list")
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    target = [item for item in items if item["scene_id"] == "scene_as_list"]
    assert target and "author_state" in target[0]


def test_workbench_exposes_projection(client, session):
    """API 挂载点 3：workbench 响应带 author_state 投影块。"""
    from tests.support.catalog import create_v1_chapter as _create_chapter, create_v1_scene as _create_scene

    _create_chapter(client, "chapter_as_wb")
    _create_scene(client, "scene_as_wb", chapter_id="chapter_as_wb", scene_seq=1)
    state_count_before = session.query(SceneRunState).count()
    response = client.get("/api/v1/scenes/scene_as_wb/workbench")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "author_state" in data
    for field in CONTRACT_FIELDS:
        assert field in data["author_state"]
    assert session.query(SceneRunState).count() == state_count_before
