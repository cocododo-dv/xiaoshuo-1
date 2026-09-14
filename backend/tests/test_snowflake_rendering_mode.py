"""阶段 C（2026-09-13 雪花评估）：反应场的呈现方式 full / summary，从第 10 步一路带到起草。

Ingermanson：反应场可以整场写、缩成两段概述、或干脆略过；现代趋势是少写反应场。第一版做前两档：
- 第 10 步只对反应场保存 rendering_mode；主动场与非法值一律 full；
- 物化 / 回流：summary 场拿到数值篇幅带 200-500（现有数值长度机制据此硬约束），简报带 rendering_mode，
  改回 full 时场景卡回到 medium；
- 结构简报渲染「Rendering mode: summary」，起草 / 蓝图 / 场景规划提示词认识它；
- 默认值 full 与「缺席」同义（semantic_payload 剥掉），升级不把已确认的场景规划打回待审；
- 分章节奏体检把概述场按半场计。
"""

from __future__ import annotations

import pathlib

import yaml

from novel_system.db.models import (
    OutlinePlan,
    SceneCard,
    SnowflakeScenePlan,
    StoryProject,
)
from novel_system.services.projects import PLAN_STATUS_PENDING_REVIEW, ProjectService
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService, _rhythm_report
from novel_system.services.snowflake_staleness import semantic_payload
from novel_system.services.snowflake_steps import RENDERING_MODES, SUMMARY_LENGTH_BAND, _scene_detail_seed
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService, _effective_rendering_mode
from novel_system.services.snowflake_workspace_llm import _sanitize_scene_detail_items

PROJECT_ID = "prj-render"


def _scene_rows() -> list[dict]:
    return [
        {"row_uid": "u1", "scene_seq": 1, "summary": "取账本", "primary_form": "proactive", "scene_type": "proactive",
         "location": "码头", "crucible": "退不出的困局", "pov_character_id": "c1", "chapter_role": "起疑"},
        {"row_uid": "u2", "scene_seq": 2, "summary": "消化挫败", "primary_form": "reactive", "scene_type": "reactive",
         "location": "旅馆", "crucible": "无人可信", "pov_character_id": "c1", "chapter_role": "转向"},
        {"row_uid": "u3", "scene_seq": 3, "summary": "再受挫", "primary_form": "reactive", "scene_type": "reactive",
         "location": "码头", "crucible": "只剩一晚", "pov_character_id": "c1", "chapter_role": "转向"},
    ]


def _seed(session) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="概述两段",
            outline_text="概述两段大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": _scene_rows()}})
    scenes = service.workspace(PROJECT_ID)
    listed = next(step for step in scenes["steps"] if step["step_key"] == "scene_list")["draft"]["scenes"]
    details = []
    for scene in listed:
        row = {
            **scene,
            "title": scene["summary"],
            "goal": "拿到账本" if scene["primary_form"] == "proactive" else "",
            "conflict": "三轮受阻" if scene["primary_form"] == "proactive" else "",
            "setback": "账本被烧" if scene["primary_form"] == "proactive" else "",
            "reaction": "" if scene["primary_form"] == "proactive" else "手抖，半天说不出话。",
            "dilemma": "" if scene["primary_form"] == "proactive" else "报警伤弟弟；不报警明天轮到自己。",
            "decision": "" if scene["primary_form"] == "proactive" else "去找当年的证人。",
            "cost_requirement": "失去遗物",
        }
        if scene["row_uid"] == "u1":
            row["rendering_mode"] = "summary"  # 主动场：必须被收口成 full
        elif scene["row_uid"] == "u2":
            row["rendering_mode"] = "summary"
        else:
            row["rendering_mode"] = "bogus"  # 非法值：full
        details.append(row)
    service.update_step(PROJECT_ID, "scene_details", {"draft": {"scenes": details}})
    return service


def _plan(session, row_uid: str) -> SnowflakeScenePlan:
    return next(
        plan
        for plan in session.query(SnowflakeScenePlan).filter(SnowflakeScenePlan.project_id == PROJECT_ID).all()
        if plan.row_uid == row_uid
    )


# ---------------------------------------------------------------------------
# 第 10 步：只有反应场能选 summary
# ---------------------------------------------------------------------------


