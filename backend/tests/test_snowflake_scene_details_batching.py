"""场景规划（scene_details）整表生成的分批深化契约。

回归三个真实故障——它们的共同结果都是「作者点了生成、等了、付了 token，
拿回一份割裂或原地不动的草稿，而系统报告成功」：

1. 整表一次生成对任何真实体量的书都装不下。30 场的完整 Scene/Sequel 明细就已
   逼近 max_output_tokens=8192（客户端降级阶梯的上限），60-150 场的长篇必然被砍断。
   模型要么触发 LLM_RESPONSE_TRUNCATED 硬失败，要么听从「装不下就少深化几场」
   自行只做前几场——后者旧代码照单全收，落库一份有的场深、有的场空的草稿。
2. 模型自造场景编号（SC001 → S1），清洗器按 scene_id 合并后一个字都没变，
   旧代码仍标记 source=llm 并弹「已生成」。
3. 完备性修复重试盯着整表的缺口重发整表，加倍消耗且同样装不下。
"""
from __future__ import annotations

import json

import pytest

from novel_system.db.models import SnowflakeStepRun, StoryProject
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMResponse
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from novel_system.services.snowflake_workspace_llm import (
    SCENE_DETAIL_BATCH_SIZE,
    SCENE_DETAIL_MAX_BATCHES_PER_RUN,
)


def _scenes(count: int) -> list[dict]:
    return [
        {
            "row_uid": f"u{i}", "scene_id": f"SC{i:03d}", "chapter_id": "CH01", "scene_seq": i,
            "summary": f"第{i}场概要", "primary_form": "proactive", "scene_type": "proactive",
            "location": "码头", "crucible": "退不出的困局",
            "pov_character_id": "c1", "chapter_role": "起疑",
        }
        for i in range(1, count + 1)
    ]


def _seed(session, project_id: str, *, scene_count: int) -> None:
    scenes = _scenes(scene_count)
    session.add(StoryProject(
        project_id=project_id, title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    for step_key in ("scene_list", "scene_details"):
        session.add(SnowflakeStepRun(
            step_run_id=f"{project_id}-{step_key}", project_id=project_id, step_key=step_key,
            version=1, status="pending_review", draft_json={"scenes": scenes},
            health_json={}, input_refs_json={},
        ))
    session.flush()


def _install_llm(monkeypatch, responder):
    from novel_system.services import snowflake_workspace_llm as mod

    monkeypatch.setattr(mod, "execute_accounted_call",
                        lambda session, client, request, context, *, llm_call_id: responder(request))
    # 记账父行由上面的桩件跳过了，清洗失败的标记路径不能反过来把真实错误吃掉
    monkeypatch.setattr(mod, "mark_postprocess_failure",
                        lambda session, llm_call_id, **kwargs: None)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod, "supplement_accounted_call", lambda session, llm_call_id, **kwargs: None)


def _payload_of(request) -> dict:
    prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
    return json.loads(prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0])


def _deep(scene_id: str) -> dict:
    """一份「深化过」的场景——每个契约字段都有实质内容。"""
    return {
        "scene_id": scene_id, "title": f"{scene_id} 标题", "summary": f"{scene_id} 概要",
        "scene_type": "proactive", "location": "码头", "scene_crucible": "困局", "crucible": "困局",
        "goal": f"{scene_id} 拿到账本", "conflict": "三轮受阻", "setback": "账本被烧",
        "cost_requirement": "失去父亲遗物", "exit_change": "退路断了", "hook": "门外脚步声",
        "target_length_band": "medium", "must_include_text": "账本", "beats_json": ["起", "承", "转"],
    }


def _respond(payload: dict) -> LLMResponse:
    return LLMResponse(
        request_id="r", provider="p", model="m", text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload, response_format="json_object", raw_response={}, usage={},
        finish_reason="stop",
    )


def _focus_ids(payload: dict) -> list[str]:
    return [s["scene_id"] for s in (payload.get("focus_scenes") or {}).get("scenes") or []]


