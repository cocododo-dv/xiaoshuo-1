"""R15a（2026-09-30 作者批准 #24a）：构思「历史」页「服务器上保存的版本」的后端前提。

- 恢复一版旧的 07 只恢复它的文字：章表是分章结果的只读镜像（R11），章表行、章名、场景归属保留现表，恢复出来的
  草稿里的章表就是现表（以前把那一版当时的章表同步回来：改过的章名退回、后来的章被软删、场退回「未分章」，回包
  再报一句「章表收缩」，复核 P04-R3）；
- 版本列表不带草稿，预览某一版时按 ``step_run_id`` 只取那一版的草稿；
- 抹空保护新起的那一版记着它保住的是哪一版，界面可以据此一键取回。
"""

from __future__ import annotations

import pytest

from novel_system.db.models import SnowflakeStepRun
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService
from tests.test_snowflake_rendering_mode import PROJECT_ID, _seed


def _chapter(title: str, **extra) -> dict:
    return {"act": 1, "title": title, "summary": f"{title}的摘要", "chapter_goal": f"推进{title}", **extra}


def test_restoring_an_older_07_keeps_the_live_chapter_table(session) -> None:
    """R11（批准 #18a）：07 的章表是分章结果的只读镜像，章表行只有一个写入方（分章面板 / 确认写入）。恢复一版旧的 07
    只恢复它的文字：章表行、章名（分章面板 / 写作台起的名字）、场景归属原样，恢复出来的草稿里的章表是现在这张章表，
    也就没有「章表收缩」可报；那一版的前端写穿缓存里那时候的章表副本去掉（07 的章表只在规范的 chapters 里）。以前
    恢复把那一版当时的章表同步回章表行：改过的章名退回旧名、后来加的章被软删、挂在上面的场退回「未分章」（复核
    P04-R3，主管决定）。"""
    from sqlalchemy import select

    from novel_system.db.models import SnowflakeScenePlan
    from novel_system.services.snowflake_chapter_table import live_chapter_plans
    from tests.test_snowflake_chaptering_story_order import _payload

    service = _seed(session)
    # 一版确认过的旧 07：只有一章，前端写穿缓存里也是那时候的一章（直接落库——示例作品的前六步没有确认）
    old_table = [_chapter("第一章", row_uid="ch-a")]
    session.add(
        SnowflakeStepRun(
            step_run_id="run-07-v1",
            project_id=PROJECT_ID,
            step_key="long_synopsis",
            version=1,
            status="approved",
            draft_json={
                "paragraphs": ["第一段", "", "", "", ""],
                "chapters": old_table,
                "fe_scaffold": {"expansions": {"setup": "第一段"}, "chapters": [{"row_uid": "ch-a", "id": "01", "act": 1, "title": "第一章"}]},
            },
            health_json={},
            input_refs_json={},
        )
    )
    session.flush()
    service.update_step(
        PROJECT_ID,
        "long_synopsis",
        {"draft": {"paragraphs": ["改过的第一段"], "chapters": [*old_table, _chapter("第二章")]}},
    )
    chaptering = SnowflakeChapteringService(session)
    chaptering.autoassign(PROJECT_ID, "even")
    session.flush()
    # 分章面板「只保存章表」给两章起名
    payload = _payload(chaptering.preview(PROJECT_ID, {"strategy": "keep_current"}))
    payload["chapters"][0]["title"] = "旧信回城"
    payload["chapters"][1]["title"] = "雨夜对质"
    service.save_chapter_plan(PROJECT_ID, payload)
    session.flush()

    def table() -> list[tuple[str, str, str]]:
        return [(row.row_uid, row.title, row.status) for row in live_chapter_plans(session, PROJECT_ID)]

    def bindings() -> dict[str, str | None]:
        plans = session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == PROJECT_ID)).scalars()
        return {plan.scene_plan_id: plan.chapter_plan_id for plan in plans}

    before_table, before_bindings = table(), bindings()
    assert [title for _uid, title, _status in before_table] == ["旧信回城", "雨夜对质"]
    assert len(set(before_bindings.values())) == 2 and all(before_bindings.values())

    result = service.restore_step(PROJECT_ID, "long_synopsis", {"step_run_id": "run-07-v1"})

    assert "notice" not in result
    assert "generation_notice" not in result["step"]["health"]
    assert result["step"]["health"]["generation_source"] == "history_restore"
    session.expire_all()
    assert table() == before_table, "恢复旧 07 改动了章表行 / 章名"
    assert bindings() == before_bindings, "恢复旧 07 改动了场景归属"
    restored = session.get(SnowflakeStepRun, result["step_run"]["step_run_id"])
    assert restored.draft_json["paragraphs"][0] == "第一段"  # 文字是那一版的
    assert [(item["row_uid"], item["title"]) for item in restored.draft_json["chapters"]] == [
        (uid, title) for uid, title, _status in before_table
    ]
    assert "chapters" not in restored.draft_json["fe_scaffold"], "那一版写穿缓存里的旧章表副本跟着恢复回来了"
    assert restored.draft_json["fe_scaffold"]["expansions"] == {"setup": "第一段"}


