"""B06-01（2026-09-30 作者批准 #16c）：手加角色的 id 带作品前缀，不同作品的 c1 / c2 不再撞上全局主键。

React 的角色表按 c1 / c2 / … 给手加的角色编号，服务端原样写进 ``story_characters.character_id``（全局主键）：
第二部作品确认角色表时把第一部作品的同号角色改成了自己的名字，按 id 解析视角 / 在场人物的读者把别的作品的人名
带进提示词。现在写入即规范成 ``f"{project_id}_{raw}"``（视角 / 在场 / 全书主角的引用指着角色才补前缀，手填的
姓名原样），交给前端的草稿再剥掉服务端补的那层前缀——前端本机缓存与 fe_scaffold 里的 c1 仍然对得上，服务端自己
铸的号原样给，保真合并按 id 对位不断。
"""

from __future__ import annotations

from itertools import count
from typing import Any

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    SceneCard,
    SnowflakeCharacterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
)
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.snowflake_character_ids import (
    canonical_character_id,
    canonicalize_draft,
    present_character_id,
    present_draft,
)
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from tests.support.snowflake import (
    approve_step as _approve,
    create_project,
    patch_step as _patch,
    post_generate,
    step_of as _step,
)

_KEYS = count()


def _key(prefix: str) -> dict[str, str]:
    return {"X-Idempotency-Key": f"{prefix}-{next(_KEYS)}"}


def _create(client, title: str) -> str:
    return create_project(client, title=title, outline_text="旧信把她带回雨城。")["project_id"]


def _skip(client, project_id: str, step_key: str) -> None:
    response = post_generate(client, project_id, step_key, {"skip": True, "skip_reason": "这本书用不上这一层"})
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

    # 服务端铸的号带着前缀原样交给前端；原样推回去，库里还是同一个人（不因位置变化换号）
    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    presented = _step(workspace, "character_sheets")["draft"]["characters"]
    assert [item["character_id"] for item in presented] == minted
    assert [present_character_id(project_id, value) for value in minted] == minted
    _patch(client, project_id, "character_sheets", {"characters": list(reversed(presented))})
    session.expire_all()
    plans = {
        row.display_name: row.character_id
        for row in session.execute(select(SnowflakeCharacterPlan).where(SnowflakeCharacterPlan.project_id == project_id)).scalars()
    }
    assert plans == {"林昭": minted[0], "程远": minted[1]}


@pytest.mark.parametrize(
    ("frontend", "canonical"),
    [
        ("c1", "P_c1"),  # 前端编的号：服务端补前缀，交回去再剥掉
        ("char_lin", "P_char_lin"),  # 模型起的别名同理
        ("P_CHAR01", "P_CHAR01"),  # 服务端自己铸的号一向带前缀交出：两个口径是同一个字符串
        ("P_CHAR_ab12cd34", "P_CHAR_ab12cd34"),
        ("", ""),
    ],
)
def test_the_two_id_spaces_round_trip(frontend: str, canonical: str) -> None:
    assert canonical_character_id("P", frontend) == canonical
    assert present_character_id("P", canonical) == frontend
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
    assert presented["characters"][1]["character_id"] == canonical["characters"][1]["character_id"], "服务端铸的号原样交出"
    assert presented["scenes"][0]["onstage_chars_json"] == ["c1", "c2"]
    assert presented["fe_scaffold"] == draft["fe_scaffold"]


def test_references_are_prefixed_only_when_they_point_at_a_character() -> None:
    """视角 / 在场 / 全书主角可以是手填的姓名（04 名册还空时视角是自由文本框，模型也会回姓名）：只有指着角色的
    引用才补前缀——名册里有它，或者它长着前端编号的样子。资料库 / 章节编排铸的全局 CHAR_ id 同样原样。"""
    roster_reads: list[int] = []

    def roster() -> list[str]:
        roster_reads.append(1)
        return ["P_char_lin", "P_CHAR01"]

    draft = {
        "scenes": [
            {"pov_character_id": "林昭", "onstage_chars_json": ["程远", "c4", "char_lin", "CHAR_9F3A0B1C2D"]},
            {"pov_character_id": "P_CHAR01", "onstage_chars_json": ["c1"]},
        ]
    }
    canonical = canonicalize_draft("P", draft, roster=roster)
    assert canonical["scenes"][0] == {
        "pov_character_id": "林昭",
        "onstage_chars_json": ["程远", "P_c4", "P_char_lin", "CHAR_9F3A0B1C2D"],
    }
    assert canonical["scenes"][1] == {"pov_character_id": "P_CHAR01", "onstage_chars_json": ["P_c1"]}
    assert len(roster_reads) == 1, "名册只在认不出一个引用时取一次"
    assert present_draft("P", canonical) == draft, "交回前端的正是它送来的样子"

    # 前端的编号与已带前缀的 id 用不着名册
    roster_reads.clear()
    canonicalize_draft("P", {"scenes": [{"pov_character_id": "c1", "onstage_chars_json": ["P_CHAR01"]}]}, roster=roster)
    assert roster_reads == []
    # 04 的全书主角指着本稿自己的成员（别名也认得出，不必查名册）
    sheets = canonicalize_draft("P", {"characters": [{"character_id": "lead", "display_name": "林昭"}], "protagonist_character_id": "lead"})
    assert sheets["protagonist_character_id"] == "P_lead"
    assert canonicalize_draft("P", {"protagonist_character_id": "nobody"})["protagonist_character_id"] == "nobody"


