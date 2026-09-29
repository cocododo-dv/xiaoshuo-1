"""PRE-01（Q2 复核转来）：已过期（stale）的步骤收到一次故事内容不变的 PATCH，不该被打回待审。

前端每次打开构思都会把 09 / 10 各上行一次（写穿缓存 fe_* 变了，故事字段没变）。update_step 只对
approved / skipped 的步骤「原样保留状态、原位写 fe_*」；stale 的步骤——包括作者点过「已复核」的——
却被新建成一版 pending_review：「已复核」满足整理闸门，待审不满足，于是作者只是打开了页面，
「确认写入」就被挡住。现在 stale 与 approved 同样处理：状态与失效留痕（stale_*）原样保留，只原位写 fe_*。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import SnowflakeStepRun
from tests.real_llm_fakes import install_skeleton_snowflake
from tests.test_snowflake_workspace_v2 import _approve_generated_step, _approve_step, _create_project, _generate_step, _intent_key


@pytest.fixture(autouse=True)
def _skeleton(monkeypatch):
    install_skeleton_snowflake(monkeypatch)


def _make_one_sentence_stale(client, project_id: str) -> None:
    _approve_generated_step(client, project_id, "book_brief")
    _approve_generated_step(client, project_id, "one_sentence_summary")
    replacement = _generate_step(client, project_id, "book_brief")
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
        headers={"X-Idempotency-Key": _intent_key("stale-repatch-brief")},
    )
    assert response.status_code == 200, response.text
    _approve_step(client, project_id, "book_brief")


def _runs(session, project_id: str) -> list[SnowflakeStepRun]:
    session.expire_all()
    return list(
        session.execute(
            select(SnowflakeStepRun)
            .where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "one_sentence_summary")
            .order_by(SnowflakeStepRun.version.asc())
        ).scalars()
    )


@pytest.mark.parametrize("accepted", [True, False])
def test_same_story_patch_keeps_a_stale_step_stale(client, session, accepted: bool) -> None:
    project_id = _create_project(client, key=f"stale-repatch-{accepted}")["project_id"]
    _make_one_sentence_stale(client, project_id)
    if accepted:
        response = client.post(
            f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_sentence_summary/accept-stale",
            json={"note": "仍然有效"},
            headers={"X-Idempotency-Key": _intent_key("stale-repatch-accept")},
        )
        assert response.status_code == 200, response.text
    [stale_run] = [run for run in _runs(session, project_id) if run.status == "stale"]
    stale_draft = dict(stale_run.draft_json or {})
    accepted_at = stale_run.stale_accepted_at

    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_sentence_summary",
        json={"draft": {**stale_draft, "fe_state": "done", "fe_t": 1234}},
        headers={"X-Idempotency-Key": _intent_key("stale-repatch-same")},
    )
    assert response.status_code == 200, response.text
    step = response.json()["data"]["step"]
    assert step["status"] == "stale"
    assert step["version"] == stale_run.version
    assert step["gate_satisfied"] is accepted

    runs = _runs(session, project_id)
    assert [run.status for run in runs].count("pending_review") == 0
    [still_stale] = [run for run in runs if run.status == "stale"]
    assert still_stale.step_run_id == stale_run.step_run_id
    assert still_stale.stale_accepted_at == accepted_at
    assert still_stale.stale_reason
    assert still_stale.draft_json.get("fe_t") == 1234  # 写穿缓存照旧原位落库


def test_a_real_story_change_to_a_stale_step_still_makes_a_new_pending_version(client, session) -> None:
    project_id = _create_project(client, key="stale-repatch-edit")["project_id"]
    _make_one_sentence_stale(client, project_id)
    [stale_run] = [run for run in _runs(session, project_id) if run.status == "stale"]

    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/one_sentence_summary",
        json={"draft": {**(stale_run.draft_json or {}), "summary": "她必须在真相与家人之间选一个，但每一步都更贵。"}},
        headers={"X-Idempotency-Key": _intent_key("stale-repatch-edit")},
    )
    assert response.status_code == 200, response.text
    step = response.json()["data"]["step"]
    assert step["status"] == "pending_review"
    assert step["version"] == stale_run.version + 1
