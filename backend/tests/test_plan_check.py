"""终稿逐条复核（``services/plan_check.py``）：条目从冻结的场景结构段认出来（与渲染器同一套行首）、按条目生成 schema、
归一与偏离发现、准终稿评审端到端（问法、存储、不改放行）、写前笔记接口读最近一次复核，以及诊断抽屉读得出准终稿评审
v11 那种只有 evidence 的旧发现。"""

from __future__ import annotations

import json

from sqlalchemy import select

from novel_system.db.models import AttemptTracker, SceneCard, WriterEvaluation
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import LLMRequest, LLMResponse, OnlineAccountedExecution
from novel_system.services.near_final_payload import _apply_scene_near_final_gates, _normalize_acceptance_payload
from novel_system.services.near_final_review import NearFinalAcceptanceService
from novel_system.services.plan_check import (
    GRADES,
    PLAN_CHECK_KEY,
    PLAN_DIMENSION,
    apply_plan_check_schema,
    normalize_plan_check,
    plan_check_findings,
    plan_items,
)
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.scene_blueprint_view import SceneBlueprintViewService
from novel_system.services.scene_diagnosis.findings import evaluation_findings
from novel_system.services.scene_diagnosis.text import DiagnosisText
from novel_system.services.scene_diagnosis.vocabulary import REVIEW_DIMENSION_LABELS
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.scene_workbench import serialize_near_final_summary
from tests.support.seed import seed_chapter, seed_project, seed_scene

PROJECT_ID = "PRJ_PLAN"
CHAPTER_ID = "PRJ_PLAN_CH01"
SCENE_ID = "PRJ_PLAN_CH01_SC01"

FULL_BRIEF = {
    "scene_form": "proactive",
    "scene_crucible": "雨城封港，林昭非拿到案卷不可",
    "goal": "从档案室取出旧信",
    "conflict": "看门人坚持要登记姓名",
    "setback": "旧信被人先一步取走",
    "reaction": "林昭在雨里站了很久",
    "dilemma": "报官还是自己追",
    "cost_requirement": "她用掉了唯一的通行证",
    "expected_reader_emotion": "不安",
    "must_reveal": "看门人认识取信的人",
    "must_withhold": "取信的是她哥哥",
    "next_scene_pull": "门缝里塞进一张字条",
}
CONTENT = (
    "林昭推开档案室的门，看门人抬头说要登记。\n"
    "她报了一个假名字，看门人笑了笑，说刚才也有人这样报过。\n"
    "柜子里空了，旧信已经被取走。她把通行证押在桌上，转身走进雨里。\n"
    "回到住处，门缝里塞着一张字条。"
)


def _scene(**fields) -> SceneCard:
    return SceneCard(scene_id="tmp", chapter_id="tmp", scene_seq=1, writer_brief_json=dict(FULL_BRIEF), **fields)


# ---------------------------------------------------------------------------
# 条目
# ---------------------------------------------------------------------------


def test_every_planned_line_the_brief_renders_is_read_back_as_an_item() -> None:
    brief = render_scene_structure_brief(_scene(pov_character_id="林昭", exit_change="她决定自己追"))
    items = plan_items(brief)
    assert [item.id for item in items] == [
        "pov", "crucible", "goal", "conflict", "setback", "cost", "reader_feel",
        "must_reveal", "must_withhold", "exit_change", "hook", "follow_up",
    ]
    by_id = {item.id: item for item in items}
    assert by_id["must_withhold"].planned == "取信的是她哥哥" and by_id["must_withhold"].label == "先别揭"
    assert by_id["follow_up"].planned.startswith("Reaction (反应): 林昭在雨里站了很久")


def test_unplanned_beats_and_a_missing_section_give_no_items() -> None:
    brief = render_scene_structure_brief(
        SceneCard(scene_id="tmp", chapter_id="tmp", scene_seq=2, writer_brief_json={"scene_form": "reactive", "reaction": "她哭了"})
    )
    assert "Dilemma (两难): 未规划" in brief
    assert [item.id for item in plan_items(brief)] == ["reaction"], "坩埚与另外两拍是「未规划」"
    assert plan_items(None) == [] and plan_items("") == []


# ---------------------------------------------------------------------------
# schema、归一、发现
# ---------------------------------------------------------------------------