def _service(session, project_id: str) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=project_id,
            title="手填视角",
            outline_text="她回到雨城。\n旧信揭开旧案。\n她公开真相。",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
    return SnowflakeWorkspaceService(session)


def _workspace_step(service: SnowflakeWorkspaceService, project_id: str, step_key: str) -> dict:
    return _step(service.workspace(project_id), step_key)


def test_a_typed_name_pov_stays_a_name_in_the_store_and_in_the_drafting_brief(session) -> None:
    """04 名册还空时 09 的视角是自由文本框。给姓名补前缀，起草简报就写「POV character: <作品>_林昭」，
    按姓名判「视角是不是全书主角」也判反（说成视角人物不是主角、他得偿所愿反倒算挫败）。"""
    project_id = "prj-typed-pov"
    service = _service(session, project_id)
    rows = [{"row_uid": "r1", "summary": "她在码头截下旧信", "pov_character_id": "林昭", "location": "码头", "crucible": "信被截走就再也追不回", "chapter_role": "起疑"}]
    listed = service.update_step(project_id, "scene_list", {"draft": {"scenes": rows}})
    assert _step(listed["workspace"], "scene_list")["draft"]["scenes"][0]["pov_character_id"] == "林昭"
    detail = {**_workspace_step(service, project_id, "scene_details")["draft"]["scenes"][0], "goal": "截下旧信", "onstage_chars_json": ["程远"]}
    service.update_step(project_id, "scene_details", {"draft": {"scenes": [detail]}})

    plan = session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars().one()
    assert plan.pov_character_id == "林昭"
    assert plan.onstage_chars_json == ["程远"]
    card = SceneCard(
        scene_id="typed-pov-card",
        chapter_id="typed-pov-ch",
        project_id=project_id,
        scene_seq=1,
        scene_goal="截下旧信",
        scene_type="proactive",
        pov_character_id=plan.pov_character_id,
        onstage_chars_json=list(plan.onstage_chars_json or []),
        writer_brief_json={"goal": "截下旧信", "conflict": "送信人不肯交", "setback": "信被烧了一半", "protagonist_hint": "林昭"},
    )
    lines = (render_scene_structure_brief(card, session) or "").splitlines()
    assert "POV character: 林昭" in lines
    assert "Onstage characters: 程远" in lines
    protagonist = next(line for line in lines if line.startswith("Protagonist"))
    assert "not the protagonist" not in protagonist, protagonist
    assert project_id not in "\n".join(lines)


def test_an_approved_scene_list_with_a_typed_name_pov_survives_the_frontend_echo(session) -> None:
    """部署前存下的手填视角（迁移只改名册里的 id，姓名原样留着）：前端每次打开构思都会把 09 原样上行一次，
    故事没变就不能把已确认的 09 打回待审——那会挡住「确认写入」，再确认又被当成改了视角。"""
    project_id = "prj-typed-echo"
    service = _service(session, project_id)
    fe_rows = [{"row_uid": "r1", "scene_seq": 1, "summary": "她在码头截下旧信", "primary_form": "proactive",
                "pov_character_id": "林昭", "location": "码头", "crucible": "信被截走就再也追不回", "chapter_role": "起疑", "spine": ""}]
    service.update_step(project_id, "scene_list", {"draft": {"scenes": fe_rows}})
    run = session.execute(
        select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "scene_list")
    ).scalars().one()
    # 旧库里的样子：视角原样是姓名；这一版已经确认
    run.draft_json = {**run.draft_json, "scenes": [dict(row, pov_character_id="林昭") for row in run.draft_json["scenes"]]}
    run.status = "approved"
    for plan in session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars():
        plan.pov_character_id = "林昭"
        plan.status = "approved"
    session.flush()
    before = _workspace_step(service, project_id, "scene_list")
    assert (before["status"], before["version"]) == ("approved", 1)

    # 前端的上行：服务端规范草稿的镜像与本机规范草稿按行合并（mergeCanon）
    echo = _merge_canon(before["draft"]["scenes"], fe_rows)
    service.update_step(project_id, "scene_list", {"draft": {"scenes": echo}})

    after = _workspace_step(service, project_id, "scene_list")
    assert (after["status"], after["version"], after["revised_after_approval"]) == ("approved", 1, False)
    assert after["draft"]["scenes"][0]["pov_character_id"] == "林昭"


