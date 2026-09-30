"""雪花整步生成的上游上下文契约。

回归的真实故障（作品《何有》）：前八步全是 pending_review，上下文构建只收
approved/skipped，于是模型只看到书名，凭空另编一本书——场景列表主角叫「林一鸣」，
与前八步的「何有」毫无关系。
"""
from __future__ import annotations

import json

import pytest

from novel_system.db.models import SnowflakeStepRun, StoryProject
from novel_system.services.llm_client import LLMResponse
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

UPSTREAM_DRAFTS = {
    "book_brief": {"category": "悬疑", "target_reader": "都市读者", "story_kind": "追凶",
                   "delight_reason": "抽丝剥茧", "genre_promise": "真相必揭",
                   "expected_reader_emotion": "窒息感"},
    "one_sentence_summary": {"summary": "何有借胜局曝光真相，反陷死局遭连环追杀。"},
    "one_paragraph_summary": {
        "sentences": ["何有入侵警局数据库寻找失踪的妹妹", "线人阿坤被灭口，他背上血债",
                      "他营救妹妹反被诬陷入狱", "出狱后面临公开证据或沉默的抉择",
                      "他匿名发布证据并自首"],
        "moral_premise": "逃避真相带来更大的毁灭"},
    "character_sheets": {"characters": [
        {"character_id": "HY", "display_name": "何有", "role": "主角", "goal": "找回妹妹"},
        {"character_id": "LXZ", "display_name": "林修竹", "role": "对手", "goal": "掩盖交易"}]},
    "short_synopsis": {"paragraphs": ["何有入侵警局", "阿坤之死", "诬陷入狱", "狱中真相", "自首引爆"]},
    "character_synopses": {"characters": [
        {"character_id": "HY", "display_name": "何有", "synopsis": "妹妹失踪后他辞去公职专职追查。"}]},
    "long_synopsis": {"paragraphs": ["第一幕：入侵与灭口", "第二幕：诬陷与牢狱", "第三幕：证据与殉道"]},
    "character_bibles": {"characters": [{"character_id": "HY", "display_name": "何有"}]},
}

# 前序事实：只要模型看得见上游，这些词就必须出现在提示词里
UPSTREAM_FACTS = ["何有", "林修竹", "阿坤", "逃避真相带来更大的毁灭", "第二幕：诬陷与牢狱"]


@pytest.fixture()
def captured_requests(monkeypatch):
    """截住 provider 调用，留下请求供断言；不真正发网络请求。"""
    from novel_system.services import snowflake_workspace_llm as mod

    captured: list = []

    def fake_execute(session, client, request, context, *, llm_call_id):
        captured.append(request)
        payload = {"scenes": [{"scene_seq": 1, "summary": "何有夜探档案室", "primary_form": "proactive",
                               "scene_type": "proactive", "location": "警局档案室",
                               "crucible": "退无可退", "chapter_role": "起疑", "pov_character_id": "HY"}]}
        return LLMResponse(request_id="r", provider="p", model="m",
                           text=json.dumps(payload, ensure_ascii=False), structured_output=payload,
                           response_format="json_object", raw_response={}, usage={}, finish_reason="stop")

    monkeypatch.setattr(mod, "execute_accounted_call", fake_execute)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod, "supplement_accounted_call", lambda session, llm_call_id, **kwargs: None)
    return captured


def _seed_project(session, *, status: str, project_id: str = "prj-upstream") -> str:
    session.add(StoryProject(
        project_id=project_id, title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    session.flush()
    for step_key, draft in UPSTREAM_DRAFTS.items():
        session.add(SnowflakeStepRun(
            step_run_id=f"{project_id}-{step_key}", project_id=project_id, step_key=step_key,
            version=1, status=status, draft_json=draft, health_json={}, input_refs_json={},
        ))
    session.flush()
    return project_id


def _prompt_payload(request) -> dict:
    prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
    body = prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0]
    return json.loads(body)


@pytest.mark.parametrize("status", ["approved", "pending_review", "stale", "draft"])
def test_upstream_steps_reach_the_prompt_regardless_of_confirmation(session, captured_requests, status):
    """未确认/已过期的上游草稿同样是这本书的故事，必须进提示词。"""
    project_id = _seed_project(session, status=status)
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})

    payload = _prompt_payload(captured_requests[-1])
    seen = {item["step_key"] for item in payload["upstream_steps"]}
    assert seen == set(UPSTREAM_DRAFTS), f"status={status} 时上游步骤丢失：缺 {set(UPSTREAM_DRAFTS) - seen}"

    prompt_text = json.dumps(payload, ensure_ascii=False)
    for fact in UPSTREAM_FACTS:
        assert fact in prompt_text, f"status={status} 时前序事实「{fact}」没进提示词"


def test_upstream_steps_carry_confirmation_status(session, captured_requests):
    """状态要如实标注——模型据此知道哪些还可能变，但不得因此忽略。"""
    project_id = _seed_project(session, status="pending_review")
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})

    payload = _prompt_payload(captured_requests[-1])
    upstream = payload["upstream_steps"]
    assert len(upstream) == len(UPSTREAM_DRAFTS)
    assert all(item["confirmed"] is False for item in upstream)
    assert all(item["status"] == "pending_review" for item in upstream)
    assert payload["upstream_steps_how_to_use"]


def test_upstream_steps_are_ordered_and_exclude_self_and_downstream(session, captured_requests):
    """只给本步之前的材料，按雪花顺序——本步草稿走 current_draft，不重复占预算。"""
    project_id = _seed_project(session, status="pending_review")
    session.add(SnowflakeStepRun(
        step_run_id="downstream", project_id=project_id, step_key="scene_details",
        version=1, status="pending_review", draft_json={"scenes": [{"scene_id": "S1", "title": "下游"}]},
        health_json={}, input_refs_json={},
    ))
    session.flush()

    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    keys = [item["step_key"] for item in _prompt_payload(captured_requests[-1])["upstream_steps"]]

    assert keys == list(UPSTREAM_DRAFTS), "上游步骤必须按雪花顺序排列"
    assert "scene_list" not in keys and "scene_details" not in keys


def test_empty_upstream_skeletons_do_not_enter_the_prompt(session, captured_requests):
    """还没写的步骤（空骨架）不占提示预算，也不能让模型误以为作者交代过什么。"""
    session.add(StoryProject(
        project_id="prj-blank", title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    session.add(SnowflakeStepRun(
        step_run_id="blank-1", project_id="prj-blank", step_key="one_sentence_summary",
        version=1, status="pending_review", draft_json={"summary": "   "},
        health_json={}, input_refs_json={},
    ))
    session.add(SnowflakeStepRun(
        step_run_id="blank-2", project_id="prj-blank", step_key="one_paragraph_summary",
        version=1, status="pending_review", draft_json=UPSTREAM_DRAFTS["one_paragraph_summary"],
        health_json={}, input_refs_json={},
    ))
    session.flush()

    SnowflakeWorkspaceService(session).generate_step("prj-blank", "scene_list", {})
    keys = [item["step_key"] for item in _prompt_payload(captured_requests[-1])["upstream_steps"]]
    assert keys == ["one_paragraph_summary"]
