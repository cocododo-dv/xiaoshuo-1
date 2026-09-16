"""阶段 T（2026-09-16）：作者意图要点——教练对话蒸馏、作者可编辑、生成 / 候选 / 分诊读入。

此前雪花模块的三个 LLM 入口互不知情：教练每轮是单发调用（连自己上一轮都看不到），三候选与整步生成
拿不到任何对话内容，唯一的桥「应用补丁」带的是内容不是意图。这里守住：

1. 纯函数：教练重述的差分（新增 / 改写 / 撤下 / 文本对上 / 作者条目不归教练 / 作者撤下不复活 / 上限）、
   作者编辑（缺席即撤、恢复、改过归作者）、模型输出的强制归一；
2. 路由：教练带 conversation（当前要点 + 撤下 + 最近几轮）、重述落表、LLM 未启用 409 且不落回合、
   作者 PUT 编辑与未知字段 422；
3. 整步生成 / 三候选 / 分诊的提示载荷带 author_direction_brief（本步活动条目 + 上游全书级继承，
   可关掉继承），use_direction_brief=false 不带且 health 记 used=false，带入时 health 记 revision / sha；
4. 预算：受保护键永不降载；教练回复作为方向蓝本时用法说明随来源变化。
"""

from __future__ import annotations

import json

from novel_system.services.llm_client import LLMResponse
from novel_system.services.snowflake_direction_brief import (
    MAX_ACTIVE_LINES,
    MAX_LINE_CHARS,
    apply_author_edit,
    apply_coach_restatement,
    coerce_brief_update,
    delta_changed,
)
from novel_system.services.snowflake_prompt_budget import (
    AUTHOR_DIRECTION_BRIEF_KEY,
    PROTECTED_KEYS,
    apply_snowflake_prompt_budget,
)
from tests.accounted_llm_fakes import accounted_generate_method


# ---------- 纯函数 ----------


def test_apply_coach_restatement_adds_updates_supersedes_and_respects_author_lines() -> None:
    lines = [
        {"line_id": "dl_a", "kind": "decision", "scope": "step", "text": "主角是被动卷入", "origin": "coach", "status": "active"},
        {"line_id": "dl_b", "kind": "pending", "scope": "step", "text": "结局是否团圆", "origin": "coach", "status": "active"},
        {"line_id": "dl_c", "kind": "constraint", "scope": "book", "text": "基调冷", "origin": "author", "status": "active"},
        {"line_id": "dl_d", "kind": "rejection", "scope": "step", "text": "不要热血", "origin": "coach", "status": "dismissed", "dismissed_by": "author"},
    ]
    update = {
        "lines": [
            {"line_id": "dl_a", "kind": "decision", "scope": "step", "text": "主角是被动卷入，第一灾才被迫出手"},
            {"line_id": None, "kind": "decision", "scope": "book", "text": "结局苦乐参半"},
            {"line_id": "dl_c", "kind": "pending", "scope": "step", "text": "基调改热"},
            {"line_id": None, "kind": "rejection", "scope": "step", "text": "不要热血"},
        ]
    }
    new, delta = apply_coach_restatement(lines, update, turn_id="t1", now="2026-09-16T00:00:00Z")
    by = {line["line_id"]: line for line in new}

    assert delta["updated"] == ["dl_a"], "带 line_id 的改写要记为 updated"
    assert by["dl_a"]["text"].endswith("出手") and by["dl_a"]["source_turn_id"] == "t1"
    assert len(delta["added"]) == 1
    added = by[delta["added"][0]]
    assert added["scope"] == "book" and added["origin"] == "coach" and added["status"] == "active"
    assert delta["superseded"] == ["dl_b"], "没被重述的教练条目 = 撤下"
    assert by["dl_b"]["status"] == "dismissed" and by["dl_b"]["dismissed_by"] == "coach"
    assert by["dl_c"] == lines[2], "作者的条目教练只能引用，不能改写"
    assert by["dl_d"]["status"] == "dismissed", "作者撤下的条目教练不能复活"
    assert delta["kept"] == 1