def test_the_schema_asks_for_exactly_this_scenes_items_and_drops_plan_check_without_items() -> None:
    template = load_prompt_templates()["near_final_acceptance_review"]
    items = plan_items("Goal (目标): 取旧信\nMust withhold: 取信的是她哥哥")
    schema = apply_plan_check_schema(template.structured_schema, items)
    plan = schema["properties"][PLAN_CHECK_KEY]
    assert PLAN_CHECK_KEY in schema["required"] and plan["required"] == ["goal", "must_withhold"]
    assert plan["additionalProperties"] is False
    assert plan["properties"]["goal"]["properties"]["grade"]["enum"] == list(GRADES)
    assert PLAN_CHECK_KEY not in (template.structured_schema.get("required") or []), "模板本身不变"
    bare = apply_plan_check_schema(template.structured_schema, [])
    assert PLAN_CHECK_KEY not in bare["properties"] and PLAN_CHECK_KEY not in bare["required"]


def test_answers_are_normalized_unanswered_items_are_listed_and_departures_become_findings() -> None:
    items = plan_items("Goal (目标): 取旧信\nSetback (挫折): 旧信被取走\nMust withhold: 取信的是她哥哥\nCost paid (代价): 通行证")
    check = normalize_plan_check(
        {
            "goal": {"grade": "kept", "evidence_excerpt": "林昭推开档案室的门", "note": "她进了档案室。"},
            "setback": {"grade": "部分偏离", "evidence_excerpt": "柜子里空了，  旧信已经被取走。", "note": "挫折有，但没写谁取的。"},
            "must_withhold": {"grade": "missed", "evidence_excerpt": "说刚才也有人这样报过", "note": "暗示太直白。"},
            "cost": {"grade": "maybe", "evidence_excerpt": "", "note": ""},
        },
        items,
        CONTENT,
    )
    assert [row["grade"] for row in check["items"]] == ["kept", "partly", "missed", None]
    assert check["missing"] == ["cost"] and check["complete"] is False
    assert check["counts"] == {"kept": 1, "mostly": 0, "partly": 1, "missed": 1}
    assert all(row["located"] for row in check["items"][:3]), "空白不同也能在正文里找到原句"
    findings = plan_check_findings(check)
    assert [(f["plan_item"], f["severity"]) for f in findings] == [("setback", "info"), ("must_withhold", "revision")]
    assert findings[1]["dimension"] == PLAN_DIMENSION and "先别揭" in findings[1]["issue"] and findings[1]["evidence_excerpt"]
    assert normalize_plan_check({}, [], CONTENT) is None


# ---------------------------------------------------------------------------
# 准终稿评审端到端
# ---------------------------------------------------------------------------


class _ReviewClient(OnlineAccountedExecution):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            request_id=f"req_{len(self.requests)}",
            provider="test-provider",
            model="test-model",
            text=json.dumps(self.payload, ensure_ascii=False),
            structured_output=self.payload,
            response_format=request.response_format,
            raw_response={"usage": {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60}},
            usage={"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
            finish_reason="stop",
        )

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def _review_payload(plan_check: dict) -> dict:
    keys = (
        "story_necessity", "character_pressure", "forced_choice_pressure", "dialogue_edge", "information_release",
        "prose_freshness", "ending_drive", "continuity", "author_voice_match", "model_voice_risk", "reference_safety",
    )
    return {
        "near_final_status": "near_final_ready",
        "pass_flag": True,
        "overall_score": 8.4,
        "scores": {key: 8.0 for key in keys},
        "findings": [
            {
                "dimension": "dialogue_edge",
                "issue": "看门人的话太顺。",
                "evidence_excerpt": "说刚才也有人这样报过",
                "severity": "taste",
                "recommendation": "让他少说半句。",
            }
        ],
        "revision_brief": [],
        "failure_class": None,
        "requires_human_review": False,
        "scene_story_check": {"crucible_identified": True, "shape_landed": True, "verdict": "yes", "note": "成立。"},
        "plan_check": plan_check,
    }


def _seed_plan_scene() -> None:
    seed_project(PROJECT_ID, title="雨城", outline_text="")
    seed_chapter(CHAPTER_ID, project_id=PROJECT_ID, planned_scene_count=1, chapter_goal="取回旧信")
    seed_scene(
        SCENE_ID, chapter_id=CHAPTER_ID, scene_seq=1, project_id=PROJECT_ID, scene_goal="取回旧信", writer_brief_json=dict(FULL_BRIEF)
    )