def test_only_reactive_scenes_keep_a_summary_rendering_mode(session) -> None:
    service = _seed(session)
    assert _plan(session, "u1").rendering_mode == "full"
    assert _plan(session, "u2").rendering_mode == "summary"
    assert _plan(session, "u3").rendering_mode == "full"

    payload = next(
        step for step in service.workspace(PROJECT_ID)["steps"] if step["step_key"] == "scene_details"
    )["draft"]["scenes"]
    by_uid = {scene["row_uid"]: scene for scene in payload}
    assert by_uid["u2"]["rendering_mode"] == "summary"
    assert by_uid["u1"]["rendering_mode"] == "full"

    # 场景类型改回主动：呈现方式跟着收口
    plan = _plan(session, "u2")
    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"primary_form": "proactive"})
    assert _plan(session, "u2").rendering_mode == "full"


def test_effective_rendering_mode_and_seed_defaults() -> None:
    assert RENDERING_MODES == ("full", "summary", "skip")
    assert _effective_rendering_mode("reactive", "summary") == "summary"
    assert _effective_rendering_mode("reactive", "SUMMARY ") == "summary"
    assert _effective_rendering_mode("proactive", "summary") == "full"
    assert _effective_rendering_mode("reactive", "skip") == "skip"  # 阶段 I：原著的第三个选项
    assert _effective_rendering_mode("proactive", "skip") == "full"
    assert _effective_rendering_mode("reactive", "bogus") == "full"
    assert _scene_detail_seed({"summary": "x", "primary_form": "reactive"}, 1)["rendering_mode"] == "full"


def test_llm_output_may_suggest_summary_for_reactive_scenes_only() -> None:
    base = [
        {"scene_id": "SC1", "primary_form": "proactive", "scene_type": "proactive", "summary": "取账本"},
        {"scene_id": "SC2", "primary_form": "reactive", "scene_type": "reactive", "summary": "消化挫败"},
    ]
    merged = _sanitize_scene_detail_items(
        [
            {"scene_id": "SC1", "rendering_mode": "summary", "goal": "拿到账本"},
            {"scene_id": "SC2", "rendering_mode": "Summary", "reaction": "手抖"},
        ],
        project_id="P",
        base_items=base,
    )
    by_id = {item["scene_id"]: item for item in merged}
    assert "rendering_mode" not in by_id["SC1"]
    assert by_id["SC2"]["rendering_mode"] == "summary"


# ---------------------------------------------------------------------------
# 物化 / 回流：summary 场拿数值篇幅带，简报带呈现方式
# ---------------------------------------------------------------------------


def _materialize(session, service: SnowflakeWorkspaceService) -> dict:
    service.update_step(
        PROJECT_ID,
        "long_synopsis",
        {"draft": {"paragraphs": ["第一幕"], "chapters": [{"act": 1, "title": "第一章", "summary": "全书一章", "chapter_goal": "推进"}]}},
    )
    SnowflakeChapteringService(session).autoassign(PROJECT_ID, "even")
    project = session.get(StoryProject, PROJECT_ID)
    plan_json = service._build_chaptered_outline_plan(project, service._scene_plans(PROJECT_ID))
    outline = OutlinePlan(
        plan_id="outline_plan_prj-render_01",
        project_id=PROJECT_ID,
        version=1,
        status=PLAN_STATUS_PENDING_REVIEW,
        plan_json=plan_json,
    )
    session.add(outline)
    session.flush()
    ProjectService(session).approve_outline_plan(PROJECT_ID, outline.plan_id)
    session.flush()
    return plan_json


