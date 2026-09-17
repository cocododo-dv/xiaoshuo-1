"""阶段 U（2026-09-17）：「先看 3 个方向」是教练日志里的一种回合，方向与教练回复都可「按此生成本步」。

守住的契约：
- fe-candidates 底稿与 generate / assistant 同源（draft_override），作者要求（ask）与第 10 步聚焦场进提示；
  结果落成 turn_kind=candidates 的回合，回包与工作台都带整条教练历史；模型给不出方向 → 502，不写回合。
- generate 带 direction_turn_id（+ direction_index）：回合记 adoption，这一版 health.direction 记出处，
  方向回合 → adopted_direction.source=candidate，教练回复回合 → coach_reply；编号 / 回合不对就拒绝。
- 教练下一轮的 conversation.recent_turns 看得到方向回合（directions + chosen）与「已按此生成」的回复。
- 教练每轮的要点差异随回合落表（brief_delta）。
"""

from __future__ import annotations

from tests.test_snowflake_direction_brief import COACH_REPLY, _create_project, _install, _prompt_payload

DIRECTIONS = {
    "candidates": [
        {"label": "冷处理", "tag": "情绪压强", "text": "她被旧案拖回雨城，谁也不肯先开口。", "notes": ["锁定代价"]},
        {"label": "推进向", "tag": "情节推进", "text": "一封信逼她在三天内回乡。", "notes": []},
        {"label": "对照向", "tag": "道德对照", "text": "她替恩师撒的谎，如今要她自己付账。", "notes": []},
    ]
}


def _candidates(client, pid: str, body: dict, key: str) -> dict:
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/fe-candidates",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_candidates_become_a_coach_turn_with_ask_and_local_draft(client, monkeypatch) -> None:
    pid = _create_project(client, "dir-turn")
    captured: list = []
    _install(monkeypatch, captured, DIRECTIONS)

    data = _candidates(client, pid, {
        "ask": "都要更冷一点，主角不能主动出手",
        "draft_override": {"summary": "她回到雨城。"},
        "target_chars": 80,
    }, "dir-turn-1")
    assert [c["label"] for c in data["candidates"]] == ["冷处理", "推进向", "对照向"]
    assert data["source"] == "llm" and data["turn_id"]
    turn = data["turn"]
    assert turn["turn_kind"] == "candidates" and turn["message"] == "都要更冷一点，主角不能主动出手"
    assert [c["label"] for c in turn["candidates"]] == ["冷处理", "推进向", "对照向"]
    assert turn["adoption"] is None and turn["reply"] == ""
    assert [t["turn_id"] for t in data["assistant_history"]] == [turn["turn_id"]]

    payload = _prompt_payload(captured[0])
    assert payload["author_ask"]["text"] == "都要更冷一点，主角不能主动出手" and payload["author_ask"]["how_to_use"]
    assert payload["current_canonical_draft"]["summary"] == "她回到雨城。", "本地最新草稿盖在存档上"
    assert "fe_local_context" not in payload and "current_draft_text" not in payload

    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert workspace["assistant_history"][0]["turn_kind"] == "candidates"

    # 没带要求：回合里的「我」这一行是默认句
    plain = _candidates(client, pid, {"target_chars": 80}, "dir-turn-2")
    assert plain["turn"]["message"] == "给我 3 个不同方向"
    assert "author_ask" not in _prompt_payload(captured[1])


def test_empty_directions_are_a_502_and_write_no_turn(client, monkeypatch) -> None:
    pid = _create_project(client, "dir-empty")
    captured: list = []
    _install(monkeypatch, captured, {"candidates": []})
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/fe-candidates",
        json={"target_chars": 80},
        headers={"X-Idempotency-Key": "dir-empty-1"},
    )
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "SNOWFLAKE_CANDIDATES_EMPTY"
    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert workspace["assistant_history"] == []