def test_the_near_final_review_reports_every_plan_item_without_changing_the_verdict(session) -> None:
    _seed_plan_scene()
    brief = "Goal (目标): 从档案室取出旧信\nSetback (挫折): 未规划\nMust withhold: 取信的是她哥哥"
    client = _ReviewClient(
        _review_payload(
            {
                "goal": {"grade": "kept", "evidence_excerpt": "林昭推开档案室的门", "note": "进了档案室。"},
                "must_withhold": {"grade": "missed", "evidence_excerpt": "说刚才也有人这样报过", "note": "暗示太直白。"},
            }
        )
    )
    result = NearFinalAcceptanceService(session, llm_client=client).evaluate_scene(
        SCENE_ID,
        bundle={"bundle_id": "bundle_plan", "bundle_snapshot_hash": "hash_plan", "snapshot": {"inline_digests": {"scene_structure_brief": brief}}},
        source_draft_row_id="draft_plan",
        source_content=CONTENT,
    )
    session.commit()

    request = client.requests[0]
    user = request.messages[-1]["content"]
    assert "## Plan Items" in user and "- goal — 目标: 从档案室取出旧信" in user and "- must_withhold — 先别揭" in user
    assert "setback" not in user.split("## Plan Items", 1)[1], "未规划的拍子不列"
    schema = request.response_schema["schema"]
    assert schema["properties"][PLAN_CHECK_KEY]["required"] == ["goal", "must_withhold"]
    # 不改放行：与同一份评审不带逐条复核时（加上房风门）的结论一样
    baseline = _apply_scene_near_final_gates(_normalize_acceptance_payload(_review_payload({})), CONTENT)
    verdict_keys = ("pass_flag", "near_final_status", "failure_class")
    assert tuple(result[key] for key in verdict_keys) == tuple(baseline[key] for key in verdict_keys)
    assert [row["grade"] for row in result["plan_check"]["items"]] == ["kept", "missed"]
    # 偏离的那条进评审记录（模型自己的发现在前）
    evaluation = session.get(WriterEvaluation, result["evaluation_id"])
    dimensions = [item["dimension"] for item in evaluation.findings_json]
    assert dimensions[-2:] == ["dialogue_edge", PLAN_DIMENSION] and dimensions.count(PLAN_DIMENSION) == 1
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == SCENE_ID, AttemptTracker.step == "near_final_acceptance_review")
    ).scalars().one()
    assert attempt.details_json["plan_check"]["counts"]["missed"] == 1
    assert serialize_near_final_summary(session, SCENE_ID)["plan_check"]["items"][1]["id"] == "must_withhold"
    # 写前笔记接口读最近一次复核
    view = SceneBlueprintViewService(session).view(SCENE_ID)
    assert view["plan_check"]["items"][0]["grade_label"] == "照办" and view["plan_check"]["reviewed_at"]


def test_a_scene_without_structure_is_reviewed_as_before(session) -> None:
    _seed_plan_scene()
    client = _ReviewClient(_review_payload({}))
    result = NearFinalAcceptanceService(session, llm_client=client).evaluate_scene(
        SCENE_ID,
        bundle={"bundle_id": "bundle_plain", "bundle_snapshot_hash": "hash_plain", "snapshot": {"inline_digests": {}}},
        source_draft_row_id="draft_plain",
        source_content=CONTENT,
    )
    session.commit()
    assert "## Plan Items" not in client.requests[0].messages[-1]["content"]
    assert PLAN_CHECK_KEY not in client.requests[0].response_schema["schema"]["properties"]
    assert result["plan_check"] is None
    with SessionLocal() as other:
        assert SceneBlueprintViewService(other).view(SCENE_ID)["plan_check"] is None


# ---------------------------------------------------------------------------
# 诊断抽屉
# ---------------------------------------------------------------------------


def test_the_drawer_reads_old_evidence_only_findings_and_labels_the_review_dimensions() -> None:
    text = DiagnosisText(layer="runtime_final_scene", ref="final", content=CONTENT, paragraphs=CONTENT.split("\n"))
    row = WriterEvaluation(
        evaluation_id="ev_old",
        rubric_id="near_final_acceptance_v1",
        findings_json=[
            {"dimension": "dialogue_edge", "evidence": "说刚才也有人这样报过", "severity": "medium"},
            {"dimension": "ending_drive", "evidence": "结尾没有推力，只是回到住处。", "severity": "high"},
            {"dimension": PLAN_DIMENSION, "issue": "计划里的「先别揭」明显偏离", "evidence_excerpt": "门缝里塞着一张字条", "severity": "revision", "recommendation": "改。"},
            {},
        ],
        contract_field_refs_json={},
    )
    findings = evaluation_findings(row, text, source="review", label_for=REVIEW_DIMENSION_LABELS)
    assert [f["label"] for f in findings] == ["对白的锋芒", "结尾推力", "计划对照"]
    quoted, described, plan = findings
    assert quoted["issue"] == "说刚才也有人这样报过" and quoted["evidence"]["paragraph_index"] == 1 and not quoted["stale"]
    assert described["evidence"] is None and not described["stale"], "说明文字不是原句：不钉段、也不算过期"
    assert plan["evidence"]["paragraph_index"] == 3 and plan["severity"] == "revision"
