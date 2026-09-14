"""阶段 J（2026-09-14 雪花评估第二轮）：原著有、本项目没有的几栏。

Ingermanson 第 9 步：列出在场人物、描述设定；场景表可以带时间戳；分诊第 5 步：写下这一场要给读者的
情绪；Dynamite Scene 第 4 章：每场都要决定人称与时态。这里锁住：第 10 步的在场人物 / 故事时间 /
读者应感到能存、能物化、能回流、能进结构简报；01 的叙述人称与时态进设计上下文并有约束力。
"""

from __future__ import annotations

import pathlib

import yaml

from sqlalchemy import select

from novel_system.db.models import SceneCard, SnowflakeStepRun, StoryCharacter, StoryProject
from novel_system.services.scene_design_context import render_scene_design_context
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from tests.test_snowflake_rendering_mode import PROJECT_ID, _materialize, _plan, _seed


def _fill_u1(session, service: SnowflakeWorkspaceService) -> None:
    plan = _plan(session, "u1")
    service.update_scene_plan(
        PROJECT_ID,
        plan.scene_plan_id,
        {
            "onstage_chars_json": ["c2", "c3"],
            "story_time": "第三天傍晚",
            "expected_reader_emotion": "替她捏一把汗，又暗暗希望她再赌一次。",
        },
    )


def test_method_fields_persist_materialize_and_reach_the_structure_brief(session) -> None:
    service = _seed(session)
    for character_id, name, role in (("c1", "她", "主角"), ("c2", "弟弟", "盟友"), ("c3", "债主", "对手")):
        session.add(StoryCharacter(character_id=character_id, project_id=PROJECT_ID, display_name=name, role=role, summary_json={}, synopsis_json={}, bible_json={}, status="approved"))
    session.flush()
    _fill_u1(session, service)
    plan = _plan(session, "u1")
    assert plan.onstage_chars_json == ["c2", "c3"]
    assert plan.story_time == "第三天傍晚"
    assert plan.expected_reader_emotion.startswith("替她捏一把汗")
    payload = next(step for step in service.workspace(PROJECT_ID)["steps"] if step["step_key"] == "scene_details")["draft"]["scenes"]
    row = next(item for item in payload if item["row_uid"] == "u1")
    assert row["onstage_chars_json"] == ["c2", "c3"] and row["story_time"] == "第三天傍晚"

    _materialize(session, service)
    card = session.get(SceneCard, plan.scene_id)
    assert card.onstage_chars_json == ["c2", "c3"]
    assert card.writer_brief_json["story_time"] == "第三天傍晚"
    assert card.writer_brief_json["expected_reader_emotion"].startswith("替她捏一把汗")
    brief = render_scene_structure_brief(card, session) or ""
    assert "Onstage characters: 弟弟, 债主" in brief
    assert "Story time (故事时间): 第三天傍晚" in brief
    assert "Reader should feel (读者应感到): 替她捏一把汗，又暗暗希望她再赌一次。" in brief
    # 刚物化完没有待同步；改了故事时间之后回流能追上
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0
    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"story_time": "第四天清晨"})
    status = service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))
    assert plan.scene_id in status["pending_scene_plan_ids"] or any(item["scene_id"] == plan.scene_id for item in status["pending_scenes"])
    service.resync_materialized_scenes(PROJECT_ID, {"scene_ids": [plan.scene_id]})
    session.expire_all()
    assert session.get(SceneCard, plan.scene_id).writer_brief_json["story_time"] == "第四天清晨"

    # 设计上下文的在场一句话来自角色摘要表；这里没有摘要表，但在场名单仍从计划行 / 场景卡进简报
    design = render_scene_design_context(card, session) or ""
    assert "Next scene" in design or "Previous scene" in design or design


def test_narrative_stance_reaches_the_design_context_and_is_kept_when_compressed(session) -> None:
    service = _seed(session)
    session.add(
        SnowflakeStepRun(
            step_run_id="run_prj-render_book_brief_approved",
            project_id=PROJECT_ID,
            step_key="book_brief",
            version=1,
            status="approved",
            draft_json={"category": "悬疑", "narrative_stance": "第三人称限知，过去时，每场固定一个视角人物"},
            approved_at="2026-09-14T00:00:00Z",
        )
    )
    session.flush()
    _materialize(session, service)
    card = session.get(SceneCard, _plan(session, "u1").scene_id)
    text = render_scene_design_context(card, session) or ""
    assert "Narrative stance (binding: person and tense): 第三人称限知，过去时，每场固定一个视角人物" in text
    from novel_system.services.context_budget import compress_design_context

    assert "Narrative stance" in compress_design_context(text)


