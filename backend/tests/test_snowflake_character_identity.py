"""B06-01（2026-09-30 作者批准 #16c）：手加角色的 id 带作品前缀，不同作品的 c1 / c2 不再撞上全局主键。

React 的角色表按 c1 / c2 / … 给手加的角色编号，服务端原样写进 ``story_characters.character_id``（全局主键）：
第二部作品确认角色表时把第一部作品的同号角色改成了自己的名字，按 id 解析视角 / 在场人物的读者把别的作品的人名
带进提示词。现在写入即规范成 ``f"{project_id}_{raw}"``（视角 / 在场 / 全书主角的引用同一口径），交给前端的草稿
再剥掉前缀——前端本机缓存与 fe_scaffold 里的 c1 仍然对得上，保真合并按 id 对位不断。
"""

from __future__ import annotations

from itertools import count

import pytest
from sqlalchemy import select

from novel_system.db.models import SceneCard, SnowflakeCharacterPlan, SnowflakeScenePlan, SnowflakeStepRun, StoryCharacter
from novel_system.services.snowflake_character_ids import (
    canonical_character_id,
    canonicalize_draft,
    present_character_id,
    present_draft,
)

_KEYS = count()


def _key(prefix: str) -> dict[str, str]:
    return {"X-Idempotency-Key": f"{prefix}-{next(_KEYS)}"}


def _create(client, title: str) -> str:
    response = client.post("/api/v2/projects", json={"title": title, "outline_text": "旧信把她带回雨城。"}, headers=_key("char-id-create"))
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _patch(client, project_id: str, step_key: str, draft: dict) -> dict:
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        json={"draft": draft, "force": True},
        headers=_key("char-id-patch"),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve(client, project_id: str, step_key: str) -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve", json={}, headers=_key("char-id-approve")
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _skip(client, project_id: str, step_key: str) -> None:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json={"skip": True, "skip_reason": "这本书用不上这一层"},
        headers=_key("char-id-skip"),
    )
    assert response.status_code == 200, response.text


def _sheet(character_id: str, name: str, role: str) -> dict:
    return {"character_id": character_id, "display_name": name, "role": role, "goal": f"{name}想要的东西"}


def _confirm_through_sheets(client, project_id: str, characters: list[dict], **extra) -> dict:
    _patch(client, project_id, "book_brief", {"category": "悬疑", "target_reader": "喜欢旧案的读者"})
    _approve(client, project_id, "book_brief")
    _patch(client, project_id, "one_sentence_summary", {"summary": "她必须查清旧信的来历，但每一步都更贵。"})
    _approve(client, project_id, "one_sentence_summary")
    _patch(client, project_id, "one_paragraph_summary", {"sentences": ["一", "二", "三", "四", "五"]})
    _approve(client, project_id, "one_paragraph_summary")
    draft = {
        "characters": characters,
        "fe_scaffold": {"sel": characters[0]["character_id"], "chars": {c["character_id"]: {"name": c["display_name"]} for c in characters}},
        **extra,
    }
    _patch(client, project_id, "character_sheets", draft)
    return _approve(client, project_id, "character_sheets")


def _step(workspace: dict, step_key: str) -> dict:
    return next(step for step in workspace["steps"] if step["step_key"] == step_key)


def test_two_works_with_the_same_hand_made_ids_keep_their_own_characters(client, session) -> None:
    work_a = _create(client, "甲作品")
    work_b = _create(client, "乙作品")
    _confirm_through_sheets(client, work_a, [_sheet("c1", "林昭", "主角"), _sheet("c2", "程远", "对手")])
    _confirm_through_sheets(client, work_b, [_sheet("c1", "苏晴", "主角"), _sheet("c2", "韩默", "对手")])

    session.expire_all()
    rows = {row.character_id: row for row in session.execute(select(StoryCharacter)).scalars()}
    assert rows[f"{work_a}_c1"].display_name == "林昭" and rows[f"{work_a}_c1"].project_id == work_a
    assert rows[f"{work_a}_c2"].display_name == "程远"
    assert rows[f"{work_b}_c1"].display_name == "苏晴" and rows[f"{work_b}_c1"].project_id == work_b
    assert rows[f"{work_b}_c2"].display_name == "韩默"
    assert "c1" not in rows and "c2" not in rows
    plans = {
        row.character_id
        for row in session.execute(select(SnowflakeCharacterPlan).where(SnowflakeCharacterPlan.project_id == work_a)).scalars()
    }
    assert plans == {f"{work_a}_c1", f"{work_a}_c2"}


