"""R15a（2026-09-30 作者批准 #24a）：构思「历史」页「服务器上保存的版本」的后端前提。

- 恢复一版旧的 07 可能让章表变短（尾部的章连同场景归属一起没了）：恢复与整步生成同一条路，把「章表收缩」的
  事实挂进健康度，回包也如实带着（以前恢复把它吞掉，作者看到的是一次悄无声息的「恢复成功」）；
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


def test_restoring_an_older_07_reports_the_chapter_table_shrink(session) -> None:
    service = _seed(session)
    # 一版确认过的旧 07：只有一章（直接落库——示例作品的前六步没有确认，走不到 07 的确认闸门）
    session.add(
        SnowflakeStepRun(
            step_run_id="run-07-v1",
            project_id=PROJECT_ID,
            step_key="long_synopsis",
            version=1,
            status="approved",
            draft_json={"paragraphs": ["第一段", "", "", "", ""], "chapters": [_chapter("第一章", row_uid="ch-a")]},
            health_json={},
            input_refs_json={},
        )
    )
    session.flush()
    service.update_step(
        PROJECT_ID,
        "long_synopsis",
        {"draft": {"paragraphs": ["第一段"], "chapters": [_chapter("第一章", row_uid="ch-a"), _chapter("第二章")]}},
    )
    SnowflakeChapteringService(session).autoassign(PROJECT_ID, "even")
    session.flush()

    result = service.restore_step(PROJECT_ID, "long_synopsis", {"step_run_id": "run-07-v1"})

    notice = result["notice"]
    assert notice["code"] == "CHAPTER_PLAN_SHRUNK"
    assert notice["unbound_scene_count"] >= 1
    assert "第二章" in notice["message"]
    assert result["step"]["health"]["generation_notice"]["code"] == "CHAPTER_PLAN_SHRUNK"
    assert result["step"]["health"]["generation_source"] == "history_restore"


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
