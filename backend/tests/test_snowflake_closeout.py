"""收口三项改造方案 (P1-1 / P1-2 / P0-3) 的验收测试。

设计依据：雪花流程 · 收口三项改造方案。三项的共同主线是「原型即规格」——把前端
``ws-snow.jsx`` 早就写对的模型（行级不可变身份、三幕单向派生、祖先快照失效）固化回
后端真相层。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import SnowflakeScenePlan, SnowflakeStepRun
from novel_system.services.snowflake_staleness import (
    FIELDS_CONSUMED,
    changed_fields,
    field_sigs,
    recompute_stale,
)
from novel_system.services.snowflake_steps import (
    STEP_ORDER,
    derive_three_act,
    diagnose_step_pressure,
    get_step_definition,
)
import pytest


@pytest.fixture(autouse=True)
def _skeleton_snowflake_generate(monkeypatch):
    """假生成已退役：本文件回归收口三项（行级不可变身份/三幕单向派生/祖先快照失效），
    不关心生成质量——把 generate_step 打成「规划器骨架直通」，并开 llm_enabled 过路由闸。"""
    from novel_system.services.hash_engine import normalize
    from novel_system.services.snowflake_planner import SnowflakePlannerService
    from novel_system.services.snowflake_workspace_llm import (
        SnowflakeWorkspaceLLMService,
        WorkspaceLLMResult,
    )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate_step(self, *, project, step_key, latest_by_step, **kwargs):
        payload = SnowflakePlannerService(self.session)._build_artifact_json(
            project, step_key, dict(latest_by_step)
        )
        return WorkspaceLLMResult(source="llm", llm_call_id=None, payload=normalize(payload))

    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "generate_step", fake_generate_step)


def _create_project(client, *, key: str) -> dict:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Rain City Signal",
            "genre": "Urban Mystery",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": (
                "An old letter pulls the heroine back to Rain City.\n"
                "The cold case turns out to be tied to her family.\n"
                "She must decide whether the truth is worth the cost."
            ),
        },
        headers={"X-Idempotency-Key": f"create-closeout-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _generate(client, project_id: str, step_key: str) -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json={},
        headers={"X-Idempotency-Key": f"gen-closeout-{project_id}-{step_key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve(client, project_id: str, step_key: str) -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"app-closeout-{project_id}-{step_key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _workspace(client, project_id: str) -> dict:
    response = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _scene_list_step(workspace: dict) -> dict:
    return next(step for step in workspace["steps"] if step["step_key"] == "scene_list")


def _step(workspace: dict, step_key: str) -> dict:
    return next(step for step in workspace["steps"] if step["step_key"] == step_key)


def _revise_and_approve(client, project_id: str, step_key: str, draft: dict) -> dict:
    """Patch a step's draft (creating a pending revision) then re-approve it."""
    patch = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        json={"draft": draft},
    )
    assert patch.status_code == 200, patch.text
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"reapprove-{project_id}-{step_key}-{draft.get('_rev', 'x')}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve_through(client, project_id: str, last_step: str) -> None:
    order = [
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
    for step_key in order[: order.index(last_step) + 1]:
        _generate(client, project_id, step_key)
        _approve(client, project_id, step_key)


# ----------------------------------------------------------------------------- #
# 收口-1 · 场景 / 章节 ID 系统铸造、不可编辑
# ----------------------------------------------------------------------------- #


def test_closeout1_scene_list_editor_marks_identity_fields_readonly(client) -> None:
    project = _create_project(client, key="readonly-fields")
    workspace = _workspace(client, project["project_id"])
    scenes_field = next(
        field for field in _scene_list_step(workspace)["editor"]["fields"] if field["key"] == "scenes"
    )
    assert scenes_field["readonly_fields"] == ["scene_id", "chapter_id", "row_uid"]
    assert "row_uid" in scenes_field["template"]


def test_closeout1_editing_scene_id_does_not_orphan_the_plan(client, session) -> None:
    project = _create_project(client, key="no-orphan")
    pid = project["project_id"]
    _generate(client, pid, "scene_list")

    workspace = _workspace(client, pid)
    scenes = _scene_list_step(workspace)["draft"]["scenes"]
    assert scenes, "fallback generator should seed at least one scene"
    original_count = len(scenes)
    target = scenes[0]
    original_row_uid = target["row_uid"]
    original_scene_id = target["scene_id"]
    assert original_row_uid, "every seeded row must carry an immutable row_uid"

    # The author tampers with the system-minted ids in the form payload and saves.
    tampered = [dict(scene) for scene in scenes]
    tampered[0] = {**tampered[0], "scene_id": "AUTHOR_HACKED", "chapter_id": "AUTHOR_HACKED_CH"}
    save = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_list",
        json={"draft": {"scenes": tampered}},
    )
    assert save.status_code == 200, save.text

    plans = session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == pid)
    ).scalars().all()
    # No orphan row was created, and the tampered ids were discarded.
    assert len(plans) == original_count
    plan = next(p for p in plans if p.row_uid == original_row_uid)
    assert plan.scene_id == original_scene_id
    assert plan.scene_id != "AUTHOR_HACKED"
    assert plan.chapter_id != "AUTHOR_HACKED_CH"