def test_materialization_gives_summary_scenes_a_numeric_band_and_briefs_carry_the_mode(session) -> None:
    service = _seed(session)
    plan_json = _materialize(session, service)
    scenes = {scene["scene_id"]: scene for chapter in plan_json["chapters"] for scene in chapter["scenes"]}
    summary_scene = scenes[_plan(session, "u2").scene_id]
    full_scene = scenes[_plan(session, "u1").scene_id]
    other_reactive = scenes[_plan(session, "u3").scene_id]

    assert summary_scene["target_length_band"] == SUMMARY_LENGTH_BAND == "200-500"
    assert summary_scene["writer_brief_json"]["rendering_mode"] == "summary"
    assert summary_scene["writer_brief_json"]["timebox"] == SUMMARY_LENGTH_BAND
    assert full_scene["target_length_band"] == "medium"
    assert full_scene["writer_brief_json"]["rendering_mode"] == "full"
    assert other_reactive["writer_brief_json"]["rendering_mode"] == "full"

    card = session.get(SceneCard, summary_scene["scene_id"])
    assert card.target_length_band == SUMMARY_LENGTH_BAND
    brief = render_scene_structure_brief(card, session)
    assert "Rendering mode: summary (概述两段)" in brief
    assert "Target length band: 200-500" in brief
    assert "Rendering mode" not in render_scene_structure_brief(session.get(SceneCard, full_scene["scene_id"]), session)


def test_switching_back_to_full_resyncs_the_card_to_the_default_band(session) -> None:
    service = _seed(session)
    _materialize(session, service)
    plan = _plan(session, "u2")
    card = session.get(SceneCard, plan.scene_id)
    assert card.target_length_band == SUMMARY_LENGTH_BAND

    # 刚物化完不应报待同步
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0

    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"rendering_mode": "full"})
    status = service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))
    pending = {item["scene_id"]: item for item in status["pending_scenes"]}
    assert plan.scene_id in pending
    assert "target_length_band" in pending[plan.scene_id]["changed_fields"]

    service.resync_materialized_scenes(PROJECT_ID, {"scene_ids": [plan.scene_id]})
    session.expire_all()
    card = session.get(SceneCard, plan.scene_id)
    assert card.target_length_band == "medium"
    assert card.writer_brief_json["rendering_mode"] == "full"
    assert card.writer_brief_json["timebox"] == "medium"
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0


# ---------------------------------------------------------------------------
# 升级不打回：默认值 full 与缺席同义
# ---------------------------------------------------------------------------


def test_default_rendering_mode_does_not_change_the_semantic_payload() -> None:
    before = {"scenes": [{"row_uid": "u2", "reaction": "手抖"}]}
    after = {"scenes": [{"row_uid": "u2", "reaction": "手抖", "rendering_mode": "full"}]}
    assert semantic_payload(before) == semantic_payload(after)
    summary = {"scenes": [{"row_uid": "u2", "reaction": "手抖", "rendering_mode": "summary"}]}
    assert semantic_payload(summary) != semantic_payload(before)
    assert semantic_payload(summary)["scenes"][0]["rendering_mode"] == "summary"


# ---------------------------------------------------------------------------
# 分章节奏体检：概述场按半场计
# ---------------------------------------------------------------------------


def test_rhythm_report_weights_summary_scenes_as_half() -> None:
    chapters = [
        {"chapter_seq": 1, "title": "一", "act": 1, "spine": "灾一", "scene_count": 4,
         "scenes": [{"rendering_mode": "full"}, {"rendering_mode": "summary"}, {"rendering_mode": "summary"}, {"rendering_mode": "full"}]},
        {"chapter_seq": 2, "title": "二", "act": 2, "spine": "灾二", "scene_count": 2,
         "scenes": [{"rendering_mode": "full"}, {"rendering_mode": "full"}]},
    ]
    report = _rhythm_report(chapters)
    assert report["scene_counts"] == [4, 2]
    assert report["weighted_scene_counts"] == [3.0, 2.0]
    assert report["summary_scene_count"] == 2
    assert report["mean_scenes_per_chapter"] == 2.5
    assert report["max_scenes"] == 4


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def test_prompts_know_the_summary_rendering_mode() -> None:
    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    assert templates["snowflake_generate_scene_details"]["version"] == "2026-09-14.v12"
    assert "rendering_mode (reactive scenes only" in templates["snowflake_generate_scene_details"]["task_prompt"]
    for name, version in (("neutral_draft", "2026-09-14.v11"), ("style_first_draft", "2026-09-14.v6"), ("scene_blueprint", "2026-09-14.v9")):
        assert templates[name]["version"] == version, name
        assert "Rendering mode: summary" in templates[name]["task_prompt"], name
