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


def _chapter_of(client, project_id: str) -> dict[int, str]:
    """故事序号（09 的第几场）→ 它现在所在的目录章 id。场景 id 由 row_uid 铸，这里按目录顺序数。"""
    chapters = client.get(f"/api/v2/projects/{project_id}/catalog").json()["data"]["chapters"]
    planned = [(chapter["chapter_id"], scene) for chapter in chapters for scene in chapter["scenes"] if scene["design"]["origin"] == "snowflake"]
    return {index: chapter_id for index, (chapter_id, _scene) in enumerate(planned, start=1)}


def test_chapters_emptied_by_rechaptering_go_to_the_trash_and_the_rest_keep_their_rows(client, session) -> None:
    """阶段 Y：章 id 钉在章计划上。6 章收成 4 章——场至少一半还在的章还是目录里的那一行（对半时归靠前的一章），
    场被分光的章进回收站；再分回 6 章时新出现的章拿新的序列号、插在章表里它该在的位置；手加的场跟着锚点场走。"""
    project_id = _create_project(client, "leftover")
    _seed(client, project_id)
    _pass_triage(client, project_id)

    six = _preview(client, project_id, scenes_per_chapter=2)
    assert [[s["story_index"] for s in c["scenes"]] for c in six["chapters"]] == [[1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11, 12]]
    _confirm(client, project_id, six, "leftover-6")
    before = _chapter_of(client, project_id)
    assert [chapter.chapter_id[-5:] for chapter in _active_chapters(session, project_id)] == [f"_CH0{i}" for i in range(1, 7)]

    # 手加一场到最后一章的末尾：它的锚点是第 12 场
    last_chapter = before[12]
    assert client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{last_chapter}/scenes",
        json={"title": "作者手加的一场"}, headers={"X-Idempotency-Key": "leftover-extra"},
    ).status_code == 200

    four = _preview(client, project_id, scenes_per_chapter=3)
    assert [[s["story_index"] for s in c["scenes"]] for c in four["chapters"]] == [[1, 2, 3], [4, 5, 6, 7], [8, 9], [10, 11, 12]]
    # 面板预览里就看得出每一章还是原来的哪一章（钉着目录章号）：[4–7] 是 [4,5] 与 [6,7] 对半并成的，归靠前的 [4,5]
    assert [c["chapter_id"] for c in four["chapters"]] == [before[2], before[4], before[8], before[10]]
    assert "回收站" in {w["kind"]: w["message"] for w in four["warnings"]}["catalog_leftover_chapters"]
    result = _confirm(client, project_id, four, "leftover-4")
    assert sorted(item["chapter_id"] for item in result["trashed_empty_chapters"]) == sorted({before[1], before[6]})

    after = _chapter_of(client, project_id)
    assert [after[i] for i in (1, 4, 6, 8, 12)] == [before[2], before[4], before[4], before[8], before[12]]
    assert [chapter.chapter_id for chapter in _active_chapters(session, project_id)] == [before[2], before[4], before[8], before[12]]
    for chapter_id in (before[1], before[6]):
        row = session.get(ChapterGoal, chapter_id)
        assert row.trashed_flag == 1 and row.trashed_by == AUTO_TRASHED_EMPTY_CHAPTER
    cards = session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
    ).scalars().all()
    assert len(cards) == 13
    hand_made = next(card for card in cards if (card.writer_brief_json or {}).get("source") == "catalog_api")
    assert hand_made.chapter_id == after[12] and hand_made.is_chapter_last == 1, "手加的场还跟在第 12 场后面"

    # 再分回 6 章：[2,3] / [4,5] / [8,9] / [10–12] 还是自己那一行；[1] 与 [6,7] 是新章——拿新的序列号，
    # 绝不复用回收站里那两行的号（那两行随时可能被作者取回），并且插在章表里它该在的位置，不是接到全书最后
    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "leftover-6-again")
    again = _chapter_of(client, project_id)
    assert [again[i] for i in (2, 4, 8, 12)] == [before[2], before[4], before[8], before[12]]
    assert {again[1], again[6]}.isdisjoint(set(before.values()))
    assert result["trashed_empty_chapters"] == [] and result["restored_chapter_ids"] == []
    active = _active_chapters(session, project_id)
    assert [chapter.chapter_id for chapter in active] == [again[1], again[2], again[4], again[6], again[8], again[12]]
    orders = [chapter.display_order for chapter in active]
    assert orders == sorted(set(orders)) and len(orders) == 6


