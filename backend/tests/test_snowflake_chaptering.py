"""Phase 2 回归：构思侧分章（章表 → 预览 → 确认 → 物化）。

守的是这次重设计的核心承诺：

- 07 里编的章真的会变成目录里的章（以前全书落进一章，章标题就是章 id 字符串）；
- 「整理」之前作者看得见会得到什么（preview 只读、确定性可复算）；
- 没分章不许物化（以前默默给你一章）；
- 重新分章能把已落库的场景卡搬到新章，正文不丢。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import ChapterGoal, SceneCard, SnowflakeChapterPlan
from novel_system.services.catalog import chapter_title


def _create_project(client, key: str) -> str:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Rain City Signal",
            "genre": "Urban Mystery",
            "target_chapter_count": 6,
            "target_word_count": 120000,
            "outline_text": "旧信把她拉回雨城。\n悬案与家族纠缠。\n她必须决定真相值不值得。",
        },
        headers={"X-Idempotency-Key": f"chp-create-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _patch(client, project_id: str, step_key: str, draft: dict) -> dict:
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        json={"draft": draft, "force": True},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve(client, project_id: str, step_key: str) -> None:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve", json={}
    )
    assert response.status_code == 200, response.text


#: 07 长篇大纲：作者真的编出来的六章（结构化 chapters —— P2 的新契约）
_CHAPTERS = [
    {"row_uid": "", "chapter_seq": 1, "act": 1, "title": "雨夜来信", "summary": "一封旧信把她拉回雨城。", "spine": "", "chapter_goal": "让她无法不回去"},
    {"row_uid": "", "chapter_seq": 2, "act": 1, "title": "旧案卷宗", "summary": "她翻出封存的案卷。", "spine": "", "chapter_goal": "把旧案摆上台面"},
    {"row_uid": "", "chapter_seq": 3, "act": 1, "title": "被迫卷入", "summary": "她被停职。", "spine": "灾一", "chapter_goal": "断掉退路"},
    {"row_uid": "", "chapter_seq": 4, "act": 2, "title": "父亲的谎", "summary": "父亲的时间线对不上。", "spine": "", "chapter_goal": "把矛头转向家里"},
    {"row_uid": "", "chapter_seq": 5, "act": 2, "title": "世界观碎", "summary": "她发现父亲在场。", "spine": "灾二", "chapter_goal": "打碎她的信念"},
    {"row_uid": "", "chapter_seq": 6, "act": 3, "title": "余波", "summary": "代价落地。", "spine": "灾三", "chapter_goal": "让代价可见"},
]

_UPSTREAM = {
    "book_brief": {"category": "都市悬疑", "target_reader": "25-35 岁读者", "delight_reason": "抽丝剥茧",
                   "story_kind": "长篇", "genre_promise": "不写甜宠", "expected_reader_emotion": "紧张"},
    "one_sentence_summary": {"summary": "一位记者必须查清旧案，但真凶是她的父亲。"},
    "one_paragraph_summary": {"sentences": ["回到雨城", "灾一：被迫卷入", "灾二：世界观被打碎", "灾三：局势失控", "决战与收尾"],
                              "moral_premise": "真相高于安稳"},
    "character_sheets": {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角", "goal": "查清旧案",
                                         "ambition": "自由", "values": ["诚实"], "conflict": "家族", "epiphany": "真相有代价"}]},
    "short_synopsis": {"paragraphs": ["铺垫", "灾一", "灾二", "灾三", "收尾"]},
    "character_synopses": {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角",
                                           "synopsis": "信念：真相\n旧伤：母亲之死"}]},
    "long_synopsis": {"paragraphs": ["", "", "", ""], "chapters": _CHAPTERS},
    "character_bibles": {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角",
                                         "physical_profile": {"appearance": "瘦"},
                                         "personality_profile": {"strongest_trait": "固执"},
                                         "environment_profile": {"home": "雨城"},
                                         "psychological_profile": {"philosophy": "真相", "self_image": "逃兵",
                                                                   "deepest_fear": "重蹈覆辙"}}]},
}


def _scene(uid: str, seq: int, text: str, spine: str = "") -> dict:
    return {"row_uid": uid, "scene_seq": seq, "summary": text, "primary_form": "proactive",
            "pov_character_id": "c1", "location": "雨城", "crucible": "她不能就这样走开",
            "chapter_role": "推进", "spine": spine}


def _detail(uid: str, seq: int, text: str, spine: str = "") -> dict:
    return {"row_uid": uid, "scene_seq": seq, "title": text, "summary": text, "primary_form": "proactive",
            "location": "雨城", "crucible": "她不能就这样走开", "scene_crucible": "她不能就这样走开",
            "pov_character_id": "c1", "spine": spine,
            "goal": f"{text}·目标", "conflict": f"{text}·冲突", "setback": f"{text}·挫败",
            "cost_requirement": f"{text}·代价"}


#: 12 场，其中三场带脊柱标记，用来锚定灾一/灾二/灾三 三章
_SPINE_AT = {3: "灾一", 7: "灾二", 12: "灾三"}


def _seed(client, project_id: str) -> None:
    for step_key, draft in _UPSTREAM.items():
        _patch(client, project_id, step_key, draft)
        _approve(client, project_id, step_key)
    scenes = [_scene(f"S{i:02d}", i, f"事件{i}", _SPINE_AT.get(i, "")) for i in range(1, 13)]
    _patch(client, project_id, "scene_list", {"scenes": scenes})
    _approve(client, project_id, "scene_list")
    _patch(client, project_id, "scene_details",
           {"scenes": [_detail(f"S{i:02d}", i, f"事件{i}", _SPINE_AT.get(i, "")) for i in range(1, 13)]})
    _approve(client, project_id, "scene_details")


def _pass_triage(client, project_id: str) -> None:
    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    items = [{"scene_plan_id": item["scene_plan_id"], "status": "pass"} for item in workspace["triage_items"]]
    if items:
        assert client.post(
            f"/api/v2/projects/{project_id}/snowflake-workspace/scene-triage", json={"items": items}
        ).status_code == 200


# --------------------------------------------------------------- 07 章表落库


def test_long_synopsis_chapters_become_chapter_plans(client, session) -> None:
    """07 的结构化章表直接同步成构思侧章行，身份锚是 row_uid（改标题不重建行）。"""
    project_id = _create_project(client, "sync")
    _patch(client, project_id, "long_synopsis", _UPSTREAM["long_synopsis"])

    session.expire_all()
    rows = list(session.execute(
        select(SnowflakeChapterPlan)
        .where(SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None))
        .order_by(SnowflakeChapterPlan.chapter_seq)
    ).scalars())
    assert [row.title for row in rows] == [c["title"] for c in _CHAPTERS]
    assert [row.spine for row in rows] == [c["spine"] for c in _CHAPTERS]
    assert [row.act for row in rows] == [c["act"] for c in _CHAPTERS]
    anchors = {row.row_uid for row in rows}
    assert len(anchors) == len(rows) and all(anchors)

    # 改标题不得重建行（否则已分好的场景归属会整片断掉）
    renamed = [dict(c) for c in _CHAPTERS]
    for row, item in zip(rows, renamed):
        item["row_uid"] = row.row_uid
    renamed[0]["title"] = "雨夜来信（改）"
    _patch(client, project_id, "long_synopsis", {"paragraphs": ["", "", "", ""], "chapters": renamed})
    session.expire_all()
    after = list(session.execute(
        select(SnowflakeChapterPlan)
        .where(SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None))
        .order_by(SnowflakeChapterPlan.chapter_seq)
    ).scalars())
    assert {row.row_uid for row in after} == anchors
    assert after[0].title == "雨夜来信（改）"


def test_free_prose_long_synopsis_does_not_fabricate_chapters(client, session) -> None:
    """回归：散文式 07 草稿不得被当成章表。

    回退解析曾把任意非空行都当一章，于是「四段散文」被编造成一堆假章，还抢在真正的
    章归属之前落库。宁可解析不出章（作者去 07 把章列出来），也不要造假章。
    """
    project_id = _create_project(client, "prose")
    _patch(client, project_id, "long_synopsis",
           {"paragraphs": ["她回到雨城，雨下了三天。", "父亲的时间线对不上。", "她把材料交了出去。", ""]})
    session.expire_all()
    rows = list(session.execute(
        select(SnowflakeChapterPlan).where(SnowflakeChapterPlan.project_id == project_id)
    ).scalars())
    assert rows == []


# ------------------------------------------------------------------- 预览


def test_preview_is_deterministic_and_anchors_on_spine(client) -> None:
    project_id = _create_project(client, "preview")
    _seed(client, project_id)

    first = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                        json={"strategy": "spine_anchor"})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                         json={"strategy": "spine_anchor"})
    assert first.json()["data"] == second.json()["data"], "同一输入两次预览结果必须逐字节相同"

    preview = first.json()["data"]
    assert [c["title"] for c in preview["chapters"]] == [c["title"] for c in _CHAPTERS]
    assert preview["totals"]["scene_count"] == 12
    assert preview["totals"]["unassigned_count"] == 0

    # 三个灾难场必须落在同标记的章里 —— 这是脊柱锚点的全部意义
    placed = {
        scene["title"]: chapter["spine"]
        for chapter in preview["chapters"]
        for scene in chapter["scenes"]
    }
    assert placed["事件3"] == "灾一"
    assert placed["事件7"] == "灾二"
    assert placed["事件12"] == "灾三"


def test_preview_does_not_write_scene_assignments(client, session) -> None:
    """预览是只读推演：作者没确认之前，一个场的归属都不许落库。"""
    project_id = _create_project(client, "readonly")
    _seed(client, project_id)

    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                       json={"strategy": "even"}).status_code == 200
    session.expire_all()
    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    assert workspace["chapter_plan_status"]["assigned_scene_count"] == 0
    assert workspace["chapter_plan_status"]["unassigned_scene_count"] == 12


def test_even_strategy_spreads_scenes_across_all_chapters(client) -> None:
    project_id = _create_project(client, "even")
    _seed(client, project_id)
    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "even"}).json()["data"]
    counts = [c["scene_count"] for c in preview["chapters"]]
    assert sum(counts) == 12
    assert all(count for count in counts), f"均分不该留空章：{counts}"


# --------------------------------------------------------------- 闸门与物化


def test_materialize_without_chaptering_is_blocked_with_an_author_action(client) -> None:
    """回归本次重设计的起点：没分章就物化，以前默默给你「全书一章」。"""
    project_id = _create_project(client, "gate")
    _seed(client, project_id)
    _pass_triage(client, project_id)

    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
                           json={}, headers={"X-Idempotency-Key": "gate-mat"})
    assert response.status_code == 409, response.text
    gate = response.json()["error"]["details"]["materialization_gate"]
    assert any(item["kind"] == "chapter_plan_required" for item in gate["items"])
    action = next(item for item in gate["items"] if item["kind"] == "chapter_plan_required")
    assert action["primary_action"]["type"] == "open_chapter_plan"


def test_confirmed_chapter_plan_materializes_the_authored_chapters(client, session) -> None:
    """端到端：07 编的六章 → 目录里就是那六章，章名是作者写的、不是章 id。"""
    project_id = _create_project(client, "e2e")
    _seed(client, project_id)
    _pass_triage(client, project_id)

    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "spine_anchor"}).json()["data"]
    payload = {
        "chapters": [
            {"row_uid": c["row_uid"], "title": c["title"], "act": c["act"],
             "spine": c["spine"], "chapter_goal": c["chapter_goal"]}
            for c in preview["chapters"]
        ],
        "assignments": [
            {"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": c["row_uid"], "scene_seq": s["scene_seq"]}
            for c in preview["chapters"] for s in c["scenes"]
        ],
    }

    materialize = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
                              json=payload, headers={"X-Idempotency-Key": "e2e-mat"})
    assert materialize.status_code == 200, materialize.text
    plan_json = materialize.json()["data"]["plan"]["plan_json"]
    assert [c["title"] for c in plan_json["chapters"]] == [c["title"] for c in _CHAPTERS]

    approve = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
                          json={}, headers={"X-Idempotency-Key": "e2e-approve"})
    assert approve.status_code == 200, approve.text
    assert approve.json()["data"]["created_chapter_count"] == len(_CHAPTERS)
    assert approve.json()["data"]["created_scene_count"] == 12

    session.expire_all()
    chapters = list(session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id).order_by(ChapterGoal.display_order)
    ).scalars())
    assert [chapter_title(chapter) for chapter in chapters] == [c["title"] for c in _CHAPTERS]
    # 章 id 字符串绝不能再出现在作者眼前
    assert all(chapter_title(chapter) != chapter.chapter_id for chapter in chapters)
    assert [chapter.narrative_json["act"] for chapter in chapters] == [c["act"] for c in _CHAPTERS]
    assert [chapter.display_order for chapter in chapters] == list(range(1, len(_CHAPTERS) + 1))

    cards = list(session.execute(select(SceneCard).where(SceneCard.project_id == project_id)).scalars())
    assert len(cards) == 12
    assert len({card.chapter_id for card in cards}) == len(_CHAPTERS)


def test_explicit_strategy_lets_api_callers_skip_the_preview_round_trip(client, session) -> None:
    """脚本 / API 调用方可以直接指名策略，但策略必须显式写出来（不是服务端偷偷决定）。"""
    project_id = _create_project(client, "strategy")
    _seed(client, project_id)
    _pass_triage(client, project_id)

    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
                           json={"strategy": "spine_anchor"}, headers={"X-Idempotency-Key": "strategy-mat"})
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["plan"]["plan_json"]["chapters"]) == len(_CHAPTERS)


def test_saving_a_chapter_plan_alone_does_not_materialize(client, session) -> None:
    """只保存分章不物化：作者可以先存一版，目录不会因此凭空多出章。"""
    project_id = _create_project(client, "save-only")
    _seed(client, project_id)

    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "even"}).json()["data"]
    payload = {
        "chapters": [{"row_uid": c["row_uid"], "title": c["title"], "act": c["act"],
                      "spine": c["spine"], "chapter_goal": c["chapter_goal"]} for c in preview["chapters"]],
        "assignments": [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": c["row_uid"],
                         "scene_seq": s["scene_seq"]} for c in preview["chapters"] for s in c["scenes"]],
    }
    saved = client.patch(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["assigned_scene_count"] == 12
    assert saved.json()["data"]["workspace"]["chapter_plan_status"]["chaptered"] is True

    session.expire_all()
    assert list(session.execute(select(ChapterGoal).where(ChapterGoal.project_id == project_id)).scalars()) == []


def test_rechaptering_moves_materialized_scene_cards(client, session) -> None:
    """重新分章：已落库的场景卡跟着搬到新章，场景身份（scene_id）不变，正文不受影响。"""
    project_id = _create_project(client, "rechapter")
    _seed(client, project_id)
    _pass_triage(client, project_id)
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
                       json={"strategy": "spine_anchor"},
                       headers={"X-Idempotency-Key": "re-mat"}).status_code == 200
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
                       json={}, headers={"X-Idempotency-Key": "re-approve"}).status_code == 200

    session.expire_all()
    before = {card.scene_id: card.chapter_id for card in session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id)).scalars()}

    # 把所有场重新塞进第一章
    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "keep_current"}).json()["data"]
    first = preview["chapters"][0]
    every_scene = [s for c in preview["chapters"] for s in c["scenes"]]
    payload = {
        "chapters": [{"row_uid": c["row_uid"], "title": c["title"], "act": c["act"],
                      "spine": c["spine"], "chapter_goal": c["chapter_goal"]} for c in preview["chapters"]],
        "assignments": [{"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": first["row_uid"],
                         "scene_seq": i + 1} for i, s in enumerate(every_scene)],
    }
    assert client.patch(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan",
                        json=payload).status_code == 200

    resync = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/resync", json={})
    assert resync.status_code == 200, resync.text

    session.expire_all()
    after = {card.scene_id: card.chapter_id for card in session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id)).scalars()}
    assert set(after) == set(before), "重新分章不得改变场景身份"
    assert len(set(after.values())) == 1, "所有场应该已经搬进同一章"


# ------------------------------------------------------------ P3 节奏体检


def test_preview_reports_rhythm_without_calling_an_llm(client) -> None:
    """节奏体检是纯确定性的结构统计：每章场数分布、三幕配比、灾难落点。"""
    project_id = _create_project(client, "rhythm")
    _seed(client, project_id)

    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "spine_anchor"}).json()["data"]
    rhythm = preview["rhythm"]
    assert sum(rhythm["scene_counts"]) == 12
    assert rhythm["min_scenes"] >= 1 and rhythm["max_scenes"] >= rhythm["min_scenes"]
    assert [a["act"] for a in rhythm["acts"]] == [1, 2, 3]
    assert sum(a["scene_count"] for a in rhythm["acts"]) == 12

    placement = {item["spine"]: item for item in rhythm["spine_placement"]}
    assert set(placement) == {"灾一", "灾二", "灾三"}
    assert all(item["placed"] for item in placement.values())
    # 07 里灾一在第一幕最后一章、灾三在第三幕 —— 灾三不在第二幕，体检应如实说它偏了
    assert placement["灾一"]["on_hinge"] is True
    assert placement["灾三"]["on_hinge"] is False
    assert any(w["kind"] == "spine_off_hinge" for w in preview["warnings"])
    # 提示只是 advisory，绝不阻断
    assert all(w["severity"] != "blocker" for w in preview["warnings"] if w["kind"].startswith("spine_"))


def test_missing_spine_marks_are_reported_but_never_block(client) -> None:
    project_id = _create_project(client, "no-spine")
    flat = [dict(c, spine="") for c in _CHAPTERS]
    for step_key, draft in _UPSTREAM.items():
        _patch(client, project_id, step_key,
               {"paragraphs": ["", "", "", ""], "chapters": flat} if step_key == "long_synopsis" else draft)
        _approve(client, project_id, step_key)
    _patch(client, project_id, "scene_list", {"scenes": [_scene(f"S{i:02d}", i, f"事件{i}") for i in range(1, 13)]})
    _approve(client, project_id, "scene_list")

    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "even"}).json()["data"]
    missing = [w for w in preview["warnings"] if w["kind"] == "spine_not_placed"]
    assert len(missing) == 3
    assert all(w["severity"] == "advisory" for w in missing)
    assert all(not item["placed"] for item in preview["rhythm"]["spine_placement"])


# ------------------------------------------------------- P3 LLM 分章建议


def test_chapter_plan_suggest_is_fail_closed_without_a_live_llm(client) -> None:
    """作者点的是「AI 建议」。模型没配好就诚实报错 —— 绝不拿规则结果冒充建议。"""
    project_id = _create_project(client, "suggest-closed")
    _seed(client, project_id)

    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/suggest", json={})
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] in {"SNOWFLAKE_LLM_NOT_CONFIGURED", "SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING"}
    assert error["details"]["node_id"] == "snowflake_chapter_plan"


# --------------------------------------------- 07 重生成不得摧毁已分好的章


def _install_llm(monkeypatch, responder):
    from novel_system.services import snowflake_workspace_llm as mod

    monkeypatch.setattr(mod, "execute_accounted_call",
                        lambda session, client, request, context, *, llm_call_id: responder(request))
    monkeypatch.setattr(mod, "mark_postprocess_failure", lambda session, llm_call_id, **kwargs: None)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_supplement_accounted_call",
                        lambda self, **kwargs: None)


def _chapter_llm_response(chapters):
    import json as _json

    from novel_system.services.llm_client import LLMResponse

    payload = {
        "paragraphs": ["一幕", "二幕", "三幕", ""],
        # 模型按契约不回 row_uid（提示词明说 "Leave row_uid as \"\""）
        "chapters": [{"act": c["act"], "title": c["title"], "summary": c["summary"],
                      "spine": c["spine"], "chapter_goal": c["chapter_goal"]} for c in chapters],
    }
    return LLMResponse(request_id="r", provider="p", model="m",
                       text=_json.dumps(payload, ensure_ascii=False), structured_output=payload,
                       response_format="json_object", raw_response={}, usage={}, finish_reason="stop")


def test_chapter_plan_suggestion_replays_without_a_second_llm_call(client, monkeypatch) -> None:
    import json as _json

    from novel_system.services.llm_client import LLMResponse

    project_id = _create_project(client, "suggest-idempotent")
    _seed(client, project_id)
    calls = 0

    def responder(_request):
        nonlocal calls
        calls += 1
        payload = {"assignments": [], "rationale": "Keep the deterministic grouping."}
        return LLMResponse(
            request_id="suggest-once",
            provider="fake",
            model="fake",
            text=_json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={},
            usage={},
            finish_reason="stop",
        )

    _install_llm(monkeypatch, responder)
    headers = {"X-Idempotency-Key": "chapter-plan-suggest-once"}
    path = f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/suggest"

    suggested = client.post(path, headers=headers, json={})
    replayed = client.post(path, headers=headers, json={})

    assert suggested.status_code == 200, suggested.text
    assert replayed.status_code == 200, replayed.text
    assert replayed.headers["X-Idempotency-Status"] == "replayed"
    assert replayed.json()["data"] == suggested.json()["data"]
    assert calls == 1


def _live_chapters(session, project_id: str) -> list[SnowflakeChapterPlan]:
    session.expire_all()
    return list(session.execute(
        select(SnowflakeChapterPlan)
        .where(SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None))
        .order_by(SnowflakeChapterPlan.chapter_seq)
    ).scalars())


def _bound_scene_count(session, project_id: str) -> int:
    from novel_system.db.models import SnowflakeScenePlan

    return sum(1 for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars() if plan.chapter_plan_id)


def _autoassign(session, project_id: str) -> None:
    from novel_system.services.snowflake_chaptering import SnowflakeChapteringService

    SnowflakeChapteringService(session).autoassign(project_id, strategy="spine_anchor", actor_ref="test")
    session.commit()


def test_regenerating_step_07_keeps_chapter_identity_and_scene_bindings(client, session, monkeypatch) -> None:
    """作者分好章之后点「AI 生成」润色章标题——全书归属必须原样活下来。

    回归一次真正的数据销毁：``_sanitize_chapter_items`` 把每一章的 row_uid 一律清空，
    ``_sync_chapter_plans`` 于是判定「这是全新的六章」——软删全部旧章行、给分在里面的
    每一场 ``chapter_plan_id = None``。作者只想改个标题，整本书的分章无声消失，物化
    闸门退回 blocked，而界面报的是「已生成」。
    """
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "regen-keep")
    _seed(client, project_id)
    _autoassign(session, project_id)

    before_uids = [row.row_uid for row in _live_chapters(session, project_id)]
    before_bound = _bound_scene_count(session, project_id)
    assert len(before_uids) == len(_CHAPTERS) and before_bound == 12

    polished = [dict(c, title=c["title"] + "·润") for c in _CHAPTERS]
    _install_llm(monkeypatch, lambda request: _chapter_llm_response(polished))
    result = SnowflakeWorkspaceService(session).generate_step(project_id, "long_synopsis", {})
    session.commit()

    after = _live_chapters(session, project_id)
    assert [row.row_uid for row in after] == before_uids, "章身份被重铸，已分好的归属会整片断掉"
    assert [row.title for row in after] == [c["title"] for c in polished], "润色没落库"
    assert _bound_scene_count(session, project_id) == before_bound, "场景归属被解绑"
    assert not (result["step"]["health"] or {}).get("generation_notice"), "非破坏性重生成不该报警"


def test_a_shrinking_chapter_table_tells_the_author_which_scenes_came_loose(client, session, monkeypatch) -> None:
    """模型真把六章合成三章时，松绑是合法结果——但绝不能报成一次干净的成功。"""
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "regen-shrink")
    _seed(client, project_id)
    _autoassign(session, project_id)

    _install_llm(monkeypatch, lambda request: _chapter_llm_response(_CHAPTERS[:3]))
    result = SnowflakeWorkspaceService(session).generate_step(project_id, "long_synopsis", {})
    session.commit()

    health = result["step"]["health"]
    notice = health.get("generation_notice") or {}
    assert notice.get("code") == "CHAPTER_PLAN_SHRUNK"
    assert health["severity"] == "warning", "作者会把它当成普通成功"
    assert notice["unbound_scene_count"] == 12 - _bound_scene_count(session, project_id)
    assert notice["unbound_scene_count"] > 0
    # 幸存的三章仍是原来那三章（身份没被重铸），松绑的只有消失的那三章里的场
    survivors = _live_chapters(session, project_id)
    assert len(survivors) == 3
    assert [row.title for row in survivors] == [c["title"] for c in _CHAPTERS[:3]]
    assert all(row.row_uid for row in survivors)


def test_a_growing_chapter_table_mints_identity_only_for_the_new_chapters(client, session, monkeypatch) -> None:
    """模型加了两章：前六章的身份不动，只有新增的两章拿新 uid。"""
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "regen-grow")
    _seed(client, project_id)
    _autoassign(session, project_id)
    before_uids = [row.row_uid for row in _live_chapters(session, project_id)]

    grown = _CHAPTERS + [
        {"row_uid": "", "chapter_seq": 7, "act": 3, "title": "新章甲", "summary": "补一章。", "spine": "", "chapter_goal": "过渡"},
        {"row_uid": "", "chapter_seq": 8, "act": 3, "title": "新章乙", "summary": "再补一章。", "spine": "", "chapter_goal": "收束"},
    ]
    _install_llm(monkeypatch, lambda request: _chapter_llm_response(grown))
    SnowflakeWorkspaceService(session).generate_step(project_id, "long_synopsis", {})
    session.commit()

    after = _live_chapters(session, project_id)
    assert [row.row_uid for row in after[:6]] == before_uids
    assert len(after) == 8
    minted = {row.row_uid for row in after[6:]}
    assert len(minted) == 2 and not (minted & set(before_uids)), "新章必须拿到自己的新身份"
    assert _bound_scene_count(session, project_id) == 12, "加章不该动已有归属"


def test_a_frontend_payload_missing_chapters_cannot_wipe_the_chapter_table(session) -> None:
    """draft_override 是「补上未保存的本地编辑」，不是删除指令——章表同场景表一样受保护。"""
    from novel_system.services.snowflake_workspace import _merge_dicts_keeping_members

    base = {"chapters": [{"row_uid": "a", "title": "一"}, {"row_uid": "b", "title": "二"}]}
    override = {"chapters": [{"row_uid": "a", "title": "一（改）"}]}
    merged = _merge_dicts_keeping_members(base, override)
    assert [c["row_uid"] for c in merged["chapters"]] == ["a", "b"], "FE 少带一章就把它删了"
    assert merged["chapters"][0]["title"] == "一（改）"


def test_status_sees_the_same_chapters_materialize_would_derive(client, session) -> None:
    """闸门的诊断必须和 ensure_chapter_plans 用同一条派生链，否则它会撒谎。

    07 里编好了章表、但章行还没落库（历史项目 / 刚导入）时，status 曾经漏掉
    ``_derive_from_long_synopsis``，于是报「还没有分章：章节结构要先决定每一场归哪一章」，
    把作者支回 07 去做一件他已经做完的事。真相是章有了、只是还没有一场绑上去。
    """
    from novel_system.db.models import SnowflakeScenePlan
    from novel_system.services.snowflake_chaptering import SnowflakeChapteringService

    project_id = _create_project(client, "status-truth")
    for step_key, draft in _UPSTREAM.items():
        _patch(client, project_id, step_key, draft)
    _patch(client, project_id, "scene_list", {"scenes": [_scene(f"S{i:02d}", i, f"事件{i}") for i in range(1, 13)]})

    # 模拟「07 有章表，但 SnowflakeChapterPlan 行还不存在」
    session.expire_all()
    for row in session.execute(
        select(SnowflakeChapterPlan).where(SnowflakeChapterPlan.project_id == project_id)
    ).scalars():
        session.delete(row)
    for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars():
        plan.chapter_plan_id = None
    session.flush()

    service = SnowflakeChapteringService(session)
    status = service.status(project_id, service.scene_plans(project_id))
    assert status["chapter_count"] == len(_CHAPTERS), "status 看不见 07 的章表"
    assert status["unassigned_scene_count"] == 12, "章有了但还没有一场绑上去——这才是此时的真相"
    assert status["chaptered"] is False
    # 与 materialize 走的那条链结果一致
    assert len(service.ensure_chapter_plans(project_id)) == status["chapter_count"]


# ------------------------------------------------- 孤儿场：blocker 必须有出路


def _make_orphan(client, session, key: str):
    """作者删掉一场已经物化成场景卡（可能已写正文）的场。"""
    from novel_system.db.models import SnowflakeScenePlan

    project_id = _create_project(client, key)
    _seed(client, project_id)
    _pass_triage(client, project_id)
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
                       json={"strategy": "spine_anchor"},
                       headers={"X-Idempotency-Key": f"{key}-mat"}).status_code == 200
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
                       json={}, headers={"X-Idempotency-Key": f"{key}-app"}).status_code == 200
    _patch(client, project_id, "scene_list",
           {"scenes": [_scene(f"S{i:02d}", i, f"事件{i}") for i in range(1, 12)]})

    session.expire_all()
    orphan = next(plan for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars() if plan.orphaned_flag)
    return project_id, orphan


def _blockers(client, project_id: str) -> list[dict]:
    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "keep_current"})
    assert preview.status_code == 200, preview.text
    return [w for w in preview.json()["data"]["warnings"] if w["severity"] == "blocker"]


def test_readding_the_scene_clears_the_orphan_flag(client, session) -> None:
    """把场加回场景列表 = 它不再是孤儿。

    回归一个死结：清 ``orphaned_flag`` 的那一行写在「复活软删行」分支里，而已物化的场
    被删走的是**打标记不软删**那条路（``removed_at`` 保持 NULL），复活分支永远摸不到它。
    于是 orphaned_flag 只写不清，分章面板的 blocker 永久挂着、「确认分章」再也点不动。
    """
    project_id, orphan = _make_orphan(client, session, "orphan-readd")
    assert _blockers(client, project_id), "前提没成立：没有产生孤儿场"

    _patch(client, project_id, "scene_list",
           {"scenes": [_scene(f"S{i:02d}", i, f"事件{i}") for i in range(1, 13)]})
    session.expire_all()
    session.refresh(orphan)
    assert not orphan.orphaned_flag
    assert not _blockers(client, project_id), "场加回来了，blocker 还在"


def test_keeping_the_prose_resolves_the_blocker_without_touching_the_scene_card(client, session) -> None:
    """「保留正文」：目录里那一场留着（可能已经写了几千字），构思侧不再管它。"""
    project_id, orphan = _make_orphan(client, session, "orphan-keep")
    scene_id = orphan.scene_id

    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{orphan.scene_plan_id}/resolve",
        json={"action": "keep"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["trashed_scene_card"] is False

    assert not _blockers(client, project_id)
    session.expire_all()
    card = session.get(SceneCard, scene_id)
    assert card is not None and not card.trashed_flag, "「保留」把正文删了"


def test_discarding_sends_the_scene_card_to_the_trash_not_oblivion(client, session) -> None:
    """「一并删除」：场景卡进回收站——上面可能有作者写的稿子，必须可反悔。"""
    project_id, orphan = _make_orphan(client, session, "orphan-discard")
    scene_id = orphan.scene_id

    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{orphan.scene_plan_id}/resolve",
        json={"action": "discard"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["trashed_scene_card"] is True

    assert not _blockers(client, project_id)
    session.expire_all()
    card = session.get(SceneCard, scene_id)
    assert card is not None and card.trashed_flag, "应该进回收站，而不是物理删除"


def test_a_resolved_orphan_stays_resolved_across_the_next_scene_list_patch(client, session) -> None:
    """处置必须持久。只清 flag 不软删的话，下一次 PATCH 场景列表会立刻把它重新标成孤儿。"""
    project_id, orphan = _make_orphan(client, session, "orphan-durable")
    client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{orphan.scene_plan_id}/resolve",
        json={"action": "keep"})

    _patch(client, project_id, "scene_list",
           {"scenes": [_scene(f"S{i:02d}", i, f"事件{i}·改") for i in range(1, 12)]})
    assert not _blockers(client, project_id), "作者陷在同一个循环里：处置完又被重新标成孤儿"


def test_resolving_a_scene_that_is_not_an_orphan_is_refused(client, session) -> None:
    """不是孤儿的场不许被这个端点处置——它会软删计划行。"""
    from novel_system.db.models import SnowflakeScenePlan

    project_id, orphan = _make_orphan(client, session, "orphan-guard")
    healthy = next(plan for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars() if not plan.orphaned_flag and not plan.removed_at)

    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{healthy.scene_plan_id}/resolve",
        json={"action": "discard"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SNOWFLAKE_SCENE_NOT_ORPHANED"

    bad_action = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{orphan.scene_plan_id}/resolve",
        json={"action": "burn"})
    assert bad_action.status_code == 400
    assert bad_action.json()["error"]["code"] == "SNOWFLAKE_ORPHAN_ACTION_INVALID"


# -------------------------------- 09 生成的脊柱标记必须落地（分章锚点靠它）


def _scene_list_response(spine_by_index: dict[int, str]):
    import json as _json

    from novel_system.services.llm_client import LLMResponse

    payload = {"scenes": [
        {"row_uid": f"S{i:02d}", "summary": f"新事件{i}", "primary_form": "proactive",
         "pov_character_id": "c1", "location": "雨城", "crucible": "走不开",
         "chapter_role": "推进", **({"spine": spine_by_index[i]} if i in spine_by_index else {})}
        for i in range(1, 13)]}
    return LLMResponse(request_id="r", provider="p", model="m",
                       text=_json.dumps(payload, ensure_ascii=False), structured_output=payload,
                       response_format="json_object", raw_response={}, usage={}, finish_reason="stop")


def _db_spines(session, project_id: str) -> dict[str, str]:
    from novel_system.db.models import SnowflakeScenePlan

    session.expire_all()
    return {plan.row_uid: (plan.spine or "") for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars() if plan.spine}


def test_generated_scene_list_keeps_the_disaster_marks(client, session, monkeypatch) -> None:
    """提示词要模型标 灾一/灾二/灾三，服务端就必须收下——分章的锚点全靠它。

    ``spine`` 不在 scene_list 的编辑器模板里，而清洗器按模板逐键搬运，于是模型标好的
    脊柱被整批丢弃：提示词那句「服务端丢弃其它键，写错键名就是内容丢失」说的正是它自己。
    锚点一丢，脊柱锚点分章找不到灾难落点，退化成按场数平均切章。
    """
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "spine-generate")
    _seed(client, project_id)

    _install_llm(monkeypatch, lambda request: _scene_list_response({2: "灾一", 6: "灾二", 11: "灾三"}))
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    session.commit()

    assert _db_spines(session, project_id) == {"S02": "灾一", "S06": "灾二", "S11": "灾三"}


def test_a_regenerated_scene_list_replaces_the_old_marks_rather_than_stacking(client, session, monkeypatch) -> None:
    """整表重生成时模型给的脊柱是新真相：旧标记要让位，不能新旧并存。"""
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "spine-replace")
    _seed(client, project_id)  # 基线标在第 3 / 7 / 12 场
    assert _db_spines(session, project_id) == {"S03": "灾一", "S07": "灾二", "S12": "灾三"}

    _install_llm(monkeypatch, lambda request: _scene_list_response({5: "灾一", 8: "灾二", 12: "灾三"}))
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    session.commit()

    spines = _db_spines(session, project_id)
    assert spines == {"S05": "灾一", "S08": "灾二", "S12": "灾三"}, "旧的灾一还挂在第 3 场，两个灾一同时生效"


def test_a_model_that_never_mentions_spine_leaves_the_author_marks_alone(client, session, monkeypatch) -> None:
    """模型压根没碰这个字段时，绝不能把作者手标的三个灾难清空。"""
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "spine-untouched")
    _seed(client, project_id)
    before = _db_spines(session, project_id)

    _install_llm(monkeypatch, lambda request: _scene_list_response({}))
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    session.commit()

    assert _db_spines(session, project_id) == before


def test_an_invented_spine_mark_is_rejected(client, session, monkeypatch) -> None:
    """只认三个合法标记；模型编的「灾四」不进库，也不因此触发整表改写。"""
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = _create_project(client, "spine-invented")
    _seed(client, project_id)
    before = _db_spines(session, project_id)

    _install_llm(monkeypatch, lambda request: _scene_list_response({4: "灾四", 9: "高潮"}))
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    session.commit()

    assert _db_spines(session, project_id) == before


# ------------------------------ 回流：搬不进目录的章要如实报告，不能整次炸掉


def _materialized_project(client, key: str) -> str:
    project_id = _create_project(client, key)
    _seed(client, project_id)
    _pass_triage(client, project_id)
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
                       json={"strategy": "spine_anchor"},
                       headers={"X-Idempotency-Key": f"{key}-m"}).status_code == 200
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
                       json={}, headers={"X-Idempotency-Key": f"{key}-a"}).status_code == 200
    return project_id


def _rechapter_into_unmaterialized_chapters(client, project_id: str) -> None:
    """作者在 07 加了两章、把所有场搬进去，但还没「整理为章节结构」。

    构思侧于是指向 ``…_CH07`` / ``…_CH08``，而目录里只有 CH01..CH06。
    """
    grown = [dict(c) for c in _CHAPTERS] + [
        {"row_uid": "", "chapter_seq": 7, "act": 3, "title": "新章甲", "summary": "补一章。",
         "spine": "", "chapter_goal": "过渡"},
        {"row_uid": "", "chapter_seq": 8, "act": 3, "title": "新章乙", "summary": "再补一章。",
         "spine": "", "chapter_goal": "收束"},
    ]
    _patch(client, project_id, "long_synopsis", {"paragraphs": ["", "", "", ""], "chapters": grown})

    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "keep_current"}).json()["data"]
    chapters = preview["chapters"]
    scenes = [s for c in chapters for s in c["scenes"]] + list(preview.get("unassigned") or [])
    payload = {
        "chapters": [{"row_uid": c["row_uid"], "act": c["act"], "title": c["title"],
                      "summary": c.get("summary", ""), "spine": c.get("spine", ""),
                      "chapter_goal": c.get("chapter_goal", "")} for c in chapters],
        "assignments": [
            {"scene_plan_id": s["scene_plan_id"],
             "chapter_row_uid": chapters[6 if i < 6 else 7]["row_uid"], "scene_seq": (i % 6) + 1}
            for i, s in enumerate(scenes)],
    }
    saved = client.patch(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan", json=payload)
    assert saved.status_code == 200, saved.text


def test_resync_survives_a_chapter_that_is_not_in_the_catalog_yet(client, session) -> None:
    """回归一次 500：``SceneCard.chapter_id`` 是指向 chapter_goals 的外键。

    重新分章只写构思侧（``{project}_CH{seq:02d}``），目录里的 ChapterGoal 要等
    「整理为章节结构」才建。中间窗口里回流会把一个根本不存在的章号写进外键列，
    SQLite 抛 FOREIGN KEY constraint failed，整次回流变成「database operation failed」——
    连本来能同步的内容改动一起赔进去。
    """
    project_id = _materialized_project(client, "resync-fk")
    _rechapter_into_unmaterialized_chapters(client, project_id)
    # 真的改一处内容再回流。以前这里不改也能过：物化与回流的 exit_change 配方不同，刚物化完的
    # 每一场都被算成「有改动」（阶段 C 把两边配方统一后，这个假阳性消失了）。
    board = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]["scene_board"]
    first_plan_id = board["scenes"][0]["scene_plan_id"]
    edited = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/scenes/{first_plan_id}",
        json={"hook": "回流前改过的钩子"},
    )
    assert edited.status_code == 200, edited.text

    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/resync", json={})
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    # 内容改动照常回流
    assert any(item["synced"] for item in data["results"])
    synced_ids = {item["scene_plan_id"] for item in data["results"] if item["synced"]}
    assert first_plan_id in synced_ids
    # 搬章这件事如实上报，而不是静默跳过
    notice = data.get("notice") or {}
    assert notice.get("code") == "CHAPTER_MOVE_NEEDS_MATERIALIZE"
    assert len(notice["pending_moves"]) == 12
    assert "整理为章节结构" in notice["message"]

    session.expire_all()
    catalog_ids = {row.chapter_id for row in session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id)).scalars()}
    cards = list(session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id)).scalars())
    assert cards and all(card.chapter_id in catalog_ids for card in cards), "场景卡指向了不存在的章"


def test_a_blocked_chapter_move_does_not_renumber_the_scene_inside_its_old_chapter(client, session) -> None:
    """位置 = 章 + 序。搬章被拦下时 scene_seq 也必须留在原地，否则会在**旧**章里撞号。"""
    project_id = _materialized_project(client, "resync-seq")
    session.expire_all()
    before = {card.scene_id: (card.chapter_id, card.scene_seq) for card in session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id)).scalars()}

    _rechapter_into_unmaterialized_chapters(client, project_id)
    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/resync",
                       json={}).status_code == 200

    session.expire_all()
    after = {card.scene_id: (card.chapter_id, card.scene_seq) for card in session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id)).scalars()}
    assert after == before, "章没搬成，序号却按新章重排了——同一章里出现了重复的 scene_seq"


def test_resync_still_moves_scenes_between_chapters_that_do_exist(client, session) -> None:
    """护栏不能把正常的搬章一起挡掉：目录里有的章照搬不误。"""
    project_id = _materialized_project(client, "resync-move-ok")
    session.expire_all()

    preview = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                          json={"strategy": "keep_current"}).json()["data"]
    chapters = preview["chapters"]
    scenes = [s for c in chapters for s in c["scenes"]]
    # 全书塞进第一章（目录里确实存在）
    payload = {
        "chapters": [{"row_uid": c["row_uid"], "act": c["act"], "title": c["title"],
                      "summary": c.get("summary", ""), "spine": c.get("spine", ""),
                      "chapter_goal": c.get("chapter_goal", "")} for c in chapters],
        "assignments": [{"scene_plan_id": s["scene_plan_id"],
                         "chapter_row_uid": chapters[0]["row_uid"], "scene_seq": i + 1}
                        for i, s in enumerate(scenes)],
    }
    assert client.patch(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan",
                        json=payload).status_code == 200

    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/resync", json={})
    assert response.status_code == 200, response.text
    assert not response.json()["data"].get("notice")

    session.expire_all()
    first_chapter = f"{project_id}_CH01"
    cards = list(session.execute(
        select(SceneCard).where(SceneCard.project_id == project_id)).scalars())
    assert {card.chapter_id for card in cards} == {first_chapter}


def test_preview_records_existing_membership_but_never_a_new_decision(client, session) -> None:
    """预览端点的副作用边界：只补录既成事实，绝不落这次推演算出来的归属。

    ``ensure_chapter_plans`` 会为历史项目补建章行、并把目录里已有的归属绑回构思侧
    （系统已经知道的事）。但 ``preview`` 用 spine_anchor 重算出来的那份方案不许落库——
    作者还没确认。这两件事以前混在一句「场景归属不会被这个端点改动」里，而那句是假的。
    """
    from novel_system.db.models import SnowflakeScenePlan

    project_id = _materialized_project(client, "preview-sideeffect")

    # 抹掉构思侧章行与归属，模拟「目录里有章、构思侧没有」的历史项目
    session.expire_all()
    for row in session.execute(select(SnowflakeChapterPlan).where(
            SnowflakeChapterPlan.project_id == project_id)).scalars():
        session.delete(row)
    for plan in session.execute(select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id)).scalars():
        plan.chapter_plan_id = None
    session.commit()

    catalog = {row.chapter_id: row for row in session.execute(
        select(ChapterGoal).where(ChapterGoal.project_id == project_id)).scalars()}
    before = {plan.scene_plan_id: plan.chapter_id for plan in session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars()}

    assert client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
                       json={"strategy": "even"}).status_code == 200

    session.expire_all()
    plans = list(session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars())
    # 补录：每一场都绑回了它在目录里本来就在的那一章
    assert all(plan.chapter_plan_id for plan in plans)
    assert {plan.scene_plan_id: plan.chapter_id for plan in plans} == before, \
        "预览把重算出来的方案落库了——作者还没确认"
    assert all(plan.chapter_id in catalog for plan in plans)


def test_a_failed_ai_suggestion_leaves_no_chaptering_behind(client, session) -> None:
    """「AI 建议」失败必须干净回滚——不能留下一份作者从没确认过的章表。"""
    project_id = _create_project(client, "suggest-rollback")
    _seed(client, project_id)
    session.expire_all()
    for row in session.execute(select(SnowflakeChapterPlan).where(
            SnowflakeChapterPlan.project_id == project_id)).scalars():
        session.delete(row)
    session.commit()

    response = client.post(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/suggest", json={})
    assert response.status_code == 409  # fail-closed：没有可用 LLM

    session.expire_all()
    rows = list(session.execute(select(SnowflakeChapterPlan).where(
        SnowflakeChapterPlan.project_id == project_id)).scalars())
    assert rows == [], "建议失败了，章表却留了下来"