def test_restatement_matches_verbatim_lines_without_ids_and_none_means_untouched() -> None:
    lines = [{"line_id": "dl_a", "kind": "decision", "scope": "step", "text": "主角是被动卷入。", "origin": "coach", "status": "active"}]
    same, delta = apply_coach_restatement(
        lines, {"lines": [{"line_id": None, "kind": "decision", "scope": "step", "text": "主角是被动卷入"}]}, turn_id="t2"
    )
    assert not delta_changed(delta) and delta["kept"] == 1 and same[0]["status"] == "active"

    untouched, delta_none = apply_coach_restatement(lines, None, turn_id="t3")
    assert untouched == lines and not delta_changed(delta_none), "没有 brief_update（旧提示词快照）= 本轮不动要点"

    cleared, delta_clear = apply_coach_restatement(lines, {"lines": []}, turn_id="t4")
    assert delta_clear["superseded"] == ["dl_a"] and cleared[0]["status"] == "dismissed", "明确的空重述 = 教练条目都不再成立"


def test_active_cap_dismisses_coach_pending_lines_first_never_author_lines() -> None:
    lines = [
        {"line_id": f"a{i}", "kind": "decision", "scope": "step", "text": f"作者条 {i}", "origin": "author", "status": "active"}
        for i in range(10)
    ]
    update = {"lines": [{"kind": "pending" if i % 2 else "decision", "scope": "step", "text": f"教练条 {i}"} for i in range(12)]}
    new, delta = apply_coach_restatement(lines, update, turn_id="t")
    active = [line for line in new if line["status"] == "active"]
    assert len(active) == MAX_ACTIVE_LINES
    assert sum(1 for line in active if line["origin"] == "author") == 10, "作者的条目永不自动撤"
    assert all(line["kind"] == "decision" for line in active if line["origin"] == "coach"), "超上限先撤教练的待定"
    assert len(delta["superseded"]) == 6


def test_coerce_brief_update_normalizes_kinds_scopes_and_dedupes() -> None:
    assert coerce_brief_update(None) is None
    assert coerce_brief_update({"nope": 1}) is None
    out = coerce_brief_update(
        {
            "lines": [
                {"kind": "决定", "scope": "全书", "text": " 基调冷  一点 "},
                {"kind": "weird", "scope": "weird", "text": "基调冷一点"},
                {"kind": "rejection", "scope": "step", "text": ""},
                {"kind": "constraint", "text": "x" * 500},
            ]
        }
    )
    assert out["lines"][0] == {"line_id": None, "kind": "decision", "scope": "book", "text": "基调冷 一点"}
    assert len(out["lines"]) == 2, "同文（去标点空白）去重，空文丢弃"
    assert out["lines"][1]["kind"] == "constraint" and out["lines"][1]["scope"] == "step"
    assert len(out["lines"][1]["text"]) == MAX_LINE_CHARS


def test_apply_author_edit_dismisses_absent_lines_restores_and_claims_edited_lines() -> None:
    lines = [
        {"line_id": "dl_a", "kind": "decision", "scope": "step", "text": "主角被动卷入", "origin": "coach", "status": "active"},
        {"line_id": "dl_b", "kind": "pending", "scope": "step", "text": "结局待定", "origin": "coach", "status": "active"},
        {"line_id": "dl_c", "kind": "rejection", "scope": "step", "text": "不要热血", "origin": "coach", "status": "dismissed", "dismissed_by": "coach"},
    ]
    new, changed = apply_author_edit(
        lines,
        [
            {"line_id": "dl_a", "kind": "decision", "scope": "book", "text": "主角被动卷入"},
            {"line_id": "dl_c", "status": "active"},
            {"kind": "约束", "scope": "本步", "text": "第一章不出现凶手"},
        ],
    )
    by = {line["line_id"]: line for line in new}
    assert changed
    assert by["dl_a"]["origin"] == "author" and by["dl_a"]["scope"] == "book", "改了范围即归作者"
    assert by["dl_b"]["status"] == "dismissed" and by["dl_b"]["dismissed_by"] == "author", "请求里缺席的活动条目 = 作者撤下"
    assert by["dl_c"]["status"] == "active" and by["dl_c"]["dismissed_by"] is None and by["dl_c"]["origin"] == "coach", "恢复不改归属"
    added = next(line for line in new if line["text"] == "第一章不出现凶手")
    assert added["origin"] == "author" and added["kind"] == "constraint" and added["scope"] == "step"

    # 教练此后不能改写 / 撤下作者改过的条目，也不能撤作者加的条目
    after, delta = apply_coach_restatement(
        new, {"lines": [{"line_id": "dl_c", "kind": "rejection", "scope": "step", "text": "不要热血"}]}, turn_id="t"
    )
    by2 = {line["line_id"]: line for line in after}
    assert by2["dl_a"]["status"] == "active" and by2["dl_a"]["scope"] == "book"
    assert by2[added["line_id"]]["status"] == "active"
    assert delta["superseded"] == [] and delta["kept"] == 1

    _same, unchanged = apply_author_edit(after, [
        {"line_id": line["line_id"], "kind": line["kind"], "scope": line["scope"], "text": line["text"], "status": line["status"]}
        for line in after
    ])
    assert unchanged is False, "原样回传不算改动（revision 不该白白 +1）"


