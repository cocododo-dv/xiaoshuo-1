"""阶段 L（2026-09-14 雪花评估第二轮）：评估里的 bug 清单。

- 回流跨章移动把目标章的章目标盖到旧章上；搬动后 is_chapter_last 不重算；
- 双主角作品被「第一个定位为主角的人」猜错——04 显式指定 protagonist_character_id；
- 「概述两段」的反应场在风格直起下被放宽成 100–750 字；
- 重生成 09 时场景 id 按位置铸造——提示词要求回显 row_uid / scene_id，清洗器保留它们。
"""

from __future__ import annotations

import pathlib

import yaml

from novel_system.db.models import ChapterGoal, SceneCard, SnowflakeChapterPlan, StoryCharacter
from novel_system.services.scene_generation import _style_first_length_slack
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from novel_system.services.snowflake_workspace_llm import _sanitize_scene_list_items
from tests.test_snowflake_rendering_mode import PROJECT_ID, _materialize, _plan, _seed


def test_resync_cross_chapter_move_keeps_the_source_chapter_goal_and_recomputes_chapter_last(session) -> None:
    service = _seed(session, )
    service.update_step(
        PROJECT_ID,
        "long_synopsis",
        {"draft": {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": [
            {"act": 1, "title": "第一章", "summary": "起", "chapter_goal": "第一章的目标"},
            {"act": 1, "title": "第二章", "summary": "承", "chapter_goal": "第二章的目标"},
        ]}},
    )
    chaptering = SnowflakeChapteringService(session)
    chapters = chaptering.chapter_plans(PROJECT_ID)
    assert len(chapters) == 2
    plans = service._scene_plans(PROJECT_ID)
    # u1, u2 → 第一章；u3 → 第二章
    chaptering.save(PROJECT_ID, {"assignments": [
        {"scene_plan_id": _plan(session, "u1").scene_plan_id, "chapter_row_uid": chapters[0].row_uid},
        {"scene_plan_id": _plan(session, "u2").scene_plan_id, "chapter_row_uid": chapters[0].row_uid},
        {"scene_plan_id": _plan(session, "u3").scene_plan_id, "chapter_row_uid": chapters[1].row_uid},
    ]})
    from novel_system.db.models import OutlinePlan, StoryProject
    from novel_system.services.projects import PLAN_STATUS_PENDING_REVIEW, ProjectService

    project = session.get(StoryProject, PROJECT_ID)
    plan_json = service._build_chaptered_outline_plan(project, service._scene_plans(PROJECT_ID))
    outline = OutlinePlan(plan_id="outline_plan_prj-render_L", project_id=PROJECT_ID, version=1, status=PLAN_STATUS_PENDING_REVIEW, plan_json=plan_json)
    session.add(outline)
    session.flush()
    ProjectService(session).approve_outline_plan(PROJECT_ID, outline.plan_id)
    session.flush()

    first_id = f"{PROJECT_ID}_CH01"
    second_id = f"{PROJECT_ID}_CH02"
    assert session.get(ChapterGoal, first_id).chapter_goal == "第一章的目标"
    assert session.get(ChapterGoal, second_id).chapter_goal == "第二章的目标"
    u2 = session.get(SceneCard, _plan(session, "u2").scene_id)
    assert u2.chapter_id == first_id and u2.is_chapter_last == 1

    # 把 u2 搬到第二章，回流
    chaptering.save(PROJECT_ID, {"assignments": [
        {"scene_plan_id": _plan(session, "u1").scene_plan_id, "chapter_row_uid": chapters[0].row_uid},
        {"scene_plan_id": _plan(session, "u3").scene_plan_id, "chapter_row_uid": chapters[1].row_uid},
        {"scene_plan_id": _plan(session, "u2").scene_plan_id, "chapter_row_uid": chapters[1].row_uid},
    ]})
    service.resync_materialized_scenes(PROJECT_ID, {})
    session.expire_all()
    u1 = session.get(SceneCard, _plan(session, "u1").scene_id)
    u2 = session.get(SceneCard, _plan(session, "u2").scene_id)
    u3 = session.get(SceneCard, _plan(session, "u3").scene_id)
    assert u2.chapter_id == second_id
    # 旧章的章目标没被目标章的盖掉
    assert session.get(ChapterGoal, first_id).chapter_goal == "第一章的目标"
    assert session.get(ChapterGoal, second_id).chapter_goal == "第二章的目标"
    # 章末标记重算：第一章只剩 u1。第二章里 u2 排在 u3 前面——章内顺序永远等于故事序（09 的行序 u1、u2、u3），
    # 不看分章载荷里的先后（阶段 V：章是故事序上连续的一段，没有第二套「章内手排」），所以章末是 u3。
    assert u1.is_chapter_last == 1
    assert (u2.scene_seq, u3.scene_seq) == (1, 2)
    assert u2.is_chapter_last == 0 and u3.is_chapter_last == 1


