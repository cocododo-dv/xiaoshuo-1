"""阶段 N–S（2026-09-15 雪花评估第三轮）：场景形态的两处硬编码、场景篇的标准进提示词与规则层、
分诊 / 教练 / 一段话提示词更新、破例理由、作者裁定「待删」。

原著依据（docs/ingermanson-snowflake-ideas.md）：
- 第 1 场是带两组三拍的「叙述概述」（主动场也可以概述），收尾的 23–25 场只有一段说明、没有三拍；
- 第 22 场明写「冲突：无」——不过关也可以放行，但要知道理由；
- No 的场标记待删、下一稿再删，不真删；哪一场都不该把整本书的结构卡住；
- 好目标五条 / 好反应四条 / 好决定四条；冲突回合数没有规则；坩埚每场要新；POV 选损失最大的人。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from novel_system.db.models import SceneCard, SnowflakeSceneTriageItem
from novel_system.services.scene_design_context import render_scene_design_context
from novel_system.services.scene_structure_brief import (
    missing_structure_fields,
    render_scene_structure_brief,
    scene_has_structure,
)
from novel_system.services.snowflake_chaptering import _rhythm_report
from novel_system.services.snowflake_planner import _outline_scene_detail, _outline_scene_list, _scene_writer_brief
from novel_system.services.snowflake_steps import (
    SCENE_FIELD_EXAMPLES,
    diagnose_scene_detail,
    diagnose_step_pressure,
    effective_rendering_mode,
    get_step_definition,
    step_completeness,
    step_guidance,
)
from novel_system.services.snowflake_workspace import (
    EXCLUDED_TRIAGE_STATUSES,
    SnowflakeWorkspaceService,
    _coerce_triage_status,
)
from novel_system.services.snowflake_workspace_llm import _normalize_triage_output
from tests.test_snowflake_rendering_mode import PROJECT_ID, _materialize, _plan, _seed

_PROMPTS = pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"


def _templates() -> dict:
    return yaml.safe_load(_PROMPTS.read_text(encoding="utf-8"))["templates"]


# 原著样例的译写形态（不复制原文）：开场的叙述概述带两组三拍；「冲突：无」的收尾课；没有三拍的尾声。
GOLDILOCKS_EXCEPTIONS = {
    "scene_1_narrative_opening": {
        "scene_id": "S01",
        "primary_form": "proactive",
        "title": "开场概述",
        "summary": "关于女主角和她那个不切实际之梦的叙述概述。",
        "crucible": "她想以讲故事为生，可全镇没有一个人把这当回事。",
        "goal": "让镇上的人听她讲一个故事。",
        "conflict": "她逐家去说，每一家都以各自的理由把她打发走。",
        "setback": "最后一家把门关上，她一个听众都没有。",
        "reaction": "她坐在台阶上，半天没动。",
        "dilemma": "回家继续过安稳日子，还是去别处碰运气。",
        "decision": "她决定去森林另一头的镇子试试。",
        "rendering_mode": "summary",
    },
    "scene_22_no_conflict": {
        "scene_id": "S22",
        "primary_form": "proactive",
        "title": "收尾的一课",
        "summary": "导师把最后一课讲完。",
        "crucible": "课不讲完，她仍不敢相信自己的直觉。",
        "goal": "听完最后一课。",
        "conflict": "无",
        "setback": "课讲完了，接下来只能靠她自己。",
        "exception_reason": "教学寓言的收尾课：这一场没有冲突，作者知道理由。",
    },
    "scene_23_wrap_up": {
        "scene_id": "S23",
        "primary_form": "proactive",
        "title": "尾声",
        "summary": "交代每个人的去向。",
        "exception_reason": "全书收尾的叙述交代，没有新的冲突；读者需要看到每个人的去向。",
    },
}


# ---------------------------------------------------------------------------
# 阶段 N · 破例理由 + 主动场的叙述概述
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(GOLDILOCKS_EXCEPTIONS))
def test_ingermansons_exceptions_pass_the_rule_layer(key: str) -> None:
    scene = dict(GOLDILOCKS_EXCEPTIONS[key])
    diagnosis = diagnose_scene_detail(scene)
    assert diagnosis["missing_fields"] == [], diagnosis
    assert diagnosis["pressure_flags"] == [], diagnosis
    assert diagnosis["recommended_status"] == "pass"
    assert diagnosis["score"] == 100
    if scene.get("exception_reason"):
        assert diagnosis["exception_reason"] == scene["exception_reason"]
        assert any(step.startswith("作者破例：") for step in diagnosis["fix_steps"])
    else:
        assert diagnosis["exception_reason"] == ""
    # 「冲突：无」是作者写下的内容，不是占位
    assert "placeholder_conflict" not in diagnosis["pressure_flags"]


def test_exception_reason_exempts_completeness_and_the_workspace_editor_offers_it() -> None:
    draft = {"scenes": [dict(GOLDILOCKS_EXCEPTIONS["scene_23_wrap_up"])]}
    completeness = step_completeness("scene_details", draft)
    assert completeness["missing_fields"] == []
    assert completeness["filled_count"] == completeness["total_count"]
    step = diagnose_step_pressure("scene_details", draft)
    assert step["hard_blockers"] == [] and step["status"] == "pass"

    template = next(f for f in get_step_definition("scene_details")["editor"]["fields"] if f["key"] == "scenes")["template"]
    assert template["exception_reason"] == ""
    modes = {mode["value"]: mode for mode in next(f for f in get_step_definition("scene_details")["editor"]["fields"] if f["key"] == "scenes")["scene_modes"]}
    for value, mode in modes.items():
        keys = [field["key"] for field in mode["fields"]]
        assert "exception_reason" in keys, value
        rendering = next(field for field in mode["fields"] if field["key"] == "rendering_mode")
        options = [option["value"] for option in rendering["options"]]
        assert options == (["full", "summary", "skip"] if value == "reactive" else ["full", "summary"]), value


def test_structure_brief_renders_the_exception_and_preflight_stops_nagging() -> None:
    card = SceneCard(
        scene_id="S23",
        project_id="p",
        chapter_id="c",
        scene_seq=1,
        scene_type="proactive",
        writer_brief_json=_scene_writer_brief("proactive", GOLDILOCKS_EXCEPTIONS["scene_23_wrap_up"]),
    )
    assert card.writer_brief_json["exception_reason"].startswith("全书收尾")
    assert scene_has_structure(card)
    assert missing_structure_fields(card) == []
    brief = render_scene_structure_brief(card, None)
    assert "Goal (目标): 未规划" in brief
    assert "Author's exception (破例理由): 全书收尾的叙述交代" in brief
    assert "deliberately unplanned" in brief

    # 没有破例理由的残缺场照旧报缺
    plain = SceneCard(scene_id="S24", project_id="p", chapter_id="c", scene_seq=2, scene_type="proactive",
                      writer_brief_json=_scene_writer_brief("proactive", {"crucible": "困局", "goal": "拿到东西"}))
    assert missing_structure_fields(plain) == ["conflict", "setback"]


def test_effective_rendering_mode_is_one_rule_for_every_consumer() -> None:
    assert effective_rendering_mode("proactive", "summary") == "summary"
    assert effective_rendering_mode("proactive", "skip") == "full"
    assert effective_rendering_mode("reactive", "skip") == "skip"
    assert effective_rendering_mode("", "summary") == "summary"


# ---------------------------------------------------------------------------
# 阶段 N · 作者裁定：该重写 / 待删排除在物化之外，不阻断全书
# ---------------------------------------------------------------------------


def _verdict(service: SnowflakeWorkspaceService, session, row_uid: str, status: str) -> None:
    plan = _plan(session, row_uid)
    service.save_scene_triage(
        PROJECT_ID,
        {"items": [{"scene_plan_id": plan.scene_plan_id, "scene_id": plan.scene_id, "status": status}]},
    )


def test_cut_is_an_author_only_status() -> None:
    assert _coerce_triage_status("cut") == "cut"
    assert _coerce_triage_status("CUT ") == "cut"
    assert EXCLUDED_TRIAGE_STATUSES == frozenset({"rewrite", "cut"})
    draft = {"scenes": [{"scene_id": "SC1", "primary_form": "proactive", "crucible": "困", "goal": "g", "conflict": "c", "setback": "s"}]}
    normalized = _normalize_triage_output({"items": [{"scene_id": "SC1", "status": "cut", "notes": "模型想删"}]}, draft)
    assert normalized["items"][0]["status"] == "pass"  # LLM 不能标待删：沿用规则建议
    assert normalized["items"][0]["notes"] != "模型想删"


def test_cut_and_rewrite_verdicts_exclude_the_scene_and_warn_instead_of_blocking(session) -> None:
    service = _seed(session)
    _verdict(service, session, "u3", "cut")
    _verdict(service, session, "u2", "rewrite")
    gate = service.workspace(PROJECT_ID)["materialization_gate"]
    kinds = {item["kind"]: item for item in gate["items"]}
    assert kinds["triage_cut"]["severity"] == "warning" and "待删" in kinds["triage_cut"]["message"]
    assert kinds["triage_rewrite"]["severity"] == "warning" and "不建它的场景卡" in kinds["triage_rewrite"]["message"]
    assert not any(item["kind"] in {"triage_cut", "triage_rewrite"} and item["severity"] == "blocker" for item in gate["items"])
    assert "no_materializable_scene" not in kinds
    rows = {row.scene_plan_id: row for row in session.query(SnowflakeSceneTriageItem).all()}
    assert rows[_plan(session, "u3").scene_plan_id].blocking == 1
    assert rows[_plan(session, "u3").scene_plan_id].effective_status == "cut"

    plan_json = _materialize(session, service)
    scene_ids = {scene["scene_id"] for chapter in plan_json["chapters"] for scene in chapter["scenes"]}
    assert _plan(session, "u1").scene_id in scene_ids
    assert _plan(session, "u2").scene_id not in scene_ids  # 该重写：先不建卡
    assert _plan(session, "u3").scene_id not in scene_ids  # 待删：不建卡
    assert session.get(SceneCard, _plan(session, "u3").scene_id) is None


def test_every_scene_excluded_blocks_with_a_clear_message(session) -> None:
    service = _seed(session)
    for row_uid in ("u1", "u2", "u3"):
        _verdict(service, session, row_uid, "cut")
    gate = service.workspace(PROJECT_ID)["materialization_gate"]
    blocker = next(item for item in gate["items"] if item["kind"] == "no_materializable_scene")
    assert blocker["severity"] == "blocker" and gate["status"] == "blocked"
    assert "没有可整理的场景" in blocker["message"]


def test_cutting_a_materialized_scene_trashes_its_card_and_reverting_restores_it(session) -> None:
    service = _seed(session)
    _materialize(session, service)
    plan = _plan(session, "u1")
    card = session.get(SceneCard, plan.scene_id)
    assert card is not None and card.trashed_flag == 0
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0

    _verdict(service, session, "u1", "cut")
    status = service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))
    pending = {item["scene_id"]: item for item in status["pending_scenes"]}
    assert plan.scene_id in pending and "trashed_flag" in pending[plan.scene_id]["changed_fields"]
    service.resync_materialized_scenes(PROJECT_ID, {"scene_ids": [plan.scene_id]})
    session.expire_all()
    card = session.get(SceneCard, plan.scene_id)
    assert card.trashed_flag == 1
    assert card.writer_brief_json.get("skipped_by_plan") is True
    assert card.writer_brief_json.get("excluded_by_triage") is True

    # 改主意：裁定回「通过」→ 回流取回场景卡
    _verdict(service, session, "u1", "pass")
    service.resync_materialized_scenes(PROJECT_ID, {"scene_ids": [plan.scene_id]})
    session.expire_all()
    card = session.get(SceneCard, plan.scene_id)
    assert card.trashed_flag == 0 and card.writer_brief_json.get("excluded_by_triage") is False


def test_design_context_skips_a_cut_neighbour_but_keeps_a_rewrite_one(session) -> None:
    service = _seed(session)
    _materialize(session, service)
    card_u1 = session.get(SceneCard, _plan(session, "u1").scene_id)
    text = render_scene_design_context(card_u1, session) or ""
    assert "Next scene (S02)" in text

    _verdict(service, session, "u2", "cut")
    text = render_scene_design_context(card_u1, session) or ""
    assert "Next scene (S02)" not in text
    assert "Next scene (S03)" in text  # 待删的场对邻居来说不存在，跳到再下一场

    _verdict(service, session, "u2", "rewrite")
    text = render_scene_design_context(card_u1, session) or ""
    assert "Next scene (S02)" in text  # 该重写的场仍是设计里的一场


def test_rhythm_report_counts_excluded_scenes_as_nothing() -> None:
    report = _rhythm_report(
        [{"act": 1, "scene_count": 3, "scenes": [{"rendering_mode": "full"}, {"rendering_mode": "full", "excluded": True}, {"rendering_mode": "summary"}]}]
    )
    assert report["weighted_scene_counts"] == [1.5]


# ---------------------------------------------------------------------------
# 阶段 O · 规则层与文案：坩埚每场要新、冲突回合数没有规则、说明与提示词说同一句话
# ---------------------------------------------------------------------------


def test_identical_consecutive_crucibles_only_earn_advice() -> None:
    same = "退不出的困局"
    scenes = [
        {"scene_id": "S1", "title": "取账本", "primary_form": "proactive", "crucible": same, "goal": "g", "conflict": "c", "setback": "s"},
        {"scene_id": "S2", "title": "再取账本", "primary_form": "proactive", "crucible": same, "goal": "g2", "conflict": "c2", "setback": "s2"},
        {"scene_id": "S3", "title": "换条路", "primary_form": "proactive", "crucible": "新的困局", "goal": "g3", "conflict": "c3", "setback": "s3"},
    ]
    step = diagnose_step_pressure("scene_details", {"scenes": scenes})
    assert step["status"] == "pass" and step["hard_blockers"] == []
    hits = [line for line in step["next_actions"] if "一字不差" in line]
    assert len(hits) == 1 and "取账本" in hits[0] and "再取账本" in hits[0]
    listed = diagnose_step_pressure("scene_list", {"scenes": [{**s, "summary": s["title"], "chapter_role": "起疑"} for s in scenes]})
    assert any("一字不差" in line for line in listed["next_actions"])


def test_conflict_advice_no_longer_prescribes_two_or_three_rounds() -> None:
    scene = {"scene_id": "S1", "primary_form": "proactive", "crucible": "困", "goal": "拿到账本", "conflict": "被拒绝。", "setback": "账本被烧"}
    advice = diagnose_scene_detail(scene)["advice"]
    hit = next(line for line in advice if "尝试→受阻" in line)
    assert "至少两轮" in hit and "2–3" not in hit
    editor = get_step_definition("scene_details")["editor"]["fields"][0]
    proactive = next(mode for mode in editor["scene_modes"] if mode["value"] == "proactive")
    conflict = next(field for field in proactive["fields"] if field["key"] == "conflict")
    assert "2-3" not in conflict["hint"] and "至少两轮" in conflict["hint"]
    goal = next(field for field in proactive["fields"] if field["key"] == "goal")
    assert "时间槽" in goal["hint"] and "价值观" in goal["hint"]
    assert "倒计时" not in SCENE_FIELD_EXAMPLES["goal"]
    reactive = next(mode for mode in editor["scene_modes"] if mode["value"] == "reactive")
    assert "全押" in next(field for field in reactive["fields"] if field["key"] == "decision")["hint"]
    assert "断层线" in next(field for field in reactive["fields"] if field["key"] == "dilemma")["hint"]
    assert "成比例" in next(field for field in reactive["fields"] if field["key"] == "reaction")["hint"]


def test_step_instructions_agree_with_the_prompts() -> None:
    assert "600–1000" in step_guidance("long_synopsis")["instruction"]
    assert "300–600" not in step_guidance("long_synopsis")["instruction"]
    scenes = step_guidance("scene_list")["instruction"]
    assert "主动/反应" in scenes and "被动" not in scenes and "损失最大" in scenes and "每场都要新" in scenes
    details = step_guidance("scene_details")["instruction"]
    assert "五关" in details and "全押" in details and "破例" in details and "越短越好" in details
    assert "苦乐" in step_guidance("one_paragraph_summary")["instruction"]


def test_v1_planner_detail_fallback_is_proactive_not_parity() -> None:
    from novel_system.db.models import StoryProject

    # 夹具形状不变：每章一场主动加一场反应（回流 / 目录测试要有反应场样本）
    project = StoryProject(project_id="prj-v1", title="v1", outline_text="a\nb", planning_mode="snowflake", target_chapter_count=2)
    scenes = _outline_scene_list(project, ["雨夜来信", "旧案"], zh=True)
    assert [scene["primary_form"] for scene in scenes] == ["proactive", "reactive", "proactive", "reactive"]
    # 没标形态的场不再按行号奇偶猜：默认主动（与 v2 的 _normalize_scene_item 同一口径）
    assert _outline_scene_detail({"summary": "x"}, 2, ["a"], zh=True)["primary_form"] == "proactive"
    assert _outline_scene_detail({"summary": "x"}, 1, ["a"], zh=True)["primary_form"] == "proactive"


# ---------------------------------------------------------------------------
# 阶段 O / P / Q · 提示词
# ---------------------------------------------------------------------------


def test_round3_prompt_contracts() -> None:
    templates = _templates()
    triage = templates["snowflake_scene_triage_suggest"]
    for phrase in ("you can name the scene crucible", "exception_reason", '"summary"', '"skip"', "mixed victory", "never suggest cutting", "cost_requirement is advisory"):
        assert phrase in triage["task_prompt"], phrase
    paragraph = templates["snowflake_generate_one_paragraph_summary"]
    assert "bittersweet" in paragraph["task_prompt"] and "false belief" in paragraph["task_prompt"]
    sentence = templates["snowflake_generate_one_sentence_summary"]
    assert "do not reveal the ending" in sentence["task_prompt"]
    scene_list = templates["snowflake_generate_scene_list"]
    assert "most to lose" in scene_list["task_prompt"] and "fresh scene crucible" in scene_list["task_prompt"]
    details = templates["snowflake_generate_scene_details"]
    for phrase in ("five tests", "no upper limit", "the last attempt, told briefly", "own fault line", "committing all the way",
                   'rendering_mode: "full" / "summary" for either form', "exception_reason", "cost_requirement is advisory", "never make every scene medium by default"):
        assert phrase in details["task_prompt"], phrase
    assert "2-3 rounds" not in details["task_prompt"] and "time-bound" not in details["task_prompt"]
    for name in ("snowflake_workspace_assistant", "snowflake_step_candidates"):
        # 阶段 T（2026-09-16）v5：教练有记忆并重述作者意图要点；候选在要点范围内分岔。留白规则不变。
        # 阶段 U（2026-09-17）v6：方向回合进教练日志（recent_turns kind=candidates / author_ask / focus_scene）。
        prompt = templates[name]["task_prompt"] if name == "snowflake_workspace_assistant" else templates[name]["system_prompt"]
        assert "deliberate blanks" in prompt, name
        assert "Treat all of them as this book's established facts" not in prompt, name
    drafting = {
        "neutral_draft": ("never the story crucible", "Author's exception", "the last attempt", "dance around the plan", "rather than named"),
        "style_first_draft": ("Author's exception", "the Setback is the last attempt", "proactive or reactive — is told as narrative summary"),
        "hard_qc": ("Author's exception", "is not a violation"),
        "soft_qc": ("story-crucible exposition", "an emotion named", "dragged over pages", "never a gap"),
        "near_final_acceptance_review": ("Author's exception", "whether that reason holds"),
        "scene_blueprint": ("Author's exception", "the Setback itself (for a proactive scene)"),
    }
    for name, phrases in drafting.items():
        for phrase in phrases:
            assert phrase in templates[name]["task_prompt"], (name, phrase)
