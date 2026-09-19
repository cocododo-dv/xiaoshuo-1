"""2026-09-18 抹空保护：pending_review 步平时原位改写，但「整步抹空」另起一版，旧稿留在历史里可取回。

起因：新浏览器 / 清过缓存的会话里，前端会把从没水合过的空白默认稿 force 上行；pending_review 步是原位改写，
未确认的草稿一旦被空白覆盖就永久丢失。前端同步层有水合闸门，这里是服务端的最后一道。
"""

from __future__ import annotations

from novel_system.services.snowflake_workspace import _would_wipe_story

# 前端空白默认稿上行的形状：规范字段全空，只有身份 / 枚举与 fe_* 写穿键
BLANK_LOGLINE = {"summary": "", "fe_text": "", "fe_scaffold": None, "fe_checks": [], "fe_state": "todo", "fe_t": 1}
BLANK_CHARACTERS = {
    "characters": [{"character_id": "c1", "display_name": "", "role": "主角", "goal": "", "ambition": "", "values": [], "conflict": "", "epiphany": ""}],
    "fe_text": "", "fe_scaffold": {"sel": "c1", "chars": {"c1": {"name": "", "role": "主角"}}}, "fe_state": "todo",
}


def _create_project(client, key: str) -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": "抹空保护之书", "outline_text": "抹空保护验证用项目。"},
        headers={"X-Idempotency-Key": f"wipe-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _patch(client, pid: str, step_key: str, draft: dict) -> dict:
    response = client.patch(f"/api/v2/projects/{pid}/snowflake-workspace/steps/{step_key}", json={"draft": draft, "force": True})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_would_wipe_story_only_fires_on_a_total_wipe() -> None:
    full = {"summary": "她回到雨城，替恩师撒的谎要她自己付账。"}
    assert _would_wipe_story(full, BLANK_LOGLINE)
    assert not _would_wipe_story(full, {"summary": "她回到雨城。"}), "改短不是抹空"
    assert not _would_wipe_story({"summary": "雨"}, BLANK_LOGLINE), "两三个字的试笔被清空不值得多留一版"
    assert not _would_wipe_story(BLANK_LOGLINE, BLANK_LOGLINE)
    sheets = {"characters": [{"character_id": "c1", "display_name": "林晚", "role": "主角", "goal": "在交班前拿到母本"}]}
    assert _would_wipe_story(sheets, BLANK_CHARACTERS), "role / id 这类默认值不算故事文字"
    assert not _would_wipe_story(sheets, {"characters": [{"character_id": "c1", "display_name": "林晚", "role": "主角"}]})


def test_blank_overwrite_of_a_pending_draft_keeps_the_old_version_restorable(client) -> None:
    pid = _create_project(client, "restore")
    first = _patch(client, pid, "one_sentence_summary", {"summary": "她回到雨城，替恩师撒的谎要她自己付账。"})
    assert first["step_run"]["version"] == 1 and first["step"]["status"] == "pending_review"
    # 平常的编辑：原位改写，不造版本
    edited = _patch(client, pid, "one_sentence_summary", {"summary": "她回到雨城，替恩师撒的谎如今要她自己付账。"})
    assert edited["step_run"]["version"] == 1
    assert edited["step_run"]["step_run_id"] == first["step_run"]["step_run_id"]

    # 空白默认稿覆盖：另起一版，旧稿还在
    wiped = _patch(client, pid, "one_sentence_summary", BLANK_LOGLINE)
    assert wiped["step_run"]["version"] == 2
    assert wiped["step"]["draft"].get("summary", "") == ""
    history = client.get(f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/history?include_draft=true")
    assert history.status_code == 200, history.text
    rows = {row["version"]: row for row in history.json()["data"]["items"]}
    assert set(rows) == {1, 2}
    assert rows[1]["draft"]["summary"] == "她回到雨城，替恩师撒的谎如今要她自己付账。"

    restored = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/restore",
        json={"step_run_id": rows[1]["step_run_id"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["step"]["draft"]["summary"] == "她回到雨城，替恩师撒的谎如今要她自己付账。"
    assert restored.json()["data"]["step_run"]["version"] == 3

    # 空白 → 空白不再多造版本
    again = _patch(client, pid, "book_brief", {"category": "", "fe_state": "todo"})
    assert again["step_run"]["version"] == 1
    same = _patch(client, pid, "book_brief", {"category": "", "fe_state": "todo", "fe_t": 2})
    assert same["step_run"]["version"] == 1