def test_a_chapter_that_was_emptied_comes_back_from_the_trash_when_scenes_return_to_it(client, session) -> None:
    """一章在章表里留着、场被挪空 → 目录里那一行进回收站；场又挪回这一章 → 取回同一行，不把卡搬进看不见的章。"""
    project_id = _create_project(client, "leftover-revive")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "revive-1")
    kept = client.post(f"{_base(project_id)}/chapter-plan/preview", json={"strategy": "keep_current"}).json()["data"]
    third = kept["chapters"][2]
    assert [s["story_index"] for s in third["scenes"]] == [8, 9]

    # 把第 3 章的两场并进第 2 章，但第 3 章留在章表里（空章）
    payload = _payload(kept)
    second_uid = kept["chapters"][1]["row_uid"]
    moved = {scene["scene_plan_id"] for scene in third["scenes"]}
    for item in payload["assignments"]:
        if item["scene_plan_id"] in moved:
            item["chapter_row_uid"] = second_uid
    result = _confirm_payload(client, project_id, payload, "revive-2")
    assert [item["chapter_id"] for item in result["trashed_empty_chapters"]] == [third["chapter_id"]]

    # 挪回去：计划指着回收站里的那一行 → 取回
    result = _confirm_payload(client, project_id, _payload(kept), "revive-3")
    assert result["restored_chapter_ids"] == [third["chapter_id"]] and result["trashed_empty_chapters"] == []
    row = session.get(ChapterGoal, third["chapter_id"])
    session.refresh(row)
    assert row.trashed_flag == 0 and row.trashed_by is None
    active = _active_chapters(session, project_id)
    assert [chapter.chapter_id for chapter in active] == [chapter["chapter_id"] for chapter in kept["chapters"]]
    assert len(session.execute(
        select(SceneCard).where(SceneCard.chapter_id == third["chapter_id"], SceneCard.trashed_flag == 0)
    ).scalars().all()) == 2


def _confirm_payload(client, project_id: str, payload: dict, key: str) -> dict:
    materialize = client.post(f"{_base(project_id)}/materialize", json=payload, headers={"X-Idempotency-Key": f"{key}-mat"})
    assert materialize.status_code == 200, materialize.text
    approve = client.post(f"{_base(project_id)}/outline/approve", json={}, headers={"X-Idempotency-Key": f"{key}-approve"})
    assert approve.status_code == 200, approve.text
    return approve.json()["data"]


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
    before = _chapter_of(client, project_id)
    # 6 → 4 章：[4–7] 沿用的是 [4,5] 那一行；空出来的是第 1 场与第 6、7 场原来的章
    approved = session.get(ChapterGoal, before[6])
    approved.state = "approved"
    session.commit()
    result = _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "guards-4")
    assert [item["chapter_id"] for item in result["trashed_empty_chapters"]] == [before[1]]
    titles = [(chapter.narrative_json or {}).get("title") for chapter in _active_chapters(session, project_id)]
    assert "手建的空章" in titles
    session.expire_all()
    assert session.get(ChapterGoal, before[6]).trashed_flag == 0, "终审过的章空了也不许动"


def test_resync_applies_the_same_cleanup(client, session) -> None:
    project_id = _create_project(client, "leftover-resync")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=2), "resync-6")
    before = _chapter_of(client, project_id)
    # 只保存分章（不物化）：4 章全都沿用目录里已有的行 → 回流直接把场景卡搬过去，空出来的两章进回收站
    four = _preview(client, project_id, scenes_per_chapter=3)
    assert all(chapter["chapter_id"] for chapter in four["chapters"])
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=_payload(four)).status_code == 200
    resync = client.post(f"{_base(project_id)}/resync", json={})
    assert resync.status_code == 200, resync.text
    data = resync.json()["data"]
    assert not data.get("notice")
    assert sorted(item["chapter_id"] for item in data["trashed_empty_chapters"]) == sorted({before[1], before[6]})
    after = _chapter_of(client, project_id)
    assert [after[i] for i in (1, 6, 7)] == [before[2], before[4], before[4]]


def test_resync_leaves_scenes_bound_for_a_chapter_the_catalog_does_not_have_yet(client, session) -> None:
    project_id = _create_project(client, "leftover-resync-new")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    _confirm(client, project_id, _preview(client, project_id, scenes_per_chapter=3), "resync-new-4")
    before = _chapter_of(client, project_id)
    # 只保存分章：第 1 场、第 6–7 场各成一章新章（目录里还没有这一行）→ 这三场暂时搬不动，其余的照常回流
    six = _preview(client, project_id, scenes_per_chapter=2)
    assert [bool(chapter["chapter_id"]) for chapter in six["chapters"]] == [False, True, True, False, True, True]
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=_payload(six)).status_code == 200
    data = client.post(f"{_base(project_id)}/resync", json={}).json()["data"]
    assert data["notice"]["code"] == "CHAPTER_MOVE_NEEDS_MATERIALIZE"
    assert data["trashed_empty_chapters"] == []
    assert _chapter_of(client, project_id) == before, "目标章还没物化的场留在原地，等「整理为章节结构」"


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


