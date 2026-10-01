"""雪花第 10 步的场景分诊：规则先诊断（缺拍 / 占位 / 只给建议的压力提示）、作者裁定与修补补丁落库、AI 分诊建议只读且没有模型就拒绝、「该重写」只是确认写入的警告。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    LlmCall,
    OutlinePlan,
    SceneCard,
    SnowflakeArtifact,
    SnowflakeScenePlan,
    SnowflakeSceneTriageItem,
    SnowflakeStepRun,
)
from novel_system.services.llm_client import LLMResponse
from tests.support.snowflake import (
    approve_generated_step as _approve_generated_step,
    approve_workspace_step as _approve_step,
    create_workspace_project as _create_project,
    patch_llm_client_generate as _patch_accounted_generate,
)

pytestmark = pytest.mark.usefixtures("online_author_pipeline", "skeleton_snowflake")


def test_workspace_v2_uses_structured_scene_plans_and_persists_triage_repair_patches(client, session) -> None:
    project = _create_project(client, key="structured-scene-plans")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)
    _approve_generated_step(client, project["project_id"], "scene_list")
    _approve_generated_step(client, project["project_id"], "scene_details")

    session.expire_all()
    assert (
        session.query(SnowflakeArtifact)
        .filter(SnowflakeArtifact.project_id == project["project_id"])
        .count()
        == 0
    )
    assert (
        session.query(SnowflakeStepRun)
        .filter(SnowflakeStepRun.project_id == project["project_id"])
        .count()
        == 10
    )
    scene_plan = (
        session.query(SnowflakeScenePlan)
        .filter(SnowflakeScenePlan.project_id == project["project_id"])
        .order_by(SnowflakeScenePlan.chapter_id.asc(), SnowflakeScenePlan.scene_seq.asc())
        .first()
    )
    assert scene_plan is not None
    assert scene_plan.scene_plan_id

    def patch_scene(**fields) -> dict:
        # 作者改一场走第 10 步的草稿（R15a 删掉了逐场的 PATCH …/scenes/{id}）
        workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
        rows = [dict(row) for row in next(s for s in workspace["steps"] if s["step_key"] == "scene_details")["draft"]["scenes"]]
        for row in rows:
            if row["scene_plan_id"] == scene_plan.scene_plan_id:
                row.update(fields)
        response = client.patch(
            f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
            json={"draft": {"scenes": rows}},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["workspace"]

    broken_workspace = patch_scene(setback="", goal="Get the witness statement before the train leaves.")
    broken_scene = next(
        item
        for item in next(s for s in broken_workspace["steps"] if s["step_key"] == "scene_details")["draft"]["scenes"]
        if item["scene_plan_id"] == scene_plan.scene_plan_id
    )
    assert broken_scene["goal"].startswith("Get the witness")
    assert broken_scene["setback"] == ""

    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_plan_id": scene_plan.scene_plan_id,
                    "scene_id": scene_plan.scene_id,
                    "status": "maybe",
                    "notes": "The scene can work if the ending leaves a stronger cost.",
                    "missing_fields": ["setback"],
                    "fix_steps": ["Make the ending leave the protagonist worse off."],
                    "repair_patch": {"setback": "The witness gives proof, but it publicly implicates the heroine's family."},
                }
            ]
        },
    )
    assert triage_response.status_code == 200, triage_response.text
    triage_payload = triage_response.json()["data"]
    triage_item = triage_payload["items"][0]
    assert triage_item["triage_id"]
    assert triage_item["repair_patch"]["setback"].startswith("The witness gives proof")

    # 修复补丁由作者在第 10 步里采纳（前端写进本地计划、随草稿上行）；服务端不再有「一键应用」的旁路
    patch_scene(setback=triage_item["repair_patch"]["setback"])

    session.expire_all()
    stored_triage = session.get(SnowflakeSceneTriageItem, triage_item["triage_id"])
    assert stored_triage is not None
    assert stored_triage.repair_patch_json["setback"].startswith("The witness gives proof")
    stored_scene = session.get(SnowflakeScenePlan, scene_plan.scene_plan_id)
    assert stored_scene.setback.startswith("The witness gives proof")


def test_workspace_v2_persists_scene_triage_and_approves_materialized_outline(client, session) -> None:
    project = _create_project(client, key="triage-materialize")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)
    _approve_generated_step(client, project["project_id"], "scene_list")
    _approve_generated_step(client, project["project_id"], "scene_details")

    workspace_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    workspace = workspace_response.json()["data"]
    scene_details = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    first_scene_id = scene_details["draft"]["scenes"][0]["scene_id"]

    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_id": first_scene_id,
                    "status": "maybe",
                    "notes": "Raise the cost inside the scene conflict.",
                }
            ]
        },
    )
    assert triage_response.status_code == 200, triage_response.text
    triage = triage_response.json()["data"]
    assert triage["items"][0]["status"] == "maybe"
    assert triage["items"][0]["notes"].startswith("Raise the cost")
    assert triage["workspace"]["materialization_gate"]["status"] == "warning"
    assert triage["workspace"]["materialization_gate"]["warnings"]

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-workspace-v2"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    preview = materialize_response.json()["data"]
    assert preview["plan"]["status"] == "pending_review"
    assert preview["workspace"]["latest_plan"]["plan_json"]["source"] == "snowflake_method"

    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-workspace-v2-outline"},
    )
    assert approve_response.status_code == 200, approve_response.text
    approved = approve_response.json()["data"]
    assert approved["workspace"]["project"]["status"] in {"chapter_ready", "chapter_running", "chapter_blocked", "chapter_final_review"}

    session.expire_all()
    plan_id = approved["plan"]["plan_id"]
    db_plan = session.get(OutlinePlan, plan_id)
    first_scene = db_plan.plan_json["chapters"][0]["scenes"][0]
    scene = session.get(SceneCard, first_scene["scene_id"])
    assert scene is not None
    assert scene.writer_brief_json["source"] == "snowflake_method"
    assert scene.writer_brief_json["scene_crucible"]
    # 阶段 B（B5）：角色摘要表里的主角随物化进入每一场的简报——挫折以此人衡量
    assert scene.writer_brief_json["protagonist_hint"] == "Lead"
    assert scene.writer_brief_json["protagonist_character_id"] == f"{project['project_id']}_CHAR01"


def test_workspace_v2_cost_requirement_clears_missing_flag_and_persists_through_materialization(client, session) -> None:
    """回归守护：cost_requirement 曾经在编辑器模板/PATCH 白名单/场景序列化/LLM 清洗里
    全链路缺失——填了也存不住、诊断永远报 missing_cost_requirement。这里验证：填入后
    诊断标记消失、分数提升，且值能一路存活到 SnowflakeScenePlan 和物化后的 SceneCard。"""
    project = _create_project(client, key="cost-requirement")
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
    scenes = [dict(s) for s in scene_step["draft"]["scenes"]]
    assert len(scenes) >= 2, "需要至少两个场景来对比有/无 cost_requirement 的诊断差异"

    baseline_items = workspace["triage_items"]
    baseline_item = next(item for item in baseline_items if item["scene_id"] == scenes[0]["scene_id"])
    # 阶段 H：缺代价只提醒，不再是旗标、不再扣分
    assert "missing_cost_requirement" not in baseline_item["pressure_flags"], baseline_item
    assert any("代价" in step for step in baseline_item["fix_steps"]), baseline_item
    baseline_score = baseline_item["score"]

    cost_text = "拿到线索的代价是永久失去这个线人的信任。"
    scenes[0] = {**scenes[0], "cost_requirement": cost_text}

    patch_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": scenes}},
    )
    assert patch_response.status_code == 200, patch_response.text
    triage_items = patch_response.json()["data"]["workspace"]["triage_items"]
    filled_item = next(item for item in triage_items if item["scene_id"] == scenes[0]["scene_id"])
    bare_item = next(item for item in triage_items if item["scene_id"] == scenes[1]["scene_id"])
    # 同一场景填前/填后对比：只应该是 cost_requirement 这一个变量的效应——建议消失，分数不变（不扣分）。
    assert "missing_cost_requirement" not in filled_item["pressure_flags"], filled_item
    assert not any("免费选择" in step for step in filled_item["fix_steps"]), filled_item
    assert filled_item["score"] == baseline_score
    # 没碰过的场景（对照组）应该保持原样，继续提醒。
    assert any("免费选择" in step for step in bare_item["fix_steps"]), bare_item

    # 编辑已确认的 scene_details 会把它打回 pending_review，需要重新确认才能物化。
    _approve_step(client, project["project_id"], "scene_details")

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-cost-requirement"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-cost-requirement-outline"},
    )
    assert approve_response.status_code == 200, approve_response.text

    session.expire_all()
    plan = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project["project_id"],
            SnowflakeScenePlan.scene_id == scenes[0]["scene_id"],
        )
    ).scalars().first()
    assert plan is not None
    assert plan.cost_requirement == cost_text

    scene_card = session.get(SceneCard, scenes[0]["scene_id"])
    assert scene_card is not None
    assert scene_card.writer_brief_json["cost_requirement"] == cost_text


def test_workspace_v2_scene_triage_suggest_returns_non_persistent_suggestions(client, session, monkeypatch) -> None:
    project = _create_project(client, key="triage-suggest")
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

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request):  # noqa: ANN001
        payload = {
            "items": [
                {
                    "scene_id": f"{project['project_id']}_CH01_SC01",
                    "status": "maybe",
                    "notes": "The scene has desire, but the conflict cost should climb further.",
                    "missing_fields": ["setback"],
                    "fix_steps": ["Make the final turn leave the protagonist worse off."],
                }
            ]
        }
        return LLMResponse(
            request_id="resp_scene_triage_suggest",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_scene_triage_suggest"},
            usage={"input_tokens": 55, "output_tokens": 77, "total_tokens": 132},
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage/suggest",
        json={},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["source"] == "llm"
    assert payload["items"][0]["status"] == "maybe"
    assert payload["items"][0]["missing_fields"] == ["setback"]
    assert payload["items"][0]["fix_steps"][0].startswith("Make the final turn")
    assert payload["llm_call_id"]

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    assert workspace["triage_items"][0]["status"] == ""
    assert workspace["triage_items"][0]["notes"] == ""

    session.expire_all()
    stored_call = session.get(LlmCall, payload["llm_call_id"])
    assert stored_call is not None
    assert (stored_call.scope_type, stored_call.scope_id) == (
        "project",
        project["project_id"],
    )
    proactive = stored_call.request_payload_summary["pressure_rubric"]["scene_rules"]["proactive"]
    assert len(proactive) == 3
    assert all(item["kind"] == "text_fingerprint" for item in proactive)
    assert stored_call.request_payload_summary["current_pressure_diagnosis"]["step_key"] == "scene_details"
    assert (
        session.query(SnowflakeSceneTriageItem)
        .filter(SnowflakeSceneTriageItem.project_id == project["project_id"])
        .count()
        == 0
    )


def test_workspace_v2_scene_triage_suggest_is_fail_closed_without_llm(client, session, monkeypatch) -> None:
    """B06-20：AI 分诊与教练 / 方向同一条路——没有模型就 409 + 去配置的 author_action，不拿规则诊断冒充 AI 分诊。
    规则诊断照旧不用点就在 triage_items 里（triage_source = auto_diagnosis）。"""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    project = _create_project(client, key="triage-fail-closed")
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

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage/suggest",
        json={},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"
    assert error["details"]["author_action"]
    assert error["details"]["node_id"] == "snowflake_scene_triage"

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    assert workspace["triage_items"]
    assert all(item["triage_source"] == "auto_diagnosis" for item in workspace["triage_items"])
    session.expire_all()
    assert (
        session.query(SnowflakeSceneTriageItem)
        .filter(SnowflakeSceneTriageItem.project_id == project["project_id"])
        .count()
        == 0
    )


def test_workspace_v2_persists_triage_repair_metadata_and_the_rewrite_verdict_is_a_gate_warning(client, session) -> None:
    project = _create_project(client, key="triage-blocks-materialize")
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
    first_scene_id = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")["draft"]["scenes"][0]["scene_id"]
    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_id": first_scene_id,
                    "status": "rewrite",
                    "notes": "This scene has no recoverable pressure.",
                    "missing_fields": ["goal", "conflict", "setback"],
                    "fix_steps": ["Rebuild the scene premise before materializing."],
                }
            ]
        },
    )

    assert triage_response.status_code == 200, triage_response.text
    triaged = triage_response.json()["data"]
    assert triaged["items"][0]["missing_fields"] == ["goal", "conflict", "setback"]
    assert triaged["items"][0]["fix_steps"][0].startswith("Rebuild")
    # 阶段 N（2026-09-15）：作者的「该重写」不再阻断全书——这一场被排除在物化之外（警告），其余照常整理。
    gate = triaged["workspace"]["materialization_gate"]
    assert gate["status"] != "blocked"
    rewrite_items = [item for item in gate["items"] if item["kind"] == "triage_rewrite"]
    assert len(rewrite_items) == 1 and rewrite_items[0]["severity"] == "warning"
    assert "不建它的场景卡" in rewrite_items[0]["message"] and "该重写" in rewrite_items[0]["message"]

    session.expire_all()
    first_triage = (
        session.query(SnowflakeSceneTriageItem)
        .filter(SnowflakeSceneTriageItem.project_id == project["project_id"])
        .first()
    )
    assert first_triage is not None
    assert first_triage.missing_fields_json == ["goal", "conflict", "setback"]
    assert first_triage.fix_steps_json[0].startswith("Rebuild")


def test_workspace_v2_computes_rule_first_scene_diagnostics_and_recommends_rewrite_without_blocking(client) -> None:
    project = _create_project(client, key="auto-diagnosis")
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

    # 形态归 09（第 10 步的草稿不改已有场的形态 / 视角）：先在场景列表里把前两场的形态对调并确认
    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    listed = [dict(row) for row in next(step for step in workspace["steps"] if step["step_key"] == "scene_list")["draft"]["scenes"]]
    listed[0].update(primary_form="reactive", scene_type="reactive")
    listed[1].update(primary_form="proactive", scene_type="proactive")
    relisted = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_list",
        json={"draft": {"scenes": listed}},
    )
    assert relisted.status_code == 200, relisted.text
    _approve_step(client, project["project_id"], "scene_list")

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    first_scene = scene_step["draft"]["scenes"][0]
    broken_scene = {
        **first_scene,
        "title": "",
        "summary": "",
        "primary_form": "reactive",
        "scene_type": "reactive",
        "crucible": "",
        "scene_crucible": "",
        "reaction": "",
        "dilemma": "",
        "decision": "",
    }
    partial_scene = {
        **scene_step["draft"]["scenes"][1],
        "primary_form": "proactive",
        "scene_type": "proactive",
        "crucible": "The heroine cannot leave without losing the only witness.",
        "goal": "Get the witness statement before the train leaves.",
        "conflict": "The witness keeps changing the story as family pressure rises.",
        "setback": "",
    }

    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [broken_scene, partial_scene]}},
    )
    assert save_response.status_code == 200, save_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-auto-diagnosis-scenes"},
    )
    assert approve_response.status_code == 200, approve_response.text

    diagnosed = approve_response.json()["data"]["workspace"]
    triage_items = diagnosed["triage_items"]
    rewrite_item = next(item for item in triage_items if item["scene_id"] == broken_scene["scene_id"])
    maybe_item = next(item for item in triage_items if item["scene_id"] == partial_scene["scene_id"])

    assert rewrite_item["status"] == ""
    assert rewrite_item["recommended_status"] == "rewrite"
    assert rewrite_item["triage_source"] == "auto_diagnosis"
    assert rewrite_item["effective_status"] == "unreviewed"
    assert rewrite_item["blocking"] is False
    assert rewrite_item["score"] < 40
    assert rewrite_item["missing_fields"] == ["crucible", "reaction", "dilemma", "decision"]
    assert "scene_core_empty" in rewrite_item["pressure_flags"]
    assert rewrite_item["fix_steps"]

    assert maybe_item["recommended_status"] == "maybe"
    assert maybe_item["triage_source"] == "auto_diagnosis"
    assert maybe_item["effective_status"] == "unreviewed"
    assert maybe_item["blocking"] is False
    assert maybe_item["score"] < 100
    assert maybe_item["missing_fields"] == ["setback"]

    gate = diagnosed["materialization_gate"]
    # 阶段 H：规则层的「重写」只是缺失 / 占位的机械判断——是警告，不是 blocker；作者与 LLM 分诊拍板
    assert gate["status"] == "warning"
    assert any(broken_scene["scene_id"] in warning for warning in gate["warnings"])
    assert any(item["kind"] == "triage_unreviewed_rewrite" and item["severity"] == "warning" for item in gate["items"])
    assert not any(item["kind"] == "triage_confirmation_required" for item in gate["items"])
    assert all("marked rewrite" not in blocker for blocker in gate["blockers"])


def test_workspace_v2_weak_scene_pressure_is_advice_not_a_flag(client) -> None:
    project = _create_project(client, key="weak-scene-pressure")
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
    weak_proactive = {
        **scene_step["draft"]["scenes"][0],
        "scene_type": "proactive",
        "scene_crucible": "A room.",
        "goal": "Talk to the witness.",
        "conflict": "They argue.",
        "setback": "She succeeds.",
        "cost_requirement": "She loses something.",
    }
    weak_reactive = {
        **scene_step["draft"]["scenes"][1],
        "scene_type": "reactive",
        "scene_crucible": "She feels bad.",
        "reaction": "She is upset.",
        "dilemma": "Stay or leave.",
        "decision": "She decides.",
        "cost_requirement": "She gives up something.",
    }

    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [weak_proactive, weak_reactive]}},
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["workspace"]["triage_items"]
    proactive_item = next(item for item in items if item["scene_id"] == weak_proactive["scene_id"])
    reactive_item = next(item for item in items if item["scene_id"] == weak_reactive["scene_id"])

    # 阶段 B / H：规则层只认「缺失 / 占位」；泛泛短语、缺代价、「挫折没有代价」这类质量判断全部降为建议。
    assert proactive_item["recommended_status"] == "pass"
    assert proactive_item["pressure_flags"] == []
    assert proactive_item["score"] == 100
    assert any(step.startswith("建议：") and "泛泛短语" in step for step in proactive_item["fix_steps"])  # "They argue."
    assert any(step.startswith("建议：") and "挫折" in step for step in proactive_item["fix_steps"])  # "She succeeds."

    assert reactive_item["recommended_status"] == "pass"
    assert reactive_item["pressure_flags"] == []
    assert "fake_dilemma" not in reactive_item["pressure_flags"]
    assert any(step.startswith("建议：") and "泛泛短语" in step for step in reactive_item["fix_steps"])  # "Stay or leave." / "She decides."


def test_workspace_v2_manual_triage_override_of_auto_rewrite_becomes_gate_warning(client) -> None:
    project = _create_project(client, key="auto-diagnosis-override")
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
    first_scene = {
        **scene_step["draft"]["scenes"][0],
        "title": "",
        "summary": "",
        "scene_type": "proactive",
        "crucible": "",
        "scene_crucible": "",
        "goal": "",
        "conflict": "",
        "setback": "",
    }
    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [first_scene]}},
    )
    assert save_response.status_code == 200, save_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-auto-diagnosis-override-scenes"},
    )
    assert approve_response.status_code == 200, approve_response.text
    # 阶段 H：规则层的「重写」是警告，不再挡物化
    assert approve_response.json()["data"]["workspace"]["materialization_gate"]["status"] == "warning"

    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_id": first_scene["scene_id"],
                    "status": "maybe",
                    "notes": "I will keep the scene but repair the pressure before drafting.",
                }
            ]
        },
    )
    assert triage_response.status_code == 200, triage_response.text
    payload = triage_response.json()["data"]
    item = payload["items"][0]

    assert item["recommended_status"] == "rewrite"
    assert item["effective_status"] == "maybe"
    assert item["blocking"] is False
    assert item["manual_override"] is True
    assert payload["workspace"]["materialization_gate"]["status"] == "warning"
    warnings = payload["workspace"]["materialization_gate"]["warnings"]
    assert any("人工覆盖了自动废除重写诊断" in warning for warning in warnings)
    assert all("overrides an automatic rewrite diagnosis" not in warning for warning in warnings)
