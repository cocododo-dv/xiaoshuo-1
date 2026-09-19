"""2026-09-18「整理成章节结构出来的章乱七八糟」回归。

真实项目（17 场，三场的功能标签写着 灾难一 / 灾难二 / 灾难三）上一次走完的路：

1. 07 里点了两行「（待补）」占位章 → 面板把 17 场均摊进两个「（待补）」；
2. 「确认写入」→ 目录里作者手建过一章「第 1 章」，物化撞 ``(project_id, display_order)`` 唯一索引 → 500；
3. 再点「按场景重排章表」→ ``scene_seq`` 此时已是章内序，分章却按它当全书序读 → 两章的场交错洗在一起
   （1、10、2、11、3、12……）；功能标签里的「灾难一」旧正则认不出 → 没有铰链、全书一幕；
4. 参考书章长把每章场数推到 12 → 17 场只分两章，面板上没有任何地方能改；
5. 分章写在计划行上的章字段经前端回传进 09 草稿 → 已确认的 09 被判成「故事改了」打回待审 → 物化闸门拦下。

这里一条一条锁住。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryProject,
)
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    propose_chapter_chunks,
    scene_spine,
    spine_from_role,
)
from novel_system.services.snowflake_staleness import semantic_payload
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from novel_system.services.snowflake_workspace_llm import enrich_structured_schema

PROJECT_ID = "prj-story-order"

#: 真实项目的形状：17 场，灾难只写在功能标签里（spine 列是空的）
_ROLES = {5: "灾难一·一幕高潮", 9: "中点逆转/道德抉择", 12: "灾难二·二幕高潮", 15: "灾难三·三幕高潮"}


def _rows(count: int = 17, *, roles: dict[int, str] | None = None) -> list[dict]:
    marks = _ROLES if roles is None else roles
    return [
        {
            "row_uid": f"u{index:02d}",
            "scene_seq": index,  # 前端 09 发的是全书序 i + 1
            "summary": f"第 {index} 场",
            "primary_form": "proactive",
            "scene_type": "proactive",
            "location": "林场",
            "crucible": "退不出的困局",
            "pov_character_id": "c1",
            "chapter_role": marks.get(index, "推进"),
            "spine": "",
        }
        for index in range(1, count + 1)
    ]


def _seed(session, *, chapters: list[dict] | None = None, count: int = 17) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="何来",
            outline_text="大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(
        PROJECT_ID, "long_synopsis", {"draft": {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": chapters or []}}
    )
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": _rows(count)}})
    return service


def _uids(preview: dict) -> list[list[str]]:
    return [[scene["row_uid"] for scene in chapter["scenes"]] for chapter in preview["chapters"]]


def _payload(preview: dict) -> dict:
    return {
        "replace_chapters": True,
        "chapters": [
            {"row_uid": c["row_uid"], "title": c["title"], "act": c["act"], "spine": c["spine"],
             "chapter_goal": c["chapter_goal"], "summary": c["summary"]}
            for c in preview["chapters"]
        ],
        "assignments": [
            {"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": c["row_uid"]}
            for c in preview["chapters"] for s in c["scenes"]
        ],
    }


# ------------------------------------------------------------------ 灾难标记


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("灾难一·一幕高潮", "灾一"),
        ("灾难二·二幕高潮", "灾二"),
        ("灾难三", "灾三"),
        ("灾一", "灾一"),
        ("第二个灾难", "灾二"),
        ("灾难 3 / 高潮", "灾三"),
        ("Disaster 1", "灾一"),
        ("一幕高潮", ""),
        ("中点逆转/道德抉择", ""),
        ("", ""),
    ],
)
def test_the_disaster_mark_is_read_from_the_function_label_the_prompt_itself_uses(role: str, expected: str) -> None:
    assert spine_from_role(role) == expected


def test_an_explicit_spine_outranks_the_function_label(session) -> None:
    service = _seed(session)
    plans = service._scene_plans(PROJECT_ID)
    assert scene_spine(plans[4]) == "灾一"  # 只写在功能标签里
    plans[4].spine = "灾二"
    assert scene_spine(plans[4]) == "灾二"
    plans[4].spine = "不是标记"
    assert scene_spine(plans[4]) == ""


def test_the_wire_schema_lets_a_schema_enforcing_backend_write_spine() -> None:
    """spine 不在 09 的编辑器模板里；不单独补进 wire schema，按 schema 解码的中转就写不出提示词要的标记。"""
    schema = {"type": "object", "properties": {"scenes": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}}
    enriched, applied = enrich_structured_schema(schema, step_key="scene_list")
    assert "scenes_items" in applied
    assert enriched["properties"]["scenes"]["items"]["properties"]["spine"] == {"type": "string"}
    # 别的键照旧来自编辑器模板
    assert {"summary", "chapter_role", "primary_form"} <= set(enriched["properties"]["scenes"]["items"]["properties"])


# ------------------------------------------------------------------ 故事序


def test_rechaptering_after_a_saved_chaptering_keeps_the_story_order(session) -> None:
    """核心回归：第一次分章落库后 scene_seq 是章内序——第二次分章不许把两章的场交错洗在一起。"""
    _seed(session)
    chaptering = SnowflakeChapteringService(session)
    first = chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 9})
    expected = [f"u{index:02d}" for index in range(1, 18)]
    assert [uid for chapter in _uids(first) for uid in chapter] == expected

    again = chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 3, "replace": True})
    assert [uid for chapter in _uids(again) for uid in chapter] == expected, "重排章表把场景顺序洗乱了"
    for strategy in ("from_scenes", "even", "spine_anchor", "keep_current"):
        preview = chaptering.preview(PROJECT_ID, {"strategy": strategy})
        assert [uid for chapter in _uids(preview) for uid in chapter] == expected, strategy

    # scene_seq 只有一种语义：章内位置，按故事序从 1 数
    by_chapter: dict[str, list[int]] = {}
    for plan in chaptering.scene_plans(PROJECT_ID):
        by_chapter.setdefault(plan.chapter_plan_id, []).append(plan.scene_seq)
    assert all(seqs == list(range(1, len(seqs) + 1)) for seqs in by_chapter.values())


def test_a_scene_details_sync_cannot_rewrite_the_order(session) -> None:
    """第 10 步的草稿可能只带回一部分场（分批生成 / 单场补全）：它不拥有顺序，也不许把章内序写乱。"""
    service = _seed(session)
    chaptering = SnowflakeChapteringService(session)
    chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 6})
    before = [(plan.row_uid, plan.chapter_plan_id, plan.scene_seq) for plan in chaptering.scene_plans(PROJECT_ID)]
    partial = [{**row, "goal": "目标", "conflict": "冲突", "setback": "挫折"} for row in reversed(_rows()[9:13])]
    service.update_step(PROJECT_ID, "scene_details", {"draft": {"scenes": partial}})
    after = [(plan.row_uid, plan.chapter_plan_id, plan.scene_seq) for plan in chaptering.scene_plans(PROJECT_ID)]
    assert after == before


def test_the_workspace_hands_the_frontend_the_scene_list_in_story_order(session) -> None:
    """工作台给 09 / 10 的草稿是从计划行现算的：分章之后也必须是作者排的那张表，而不是按章重洗的。"""
    service = _seed(session)
    SnowflakeChapteringService(session).propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 4})
    workspace = service.workspace(PROJECT_ID)
    for step_key in ("scene_list", "scene_details"):
        step = next(item for item in workspace["steps"] if item["step_key"] == step_key)
        assert [row["row_uid"] for row in step["draft"]["scenes"]] == [f"u{index:02d}" for index in range(1, 18)]


def test_reordering_the_scene_list_moves_the_story_order_with_it(session) -> None:
    service = _seed(session)
    rows = _rows()
    rows.insert(2, rows.pop(10))  # 把第 11 场拖到第 3 位
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": rows}})
    order = [plan.row_uid for plan in SnowflakeChapteringService(session).scene_plans(PROJECT_ID)]
    assert order[:4] == ["u01", "u02", "u11", "u03"]
    assert [plan.scene_seq for plan in service._scene_plans(PROJECT_ID)] == list(range(1, 18))


# ------------------------------------------------------------------ 提议：铰链 + 尺度


def test_the_three_disasters_close_their_chapters_on_the_real_book_shape(session) -> None:
    _seed(session)
    preview = SnowflakeChapteringService(session).preview(PROJECT_ID, {"strategy": "from_scenes", "scenes_per_chapter": 12})
    # 每章 12 场只够两章，但铰链优先：一幕收在灾一、灾二收束自己的章、灾三收束二幕、三幕另起
    assert _uids(preview) == [
        [f"u{i:02d}" for i in range(1, 6)],
        [f"u{i:02d}" for i in range(6, 13)],
        [f"u{i:02d}" for i in range(13, 16)],
        [f"u{i:02d}" for i in range(16, 18)],
    ]
    assert [chapter["spine"] for chapter in preview["chapters"]] == ["灾一", "灾二", "灾三", ""]
    assert [chapter["act"] for chapter in preview["chapters"]] == [1, 2, 2, 3]
    assert preview["scale"]["hinge_min_chapters"] == 4
    assert not [w for w in preview["warnings"] if w["kind"].startswith("spine_")]
    # 章摘要 = 章末那一场（这一章把局面推到哪）
    assert preview["chapters"][0]["summary"] == "第 5 场"
    # 每一场带着故事序号与功能标签，面板才扫得动
    fifth = preview["chapters"][0]["scenes"][4]
    assert fifth["story_index"] == 5 and fifth["function"] == "灾难一·一幕高潮" and fifth["spine"] == "灾一"


def test_the_disaster_two_act_always_gets_its_second_chapter(session) -> None:
    """下限按幕给：多出来的那一章不许被分给别的幕，让灾二、灾三挤在一章里。"""
    _seed(session)
    plans = SnowflakeChapteringService(session).scene_plans(PROJECT_ID)
    for target in (1, 2, 3, 4, 5, 8):
        chunks = propose_chapter_chunks(plans, target_chapter_count=target)
        spines = [chunk["spine"] for chunk in chunks]
        assert [mark for mark in spines if mark] == ["灾一", "灾二", "灾三"], target
        assert len(chunks) == max(4, target)


def test_the_panel_scale_request_outranks_the_project_setting(session) -> None:
    _seed(session)
    project = session.get(StoryProject, PROJECT_ID)
    project.target_chapter_count = 17
    session.flush()
    chaptering = SnowflakeChapteringService(session)
    assert chaptering.preview(PROJECT_ID, {"strategy": "from_scenes"})["scale"]["source"] == "project_target"
    asked = chaptering.preview(PROJECT_ID, {"strategy": "from_scenes", "scenes_per_chapter": 4})
    assert asked["scale"]["source"] == "request_per_chapter" and asked["scale"]["scenes_per_chapter"] == 4
    assert asked["totals"]["chapter_count"] == 5  # ceil(17 / 4)，铰链下限 4 之上
    project.target_chapter_count = None
    session.flush()
    assert chaptering.preview(PROJECT_ID, {"strategy": "from_scenes"})["scale"]["source"] == "default"


# ------------------------------------------------------------------ 面板打开时给作者看什么


def test_placeholder_chapters_are_not_a_chapter_table(session) -> None:
    """07 里点出来的两行「（待补）」不是分章决定：auto 直接按场景列表提议，而不是把场均摊进去。"""
    _seed(session, chapters=[
        {"row_uid": "", "act": 1, "title": "（待补）", "summary": "", "spine": ""},
        {"row_uid": "", "act": 2, "title": "（待补）", "summary": "", "spine": ""},
    ])
    chaptering = SnowflakeChapteringService(session)
    preview = chaptering.preview(PROJECT_ID, {"strategy": "auto"})
    assert preview["strategy"] == "from_scenes"
    assert preview["replaces_chapter_count"] == 2
    assert all(chapter["row_uid"].startswith("new:") for chapter in preview["chapters"])
    # 预览不落库：占位章还在，场一场都没绑
    assert len(chaptering.chapter_plans(PROJECT_ID)) == 2
    assert not any(plan.chapter_plan_id for plan in chaptering.scene_plans(PROJECT_ID))

    # 作者真的写过章表 → 把场倒进作者的章
    session.rollback()


def test_auto_shows_an_authored_table_or_the_saved_chaptering(session) -> None:
    _seed(session, chapters=[
        {"row_uid": "", "act": 1, "title": "旧日志", "summary": "他发现记录被人遮过。", "spine": "灾一"},
        {"row_uid": "", "act": 2, "title": "磁带", "summary": "他听见自己的声音。", "spine": "灾二"},
    ])
    chaptering = SnowflakeChapteringService(session)
    assert chaptering.preview(PROJECT_ID, {"strategy": "auto"})["strategy"] == "spine_anchor"
    chaptering.autoassign(PROJECT_ID, "even")
    # 已经分过章：打开面板看到的是存着的那一版，不是又一次重算
    assert chaptering.preview(PROJECT_ID, {"strategy": "auto"})["strategy"] == "keep_current"


def test_confirming_a_proposal_replaces_the_placeholder_table_and_mirrors_it(session) -> None:
    _seed(session, chapters=[
        {"row_uid": "", "act": 1, "title": "（待补）", "summary": "", "spine": ""},
        {"row_uid": "", "act": 2, "title": "（待补）", "summary": "", "spine": ""},
    ])
    chaptering = SnowflakeChapteringService(session)
    run = session.execute(
        select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == PROJECT_ID, SnowflakeStepRun.step_key == "long_synopsis")
    ).scalars().first()
    # 前端写穿缓存里也有那两行占位章——水合时它优先于规范字段
    run.draft_json = {**run.draft_json, "fe_scaffold": {"expansions": {}, "chapters": [{"id": "01", "act": 1, "title": "（待补）"}]}}
    session.flush()

    preview = chaptering.preview(PROJECT_ID, {"strategy": "from_scenes", "scenes_per_chapter": 5})
    payload = _payload(preview)
    payload["chapters"][0]["title"] = "旧日志"
    chaptering.save(PROJECT_ID, payload)

    chapters = chaptering.chapter_plans(PROJECT_ID)
    assert [chapter.title for chapter in chapters] == ["旧日志", "第 2 章", "第 3 章", "第 4 章"]
    assert all(not chapter.row_uid.startswith("new:") for chapter in chapters)
    removed = session.execute(
        select(SnowflakeChapterPlan).where(SnowflakeChapterPlan.project_id == PROJECT_ID, SnowflakeChapterPlan.removed_at.is_not(None))
    ).scalars().all()
    assert [row.title for row in removed] == ["（待补）", "（待补）"]
    plans = chaptering.scene_plans(PROJECT_ID)
    assert all(plan.chapter_plan_id for plan in plans)
    assert [plan.chapter_id for plan in plans][:6] == [f"{PROJECT_ID}_CH01"] * 5 + [f"{PROJECT_ID}_CH02"]

    session.refresh(run)
    assert [item["title"] for item in run.draft_json["chapters"]] == ["旧日志", "第 2 章", "第 3 章", "第 4 章"]
    mirrored = run.draft_json["fe_scaffold"]["chapters"]
    assert [item["title"] for item in mirrored] == ["旧日志", "第 2 章", "第 3 章", "第 4 章"]
    assert [item["row_uid"] for item in mirrored] == [chapter.row_uid for chapter in chapters]
    assert run.draft_json["fe_scaffold"]["expansions"] == {}


def test_split_and_merge_through_save(session) -> None:
    """面板里的「从这里另起一章」/「并入上一章」：新章用 new:N，没列出来的章被软删。"""
    _seed(session)
    chaptering = SnowflakeChapteringService(session)
    chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 12})
    current = chaptering.preview(PROJECT_ID, {"strategy": "keep_current"})
    assert [len(chapter["scenes"]) for chapter in current["chapters"]] == [5, 7, 3, 2]

    payload = _payload(current)
    first, second = current["chapters"][0], current["chapters"][1]
    # 第一章从第 3 场拆开；第三、四章并成一章
    payload["chapters"] = [
        payload["chapters"][0],
        {"row_uid": "new:a", "title": "拆出来的一章", "act": 1, "spine": "灾一"},
        payload["chapters"][1],
        payload["chapters"][2],
    ]
    payload["assignments"] = (
        [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": first["row_uid"]} for s in first["scenes"][:2]]
        + [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": "new:a"} for s in first["scenes"][2:]]
        + [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": second["row_uid"]} for s in second["scenes"]]
        + [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": current["chapters"][2]["row_uid"]}
           for chapter in current["chapters"][2:] for s in chapter["scenes"]]
    )
    chaptering.save(PROJECT_ID, payload)
    after = chaptering.preview(PROJECT_ID, {"strategy": "keep_current"})
    # 系统起的占位章名跟着章序重编（「第 2 章」现在排第三 → 「第 3 章」）；作者起的名字不动
    assert [chapter["title"] for chapter in after["chapters"]] == ["第 1 章", "拆出来的一章", "第 3 章", "第 4 章"]
    assert [len(chapter["scenes"]) for chapter in after["chapters"]] == [2, 3, 7, 5]
    # 从场上抄来的章摘要按新的章末重算：第一章现在收在第 2 场，拆出来的章收在第 5 场，合并后的末章收在第 17 场
    assert [chapter["summary"] for chapter in after["chapters"]] == ["第 2 场", "第 5 场", "第 12 场", "第 17 场"]
    # 章目标不拿摘要顶替：面板原样交回来的空值就是空值
    assert all(chapter["chapter_goal"] == "" for chapter in after["chapters"])
    # 场景行上的章标题 / 章目标跟着现在的章走，不残留上一个章的值
    stamped = {plan.row_uid: (plan.chapter_title, plan.chapter_goal) for plan in chaptering.scene_plans(PROJECT_ID)}
    assert stamped["u03"] == ("拆出来的一章", "第 5 场") and stamped["u17"] == ("第 4 章", "第 17 场")
    assert [uid for chapter in _uids(after) for uid in chapter] == [f"u{i:02d}" for i in range(1, 18)]
    # 物化目标章号跟着新的章序走——包括这次没被「点名搬动」的场
    assert {plan.chapter_id for plan in chaptering.scene_plans(PROJECT_ID) if plan.row_uid in {"u06", "u12"}} == {f"{PROJECT_ID}_CH03"}

    with pytest.raises(DomainError) as exc:
        chaptering.save(PROJECT_ID, {"replace_chapters": True, "chapters": [], "assignments": payload["assignments"]})
    assert exc.value.code == "SNOWFLAKE_CHAPTER_PLAN_PAYLOAD_EMPTY"
    with pytest.raises(DomainError) as exc:
        chaptering.save(PROJECT_ID, {"chapters": [{"row_uid": "new:zz", "title": "x"}], "assignments": payload["assignments"]})
    assert exc.value.code == "SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND", "不带 replace_chapters 时认不得的章仍然是 404"


def test_a_scene_dragged_out_of_its_chapter_range_is_reported(session) -> None:
    service = _seed(session)
    chaptering = SnowflakeChapteringService(session)
    chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 12})
    rows = _rows()
    rows.insert(1, rows.pop(13))  # 二幕后段的第 14 场被拖到第 2 位，章归属还留在原章
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": rows}})
    preview = chaptering.preview(PROJECT_ID, {"strategy": "keep_current"})
    kinds = [warning["kind"] for warning in preview["warnings"]]
    assert "chapter_order_conflict" in kinds
    assert all(warning["severity"] != "blocker" for warning in preview["warnings"])
    # 按场景重新分章就回到连续的一段一段
    fresh = chaptering.preview(PROJECT_ID, {"strategy": "from_scenes"})
    assert "chapter_order_conflict" not in [warning["kind"] for warning in fresh["warnings"]]


# ------------------------------------------------------------------ 分章不算「故事改了」


def test_chaptering_fields_on_scene_rows_are_not_story_content() -> None:
    before = {"scenes": [{"row_uid": "u01", "summary": "第 1 场", "chapter_id": "p_CH01", "chapter_title": "p_CH01",
                          "chapter_plan_id": "", "chapter_goal": "", "scene_seq": 1, "scene_plan_id": "sp1"}]}
    after = {"scenes": [{"row_uid": "u01", "summary": "第 1 场", "chapter_id": "p_CH02", "chapter_title": "第 2 章",
                         "chapter_plan_id": "cp2", "chapter_goal": "推到悬崖", "scene_seq": 5, "scene_plan_id": "sp1"}]}
    assert semantic_payload(before) == semantic_payload(after)
    edited = {"scenes": [{**after["scenes"][0], "summary": "改过的第 1 场"}]}
    assert semantic_payload(before) != semantic_payload(edited)


def test_confirming_a_chaptering_does_not_send_the_scene_list_back_to_review(client, session) -> None:
    from tests.test_snowflake_chaptering import _create_project, _seed as seed_project

    from tests.test_snowflake_chaptering import _approve

    project_id = _create_project(client, "no-flip")
    seed_project(client, project_id)
    base = f"/api/v2/projects/{project_id}/snowflake-workspace"

    def push_workspace_rows(step_key: str) -> str:
        # 前端的保真合并（mergeCanon）会把工作台交回来的场景行——连同章字段、status、诊断——原样上行
        workspace = client.get(base).json()["data"]
        step = next(item for item in workspace["steps"] if item["step_key"] == step_key)
        patched = client.patch(f"{base}/steps/{step_key}", json={"draft": step["draft"], "force": True})
        assert patched.status_code == 200, patched.text
        return patched.json()["data"]["step"]["status"]

    # 先让落库的草稿长成前端上行的形状（带全部服务端键），并处在已确认状态
    for step_key in ("scene_list", "scene_details"):
        if push_workspace_rows(step_key) != "approved":
            _approve(client, project_id, step_key)

    preview = client.post(f"{base}/chapter-plan/preview", json={"strategy": "from_scenes"}).json()["data"]
    assert client.patch(f"{base}/chapter-plan", json=_payload(preview)).status_code == 200

    # 分章改的只是计划行上的章字段；09 重新确认改的只是行上的 status——都不是故事
    for step_key in ("scene_list", "scene_details"):
        assert push_workspace_rows(step_key) == "approved", f"{step_key} 被分章打回了待审"


# ------------------------------------------------------------------ 落进目录


def _confirm(client, project_id: str, preview: dict, key: str) -> dict:
    materialize = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        json=_payload(preview), headers={"X-Idempotency-Key": f"{key}-mat"},
    )
    assert materialize.status_code == 200, materialize.text
    approve = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
        json={}, headers={"X-Idempotency-Key": f"{key}-approve"},
    )
    assert approve.status_code == 200, approve.text
    return approve.json()["data"]


def test_materializing_next_to_a_hand_made_chapter_does_not_500(client, session) -> None:
    """目录里作者手建过一章（display_order = 1）：新章接在它后面，不撞唯一索引、不动那一章。"""
    from tests.test_snowflake_chaptering import _create_project, _pass_triage, _seed as seed_project

    project_id = _create_project(client, "hand-made")
    seed_project(client, project_id)
    _pass_triage(client, project_id)
    created = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters", json={"title": "第 1 章"},
        headers={"X-Idempotency-Key": "hand-made-chapter"},
    )
    assert created.status_code == 200, created.text

    preview = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview", json={"strategy": "auto"}
    ).json()["data"]
    # 手建的章不是构思侧的分章决定：面板照常给 07 的章表，并提醒目录里有这么一章
    assert preview["totals"]["unassigned_count"] == 0
    assert "catalog_hand_made_chapters" in [warning["kind"] for warning in preview["warnings"]]

    result = _confirm(client, project_id, preview, "hand-made")
    assert result["created_chapter_count"] == len(preview["chapters"])
    session.expire_all()
    chapters = list(session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id).order_by(ChapterGoal.display_order)
    ).scalars())
    orders = [chapter.display_order for chapter in chapters]
    assert orders == sorted(set(orders)) and orders[0] == 1
    assert not chapters[0].chapter_id.endswith("_CH01"), "手建的那一章原地不动"
    assert [chapter.chapter_id[-5:] for chapter in chapters[1:]] == [f"_CH0{i}" for i in range(1, len(chapters))]


def test_rematerializing_after_rechaptering_moves_cards_without_a_constraint_error(client, session) -> None:
    """重新分章后再「确认写入」：场景卡跨章搬动，作者手加在章里的场跟着它原来的前一场走。"""
    from tests.test_snowflake_chaptering import _create_project, _pass_triage, _seed as seed_project

    project_id = _create_project(client, "remat")
    seed_project(client, project_id)
    _pass_triage(client, project_id)
    base = f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview"
    first = client.post(base, json={"strategy": "from_scenes", "scenes_per_chapter": 6}).json()["data"]
    _confirm(client, project_id, first, "remat-1")

    session.expire_all()
    first_chapter = f"{project_id}_CH01"
    extra = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{first_chapter}/scenes",
        json={"title": "作者手加的一场", "at": 1}, headers={"X-Idempotency-Key": "remat-extra"},
    )
    assert extra.status_code == 200, extra.text

    second = client.post(base, json={"strategy": "from_scenes", "scenes_per_chapter": 2}).json()["data"]
    assert len(second["chapters"]) > len(first["chapters"])
    _confirm(client, project_id, second, "remat-2")

    session.expire_all()
    cards = list(session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
    ).scalars())
    by_chapter: dict[str, list[SceneCard]] = {}
    for card in cards:
        by_chapter.setdefault(card.chapter_id, []).append(card)
    for chapter_id, members in by_chapter.items():
        seqs = sorted(card.scene_seq for card in members)
        assert seqs == list(range(1, len(seqs) + 1)), chapter_id
        assert sum(card.is_chapter_last for card in members) == 1, chapter_id
    plans = {plan.scene_id: plan for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars()}
    assert all(card.chapter_id == plans[card.scene_id].chapter_id for card in cards if card.scene_id in plans)
    hand_made = next(card for card in cards if card.scene_id not in plans)
    assert hand_made.chapter_id == first_chapter and hand_made.scene_seq == 2, "手加的场还跟在第 1 场后面"


# ------------------------------------------------------------------ 回流：章内顺序


def _cards(session, project_id: str) -> dict[str, list[str]]:
    session.expire_all()
    grouped: dict[str, list[SceneCard]] = {}
    for card in session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
    ).scalars():
        grouped.setdefault(card.chapter_id, []).append(card)
    for chapter_id, members in grouped.items():
        seqs = sorted(card.scene_seq for card in members)
        assert seqs == list(range(1, len(seqs) + 1)), (chapter_id, seqs)
        assert sum(card.is_chapter_last for card in members) == 1, chapter_id
    return {
        chapter_id[-5:]: [card.scene_id[-3:] for card in sorted(members, key=lambda card: card.scene_seq)]
        for chapter_id, members in sorted(grouped.items())
    }


def test_reordering_the_scene_list_after_materializing_resyncs_without_a_constraint_error(client, session) -> None:
    """09 里把两场对调 → 回流要在同一章里交换两张卡的序号：逐张 UPDATE 必撞 (chapter_id, scene_seq) 唯一索引。"""
    from tests.test_snowflake_chaptering import _create_project, _pass_triage, _patch, _scene, _seed as seed_project

    project_id = _create_project(client, "resync-order")
    seed_project(client, project_id)
    _pass_triage(client, project_id)
    base = f"/api/v2/projects/{project_id}/snowflake-workspace"
    preview = client.post(f"{base}/chapter-plan/preview", json={"strategy": "from_scenes", "scenes_per_chapter": 6}).json()["data"]
    _confirm(client, project_id, preview, "resync-order")
    before = _cards(session, project_id)
    assert before["_CH01"][:3] == ["S01", "S02", "S03"]
    # 手加一场，夹在第 1、2 场之间：计划外的卡不该让整章被误报成待同步
    extra = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{project_id}_CH01/scenes",
        json={"title": "作者手加的一场", "at": 1}, headers={"X-Idempotency-Key": "resync-order-extra"},
    )
    assert extra.status_code == 200, extra.text
    status = client.get(base).json()["data"]["resync_status"]
    assert status["pending_count"] == 0, status

    # 09：第 2、3 场对调
    order = [1, 3, 2, *range(4, 13)]
    from tests.test_snowflake_chaptering import _SPINE_AT
    _patch(client, project_id, "scene_list", {"scenes": [_scene(f"S{i:02d}", i, f"事件{i}", _SPINE_AT.get(i, "")) for i in order]})
    status = client.get(base).json()["data"]["resync_status"]
    drifting = {item["scene_id"][-3:]: item["changed_fields"] for item in status["pending_scenes"]}
    assert set(drifting) == {"S02", "S03"} and all("scene_order" in fields for fields in drifting.values())

    resync = client.post(f"{base}/resync", json={})
    assert resync.status_code == 200, resync.text
    after = _cards(session, project_id)
    first = after["_CH01"]
    assert [item for item in first if item.startswith("S")][:3] == ["S01", "S03", "S02"]
    assert first[1] not in {"S02", "S03"}, "手加的那一场还跟在第 1 场后面"
    assert client.get(base).json()["data"]["resync_status"]["pending_count"] == 0


def test_an_authored_chapter_summary_survives_a_restructure(session) -> None:
    _seed(session)
    chaptering = SnowflakeChapteringService(session)
    chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 12})
    current = chaptering.preview(PROJECT_ID, {"strategy": "keep_current"})
    payload = _payload(current)
    payload["chapters"][0]["summary"] = "他第一次怀疑那本日志被人动过。"
    payload["chapters"][0]["title"] = ""  # 空章名不许落进目录变成章 id
    chaptering.save(PROJECT_ID, payload)
    first = chaptering.chapter_plans(PROJECT_ID)[0]
    assert first.summary == "他第一次怀疑那本日志被人动过。"
    assert first.title == "第 1 章"
