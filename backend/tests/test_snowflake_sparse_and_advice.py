"""阶段 H（2026-09-13 雪花评估第二轮）：留白即合法，诊断只认缺失与占位。

原著的角色表满是「尚未定义」、配角只有一行定位；第 9 步的场景计划五分钟一场，没有「代价」栏。
本项目的提示词却写着「空字段即缺陷」、诊断用「但 / 却」与泛泛短语表改状态、缺代价必「需修补」、
角色全档案读错键永远「压力不足」、五段被静默截断且空槽会顶位、规则层的「重写」还能挡物化。这里锁住修复。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.snowflake_steps import (
    diagnose_scene_detail,
    diagnose_step_pressure,
    merge_step_draft,
)
from tests.test_snowflake_closeout import _approve_through, _create_project, _step, _workspace


@pytest.fixture(autouse=True)
def _skeleton_snowflake_generate(monkeypatch):
    from novel_system.services.hash_engine import normalize
    from novel_system.services.snowflake_planner import SnowflakePlannerService
    from novel_system.services.snowflake_workspace_llm import (
        SnowflakeWorkspaceLLMService,
        WorkspaceLLMResult,
    )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate_step(self, *, project, step_key, latest_by_step, **kwargs):
        payload = SnowflakePlannerService(self.session)._build_artifact_json(project, step_key, dict(latest_by_step))
        return WorkspaceLLMResult(source="llm", llm_call_id=None, payload=normalize(payload))

    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "generate_step", fake_generate_step)


# ---------------------------------------------------------------------------
# 关键词与短语表只给建议
# ---------------------------------------------------------------------------


def test_keyword_rules_only_advise_for_the_early_steps() -> None:
    logline = diagnose_step_pressure("one_sentence_summary", {"summary": "少女写小说。"})  # 没有「但 / 却」
    assert logline["pressure_flags"] == []
    assert logline["status"] == "pass"
    assert any(step.startswith("建议：") for step in logline["fix_steps"])

    brief = diagnose_step_pressure(
        "book_brief",
        {
            "category": "悬疑",
            "target_reader": "喜欢悬疑的读者",  # 泛泛短语表命中
            "story_kind": "一段故事",
            "delight_reason": "追索",
            "genre_promise": "真相",
            "expected_reader_emotion": "紧张",
            "safety_rules": ["不写未成年人的性内容"],
        },
    )
    assert not any(flag.endswith("too_generic") for flag in brief["pressure_flags"])
    assert brief["status"] == "pass"
    assert any(step.startswith("建议：") and "读者" in step for step in brief["fix_steps"])

    scenes = diagnose_step_pressure(
        "scene_list",
        {"scenes": [{"scene_id": "S1", "summary": "一段故事", "chapter_role": "x", "pov_character_id": "c1"}]},
    )
    assert "scene_jobs_too_generic" not in scenes["pressure_flags"]
    assert any(step.startswith("建议：") for step in scenes["fix_steps"])

    synopsis = diagnose_step_pressure("short_synopsis", {"paragraphs": ["她回到雨城。", "她接手旧案。", "她查到线人。", "她找到证据。", "她赢了。"]})
    assert "synopsis_lacks_escalation" not in synopsis["pressure_flags"]
    assert synopsis["status"] == "pass"
    # 真正的缺失仍是旗标
    empty = diagnose_step_pressure("short_synopsis", {"paragraphs": []})
    assert "synopsis_missing" in empty["pressure_flags"]


def test_scene_beats_generic_phrases_advise_but_placeholders_still_flag() -> None:
    terse = {
        "scene_id": "S1",
        "primary_form": "proactive",
        "title": "上课",
        "summary": "她去上熊爸爸的课",
        "crucible": "不上课就写不出来",
        "goal": "上大纲课",
        "conflict": "They argue.",  # 泛泛短语表命中
        "setback": "她讨厌大纲。",
    }
    diagnosis = diagnose_scene_detail(terse)
    assert diagnosis["pressure_flags"] == []
    assert diagnosis["recommended_status"] == "pass" and diagnosis["score"] == 100
    assert any(step.startswith("建议：") and "泛泛短语" in step and "冲突" in step for step in diagnosis["advice"])
    assert any("免费选择" in step for step in diagnosis["advice"])  # 缺代价只提醒

    placeholder = dict(terse, setback="待补")
    diagnosis = diagnose_scene_detail(placeholder)
    assert diagnosis["pressure_flags"] == ["placeholder_setback"]
    assert diagnosis["recommended_status"] == "maybe"


# ---------------------------------------------------------------------------
# 角色三步：读嵌套档，配角可以留白
# ---------------------------------------------------------------------------


def test_character_bibles_read_nested_profiles_and_minor_characters_may_stay_sparse() -> None:
    bible = {
        "characters": [
            {
                "character_id": "c1",
                "display_name": "林一鸣",
                "role": "主角",
                "physical_profile": {"age": "34"},
                "psychological_profile": {
                    "deepest_fear": "再一次被人相信是凶手，却没有人愿意听他说完。",
                    "character_arc": "从体面的自保走到不惜身败名裂也要说出真相。",
                },
            },
            {"character_id": "c9", "display_name": "邻居老太", "role": "配角", "physical_profile": {}, "psychological_profile": {}},
        ]
    }
    diagnosis = diagnose_step_pressure("character_bibles", bible)
    assert not any(flag.endswith("_pressure_too_soft") for flag in diagnosis["pressure_flags"])
    assert diagnosis["status"] == "pass"
    assert any("林一鸣" in item for item in diagnosis["strengths"])
    # 配角全空：不提醒；主角全空：提醒
    assert not any("邻居老太" in step for step in diagnosis["fix_steps"])
    lead_empty = diagnose_step_pressure("character_sheets", {"characters": [{"character_id": "c1", "display_name": "林一鸣", "role": "主角"}]})
    assert lead_empty["pressure_flags"] == []
    assert any(step.startswith("建议：") and "林一鸣" in step for step in lead_empty["fix_steps"])


# ---------------------------------------------------------------------------
# 五段按位置保留，不截断
# ---------------------------------------------------------------------------


def test_synopsis_slots_keep_position_and_a_sixth_paragraph_survives() -> None:
    merged = merge_step_draft("short_synopsis", {"paragraphs": ["一", "", "三", "四", "五", "作者手写的第六段"]})
    assert merged["paragraphs"] == ["一", "", "三", "四", "五", "作者手写的第六段"]
    padded = merge_step_draft("long_synopsis", {"paragraphs": ["一", ""]})
    assert padded["paragraphs"] == ["一", "", "", "", ""]


# ---------------------------------------------------------------------------
# 规则层的「重写」不挡物化；执行合同的代价只提醒
# ---------------------------------------------------------------------------


def test_rule_rewrite_is_a_warning_not_a_materialization_blocker(client) -> None:
    pid = _create_project(client, key="h-gate")["project_id"]
    _approve_through(client, pid, "scene_details")
    details = _step(_workspace(client, pid), "scene_details")["draft"]["scenes"]
    hollow = dict(details[0], goal="", conflict="", setback="", reaction="", dilemma="", decision="", crucible="", scene_crucible="")
    response = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [hollow, *details[1:]]}},
    )
    assert response.status_code == 200, response.text
    gate = response.json()["data"]["workspace"]["materialization_gate"]
    kinds = {item["kind"]: item["severity"] for item in gate["items"]}
    assert kinds.get("triage_unreviewed_rewrite") == "warning"
    assert "triage_confirmation_required" not in kinds


def test_execution_contract_treats_cost_as_advisory() -> None:
    payload = {
        "scene_mode": "proactive",
        "pov_character_id": "c1",
        "scene_crucible": "审讯室封闭",
        "goal": "拿到离开许可",
        "conflict": "三轮受阻",
        "setback_or_victory": "被拘留",
        "_has_explicit_crucible": True,
    }
    missing = SceneExecutionContractService._missing_fields(None, payload)
    assert "cost_requirement" not in missing
    assert "cost_requirement(advisory)" in missing


# ---------------------------------------------------------------------------
# 提示词：留白即合法
# ---------------------------------------------------------------------------


def test_prompts_say_leave_empty_do_not_invent() -> None:
    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    expectations = {
        "snowflake_generate_character_sheets": ("2026-09-14.v6", "an honest empty key is not"),
        "snowflake_generate_character_synopses": ("2026-09-13.v5", "stays empty after its prefix"),
        "snowflake_generate_character_bibles": ("2026-09-13.v4", "an honest empty field is not"),
        "snowflake_generate_scene_details": ("2026-09-15.v13", "an honest empty one is not"),
    }
    for name, (version, phrase) in expectations.items():
        template = templates[name]
        assert template["version"] == version, name
        assert phrase in template["task_prompt"], name
    for name in ("snowflake_generate_character_sheets", "snowflake_generate_character_bibles"):
        assert "Fill every" not in templates[name]["task_prompt"], name
    from novel_system.services.snowflake_workspace_llm import UPSTREAM_STEPS_HOW_TO_USE

    assert "留白" in UPSTREAM_STEPS_HOW_TO_USE
