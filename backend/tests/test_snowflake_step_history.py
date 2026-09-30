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
    只恢复它的文字：章表行、章名（分章面板 / 写作台起的名字）、场景归属原样，恢复出来的草稿里的章表——连同前端写穿
    缓存里的那一份——是现在这张章表，也就没有「章表收缩」可报。以前恢复把那一版当时的章表同步回章表行：改过的章名
    退回旧名、后来加的章被软删、挂在上面的场退回「未分章」（复核 P04-R3，主管决定）。"""
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
    assert [item["title"] for item in restored.draft_json["fe_scaffold"]["chapters"]] == ["旧信回城", "雨夜对质"]
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
