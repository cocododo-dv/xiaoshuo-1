"""阶段 W（2026-09-19）：分章重做之后留下的三件事。

1. **AI 起章名**：按场景分章出来的章只有占位名「第 N 章」。`chapter-plan/titles` 给占位名各起一个名字、
   写一句章摘要——只读、fail-closed、不碰作者起的名字、分批、过滤模型违约的条目。
2. **重新分章后变空的旧章进回收站**：章号是位置式的，6 章收成 4 章，CH05 / CH06 就是两个空壳；
   之后又分回 6 章时，计划指向的是回收站里的那一行，必须取回，不能把场景卡搬进一个看不见的章。
3. **07 的章表不是 09 的输入**：章是列完场之后的包装决定，只改章表再确认 07，09 不该「需复核」。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from novel_system.db.models import ChapterGoal, SceneCard, SnowflakeStepRun
from novel_system.services.llm_client import LLMResponse
from novel_system.services.projects import AUTO_TRASHED_EMPTY_CHAPTER
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService, is_auto_chapter_title
from novel_system.services.snowflake_staleness import FIELDS_CONSUMED
from novel_system.services.snowflake_workspace_llm import clean_chapter_title
from tests.test_snowflake_chaptering import (
    _CHAPTERS,
    _approve,
    _create_project,
    _install_llm,
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


def _llm(payload_for) -> tuple[list[dict], callable]:
    """按请求里的 chapters 现编回包；返回（收到的提示词载荷列表，responder）。"""
    seen: list[dict] = []

    def responder(request):
        user = next(message["content"] for message in request.messages if message["role"] == "user")
        seen.append({"user": user, "schema": request.response_schema})
        payload = payload_for(user, len(seen))
        return LLMResponse(
            request_id=f"titles-{len(seen)}", provider="fake", model="fake",
            text=json.dumps(payload, ensure_ascii=False), structured_output=payload,
            response_format="json_object", raw_response={}, usage={}, finish_reason="stop",
        )

    return seen, responder


# ------------------------------------------------------------------ 1. AI 起章名


@pytest.mark.parametrize(
    ("title", "auto"),
    [("", True), ("第 3 章", True), ("第12章", True), ("（待补）", True), ("旧日志", False), ("第三章 雪夜", False)],
)
def test_only_system_made_titles_count_as_unnamed(title: str, auto: bool) -> None:
    assert is_auto_chapter_title(title) is auto


def test_a_model_title_is_cleaned_before_it_reaches_the_chapter_table() -> None:
    assert clean_chapter_title("《旧日志》") == "旧日志"
    assert clean_chapter_title("第三章：雪夜追缉。") == "雪夜追缉"
    assert clean_chapter_title("高潮") == "", "光秃秃的结构标签不是章名"
    assert clean_chapter_title("第 5 章") == "", "只有章号 = 没起名"
    assert clean_chapter_title("字" * 25) == "", "一句话不是章名"


def test_chapter_titles_are_fail_closed_without_a_live_llm(client) -> None:
    project_id = _create_project(client, "titles-no-llm")
    _seed(client, project_id)
    preview = _preview(client, project_id)
    response = client.post(f"{_base(project_id)}/chapter-plan/titles", json={"chapters": _chapters_payload(preview)})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"


def _chapters_payload(preview: dict) -> list[dict]:
    return [
        {"row_uid": c["row_uid"], "title": c["title"], "act": c["act"], "spine": c["spine"],
         "scene_plan_ids": [s["scene_plan_id"] for s in c["scenes"]]}
        for c in preview["chapters"]
    ]


def test_titles_name_the_unsaved_panel_chapters_and_leave_authored_names_alone(client, session, monkeypatch) -> None:
    project_id = _create_project(client, "titles-panel")
    _seed(client, project_id)
    preview = _preview(client, project_id, scenes_per_chapter=3)
    chapters = _chapters_payload(preview)
    assert all(chapter["row_uid"].startswith("new:") for chapter in chapters), "面板里的章还没落库"
    chapters[0]["title"] = "雨夜来信"  # 作者自己起的

    def payload_for(user: str, _call: int) -> dict:
        return {"titles": [
            {"row_uid": chapters[0]["row_uid"], "title": "不该被采用", "summary": "作者起过名字的章不在请求里"},
            {"row_uid": chapters[1]["row_uid"], "title": "《封存的卷宗》", "summary": "她把旧案摆上了台面。"},
            {"row_uid": chapters[2]["row_uid"], "title": "第三章：停职", "summary": "退路断了。"},
            {"row_uid": chapters[3]["row_uid"], "title": "封存的卷宗", "summary": "和前面重名"},
            {"row_uid": "new:does-not-exist", "title": "幽灵章", "summary": ""},
        ]}

    seen, responder = _llm(payload_for)
    _install_llm(monkeypatch, responder)
    before = session.execute(select(SnowflakeStepRun)).scalars().all()
    response = client.post(f"{_base(project_id)}/chapter-plan/titles", json={"chapters": chapters})
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert [(item["row_uid"], item["title"]) for item in data["titles"]] == [
        (chapters[1]["row_uid"], "封存的卷宗"),
        (chapters[2]["row_uid"], "停职"),
    ]
    assert data["titles"][0]["summary"] == "她把旧案摆上了台面。"
    assert data["skipped_authored_count"] == 1 and data["named_count"] == 2
    assert data["remaining_count"] == len(chapters) - 1 - 2
    assert data["notice"]["code"] == "CHAPTER_TITLES_PARTIAL"

    # 提示词：作者起过名字的章只作为「已有章名」给口径，不在待起名的章里；每章带着它的场
    user = seen[0]["user"]
    assert "雨夜来信" in user and chapters[0]["row_uid"] not in user
    assert "事件4" in user and '"story_index"' in user
    # 成员对象的 properties 写在模板里：按 schema 约束解码的后端才写得出内容
    items = seen[0]["schema"]["schema"]["properties"]["titles"]["items"]
    assert set(items["properties"]) == {"row_uid", "title", "summary"}
    # 只读：什么都没落库
    session.expire_all()
    assert SnowflakeChapteringService(session).chapter_plans(project_id)[0].title == _CHAPTERS[0]["title"]
    assert len(session.execute(select(SnowflakeStepRun)).scalars().all()) == len(before)


def test_titles_are_batched_and_later_batches_see_the_earlier_names(client, monkeypatch) -> None:
    project_id = _create_project(client, "titles-batched")
    _seed(client, project_id)
    monkeypatch.setattr(SnowflakeChapteringService, "TITLE_BATCH_SIZE", 2)
    monkeypatch.setattr(SnowflakeChapteringService, "TITLE_MAX_BATCHES", 2)
    preview = _preview(client, project_id, scenes_per_chapter=2)
    chapters = _chapters_payload(preview)
    assert len(chapters) >= 5

    def payload_for(user: str, call: int) -> dict:
        wanted = [c["row_uid"] for c in chapters if f'"{c["row_uid"]}"' in user]
        return {"titles": [{"row_uid": uid, "title": f"章名{call}{i}", "summary": ""} for i, uid in enumerate(wanted)]}

    seen, responder = _llm(payload_for)
    _install_llm(monkeypatch, responder)
    data = client.post(f"{_base(project_id)}/chapter-plan/titles", json={"chapters": chapters}).json()["data"]
    assert len(seen) == 2, "一次请求最多两批"
    assert data["named_count"] == 4 and data["remaining_count"] == len(chapters) - 4
    assert "章名10" in seen[1]["user"], "第二批看得见第一批起好的名字，口径才一致"
    assert "再点一次" in data["notice"]["message"]


def test_a_model_that_names_nothing_is_an_error_not_an_empty_success(client, monkeypatch) -> None:
    project_id = _create_project(client, "titles-empty")
    _seed(client, project_id)
    chapters = _chapters_payload(_preview(client, project_id))
    _seen, responder = _llm(lambda _user, _call: {"titles": [{"row_uid": chapters[0]["row_uid"], "title": "高潮"}]})
    _install_llm(monkeypatch, responder)
    response = client.post(f"{_base(project_id)}/chapter-plan/titles", json={"chapters": chapters})
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "SNOWFLAKE_CHAPTER_TITLES_EMPTY"


def test_nothing_to_name_does_not_call_the_model(client, monkeypatch) -> None:
    project_id = _create_project(client, "titles-all-authored")
    _seed(client, project_id)
    seen, responder = _llm(lambda _user, _call: {"titles": []})
    _install_llm(monkeypatch, responder)
    # 不带 chapters：按已保存的分章——这里 07 的六章都是作者起的名字
    SnowflakeChapteringService_preview = client.post(
        f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "spine_anchor"}
    ).json()["data"]
    saved = {
        "replace_chapters": True,
        "chapters": [{"row_uid": c["row_uid"], "title": c["title"], "act": c["act"], "spine": c["spine"]}
                     for c in SnowflakeChapteringService_preview["chapters"]],
        "assignments": [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": c["row_uid"]}
                        for c in SnowflakeChapteringService_preview["chapters"] for s in c["scenes"]],
    }
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=saved).status_code == 200
    data = client.post(f"{_base(project_id)}/chapter-plan/titles", json={}).json()["data"]
    assert data["titles"] == [] and data["notice"]["code"] == "CHAPTER_TITLES_NOTHING_TO_NAME"
    assert seen == []


# ------------------------------------------------------------------ 2. 变空的旧章


def _active_chapters(session, project_id: str) -> list[ChapterGoal]:
    session.expire_all()
    return list(session.execute(
        select(ChapterGoal)
        .where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
        .order_by(ChapterGoal.display_order)
    ).scalars())


def test_chapters_emptied_by_rechaptering_go_to_the_trash_and_come_back_when_needed(client, session) -> None:
    project_id = _create_project(client, "leftover")
    _seed(client, project_id)
    _pass_triage(client, project_id)

    six = _preview(client, project_id, scenes_per_chapter=2)
    assert len(six["chapters"]) == 6
    _confirm(client, project_id, six, "leftover-6")
    assert len(_active_chapters(session, project_id)) == 6

    # 手加一场到第 6 章：这一章之后不再有计划内的场，但它不是空的 → 必须原样保留
    sixth = f"{project_id}_CH06"
    assert client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{sixth}/scenes",
        json={"title": "作者手加的一场"}, headers={"X-Idempotency-Key": "leftover-extra"},
    ).status_code == 200

    four = _preview(client, project_id, scenes_per_chapter=3)
    assert len(four["chapters"]) == 4
    kinds = {warning["kind"]: warning["message"] for warning in four["warnings"]}
    assert "回收站" in kinds["catalog_leftover_chapters"]
    assert "原样保留" in kinds["catalog_leftover_chapters_kept"]
    result = _confirm(client, project_id, four, "leftover-4")
    assert [item["chapter_id"] for item in result["trashed_empty_chapters"]] == [f"{project_id}_CH05"]

    active = _active_chapters(session, project_id)
    assert [chapter.chapter_id[-5:] for chapter in active] == ["_CH01", "_CH02", "_CH03", "_CH04", "_CH06"]
    fifth = session.get(ChapterGoal, f"{project_id}_CH05")
    assert fifth.trashed_flag == 1 and fifth.trashed_by == AUTO_TRASHED_EMPTY_CHAPTER
    cards = session.execute(select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)).scalars().all()
    assert len(cards) == 13 and all(card.chapter_id != fifth.chapter_id for card in cards)

    # 又分回 6 章：计划指向回收站里的 CH05 —— 取回它，而不是把场景卡搬进一个目录里看不见的章
    again = _preview(client, project_id, scenes_per_chapter=2)
    result = _confirm(client, project_id, again, "leftover-6-again")
    assert result["restored_chapter_ids"] == [f"{project_id}_CH05"]
    assert result["trashed_empty_chapters"] == []
    active = _active_chapters(session, project_id)
    assert {chapter.chapter_id[-5:] for chapter in active} == {f"_CH0{i}" for i in range(1, 7)}
    orders = [chapter.display_order for chapter in active]
    assert orders == sorted(set(orders)), "取回的章不能和别的章撞章序"
    fifth = session.get(ChapterGoal, f"{project_id}_CH05")
    assert fifth.trashed_flag == 0 and fifth.trashed_by is None
    in_fifth = session.execute(
        select(SceneCard).where(SceneCard.chapter_id == fifth.chapter_id, SceneCard.trashed_flag == 0)
    ).scalars().all()
    assert len(in_fifth) == 2


def test_hand_made_and_approved_chapters_are_never_auto_trashed(client, session) -> None:
    project_id = _create_project(client, "leftover-guards")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    created = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters", json={"title": "手建的空章", "with_scene": False},
        headers={"X-Idempotency-Key": "leftover-guards-hand"},
    )
    assert created.status_code == 200, created.text
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "guards-6")
    # 第 6 章已终审通过：场搬走之后就算空了也不许动（终审不可变）
    sixth = session.get(ChapterGoal, f"{project_id}_CH06")
    sixth.state = "approved"
    session.commit()

    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "guards-4")
    assert [item["chapter_id"][-5:] for item in result["trashed_empty_chapters"]] == ["_CH05"]
    titles = [(chapter.narrative_json or {}).get("title") for chapter in _active_chapters(session, project_id)]
    assert "手建的空章" in titles
    assert session.get(ChapterGoal, f"{project_id}_CH06").trashed_flag == 0


def test_resync_applies_the_same_cleanup(client, session) -> None:
    project_id = _create_project(client, "leftover-resync")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "resync-6")
    # 只保存分章（不物化）→ 回流把场景卡搬进前四章
    four = _preview(client, project_id, scenes_per_chapter=3)
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=_payload(four)).status_code == 200
    resync = client.post(f"{_base(project_id)}/resync", json={})
    assert resync.status_code == 200, resync.text
    assert [item["chapter_id"][-5:] for item in resync.json()["data"]["trashed_empty_chapters"]] == ["_CH05", "_CH06"]
    assert len(_active_chapters(session, project_id)) == 4


# ------------------------------------------------------------------ 3. 07 章表不是 09 的输入


def test_the_chapter_table_is_not_an_input_of_the_scene_list() -> None:
    assert FIELDS_CONSUMED["scene_list"] == {"long_synopsis": {"paragraphs"}}


def test_editing_only_the_chapter_table_does_not_send_the_scene_list_to_review(client) -> None:
    project_id = _create_project(client, "chapters-not-upstream")
    _seed(client, project_id)

    def status(step_key: str) -> str:
        workspace = client.get(_base(project_id)).json()["data"]
        return next(step["status"] for step in workspace["steps"] if step["step_key"] == step_key)

    renamed = [dict(chapter) for chapter in _CHAPTERS]
    renamed[0]["title"] = "改过的章名"
    renamed.append({"row_uid": "", "chapter_seq": 7, "act": 3, "title": "尾声", "summary": "收。", "spine": "", "chapter_goal": ""})
    _patch(client, project_id, "long_synopsis", {"paragraphs": ["", "", "", ""], "chapters": renamed})
    _approve(client, project_id, "long_synopsis")
    assert status("scene_list") == "approved", "只改了章表，09 不该需复核"
    assert status("scene_details") == "approved"

    # 五段展开真的改了 → 09 照常需复核（这条依赖还在）
    _patch(client, project_id, "long_synopsis", {"paragraphs": ["铺垫改写了", "", "", ""], "chapters": renamed})
    _approve(client, project_id, "long_synopsis")
    assert status("scene_list") == "stale"


# ------------------------------------------------------------------ 目录章名跟随确认的章表


def _catalog_titles(session, project_id: str) -> list[str]:
    return [str((chapter.narrative_json or {}).get("title") or "") for chapter in _active_chapters(session, project_id)]


def test_catalog_chapter_names_follow_the_confirmed_table_unless_the_author_renamed_them_there(client, session) -> None:
    """章号是位置式的：重新分章之后 CH02 可能已经是另一组场。目录章名只在首次建章时写的话，
    作者在面板里确认的章名（含 AI 起的）到不了目录——目录挂着上一版的名字。"""
    project_id = _create_project(client, "catalog-names")
    _seed(client, project_id)
    _pass_triage(client, project_id)

    six = _preview(client, project_id, scenes_per_chapter=2)
    payload = _payload(six)
    for index, name in enumerate(["旧日志", "撕掉的一页", "磁带", "雪线", "认罪书", "铁窗"]):
        payload["chapters"][index]["title"] = name
    assert client.post(f"{_base(project_id)}/materialize", json=payload, headers={"X-Idempotency-Key": "names-mat-1"}).status_code == 200
    assert client.post(f"{_base(project_id)}/outline/approve", json={}, headers={"X-Idempotency-Key": "names-app-1"}).status_code == 200
    assert _catalog_titles(session, project_id) == ["旧日志", "撕掉的一页", "磁带", "雪线", "认罪书", "铁窗"]

    # 作者在章节编排里亲手把第二章改了名
    renamed = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{project_id}_CH02", json={"title": "我在目录里改的名字"},
        headers={"X-Idempotency-Key": "names-rename"},
    )
    assert renamed.status_code == 200, renamed.text

    four = _preview(client, project_id, scenes_per_chapter=3)
    payload = _payload(four)
    for index, name in enumerate(["风雪夜", "水窖", "火柴梗", "传唤令"]):
        payload["chapters"][index]["title"] = name
    assert client.post(f"{_base(project_id)}/materialize", json=payload, headers={"X-Idempotency-Key": "names-mat-2"}).status_code == 200
    assert client.post(f"{_base(project_id)}/outline/approve", json={}, headers={"X-Idempotency-Key": "names-app-2"}).status_code == 200
    assert _catalog_titles(session, project_id) == ["风雪夜", "我在目录里改的名字", "火柴梗", "传唤令"]


def test_reproposing_keeps_the_chapters_whose_scenes_did_not_change(client, session) -> None:
    project_id = _create_project(client, "reuse-unchanged")
    _seed(client, project_id)
    six = _preview(client, project_id, scenes_per_chapter=2)
    payload = _payload(six)
    payload["chapters"][0]["title"] = "旧日志"
    payload["chapters"][0]["summary"] = "他第一次怀疑那本日志被人动过。"
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=payload).status_code == 200
    saved = {c.row_uid: c for c in SnowflakeChapteringService(session).chapter_plans(project_id)}

    # 同一个每章场数再提议一次：每一章的场都没变 → 身份、章名、章摘要原样沿用，没有一章要「替换」
    again = _preview(client, project_id, scenes_per_chapter=2)
    assert [c["row_uid"] for c in again["chapters"]] == [c.row_uid for c in sorted(saved.values(), key=lambda c: c.chapter_seq)]
    assert again["chapters"][0]["title"] == "旧日志"
    assert again["chapters"][0]["summary"] == "他第一次怀疑那本日志被人动过。"
    assert again["replaces_chapter_count"] == 0

    # 换成每章 3 场：12 场里只有灾三之后的尾章可能没变；没变的沿用，变了的是新章（占位名按新位置编号）
    four = _preview(client, project_id, scenes_per_chapter=3)
    reused = [c for c in four["chapters"] if not c["row_uid"].startswith("new:")]
    fresh = [c for c in four["chapters"] if c["row_uid"].startswith("new:")]
    assert all(c["row_uid"] in saved for c in reused)
    assert fresh and all(c["title"] == f"第 {c['chapter_seq']} 章" for c in fresh)
    assert four["replaces_chapter_count"] == len(saved) - len(reused)