def test_restoring_without_a_shrink_carries_no_notice(session) -> None:
    service = _seed(session)
    service.update_step(PROJECT_ID, "book_brief", {"draft": {"category": "悬疑", "target_reader": "喜欢旧案的读者"}})
    first = service.approve_step(PROJECT_ID, "book_brief")["step"]["artifact"]["step_run_id"]
    service.update_step(PROJECT_ID, "book_brief", {"draft": {"category": "悬疑", "target_reader": "改过的读者"}})

    result = service.restore_step(PROJECT_ID, "book_brief", {"step_run_id": first})

    assert "notice" not in result
    assert "generation_notice" not in result["step"]["health"]


def test_history_lists_versions_without_drafts_and_previews_one_version(session) -> None:
    service = _seed(session)
    service.update_step(PROJECT_ID, "book_brief", {"draft": {"category": "悬疑", "target_reader": "第一版读者"}})
    first = service.approve_step(PROJECT_ID, "book_brief")["step"]["artifact"]["step_run_id"]
    service.update_step(PROJECT_ID, "book_brief", {"draft": {"category": "悬疑", "target_reader": "第二版读者"}})

    listing = service.step_history(PROJECT_ID, "book_brief")
    assert [item["version"] for item in listing["items"]] == [2, 1]
    assert all("draft" not in item for item in listing["items"])

    preview = service.step_history(PROJECT_ID, "book_brief", include_draft=True, step_run_id=first)
    [item] = preview["items"]
    assert item["step_run_id"] == first
    assert item["draft"]["target_reader"] == "第一版读者"

    with pytest.raises(DomainError) as error:
        service.step_history(PROJECT_ID, "scene_list", include_draft=True, step_run_id=first)
    assert error.value.code == "SNOWFLAKE_STEP_RUN_NOT_FOUND"


def test_history_marks_the_version_the_wipe_guard_created(session) -> None:
    service = _seed(session)
    kept = service.update_step(
        PROJECT_ID, "one_sentence_summary", {"draft": {"summary": "她必须查清旧信的来历，但每一步都更贵。"}}
    )["step_run"]["step_run_id"]
    wiped = service.update_step(PROJECT_ID, "one_sentence_summary", {"draft": {"summary": ""}})["step_run"]["step_run_id"]
    assert wiped != kept

    items = {item["step_run_id"]: item for item in service.step_history(PROJECT_ID, "one_sentence_summary")["items"]}
    assert items[wiped]["wipe_guard_preserved_step_run_id"] == kept
    assert items[kept]["wipe_guard_preserved_step_run_id"] is None


