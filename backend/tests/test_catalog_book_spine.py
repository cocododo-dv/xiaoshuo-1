"""阶段 X（2026-09-19）「一条书脊」：雪花整理出来的章必须真的长在目录里，台子读到的是同一份东西。

作者的原话：「雪花生成的章节感觉是孤立的，没有同步到 AI 起草台和写作台」。真实项目上一条一条对出来的原因：

1. 物化把幕写成整数 1 / 2 / 3，章节编排按 ``act === "act1"`` 分卷 → 雪花的章在编排台上一张都不显示；
2. 目录里躺着一章手建的空白占位「第 1 章 / 开场」→ 雪花的章只能排在它后面（编号整体错位、两章同名），
   写作台默认落在那张空白场上；
3. 场景 slug 是位置式的（``ch02s1``），前端按场景落地的本机状态全拿它当身份 → 目录一动，身份就错位；
4. 目录只把三拍交给台子，坩埚 / 地点 / 时间 / 情绪 / 钩子 / 章摘要都留在雪花那一侧；
5. 「现在该写哪一场」三处三条规则；重新物化还会把作者的书签拽回第 1 章；
6. 构思确认之后场景卡不跟，要作者再去点一次「同步到目录」；
7. 已保存的分章可以不是连续切片（第 4 章 = 第 4、10–13 场），面板照样原样摆出来、照样能存；
8. 空白稿被抄成一段「【章节目标】…【节拍】…」脚手架塞进正文。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    OperationLog,
    SceneCard,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    StoryProject,
)
from novel_system.services.catalog import focus_scene_payload, normalize_act, short_scene_title
from novel_system.services.catalog_placeholders import AUTO_TRASHED_PLACEHOLDER_CHAPTER
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService, misplaced_scene_plan_ids
from tests.test_snowflake_chaptering import (
    _approve,
    _create_project,
    _detail,
    _pass_triage,
    _patch,
    _seed,
)
from tests.test_snowflake_chaptering_story_order import _confirm, _payload


def _base(project_id: str) -> str:
    return f"/api/v2/projects/{project_id}/snowflake-workspace"


def _preview(client, project_id: str, **body) -> dict:
    response = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "from_scenes", **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _catalog(client, project_id: str) -> list[dict]:
    response = client.get(f"/api/v2/projects/{project_id}/catalog")
    assert response.status_code == 200, response.text
    return response.json()["data"]["chapters"]


def _scene_ids(client, project_id: str) -> list[str]:
    """目录里的场景 id，按全书顺序（场景 id 由 row_uid 铸，不含位置——测试不去猜它的形状）。"""
    return [scene["scene_id"] for chapter in _catalog(client, project_id) for scene in chapter["scenes"]]


def _materialized(client, key: str, **preview_body) -> str:
    project_id = _create_project(client, key)
    _seed(client, project_id)
    _pass_triage(client, project_id)
    _confirm(client, project_id, _preview(client, project_id, **preview_body), key)
    return project_id


def _placeholder_chapter(client, project_id: str, title: str = "第 1 章") -> dict:
    created = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters", json={"title": title},
        headers={"X-Idempotency-Key": f"placeholder-{project_id}-{len(title)}"},
    )
    assert created.status_code == 200, created.text
    return created.json()["data"]["chapter"]


# ------------------------------------------------------------------ 1. 幕


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, "act1"), ("2", "act2"), ("act3", "act3"), (None, "act1"), ("第二幕", "act2"), (9, "act1"), ("", "act1")],
)
def test_the_catalog_only_speaks_act1_act2_act3(raw, expected) -> None:
    assert normalize_act(raw) == expected


def test_snowflake_chapters_land_in_the_acts_the_arrangement_board_groups_by(client, session) -> None:
    project_id = _materialized(client, "spine-acts")
    chapters = _catalog(client, project_id)
    assert {chapter["act"] for chapter in chapters} <= {"act1", "act2", "act3"}
    assert len({chapter["act"] for chapter in chapters}) > 1, "三幕的章不该全挤在第一幕"
    stored = session.execute(select(ChapterGoal).where(ChapterGoal.project_id == project_id)).scalars().all()
    assert all(str((row.narrative_json or {}).get("act")).startswith("act") for row in stored)

    # 旧数据：物化曾把幕写成整数。读取时归一，绝不让一章从看板上消失
    legacy = stored[-1]
    legacy.narrative_json = {**dict(legacy.narrative_json or {}), "act": 3}
    session.commit()
    assert _catalog(client, project_id)[-1]["act"] == "act3"


# ------------------------------------------------------------------ 2. 空白占位章


def test_a_pristine_placeholder_chapter_steps_aside_for_the_snowflake_chapters(client, session) -> None:
    project_id = _create_project(client, "spine-placeholder")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    placeholder = _placeholder_chapter(client, project_id)
    # 作者进过一次写作台：ensure 给占位场建了一份一个字没动的空白稿（旧数据里是一段脚手架）
    scene_id = placeholder["scenes"][0]["scene_id"]
    assert client.post(f"/api/v1/author-drafts/scene/{scene_id}/ensure").status_code == 200
    draft = session.execute(select(AuthorDraft).where(AuthorDraft.object_id == scene_id)).scalars().one()
    draft.content = "【章节目标】第 1 章\n【场景目标】开场"
    session.commit()

    preview = _preview(client, project_id)
    kinds = [warning["kind"] for warning in preview["warnings"]]
    assert "catalog_placeholder_chapters" in kinds and "catalog_hand_made_chapters" not in kinds

    result = _confirm(client, project_id, preview, "spine-placeholder")
    assert [item["chapter_id"] for item in result["trashed_placeholder_chapters"]] == [placeholder["chapter_id"]]

    chapters = _catalog(client, project_id)
    assert [chapter["no"] for chapter in chapters] == [f"{i:02d}" for i in range(1, len(chapters) + 1)]
    assert chapters[0]["chapter_id"] == f"{project_id}_CH01" and chapters[0]["current"] is True
    assert all(chapter["origin"] == "snowflake" for chapter in chapters)

    session.expire_all()
    row = session.get(ChapterGoal, placeholder["chapter_id"])
    assert row.trashed_flag == 1 and row.trashed_by == AUTO_TRASHED_PLACEHOLDER_CHAPTER
    assert session.get(SceneCard, scene_id).trashed_at == row.trashed_at, "卡和章同一个时间戳进回收站，恢复时一起回来"
    assert session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_placeholder_chapter_trashed")
    ).scalars().one().object_ref == placeholder["chapter_id"]

    # 回收站里取得回来，取回后不撞章序
    restored = client.post(f"/api/v2/trash/chapter:{placeholder['chapter_id']}/restore", json={})
    assert restored.status_code == 200, restored.text
    orders = [chapter["no"] for chapter in _catalog(client, project_id)]
    assert len(orders) == len(chapters) + 1 and len(set(orders)) == len(orders)


@pytest.mark.parametrize("touch", ["named", "words", "beat", "second_revision"])
def test_a_chapter_the_author_touched_is_never_moved(client, session, touch: str) -> None:
    project_id = _create_project(client, f"spine-touched-{touch}")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    chapter = _placeholder_chapter(client, project_id, "楔子" if touch == "named" else "第 1 章")
    scene_id = chapter["scenes"][0]["scene_id"]
    if touch == "words":
        session.get(SceneCard, scene_id).words_current = 12
    elif touch == "beat":
        patched = client.patch(f"/api/v2/projects/{project_id}/catalog/scenes/{scene_id}", json={"brief": {"conflict": "门锁着"}})
        assert patched.status_code == 200, patched.text
    elif touch == "second_revision":
        assert client.post(f"/api/v1/author-drafts/scene/{scene_id}/ensure").status_code == 200
        draft = session.execute(select(AuthorDraft).where(AuthorDraft.object_id == scene_id)).scalars().one()
        draft.content, draft.revision_no = "雨下了一夜。", 2
    session.commit()

    result = _confirm(client, project_id, _preview(client, project_id), f"spine-touched-{touch}")
    assert result["trashed_placeholder_chapters"] == []
    chapters = _catalog(client, project_id)
    assert chapters[0]["chapter_id"] == chapter["chapter_id"] and chapters[0]["origin"] == "manual"


def test_rematerializing_keeps_the_authors_bookmark(client, session) -> None:
    project_id = _materialized(client, "spine-bookmark", scenes_per_chapter=3)
    third = f"{project_id}_CH03"
    moved = client.patch(f"/api/v2/projects/{project_id}/catalog/chapters/{third}", json={"current": True})
    assert moved.status_code == 200, moved.text

    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=4), "spine-bookmark-2")
    session.expire_all()
    assert session.get(StoryProject, project_id).current_chapter_id == third, "重新物化不得把书签拽回第 1 章"

    # 书签指着的章不在目录里了（重新分章后空了、进了回收站）→ 才回到这一版的第一章
    project = session.get(StoryProject, project_id)
    project.current_chapter_id = f"{project_id}_CH09"
    session.commit()
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=6), "spine-bookmark-3")
    session.expire_all()
    assert session.get(StoryProject, project_id).current_chapter_id == f"{project_id}_CH01"


# ------------------------------------------------------------------ 3. 场景身份


def test_scene_slugs_follow_the_row_not_the_position(client, session) -> None:
    project_id = _materialized(client, "spine-slug", scenes_per_chapter=3)
    before = {scene["scene_id"]: scene for chapter in _catalog(client, project_id) for scene in chapter["scenes"]}
    assert all(scene["slug"] == scene_id for scene_id, scene in before.items())
    assert before[_scene_ids(client, project_id)[0]]["legacy_slug"] == "ch01s1"

    # 重新分章：场景卡跨章搬动，位置式旧 slug 全变了，身份一个字都不变
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=5), "spine-slug-2")
    after = {scene["scene_id"]: scene for chapter in _catalog(client, project_id) for scene in chapter["scenes"]}
    assert set(after) == set(before)
    assert all(after[scene_id]["slug"] == scene_id for scene_id in before)
    assert any(after[scene_id]["legacy_slug"] != before[scene_id]["legacy_slug"] for scene_id in before)


# ------------------------------------------------------------------ 4. 设计卡随目录到达台子


def test_short_titles_come_from_the_first_clause() -> None:
    assert short_scene_title("开场") == "开场"
    assert short_scene_title("她推开档案馆的侧门，值班表上那一页已经被人撕走了一半还多") == "她推开档案馆的侧门"
    long_clause = "林昭沿着雨城旧码头一路追问当年值班的每一个搬运工人"
    assert short_scene_title(f"{long_clause}，却没有人肯开口") == f"{long_clause[:17]}…"


def test_the_catalog_carries_the_whole_design_card(client, session) -> None:
    project_id = _create_project(client, "spine-design")
    _seed(client, project_id)
    details = [_detail(f"S{i:02d}", i, f"事件{i}") for i in range(1, 13)]
    details[0].update({
        "title": "雨夜来信", "summary": "一封没有寄信人的信把她拉回雨城，信封里只有一张二十年前的车票。",
        "story_time": "第一夜 · 23:40", "expected_reader_emotion": "不安", "hook": "车票背面有她母亲的字迹。",
        "exit_change": "她决定回去", "must_include_text": "你欠这座城一个交代。", "target_length_band": "1200-1500",
        "reaction": "她盯着车票看了很久", "dilemma": "回去，还是装作没看见", "decision": "订了最早的一班车",
    })
    _patch(client, project_id, "scene_details", {"scenes": details})
    _approve(client, project_id, "scene_details")
    _pass_triage(client, project_id)
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "spine-design")

    chapter = _catalog(client, project_id)[0]
    assert chapter["origin"] == "snowflake" and chapter["summary"] and "spine" in chapter
    scene = chapter["scenes"][0]
    assert scene["title"] == "雨夜来信", "构思里起过的短题名随卡进目录"
    assert scene["summary"].startswith("一封没有寄信人的信")
    design = scene["design"]
    assert design["origin"] == "snowflake"
    assert design["crucible"] == "她不能就这样走开" and design["location"] == "雨城"
    assert design["story_time"] == "第一夜 · 23:40" and design["reader_emotion"] == "不安"
    assert design["must_include"] == "你欠这座城一个交代。" and design["length_band"] == "1200-1500"
    assert design["cost"] == "事件1·代价" and design["rendering_mode"] == "full"
    assert design["followup"] == {
        "reaction": "她盯着车票看了很久", "dilemma": "回去，还是装作没看见", "decision": "订了最早的一班车",
    }, "主动场接着的那组反应三拍（阶段 I）也到得了台子"
    assert scene["hook"] == "车票背面有她母亲的字迹。" and scene["exit_change"] == "她决定回去"
    assert scene["pov_character_name"] == "林昭"
    assert scene["work"] == {"run_status": "ready", "has_final": False, "has_words": False}

    # 没起过题名的场：整句摘要不当题名用，取一个短题；整句另给
    untitled = _catalog(client, project_id)[0]["scenes"][1]
    assert untitled["title"] == "事件2" and untitled["summary"] == "事件2"


def test_a_desk_rename_survives_resync_and_rematerialization(client, session) -> None:
    project_id = _materialized(client, "spine-rename", scenes_per_chapter=3)
    scene_id = _scene_ids(client, project_id)[0]
    renamed = client.patch(f"/api/v2/projects/{project_id}/catalog/scenes/{scene_id}", json={"title": "信"})
    assert renamed.status_code == 200, renamed.text

    resync = client.post(f"{_base(project_id)}/resync", json={"scene_ids": [scene_id]})
    assert resync.status_code == 200, resync.text
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=4), "spine-rename-2")
    titles = {scene["scene_id"]: scene["title"] for chapter in _catalog(client, project_id) for scene in chapter["scenes"]}
    assert titles[scene_id] == "信"


# ------------------------------------------------------------------ 5. 现在该写哪一场


def test_one_rule_for_the_scene_to_work_on_next() -> None:
    scenes = [{"state": "done"}, {"state": "todo", "n": 2}, {"state": "writing", "n": 3}, {"state": "todo"}]
    assert focus_scene_payload(scenes)["n"] == 3, "在写的那一场"
    assert focus_scene_payload([{"state": "done"}, {"state": "todo", "n": 2}, {"state": "todo"}])["n"] == 2
    assert focus_scene_payload([{"state": "done", "n": 1}, {"state": "done", "n": 2}])["n"] == 2
    assert focus_scene_payload([]) is None


def test_home_resumes_at_the_first_unwritten_scene_not_the_last(client, session) -> None:
    project_id = _materialized(client, "spine-resume", scenes_per_chapter=4)
    resume = client.get(f"/api/v2/projects/{project_id}/dashboard").json()["data"]["resume"]
    assert resume["chapter_no"] == "01" and resume["scene_no"] == 1
    assert resume["scene_slug"] == _scene_ids(client, project_id)[0]


# ------------------------------------------------------------------ 6. 确认即同步


def _edit_scene_details(client, project_id: str, changes: dict[int, dict]) -> None:
    details = [_detail(f"S{i:02d}", i, f"事件{i}") for i in range(1, 13)]
    for index, patch in changes.items():
        details[index - 1].update(patch)
    _patch(client, project_id, "scene_details", {"scenes": details})


def _approve_with_sync(client, project_id: str, step_key: str) -> dict:
    response = client.post(f"{_base(project_id)}/steps/{step_key}/approve", json={"sync_catalog": True})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_confirming_a_step_brings_the_scene_cards_along(client, session) -> None:
    project_id = _materialized(client, "spine-autosync", scenes_per_chapter=3)
    _edit_scene_details(client, project_id, {2: {"goal": "撬开档案柜"}})

    status = client.get(f"{_base(project_id)}/resync-status").json()["data"]
    assert status["pending_count"] == 1
    assert status["pending_scenes"][0]["plan_status"] == "draft", "还在改的规划——台子不为它喊「待同步」"

    data = _approve_with_sync(client, project_id, "scene_details")
    assert data["catalog_sync"]["synced_count"] == 1 and data["catalog_sync"]["held"] == []
    assert data["workspace"]["resync_status"]["pending_count"] == 0
    scene = _catalog(client, project_id)[0]["scenes"][1]
    assert scene["brief"]["goal"] == "撬开档案柜"
    log = session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_scene_resynced")
    ).scalars().all()
    assert log and str(log[-1].payload_json["actor_ref"]).startswith("auto_sync:")


def test_confirming_a_design_change_does_not_fake_a_failed_run_for_a_scene_that_never_ran(client, session) -> None:
    """确认即同步让「改设计 → 确认」变成常态：从没进过管线的场不该因此变成起草台上的失败稿、待办里的一张卡。"""
    from novel_system.db.models import SceneRunState

    project_id = _materialized(client, "spine-neverran", scenes_per_chapter=3)
    _edit_scene_details(client, project_id, {2: {"goal": "撬开档案柜"}})
    data = _approve_with_sync(client, project_id, "scene_details")
    assert data["catalog_sync"]["synced_count"] == 1
    scene_id = _scene_ids(client, project_id)[1]
    session.expire_all()
    assert session.get(SceneRunState, scene_id).scene_status == "ready"
    assert session.get(StoryProject, project_id).status == "chapter_ready"
    states = client.get(f"/api/v1/scene-run-states?project_id={project_id}").json()["data"]["items"]
    assert states == [], "起草台从这里恢复「在办」的场：没跑过的场不在其中"
    card = _catalog(client, project_id)[0]["scenes"][1]
    assert card["work"]["run_status"] == "ready" and card["brief"]["goal"] == "撬开档案柜"


def test_without_the_flag_confirming_a_step_leaves_the_cards_alone(client, session) -> None:
    project_id = _materialized(client, "spine-noflag", scenes_per_chapter=3)
    _edit_scene_details(client, project_id, {2: {"goal": "撬开档案柜"}})
    _approve(client, project_id, "scene_details")
    status = client.get(f"{_base(project_id)}/resync-status").json()["data"]
    assert status["pending_count"] == 1 and status["pending_scenes"][0]["plan_status"] == "approved"
    assert _catalog(client, project_id)[0]["scenes"][1]["brief"]["goal"] == "事件2·目标"


def test_a_card_the_author_edited_at_a_desk_is_held_for_the_diff_preview(client, session) -> None:
    project_id = _materialized(client, "spine-deskedit", scenes_per_chapter=3)
    scene_id = _scene_ids(client, project_id)[1]
    edited = client.patch(
        f"/api/v2/projects/{project_id}/catalog/scenes/{scene_id}", json={"brief": {"goal": "我在编排台改的目标"}}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"]["scene"]["design"]["desk_edited"] is True

    _edit_scene_details(client, project_id, {2: {"conflict": "柜门焊死了"}, 3: {"goal": "追上去"}})
    data = _approve_with_sync(client, project_id, "scene_details")
    assert data["catalog_sync"]["synced_count"] == 1
    assert [(item["scene_id"], item["reason"]) for item in data["catalog_sync"]["held"]] == [(scene_id, "desk_edited")]
    scenes = _catalog(client, project_id)[0]["scenes"]
    assert scenes[1]["brief"]["goal"] == "我在编排台改的目标", "自动回流不得静默盖掉台面上的改动"
    assert scenes[2]["brief"]["goal"] == "追上去"

    # 改状态、改题名不算改设计；换 POV、改钩子算
    third = _scene_ids(client, project_id)[2]
    for body, expected in (({"state": "writing"}, False), ({"title": "追"}, False), ({"hook": "门外有脚步声"}, True)):
        patched = client.patch(f"/api/v2/projects/{project_id}/catalog/scenes/{third}", json=body)
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["scene"]["design"]["desk_edited"] is expected, body

    # 作者看过差异、显式回流之后，这张卡与构思一致，记号清掉
    assert client.post(f"{_base(project_id)}/resync", json={"scene_ids": [scene_id]}).status_code == 200
    card = _catalog(client, project_id)[0]["scenes"][1]
    assert card["design"]["desk_edited"] is False and card["brief"]["conflict"] == "柜门焊死了"


def test_a_written_scene_is_never_auto_trashed(client, session) -> None:
    project_id = _materialized(client, "spine-written", scenes_per_chapter=3)
    scene_id = _scene_ids(client, project_id)[1]
    session.get(SceneCard, scene_id).words_current = 800
    session.commit()
    plan = session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.scene_id == scene_id)
    ).scalars().one()
    verdict = client.post(
        f"{_base(project_id)}/scene-triage", json={"items": [{"scene_plan_id": plan.scene_plan_id, "status": "cut"}]}
    )
    assert verdict.status_code == 200, verdict.text

    data = _approve_with_sync(client, project_id, "scene_details")
    assert [(item["scene_id"], item["reason"]) for item in data["catalog_sync"]["held"]] == [
        (scene_id, "would_trash_written_scene")
    ]
    session.expire_all()
    assert session.get(SceneCard, scene_id).trashed_flag == 0


def test_the_desks_may_ask_about_any_work_without_tripping_an_error(client) -> None:
    """写作台 / AI 起草台对每部作品都会问「有没有待同步」；不是雪花法的作品如实回答，而不是 409。"""
    created = client.post(
        "/api/v1/projects", json={"title": "手写的书", "outline_text": "第一章\n第二章"},
        headers={"X-Idempotency-Key": "spine-plain-project"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["data"]["project"]["project_id"]
    response = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace/resync-status")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "supported": False, "pending_count": 0, "pending_scene_plan_ids": [], "pending_scenes": [],
    }


def test_a_card_bound_for_a_chapter_not_yet_in_the_catalog_still_gets_its_content(client, session) -> None:
    project_id = _materialized(client, "spine-unmoved", scenes_per_chapter=6)
    scene_id = _scene_ids(client, project_id)[1]
    # 构思侧重新分了章（只落分章、没有「整理为章节结构」）：第 2 场的目标章目录里还没有
    plan = session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.scene_id == scene_id)).scalars().one()
    plan.chapter_id = f"{project_id}_CH09"
    session.commit()
    _edit_scene_details(client, project_id, {2: {"goal": "撬开档案柜"}})

    data = _approve_with_sync(client, project_id, "scene_details")
    assert data["catalog_sync"]["held"] == []
    assert data["catalog_sync"]["notice"]["code"] == "CHAPTER_MOVE_NEEDS_MATERIALIZE"
    card = next(s for c in _catalog(client, project_id) for s in c["scenes"] if s["scene_id"] == scene_id)
    assert card["brief"]["goal"] == "撬开档案柜" and card["chapter_id"] == f"{project_id}_CH01"


# ------------------------------------------------------------------ 7. 章是故事序上连续的一段


def _scramble(session, project_id: str) -> list[str]:
    """阶段 V 之前留下的那种归属：两章的场交错洗在一起。"""
    service = SnowflakeChapteringService(session)
    chapters = service.chapter_plans(project_id)
    scenes = service.scene_plans(project_id)
    for index, plan in enumerate(scenes):
        plan.chapter_plan_id = chapters[index % 2].chapter_plan_id
    session.commit()
    return [plan.scene_plan_id for plan in scenes]


def test_a_scrambled_saved_chaptering_is_not_offered_back_as_is(client, session) -> None:
    project_id = _materialized(client, "spine-scrambled", scenes_per_chapter=6)
    _scramble(session, project_id)
    service = SnowflakeChapteringService(session)
    assert misplaced_scene_plan_ids(service.chapter_plans(project_id), service.scene_plans(project_id))

    auto = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "auto"}).json()["data"]
    assert auto["strategy"] == "from_scenes"
    assert auto["warnings"][0]["kind"] == "chapter_order_healed"
    for chapter in auto["chapters"]:
        indices = [scene["story_index"] for scene in chapter["scenes"]]
        assert indices == list(range(indices[0], indices[0] + len(indices)))

    kept = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]
    assert kept["warnings"][0]["kind"] == "chapter_order_healed"
    flat = [scene["story_index"] for chapter in kept["chapters"] for scene in chapter["scenes"]]
    assert flat == sorted(flat), "面板摆出来的就是会落库的那一版"


def test_save_never_persists_a_chapter_that_is_not_a_contiguous_slice(client, session) -> None:
    project_id = _materialized(client, "spine-save-heal", scenes_per_chapter=6)
    preview = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]
    payload = _payload(preview)
    rows = [chapter["row_uid"] for chapter in preview["chapters"]]
    # 面板不会这么发，API 调用方（或一版本来就交错的旧归属）会：第 1、3、5… 场进第一章，其余进第二章
    payload["assignments"] = [
        {"scene_plan_id": item["scene_plan_id"], "chapter_row_uid": rows[index % 2]}
        for index, item in enumerate(payload["assignments"])
    ]
    saved = client.patch(f"{_base(project_id)}/chapter-plan", json=payload)
    assert saved.status_code == 200, saved.text

    session.expire_all()
    service = SnowflakeChapteringService(session)
    assert misplaced_scene_plan_ids(service.chapter_plans(project_id), service.scene_plans(project_id)) == []
    healed = session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_chapter_plan_saved")
    ).scalars().all()[-1].payload_json["healed_scene_plan_ids"]
    assert healed, "被并回相邻章的场记在操作日志里"
    assert session.execute(select(SnowflakeChapterPlan).where(SnowflakeChapterPlan.project_id == project_id)).scalars().first()


# ------------------------------------------------------------------ 8. 空白稿就是空白


def test_a_blank_scene_draft_is_blank(client, session) -> None:
    project_id = _materialized(client, "spine-blank", scenes_per_chapter=3)
    scene_id = _scene_ids(client, project_id)[0]
    draft = client.post(f"/api/v1/author-drafts/scene/{scene_id}/ensure").json()["data"]["draft"]
    assert draft["content"] == "", "设计卡常驻在正文旁边，不再抄成脚手架塞进正文"
    assert draft["source_text_ref"].endswith(":blank")
