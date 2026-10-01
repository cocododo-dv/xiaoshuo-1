"""B06-03（2026-09-30 作者批准 #16b）：完备性修复重试不再逼配角填满。

阶段 H 的提示词写着「留白即合法——只有主角与对手必须完整」，规则层的诊断也只对主角 / 对手的空表提醒；
可整步生成后的修复重试把**每个角色的每个空字段**都列成缺陷、要求模型补齐。配角本来就可以只有一行定位，
于是几乎每次角色三步的生成都多花一次调用，而重试「更完整」往往只是模型替作者编了更多配角细节。
现在：主角 / 对手的空字段照旧进修复清单；配角只在整个成员一个字都没写（连定位都没有）时算缺口。
"""

from __future__ import annotations

from novel_system.services.snowflake_workspace_llm import _collect_generation_gaps
from tests.support.snowflake import (
    install_snowflake_llm as _install_llm,
    llm_payload_response as _respond,
    seed_synopsis_project as _seed_synopsis_project,
    working_payload_of as _payload_of,
)


def _sheet(name: str, role: str, **fields: object) -> dict:
    return {"character_id": f"id-{name}", "display_name": name, "role": role, **fields}


_COMPLETE_LEAD = {
    "goal": "查清旧案",
    "ambition": "证明自己没有看错人",
    "values": ["没有什么比真相更重要", "没有什么比家人更重要"],
    "conflict": "查下去就要把哥哥送进去",
    "epiphany": "真相救不了所有人",
    "one_sentence_summary": "她必须在真相与哥哥之间选一个。",
    "one_paragraph_summary": "她回到雨城查旧信，一路查到哥哥身上，最后亲手交出了录音。",
}


def test_sparse_minor_characters_are_not_repair_targets() -> None:
    gaps = _collect_generation_gaps(
        "character_sheets",
        {"characters": [_sheet("林昭", "主角", **_COMPLETE_LEAD), _sheet("老周", "配角 · 邮差")]},
    )
    assert gaps == []

    gaps = _collect_generation_gaps(
        "character_synopses",
        {"characters": [_sheet("林昭", "主角", synopsis="她的视角故事。"), _sheet("老周", "配角", synopsis="")]},
    )
    assert gaps == []


def test_lead_roles_keep_their_field_level_gaps() -> None:
    gaps = _collect_generation_gaps(
        "character_sheets",
        {"characters": [_sheet("林昭", "主角", goal="查清旧案"), _sheet("程远", "对手（反派）")]},
    )
    assert "characters[林昭].conflict" in gaps and "characters[林昭].epiphany" in gaps
    assert "characters[程远].goal" in gaps
    assert "characters[林昭].goal" not in gaps


def test_a_scene_list_column_is_not_a_scene_details_repair_target() -> None:
    """合并胶水 G7：坩埚归 09——第 10 步存不进去（G4），模板 v15 也让模型别改它；场景规划的补全重试只盯第 10 步
    自己写得进去的栏（三拍），不为空着的 09 坩埚多花一次调用。"""
    gaps = _collect_generation_gaps(
        "scene_details",
        {"scenes": [{"scene_id": "SC01", "primary_form": "proactive", "crucible": "", "goal": "拿到账本", "conflict": "三轮受阻", "setback": ""}]},
    )
    assert gaps == ["SC01.setback"]
    assert _collect_generation_gaps("scene_details", {"scenes": []}) == ["scenes"]


def test_a_member_with_nothing_but_a_name_is_still_a_gap() -> None:
    gaps = _collect_generation_gaps(
        "character_bibles",
        {"characters": [{"character_id": "id-x", "display_name": "路人甲", "role": "", "physical_profile": {"age": ""}}]},
    )
    assert gaps == ["characters[路人甲]"]


def test_character_sheet_generation_does_not_spend_a_retry_on_sparse_minor_characters(session, monkeypatch) -> None:
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    calls: list[dict] = []

    def responder(request):
        calls.append(_payload_of(request))
        return _respond(
            {
                "characters": [
                    {"display_name": "林昭", "role": "主角", **_COMPLETE_LEAD},
                    {"display_name": "老周", "role": "配角 · 邮差"},
                ]
            }
        )

    _install_llm(monkeypatch, responder)
    _seed_synopsis_project(session, "prj-sparse-cast")

    result = SnowflakeWorkspaceService(session).generate_step("prj-sparse-cast", "character_sheets", {})

    assert len(calls) == 1, "配角留白是合法的：不为它多花一次调用"
    names = [item["display_name"] for item in result["step"]["draft"]["characters"]]
    assert names == ["林昭", "老周"]


def test_character_sheet_generation_still_repairs_an_incomplete_lead(session, monkeypatch) -> None:
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    calls: list[dict] = []

    def responder(request):
        calls.append(_payload_of(request))
        lead = {"display_name": "林昭", "role": "主角", "goal": "查清旧案", "conflict": "查下去要送哥哥进去"}
        if len(calls) > 1:
            lead = {"display_name": "林昭", "role": "主角", **_COMPLETE_LEAD}
        return _respond({"characters": [lead, {"display_name": "老周", "role": "配角"}]})

    _install_llm(monkeypatch, responder)
    _seed_synopsis_project(session, "prj-thin-lead")

    result = SnowflakeWorkspaceService(session).generate_step("prj-thin-lead", "character_sheets", {})

    assert len(calls) == 2
    empty_fields = calls[1]["completeness_repair"]["empty_fields"]
    assert "characters[林昭].epiphany" in empty_fields
    assert not any(field.startswith("characters[老周]") for field in empty_fields)
    lead = result["step"]["draft"]["characters"][0]
    assert lead["epiphany"] == _COMPLETE_LEAD["epiphany"]
