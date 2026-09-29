"""B06-16：整步生成的 ``upstream_steps`` 与教练 / AI 分诊的 ``approved_context`` 是同一种上下文、同一条规则。

以前是两套构建：生成按 ``status in {approved, skipped}`` 标 confirmed（作者点过「已复核」的过期步骤也算没确认），
教练按闸门口径（算确认）；生成剥掉前端写穿键（fe_*），教练整份照给（脚手架 JSON 与规范字段重复占预算，
作者的自由草稿 fe_text 也没有以 author_free_draft 交代出来）。同一步在两个节点里说法不一。
现在一条规则（与闸门一致：确认 / 略过，或过期但作者点过「已复核」），一种形状（剥 fe_*，自由草稿留作
author_free_draft，空骨架不进上下文）。
"""

from __future__ import annotations

import json

import pytest

from novel_system.db.models import SnowflakeStepRun, StoryProject, utcnow
from novel_system.services.llm_client import LLMResponse
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

PROJECT_ID = "prj-context-builders"
REPLY = {
    # 一份载荷同时满足整步生成（一句话概括）与教练回复的归一
    "summary": "林昭带着旧信回到雨城，才发现案卷早被人借走。",
    "reply": "先把她回城的理由写实。",
    "suggestions": [],
    "candidate_label": "",
    "candidate_patch": {},
    "brief_update": {"lines": []},
}
BRIEF = {
    "category": "悬疑",
    "target_reader": "喜欢旧案与家族秘密的读者",
    "story_kind": "追查旧案",
    "fe_text": "作者自己的话：雨城的旧信要写得冷。",
    "fe_scaffold": {"category": "悬疑", "target_reader": "喜欢旧案与家族秘密的读者"},
    "fe_state": "done",
}


@pytest.fixture()
def captured(monkeypatch):
    from novel_system.services import snowflake_workspace_llm as mod

    requests: list = []

    def fake_execute(session, client, request, context, *, llm_call_id):
        requests.append(request)
        return LLMResponse(
            request_id="r", provider="p", model="m", text=json.dumps(REPLY, ensure_ascii=False),
            structured_output=dict(REPLY), response_format="json_object", raw_response={}, usage={}, finish_reason="stop",
        )

    monkeypatch.setattr(mod, "execute_accounted_call", fake_execute)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_supplement_accounted_call", lambda self, **kwargs: None)
    return requests


def _payload(request) -> dict:
    prompt = "\n".join(str(message.get("content", "")) for message in request.messages)
    body = prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0]
    return json.loads(body)


def _seed(session) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=PROJECT_ID, title="雨城来信", outline_text="林昭带着旧信回到雨城。", planning_mode="snowflake",
            snowflake_workflow_mode="explore", target_word_count=100000,
        )
    )
    session.flush()
    # 读者定位：作者确认过，后来上游一改被打成过期，作者看过后点了「已复核」（仍然有效）
    session.add(
        SnowflakeStepRun(
            step_run_id="run-brief", project_id=PROJECT_ID, step_key="book_brief", version=1, status="stale",
            draft_json=dict(BRIEF), health_json={}, input_refs_json={}, stale_reason="上游改了",
            stale_accepted_at=utcnow(), stale_accepted_by="author",
        )
    )
    session.flush()
    return SnowflakeWorkspaceService(session)


def _brief_item(items: list[dict]) -> dict:
    return next(item for item in items if item["step_key"] == "book_brief")


def test_generation_and_coach_describe_an_accepted_stale_step_the_same_way(session, captured) -> None:
    service = _seed(session)

    service.generate_step(PROJECT_ID, "one_sentence_summary", {})
    upstream = _brief_item(_payload(captured[-1])["upstream_steps"])
    service.request_assistant(PROJECT_ID, {"step_key": "one_sentence_summary", "message": "开头怎么写？"})
    coach = _brief_item(_payload(captured[-1])["approved_context"])

    for item in (upstream, coach):
        assert item["confirmed"] is True, "作者点过「已复核」的过期步骤是确认过的事实（与闸门同一条规则）"
        assert item["status"] == "stale"
        assert not any(key.startswith("fe_") for key in item["draft"]), "前端写穿键不进提示词"
        assert item["draft"]["author_free_draft"] == BRIEF["fe_text"]
        assert item["draft"]["target_reader"] == BRIEF["target_reader"]
    assert upstream == coach


def test_a_step_that_only_carries_frontend_keys_stays_out_of_the_coach_context(session, captured) -> None:
    service = _seed(session)
    session.add(
        SnowflakeStepRun(
            step_run_id="run-sheets", project_id=PROJECT_ID, step_key="character_sheets", version=1,
            status="pending_review", draft_json={"fe_state": "active", "fe_scaffold": {"sel": "c1"}},
            health_json={}, input_refs_json={},
        )
    )
    session.flush()

    service.request_assistant(PROJECT_ID, {"step_key": "book_brief", "message": "读者是谁？"})
    keys = [item["step_key"] for item in _payload(captured[-1])["approved_context"]]
    assert "character_sheets" not in keys, "只有前端脚手架、没有一个规范字段的步骤是空骨架"
    assert "book_brief" in keys