def test_closeout1_reorder_changes_only_scene_seq(client, session) -> None:
    project = _create_project(client, key="reorder")
    pid = project["project_id"]
    _generate(client, pid, "scene_list")
    workspace = _workspace(client, pid)
    scenes = _scene_list_step(workspace)["draft"]["scenes"]
    id_before = {scene["row_uid"]: scene["scene_id"] for scene in scenes}

    # Bump every scene_seq by 10 — a pure reorder. Identity must not move.
    reordered = [{**scene, "scene_seq": scene["scene_seq"] + 10} for scene in scenes]
    save = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_list",
        json={"draft": {"scenes": reordered}},
    )
    assert save.status_code == 200, save.text

    plans = session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == pid)
    ).scalars().all()
    assert len(plans) == len(scenes)
    for plan in plans:
        assert plan.scene_id == id_before[plan.row_uid]
        assert plan.scene_seq >= 11


def test_closeout1_scene_details_reuses_scene_list_identity(client, session) -> None:
    project = _create_project(client, key="step8-step9-identity")
    pid = project["project_id"]
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
        _generate(client, pid, step_key)
        _approve(client, pid, step_key)

    workspace = _workspace(client, pid)
    list_scenes = _scene_list_step(workspace)["draft"]["scenes"]
    detail_scenes = next(
        step for step in workspace["steps"] if step["step_key"] == "scene_details"
    )["draft"]["scenes"]

    # Step-9 must hit every step-8 seed exactly: identical row_uid + scene_id sets.
    assert {s["row_uid"] for s in list_scenes} == {s["row_uid"] for s in detail_scenes}
    assert {s["scene_id"] for s in list_scenes} == {s["scene_id"] for s in detail_scenes}

    plans = session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == pid)
    ).scalars().all()
    # Approving both steps did not duplicate any plan, and scene_plan_id is now
    # keyed on the immutable row_uid rather than the editable scene_id.
    assert len(plans) == len(list_scenes)
    for plan in plans:
        assert plan.row_uid
        assert plan.scene_plan_id == f"snowflake_scene_plan_{pid}_{plan.row_uid}"


# ----------------------------------------------------------------------------- #
# 收口-2 · 三幕灾难从五句脊派生
# ----------------------------------------------------------------------------- #


def test_closeout2_three_act_removed_from_schema() -> None:
    step = get_step_definition("one_paragraph_summary")
    assert "three_act_check" not in step["default_draft"]
    field_keys = {field["key"] for field in step["editor"]["fields"]}
    assert field_keys == {"sentences", "moral_premise"}
    assert "three_act_check" not in field_keys


def test_closeout2_derive_three_act_is_a_view_over_sentences() -> None:
    draft = {"sentences": ["开局", "第一灾难", "第二灾难", "第三灾难", "结局走向"]}
    assert derive_three_act(draft) == {
        "first_disaster": "第一灾难",
        "second_disaster": "第二灾难",
        "third_disaster": "第三灾难",
        "ending": "结局走向",
    }
    # Partial spine: missing sentences derive to empty strings, never raises.
    assert derive_three_act({"sentences": ["只有开局"]}) == {
        "first_disaster": "",
        "second_disaster": "",
        "third_disaster": "",
        "ending": "",
    }
    assert derive_three_act(None)["ending"] == ""