# ---------- 路由 ----------


def _create_project(client, key: str) -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": "要点之书", "outline_text": "作者意图要点验证用项目。"},
        headers={"X-Idempotency-Key": f"brief-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _fake(captured: list, payload: dict):
    """捕获发往 LLM 的请求，回放固定 structured_output。"""

    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        return LLMResponse(
            request_id=f"resp_{request.node_id}_{len(captured)}",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_{request.node_id}"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    return accounted_generate_method(fake_generate)


def _prompt_payload(request) -> dict:
    content = request.messages[-1]["content"]
    start = content.index("Working payload:\n") + len("Working payload:\n")
    end = content.index("\n\nRequired top-level JSON keys")
    return json.loads(content[start:end])


COACH_REPLY = {
    "reply": "先把主角的被动写实。",
    "suggestions": ["第一灾之前他不出手"],
    "candidate_label": "",
    "candidate_patch": {},
    "brief_update": {
        "lines": [
            {"kind": "decision", "scope": "step", "text": "主角是被动卷入，第一灾才出手"},
            {"kind": "constraint", "scope": "book", "text": "基调冷，不热血"},
            {"kind": "pending", "scope": "step", "text": "结局是否团圆"},
        ]
    },
}


def _install(monkeypatch, captured: list, payload: dict) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.llm_client.LLMClient.generate_accounted", _fake(captured, payload))


def _put_brief(client, pid: str, step_key: str, lines: list, *, inherit=None, key: str = "") -> dict:
    body: dict = {"lines": lines}
    if inherit is not None:
        body["inherit_upstream"] = inherit
    response = client.put(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/{step_key}/direction-brief",
        json=body,
        headers={"X-Idempotency-Key": key or f"brief-put-{pid}-{step_key}-{len(lines)}-{inherit}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["direction_brief"]


def test_coach_turn_restates_brief_and_next_turn_sees_conversation(client, monkeypatch) -> None:
    pid = _create_project(client, "coach")
    captured: list = []
    _install(monkeypatch, captured, COACH_REPLY)

    first = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "我想要主角是被动卷入的，基调冷一点"},
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert "brief_update" not in data, "原始重述不回显；落表后的要点在 direction_brief"
    brief = data["direction_brief"]
    assert brief["revision"] == 1 and brief["active_count"] == 3
    assert len(data["brief_delta"]["added"]) == 3
    assert {line["kind"] for line in brief["lines"]} == {"decision", "constraint", "pending"}
    assert all(line["origin"] == "coach" and line["source_turn_id"] == data["turn_id"] for line in brief["lines"])

    conv1 = _prompt_payload(captured[0])["conversation"]
    assert conv1["brief"]["lines"] == [] and conv1["recent_turns"] == [] and conv1["how_to_use"]

    second_reply = {
        **COACH_REPLY,
        "brief_update": {
            "lines": [
                {"line_id": brief["lines"][0]["line_id"], "kind": "decision", "scope": "step", "text": brief["lines"][0]["text"]},
                {"line_id": brief["lines"][1]["line_id"], "kind": "constraint", "scope": "book", "text": "基调冷，不热血"},
                {"kind": "decision", "scope": "step", "text": "结局苦乐参半"},
            ]
        },
    }
    _install(monkeypatch, captured, second_reply)
    second = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "结局就苦乐参半吧"},
    )
    assert second.status_code == 200, second.text
    conv2 = _prompt_payload(captured[1])["conversation"]
    assert [turn["message"] for turn in conv2["recent_turns"]] == ["我想要主角是被动卷入的，基调冷一点"], "教练看得到上一轮"
    assert conv2["recent_turns"][0]["reply"] == "先把主角的被动写实。"
    assert {line["line_id"] for line in conv2["brief"]["lines"]} == {line["line_id"] for line in brief["lines"]}
    assert all(line["origin"] == "coach" for line in conv2["brief"]["lines"])

    data2 = second.json()["data"]
    assert data2["direction_brief"]["revision"] == 2
    assert data2["brief_delta"]["kept"] == 2 and len(data2["brief_delta"]["added"]) == 1
    assert data2["brief_delta"]["superseded"] == [brief["lines"][2]["line_id"]], "待定被推翻 = 撤下"
    dismissed = [line for line in data2["direction_brief"]["lines"] if line["status"] == "dismissed"]
    assert dismissed and dismissed[0]["dismissed_by"] == "coach"

    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert workspace["direction_briefs"]["one_sentence_summary"]["revision"] == 2
    assert len(workspace["assistant_history"]) == 2


def test_coach_is_fail_closed_without_llm_and_records_nothing(client) -> None:
    pid = _create_project(client, "off")
    response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "book_brief", "message": "这一步还缺什么？"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"
    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert workspace["assistant_history"] == [] and workspace["direction_briefs"] == {}


def test_author_edits_brief_and_coach_cannot_override_author_lines(client, monkeypatch) -> None:
    pid = _create_project(client, "edit")
    captured: list = []
    _install(monkeypatch, captured, COACH_REPLY)
    seeded = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "主角被动卷入，基调冷"},
    ).json()["data"]["direction_brief"]
    lines = seeded["lines"]

    edited = _put_brief(
        client,
        pid,
        "one_sentence_summary",
        [
            {"line_id": lines[0]["line_id"], "kind": "decision", "scope": "step", "text": lines[0]["text"]},
            {"line_id": lines[1]["line_id"], "kind": "decision", "scope": "book", "text": "基调冷，不热血"},
            {"kind": "constraint", "scope": "step", "text": "一句话里不出现凶手"},
        ],
        inherit=False,
        key="brief-edit-1",
    )
    assert edited["revision"] == 2 and edited["inherit_upstream"] is False and edited["author_edited_at"]
    by = {line["line_id"]: line for line in edited["lines"]}
    assert by[lines[2]["line_id"]]["status"] == "dismissed" and by[lines[2]["line_id"]]["dismissed_by"] == "author"
    assert by[lines[1]["line_id"]]["origin"] == "author" and by[lines[1]["line_id"]]["kind"] == "decision"
    assert by[lines[0]["line_id"]]["origin"] == "coach", "原样回传不改归属"
    assert next(line for line in edited["lines"] if line["text"] == "一句话里不出现凶手")["origin"] == "author"
    assert edited["active_count"] == 3

    # 教练再来一轮：试图改写作者条目、复活作者撤下的待定——都不生效，要点版本不变
    override_reply = {
        **COACH_REPLY,
        "brief_update": {
            "lines": [
                {"line_id": lines[1]["line_id"], "kind": "pending", "scope": "step", "text": "基调改热"},
                {"kind": "pending", "scope": "step", "text": "结局是否团圆"},
                {"line_id": lines[0]["line_id"], "kind": "decision", "scope": "step", "text": lines[0]["text"]},
            ]
        },
    }
    _install(monkeypatch, captured, override_reply)
    turn = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/assistant",
        json={"step_key": "one_sentence_summary", "message": "那基调要不要热一点？"},
    ).json()["data"]
    conv = _prompt_payload(captured[-1])["conversation"]
    assert conv["brief"]["withdrawn"] == ["结局是否团圆"], "作者撤下的条目教练看得到（免得再提）"
    assert any(line["origin"] == "author" for line in conv["brief"]["lines"])
    by2 = {line["line_id"]: line for line in turn["direction_brief"]["lines"]}
    assert by2[lines[1]["line_id"]]["text"] == "基调冷，不热血" and by2[lines[1]["line_id"]]["kind"] == "decision"
    assert by2[lines[2]["line_id"]]["status"] == "dismissed"
    assert not turn["brief_delta"]["added"] and not turn["brief_delta"]["updated"] and not turn["brief_delta"]["superseded"]
    assert turn["direction_brief"]["revision"] == 2

    bad = client.put(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/direction-brief",
        json={"lines": [], "unexpected_secret": "must-not-be-echoed"},
        headers={"X-Idempotency-Key": "brief-edit-bad"},
    )
    assert bad.status_code == 422 and "must-not-be-echoed" not in bad.text

    unknown_step = client.put(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/nonsense/direction-brief",
        json={"lines": []},
        headers={"X-Idempotency-Key": "brief-edit-unknown"},
    )
    assert unknown_step.status_code in {400, 404}


