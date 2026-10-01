"""R15a（2026-09-30 作者批准 #24a）：「已复核」一处点完——09 / 10 的步骤级复核连同过期的场景计划一起复核。

上游（比如 07 的一段展开）改了之后，09 与全部场景计划都被标成过期。作者在 09 点「已复核」，步骤满足了闸门，
场景计划却仍是未复核的过期——「整理为章节结构」被一场一场的「需要先复核」挡住，界面上没有按钮能解开
（只有一个没有界面调用的 POST …/scenes/accept-stale；或者改一个字再「确认本步」）。现在步骤级的「已复核」
就是对这一步产出的场景计划说「仍然有效」，那个单独的接口随之删掉；闸门的提示也指向「已复核」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import OperationLog, SnowflakeScenePlan
from tests.support.snowflake import (
    approve_generated_step as _approve_generated_step,
    create_workspace_project as _create_project,
    intent_key as _intent_key,
)

pytestmark = pytest.mark.usefixtures("skeleton_snowflake")

STEPS = [
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
]


def _workspace(client, project_id: str) -> dict:
    return client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]


def _blocker_kinds(workspace: dict) -> set[str]:
    return {item["kind"] for item in workspace["materialization_gate"]["items"] if item["severity"] == "blocker"}


def _plans(session, project_id: str) -> list[SnowflakeScenePlan]:
    session.expire_all()
    return list(
        session.execute(
            select(SnowflakeScenePlan).where(
                SnowflakeScenePlan.project_id == project_id, SnowflakeScenePlan.removed_at.is_(None)
            )
        ).scalars()
    )


def _revise_long_synopsis(client, project_id: str) -> None:
    workspace = _workspace(client, project_id)
    long_synopsis = next(step for step in workspace["steps"] if step["step_key"] == "long_synopsis")
    draft = dict(long_synopsis["draft"])
    paragraphs = list(draft.get("paragraphs") or [])
    paragraphs[0] = (paragraphs[0] or "") + "（作者追加一句新的因果。）"
    draft["paragraphs"] = paragraphs
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/long_synopsis",
        json={"draft": draft},
        headers={"X-Idempotency-Key": _intent_key("fold-patch-07")},
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/long_synopsis/approve",
        json={"sync_catalog": True},
        headers={"X-Idempotency-Key": _intent_key("fold-approve-07")},
    )
    assert response.status_code == 200, response.text


def test_accepting_09_also_accepts_its_stale_scene_plans(client, session) -> None:
    project_id = _create_project(client, key="accept-stale-fold")["project_id"]
    for step_key in STEPS:
        _approve_generated_step(client, project_id, step_key)
    _revise_long_synopsis(client, project_id)

    workspace = _workspace(client, project_id)
    statuses = {step["step_key"]: step["status"] for step in workspace["steps"]}
    assert statuses["scene_list"] == "stale"
    plans = _plans(session, project_id)
    assert plans and all(plan.status == "stale" for plan in plans)
    assert "stale_scene_plan" in _blocker_kinds(workspace)
    stale_item = next(item for item in workspace["materialization_gate"]["items"] if item["kind"] == "stale_scene_plan")
    # 提示指向真正能解开它的地方：09（这一步过期了）的「已复核」
    assert stale_item["step_key"] == "scene_list"
    assert "已复核" in stale_item["message"] and "已复核" in stale_item["primary_action"]["label"]

    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/scene_list/accept-stale",
        json={"note": "上游只多了一句因果，场景不受影响。"},
        headers={"X-Idempotency-Key": _intent_key("fold-accept-09"), "X-Operator-Ref": "author-z"},
    )
    assert response.status_code == 200, response.text

    workspace = response.json()["data"]["workspace"]
    assert "stale_scene_plan" not in _blocker_kinds(workspace)
    assert workspace["materialization_gate"]["status"] != "blocked"
    plans = _plans(session, project_id)
    assert all(plan.status == "stale" and plan.stale_accepted_at for plan in plans)
    assert {plan.stale_accepted_by for plan in plans} == {"author-z"}
    logs = session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_scene_stale_accepted")
    ).scalars().all()
    assert len(logs) == len(plans)
    assert {log.payload_json["note"] for log in logs} == {"上游只多了一句因果，场景不受影响。"}


def test_accepting_10_accepts_the_stale_scene_plans_too(client, session) -> None:
    from novel_system.db.models import SnowflakeStepRun

    project_id = _create_project(client, key="accept-stale-fold-10")["project_id"]
    for step_key in STEPS:
        _approve_generated_step(client, project_id, step_key)
    # 10 与场景计划过期（09 已确认）：与旧的逐场接口测试同一种落库形态
    for plan in _plans(session, project_id):
        plan.status = "stale"
        plan.stale_reason = "scene_list 改动影响了场景列表，复核场景计划。"
    run = session.execute(
        select(SnowflakeStepRun).where(
            SnowflakeStepRun.project_id == project_id,
            SnowflakeStepRun.step_key == "scene_details",
            SnowflakeStepRun.status == "approved",
        )
    ).scalars().one()
    run.status = "stale"
    run.stale_reason = "scene_list 改了被消费字段 ['scenes']"
    session.commit()
    assert "stale_scene_plan" in _blocker_kinds(_workspace(client, project_id))
    stale_item = next(
        item for item in _workspace(client, project_id)["materialization_gate"]["items"] if item["kind"] == "stale_scene_plan"
    )
    assert stale_item["step_key"] == "scene_details"

    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/scene_details/accept-stale",
        json={},
        headers={"X-Idempotency-Key": _intent_key("fold10-accept-10")},
    )
    assert response.status_code == 200, response.text
    workspace = response.json()["data"]["workspace"]
    assert "stale_scene_plan" not in _blocker_kinds(workspace)
    assert workspace["materialization_gate"]["status"] != "blocked"
    assert all(plan.stale_accepted_at for plan in _plans(session, project_id))


