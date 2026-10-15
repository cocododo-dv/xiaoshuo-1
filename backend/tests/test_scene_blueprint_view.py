"""AI 写前笔记（``services/scene_blueprint_view.py`` + ``/api/v1/scenes/{id}/blueprint``）。

起草前 AI 做的场景蓝图与人物压力摆给作者看：逐格标「照第 10 步」/「AI 加的」，能改成我的版本、恢复 AI 版、重做；
我的版本起草优先用、设计再变也不作废。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import GenerationPlanningArtifact, SceneBlueprint, SceneCard, SceneRunState
from novel_system.services.planning_queries import AUTHOR_BLUEPRINT_SOURCE
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_planning_staleness import supersede_scene_planning_artifacts
from tests.support.seed import seed_chapter, seed_project, seed_scene

pytestmark = pytest.mark.usefixtures("online_pipeline")

PROJECT_ID = "work-a"
CHAPTER_ID = "work-a_ch01"
SCENE_ID = "work-a_ch01_sc01"
REACTIVE_SCENE_ID = "work-a_ch01_sc02"

AI_BLUEPRINT = {
    "visible_desire": "林昭要在雨停前读完那封旧信",
    "forced_choice": "当场拆穿，还是装作没看见",
    "price_paid": "她和老友之间的信任出现裂痕",
    "information_release": "信封上的邮戳晚了十年",
    "relationship_turn": "从依赖转为戒备",
    "image_anchor": "窗台上积水里的灯影",
    "ending_action": "她把信塞回案卷，关上抽屉",
    "next_scene_pull": "抽屉锁孔里有新的划痕",
    "anti_summary_rule": "结尾不写感慨",
}


def _seed(session) -> None:
    seed_project(PROJECT_ID, title="雨城旧信", outline_text="林昭在雨城追查一封旧信。")
    seed_chapter(CHAPTER_ID, project_id=PROJECT_ID, planned_scene_count=2, chapter_goal="林昭追查旧信")
    proactive = {
        "source": "snowflake_method",
        "scene_form": "proactive",
        "scene_crucible": "雨夜封城，林昭走不出档案馆",
        "goal": "读完旧信",
        "conflict": "老友两次把信收走",
        "setback": "信被锁进案卷柜",
        "cost_requirement": "信任出现裂痕",
        "next_scene_pull": "锁孔里有新的划痕",
    }
    reactive = {
        "source": "snowflake_method",
        "scene_form": "reactive",
        "reaction": "林昭在走廊里发抖",
        "dilemma": "报警还是自己撬锁",
        "decision": "今夜自己撬锁",
    }
    seed_scene(SCENE_ID, chapter_id=CHAPTER_ID, scene_seq=1, project_id=PROJECT_ID, scene_goal="读完旧信")
    seed_scene(REACTIVE_SCENE_ID, chapter_id=CHAPTER_ID, scene_seq=2, project_id=PROJECT_ID, scene_goal="决定怎么办")
    # seed_scene 按写作简报的 13 格规整；雪花的三拍原样写回（物化就是这么存的）
    session.get(SceneCard, SCENE_ID).writer_brief_json = proactive
    session.get(SceneCard, REACTIVE_SCENE_ID).writer_brief_json = reactive
    session.commit()


def _add_ai_blueprint(session, scene_id: str = SCENE_ID, *, row_id: str = "bp_ai_1", payload: dict | None = None) -> None:
    session.add(
        SceneBlueprint(
            row_id=row_id,
            scene_id=scene_id,
            chapter_id=CHAPTER_ID,
            source_bundle_id="bundle_1",
            source_bundle_hash="hash_1",
            blueprint_json=dict(payload or AI_BLUEPRINT),
            llm_call_id="llm_call_1",
            status="accepted",
        )
    )
    session.commit()


def _origins(view: dict) -> dict[str, str]:
    return {field["key"]: field["origin"] for field in view["blueprint"]["fields"]}


def test_view_marks_each_field_as_from_step_10_or_added_by_ai(client, session) -> None:
    _seed(session)
    _add_ai_blueprint(session)
    session.add(
        GenerationPlanningArtifact(
            row_id="pressure_1",
            artifact_type="character_pressure_blueprint",
            object_type="scene",
            object_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            scene_id=SCENE_ID,
            payload_json={"surface_goal": "读完信", "hidden_fear": "老友骗了她"},
            llm_call_id="llm_call_2",
            source_bundle_id="bundle_1",
            source_bundle_hash="hash_1",
            status="active",
            created_by="near_final_planning",
        )
    )
    session.commit()

    view = client.get(f"/api/v1/scenes/{SCENE_ID}/blueprint").json()["data"]

    assert view["scene_form"] == "proactive"
    assert view["blueprint"]["source"] == "ai"
    assert _origins(view) == {
        "visible_desire": "plan",
        "forced_choice": "ai",  # 主动场第 10 步没有两难：被迫选择是 AI 加的
        "price_paid": "plan",
        "information_release": "ai",
        "relationship_turn": "ai",
        "image_anchor": "ai",
        "ending_action": "plan",
        "next_scene_pull": "plan",
        "anti_summary_rule": "ai",
    }
    assert [field["key"] for field in view["character_pressure"]["fields"]] == ["surface_goal", "hidden_fear"]


def test_reactive_scene_dilemma_grounds_the_forced_choice_and_missing_cost_does_not(client, session) -> None:
    _seed(session)
    _add_ai_blueprint(session, REACTIVE_SCENE_ID, row_id="bp_ai_reactive")

    origins = _origins(client.get(f"/api/v1/scenes/{REACTIVE_SCENE_ID}/blueprint").json()["data"])

    assert origins["forced_choice"] == "plan"
    assert origins["visible_desire"] == "plan"
    # 这一场第 10 步没写代价、没写钩子：那两格是 AI 自己补的
    assert origins["price_paid"] == "ai"
    assert origins["next_scene_pull"] == "ai"


def test_no_blueprint_yet_answers_an_empty_note(client, session) -> None:
    _seed(session)

    view = client.get(f"/api/v1/scenes/{SCENE_ID}/blueprint").json()["data"]

    assert view["blueprint"] is None and view["character_pressure"] is None and view["running"] is False


def test_author_version_is_used_for_drafting_and_survives_a_design_change(client, session) -> None:
    _seed(session)
    _add_ai_blueprint(session)

    saved = client.put(
        f"/api/v1/scenes/{SCENE_ID}/blueprint",
        json={"fields": {"forced_choice": "", "image_anchor": "案卷柜上的雨水"}},
    )
    assert saved.status_code == 200, saved.text
    view = saved.json()["data"]["blueprint"]
    assert view["source"] == "author"
    assert {field["origin"] for field in view["fields"]} == {"author"}
    values = {field["key"]: field["value"] for field in view["fields"]}
    assert values["forced_choice"] == "" and values["image_anchor"] == "案卷柜上的雨水"
    assert values["visible_desire"] == AI_BLUEPRINT["visible_desire"]  # 没改的格子照抄

    session.expire_all()
    author_row = SceneBlueprintService(session).reusable(SCENE_ID)
    assert author_row is not None and author_row.source_bundle_id == AUTHOR_BLUEPRINT_SOURCE
    assert session.get(SceneBlueprint, "bp_ai_1").status == "superseded"

    counts = supersede_scene_planning_artifacts(session, scene_ids=[SCENE_ID], reason="design_changed")
    session.commit()
    assert counts["scene_blueprints"] == 0 and counts["author_scene_blueprint_kept"] == 1

    after = client.get(f"/api/v1/scenes/{SCENE_ID}/blueprint").json()["data"]["blueprint"]
    assert after["source"] == "author"
    assert after["design_changed"] and after["design_changed"]["reason"] == "design_changed"


def test_author_version_is_reused_even_when_its_shape_differs_from_the_style_policy(session) -> None:
    _seed(session)
    facts_shape = {"visible_desire": "读完旧信", "ending_function": "信被锁住", "situation_tags": ["对峙审问"]}
    session.add(
        SceneBlueprint(
            row_id="bp_ai_facts",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            blueprint_json=facts_shape,
            llm_call_id="llm_call_3",
            status="accepted",
        )
    )
    session.commit()
    # 没绑参考书时要写法版：AI 的事实版不再复用
    assert SceneBlueprintService(session).reusable(SCENE_ID) is None

    SceneBlueprintService(session).save_author_version(session.get(SceneCard, SCENE_ID), facts_shape)
    session.commit()

    reused = SceneBlueprintService(session).reusable(SCENE_ID)
    assert reused is not None and reused.source_bundle_id == AUTHOR_BLUEPRINT_SOURCE


def test_restore_ai_brings_back_the_version_mine_replaced_unless_the_design_changed(client, session) -> None:
    _seed(session)
    _add_ai_blueprint(session)

    client.put(f"/api/v1/scenes/{SCENE_ID}/blueprint", json={"fields": {"image_anchor": "我的意象"}})
    client.put(f"/api/v1/scenes/{SCENE_ID}/blueprint", json={"fields": {"image_anchor": "改了第二遍"}})
    restored = client.post(f"/api/v1/scenes/{SCENE_ID}/blueprint/restore-ai", json={}).json()["data"]["blueprint"]

    assert restored["row_id"] == "bp_ai_1" and restored["source"] == "ai"

    # 再存一版我的，然后设计变了：被顶下去的 AI 蓝图已经过时，恢复之后这一场暂时没有笔记
    client.put(f"/api/v1/scenes/{SCENE_ID}/blueprint", json={"fields": {"image_anchor": "第三遍"}})
    session.expire_all()
    supersede_scene_planning_artifacts(session, scene_ids=[SCENE_ID], reason="design_changed")
    session.commit()
    after = client.post(f"/api/v1/scenes/{SCENE_ID}/blueprint/restore-ai", json={}).json()["data"]

    assert after["blueprint"] is None


def test_regenerate_redoes_the_blueprint_and_the_character_pressure(client, session) -> None:
    _seed(session)
    _add_ai_blueprint(session)
    client.put(f"/api/v1/scenes/{SCENE_ID}/blueprint", json={"fields": {"image_anchor": "我的意象"}})

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/blueprint/generate", json={})

    assert response.status_code == 200, response.text
    view = response.json()["data"]
    assert view["blueprint"]["source"] == "ai"
    assert view["blueprint"]["row_id"] != "bp_ai_1"
    assert view["character_pressure"] is not None
    session.expire_all()
    live = session.execute(
        select(SceneBlueprint).where(SceneBlueprint.scene_id == SCENE_ID, SceneBlueprint.status == "accepted")
    ).scalars().all()
    assert [row.row_id for row in live] == [view["blueprint"]["row_id"]]


def test_writes_are_refused_while_a_draft_job_runs_and_bad_bodies_are_400(client, session) -> None:
    _seed(session)
    _add_ai_blueprint(session)

    unknown = client.put(f"/api/v1/scenes/{SCENE_ID}/blueprint", json={"fields": {"chapter_promise": "x"}})
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "SCENE_BLUEPRINT_FIELD_UNKNOWN"
    empty = client.put(
        f"/api/v1/scenes/{SCENE_ID}/blueprint",
        json={"fields": {key: "" for key in AI_BLUEPRINT}},
    )
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "SCENE_BLUEPRINT_EMPTY"

    session.get(SceneRunState, SCENE_ID).active_run_job_id = "job_running"
    session.commit()
    busy = client.post(f"/api/v1/scenes/{SCENE_ID}/blueprint/generate", json={})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "RUN_JOB_IN_PROGRESS"
    assert client.get(f"/api/v1/scenes/{SCENE_ID}/blueprint").json()["data"]["running"] is True