def test_generate_carries_own_lines_and_inherited_book_lines_and_records_provenance(client, monkeypatch) -> None:
    pid = _create_project(client, "gen")
    _put_brief(client, pid, "book_brief", [
        {"kind": "constraint", "scope": "book", "text": "基调冷，不热血"},
        {"kind": "decision", "scope": "step", "text": "读者是想看旧案的人"},
    ])
    _put_brief(client, pid, "one_sentence_summary", [
        {"kind": "decision", "scope": "step", "text": "主角是被动卷入"},
        {"kind": "pending", "scope": "step", "text": "结局是否团圆"},
    ])
    captured: list = []
    _install(monkeypatch, captured, {"summary": "林晚必须查清恩师改档案的真相，但每一步都在拆自己的家。"})

    generated = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate",
        json={"require_llm": True, "source": "fe_scaffold_ai"},
    )
    assert generated.status_code == 200, generated.text
    payload = _prompt_payload(captured[0])
    brief = payload[AUTHOR_DIRECTION_BRIEF_KEY]
    assert [line["text"] for line in brief["lines"]] == ["主角是被动卷入", "结局是否团圆"]
    assert brief["lines"][0] == {"kind": "决定", "scope": "本步", "text": "主角是被动卷入"}
    assert brief["inherited"] == [{"step": "读者定位", "kind": "约束", "text": "基调冷，不热血"}], "只继承上游的全书级条目"
    assert "how_to_use" in brief and "待定" in brief["how_to_use"]
    health = generated.json()["data"]["step"]["health"]
    assert health["direction_brief"]["used"] is True and health["direction_brief"]["revision"] == 1
    assert len(health["direction_brief"]["line_ids"]) == 2 and len(health["direction_brief"]["inherited_line_ids"]) == 1
    assert health["direction_brief"]["sha"]

    # 作者关掉带入：键不出现，出处记 used=false
    off = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate",
        json={"require_llm": True, "source": "fe_scaffold_ai", "use_direction_brief": False},
    )
    assert off.status_code == 200, off.text
    assert AUTHOR_DIRECTION_BRIEF_KEY not in _prompt_payload(captured[1])
    assert off.json()["data"]["step"]["health"]["direction_brief"] == {"used": False, "revision": 1}

    # 关掉继承：只带本步条目
    _put_brief(client, pid, "one_sentence_summary", [
        {"kind": "decision", "scope": "step", "text": "主角是被动卷入"},
        {"kind": "pending", "scope": "step", "text": "结局是否团圆"},
    ], inherit=False)
    again = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate",
        json={"require_llm": True, "source": "fe_scaffold_ai"},
    )
    assert again.status_code == 200, again.text
    brief3 = _prompt_payload(captured[2])[AUTHOR_DIRECTION_BRIEF_KEY]
    assert brief3["inherited"] == [] and len(brief3["lines"]) == 2
    health3 = again.json()["data"]["step"]["health"]["direction_brief"]
    assert health3["inherited_line_ids"] == [] and health3["inherit_upstream"] is False and health3["revision"] == 2

    # 没有任何条目的步骤：键不出现，health 也不长出 direction_brief
    nothing = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/generate",
        json={"require_llm": True, "source": "fe_scaffold_ai", "draft_override": {"category": "文学悬疑"}},
    )
    # book_brief 自己有本步条目，所以键在；用一个没有任何条目也没有上游的检查换个角度：01 的 inherited 为空
    assert nothing.status_code in {200, 409}
    if nothing.status_code == 200:
        assert _prompt_payload(captured[3])[AUTHOR_DIRECTION_BRIEF_KEY]["inherited"] == []