def test_the_frontend_sees_its_own_ids_and_the_stored_draft_is_canonical(client, session) -> None:
    project_id = _create(client, "对位作品")
    approved = _confirm_through_sheets(
        client, project_id, [_sheet("c1", "林昭", "主角"), _sheet("c2", "程远", "对手")], protagonist_character_id="c1"
    )
    presented = approved["step"]["draft"]
    assert [item["character_id"] for item in presented["characters"]] == ["c1", "c2"]
    assert presented["protagonist_character_id"] == "c1"
    # 写穿缓存是前端自己的数据，原样
    assert set(presented["fe_scaffold"]["chars"]) == {"c1", "c2"}

    session.expire_all()
    stored = session.execute(
        select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "character_sheets")
    ).scalars().one()
    assert [item["character_id"] for item in stored.draft_json["characters"]] == [f"{project_id}_c1", f"{project_id}_c2"]
    assert stored.draft_json["protagonist_character_id"] == f"{project_id}_c1"
    assert set(stored.draft_json["fe_scaffold"]["chars"]) == {"c1", "c2"}

    # 同一份内容原样再推一次（前端每次打开都会上行）：不是改动，不打回待审
    again = _patch(client, project_id, "character_sheets", {**presented})
    assert again["step"]["status"] == "approved"


def test_scene_references_are_canonical_in_the_store_and_plain_for_the_frontend(client, session) -> None:
    project_id = _create(client, "视角作品")
    _confirm_through_sheets(client, project_id, [_sheet("c1", "林昭", "主角"), _sheet("c2", "程远", "对手")])
    scenes = [
        {"row_uid": "r1", "summary": "取账本", "primary_form": "proactive", "pov_character_id": "c1", "location": "码头", "crucible": "困局"},
        {"row_uid": "r2", "summary": "消化挫败", "primary_form": "reactive", "pov_character_id": "c2", "location": "旅馆", "crucible": "无人可信"},
    ]
    listed = _patch(client, project_id, "scene_list", {"scenes": scenes})
    rows = _step(listed["workspace"], "scene_list")["draft"]["scenes"]
    assert [row["pov_character_id"] for row in rows] == ["c1", "c2"]
    details = [{**row, "goal": "拿到账本", "onstage_chars_json": ["c1", "c2"]} for row in rows]
    detailed = _patch(client, project_id, "scene_details", {"scenes": details})
    detail_rows = _step(detailed["workspace"], "scene_details")["draft"]["scenes"]
    assert detail_rows[0]["onstage_chars_json"] == ["c1", "c2"]

    session.expire_all()
    plans = {plan.row_uid: plan for plan in session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars()}
    assert plans["r1"].pov_character_id == f"{project_id}_c1"
    assert plans["r2"].pov_character_id == f"{project_id}_c2"
    assert plans["r1"].onstage_chars_json == [f"{project_id}_c1", f"{project_id}_c2"]