def test_catalog_chapter_names_follow_the_confirmed_table_and_a_desk_rename_is_the_same_name(client, session) -> None:
    """目录章名只在首次建章时写的话，作者在面板里确认的章名（含 AI 起的）到不了目录——目录挂着上一版的名字。
    阶段 Y：章 id 钉在章计划上，那一章跟着它的场走（这里它从第二章变成了第一章）。
    阶段 Z「章名只有一个」：作者在章节编排里改的名字写穿到章计划——分章面板摆出来的就是这个名字，确认写入时它原样留着；
    之后在面板里再改，目录也跟着走（过去台面上改的是目录里的另一份：面板看不见它，目录从此也不再跟面板）。"""
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

    # 作者在章节编排里亲手把第二章（第 2、3 场）改了名
    renamed_chapter = _chapter_of(client, project_id)[2]
    renamed = client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{renamed_chapter}", json={"title": "我在目录里改的名字"},
        headers={"X-Idempotency-Key": "names-rename"},
    )
    assert renamed.status_code == 200, renamed.text

    # [1–3] 还是第 2、3 场的那一章（同一行章计划）：面板摆出来的就是作者在目录里起的名字
    four = _preview(client, project_id, scenes_per_chapter=3)
    assert four["chapters"][0]["title"] == "我在目录里改的名字"
    payload = _payload(four)
    for index, name in enumerate(["水窖", "火柴梗", "传唤令"], start=1):
        payload["chapters"][index]["title"] = name
    assert client.post(f"{_base(project_id)}/materialize", json=payload, headers={"X-Idempotency-Key": "names-mat-2"}).status_code == 200
    assert client.post(f"{_base(project_id)}/outline/approve", json={}, headers={"X-Idempotency-Key": "names-app-2"}).status_code == 200
    # 它现在排第一，名字留着；其余三章跟面板
    assert _chapter_of(client, project_id)[2] == renamed_chapter
    assert _catalog_titles(session, project_id) == ["我在目录里改的名字", "水窖", "火柴梗", "传唤令"]

    # 同一个名字：之后在面板里给它改名，目录跟着走
    again = _payload(_preview(client, project_id, strategy="keep_current"))
    again["chapters"][0]["title"] = "风雪夜"
    assert client.post(f"{_base(project_id)}/materialize", json=again, headers={"X-Idempotency-Key": "names-mat-3"}).status_code == 200
    assert client.post(f"{_base(project_id)}/outline/approve", json={}, headers={"X-Idempotency-Key": "names-app-3"}).status_code == 200
    assert _catalog_titles(session, project_id) == ["风雪夜", "水窖", "火柴梗", "传唤令"]


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

    # 换成每章 3 场（阶段 Y）：场至少一半还在的章还是那一章——[1–3] ← [2,3]，[4–7] ← [4,5]（对半时归靠前的一章），
    # [8,9] 与 [10–12] 没变。没有一章是新的；被替换掉的只有场被分光的两章。系统起的占位名按新位置重编。
    ordered = sorted(saved.values(), key=lambda c: c.chapter_seq)
    four = _preview(client, project_id, scenes_per_chapter=3)
    assert [c["row_uid"] for c in four["chapters"]] == [ordered[i].row_uid for i in (1, 2, 4, 5)]
    assert [c["chapter_id"] for c in four["chapters"]] == [ordered[i].catalog_chapter_id for i in (1, 2, 4, 5)]
    assert [c["title"] for c in four["chapters"]] == [f"第 {n} 章" for n in (1, 2, 3, 4)]
    assert four["replaces_chapter_count"] == 2

    # 存下这 4 章再换回每章 2 场：第 1 场、第 6–7 场各自够不上任何一章的一半 → 这两章才是新章
    # （占位名按新位置编号，还没有目录章号——确认写入时才铸）
    assert client.patch(f"{_base(project_id)}/chapter-plan", json=_payload(four)).status_code == 200
    six = _preview(client, project_id, scenes_per_chapter=2)
    fresh = [c for c in six["chapters"] if c["row_uid"].startswith("new:")]
    assert [[s["story_index"] for s in c["scenes"]] for c in fresh] == [[1], [6, 7]]
    assert all(c["title"] == f"第 {c['chapter_seq']} 章" and c["chapter_id"] == "" for c in fresh)
    assert six["replaces_chapter_count"] == 0, "四章都还在，只是多出两章"