def test_full_table_generation_is_split_into_bounded_batches(session, monkeypatch):
    """30 场必须拆成每批 6 场的定向生成，且 30 场全部深化——一次性整表装不下。"""
    calls: list[list[str]] = []

    def responder(request):
        payload = _payload_of(request)
        focus = _focus_ids(payload)
        calls.append(focus)
        assert focus, "分批后每次调用都必须是定向的（带 focus_scenes）"
        return _respond({"scenes": [_deep(scene_id) for scene_id in focus]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-batch", scene_count=30)

    result = SnowflakeWorkspaceService(session).generate_step("prj-batch", "scene_details", {})

    assert len(calls) == 5, f"30 场应拆成 5 批，实际 {len(calls)} 次调用"
    assert all(len(batch) <= SCENE_DETAIL_BATCH_SIZE for batch in calls), f"批次超限：{calls}"
    assert [sid for batch in calls for sid in batch] == [f"SC{i:03d}" for i in range(1, 31)], \
        "分批必须按场序完整覆盖每一场，不重不漏"

    scenes = result["step"]["draft"]["scenes"]
    assert len(scenes) == 30, f"落库草稿丢了场景：只剩 {len(scenes)} 场"
    undeepened = [s["scene_id"] for s in scenes if not str(s.get("goal") or "").strip()]
    assert not undeepened, f"仍有场景没被深化（割裂草稿）：{undeepened}"


def test_a_batch_only_sees_reference_detail_for_scenes_outside_its_assignment(session, monkeypatch):
    """焦外场景压成参照条目，紧邻前后场保留衔接字段——否则输入成本按批数翻倍。"""
    seen: dict = {}

    def responder(request):
        payload = _payload_of(request)
        seen["draft_scenes"] = payload["current_draft"]["scenes"]
        seen["upstream"] = payload["upstream_steps"]
        return _respond({"scenes": [dict(_deep(sid), goal="改写后的新目标") for sid in _focus_ids(payload)]})

    _install_llm(monkeypatch, responder)
    # 底稿是一份已经深化完的场景规划：焦外场景确实带着可压缩的明细
    session.add(StoryProject(
        project_id="prj-ctx", title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    deepened = [dict(base, **_deep(base["scene_id"])) for base in _scenes(30)]
    for step_key in ("scene_list", "scene_details"):
        session.add(SnowflakeStepRun(
            step_run_id=f"prj-ctx-{step_key}", project_id="prj-ctx", step_key=step_key,
            version=1, status="pending_review", draft_json={"scenes": deepened},
            health_json={}, input_refs_json={},
        ))
    session.flush()

    SnowflakeWorkspaceService(session).generate_step(
        "prj-ctx", "scene_details", {"focus_scene_refs": ["SC007"]}
    )

    by_id = {s["scene_id"]: s for s in seen["draft_scenes"]}
    assert by_id["SC007"].get("conflict"), "焦点场必须拿到全量明细"
    assert by_id["SC006"].get("setback"), "紧邻前场要保留衔接字段（上一场的挫败接出本场目标）"
    assert not by_id["SC020"].get("conflict"), "远处的焦外场景不该再带完整明细"
    assert by_id["SC020"].get("summary"), "远处焦外场景仍需保留参照条目（它是什么、接在哪）"

    # 上游的场景列表是同一份场表的另一副本，同样要压缩，否则分批把它按批数重发
    upstream = {item["step_key"]: item for item in seen["upstream"]}
    up_by_id = {s["scene_id"]: s for s in upstream["scene_list"]["draft"]["scenes"]}
    assert up_by_id["SC007"].get("crucible"), "上游场景列表里的焦点场要保留全量（pov/章内职能只在这里）"
    assert not up_by_id["SC020"].get("crucible"), "上游场景列表里的远处场景没有被压缩"
    assert up_by_id["SC020"].get("pov_character_id"), "参照条目要保留 POV，模型才知道这场是谁的视角"


def test_generation_that_matches_no_scene_fails_loudly(session, monkeypatch):
    """模型自造场景编号 → 一个字都没变 → 必须报错，不能报告「生成成功」。"""
    def responder(request):
        payload = _payload_of(request)
        focus = _focus_ids(payload) or [f"SC{i:03d}" for i in range(1, 5)]
        # 自造编号：SC001 → S1
        return _respond({"scenes": [_deep(f"S{sid.lstrip('SC0')}") for sid in focus]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-mismatch", scene_count=4)

    with pytest.raises(DomainError) as excinfo:
        SnowflakeWorkspaceService(session).generate_step("prj-mismatch", "scene_details", {})
    assert excinfo.value.code in {"SNOWFLAKE_LLM_EMPTY_GENERATION", "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA"}


def test_echoing_the_draft_verbatim_fails_loudly(session, monkeypatch):
    """模型原样复述底稿（合并后零变化）同样是空转，不能算成功。"""
    def responder(request):
        payload = _payload_of(request)
        return _respond({"scenes": payload["current_draft"]["scenes"]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-echo", scene_count=4)

    with pytest.raises(DomainError) as excinfo:
        SnowflakeWorkspaceService(session).generate_step("prj-echo", "scene_details", {})
    assert excinfo.value.code == "SNOWFLAKE_LLM_EMPTY_GENERATION"


def test_a_mid_run_batch_failure_keeps_finished_batches_and_reports_progress(session, monkeypatch):
    """中途失败：已深化的场必须留下（真花了 token），但进度要明明白白告诉作者。"""
    from novel_system.services.llm_client import LLMResponseError

    state = {"calls": 0}

    def responder(request):
        state["calls"] += 1
        if state["calls"] == 3:
            raise LLMResponseError("LLM_RESPONSE_TRUNCATED", "hit the ceiling")
        payload = _payload_of(request)
        return _respond({"scenes": [_deep(sid) for sid in _focus_ids(payload)]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-partial", scene_count=30)

    result = SnowflakeWorkspaceService(session).generate_step("prj-partial", "scene_details", {})

    scenes = result["step"]["draft"]["scenes"]
    deepened = [s["scene_id"] for s in scenes if str(s.get("goal") or "").strip()]
    assert len(deepened) == 12, f"前两批的 12 场应当保留，实际 {len(deepened)} 场"

    notice = (result["step"]["health"] or {}).get("generation_notice") or {}
    assert notice.get("code") == "SCENE_DETAILS_PARTIAL", f"中途失败没有告知作者：{result['step']['health']}"
    assert notice.get("scenes_deepened") == 12 and notice.get("scenes_remaining") == 18
    assert result["step"]["health"]["severity"] == "warning", "半成品不能报成 info"


def test_a_first_batch_failure_raises_instead_of_returning_an_untouched_draft(session, monkeypatch):
    """第一批就失败 = 什么都没完成，报错才诚实。"""
    from novel_system.services.llm_client import LLMResponseError

    def responder(request):
        raise LLMResponseError("LLM_RESPONSE_TRUNCATED", "hit the ceiling")

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-first-fail", scene_count=30)

    with pytest.raises(DomainError) as excinfo:
        SnowflakeWorkspaceService(session).generate_step("prj-first-fail", "scene_details", {})
    assert excinfo.value.code == "SNOWFLAKE_LLM_CALL_FAILED"


def test_a_small_scene_table_still_generates_in_one_call(session, monkeypatch):
    """不足一批的小书保持原来的单次整表生成——分批不是为了多花钱。"""
    calls: list[dict] = []

    def responder(request):
        payload = _payload_of(request)
        calls.append(payload)
        scenes = payload["current_draft"]["scenes"]
        return _respond({"scenes": [_deep(s["scene_id"]) for s in scenes]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-small", scene_count=SCENE_DETAIL_BATCH_SIZE)

    result = SnowflakeWorkspaceService(session).generate_step("prj-small", "scene_details", {})

    assert len(calls) == 1, f"{SCENE_DETAIL_BATCH_SIZE} 场不该被拆批，实际 {len(calls)} 次调用"
    assert not _focus_ids(calls[0]), "整表通道不该伪造 focus_scenes"
    assert all(str(s.get("goal") or "").strip() for s in result["step"]["draft"]["scenes"])


def test_a_long_scene_table_is_capped_per_run_and_reports_what_is_left(session, monkeypatch):
    """长篇不能让一次点击变成半小时的同步请求：本次封顶，剩余场次明确告诉作者。"""
    calls: list[list[str]] = []
    scene_count = SCENE_DETAIL_BATCH_SIZE * (SCENE_DETAIL_MAX_BATCHES_PER_RUN + 3)

    def responder(request):
        payload = _payload_of(request)
        focus = _focus_ids(payload)
        calls.append(focus)
        return _respond({"scenes": [_deep(sid) for sid in focus]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-cap", scene_count=scene_count)

    result = SnowflakeWorkspaceService(session).generate_step("prj-cap", "scene_details", {})

    assert len(calls) == SCENE_DETAIL_MAX_BATCHES_PER_RUN, f"单次批次数没有封顶：{len(calls)}"
    notice = (result["step"]["health"] or {}).get("generation_notice") or {}
    assert notice.get("code") == "SCENE_DETAILS_MORE_TO_GO", f"没告诉作者还有剩余：{notice}"
    assert notice["scenes_deepened"] == SCENE_DETAIL_BATCH_SIZE * SCENE_DETAIL_MAX_BATCHES_PER_RUN
    assert notice["scenes_remaining"] == scene_count - notice["scenes_deepened"]


def test_a_second_run_resumes_at_the_first_unfinished_scene(session, monkeypatch):
    """再点一次要从第一场未完成的场续深，而不是把已完成的场重做一遍。"""
    calls: list[list[str]] = []

    def responder(request):
        focus = _focus_ids(_payload_of(request))
        calls.append(focus)
        return _respond({"scenes": [_deep(sid) for sid in focus]})

    _install_llm(monkeypatch, responder)
    # 前 12 场已深化，后 18 场还空着
    session.add(StoryProject(
        project_id="prj-resume", title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    scenes = _scenes(30)
    seeded = [dict(base, **_deep(base["scene_id"])) if i < 12 else base
              for i, base in enumerate(scenes)]
    for step_key in ("scene_list", "scene_details"):
        session.add(SnowflakeStepRun(
            step_run_id=f"prj-resume-{step_key}", project_id="prj-resume", step_key=step_key,
            version=1, status="pending_review", draft_json={"scenes": seeded},
            health_json={}, input_refs_json={},
        ))
    session.flush()

    SnowflakeWorkspaceService(session).generate_step("prj-resume", "scene_details", {})

    touched = [sid for batch in calls for sid in batch]
    assert touched == [f"SC{i:03d}" for i in range(13, 31)], \
        f"续深没有从第 13 场开始（或重做了已完成的场）：{touched[:5]}…"


def test_resuming_fewer_scenes_than_one_batch_does_not_redo_the_finished_ones(session, monkeypatch):
    """只剩 3 场没深化时，不能因为「不足一批」就退回整表通道把 27 场重做一遍。"""
    calls: list[list[str]] = []

    def responder(request):
        payload = _payload_of(request)
        focus = _focus_ids(payload)
        calls.append(focus or [s["scene_id"] for s in payload["current_draft"]["scenes"]])
        return _respond({"scenes": [dict(_deep(sid), goal=f"{sid} 续深目标") for sid in focus]})

    _install_llm(monkeypatch, responder)
    session.add(StoryProject(
        project_id="prj-tail", title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    seeded = [dict(base, **_deep(base["scene_id"])) if i < 27 else base
              for i, base in enumerate(_scenes(30))]
    for step_key in ("scene_list", "scene_details"):
        session.add(SnowflakeStepRun(
            step_run_id=f"prj-tail-{step_key}", project_id="prj-tail", step_key=step_key,
            version=1, status="pending_review", draft_json={"scenes": seeded},
            health_json={}, input_refs_json={},
        ))
    session.flush()

    SnowflakeWorkspaceService(session).generate_step("prj-tail", "scene_details", {})

    assert calls == [["SC028", "SC029", "SC030"]], f"续深范围不对（疑似退回整表重做）：{calls}"


def test_generation_without_a_scene_list_fails_before_spending_a_call(session, monkeypatch):
    """没有场景可深化时不该白花一次调用换一份空草稿。"""
    calls: list = []

    def responder(request):
        calls.append(request)
        return _respond({"scenes": []})

    _install_llm(monkeypatch, responder)
    session.add(StoryProject(
        project_id="prj-empty", title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    session.flush()

    with pytest.raises(DomainError) as excinfo:
        SnowflakeWorkspaceService(session).generate_step("prj-empty", "scene_details", {})
    assert excinfo.value.code == "SNOWFLAKE_SCENE_LIST_EMPTY"
    assert excinfo.value.details.get("author_action", {}).get("step_key") == "scene_list"
    assert not calls, "空场景表不该发出 LLM 调用"


def test_single_scene_focus_is_untouched_by_batching(session, monkeypatch):
    """单场补全仍是一次定向调用，其余场景保持原样。"""
    calls: list[list[str]] = []

    def responder(request):
        payload = _payload_of(request)
        focus = _focus_ids(payload)
        calls.append(focus)
        return _respond({"scenes": [_deep(sid) for sid in focus]})

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-focus", scene_count=30)

    result = SnowflakeWorkspaceService(session).generate_step(
        "prj-focus", "scene_details", {"focus_scene_refs": ["SC005"]}
    )

    assert calls == [["SC005"]], f"单场定向被分批逻辑改写了：{calls}"
    scenes = {s["scene_id"]: s for s in result["step"]["draft"]["scenes"]}
    assert scenes["SC005"]["goal"], "焦点场没被深化"
    assert not str(scenes["SC006"].get("goal") or "").strip(), "焦点外场景被动了"


def _seed_bulky_project(session, project_id: str) -> None:
    """上游堆一份大材料的作品：不设闸时整份会随每批重发。"""
    session.add(StoryProject(
        project_id=project_id, title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    bulk = "她在交班前比对墨迹年份，被缺页阻拦，确认补写却不敢声张。" * 40
    session.add(SnowflakeStepRun(
        step_run_id=f"{project_id}-long", project_id=project_id, step_key="long_synopsis",
        version=1, status="approved", draft_json={"paragraphs": [bulk] * 5},
        health_json={}, input_refs_json={},
    ))
    session.add(SnowflakeStepRun(
        step_run_id=f"{project_id}-chars", project_id=project_id, step_key="character_bibles",
        version=1, status="approved",
        draft_json={"characters": [
            {"character_id": f"c{i}", "display_name": f"角色{i}", "role": "配角",
             "physical_profile": {f"f{j}": bulk[:200] for j in range(4)},
             "psychological_profile": {f"f{j}": bulk[:200] for j in range(4)}}
            for i in range(1, 9)
        ]},
        health_json={}, input_refs_json={},
    ))
    for step_key in ("scene_list", "scene_details"):
        session.add(SnowflakeStepRun(
            step_run_id=f"{project_id}-{step_key}", project_id=project_id, step_key=step_key,
            version=1, status="pending_review", draft_json={"scenes": _scenes(30)},
            health_json={}, input_refs_json={},
        ))
    session.flush()


def test_the_prompt_budget_is_actually_enforced_on_the_generate_path(session, monkeypatch):
    """遗留缺陷回归：这条路径以前从不读 input_token_budget，载荷随作品体量无界增长。

    断言用「有闸 vs 无闸」对比，不用绝对数值——预算是对载荷的估算，整条 prompt 还要
    加上 task_prompt 与包裹语，写死数字只会变成一条脆弱的断言。
    """
    from novel_system.services.context_budget import estimate_tokens

    def run(project_id: str, budget: str) -> int:
        sizes: list[int] = []

        def responder(request):
            prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
            sizes.append(estimate_tokens(prompt))
            return _respond({"scenes": [_deep(sid) for sid in _focus_ids(_payload_of(request))]})

        _install_llm(monkeypatch, responder)
        monkeypatch.setenv("NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET", budget)
        _seed_bulky_project(session, project_id)
        SnowflakeWorkspaceService(session).generate_step(project_id, "scene_details", {})
        assert sizes, "没有发出调用"
        return max(sizes)

    unfenced = run("prj-unfenced", "99000000")   # 大到等于不设闸
    fenced = run("prj-fenced", "6000")

    assert fenced < unfenced * 0.6, (
        f"输入预算没被执行：无闸单批 {unfenced} tok，设闸 6000 后仍有 {fenced} tok"
    )


def test_an_unreachable_budget_tells_the_author_instead_of_silently_shedding(session, monkeypatch):
    """阶梯跑完仍超预算 → 作者必须看见「模型这次看到的是删减版」。"""
    def responder(request):
        return _respond({"scenes": [_deep(sid) for sid in _focus_ids(_payload_of(request))]})

    _install_llm(monkeypatch, responder)
    monkeypatch.setenv("NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET", "400")
    _seed(session, "prj-overrun", scene_count=30)

    result = SnowflakeWorkspaceService(session).generate_step("prj-overrun", "scene_details", {})

    notice = (result["step"]["health"] or {}).get("generation_notice") or {}
    assert notice.get("code") == "PROMPT_BUDGET_EXCEEDED", f"超预算没告知作者：{notice}"
    assert notice["severity"] == "warning"
    assert notice["budget_tokens"] == 400 and notice["estimated_after"] > 400
    assert notice["applied"], "报告里没写削了什么"


def test_shedding_is_recorded_in_the_llm_audit_summary(session, monkeypatch):
    """降载必须留痕，否则「模型怎么把这个角色写丢了」永远查不出是预算削的。"""
    from novel_system.services import snowflake_workspace_llm as mod
    summaries: list[dict] = []

    def responder(request):
        return _respond({"scenes": [_deep(sid) for sid in _focus_ids(_payload_of(request))]})

    _install_llm(monkeypatch, responder)
    monkeypatch.setattr(mod, "supplement_accounted_call",
                        lambda session, llm_call_id, **kwargs: summaries.append(kwargs.get("request_summary") or {}))
    monkeypatch.setenv("NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET", "2000")
    _seed(session, "prj-audit", scene_count=30)

    SnowflakeWorkspaceService(session).generate_step("prj-audit", "scene_details", {})

    # 摘要超限时会整体压缩成指纹，预算字段必须摊平成审计契约认的键才能存活
    recorded = [s for s in summaries if "prompt_budget_tokens" in s]
    assert recorded, f"审计摘要里没有预算留痕：{summaries[:1]}"
    first = recorded[0]
    assert first["prompt_budget_tokens"] == 2000
    assert first["prompt_pre_shed_input_tokens"] >= first["prompt_input_tokens"]
    assert first["prompt_budget_applied"], "没记下削了哪几级"
    assert first["prompt_budget_status"] in {"within_budget", "exceeded"}