def test_history_route_accepts_a_version_filter(client) -> None:
    created = client.post(
        "/api/v2/projects",
        json={"title": "历史预览", "outline_text": "旧信把她带回雨城。"},
        headers={"X-Idempotency-Key": "history-route-create"},
    )
    project_id = created.json()["data"]["project"]["project_id"]
    saved = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/book_brief",
        json={"draft": {"category": "悬疑", "target_reader": "喜欢旧案的读者"}},
    )
    run_id = saved.json()["data"]["step_run"]["step_run_id"]

    response = client.get(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/book_brief/history",
        params={"include_draft": "true", "step_run_id": run_id},
    )
    assert response.status_code == 200, response.text
    [item] = response.json()["data"]["items"]
    assert item["draft"]["target_reader"] == "喜欢旧案的读者"

    missing = client.get(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/book_brief/history",
        params={"step_run_id": "snowflake_step_run_nope"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SNOWFLAKE_STEP_RUN_NOT_FOUND"


@pytest.mark.parametrize(("step_key", "content_field"), [("scene_list", "summary"), ("scene_details", "goal")])
def test_restoring_an_older_09_or_10_keeps_the_live_chapter_packaging(session, step_key, content_field) -> None:
    """复核 Q2b-R2：章的包装（每场归哪一章、章名、章目标）只有一个写入方——分章面板（R11）。旧的 09 / 10 版本的行带着
    当时的包装（前端自动保存存的就是工作台的行），以前恢复时原样同步回现在的每一个场景计划：章表行是新章名 / 新目标，
    场景计划却是旧的，下一次「确认本步」带 sync_catalog 回流时再把旧章目标写进场景卡和目录的章。恢复只换这一步的
    内容（这里换掉一场的一栏：09 的事件、10 的目标），包装保留现在的——与恢复 07 保留现表同一条。"""
    from sqlalchemy import select

    from novel_system.db.models import SnowflakeScenePlan
    from novel_system.services.snowflake_chapter_table import live_chapter_plans

    service = _seed(session)
    chaptering = SnowflakeChapteringService(session)

    def live_plans() -> list[SnowflakeScenePlan]:
        return list(
            session.execute(
                select(SnowflakeScenePlan).where(
                    SnowflakeScenePlan.project_id == PROJECT_ID, SnowflakeScenePlan.removed_at.is_(None)
                )
            ).scalars()
        )

    def chapter_once(row_uid: str, title: str, goal: str) -> None:
        chaptering.save(
            PROJECT_ID,
            {
                "replace_chapters": True,
                "chapters": [{"row_uid": row_uid, "title": title, "act": 1, "chapter_goal": goal}],
                "assignments": [{"scene_plan_id": plan.scene_plan_id, "chapter_row_uid": row_uid} for plan in live_plans()],
            },
        )
        session.flush()

    chapter_once("new:1", "旧章名", "旧目标")
    rows = next(step for step in service.workspace(PROJECT_ID)["steps"] if step["step_key"] == step_key)["draft"]["scenes"]
    assert {(row["chapter_title"], row["chapter_goal"]) for row in rows} == {("旧章名", "旧目标")}
    session.add(
        SnowflakeStepRun(
            step_run_id="run-old",
            project_id=PROJECT_ID,
            step_key=step_key,
            version=0,
            status="superseded",
            draft_json={"scenes": [dict(row, **{content_field: "旧版本里的一栏"}) if row["row_uid"] == "u1" else dict(row) for row in rows]},
            health_json={},
            input_refs_json={},
        )
    )
    session.flush()
    # 之后在分章面板里改了章名与章目标
    [chapter] = live_chapter_plans(session, PROJECT_ID)
    chapter_once(chapter.row_uid, "新章名", "新目标")

    result = service.restore_step(PROJECT_ID, step_key, {"step_run_id": "run-old"})
    session.flush()
    session.expire_all()

    plans = live_plans()
    assert {(plan.chapter_title, plan.chapter_goal) for plan in plans} == {("新章名", "新目标")}
    assert {plan.chapter_plan_id for plan in plans} == {chapter.chapter_plan_id}
    assert getattr(next(plan for plan in plans if plan.row_uid == "u1"), content_field) == "旧版本里的一栏"  # 内容确实恢复了
    assert {(row["chapter_title"], row["chapter_goal"]) for row in result["step"]["draft"]["scenes"]} == {("新章名", "新目标")}
    [chapter] = live_chapter_plans(session, PROJECT_ID)
    assert (chapter.title, chapter.chapter_goal) == ("新章名", "新目标")


def test_restoring_an_older_10_keeps_the_scenes_and_fields_09_owns(session) -> None:
    """复核 Q2b 新发现：哪些场存在、场的事件 / 地点 / 坩埚 / 形态 / 视角都归 09，第 10 步只管它自己那几栏。以前恢复一版
    旧的 10：09 之后删掉的场被复活（又回到 09 的列表、分章面板与物化里），旧的事件 / 地点被盖回现在的场上。现在恢复只换
    10 自己的栏；存下的这一版 10 也照场上的样子写（删掉的场不在里面，09 的那几栏是现在的值）。没刷新的旧标签页的 10
    自动保存走同一条路：带着删掉的场也不复活它。"""
    from sqlalchemy import select

    from novel_system.db.models import SnowflakeScenePlan
    from tests.test_snowflake_rendering_mode import _scene_rows

    service = _seed(session)
    rows = next(st for st in service.workspace(PROJECT_ID)["steps"] if st["step_key"] == "scene_details")["draft"]["scenes"]
    old = [dict(row, summary="旧的摘要", location="旧地点", goal="旧的目标") if row["row_uid"] == "u1" else dict(row) for row in rows]
    session.add(
        SnowflakeStepRun(
            step_run_id="run-10-old", project_id=PROJECT_ID, step_key="scene_details", version=0,
            status="superseded", draft_json={"scenes": old}, health_json={}, input_refs_json={},
        )
    )
    session.flush()
    # 09：那一版之后作者删掉了 u3
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": [r for r in _scene_rows() if r["row_uid"] != "u3"]}})
    session.flush()

    def plans_by_uid(*, live: bool) -> dict[str, SnowflakeScenePlan]:
        query = select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == PROJECT_ID)
        if live:
            query = query.where(SnowflakeScenePlan.removed_at.is_(None))
        return {plan.row_uid: plan for plan in session.execute(query).scalars()}

    result = service.restore_step(PROJECT_ID, "scene_details", {"step_run_id": "run-10-old"})
    session.flush()
    session.expire_all()

    live = plans_by_uid(live=True)
    assert sorted(live) == ["u1", "u2"], "恢复旧的 10 复活了 09 删掉的场"
    assert plans_by_uid(live=False)["u3"].removed_at is not None
    assert (live["u1"].summary, live["u1"].location) == ("取账本", "码头"), "恢复旧的 10 改了 09 的栏"
    assert live["u1"].goal == "旧的目标"  # 10 自己的栏确实恢复了
    restored = session.get(SnowflakeStepRun, result["step_run"]["step_run_id"])
    stored = {row["row_uid"]: row for row in restored.draft_json["scenes"]}
    assert sorted(stored) == ["u1", "u2"]
    assert (stored["u1"]["summary"], stored["u1"]["location"], stored["u1"]["goal"]) == ("取账本", "码头", "旧的目标")

    # 没刷新的旧标签页：10 的自动保存还带着 u3（和它旧的事件）——同样不复活、不改 09 的栏
    stale_tab = [dict(row) for row in old]
    service.update_step(PROJECT_ID, "scene_details", {"draft": {"scenes": stale_tab}})
    session.flush()
    session.expire_all()
    assert sorted(plans_by_uid(live=True)) == ["u1", "u2"]
    assert plans_by_uid(live=True)["u1"].summary == "取账本"
