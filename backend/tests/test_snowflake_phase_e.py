"""2026-09-13 阶段 E：回溯不只是一个标签。

步骤载荷的 ``artifact.input_refs`` 记录本版写入时消费的上游 step_run_id，上游改动后下游置 stale 时它保持不变，
前端据此对照上游历史（``history?include_draft=true``）把「上游改了什么」拉成消费版本 vs 当前版本的 diff；
「已复核」走 ``accept-stale`` 在服务端留痕；「按新上游重新展开」是带 ``source=fe_restale_regen`` 的普通 generate。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _skeleton_snowflake_generate(monkeypatch):
    """假生成已退役：本文件只回归失效 / 复核 / 重展的链路，不关心生成质量——把 generate_step 打成
    规划器骨架直通，并开 llm_enabled 过路由闸。"""
    from novel_system.services.hash_engine import normalize
    from novel_system.services.snowflake_planner import SnowflakePlannerService
    from novel_system.services.snowflake_workspace_llm import (
        SnowflakeWorkspaceLLMService,
        WorkspaceLLMResult,
    )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate_step(self, *, project, step_key, latest_by_step, **kwargs):
        payload = SnowflakePlannerService(self.session)._build_artifact_json(project, step_key, dict(latest_by_step))
        return WorkspaceLLMResult(source="llm", llm_call_id=None, payload=normalize(payload))

    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "generate_step", fake_generate_step)


def _create_project(client, key: str) -> str:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": f"阶段E {key}",
            "genre": "悬疑",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": "样例大纲第一行。\n样例大纲第二行。\n样例大纲第三行。",
        },
        headers={"X-Idempotency-Key": f"phase-e-create-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _patch(client, project_id: str, step_key: str, draft: dict) -> dict:
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        json={"draft": draft, "force": True},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["step"]


def _approve(client, project_id: str, step_key: str) -> dict:
    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve", json={})
    assert response.status_code == 200, response.text
    return response.json()["data"]["step"]


def _step(client, project_id: str, step_key: str) -> dict:
    response = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace")
    assert response.status_code == 200, response.text
    return next(step for step in response.json()["data"]["steps"] if step["step_key"] == step_key)


_BOOK_BRIEF = {
    "category": "文学悬疑",
    "target_reader": "想看旧案与家庭代价的读者",
    "story_kind": "家庭真相悬疑",
    "delight_reason": "线索逼近真相的同时抬高代价",
    "genre_promise": "真相越清晰失去越多",
    "expected_reader_emotion": "压迫与向前的拉力",
}
_FIVE = [
    "林岑回到雨城接手父亲留下的档案室。",
    "第一封匿名信迫使她重开旧案，退路没了。",
    "她发现改档案的人是养母，信念碎了。",
    "证据被烧，弟弟被扣，局势失控。",
    "她交出母本换弟弟，代价是自己的名字。",
]


def test_stale_step_keeps_consumed_upstream_refs_and_accept_stale_leaves_a_trace(client) -> None:
    project_id = _create_project(client, "diff")
    _patch(client, project_id, "book_brief", _BOOK_BRIEF)
    _approve(client, project_id, "book_brief")
    _patch(client, project_id, "one_sentence_summary", {"summary": "林岑必须交出母本，但交出去弟弟就没了退路。"})
    logline_v1 = _approve(client, project_id, "one_sentence_summary")
    _patch(client, project_id, "one_paragraph_summary", {"sentences": _FIVE, "moral_premise": "逃避代价只会放大伤害。"})
    paragraph = _approve(client, project_id, "one_paragraph_summary")

    # 确认时消费的上游版本被记在 artifact.input_refs 里
    refs = paragraph["artifact"]["input_refs"]
    assert refs["one_sentence_summary"] == logline_v1["artifact"]["step_run_id"]
    assert "book_brief" in refs

    # 上游真的改了并再次批准 → 下游置 stale，原因点名上游；input_refs 仍指向当时消费的旧版本
    _patch(client, project_id, "one_sentence_summary", {"summary": "林岑必须烧掉母本，但烧掉它养母就永远逍遥。"})
    logline_v2 = _approve(client, project_id, "one_sentence_summary")
    assert logline_v2["artifact"]["step_run_id"] != logline_v1["artifact"]["step_run_id"]

    stale = _step(client, project_id, "one_paragraph_summary")
    assert stale["status"] == "stale"
    assert "one_sentence_summary" in (stale["stale_reason"] or "")
    assert stale["stale_accepted_at"] is None
    assert stale["artifact"]["input_refs"]["one_sentence_summary"] == logline_v1["artifact"]["step_run_id"]
    assert _step(client, project_id, "one_sentence_summary")["artifact"]["step_run_id"] == logline_v2["artifact"]["step_run_id"]

    # 历史带草稿：旧版本与新版本都按 step_run_id 找得到，前端就能并排展示
    history = client.get(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_sentence_summary/history",
        params={"include_draft": "true"},
    )
    assert history.status_code == 200, history.text
    by_id = {item["step_run_id"]: item for item in history.json()["data"]["items"]}
    assert by_id[logline_v1["artifact"]["step_run_id"]]["draft"]["summary"].startswith("林岑必须交出母本")
    assert by_id[logline_v2["artifact"]["step_run_id"]]["draft"]["summary"].startswith("林岑必须烧掉母本")
    assert by_id[logline_v2["artifact"]["step_run_id"]]["version"] > by_id[logline_v1["artifact"]["step_run_id"]]["version"]

    # 「已复核」在服务端留痕：状态仍是 stale（真相不变），但 stale_accepted_* 记下了谁何时确认仍然有效
    accepted = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_paragraph_summary/accept-stale",
        json={"note": "改的是措辞，五句不受影响"},
    )
    assert accepted.status_code == 200, accepted.text
    step = accepted.json()["data"]["step"]
    assert step["status"] == "stale"
    assert step["stale_accepted_at"]
    assert step["stale_accepted_note"] == "改的是措辞，五句不受影响"
    assert step["gate_satisfied"] is True, "确认仍有效的 stale 步骤重新满足闸门"
    # E3 第二步：「仍然有效」是对现在的上游说的——消费的上游版本刷新到当前，前端的「上游已有新版本」提示随之清零
    assert step["artifact"]["input_refs"]["one_sentence_summary"] == logline_v2["artifact"]["step_run_id"]
    history_after = client.get(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_paragraph_summary/history",
        params={"include_draft": "false"},
    )
    assert history_after.status_code == 200, history_after.text
    assert history_after.json()["data"]["items"][0]["stale_accepted_note"] == "改的是措辞，五句不受影响"


def test_regenerating_a_stale_step_from_new_upstream_records_the_trigger_and_refreshes_refs(client) -> None:
    project_id = _create_project(client, "regen")
    _patch(client, project_id, "book_brief", _BOOK_BRIEF)
    _approve(client, project_id, "book_brief")
    _patch(client, project_id, "one_sentence_summary", {"summary": "林岑必须交出母本，但交出去弟弟就没了退路。"})
    _approve(client, project_id, "one_sentence_summary")
    _patch(client, project_id, "one_paragraph_summary", {"sentences": _FIVE, "moral_premise": "逃避代价只会放大伤害。"})
    _approve(client, project_id, "one_paragraph_summary")
    _patch(client, project_id, "one_sentence_summary", {"summary": "林岑必须烧掉母本，但烧掉它养母就永远逍遥。"})
    logline_v2 = _approve(client, project_id, "one_sentence_summary")
    assert _step(client, project_id, "one_paragraph_summary")["status"] == "stale"

    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_paragraph_summary/generate",
        json={"require_llm": True, "source": "fe_restale_regen"},
        headers={"X-Idempotency-Key": f"phase-e-regen-{project_id}"},
    )
    assert response.status_code == 200, response.text
    step = response.json()["data"]["step"]
    assert step["status"] == "pending_review", "按新上游重新展开 = 新版本待作者确认，不再是 stale"
    assert step["stale_reason"] in ("", None)
    assert step["health"]["trigger_source"] == "fe_restale_regen"
    assert step["artifact"]["input_refs"]["one_sentence_summary"] == logline_v2["artifact"]["step_run_id"], "新版本消费的是新上游"
