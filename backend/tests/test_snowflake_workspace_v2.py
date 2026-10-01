"""雪花构思工作台 v2（``/api/v2/projects/{id}/snowflake-workspace``）：建作品、逐步生成 / 保存 / 确认、跳过与
必走步、版本历史与恢复、上游改动后的失效与复核、确认写入目录与回流。场景分诊与诊断在
test_snowflake_scene_triage.py，接真实模型的生成与教练在 test_snowflake_workspace_llm.py。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    FinalScene,
    LlmCall,
    OperationLog,
    SceneCard,
    SnowflakeRevisionLink,
    SnowflakeScenePlan,
    SnowflakeStepRun,
)
from tests.support.snowflake import (
    approve_generated_step as _approve_generated_step,
    approve_workspace_step as _approve_step,
    create_workspace_project as _create_project,
    generate_workspace_step as _generate_step,
)

pytestmark = pytest.mark.usefixtures("online_author_pipeline", "skeleton_snowflake")


def test_workspace_v2_creates_snowflake_project_and_exposes_structured_steps(client) -> None:
    project = _create_project(client)
    assert project["planning_mode"] == "snowflake"
    assert project["snowflake_workflow_mode"] == "explore"

    response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    assert response.status_code == 200
    workspace = response.json()["data"]

    assert workspace["project"]["project_id"] == project["project_id"]
    assert workspace["method_version"] == "2026-04-29.v2"
    assert workspace["quality_policy"]["flow_mode"] == "coach_flexible"
    assert workspace["materialization_requirements"]["hard_required_steps"] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "scene_list",
        "scene_details",
    ]
    assert workspace["current_step_key"] == "book_brief"
    assert workspace["ready_to_materialize"] is False
    assert workspace["latest_plan"] is None
    assert [step["step_key"] for step in workspace["steps"]][:3] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]
    first_step = workspace["steps"][0]
    assert first_step["label"] == "读者定位"
    assert first_step["can_confirm"] is False
    assert first_step["approval_blockers"] == []
    assert first_step["english_label"] == "Target Audience"
    assert first_step["phase"] == "基础准备"
    assert first_step["description"] == "明确你的小说类型和目标读者群体。你写作的核心目标是取悦你的读者——先知道为谁写，再决定写什么。"
    assert first_step["editor"]["kind"] == "form"
    assert any(field["key"] == "target_reader" for field in first_step["editor"]["fields"])
    assert first_step["guidance"]["instruction"] == (
        "回答以下三个问题：\n\n"
        "① 我的故事类型/流派是什么？\n"
        "② 这类故事的魅力在哪里？为何读者会喜欢？\n"
        "③ 我理想中的读者是谁？他们的特征是？"
    )
    assert first_step["guidance"]["timebox_minutes"] > 0
    assert first_step["guidance"]["source"] == "snowflake_method_summary"
    assert first_step["guidance"]["checklist"]
    assert first_step["guidance"]["rubric"]
    assert first_step["guidance"]["required_for_materialization"] is True
    one_sentence_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert one_sentence_step["can_confirm"] is False
    assert one_sentence_step["approval_blockers"][0]["step_key"] == "book_brief"
    assert one_sentence_step["approval_blockers"][0]["label"] == "读者定位"
    one_sentence_guidance_text = json.dumps(one_sentence_step["guidance"], ensure_ascii=False)
    assert "在一句因果句里保留主角、目标、阻力和代价。" in one_sentence_step["guidance"]["checklist"]
    assert "场景可写性" in one_sentence_step["guidance"]["rubric"]
    assert "Preserves protagonist" not in one_sentence_guidance_text
    assert "scene_writability" not in one_sentence_guidance_text
    assert [step["label"] for step in workspace["steps"]] == [
        "读者定位",
        "一句话概括",
        "一段话概括",
        "角色摘要表",
        "一页梗概",
        "角色背景故事",
        "长篇大纲",
        "角色全档案",
        "场景列表",
        "场景规划",
    ]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    scene_field = next(field for field in scene_step["editor"]["fields"] if field["key"] == "scenes")
    assert scene_field["template"]["primary_form"] == "proactive"
    proactive_mode = next(mode for mode in scene_field["scene_modes"] if mode["value"] == "proactive")
    reactive_mode = next(mode for mode in scene_field["scene_modes"] if mode["value"] == "reactive")
    proactive_goal = next(field for field in proactive_mode["fields"] if field["key"] == "goal")
    proactive_conflict = next(field for field in proactive_mode["fields"] if field["key"] == "conflict")
    reactive_crucible = next(field for field in reactive_mode["fields"] if field["key"] == "crucible")
    assert proactive_goal["label"] == "目标"
    # 阶段 O：目标提示带原著好目标的五条（能拍下来、装得进时间槽、可能、难但不可笑、合乎价值观与志向）
    assert proactive_goal["hint"].startswith("角色想达成什么？能拍下来") and "时间槽" in proactive_goal["hint"]
    assert proactive_goal["rows"] == 2
    assert proactive_goal["placeholder"].startswith("例：让警探放弃拘留")  # 阶段 O：不再给每个目标硬塞倒计时
    assert "至少两轮" in proactive_conflict["hint"] and "2-3" not in proactive_conflict["hint"]  # 阶段 O：回合数没有规则
    assert "警探拿出监控截图否定" in proactive_conflict["placeholder"]
    assert reactive_crucible["hint"] == "是什么让角色无法回避这个困境？"
    assert reactive_crucible["placeholder"] == "例：真凶今晚就要行动，主角却被关着，而且没有人相信他的话"
    assert first_step["completeness"]["total_count"] >= 1
    assert "target_reader" in first_step["completeness"]["missing_fields"]
    target_reader_field = next(field for field in first_step["editor"]["fields"] if field["key"] == "target_reader")
    assert target_reader_field["hint"]
    assert target_reader_field["placeholder"]
    short_step = next(step for step in workspace["steps"] if step["step_key"] == "short_synopsis")
    assert len(short_step["draft"]["paragraphs"]) == 5
    bible_step = next(step for step in workspace["steps"] if step["step_key"] == "character_bibles")
    bible_template = next(field for field in bible_step["editor"]["fields"] if field["key"] == "characters")["template"]
    assert {"physical_profile", "personality_profile", "environment_profile", "psychological_profile"} <= set(bible_template)
    assert workspace["materialization_gate"]["status"] == "blocked"
    assert workspace["materialization_gate"]["blockers"]
    assert workspace["materialization_gate"]["items"]
    first_gate_item = workspace["materialization_gate"]["items"][0]
    assert first_gate_item["severity"] == "blocker"
    assert first_gate_item["step_key"] == "book_brief"
    assert first_gate_item["target_view"] == "snowflake-workbench"
    assert first_gate_item["primary_action"]["type"] == "jump_to_step"
    assert first_gate_item["primary_action"]["label"]
    assert workspace["scene_board"] == {"chapters": [], "scenes": []}


def test_workspace_v2_explore_mode_allows_later_drafts_but_confirmation_stays_gated(client, session) -> None:
    project = _create_project(client, key="explore-draft-gates")
    scene_details_patch = {
        "draft": {
            "scenes": [
                {
                    "scene_id": f"{project['project_id']}_CH01_SC01",
                    "chapter_id": f"{project['project_id']}_CH01",
                    "chapter_title": "第一章",
                    "chapter_goal": "主角必须决定是否公开录音。",
                    "scene_seq": 1,
                    "title": "录音袋",
                    "summary": "她把录音袋推到桌沿。",
                    "primary_form": "proactive",
                    "scene_crucible": "证据一公开，幸存者就会暴露。",
                    "goal": "确认录音是否足够公开。",
                    "conflict": "盟友要求她先保护幸存者。",
                    "setback": "她只能拆开证据，失去完整公开机会。",
                    "hook": "门外有人听见了录音。",
                }
            ]
        }
    }

    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json=scene_details_patch,
    )

    assert save_response.status_code == 200, save_response.text
    workspace = save_response.json()["data"]["workspace"]
    assert workspace["project"]["snowflake_workflow_mode"] == "explore"
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    assert scene_step["artifact"]["status"] == "pending_review"
    assert scene_step["draft"]["scenes"][0]["scene_crucible"] == "证据一公开，幸存者就会暴露。"
    assert scene_step["can_confirm"] is False
    assert scene_step["approval_blockers"][0]["step_key"] == "book_brief"

    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-explore-scene-details-too-early"},
    )
    assert approve_response.status_code == 409
    error = approve_response.json()["error"]
    assert error["code"] == "SNOWFLAKE_PREVIOUS_STEP_REQUIRED"
    assert error["details"]["author_action"]["target_view"] == "snowflake-workbench"
    assert error["details"]["author_action"]["primary_button_label"] == "去补读者定位"

    session.expire_all()
    run = session.execute(
        select(SnowflakeStepRun).where(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "scene_details",
        )
    ).scalars().one()
    assert run.status == "pending_review"

    strict_response = client.post(
        "/api/v2/projects",
        json={
            "title": "Strict Snowflake",
            "genre": "Mystery",
            "target_chapter_count": 1,
            "target_word_count": 50000,
            "outline_text": "A strict project should still gate draft saves.",
            "snowflake_workflow_mode": "strict",
        },
        headers={"X-Idempotency-Key": "create-v2-strict-mode"},
    )
    assert strict_response.status_code == 200
    strict_project = strict_response.json()["data"]["project"]
    assert strict_project["snowflake_workflow_mode"] == "strict"

    strict_save = client.patch(
        f"/api/v2/projects/{strict_project['project_id']}/snowflake-workspace/steps/scene_details",
        json=scene_details_patch,
    )
    assert strict_save.status_code == 409
    assert strict_save.json()["error"]["code"] == "SNOWFLAKE_PREVIOUS_STEP_REQUIRED"


def test_workspace_v2_required_steps_are_not_skippable_even_with_reason(client) -> None:
    project = _create_project(client, key="required-skip-honesty")

    workspace_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    workspace = workspace_response.json()["data"]
    assert workspace["quality_policy"]["hard_required_steps"] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "scene_list",
        "scene_details",
    ]
    assert "all_steps_skippable_with_reason" not in workspace["quality_policy"]
    book_brief = next(step for step in workspace["steps"] if step["step_key"] == "book_brief")
    assert book_brief["can_skip"] is False

    skip_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/generate",
        json={"skip": True, "skip_reason": "I want to decide this later."},
    )

    assert skip_response.status_code == 400
    assert skip_response.json()["error"]["code"] == "SNOWFLAKE_STEP_NOT_SKIPPABLE"


def test_workspace_v2_step_history_and_restore_keep_author_approval_gate(client, session) -> None:
    project = _create_project(client, key="history-restore")

    first_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Urban Mystery",
                "target_reader": "Readers who want old cases and family cost.",
                "story_kind": "A mystery about choosing truth over comfort.",
            }
        },
    )
    assert first_response.status_code == 200, first_response.text
    first_run_id = first_response.json()["data"]["step"]["artifact"]["step_run_id"]
    _approve_step(client, project["project_id"], "book_brief")

    second_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Noir Mystery",
                "target_reader": "Readers who want harder-edged city pressure.",
                "story_kind": "A noir mystery with escalating public cost.",
            }
        },
    )
    assert second_response.status_code == 200, second_response.text

    history_response = client.get(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/history"
    )
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()["data"]
    assert [item["version"] for item in history["items"]] == [2, 1]
    # 列表只有元数据与出处：草稿（以及以前那行把内部 id 也拼进去的摘要）只在按版本预览时取
    assert "draft" not in history["items"][0] and "draft_summary" not in history["items"][0]

    detail_response = client.get(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/history?include_draft=true"
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["items"][1]["draft"]["category"] == "Urban Mystery"

    restore_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/restore",
        json={"step_run_id": first_run_id},
    )
    assert restore_response.status_code == 200, restore_response.text
    restored = restore_response.json()["data"]
    assert restored["step"]["artifact"]["status"] == "pending_review"
    assert restored["step"]["artifact"]["version"] == 3
    assert restored["step"]["draft"]["category"] == "Urban Mystery"
    assert restored["step_run"]["restored_from_step_run_id"] == first_run_id

    session.expire_all()
    runs = (
        session.query(SnowflakeStepRun)
        .filter(SnowflakeStepRun.project_id == project["project_id"], SnowflakeStepRun.step_key == "book_brief")
        .order_by(SnowflakeStepRun.version.asc())
        .all()
    )
    assert [run.status for run in runs] == ["approved", "pending_review", "pending_review"]
    assert session.query(SnowflakeRevisionLink).filter_by(project_id=project["project_id"]).count() == 0


def test_workspace_v2_step_history_restore_rejects_cross_project_runs(client) -> None:
    first_project = _create_project(client, key="history-cross-a")
    second_project = _create_project(client, key="history-cross-b")
    second_run = _generate_step(client, second_project["project_id"], "book_brief")["step"]["artifact"]["step_run_id"]

    response = client.post(
        f"/api/v2/projects/{first_project['project_id']}/snowflake-workspace/steps/book_brief/restore",
        json={"step_run_id": second_run},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SNOWFLAKE_STEP_RUN_NOT_FOUND"


def test_workspace_v2_logs_one_cascade_event_when_upstream_step_is_reapproved(client, session) -> None:
    """B06-14：一次失效级联留一条操作日志；snowflake_revision_links 不再写（从来没有读者）。"""
    project = _create_project(client, key="structured-revision-links")
    _approve_generated_step(client, project["project_id"], "book_brief")
    _approve_generated_step(client, project["project_id"], "one_sentence_summary")

    replacement = _generate_step(client, project["project_id"], "book_brief")
    client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
    )
    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-v2-{project['project_id']}-book-brief-replacement"},
    )
    assert response.status_code == 200, response.text
    workspace = response.json()["data"]["workspace"]
    stale_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert stale_step["status"] == "stale"
    assert stale_step["stale_reason"]
    assert replacement["step"]["version"] == 2

    session.expire_all()
    stale_run = (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "one_sentence_summary",
            SnowflakeStepRun.status == "stale",
        )
        .one()
    )
    approved_brief = (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "book_brief",
            SnowflakeStepRun.status == "approved",
        )
        .one()
    )
    [log] = session.query(OperationLog).filter_by(event_type="snowflake_downstream_marked_stale").all()
    assert log.object_ref == approved_brief.step_run_id
    assert log.payload_json["step_key"] == "book_brief"
    assert log.payload_json["affected_step_run_ids"] == [stale_run.step_run_id]
    assert log.payload_json["affected_step_keys"] == ["one_sentence_summary"]
    assert log.payload_json["reasons"]["one_sentence_summary"] == stale_run.stale_reason
    assert session.query(SnowflakeRevisionLink).filter_by(project_id=project["project_id"]).count() == 0


def test_workspace_v2_supports_structured_save_assistant_and_step_approval(client, session) -> None:
    project = _create_project(client, key="save-approve")

    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Urban Mystery",
                "target_reader": "Readers who want old cases, family cost, and a determined heroine.",
                "story_kind": "A truth-chasing mystery driven by action and relational cost.",
                "delight_reason": "Each clue narrows the truth but raises the emotional price.",
                "genre_promise": "The closer she gets to the truth, the more she stands to lose.",
                "expected_reader_emotion": "Pressure, doubt, and immediate page-turn urgency.",
                "safety_rules": [
                    "Only borrow abstract craft patterns and pacing.",
                    "Do not copy characters, settings, plot beats, or signature phrasing.",
                ],
            }
        },
    )
    assert save_response.status_code == 200, save_response.text
    saved = save_response.json()["data"]
    assert saved["step"]["draft"]["target_reader"].startswith("Readers who want")
    assert saved["step"]["artifact"]["status"] == "pending_review"

    # 阶段 T（2026-09-16）：驻场教练不再有规则罐头回退——LLM 未启用即 409，也不落回合。
    # 罐头回合既不是辅导，也不能进作者意图要点（生成 / 候选 / 分诊读的就是那份要点）。
    assistant_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/assistant",
        json={
            "step_key": "book_brief",
            "message": "Can you narrow the target reader a little more?",
        },
    )
    assert assistant_response.status_code == 409, assistant_response.text
    assert assistant_response.json()["error"]["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"

    history_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    assert history_response.status_code == 200, history_response.text
    assert history_response.json()["data"]["assistant_history"] == []
    assert history_response.json()["data"]["direction_briefs"] == {}

    approve_response = _approve_step(client, project["project_id"], "book_brief")
    workspace = approve_response["workspace"]
    assert workspace["current_step_key"] == "one_sentence_summary"

    step_run_id = saved["step"]["artifact"]["step_run_id"]
    session.expire_all()
    step_run = session.get(SnowflakeStepRun, step_run_id)
    assert step_run is not None
    assert step_run.status == "approved"
    assert step_run.draft_json["category"] == "Urban Mystery"


def test_workspace_v2_step_health_reports_structural_pressure_gaps(client) -> None:
    project = _create_project(client, key="pressure-health")

    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Urban Mystery",
                "target_reader": "Readers who like mysteries.",
                "story_kind": "A mystery.",
            }
        },
    )
    assert response.status_code == 200, response.text
    step = response.json()["data"]["step"]
    health = step["health"]

    assert isinstance(health["pressure_score"], int)
    # 阶段 H：泛泛短语只给建议，不再改状态——「Readers who like mysteries.」是建议项，不是缺陷旗标
    assert "reader_promise_too_generic" not in health["pressure_flags"]
    assert "story_pressure_too_generic" not in health["pressure_flags"]
    assert any(step.startswith("建议：") and "读者" in step for step in health["fix_steps"])
    assert health["fix_steps"]
    assert isinstance(health["strengths"], list)
    assert health["score"] == health["pressure_score"]
    assert health["status"] == health["pressure_status"]
    assert health["gaps"] == health["pressure_flags"]
    assert health["next_actions"] == health["fix_steps"]
    assert isinstance(health["hard_blockers"], list)
    # 变更回包不再带 health 的深拷贝（B06-05）；GET 的工作台照旧带
    assert "diagnosis_json" not in step["artifact"]
    assert "diagnosis_json" not in response.json()["data"]["step_run"]
    fetched = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    fetched_step = next(item for item in fetched["steps"] if item["step_key"] == "book_brief")
    assert fetched_step["artifact"]["diagnosis_json"]["pressure_score"] == health["pressure_score"]


def test_workspace_v2_marks_downstream_steps_stale_after_upstream_regeneration(client) -> None:
    project = _create_project(client, key="stale")
    _approve_generated_step(client, project["project_id"], "book_brief")
    _approve_generated_step(client, project["project_id"], "one_sentence_summary")

    # P0-3: staleness is diff-aware, so the upstream revision must actually change
    # book_brief's content to invalidate the step that consumed it.
    replacement = _generate_step(client, project["project_id"], "book_brief")
    client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
    )
    workspace = _approve_step(client, project["project_id"], "book_brief")["workspace"]

    assert workspace["current_step_key"] == "one_sentence_summary"
    stale_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert stale_step["artifact"]["status"] == "stale"


def test_workspace_v2_accepts_stale_step_with_audit_trail(client, session) -> None:
    project = _create_project(client, key="accept-stale-step")
    _approve_generated_step(client, project["project_id"], "book_brief")
    _approve_generated_step(client, project["project_id"], "one_sentence_summary")

    replacement = _generate_step(client, project["project_id"], "book_brief")
    client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
    )
    stale_workspace = _approve_step(client, project["project_id"], "book_brief")["workspace"]
    stale_step = next(step for step in stale_workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert stale_step["status"] == "stale"
    assert stale_workspace["current_step_key"] == "one_sentence_summary"

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/one_sentence_summary/accept-stale",
        json={"note": "The one-sentence promise still matches the revised audience."},
        headers={"X-Operator-Ref": "author-a"},
    )

    assert response.status_code == 200, response.text
    workspace = response.json()["data"]["workspace"]
    accepted_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert accepted_step["status"] == "stale"
    assert accepted_step["stale_accepted_at"]
    assert accepted_step["stale_accepted_by"] == "author-a"
    assert accepted_step["gate_satisfied"] is True
    assert workspace["current_step_key"] == "one_paragraph_summary"

    session.expire_all()
    stale_run = (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "one_sentence_summary",
            SnowflakeStepRun.status == "stale",
        )
        .one()
    )
    assert stale_run.stale_accepted_by == "author-a"
    log = (
        session.query(OperationLog)
        .filter_by(event_type="snowflake_step_stale_accepted", object_type="snowflake_step_run", object_ref=stale_run.step_run_id)
        .one()
    )
    assert log.payload_json["note"] == "The one-sentence promise still matches the revised audience."


def test_workspace_v2_step_accept_stale_clears_stale_scenes_for_materialization(client, session) -> None:
    project = _create_project(client, key="accept-stale-scenes")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    scene_plans = (
        session.query(SnowflakeScenePlan)
        .filter(SnowflakeScenePlan.project_id == project["project_id"])
        .order_by(SnowflakeScenePlan.scene_plan_id.asc())
        .all()
    )
    assert scene_plans
    for scene in scene_plans:
        scene.status = "stale"
        scene.stale_reason = "book_brief was revised; review dependent scene work."
    details_run = (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "scene_details",
            SnowflakeStepRun.status == "approved",
        )
        .one()
    )
    details_run.status = "stale"
    details_run.stale_reason = "scene_list 改了被消费字段 ['scenes']"
    session.commit()

    blocked = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    assert blocked["materialization_gate"]["status"] == "blocked"

    # R15a：第 10 步的「已复核」连同过期的场景计划一起复核（逐场的接口已删）
    accept_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/accept-stale",
        json={"note": "Reviewed after premise update."},
        headers={"X-Operator-Ref": "author-b"},
    )
    assert accept_response.status_code == 200, accept_response.text
    accepted_workspace = accept_response.json()["data"]["workspace"]
    assert accepted_workspace["materialization_gate"]["status"] != "blocked"
    accepted_scene = next(
        s for s in accepted_workspace["steps"] if s["step_key"] == "scene_details"
    )["draft"]["scenes"][0]
    assert accepted_scene["status"] == "stale"
    assert accepted_scene["stale_accepted_at"]
    assert accepted_scene["stale_accepted_by"] == "author-b"

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-accepted-stale-scenes"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    assert materialize_response.json()["data"]["plan"]["status"] == "pending_review"

    session.expire_all()
    logs = (
        session.query(OperationLog)
        .filter(OperationLog.event_type == "snowflake_scene_stale_accepted", OperationLog.object_type == "snowflake_scene_plan")
        .all()
    )
    assert len(logs) == len(scene_plans)


def test_workspace_v2_resync_previews_and_updates_materialized_scene_cards_without_overwriting_drafts(
    client,
    session,
) -> None:
    project = _create_project(client, key="selective-resync")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)
    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-resync"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-resync-outline"},
    )
    assert approve_response.status_code == 200, approve_response.text

    session.expire_all()
    scene_plan = session.execute(
        select(SnowflakeScenePlan)
        .where(SnowflakeScenePlan.project_id == project["project_id"])
        .order_by(SnowflakeScenePlan.scene_plan_id.asc())
    ).scalars().first()
    assert scene_plan is not None
    scene = session.get(SceneCard, scene_plan.scene_id)
    assert scene is not None
    original_goal = scene.scene_goal
    session.add(
        FinalScene(
            row_id="final_scene_resync_keep",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            content="作者已经批准的终稿不能被雪花同步覆盖。",
            status="approved",
            source_bundle_id="bundle_resync_keep",
            source_bundle_hash="hash_resync_keep",
        )
    )
    session.add(
        AuthorDraft(
            draft_id="author_draft_resync_keep",
            object_type="scene",
            object_id=scene.scene_id,
            source_text_ref="final_scene:final_scene_resync_keep",
            content="作者正在小修的草稿也不能被覆盖。",
            revision_no=3,
            status="current",
        )
    )
    session.add(
        LlmCall(
            llm_call_id="llm_call_resync_keep",
            scope_type="scene",
            scope_id=scene.scene_id,
            project_id=project["project_id"],
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            node_id="scene_finalize",
            response_payload_summary={"kept": True},
        )
    )
    scene_plan.summary = "她把新证据袋推到桌沿。"
    scene_plan.scene_crucible = "只要她现在公开，新证人就会被找到。"
    scene_plan.goal = "逼盟友承认证据来源。"
    scene_plan.conflict = "盟友用幸存者安全反压她。"
    scene_plan.setback = "她保住证人，却暂时失去完整录音。"
    scene_plan.hook = "第二个录音袋出现在门缝。"
    session.commit()

    preview_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/resync",
        json={"scene_plan_ids": [scene_plan.scene_plan_id], "dry_run": True},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()["data"]
    assert preview["dry_run"] is True
    assert preview["results"][0]["scene_id"] == scene.scene_id
    assert "scene_goal" in preview["results"][0]["diff"]

    session.expire_all()
    assert session.get(SceneCard, scene.scene_id).scene_goal == original_goal

    sync_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/resync",
        json={"scene_plan_ids": [scene_plan.scene_plan_id]},
        headers={"X-Operator-Ref": "author-resync"},
    )
    assert sync_response.status_code == 200, sync_response.text
    synced = sync_response.json()["data"]
    assert synced["dry_run"] is False
    assert synced["results"][0]["synced"] is True
    assert synced["affected_runtime"]["final_scene_count"] == 1
    assert synced["affected_runtime"]["author_draft_count"] == 1
    assert synced["affected_runtime"]["llm_call_count"] == 1

    session.expire_all()
    scene = session.get(SceneCard, scene_plan.scene_id)
    assert scene.scene_goal == "她把新证据袋推到桌沿。"
    assert scene.writer_brief_json["scene_plan_id"] == scene_plan.scene_plan_id
    assert scene.writer_brief_json["scene_crucible"] == "只要她现在公开，新证人就会被找到。"
    assert session.get(FinalScene, "final_scene_resync_keep").content == "作者已经批准的终稿不能被雪花同步覆盖。"
    assert session.get(AuthorDraft, "author_draft_resync_keep").content == "作者正在小修的草稿也不能被覆盖。"
    assert session.get(LlmCall, "llm_call_resync_keep").response_payload_summary == {"kept": True}


def test_workspace_v2_normalizes_legacy_drafts_to_method_v2(client) -> None:
    project = _create_project(client, key="legacy-normalization")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    short_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/short_synopsis",
        json={"draft": {"paragraphs": ["legacy setup", "legacy turn", "legacy ending"]}},
    )
    assert short_response.status_code == 200, short_response.text
    short_step = short_response.json()["data"]["step"]
    assert short_step["draft"]["paragraphs"][:3] == ["legacy setup", "legacy turn", "legacy ending"]
    assert len(short_step["draft"]["paragraphs"]) == 5

    _approve_step(client, project["project_id"], "short_synopsis")
    _approve_generated_step(client, project["project_id"], "character_synopses")
    _approve_generated_step(client, project["project_id"], "long_synopsis")

    bible_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/character_bibles",
        json={
            "draft": {
                "characters": [
                    {
                        "character_id": "LEGACY_CHAR",
                        "display_name": "Legacy Lead",
                        "role": "lead",
                        "age": "32",
                        "home": "Dock district",
                        "strongest_trait": "Acts under pressure",
                        "weakest_trait": "Confuses silence with loyalty",
                        "deepest_fear": "The truth will cost the family",
                        "how_character_changes": "Learns to choose truth with a cost",
                    }
                ]
            }
        },
    )
    assert bible_response.status_code == 200, bible_response.text
    character = bible_response.json()["data"]["step"]["draft"]["characters"][0]
    assert character["physical_profile"]["age"] == "32"
    assert character["personality_profile"]["strongest_trait"] == "Acts under pressure"
    assert character["environment_profile"]["home"] == "Dock district"
    assert character["psychological_profile"]["deepest_fear"] == "The truth will cost the family"
    assert character["psychological_profile"]["character_arc"] == "Learns to choose truth with a cost"


def test_workspace_v2_allows_optional_skips_but_blocks_skipped_materialization_steps(client) -> None:
    optional_project = _create_project(client, key="optional-skips")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]:
        _approve_generated_step(client, optional_project["project_id"], step_key)
    for step_key in [
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _generate_step(
            client,
            optional_project["project_id"],
            step_key,
            {"skip": True, "skip_reason": f"not needed for this quality pass: {step_key}"},
        )
    _approve_generated_step(client, optional_project["project_id"], "scene_list")
    _approve_generated_step(client, optional_project["project_id"], "scene_details")

    optional_workspace = client.get(
        f"/api/v2/projects/{optional_project['project_id']}/snowflake-workspace"
    ).json()["data"]
    assert optional_workspace["current_step_key"] is None
    assert optional_workspace["materialization_gate"]["status"] == "warning"
    assert optional_workspace["materialization_gate"]["blockers"] == []
    assert optional_workspace["materialization_gate"]["warnings"]
    assert any("已跳过" in warning for warning in optional_workspace["materialization_gate"]["warnings"])
    assert all("was skipped" not in warning for warning in optional_workspace["materialization_gate"]["warnings"])

    hard_project = _create_project(client, key="hard-skips")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]:
        _approve_generated_step(client, hard_project["project_id"], step_key)
    for step_key in [
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _generate_step(
            client,
            hard_project["project_id"],
            step_key,
            {"skip": True, "skip_reason": f"skip for gate test: {step_key}"},
        )
    required_skip = client.post(
        f"/api/v2/projects/{hard_project['project_id']}/snowflake-workspace/steps/scene_list/generate",
        json={"skip": True, "skip_reason": "required steps must be authored"},
        headers={"X-Idempotency-Key": f"generate-v2-{hard_project['project_id']}-scene-list-required-skip"},
    )
    assert required_skip.status_code == 400
    assert required_skip.json()["error"]["code"] == "SNOWFLAKE_STEP_NOT_SKIPPABLE"

    hard_workspace = client.get(f"/api/v2/projects/{hard_project['project_id']}/snowflake-workspace").json()["data"]
    assert hard_workspace["current_step_key"] == "scene_list"
    assert hard_workspace["materialization_gate"]["status"] == "blocked"
    assert any("场景列表" in blocker for blocker in hard_workspace["materialization_gate"]["blockers"])
    assert all("scene_list" not in blocker for blocker in hard_workspace["materialization_gate"]["blockers"])


def test_workspace_v2_skeleton_generation_uses_project_outline_instead_of_fixed_fixture(client) -> None:
    project = _create_project(
        client,
        key="outline-fallback",
        title="Glass Orchard Pact",
        genre="Eco Thriller",
        outline_text=(
            "Mira protects the glass orchard that stores the last clean rain.\n"
            "The Orchard Council poisons the water archive to hide an old pact.\n"
            "Mira must expose the water ledger before the orchard burns."
        ),
    )

    _approve_generated_step(client, project["project_id"], "book_brief")
    one_sentence = _generate_step(client, project["project_id"], "one_sentence_summary")["step"]["draft"]["summary"]
    assert "glass orchard" in one_sentence.lower() or "water archive" in one_sentence.lower()

    _approve_step(client, project["project_id"], "one_sentence_summary")
    _approve_generated_step(client, project["project_id"], "one_paragraph_summary")
    _generate_step(
        client,
        project["project_id"],
        "character_sheets",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "short_synopsis",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "character_synopses",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "long_synopsis",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "character_bibles",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    scene_list = _generate_step(client, project["project_id"], "scene_list")["step"]["draft"]["scenes"]
    scene_text = json.dumps(scene_list, ensure_ascii=False).lower()
    assert "glass orchard" in scene_text or "water archive" in scene_text


def test_workspace_v2_scene_details_support_primary_form_with_optional_followup_fields(client) -> None:
    project = _create_project(client, key="primary-form")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    scene = {
        **scene_step["draft"]["scenes"][0],
        "primary_form": "proactive",
        "scene_type": "reactive",
        "goal": "Get the sealed witness record before the archive closes.",
        "conflict": "The clerk refuses, then calls security, then reveals the file was moved.",
        "setback": "She gets the box but it points to her father, making the win costlier.",
        "reaction": "Her hands shake when she sees the family seal.",
        "dilemma": "Expose the seal and lose her family, or hide it and let the case die.",
        "decision": "She decides to meet the archivist who moved the file next.",
    }

    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [scene]}},
    )
    assert response.status_code == 200, response.text
    saved_scene = response.json()["data"]["step"]["draft"]["scenes"][0]
    assert saved_scene["primary_form"] == "proactive"
    assert saved_scene["scene_type"] == "proactive"
    assert saved_scene["reaction"].startswith("Her hands shake")
    assert saved_scene["dilemma"].startswith("Expose the seal")
