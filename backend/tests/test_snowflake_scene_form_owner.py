"""F02-01 的后端兜底（Q2 转来）：一场的形态（主动 / 反应）与视角人物归 09 场景列表。

前端第 10 步曾把渲染时的默认值 mode / pov 冻进本地计划，推 10 的时候把 09 刚改过的形态与视角改了回去
（服务端 ``_sanitize_scene_patch`` 把 primary_form 映射成 scene_type，第 10 步的同步照单全收）。前端已经修好
（第 10 步的形态与视角只读、在 09 改）；服务端这里再兜一层：第 10 步的草稿不改已有场景计划的形态与视角。
第 10 步里第一次出现的场（09 还没有这一行）照旧按它带来的形态与视角建行——那是唯一的来源。
"""

from __future__ import annotations

from tests.support.snowflake import RENDER_PROJECT_ID as PROJECT_ID, scene_plan as _plan, seed_render_project as _seed

# 库里的角色 id 带作品前缀（B06-01）；前端草稿里写的是不带前缀的 c1 / c2
C1, C2 = f"{PROJECT_ID}_c1", f"{PROJECT_ID}_c2"


def _details_row(service, row_uid: str) -> dict:
    workspace = service.workspace(PROJECT_ID)
    step = next(item for item in workspace["steps"] if item["step_key"] == "scene_details")
    return next(dict(scene) for scene in step["draft"]["scenes"] if scene["row_uid"] == row_uid)


def test_scene_details_cannot_revert_the_form_or_pov_that_09_owns(session) -> None:
    service = _seed(session)
    assert _plan(session, "u2").scene_type == "reactive"
    assert _plan(session, "u2").pov_character_id == C1

    stale_row = {**_details_row(service, "u2"), "primary_form": "proactive", "scene_type": "proactive", "pov_character_id": "c9"}
    stale_row["dilemma"] = "报警伤弟弟；不报警明天轮到自己，还会连累证人。"
    service.update_step(PROJECT_ID, "scene_details", {"draft": {"scenes": [stale_row]}})

    plan = _plan(session, "u2")
    assert plan.scene_type == "reactive"
    assert plan.pov_character_id == C1
    assert plan.dilemma.endswith("还会连累证人。"), "第 10 步自己的字段照常落库"


def test_09_still_changes_the_form_and_pov(session) -> None:
    service = _seed(session)
    workspace = service.workspace(PROJECT_ID)
    rows = [dict(scene) for scene in next(s for s in workspace["steps"] if s["step_key"] == "scene_list")["draft"]["scenes"]]
    for row in rows:
        if row["row_uid"] == "u2":
            row.update(primary_form="proactive", scene_type="proactive", pov_character_id="c2")
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": rows}})

    plan = _plan(session, "u2")
    assert plan.scene_type == "proactive"
    assert plan.pov_character_id == C2


def test_a_scene_first_seen_in_step_10_takes_its_form_from_step_10(session) -> None:
    service = _seed(session)
    new_row = {
        "row_uid": "u-new",
        "summary": "只在第 10 步出现的一场",
        "primary_form": "reactive",
        "pov_character_id": "c2",
        "reaction": "她把信又读了一遍。",
    }
    service.update_step(PROJECT_ID, "scene_details", {"draft": {"scenes": [new_row]}})

    plan = _plan(session, "u-new")
    assert plan.scene_type == "reactive"
    assert plan.pov_character_id == C2
