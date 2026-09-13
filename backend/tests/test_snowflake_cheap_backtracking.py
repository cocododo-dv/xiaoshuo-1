"""阶段 G（2026-09-13 雪花评估第二轮）：让回溯便宜。

Ingermanson 的核心动力是「早回溯、多回溯」；评估核实了四处让回溯昂贵的机制：
- 审批快照拍全部祖先、消费表只列直接上游、表里没有的组合一律「改了就失效」——改一句道德前提
  让 06 到 10 全部需复核，改读者定位的安全规则让九步全失效；
- 点过「已复核」的步骤被排除在候选之外，之后上游怎么改都不再亮；
- 09 重新批准把**全部**场景计划置 stale；草稿同步（含「AI 补全这一场」）把**全部**场景行打回 draft；
- 书级步骤重新批准触发全项目运行时失效：全书草稿 / QC / 终稿置 stale，运行态打回 needs_replan。
这里锁住修复；确认过的步骤被改动后由 ``revised_after_approval`` 告诉前端「待重新确认」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import SceneRunState, SnowflakeScenePlan
from tests.test_snowflake_closeout import (
    _approve,
    _approve_through,
    _create_project,
    _generate,
    _revise_and_approve,
    _step,
    _workspace,
)


@pytest.fixture(autouse=True)
def _skeleton_snowflake_generate(monkeypatch):
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


def _statuses(client, project_id: str) -> dict[str, str]:
    return {step["step_key"]: step["status"] for step in _workspace(client, project_id)["steps"]}


def _patch(client, project_id: str, step_key: str, draft: dict) -> dict:
    response = client.patch(f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}", json={"draft": draft})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _plans(session, project_id: str) -> list[SnowflakeScenePlan]:
    return list(
        session.execute(
            select(SnowflakeScenePlan)
            .where(SnowflakeScenePlan.project_id == project_id, SnowflakeScenePlan.removed_at.is_(None))
            .order_by(SnowflakeScenePlan.scene_seq.asc())
        ).scalars().all()
    )


# ---------------------------------------------------------------------------
# 失效只落在直接消费者身上
# ---------------------------------------------------------------------------


def test_upstream_edits_stale_only_direct_consumers(client) -> None:
    pid = _create_project(client, key="g-direct")["project_id"]
    _approve_through(client, pid, "scene_details")

    para = _step(_workspace(client, pid), "one_paragraph_summary")["draft"]
    # 只改道德前提：没有步骤直接读它 → 十步全部保持 approved
    _revise_and_approve(client, pid, "one_paragraph_summary", {"sentences": list(para["sentences"]), "moral_premise": "新的前提措辞。", "_rev": "g1"})
    statuses = _statuses(client, pid)
    assert all(status == "approved" for step, status in statuses.items() if step != "one_paragraph_summary"), statuses

    # 改五句之一：只有直接读五句的 04 与 05 失效，06–10 等它们再批准时再说
    revised = list(para["sentences"])
    revised[2] = "第二灾难改写：她当众烧掉了唯一能自证的信。"
    _revise_and_approve(client, pid, "one_paragraph_summary", {"sentences": revised, "moral_premise": "新的前提措辞。", "_rev": "g2"})
    statuses = _statuses(client, pid)
    assert statuses["character_sheets"] == "stale"
    assert statuses["short_synopsis"] == "stale"
    for step_key in ("character_synopses", "long_synopsis", "character_bibles", "scene_list", "scene_details"):
        assert statuses[step_key] == "approved", (step_key, statuses[step_key])


def test_book_brief_safety_rules_stale_nothing_but_target_reader_stales_the_logline(client) -> None:
    pid = _create_project(client, key="g-brief")["project_id"]
    _approve_through(client, pid, "one_paragraph_summary")
    brief = _step(_workspace(client, pid), "book_brief")["draft"]

    _revise_and_approve(client, pid, "book_brief", {**brief, "safety_rules": [*list(brief.get("safety_rules") or []), "不写未成年人的性内容。"], "_rev": "g3"})
    statuses = _statuses(client, pid)
    assert statuses["one_sentence_summary"] == "approved" and statuses["one_paragraph_summary"] == "approved"

    _revise_and_approve(client, pid, "book_brief", {**brief, "target_reader": "换成喜欢慢热本格推理的读者。", "_rev": "g4"})
    statuses = _statuses(client, pid)
    assert statuses["one_sentence_summary"] == "stale"
    assert statuses["one_paragraph_summary"] == "approved"


def test_accepted_stale_step_is_re_evaluated_on_the_next_upstream_change(client) -> None:
    pid = _create_project(client, key="g-accept")["project_id"]
    _approve_through(client, pid, "short_synopsis")
    para = _step(_workspace(client, pid), "one_paragraph_summary")["draft"]

    first = list(para["sentences"])
    first[1] = "第一灾难第一次改写。"
    _revise_and_approve(client, pid, "one_paragraph_summary", {"sentences": first, "moral_premise": para.get("moral_premise") or "", "_rev": "g5"})
    assert _statuses(client, pid)["short_synopsis"] == "stale"
    accepted = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/short_synopsis/accept-stale",
        json={"note": "措辞而已"},
        headers={"X-Idempotency-Key": f"g-accept-{pid}"},
    )
    assert accepted.status_code == 200, accepted.text
    step = _step(_workspace(client, pid), "short_synopsis")
    assert step["status"] == "stale" and step["stale_accepted_at"] and step["gate_satisfied"] is True

    # 上游再改一次：已复核不再永远有效——重新亮起，留痕清零
    second = list(first)
    second[1] = "第一灾难第二次改写：这次真的换了事件。"
    _revise_and_approve(client, pid, "one_paragraph_summary", {"sentences": second, "moral_premise": para.get("moral_premise") or "", "_rev": "g6"})
    step = _step(_workspace(client, pid), "short_synopsis")
    assert step["status"] == "stale"
    assert step["stale_accepted_at"] is None
    assert step["gate_satisfied"] is False
    assert "sentences" in (step["stale_reason"] or "")


# ---------------------------------------------------------------------------
# 场景计划只在真的改了时失效 / 打回
# ---------------------------------------------------------------------------


def test_scene_list_reapproval_stales_only_the_changed_scene_plans(client, session) -> None:
    pid = _create_project(client, key="g-rows")["project_id"]
    _approve_through(client, pid, "scene_details")
    scenes = _step(_workspace(client, pid), "scene_list")["draft"]["scenes"]
    assert len(scenes) >= 2 and all(scene.get("row_uid") for scene in scenes)

    edited = [dict(scene) for scene in scenes]
    edited[0]["summary"] = "第一场改写：她在雨里追丢了送信人。"
    _revise_and_approve(client, pid, "scene_list", {"scenes": edited, "_rev": "g7"})

    assert _statuses(client, pid)["scene_details"] == "stale"
    session.expire_all()
    by_uid = {plan.row_uid: plan for plan in _plans(session, pid)}
    assert by_uid[scenes[0]["row_uid"]].status == "stale"
    for scene in scenes[1:]:
        assert by_uid[scene["row_uid"]].status == "approved", scene["row_uid"]


def test_draft_sync_keeps_unchanged_scene_rows_approved(client, session) -> None:
    pid = _create_project(client, key="g-sync")["project_id"]
    _approve_through(client, pid, "scene_details")
    details = _step(_workspace(client, pid), "scene_details")["draft"]["scenes"]
    assert len(details) >= 2

    # 原样 PATCH（前端无谓的整表上行）：没有一行被打回
    _patch(client, pid, "scene_details", {"scenes": [dict(scene) for scene in details]})
    session.expire_all()
    assert all(plan.status == "approved" for plan in _plans(session, pid))

    # 只改一场的目标：只有那一行回到 draft
    edited = [dict(scene) for scene in details]
    target = edited[-1]
    target["goal"] = "在天亮前拿到那封信的原件。"
    _patch(client, pid, "scene_details", {"scenes": edited})
    session.expire_all()
    by_uid = {plan.row_uid: plan for plan in _plans(session, pid)}
    assert by_uid[target["row_uid"]].status == "draft"
    for scene in edited[:-1]:
        assert by_uid[scene["row_uid"]].status == "approved", scene["row_uid"]


# ---------------------------------------------------------------------------
# 运行时失效：书级步骤只提示，09 按场定位
# ---------------------------------------------------------------------------


def _materialize(client, pid: str) -> None:
    materialized = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": f"g-materialize-{pid}"},
    )
    assert materialized.status_code == 200, materialized.text
    approved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": f"g-outline-approve-{pid}"},
    )
    assert approved.status_code == 200, approved.text


def test_runtime_invalidation_is_advisory_for_book_level_steps_and_scoped_for_scene_list(client, session) -> None:
    pid = _create_project(client, key="g-runtime")["project_id"]
    _approve_through(client, pid, "scene_details")
    _materialize(client, pid)
    states_before = {row.scene_id: row.scene_status for row in session.execute(select(SceneRunState)).scalars().all()}
    assert states_before, "物化后应有场景运行态"

    logline = _step(_workspace(client, pid), "one_sentence_summary")["draft"]
    result = _revise_and_approve(client, pid, "one_sentence_summary", {**logline, "summary": "一句话改了措辞，但故事没变。", "_rev": "g8"})
    runtime = result["impact"]["runtime"]
    assert runtime["scope"] == "advisory" and runtime["affected_count"] == 0
    session.expire_all()
    states_after = {row.scene_id: row.scene_status for row in session.execute(select(SceneRunState)).scalars().all()}
    assert states_after == states_before, "改一句话不该把任何场打回 needs_replan"

    # 一句话变了 → 直接读它的 03 需复核；作者说「仍然有效」后，后面的步骤才能再确认（闸门守依赖顺序）
    assert _statuses(client, pid)["one_paragraph_summary"] == "stale"
    accepted = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_paragraph_summary/accept-stale",
        json={"note": "措辞而已"},
        headers={"X-Idempotency-Key": f"g-runtime-accept-{pid}"},
    )
    assert accepted.status_code == 200, accepted.text

    scenes = _step(_workspace(client, pid), "scene_list")["draft"]["scenes"]
    edited = [dict(scene) for scene in scenes]
    edited[0]["summary"] = "第一场换了事件：她没有追送信人，而是回家烧信。"
    result = _revise_and_approve(client, pid, "scene_list", {"scenes": edited, "_rev": "g9"})
    runtime = result["impact"]["runtime"]
    assert runtime["scope"] == "scene"
    assert runtime["affected_scene_ids"] == [scenes[0]["scene_id"]]
    session.expire_all()
    states = {row.scene_id: row.scene_status for row in session.execute(select(SceneRunState)).scalars().all()}
    assert states[scenes[0]["scene_id"]] == "needs_replan"
    assert all(states[scene["scene_id"]] == states_before[scene["scene_id"]] for scene in scenes[1:])


# ---------------------------------------------------------------------------
# 确认之后又改了：前端要的是「待重新确认」，不是自动补批准
# ---------------------------------------------------------------------------


def test_workspace_flags_a_confirmed_step_that_was_edited_afterwards(client) -> None:
    pid = _create_project(client, key="g-revised")["project_id"]
    _generate(client, pid, "book_brief")
    assert _step(_workspace(client, pid), "book_brief")["revised_after_approval"] is False  # 从未确认过
    _approve(client, pid, "book_brief")
    brief = _step(_workspace(client, pid), "book_brief")["draft"]
    assert _step(_workspace(client, pid), "book_brief")["revised_after_approval"] is False

    _patch(client, pid, "book_brief", {**brief, "target_reader": "改动后的读者画像。"})
    step = _step(_workspace(client, pid), "book_brief")
    assert step["status"] == "pending_review"
    assert step["revised_after_approval"] is True

    # 第二次批准要带新的幂等键——同键会被幂等层原样重放，等于没批
    reapproved = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/approve",
        json={},
        headers={"X-Idempotency-Key": f"g-revised-reapprove-{pid}"},
    )
    assert reapproved.status_code == 200, reapproved.text
    step = _step(_workspace(client, pid), "book_brief")
    assert step["status"] == "approved" and step["revised_after_approval"] is False