def test_explicit_protagonist_outranks_the_first_lead_role(session) -> None:
    service = _seed(session)
    service.update_step(
        PROJECT_ID,
        "character_sheets",
        {"draft": {
            "protagonist_character_id": "c2",
            "characters": [
                {"character_id": "c1", "display_name": "甲", "role": "主角", "goal": "x"},
                {"character_id": "c2", "display_name": "乙", "role": "主角", "goal": "y"},
            ],
        }},
    )
    hint = service._protagonist_hint(PROJECT_ID)
    assert hint == {"character_id": "c2", "display_name": "乙"}
    # 没有显式指定：退回第一个「主角」
    service.update_step(PROJECT_ID, "character_sheets", {"draft": {"protagonist_character_id": "", "characters": [
        {"character_id": "c1", "display_name": "甲", "role": "主角", "goal": "x"},
        {"character_id": "c2", "display_name": "乙", "role": "主角", "goal": "y"},
    ]}})
    assert service._protagonist_hint(PROJECT_ID)["character_id"] == "c1"
    # 指向不存在的角色：忽略，退回启发式
    service.update_step(PROJECT_ID, "character_sheets", {"draft": {"protagonist_character_id": "nobody", "characters": [
        {"character_id": "c1", "display_name": "甲", "role": "主角", "goal": "x"},
    ]}})
    assert service._protagonist_hint(PROJECT_ID)["character_id"] == "c1"


def test_summary_scenes_get_no_style_first_length_slack(monkeypatch) -> None:
    import novel_system.services.scene_generation as generation

    monkeypatch.setattr(generation, "is_style_bound", lambda bundle: True)
    full = {"inline_digests": {"scene_structure_brief": "Scene form: reactive scene\nRendering mode: full"}}
    summary = {"inline_digests": {"scene_structure_brief": "Scene form: reactive scene\nRendering mode: summary (概述两段) — ..."}}
    assert _style_first_length_slack(full) > 0.0
    assert _style_first_length_slack(summary) == 0.0


def test_scene_list_sanitizer_keeps_echoed_identities_and_the_prompt_asks_for_them() -> None:
    template = {"row_uid": "", "scene_id": "", "chapter_id": "", "chapter_title": "", "summary": "", "primary_form": "proactive", "scene_type": "proactive", "location": "", "crucible": "", "chapter_role": "", "spine": ""}
    items = _sanitize_scene_list_items(
        [
            {"row_uid": "row_keep", "scene_id": "PRJ_CH01_SC01", "summary": "改写后的第一场", "primary_form": "proactive"},
            {"row_uid": "", "scene_id": "", "summary": "全新的一场", "primary_form": "reactive"},
        ],
        template=template,
        project_id="PRJ",
        base_items=[],
    )
    assert items[0]["row_uid"] == "row_keep" and items[0]["scene_id"] == "PRJ_CH01_SC01"
    assert items[1]["row_uid"] == "" and items[1]["scene_id"]  # 新场由服务端铸 id
    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    scene_list = templates["snowflake_generate_scene_list"]
    assert scene_list["version"] == "2026-09-22.v11"
    assert "must echo that scene's row_uid and scene_id unchanged" in scene_list["task_prompt"]
    sheets = templates["snowflake_generate_character_sheets"]
    assert sheets["version"] == "2026-09-14.v6" and "protagonist_character_id" in sheets["structured_schema"]["properties"]