def test_closeout2_diagnose_reads_disasters_from_the_spine() -> None:
    # Editing sentence 2 (= first disaster) is what the disaster-pressure check sees.
    strong = {
        "sentences": [
            "雨夜，她回到潮汐镇接手父亲的旧案。",
            "第一份证物失窃，迫使她公开身份、再也无法回头。",
            "线人之死让她发现警局内部就有共谋，认知被彻底打碎。",
            "她押上最后筹码反被构陷，局势不可逆地失控。",
            "决战中她必须在真相与至亲之间付出代价做出抉择。",
        ],
        "moral_premise": "逃避只会扩大伤害，承担代价才能终结伤害。",
    }
    strong_diag = diagnose_step_pressure("one_paragraph_summary", strong)
    assert "disaster_chain_too_soft" not in strong_diag["pressure_flags"]
    assert not any("灾难都迫使" in step for step in strong_diag["fix_steps"])

    # 阶段 H：关键词判不了灾难链——软五句只得到建议，不再是旗标
    soft = {"sentences": ["背景介绍。", "一些事情发生了。", "然后又发生了别的。", "最后结束了。", "结局。"]}
    soft_diag = diagnose_step_pressure("one_paragraph_summary", soft)
    assert "disaster_chain_too_soft" not in soft_diag["pressure_flags"]
    assert any(step.startswith("建议：") and "灾难都迫使" in step for step in soft_diag["fix_steps"])


def test_closeout2_generator_does_not_persist_three_act(client) -> None:
    project = _create_project(client, key="no-three-act-store")
    pid = project["project_id"]
    draft = _generate(client, pid, "one_paragraph_summary")["step"]["draft"]
    assert "three_act_check" not in draft
    assert "sentences" in draft


# ----------------------------------------------------------------------------- #
# 收口-3 · 失效统一为依赖 / diff 感知
# ----------------------------------------------------------------------------- #


class _Row:
    """Minimal stand-in for a SnowflakeStepRun / SnowflakeArtifact in unit tests."""

    def __init__(self, step_key: str, consumed: dict | None) -> None:
        self.step_key = step_key
        self.status = "approved"
        self.consumed_input_sigs_json = consumed


def test_closeout3_recompute_stale_is_field_aware() -> None:
    # short_synopsis consumed one_paragraph_summary.sentences; the snapshot remembers it.
    old_para = {"sentences": ["a", "b", "c", "d", "e"], "moral_premise": "old"}
    snap = {"one_paragraph_summary": field_sigs(old_para)}
    short = _Row("short_synopsis", snap)
    # 阶段 G：character_synopses 有字段表但不读 one_paragraph_summary → 不消费，不失效；
    # 一个不在表里的未知步骤才退化为依赖边级（任何改动都标）。
    char_syn = _Row("character_synopses", {"one_paragraph_summary": field_sigs(old_para)})
    unknown = _Row("scene_details", {"one_paragraph_summary": field_sigs(old_para)})
    unknown.step_key = "unknown_future_step"
    step_order = {**STEP_ORDER, "unknown_future_step": 99}

    # 1) Only moral_premise changed → short_synopsis (reads sentences) is NOT stale,
    #    character_synopses (does not read 03 at all) is NOT stale, the unknown step flips.
    new_para = {"sentences": ["a", "b", "c", "d", "e"], "moral_premise": "new"}
    hits = recompute_stale(
        changed_step_key="one_paragraph_summary",
        current_field_sigs=field_sigs(new_para),
        candidate_rows=[short, char_syn, unknown],
        step_order=step_order,
    )
    stale = {hit.step_key for hit in hits}
    assert "short_synopsis" not in stale
    assert "character_synopses" not in stale
    assert "unknown_future_step" in stale

    # 2) A consumed field (sentences) changed → short_synopsis IS stale.
    changed_para = {"sentences": ["a", "B!", "c", "d", "e"], "moral_premise": "old"}
    hits = recompute_stale(
        changed_step_key="one_paragraph_summary",
        current_field_sigs=field_sigs(changed_para),
        candidate_rows=[short, char_syn],
        step_order=STEP_ORDER,
    )
    assert "short_synopsis" in {hit.step_key for hit in hits}


def test_closeout3_missing_snapshot_falls_back_to_conservative_stale() -> None:
    # Legacy row without a snapshot → conservatively stale by dependency edge.
    legacy = _Row("short_synopsis", None)
    hits = recompute_stale(
        changed_step_key="one_paragraph_summary",
        current_field_sigs={"sentences": "x"},
        candidate_rows=[legacy],
        step_order=STEP_ORDER,
    )
    assert [hit.step_key for hit in hits] == ["short_synopsis"]

    # Upstream / same-index rows are never downstream-affected.
    upstream = _Row("book_brief", {"book_brief": {}})
    hits = recompute_stale(
        changed_step_key="one_paragraph_summary",
        current_field_sigs={"sentences": "x"},
        candidate_rows=[upstream],
        step_order=STEP_ORDER,
    )
    assert hits == []