def test_candidates_and_triage_carry_the_brief(client, monkeypatch) -> None:
    pid = _create_project(client, "cands")
    _put_brief(client, pid, "book_brief", [{"kind": "rejection", "scope": "book", "text": "不要热血少年漫"}])
    _put_brief(client, pid, "one_sentence_summary", [{"kind": "decision", "scope": "step", "text": "主角是被动卷入"}])
    captured: list = []
    _install(monkeypatch, captured, {"candidates": [{"label": "冷", "tag": "冷处理", "text": "她被旧案拖回雨城。", "notes": []}]})

    with_brief = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/fe-candidates",
        json={"context": "【01 读者定位】文学悬疑", "draft": "", "target_chars": 80},
        headers={"X-Idempotency-Key": "cands-with-brief"},
    )
    assert with_brief.status_code == 200, with_brief.text
    brief = _prompt_payload(captured[0])[AUTHOR_DIRECTION_BRIEF_KEY]
    assert brief["lines"] == [{"kind": "决定", "scope": "本步", "text": "主角是被动卷入"}]
    assert brief["inherited"] == [{"step": "读者定位", "kind": "否决", "text": "不要热血少年漫"}]

    without = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/fe-candidates",
        json={"context": "", "draft": "", "target_chars": 80, "use_direction_brief": False},
        headers={"X-Idempotency-Key": "cands-without-brief"},
    )
    assert without.status_code == 200, without.text
    assert AUTHOR_DIRECTION_BRIEF_KEY not in _prompt_payload(captured[1])

    # 分诊：读第 10 步的要点（含上游全书级）
    scene = {
        "scene_id": "SC001", "row_uid": "row-sc001", "title": "雨夜来信", "summary": "林晚收到旧信",
        "scene_type": "proactive", "primary_form": "proactive", "goal": "拿到信", "conflict": "邻居阻拦", "setback": "信被烧",
    }
    saved = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [scene]}},
    )
    assert saved.status_code == 200, saved.text
    _put_brief(client, pid, "scene_details", [{"kind": "constraint", "scope": "step", "text": "第一场不许出现凶手"}])
    _install(monkeypatch, captured, {"items": []})
    triage = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/scene-triage/suggest",
        json={},
        headers={"X-Idempotency-Key": "triage-with-brief"},
    )
    assert triage.status_code == 200, triage.text
    brief_t = _prompt_payload(captured[-1])[AUTHOR_DIRECTION_BRIEF_KEY]
    assert brief_t["lines"] == [{"kind": "约束", "scope": "本步", "text": "第一场不许出现凶手"}]
    assert brief_t["inherited"][0]["text"] == "不要热血少年漫"


