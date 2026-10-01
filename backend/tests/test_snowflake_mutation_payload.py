"""B06-05：变更回包瘦身，GET 的工作台形状不变。

- 变更回包里的工作台不再带 ``scene_board``（前端一处都没读过），步骤 ``artifact`` 与 ``step_run`` 不再带
  ``diagnosis_json``（``health`` 的一份深拷贝；前端只读 ``artifact.step_run_id`` / ``input_refs``）；
- 自动保存可以只要这一步：``PATCH …/steps/{key}?include_workspace=false`` 只回 ``{step, step_run}``——
  那一步与 GET 工作台里的同一步一模一样（同一个构建、同一个前端口径的角色 id），只少那份深拷贝。
"""

from __future__ import annotations

from itertools import count

from tests.support.snowflake import patch_step, workspace_step as _get_step

_KEYS = count(1)


def _create_project(client) -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": "雨城来信", "genre": "悬疑", "target_word_count": 120000, "outline_text": "林昭带着旧信回到雨城。"},
        headers={"X-Idempotency-Key": f"mutation-payload-create-{next(_KEYS)}"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "scene_board" not in data["workspace"], "新建作品的回包也是变更回包"
    return data["project"]["project_id"]


def _patch(client, project_id: str, step_key: str, draft: dict, *, lean: bool = False):
    return patch_step(client, project_id, step_key, draft, force=False, lean=lean, key=f"mutation-payload-patch-{next(_KEYS)}")


BRIEF = {"category": "悬疑", "target_reader": "喜欢旧案与家族秘密的读者", "story_kind": "追查旧案"}


def test_get_keeps_the_full_shape_while_mutation_responses_drop_the_dead_weight(client) -> None:
    project_id = _create_project(client)
    saved = _patch(client, project_id, "book_brief", BRIEF)

    assert "scene_board" not in saved["workspace"]
    assert all("diagnosis_json" not in (step["artifact"] or {}) for step in saved["workspace"]["steps"])
    assert "diagnosis_json" not in saved["step_run"]
    # 前端从变更回包里读的都还在
    for key in ("steps", "triage_items", "assistant_history", "direction_briefs", "materialization_gate",
                "resync_status", "chapter_plan_status", "ready_to_materialize", "current_step_key"):
        assert key in saved["workspace"], key
    assert saved["step"]["artifact"]["step_run_id"] == saved["step_run"]["step_run_id"]
    assert "input_refs" in saved["step"]["artifact"]

    approved = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/book_brief/approve",
        json={},
        headers={"X-Idempotency-Key": f"mutation-payload-approve-{next(_KEYS)}"},
    ).json()["data"]
    assert "scene_board" not in approved["workspace"]
    assert "diagnosis_json" not in approved["step"]["artifact"]

    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    assert workspace["scene_board"] == {"chapters": [], "scenes": []}
    brief = next(step for step in workspace["steps"] if step["step_key"] == "book_brief")
    assert brief["artifact"]["diagnosis_json"] == brief["health"]


def test_autosave_can_ask_for_just_the_step(client) -> None:
    project_id = _create_project(client)
    saved = _patch(client, project_id, "book_brief", BRIEF, lean=True)

    assert "workspace" not in saved and {"step", "step_run"} <= set(saved)
    fetched = _get_step(client, project_id, "book_brief")
    assert fetched["artifact"].pop("diagnosis_json") == fetched["health"]
    assert saved["step"] == fetched
    assert saved["step"]["status"] == "pending_review"


def test_the_lean_scene_step_is_the_same_step_the_workspace_shows(client) -> None:
    """09 的草稿从场景计划行现拼、角色 id 按前端口径剥前缀：单独建的这一步必须与工作台里的一致。"""
    project_id = _create_project(client)
    _patch(
        client,
        project_id,
        "character_sheets",
        {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角"}]},
    )
    scenes = [
        {"row_uid": "u1", "summary": "林昭在码头拆开旧信", "pov_character_id": "c1", "location": "码头", "crucible": "信里提到的案卷"},
        {"row_uid": "u2", "summary": "林昭去档案馆找案卷", "pov_character_id": "c1", "location": "档案馆", "crucible": "案卷被人借走"},
    ]
    saved = _patch(client, project_id, "scene_list", {"scenes": scenes}, lean=True)

    rows = saved["step"]["draft"]["scenes"]
    assert [row["row_uid"] for row in rows] == ["u1", "u2"]
    assert {row["pov_character_id"] for row in rows} == {"c1"}, "交给前端的是不带作品前缀的 id"
    fetched = _get_step(client, project_id, "scene_list")
    fetched["artifact"].pop("diagnosis_json")
    assert saved["step"] == fetched