def test_prompts_and_templates_carry_the_method_fields() -> None:
    from novel_system.services.snowflake_steps import get_step_definition

    template = next(field for field in get_step_definition("scene_details")["editor"]["fields"] if field["key"] == "scenes")["template"]
    assert template["onstage_chars_json"] == [] and template["story_time"] == "" and template["expected_reader_emotion"] == ""
    brief_fields = {field["key"] for field in get_step_definition("book_brief")["editor"]["fields"]}
    assert "narrative_stance" in brief_fields

    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    details = templates["snowflake_generate_scene_details"]
    assert details["version"] == "2026-09-14.v12"
    for key in ("onstage_chars_json", "story_time", "expected_reader_emotion"):
        assert key in details["task_prompt"], key
    brief = templates["snowflake_generate_book_brief"]
    assert brief["version"] == "2026-09-14.v3"
    assert "narrative_stance" in brief["task_prompt"] and "narrative_stance" in brief["structured_schema"]["properties"]
    review = templates["near_final_acceptance_review"]
    assert review["version"] == "2026-09-14.v7" and "Reader should feel" in review["task_prompt"]
    for name, version in (("neutral_draft", "2026-09-14.v11"), ("style_first_draft", "2026-09-14.v6")):
        assert templates[name]["version"] == version, name
        assert "Narrative stance" in templates[name]["task_prompt"] and "Reader should feel" in templates[name]["task_prompt"], name


def _book_brief_runs(session, project_id: str) -> list[tuple[int, str]]:
    rows = session.execute(
        select(SnowflakeStepRun)
        .where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "book_brief")
        .order_by(SnowflakeStepRun.version)
    ).scalars().all()
    return [(row.version, row.status) for row in rows]


def test_empty_narrative_stance_is_not_a_content_change(session) -> None:
    """阶段 J 给 01 加的 narrative_stance 是可选栏：升级前确认过的 01 草稿里没有这个键，前端水合后会把
    "" 原样发回，default_draft 也会把 "" 合并进每一次 re-PATCH。空值与缺席同义——不能让已确认的 01
    回到待审、不能制造新版本（阶段 C 的 rendering_mode=full 是同一条规则）；真填了人称与时态才是内容改动。
    全套回归里 test_snowflake_reapprove_invalidation 的两条「原样 re-PATCH」用例就是这样被打红的。"""
    from novel_system.services.snowflake_staleness import semantic_payload

    assert semantic_payload({"a": "", "b": None, "c": [], "d": {}, "e": "x", "fe_t": 1}) == {"e": "x"}
    assert semantic_payload({"narrative_stance": "  "}) == semantic_payload({})
    assert semantic_payload({"narrative_stance": "第一人称，现在时"}) != semantic_payload({})

    project_id = "prj-stance"
    session.add(StoryProject(project_id=project_id, title="人称", outline_text="大纲", planning_mode="snowflake", snowflake_workflow_mode="explore", target_word_count=100000))
    session.flush()
    service = SnowflakeWorkspaceService(session)
    brief = {
        "category": "文学悬疑", "target_reader": "想看旧案与家庭代价的读者", "story_kind": "家庭真相悬疑",
        "genre_promise": "真相越清晰失去越多", "delight_reason": "线索逼近真相的同时抬高代价",
        "expected_reader_emotion": "压迫与向前的拉力", "safety_rules": ["只借鉴抽象手法"],
    }
    service.update_step(project_id, "book_brief", {"draft": brief})
    service.approve_step(project_id, "book_brief")
    run = session.execute(select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "book_brief")).scalars().one()
    assert run.status == "approved"
    # 模拟阶段 J 之前存下的已确认草稿：没有 narrative_stance 这个键
    run.draft_json = {key: value for key, value in dict(run.draft_json or {}).items() if key != "narrative_stance"}
    session.flush()

    service.update_step(project_id, "book_brief", {"draft": {**brief, "narrative_stance": ""}})
    session.flush()
    assert _book_brief_runs(session, project_id) == [(1, "approved")]

    service.update_step(project_id, "book_brief", {"draft": {**brief, "narrative_stance": "第三人称限知，过去时"}})
    session.flush()
    assert _book_brief_runs(session, project_id) == [(1, "approved"), (2, "pending_review")]

