"""阶段 Z（2026-09-20）「一张章表、两扇门」：章节编排接上构思的分章。

作者的原话：「章节编排功能和雪花的场景以及整理为章节结构有点孤立。」真实项目上对出来的：

1. 章节编排里给雪花的章改名，改的是目录里的**另一份**——分章面板和 09 的章头还挂着旧名，「AI 起章名」会给
   作者已经起过名的章再起一遍；反过来 07 里改的章名要等下一次「确认写入」才到得了目录；
2. 章节编排能把雪花的章拖去别的位置 / 别的卷——目录的章序从此不是故事的顺序，下一次确认写入再悄悄排回去；
3. 台子上看不出一章装着故事序上的第几到第几场：构思里说「第 6–12 场」，到了章节编排只剩 01…07。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
)
from tests.test_catalog_book_spine import _catalog, _materialized, _preview
from tests.test_snowflake_chaptering import _patch
from tests.test_snowflake_chaptering_story_order import _confirm


def _rename(client, project_id: str, chapter_id: str, title: str, key: str):
    return client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        json={"title": title},
        headers={"X-Idempotency-Key": key},
    )


def _hand_made_chapter(client, project_id: str, title: str, key: str) -> dict:
    created = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters",
        json={"title": title},
        headers={"X-Idempotency-Key": key},
    )
    assert created.status_code == 200, created.text
    return created.json()["data"]["chapter"]


def _reorder(client, project_id: str, chapter_ids: list[str], key: str):
    return client.post(
        f"/api/v2/projects/{project_id}/catalog/chapter-order",
        json={"chapter_ids": chapter_ids},
        headers={"X-Idempotency-Key": key},
    )


def _plan_row(session, project_id: str, catalog_chapter_id: str) -> SnowflakeChapterPlan:
    session.expire_all()
    return session.execute(
        select(SnowflakeChapterPlan).where(
            SnowflakeChapterPlan.project_id == project_id,
            SnowflakeChapterPlan.catalog_chapter_id == catalog_chapter_id,
            SnowflakeChapterPlan.removed_at.is_(None),
        )
    ).scalars().one()


def _long_synopsis_draft(session, project_id: str) -> dict:
    session.expire_all()
    run = session.execute(
        select(SnowflakeStepRun)
        .where(
            SnowflakeStepRun.project_id == project_id,
            SnowflakeStepRun.step_key == "long_synopsis",
            SnowflakeStepRun.status != "superseded",
        )
        .order_by(SnowflakeStepRun.version.desc())
    ).scalars().first()
    return dict(run.draft_json or {})


# ------------------------------------------------------------------ 1. 目录说得出「这一章是章表里的哪一行」


def test_the_catalog_says_which_chapters_the_plan_owns_and_which_scenes_they_hold(client, session) -> None:
    project_id = _materialized(client, "z-structure")
    hand_made = _hand_made_chapter(client, project_id, "楔子 · 旧信", "z-structure-hand")

    chapters = _catalog(client, project_id)
    planned = [chapter for chapter in chapters if chapter["origin"] == "snowflake"]
    assert planned and all(chapter["structure"]["owner"] == "plan" for chapter in planned)
    rows = {row.row_uid for row in session.execute(
        select(SnowflakeChapterPlan).where(
            SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None)
        )
    ).scalars()}
    assert {chapter["structure"]["row_uid"] for chapter in planned} == rows

    # 章是故事序上连续的一段：第一章从第 1 场起，一章接着一章，编号与分章面板的 story_index 同一套
    expected_first = 1
    for chapter in planned:
        span = chapter["structure"]["scene_range"]
        assert span["first"] == expected_first
        assert span["last"] - span["first"] + 1 == chapter["structure"]["planned_scene_count"] == len(chapter["scenes"])
        assert [scene["design"]["story_index"] for scene in chapter["scenes"]] == list(range(span["first"], span["last"] + 1))
        expected_first = span["last"] + 1
    panel = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview", json={"strategy": "keep_current"}
    ).json()["data"]
    by_scene = {scene["scene_id"]: scene["story_index"] for chapter in panel["chapters"] for scene in chapter["scenes"]}
    for chapter in planned:
        for scene in chapter["scenes"]:
            assert scene["design"]["story_index"] == by_scene[scene["scene_id"]]

    mine = next(chapter for chapter in chapters if chapter["chapter_id"] == hand_made["chapter_id"])
    assert mine["structure"] == {
        "owner": "desk", "row_uid": "", "scene_range": None, "planned_scene_count": 0, "title_auto": False,
    }
    assert [scene["design"]["story_index"] for scene in mine["scenes"]] == [0]


def test_a_chapter_whose_plan_row_is_gone_belongs_to_the_desk_again(client, session) -> None:
    project_id = _materialized(client, "z-orphan")
    first = _catalog(client, project_id)[0]
    row = _plan_row(session, project_id, first["chapter_id"])
    row.removed_at = "2026-09-20T00:00:00+00:00"
    session.commit()

    assert _catalog(client, project_id)[0]["structure"]["owner"] == "desk"


# ------------------------------------------------------------------ 2. 章名只有一个


def test_renaming_a_plan_chapter_at_the_desk_renames_it_in_the_plan_too(client, session) -> None:
    project_id = _materialized(client, "z-rename")
    target = _catalog(client, project_id)[1]
    chapter_id = target["chapter_id"]

    response = _rename(client, project_id, chapter_id, "旧案重开", "z-rename-1")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["plan_title_synced"] is True
    assert data["chapter"]["title"] == "旧案重开" and data["chapter"]["structure"]["title_auto"] is False

    row = _plan_row(session, project_id, chapter_id)
    assert row.title == "旧案重开"
    stamps = {
        plan.chapter_title
        for plan in session.execute(
            select(SnowflakeScenePlan).where(SnowflakeScenePlan.chapter_plan_id == row.chapter_plan_id)
        ).scalars()
    }
    assert stamps == {"旧案重开"}, "09 的章头读的是场景行上的章名戳"
    draft = _long_synopsis_draft(session, project_id)
    assert "旧案重开" in [item["title"] for item in draft["chapters"]], "07 的章节表是章表的镜像"
    catalog_row = session.get(ChapterGoal, chapter_id)
    assert (catalog_row.writer_brief_json or {})["chapter_title"] == "旧案重开"
    assert session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_chapter_title_adopted")
    ).scalars().one().payload_json["to"] == "旧案重开"

    # 分章面板摆出来的就是这个名字
    panel = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview", json={"strategy": "keep_current"}
    ).json()["data"]
    assert "旧案重开" in [chapter["title"] for chapter in panel["chapters"]]

    # 之后在分章面板里再改名 → 目录跟着走（目录里的名字就是上一次播下去的那个，不会被当成「作者在台子上另起的名」）
    renamed = {**panel, "chapters": [
        {**chapter, "title": "码头对质"} if chapter["title"] == "旧案重开" else chapter for chapter in panel["chapters"]
    ]}
    _confirm(client, project_id, renamed, "z-rename-2")
    assert "码头对质" in [chapter["title"] for chapter in _catalog(client, project_id)]
    assert "旧案重开" not in [chapter["title"] for chapter in _catalog(client, project_id)]


def test_an_unchanged_title_echo_and_a_hand_made_chapter_never_touch_the_plan(client, session) -> None:
    project_id = _materialized(client, "z-echo")
    target = _catalog(client, project_id)[0]
    echo = _rename(client, project_id, target["chapter_id"], target["title"], "z-echo-1")
    assert echo.status_code == 200 and echo.json()["data"]["plan_title_synced"] is False

    hand_made = _hand_made_chapter(client, project_id, "楔子", "z-echo-hand")
    renamed = _rename(client, project_id, hand_made["chapter_id"], "楔子 · 旧信", "z-echo-2")
    assert renamed.status_code == 200 and renamed.json()["data"]["plan_title_synced"] is False
    assert not session.execute(
        select(OperationLog).where(OperationLog.event_type == "snowflake_chapter_title_adopted")
    ).scalars().all()


def test_a_chapter_renamed_in_step_07_is_renamed_in_the_catalog_and_on_the_scene_rows(client, session) -> None:
    project_id = _materialized(client, "z-step07")
    chapters = _catalog(client, project_id)
    target, other = chapters[0], chapters[1]
    # 另一章：目录里的名字不是章表播下去的（阶段 Z 之前在台子上改过名、没有写穿的旧数据）——07 不去盖它
    legacy = session.get(ChapterGoal, other["chapter_id"])
    legacy.narrative_json = {**dict(legacy.narrative_json or {}), "title": "作者在台子上起的名"}
    session.commit()

    draft = _long_synopsis_draft(session, project_id)
    table = [dict(item) for item in draft["chapters"]]
    by_uid = {item["row_uid"]: item for item in table}
    by_uid[target["structure"]["row_uid"]]["title"] = "雨城的清晨"
    by_uid[other["structure"]["row_uid"]]["title"] = "07 里另起的名"
    _patch(client, project_id, "long_synopsis", {**draft, "chapters": table})

    after = {chapter["chapter_id"]: chapter for chapter in _catalog(client, project_id)}
    assert after[target["chapter_id"]]["title"] == "雨城的清晨"
    assert after[other["chapter_id"]]["title"] == "作者在台子上起的名"
    row = _plan_row(session, project_id, target["chapter_id"])
    assert {
        plan.chapter_title
        for plan in session.execute(
            select(SnowflakeScenePlan).where(SnowflakeScenePlan.chapter_plan_id == row.chapter_plan_id)
        ).scalars()
    } == {"雨城的清晨"}


# ------------------------------------------------------------------ 3. 章的先后与幕只有一处可改


def test_plan_chapters_keep_their_relative_order_and_act_at_the_desk(client, session) -> None:
    project_id = _materialized(client, "z-order")
    hand_made = _hand_made_chapter(client, project_id, "番外 · 多年以后", "z-order-hand")
    ids = [chapter["chapter_id"] for chapter in _catalog(client, project_id)]
    assert ids[-1] == hand_made["chapter_id"] and len(ids) >= 3

    swapped = [ids[1], ids[0], *ids[2:]]
    refused = _reorder(client, project_id, swapped, "z-order-1")
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "CATALOG_CHAPTER_ORDER_OWNED_BY_PLAN"
    assert [chapter["chapter_id"] for chapter in _catalog(client, project_id)] == ids

    # 手建的章可以挪到任何两章之间：雪花的章彼此的先后没变
    between = [ids[0], hand_made["chapter_id"], *ids[1:-1]]
    moved = _reorder(client, project_id, between, "z-order-2")
    assert moved.status_code == 200, moved.text
    assert [chapter["chapter_id"] for chapter in _catalog(client, project_id)] == between

    first = _catalog(client, project_id)[0]
    other_act = "act3" if first["act"] != "act3" else "act1"
    act = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{first['chapter_id']}",
        json={"act": other_act}, headers={"X-Idempotency-Key": "z-order-3"},
    )
    assert act.status_code == 409 and act.json()["error"]["code"] == "CATALOG_CHAPTER_STRUCTURE_OWNED_BY_PLAN"
    # 值没变的整卡回写照常通过；手建的章照常换卷
    same = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{first['chapter_id']}",
        json={"act": first["act"], "promise": "读者知道旧信是谁寄的"}, headers={"X-Idempotency-Key": "z-order-4"},
    )
    assert same.status_code == 200, same.text
    mine = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{hand_made['chapter_id']}",
        json={"act": "act3"}, headers={"X-Idempotency-Key": "z-order-5"},
    )
    assert mine.status_code == 200 and mine.json()["data"]["chapter"]["act"] == "act3"


def test_the_panel_still_decides_the_order_after_the_desk_guard(client, session) -> None:
    """守的是台面；分章面板重新分章照常能改章序（确认写入走的是 settle_chapter_order，不经目录的 chapter-order）。"""
    project_id = _materialized(client, "z-panel")
    before = [chapter["chapter_id"] for chapter in _catalog(client, project_id)]
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=4), "z-panel-2")
    after = _catalog(client, project_id)
    assert len(after) != len(before), "换一个每章场数，章表真的变了"
    assert all(chapter["structure"]["owner"] == "plan" for chapter in after)
    firsts = [chapter["structure"]["scene_range"]["first"] for chapter in after]
    assert firsts == sorted(firsts) and firsts[0] == 1, "目录章序 = 章表顺序 = 故事序"