def test_generate_along_a_direction_marks_the_turn_and_the_version_and_the_coach_sees_it(client, monkeypatch) -> None:
    pid = _create_project(client, "dir-adopt")
    captured: list = []
    _install(monkeypatch, captured, DIRECTIONS)
    turn_id = _candidates(client, pid, {"target_chars": 80}, "dir-adopt-1")["turn_id"]

    _install(monkeypatch, captured, {"summary": "一封信逼她在三天内回乡，替恩师的谎付账。"})
    generated = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate",
        json={
            "require_llm": True, "source": "fe_direction_adopt",
            "direction_text": DIRECTIONS["candidates"][1]["text"], "direction_turn_id": turn_id, "direction_index": 1,
        },
        headers={"X-Idempotency-Key": "dir-adopt-gen"},
    )
    assert generated.status_code == 200, generated.text
    step = generated.json()["data"]["step"]
    direction = step["health"]["direction"]
    assert direction["kind"] == "candidate" and direction["turn_id"] == turn_id
    assert direction["candidate_index"] == 1 and direction["label"] == "推进向" and len(direction["sha"]) == 16
    adopted = _prompt_payload(captured[-1])["adopted_direction"]
    assert adopted["source"] == "candidate" and adopted["text"] == DIRECTIONS["candidates"][1]["text"]

    history = generated.json()["data"]["workspace"]["assistant_history"]
    assert history[0]["turn_id"] == turn_id
    assert history[0]["adoption"]["candidate_index"] == 1
    assert history[0]["adoption"]["step_run_id"] == step["artifact"]["step_run_id"]

    # 教练下一轮看得到：给过哪些方向、作者选了哪个
    _install(monkeypatch, captured, COACH_REPLY)
    coached = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "就按推进向写，再冷一点"},
    )
    assert coached.status_code == 200, coached.text
    recent = _prompt_payload(captured[-1])["conversation"]["recent_turns"]
    assert recent[0]["kind"] == "candidates" and recent[0]["chosen"] == "推进向"
    assert [d["label"] for d in recent[0]["directions"]] == ["冷处理", "推进向", "对照向"]
    assert "chosen" in _prompt_payload(captured[-1])["conversation"]["how_to_use"]

    # 教练回复本身也可以是方向：回合记 adoption，教练之后看到 adopted_as_direction
    coach_turn_id = coached.json()["data"]["turn_id"]
    _install(monkeypatch, captured, {"summary": "先把主角的被动写实。"})
    again = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate",
        json={"require_llm": True, "direction_text": "先把主角的被动写实。", "direction_turn_id": coach_turn_id},
        headers={"X-Idempotency-Key": "dir-adopt-coach"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["step"]["health"]["direction"] == {
        "kind": "coach_reply", "sha": again.json()["data"]["step"]["health"]["direction"]["sha"],
        "turn_id": coach_turn_id, "candidate_index": None, "label": None,
    }
    assert _prompt_payload(captured[-1])["adopted_direction"]["source"] == "coach_reply"
    _install(monkeypatch, captured, COACH_REPLY)
    client.post(f"/api/v2/projects/{pid}/snowflake-workspace/assistant", json={"step_key": "one_sentence_summary", "message": "还有呢"})
    recent = _prompt_payload(captured[-1])["conversation"]["recent_turns"]
    assert recent[-1]["kind"] == "chat" and recent[-1]["adopted_as_direction"] is True
    assert recent[0]["kind"] == "candidates" and recent[0]["chosen"] == "推进向"


def test_direction_turn_must_exist_and_index_must_fit(client, monkeypatch) -> None:
    pid = _create_project(client, "dir-bad")
    captured: list = []
    _install(monkeypatch, captured, DIRECTIONS)
    turn_id = _candidates(client, pid, {"target_chars": 80}, "dir-bad-1")["turn_id"]
    url = f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate"

    bad_index = client.post(url, json={"require_llm": True, "direction_text": "x", "direction_turn_id": turn_id, "direction_index": 7},
                            headers={"X-Idempotency-Key": "dir-bad-index"})
    assert bad_index.status_code == 400 and bad_index.json()["error"]["code"] == "SNOWFLAKE_DIRECTION_INDEX_INVALID"

    missing = client.post(url, json={"require_llm": True, "direction_text": "x", "direction_turn_id": "snowflake_assistant_turn_nope"},
                          headers={"X-Idempotency-Key": "dir-bad-missing"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "SNOWFLAKE_DIRECTION_TURN_NOT_FOUND"

    no_text = client.post(url, json={"require_llm": True, "direction_turn_id": turn_id, "direction_index": 0},
                          headers={"X-Idempotency-Key": "dir-bad-text"})
    assert no_text.status_code == 400 and no_text.json()["error"]["code"] == "SNOWFLAKE_DIRECTION_TEXT_REQUIRED"

    # 另一本书的回合不能拿来当方向来源
    other = _create_project(client, "dir-bad-other")
    foreign = client.post(f"/api/v2/projects/{other}/snowflake-workspace/steps/one_sentence_summary/generate",
                          json={"require_llm": True, "direction_text": "x", "direction_turn_id": turn_id, "direction_index": 0},
                          headers={"X-Idempotency-Key": "dir-bad-foreign"})
    assert foreign.status_code == 404
    # 没有一次生成成功：回合没有 adoption
    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert workspace["assistant_history"][0]["adoption"] is None


def test_scene_planning_directions_focus_on_the_selected_scene(client, monkeypatch) -> None:
    pid = _create_project(client, "dir-focus")
    scene = {
        "scene_id": "SC001", "row_uid": "row-sc001", "title": "雨夜来信", "summary": "林晚收到旧信",
        "scene_type": "proactive", "primary_form": "proactive", "goal": "拿到信", "conflict": "邻居阻拦", "setback": "信被烧",
    }
    saved = client.patch(f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details", json={"draft": {"scenes": [scene]}})
    assert saved.status_code == 200, saved.text
    captured: list = []
    _install(monkeypatch, captured, DIRECTIONS)
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details/fe-candidates",
        json={"target_chars": 120, "focus_scene_id": "row-sc001"},
        headers={"X-Idempotency-Key": "dir-focus-1"},
    )
    assert response.status_code == 200, response.text
    payload = _prompt_payload(captured[0])
    assert payload["focus_scene_id"] == "row-sc001" and payload["focus_scene"]["title"] == "雨夜来信"
    assert response.json()["data"]["turn"]["focus_scene_id"] == "row-sc001"


def test_coach_turn_keeps_its_brief_delta_in_the_log(client, monkeypatch) -> None:
    pid = _create_project(client, "dir-delta")
    captured: list = []
    _install(monkeypatch, captured, COACH_REPLY)
    first = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "主角被动卷入，基调冷"},
    )
    assert first.status_code == 200, first.text
    turn = first.json()["data"]["assistant_history"][-1]
    assert turn["turn_kind"] == "chat" and turn["candidates"] == []
    assert len(turn["brief_delta"]["added"]) == 3 and turn["brief_delta"]["superseded"] == []
    # 原样重述（什么都没改）的一轮不写差异
    restate = {**COACH_REPLY, "brief_update": {"lines": [
        {"line_id": line["line_id"], "kind": line["kind"], "scope": line["scope"], "text": line["text"]}
        for line in first.json()["data"]["direction_brief"]["lines"]
    ]}}
    _install(monkeypatch, captured, restate)
    second = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "只是问问"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["assistant_history"][-1]["brief_delta"] is None
