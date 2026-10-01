"""整步生成的完整性契约：宁可报错，不可静默交出半新半旧的草稿。

回归两个真实故障（作品《何有》）：
1. finish_reason=length 被当成功 → 作者点了生成、等了、没报错，草稿却原地不动
   或只推进了一两场（同一份场景规划里有的场深、有的场空 = 割裂）。
2. draft_override 整表替换 → 场景规划从 12 场无声掉回 5 场，且发生在调 LLM 之前。
"""
from __future__ import annotations

import json

import pytest

from novel_system.db.models import SnowflakeStepRun, StoryProject
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMResponse
from novel_system.services.snowflake_draft_merge import merge_member_lists
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from tests.support.snowflake import install_snowflake_llm as _install_llm

SCENES = [
    {"row_uid": f"u{i}", "scene_id": f"SC{i:03d}", "chapter_id": "CH01", "scene_seq": i,
     "summary": f"旧内容第{i}场", "primary_form": "proactive", "scene_type": "proactive",
     "location": "旧地点", "crucible": "旧坩埚"}
    for i in range(1, 13)
]


def _seed(session, project_id: str) -> None:
    session.add(StoryProject(
        project_id=project_id, title="何有", outline_text="何有", planning_mode="snowflake",
        snowflake_workflow_mode="explore", target_word_count=100000,
    ))
    for step_key in ("scene_list", "scene_details"):
        session.add(SnowflakeStepRun(
            step_run_id=f"{project_id}-{step_key}", project_id=project_id, step_key=step_key,
            version=1, status="pending_review", draft_json={"scenes": SCENES},
            health_json={}, input_refs_json={},
        ))
    session.flush()


def test_truncated_generation_fails_loudly_instead_of_returning_an_unchanged_draft(session, monkeypatch):
    """被砍断的输出必须变成作者可见的错误，而不是"生成成功但什么都没变"。"""
    from novel_system.services.llm_client import LLMResponseError

    def responder(request):
        raise LLMResponseError("LLM_RESPONSE_TRUNCATED", "hit the ceiling")

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-trunc")

    with pytest.raises(DomainError) as excinfo:
        SnowflakeWorkspaceService(session).generate_step("prj-trunc", "scene_details", {})
    assert excinfo.value.code == "SNOWFLAKE_LLM_CALL_FAILED"


def test_draft_override_does_not_delete_scenes_the_frontend_omitted(session, monkeypatch):
    """draft_override 是"补上未保存的本地编辑"，不是删除指令——存档里的成员必须活下来。"""
    seen: dict = {}

    def responder(request):
        prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
        payload = json.loads(prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0])
        seen["scene_ids"] = [s["scene_id"] for s in payload["current_draft"]["scenes"]]
        out = {"scenes": [{"scene_id": "SC001", "title": "新标题", "summary": "全新第1场",
                           "goal": "新目标", "conflict": "新冲突", "setback": "新挫折",
                           "cost_requirement": "新代价", "exit_change": "新变化", "hook": "新钩子"}]}
        return LLMResponse(request_id="r", provider="p", model="m",
                           text=json.dumps(out, ensure_ascii=False), structured_output=out,
                           response_format="json_object", raw_response={}, usage={}, finish_reason="stop")

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-merge")

    # 前端只带上来前 5 场（例如本地缓存刚按场景列表重新播种），存档里有 12 场
    override = {"scenes": [dict(scene) for scene in SCENES[:5]]}
    result = SnowflakeWorkspaceService(session).generate_step(
        "prj-merge", "scene_details", {"draft_override": override}
    )

    assert len(seen["scene_ids"]) == 12, f"生成底稿丢了场景：只剩 {seen['scene_ids']}"
    # scene_id 由服务端铸造，用 summary 追踪成员身份
    kept = [scene["summary"] for scene in result["step"]["draft"]["scenes"]]
    assert len(kept) == 12, f"落库草稿丢了场景：只剩 {len(kept)} 场 {kept}"
    assert kept[1:] == [f"旧内容第{i}场" for i in range(2, 13)], f"焦点外场景被改动了：{kept}"