def test_closeout3_unconsumed_field_change_does_not_stale_downstream(client) -> None:
    project = _create_project(client, key="diff-unconsumed")
    pid = project["project_id"]
    _approve_through(client, pid, "short_synopsis")

    para = _step(_workspace(client, pid), "one_paragraph_summary")["draft"]
    # Revise ONLY moral_premise — a field neither character_sheets nor short_synopsis reads.
    _revise_and_approve(
        client,
        pid,
        "one_paragraph_summary",
        {"sentences": list(para["sentences"]), "moral_premise": "改写后的主题前提，但五句脊一字未动。", "_rev": "moral"},
    )

    workspace = _workspace(client, pid)
    assert _step(workspace, "character_sheets")["status"] == "approved"
    assert _step(workspace, "short_synopsis")["status"] == "approved"


def test_closeout3_consumed_field_change_stales_only_dependents(client) -> None:
    project = _create_project(client, key="diff-consumed")
    pid = project["project_id"]
    _approve_through(client, pid, "short_synopsis")

    para = _step(_workspace(client, pid), "one_paragraph_summary")["draft"]
    revised = list(para["sentences"])
    revised[1] = "全新的第一灾难：她当众被夺走唯一的证物，再无退路。"
    _revise_and_approve(
        client,
        pid,
        "one_paragraph_summary",
        {"sentences": revised, "moral_premise": para.get("moral_premise") or "", "_rev": "spine"},
    )

    workspace = _workspace(client, pid)
    # Steps that consume the spine go stale; the upstream one_sentence_summary does not.
    assert _step(workspace, "character_sheets")["status"] == "stale"
    assert _step(workspace, "short_synopsis")["status"] == "stale"
    assert _step(workspace, "one_sentence_summary")["status"] == "approved"


def test_closeout3_stale_accept_does_not_disturb_other_steps(client) -> None:
    project = _create_project(client, key="diff-accept")
    pid = project["project_id"]
    _approve_through(client, pid, "short_synopsis")

    para = _step(_workspace(client, pid), "one_paragraph_summary")["draft"]
    revised = list(para["sentences"])
    revised[2] = "全新的第二灾难：盟友倒戈，她的认知被彻底打碎。"
    _revise_and_approve(
        client,
        pid,
        "one_paragraph_summary",
        {"sentences": revised, "moral_premise": para.get("moral_premise") or "", "_rev": "spine2"},
    )

    accept = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/short_synopsis/accept-stale",
        json={"note": "梗概仍然成立。"},
        headers={"X-Operator-Ref": "author-z"},
    )
    assert accept.status_code == 200, accept.text
    workspace = accept.json()["data"]["workspace"]
    accepted = _step(workspace, "short_synopsis")
    assert accepted["status"] == "stale"
    assert accepted["stale_accepted_at"]
    assert accepted["gate_satisfied"] is True
    # Accepting short_synopsis must not touch the sibling that was also stale,
    # nor any upstream step.
    assert _step(workspace, "character_sheets")["status"] == "stale"
    assert _step(workspace, "one_sentence_summary")["status"] == "approved"


def test_closeout3_planner_and_workspace_agree_on_stale_set(client, session) -> None:
    # Both stacks route through the same recompute_stale judgment, so an identical
    # spine revision must invalidate the identical set of downstream steps.
    ws = _create_project(client, key="agree-ws")
    wpid = ws["project_id"]
    _approve_through(client, wpid, "short_synopsis")
    para = _step(_workspace(client, wpid), "one_paragraph_summary")["draft"]
    new_sentences = list(para["sentences"])
    new_sentences[1] = "一致性校验：第一灾难被整体改写。"
    _revise_and_approve(
        client,
        wpid,
        "one_paragraph_summary",
        {"sentences": new_sentences, "moral_premise": para.get("moral_premise") or "", "_rev": "agree"},
    )
    ws_stale = {
        row.step_key
        for row in session.execute(
            select(SnowflakeStepRun).where(
                SnowflakeStepRun.project_id == wpid, SnowflakeStepRun.status == "stale"
            )
        ).scalars().all()
    }

    assert ws_stale == {"character_sheets", "short_synopsis"}
    # The shared field map is what guarantees the planner stack would agree: the
    # revision touched only `sentences`, which short_synopsis consumes.
    assert FIELDS_CONSUMED["short_synopsis"]["one_paragraph_summary"] == {"sentences"}
    assert changed_fields(field_sigs(para), field_sigs({**para, "sentences": new_sentences})) == {"sentences"}
