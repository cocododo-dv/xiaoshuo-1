"""雪花构思工作台接模型的一面：整步生成经记账调用落库、模型回空表 / 节点没路由 / 协议不符时怎么说、教练带草稿覆盖回补丁。

模型一律是替身（``patch_llm_client_generate`` 换掉 ``LLMClient.generate_accounted``）。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    LlmCall,
    LlmCallAttempt,
    SnowflakeStepRun,
)
from novel_system.services.llm_client import LLMResponse
from tests.support.snowflake import (
    approve_generated_step as _approve_generated_step,
    create_workspace_project as _create_project,
    generate_workspace_step as _generate_step,
    patch_llm_client_generate as _patch_accounted_generate,
)

pytestmark = pytest.mark.usefixtures("online_author_pipeline", "skeleton_snowflake")


def test_workspace_v2_step_generation_uses_llm_and_persists_project_scoped_call(client, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request):  # noqa: ANN001
        payload = {
            "category": "Urban Mystery",
            "target_reader": "Readers who want old cases, family cost, and a tightly pressured heroine.",
            "story_kind": "A cost-heavy mystery that drags a heroine back into family truth.",
            "delight_reason": "Every clue closes distance to the truth and increases the personal price.",
            "genre_promise": "The clearer the truth becomes, the more the heroine risks losing.",
            "expected_reader_emotion": "Pressure, doubt, and urgent forward pull.",
            "safety_rules": [
                "Only borrow abstract craft patterns and pacing.",
                "Do not copy characters, settings, plot beats, or signature phrasing.",
            ],
        }
        return LLMResponse(
            request_id="resp_snowflake_generate",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_snowflake_generate"},
            usage={"input_tokens": 111, "output_tokens": 222, "total_tokens": 333},
            raw_usage={"input_tokens": 111, "output_tokens": 222, "total_tokens": 333},
            usage_present=True,
            usage_complete=True,
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)
    project = _create_project(client, key="llm-step")

    result = _generate_step(client, project["project_id"], "book_brief")
    step = result["step"]

    assert step["draft"]["category"] == "Urban Mystery"
    assert step["last_generation_source"] == "llm"
    assert step["last_llm_call_id"]
    assert step["artifact"]["llm_call_id"] == step["last_llm_call_id"]

    session.expire_all()
    stored_call = session.get(LlmCall, step["last_llm_call_id"])
    assert stored_call is not None
    assert stored_call.project_id == project["project_id"]
    assert (stored_call.scope_type, stored_call.scope_id) == (
        "project",
        project["project_id"],
    )
    assert stored_call.request_payload_summary["step_key"] == "book_brief"
    assert stored_call.request_payload_summary["step_label"]["kind"] == "text_fingerprint"
    assert stored_call.request_payload_summary["step_english_label"]["kind"] == "text_fingerprint"
    assert stored_call.request_payload_summary["step_instruction"]["kind"] == "text_fingerprint"
    assert stored_call.request_payload_summary["pressure_rubric"]["goal"]["kind"] == "text_fingerprint"
    assert "book_brief" in stored_call.request_payload_summary["current_pressure_diagnosis"]["step_key"]
    assert stored_call.response_payload_summary["structured_output"]["kind"] == "json_fingerprint"
    assert "category" in stored_call.response_payload_summary["structured_output"]["top_level_fields"]
    assert stored_call.accounting_status == "settled"
    assert stored_call.usage_is_estimate is False
    attempt = session.scalars(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == stored_call.llm_call_id
        )
    ).one()
    assert attempt.accounting_status == "settled"
    assert attempt.total_tokens == 333

    step_run = session.get(SnowflakeStepRun, step["artifact"]["step_run_id"])
    assert step_run is not None
    assert step_run.llm_call_id == stored_call.llm_call_id


def test_workspace_v2_live_rejects_blank_character_sheet_llm_candidates(client, session, monkeypatch) -> None:
    project = _create_project(client, key="blank-character-sheet")
    for step_key in ["book_brief", "one_sentence_summary", "one_paragraph_summary"]:
        _approve_generated_step(client, project["project_id"], step_key)

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request):  # noqa: ANN001
        payload = {
            "characters": [
                {
                    "character_id": f"{project['project_id']}_CHAR01",
                    "display_name": f"{project['project_id']}_CHAR01",
                    "role": "",
                    "goal": "",
                    "ambition": "",
                    "values": [],
                    "conflict": "",
                    "epiphany": "",
                    "one_sentence_summary": "",
                    "one_paragraph_summary": "",
                }
            ]
        }
        return LLMResponse(
            request_id="resp_blank_character_sheet",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_blank_character_sheet"},
            usage={"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/character_sheets/generate",
        json={},
        headers={"X-Idempotency-Key": "blank-character-sheet"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA"
    # 2026-09-16：报错是给作者看的中文；稀疏结果先带原因重试一次，再空才到这里（details.sparse_output）
    assert "角色摘要表" in error["message"] and "过于稀疏" in error["message"]
    assert "role" in error["message"]
    assert error["details"]["node_id"] == "snowflake_step_generate"
    assert error["details"]["sparse_output"] is True
    assert error["details"]["next_action"] == "regenerate_with_substantive_content"

    session.expire_all()
    assert (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "character_sheets",
        )
        .count()
        == 0
    )
    # 首轮 + 一次带原因的重试：两次调用都如实记成 failed / INVALID_SCHEMA，每次的 attempt 都已结算
    failed_calls = session.scalars(
        select(LlmCall).where(
            LlmCall.project_id == project["project_id"],
            LlmCall.node_id == "snowflake_step_generate",
        )
    ).all()
    assert len(failed_calls) == 2
    for failed_call in failed_calls:
        assert failed_call.accounting_status == "failed"
        assert failed_call.error_code == "LLM_RESPONSE_INVALID_SCHEMA"
        failed_attempt = session.scalars(
            select(LlmCallAttempt).where(
                LlmCallAttempt.llm_call_id == failed_call.llm_call_id
            )
        ).one()
        assert failed_attempt.accounting_status == "settled"


def test_workspace_v2_live_missing_snowflake_route_explains_node_route_gap(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    from novel_system.services.llm_client import ModelRoutingConfig

    monkeypatch.setattr(
        "novel_system.services.llm_service_base.load_model_routing_config",
        lambda: ModelRoutingConfig(
            node_routing={},
            task_routing={},
            retry_budget={},
            job_runtime={},
        ),
    )

    project = _create_project(client, key="missing-snowflake-route")
    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/generate",
        json={},
        headers={"X-Idempotency-Key": "missing-snowflake-route"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING"
    assert "模型已接入" in error["message"]
    assert "snowflake_step_generate" in error["message"]
    assert "一键补齐" in error["message"]
    assert error["details"]["node_id"] == "snowflake_step_generate"
    assert error["details"]["next_action"] == "sync_missing_llm_node_routes"


def test_workspace_v2_live_responses_404_explains_protocol_mismatch(
    client, session, monkeypatch
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    from novel_system.services.llm_client import LLMHTTPError

    def fake_generate(self, request):  # noqa: ANN001
        raise LLMHTTPError(
            "LLM_HTTP_FAILURE",
            "llm request failed with status 404",
            status_code=404,
            details={
                "provider_id": "gcli2api",
                "provider_type": "openai",
                "model": "gemini-3.1-pro-preview",
                "api_mode": "responses",
                "endpoint": "/responses",
                "next_action": "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider",
            },
        )

    _patch_accounted_generate(monkeypatch, fake_generate)
    project = _create_project(client, key="responses-404-hint")
    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/generate",
        json={},
        headers={"X-Idempotency-Key": "responses-404-hint"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_CALL_FAILED"
    assert "Responses API" in error["message"]
    assert "chat" in error["message"]
    assert "gcli2api" in error["message"]
    assert error["details"]["response_summary"]["details"]["endpoint"] == "/responses"
    assert (
        error["details"]["response_summary"]["details"]["next_action"]
        == "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider"
    )
    session.expire_all()
    parent = session.scalars(
        select(LlmCall).where(
            LlmCall.project_id == project["project_id"],
            LlmCall.node_id == "snowflake_step_generate",
        )
    ).one()
    assert parent.accounting_status == "failed"
    assert parent.error_code == "LLM_HTTP_FAILURE"
    assert parent.step == "book_brief"
    child = session.scalars(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == parent.llm_call_id
        )
    ).one()
    assert child.accounting_status == "failed"


def test_workspace_v2_assistant_uses_draft_override_and_returns_candidate_patch(client, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    override_reader = "Unsaved reader focus: readers who want cold cases and family-cost pressure."

    def fake_generate(self, request):  # noqa: ANN001
        assert override_reader in request.messages[-1]["content"]
        payload = {
            "reply": "Tighten the reader promise around old cases plus family cost.",
            "suggestions": ["Lead with the cost first, then the genre pleasure."],
            "candidate_label": "Narrow target reader",
            "candidate_patch": {
                "target_reader": "Readers who want old cases and unresolved family cost.",
                "genre_promise": "The clearer the truth becomes, the more family debt it exposes.",
                "illegal_field": "This must not be written back.",
            },
        }
        return LLMResponse(
            request_id="resp_snowflake_assistant",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_snowflake_assistant"},
            usage={"input_tokens": 90, "output_tokens": 120, "total_tokens": 210},
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)
    project = _create_project(client, key="assistant-override")

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/assistant",
        json={
            "step_key": "book_brief",
            "message": "Help me narrow the reader promise.",
            "draft_override": {
                "category": "Urban Mystery",
                "target_reader": override_reader,
            },
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]

    assert payload["source"] == "llm"
    assert payload["candidate_label"] == "Narrow target reader"
    assert payload["candidate_patch"]["target_reader"].startswith("Readers who want old cases")
    assert "illegal_field" not in payload["candidate_patch"]
    assert payload["llm_call_id"]

    session.expire_all()
    stored_call = session.get(LlmCall, payload["llm_call_id"])
    assert stored_call is not None
    assert stored_call.project_id == project["project_id"]
    assert (stored_call.scope_type, stored_call.scope_id) == (
        "project",
        project["project_id"],
    )
    assert stored_call.request_payload_summary["draft"]["kind"] == "json_fingerprint"
    assert stored_call.request_payload_summary["pressure_rubric"]["dimensions"]
    assert stored_call.request_payload_summary["current_pressure_diagnosis"]["pressure_flags"]
    assert stored_call.accounting_status == "settled"
    assert stored_call.usage_is_estimate is True
    assert stored_call.total_tokens > 0