def test_draft_override_still_carries_unsaved_local_edits(session, monkeypatch):
    """反向保护：override 里的新内容必须盖过存档，否则这个通道就失去意义。"""
    seen: dict = {}

    def responder(request):
        prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
        payload = json.loads(prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0])
        seen["draft"] = payload["current_draft"]
        out = {"scenes": [{"scene_id": "SC001", "goal": "g"}]}
        return LLMResponse(request_id="r", provider="p", model="m",
                           text=json.dumps(out, ensure_ascii=False), structured_output=out,
                           response_format="json_object", raw_response={}, usage={}, finish_reason="stop")

    _install_llm(monkeypatch, responder)
    _seed(session, "prj-edits")

    override = {"scenes": [{"scene_id": "SC002", "summary": "作者刚改的第2场"},
                           {"scene_id": "SC099", "summary": "作者刚加的新场"}]}
    SnowflakeWorkspaceService(session).generate_step(
        "prj-edits", "scene_details", {"draft_override": override}
    )

    by_id = {s["scene_id"]: s for s in seen["draft"]["scenes"]}
    assert by_id["SC002"]["summary"] == "作者刚改的第2场", "未保存的本地编辑没被带进生成底稿"
    assert "SC099" in by_id, "作者刚加的场景没被带进生成底稿"
    assert by_id["SC001"]["summary"] == "旧内容第1场", "override 没提到的场景不该被改动"


def test_step_with_override_keeps_members_on_the_assistant_and_triage_paths():
    """助手/场景三分类走的共享 _step_with_override 也必须保成员——不能只修 generate。

    request_assistant / suggest_scene_triage 都经 _step_with_override 把 draft_override
    盖上去。若它整表替换，前端只带 5 场时教练/分类器只看到半截故事（与 generate 同一 bug）。
    """
    base = {"scenes": [{"scene_id": f"SC{i:03d}", "row_uid": f"u{i}", "summary": f"旧第{i}场"}
                       for i in range(1, 13)]}
    step = {"step_key": "scene_details", "draft": base}
    # 前端只带上来前 5 场，且捎带一个 fe_ 写透键（必须被剥掉、不进入生成底稿）
    override = {"scenes": [{"scene_id": f"SC{i:03d}", "summary": f"改第{i}场"} for i in range(1, 6)],
                "fe_dirty": True}

    merged = SnowflakeWorkspaceService._step_with_override("P", step, override, latest_by_step={})
    scenes = merged["draft"]["scenes"]

    assert len(scenes) == 12, f"助手/三分类底稿丢了场景：只剩 {len(scenes)} 场"
    assert "fe_dirty" not in merged["draft"], "fe_* 写透键不该进入生成/助手底稿"
    by_id = {s["scene_id"]: s for s in scenes}
    assert by_id["SC003"]["summary"] == "改第3场", "override 里的未保存编辑没盖上"
    assert by_id["SC009"]["summary"] == "旧第9场", "override 没带的场景被抹掉或改动了"


def test_merge_member_lists_matches_a_member_keyed_by_scene_id_or_row_uid():
    """同一成员在 base/override 里键不同（scene_id vs row_uid）必须对上，不能重复留两份。

    真实情形：后端 id 还没回填到本地这份草稿，前端此次只带了 row_uid。取「第一个非空键」
    的旧身份逻辑会把它当新成员，于是场景列表凭空多出一份重复——正是要根除的「割裂」来源。
    """
    base = [{"scene_id": "s1", "row_uid": "r1", "summary": "存档第1场"},
            {"scene_id": "s2", "row_uid": "r2", "summary": "存档第2场"}]
    # override 对第 1 场只带 row_uid（丢了 scene_id），第 2 场只带 scene_id
    override = [{"row_uid": "r1", "summary": "刚编辑的第1场"},
                {"scene_id": "s2", "summary": "刚编辑的第2场"}]

    merged = merge_member_lists(base, override, id_keys=("scene_id", "row_uid"))

    assert len(merged) == 2, f"跨键成员被当成新成员重复了：{[m['summary'] for m in merged]}"
    by_summary = [m["summary"] for m in merged]
    assert by_summary == ["刚编辑的第1场", "刚编辑的第2场"], "override 的编辑没盖上或顺序错乱"
    # 对上后，base 独有的字段（scene_id）应随合并保留下来
    assert merged[0].get("scene_id") == "s1", "跨键合并把 base 的 scene_id 弄丢了"