def test_materialized_cards_resolve_the_pov_to_this_works_character(client, session) -> None:
    from novel_system.services.scene_structure_brief import render_scene_structure_brief

    work_a = _create(client, "甲作品二")
    work_b = _create(client, "乙作品二")
    _confirm_through_sheets(client, work_a, [_sheet("c1", "林昭", "主角")], protagonist_character_id="c1")
    _confirm_through_sheets(client, work_b, [_sheet("c1", "苏晴", "主角")])
    for step_key in ("short_synopsis", "character_synopses"):
        _skip(client, work_a, step_key)
    _patch(client, work_a, "long_synopsis", {"paragraphs": ["一"], "chapters": [{"act": 1, "title": "第一章", "summary": "取账本", "chapter_goal": "拿到账本"}]})
    _approve(client, work_a, "long_synopsis")
    _skip(client, work_a, "character_bibles")
    scenes = [{"row_uid": "r1", "summary": "取账本", "primary_form": "proactive", "pov_character_id": "c1", "location": "码头", "crucible": "困局"}]
    _patch(client, work_a, "scene_list", {"scenes": scenes})
    _approve(client, work_a, "scene_list")
    workspace = client.get(f"/api/v2/projects/{work_a}/snowflake-workspace").json()["data"]
    detail = {**_step(workspace, "scene_details")["draft"]["scenes"][0], "goal": "拿到账本", "conflict": "三轮受阻", "setback": "账本被烧"}
    _patch(client, work_a, "scene_details", {"scenes": [detail]})
    _approve(client, work_a, "scene_details")
    response = client.post(
        f"/api/v2/projects/{work_a}/snowflake-workspace/materialize",
        json={"strategy": "even"},
        headers=_key("char-id-materialize"),
    )
    assert response.status_code == 200, response.text
    scene_payload = response.json()["data"]["plan"]["plan_json"]["chapters"][0]["scenes"][0]
    assert scene_payload["pov_character_id"] == f"{work_a}_c1"
    assert scene_payload["writer_brief_json"]["protagonist_character_id"] == f"{work_a}_c1"
    response = client.post(f"/api/v2/projects/{work_a}/snowflake-workspace/outline/approve", json={}, headers=_key("char-id-outline"))
    assert response.status_code == 200, response.text

    session.expire_all()
    card = session.get(SceneCard, scene_payload["scene_id"])
    assert card.pov_character_id == f"{work_a}_c1"
    brief = render_scene_structure_brief(card, session) or ""
    assert "林昭" in brief and "苏晴" not in brief


def test_a_character_without_an_id_gets_a_stable_minted_id_not_a_position(client, session) -> None:
    project_id = _create(client, "铸号作品")
    _patch(client, project_id, "character_sheets", {"characters": [{"display_name": "林昭", "role": "主角"}, {"display_name": "程远", "role": "对手"}]})
    session.expire_all()
    stored = session.execute(
        select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "character_sheets")
    ).scalars().one()
    minted = [item["character_id"] for item in stored.draft_json["characters"]]
    assert all(value.startswith(f"{project_id}_CHAR_") for value in minted)
    assert f"{project_id}_CHAR01" not in minted and len(set(minted)) == 2

    # 前端拿到的是剥了前缀的 id；原样推回去，库里还是同一个人（不因位置变化换号）
    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    presented = _step(workspace, "character_sheets")["draft"]["characters"]
    assert [item["character_id"] for item in presented] == [present_character_id(project_id, value) for value in minted]
    _patch(client, project_id, "character_sheets", {"characters": list(reversed(presented))})
    session.expire_all()
    plans = {
        row.display_name: row.character_id
        for row in session.execute(select(SnowflakeCharacterPlan).where(SnowflakeCharacterPlan.project_id == project_id)).scalars()
    }
    assert plans == {"林昭": minted[0], "程远": minted[1]}


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("c1", "P_c1"), ("P_c1", "P_c1"), ("P_CHAR_ab12cd34", "P_CHAR_ab12cd34"), ("", "")],
)
def test_the_two_id_spaces_round_trip(raw: str, canonical: str) -> None:
    assert canonical_character_id("P", raw) == canonical
    assert canonical_character_id("P", present_character_id("P", canonical)) == canonical


def test_draft_mapping_leaves_the_frontend_cache_alone() -> None:
    draft = {
        "characters": [{"character_id": "c1", "display_name": "林昭"}, {"display_name": "程远", "role": "对手"}, {}],
        "protagonist_character_id": "c1",
        "scenes": [{"pov_character_id": "c1", "onstage_chars_json": ["c1", "", "c2"]}],
        "fe_scaffold": {"chars": {"c1": {"name": "林昭"}}, "list": [{"pov": "c1"}]},
    }
    canonical = canonicalize_draft("P", draft, mint_missing=True)
    assert canonical["characters"][0]["character_id"] == "P_c1"
    assert canonical["characters"][1]["character_id"].startswith("P_CHAR_")
    assert "character_id" not in canonical["characters"][2], "完全空的成员不铸号"
    assert canonical["protagonist_character_id"] == "P_c1"
    assert canonical["scenes"][0] == {"pov_character_id": "P_c1", "onstage_chars_json": ["P_c1", "P_c2"]}
    assert canonical["fe_scaffold"] == draft["fe_scaffold"]
    presented = present_draft("P", canonical)
    assert presented["characters"][0]["character_id"] == "c1"
    assert presented["scenes"][0]["onstage_chars_json"] == ["c1", "c2"]
    assert presented["fe_scaffold"] == draft["fe_scaffold"]