def test_a_frontend_reference_is_canonical_even_before_its_character_is_saved(session) -> None:
    """前端在 04 加了 c4，还没上行就在 09 选了他做视角：长着前端编号样子的引用直接补前缀，不等名册。
    模型起的别名（名册里有 <作品>_char_lin）同样认得出，交回前端还是它送来的样子。"""
    project_id = "prj-early-ref"
    service = _service(session, project_id)
    service.update_step(project_id, "character_sheets", {"draft": {"characters": [{"character_id": "char_lin", "display_name": "林昭", "role": "主角"}]}})
    rows = [
        {"row_uid": "r1", "summary": "码头截信", "pov_character_id": "c4", "location": "码头", "crucible": "困局", "chapter_role": "起疑"},
        {"row_uid": "r2", "summary": "旅馆对质", "pov_character_id": "char_lin", "location": "旅馆", "crucible": "无人可信", "chapter_role": "转向"},
    ]
    listed = service.update_step(project_id, "scene_list", {"draft": {"scenes": rows}})
    assert [row["pov_character_id"] for row in _step(listed["workspace"], "scene_list")["draft"]["scenes"]] == ["c4", "char_lin"]
    plans = {
        plan.row_uid: plan.pov_character_id
        for plan in session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars()
    }
    assert plans == {"r1": f"{project_id}_c4", "r2": f"{project_id}_char_lin"}


def _merge_canon(server: Any, fe: Any) -> Any:
    """``ws-snow-canon.js`` 的 mergeCanon 照搬（前端上行时「作者主权」的合并）：数组以前端成员为准，成员按
    character_id / row_uid / scene_id 对位继承服务端的键；对象递归；前端缺席的值保服务端的。"""
    if isinstance(fe, list):
        if not isinstance(server, list):
            return fe
        merged = []
        for item in fe:
            if not isinstance(item, dict):
                merged.append(item)
                continue
            key = next((name for name in ("character_id", "row_uid", "scene_id") if item.get(name) not in (None, "")), None)
            match = next((row for row in server if isinstance(row, dict) and key and row.get(key) == item.get(key)), None)
            merged.append(_merge_canon(match, item) if match is not None else item)
        return merged
    if isinstance(fe, dict):
        if not isinstance(server, dict):
            return fe
        return {**server, **{name: _merge_canon(server.get(name), value) for name, value in fe.items()}}
    if fe is None and server is not None:
        return server
    return fe


def test_ids_the_server_minted_before_the_deploy_reach_the_frontend_unchanged(session) -> None:
    """旧服务端铸的号（模型漏了 character_id 时清洗器兜底的 <作品>_CHAR01、v1 规划器的 <作品>_CHAR_<hex>）当时就
    带着前缀交给了前端，前端本机缓存按这个 id 记着人。交出去时再剥一层（CHAR01），保真合并就对不上：上行的成员
    丢掉服务端才有的人物小传栏（表单上没有的年龄、人物怎么变），已确认的 08 被打回待审。"""
    project_id = "prj-legacy-minted"
    service = _service(session, project_id)
    legacy_id = f"{project_id}_CHAR01"
    member = {
        "character_id": legacy_id, "display_name": "林昭", "role": "主角",
        "physical_profile": {"age": "二十八", "appearance": "瘦"},
        "personality_profile": {"strongest_trait": "倔", "weakest_trait": "多疑"},
        "environment_profile": {"home": "雨城"},
        "psychological_profile": {"philosophy": "真相", "self_image": "局外人", "deepest_fear": "被遗忘", "how_character_changes": "学会信人"},
    }
    service.update_step(project_id, "character_bibles", {"draft": {"characters": [member]}})
    run = session.execute(
        select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == "character_bibles")
    ).scalars().one()
    run.status = "approved"
    session.flush()

    presented = _workspace_step(service, project_id, "character_bibles")
    assert presented["draft"]["characters"][0]["character_id"] == legacy_id
    # 前端缓存（部署前）按旧 id 记着这个人；canonFromFE("profile") 只发表单上的栏
    fe_member = {
        "character_id": legacy_id, "display_name": "林昭", "role": "主角",
        "physical_profile": {"appearance": "瘦"}, "personality_profile": {"strongest_trait": "倔"},
        "environment_profile": {"home": "雨城"},
        "psychological_profile": {"philosophy": "真相", "self_image": "局外人", "deepest_fear": "被遗忘"},
    }
    echo = {"characters": _merge_canon(presented["draft"]["characters"], [fe_member])}
    service.update_step(project_id, "character_bibles", {"draft": echo})

    after = _workspace_step(service, project_id, "character_bibles")
    assert (after["status"], after["version"]) == ("approved", 1)
    stored = after["draft"]["characters"][0]
    assert stored["physical_profile"]["age"] == "二十八"
    assert stored["psychological_profile"]["how_character_changes"] == "学会信人"


def test_the_history_summary_speaks_the_frontend_ids(client) -> None:
    """历史列表的一行摘要与同一项的草稿一样按前端口径：不冒出库里的 <作品>_c1。"""
    project_id = _create(client, "历史摘要作品")
    _patch(client, project_id, "character_sheets", {"characters": [_sheet("c1", "林昭", "主角")]})
    response = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace/steps/character_sheets/history?include_draft=true")
    assert response.status_code == 200, response.text
    item = response.json()["data"]["items"][0]
    assert item["draft"]["characters"][0]["character_id"] == "c1"
    assert item["draft_summary"].startswith("c1 林昭 主角"), item["draft_summary"]
    assert project_id not in item["draft_summary"]