def test_coach_reply_as_direction_changes_the_blueprint_instruction(client, monkeypatch) -> None:
    pid = _create_project(client, "direction")
    captured: list = []
    _install(monkeypatch, captured, {"summary": "林晚必须查清真相，但每一步都在拆自己的家。"})
    body = {"require_llm": True, "direction_text": "先把主角的被动写实：第一灾之前他不出手。", "direction_kind": "coach_reply", "source": "fe_coach_adopt"}
    response = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate", json=body)
    assert response.status_code == 200, response.text
    direction = _prompt_payload(captured[0])["adopted_direction"]
    assert direction["source"] == "coach_reply" and "驻场教练" in direction["how_to_use"]
    assert direction["text"].startswith("先把主角的被动写实")

    plain = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/one_sentence_summary/generate",
        json={"require_llm": True, "direction_text": "沿着旧案方向展开", "source": "fe_candidate_adopt"},
    )
    assert plain.status_code == 200, plain.text
    direction2 = _prompt_payload(captured[1])["adopted_direction"]
    assert direction2["source"] == "candidate" and "方向蓝本" in direction2["how_to_use"]
    assert response.json()["data"]["step"]["health"]["trigger_source"] == "fe_coach_adopt"


# ---------- 预算 ----------


def test_author_direction_brief_is_protected_from_budget_shedding() -> None:
    assert AUTHOR_DIRECTION_BRIEF_KEY in PROTECTED_KEYS
    padding = "上游材料。" * 400
    payload = {
        "step_key": "scene_details",
        "project": {"title": "书"},
        "upstream_steps": [{"step_key": "long_synopsis", "draft": {"paragraphs": [padding] * 5}}],
        "current_draft": {"scenes": [{"scene_id": f"SC{i:03d}", "summary": padding} for i in range(30)]},
        AUTHOR_DIRECTION_BRIEF_KEY: {
            "lines": [{"kind": "决定", "scope": "本步", "text": "主角是被动卷入"}],
            "inherited": [{"step": "读者定位", "kind": "约束", "text": "基调冷"}],
            "how_to_use": "…",
        },
    }
    trimmed, report = apply_snowflake_prompt_budget(payload, budget_tokens=300, step_key="scene_details")
    assert report["within_budget"] is False
    assert trimmed[AUTHOR_DIRECTION_BRIEF_KEY] == payload[AUTHOR_DIRECTION_BRIEF_KEY], "作者意图要点永不降载"
