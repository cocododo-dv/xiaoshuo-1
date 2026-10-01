"""硬质检与软质检走进场景运行（``Orchestrator.run_scene``）：报告落库与续跑、补丁复检、重复补丁放行带结转说明、
重写分支与升级人工、假阳性忽略、硬质检回包降级、重跑之间清掉旧指针。回包校验在 test_qc_engine_soft_payload.py，
调用失败的降级在 test_qc_engine_degradation.py。"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.llm_task_runner import (
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.near_final import NearFinalAcceptanceService, NearFinalPlanningService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import ScenePipelineOnlineFake
from tests.support.qc import (
    FakeQcClient,
    FakeSoftQcClient,
    allow_legacy_neutral_required_fact_gap as _allow_legacy_neutral_required_fact_gap,
    qc_payload as _base_qc_payload,
    seed_qc_scene as _seed_scene,
    soft_qc_payload as _base_soft_qc_payload,
)

QC_REPORT_ID_RE = re.compile(r"^qc_report_CH100_SC01_\d{8}T\d{12}Z_[0-9a-f]{12}$")


# 合成场景：有「选」、有代价、结尾有动作——过得了准终稿的房风场景机制门（B03-16b：以前产品代码里有一条认
# 「Provider-generated」字头的旁路替测试跳过这道门，现在删了）
STYLE_SCENE_TEXT = "Provider-generated style scene text. She has to choose, and the cost is the ledger. A red envelope changes hands."

PATCHED_SCENE_TEXT = "Provider-generated patched scene text. She has to choose, and the cost is the ledger. A red envelope changes hands."


class FakeSceneClient(AccountedGenerateMixin):
    def __init__(self, *, satisfied_source: bool = True) -> None:
        self.requests: list[LLMRequest] = []
        self.satisfied_source = satisfied_source

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            scene_text = "Provider-generated neutral scene text."
            if self.satisfied_source:
                scene_text += " A red envelope changes hands."
            payload = {
                "scene_text": scene_text,
                "continuity_notes": ["kept the reunion tense"],
            }
            request_id = "resp_neutral_001"
            model = "fake-neutral-model"
            usage = {"input_tokens": 111, "output_tokens": 29, "total_tokens": 140}
        elif len(self.requests) == 2:
            payload = {
                "scene_text": STYLE_SCENE_TEXT,
                "style_notes": ["leaned harder into rhythm and inner tension"],
            }
            request_id = "resp_style_001"
            model = "fake-style-model"
            usage = {"input_tokens": 121, "output_tokens": 33, "total_tokens": 154}
        else:
            payload = {
                "scene_text": PATCHED_SCENE_TEXT,
                "style_notes": ["applied one controlled patch pass"],
            }
            request_id = "resp_patch_001"
            model = "fake-patch-model"
            usage = {"input_tokens": 131, "output_tokens": 37, "total_tokens": 168}

        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": model,
                "usage": usage,
                "finish_reason": "stop",
            },
            usage=usage,
            finish_reason="stop",
        )


class FakeFixedSceneClient(AccountedGenerateMixin):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("unexpected scene generation request")
        self.requests.append(request)
        payload = self.payloads.pop(0)
        return LLMResponse(
            request_id=f"resp_scene_{len(self.requests):03d}",
            provider="fake-provider",
            model="fake-scene-model",
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": f"resp_scene_{len(self.requests):03d}",
                "model": "fake-scene-model",
                "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            finish_reason="stop",
        )


def _make_orchestrator(
    session,
    *,
    hard_qc_payload: dict,
    soft_qc_payloads: list[dict] | None = None,
    scene_client: FakeSceneClient | None = None,
) -> Orchestrator:
    # 假生成退役后，蓝图 / 章节架构 / 角色压力 / 准定稿验收等支撑节点不再有离线
    # 兜底，必须显式注入在线记账替身；场景正文与 QC 仍由各自的 Fake 客户端提供。
    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=scene_client or FakeSceneClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=FakeQcClient(hard_qc_payload)),
        soft_qc_engine=SoftQcEngine(
            session,
            llm_client=FakeSoftQcClient(soft_qc_payloads or []),
        ),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=support)
    return orchestrator


class _QcPayloadRunner:
    """假 LLMNodeRunner：原样交回一份质检回答（不经记账）。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(
            llm_call_id=f"llm_call_soft_{len(self.calls)}",
            response=SimpleNamespace(structured_output=dict(self.payload)),
        )


def test_build_qc_report_id_uses_sortable_timestamp_prefix() -> None:
    from novel_system.services import qc_engine as qc_engine_module

    first = qc_engine_module._build_qc_report_id(
        "CH100_SC01",
        timestamp="20260531T130000000000Z",
        random_hex="ffffffffffff",
    )
    second = qc_engine_module._build_qc_report_id(
        "CH100_SC01",
        timestamp="20260531T130000000001Z",
        random_hex="000000000000",
    )

    assert QC_REPORT_ID_RE.match(first)
    assert QC_REPORT_ID_RE.match(second)
    assert first < second


def test_run_scene_hard_qc_pass_persists_report_and_continues(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    reports = session.execute(select(QcReport).order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())).scalars().all()
    hard_report = next(report for report in reports if report.qc_type == "hard_qc")
    soft_report = next(report for report in reports if report.qc_type == "soft_qc")
    style_draft = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "style_draft")
    ).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert result["soft_qc"]["branch"] == "continue"
    assert QC_REPORT_ID_RE.match(result["hard_qc"]["qc_report_id"])
    assert QC_REPORT_ID_RE.match(result["soft_qc"]["qc_report_id"])
    assert hard_report.qc_type == "hard_qc"
    assert hard_report.source_draft_row_id == state.current_neutral_draft_row_id
    assert hard_report.source_bundle_id == state.current_bundle_id
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.pass_flag == 1
    assert hard_report.next_action == "pass"
    assert soft_report.qc_type == "soft_qc"
    assert soft_report.source_draft_row_id == style_draft.row_id
    assert soft_report.resolution_code == "soft_pass"
    assert soft_report.pass_flag == 1
    assert soft_report.next_action == "pass"
    assert state.current_qc_report_id == soft_report.qc_report_id
    assert state.current_human_review_event_id is None
    assert style_draft.content == STYLE_SCENE_TEXT
    assert final_scene.content == style_draft.content
    assert final_scene.generation_llm_call_id == style_draft.generation_llm_call_id
    assert state.current_style_draft_row_id == style_draft.row_id
    assert state.current_final_scene_row_id == final_scene.row_id
    assert [attempt.step for attempt in attempts if attempt.step in {"style_draft", "soft_qc", "finalize"}] == [
        "style_draft",
        "soft_qc",
        "finalize",
    ]
    finalize_attempt = next(attempt for attempt in attempts if attempt.step == "finalize")
    assert finalize_attempt.details_json["source_style_draft_row_id"] == style_draft.row_id
    assert finalize_attempt.details_json["source_qc_report_id"] == soft_report.qc_report_id


def test_run_scene_soft_qc_waive_preserves_carry_note_details_and_finalizes(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_waive",
                next_action="pass_with_notes",
                carry_forward_note=True,
                note_scope="chapter_memory",
                carry_note_text="Keep the envelope motif in future callbacks.",
            )
        ],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    style_draft = session.execute(select(SceneDraft).where(SceneDraft.stage == "style_draft")).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    scene_memory = session.execute(select(SceneMemory).where(SceneMemory.scene_id == "CH100_SC01")).scalars().one()

    assert result["soft_qc"]["branch"] == "waive"
    assert report.resolution_code == "soft_waive"
    assert report.next_action == "pass_with_notes"
    assert report.pass_flag == 1
    assert report.rewrite_brief_json == [
        {
            "kind": "carry_forward_note",
            "note_scope": "chapter_memory",
            "carry_note_text": "Keep the envelope motif in future callbacks.",
        }
    ]
    assert scene_memory.carry_notes_json == [
        {
            "kind": "carry_forward_note",
            "note_scope": "chapter_memory",
            "carry_note_text": "Keep the envelope motif in future callbacks.",
        }
    ]
    assert final_scene.content == style_draft.content
    assert final_scene.generation_llm_call_id == style_draft.generation_llm_call_id


def test_run_scene_soft_qc_patch_rechecks_before_finalize(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph.", "Move the red envelope beat earlier."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
            ),
        ],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    drafts = session.execute(select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())).scalars().all()
    style_draft = next(draft for draft in drafts if draft.stage == "style_draft")
    patch_draft = next(draft for draft in drafts if draft.stage == "style_patch")
    final_scene = session.execute(select(FinalScene)).scalars().one()
    reports = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc").order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())).scalars().all()
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()
    state = session.get(SceneRunState, "CH100_SC01")

    assert result["soft_qc"]["branch"] == "continue"
    assert len(reports) == 2
    assert reports[0].next_action == "patch"
    assert reports[1].next_action == "pass"
    assert patch_draft.content == PATCHED_SCENE_TEXT
    assert patch_draft.content != style_draft.content
    assert final_scene.content == patch_draft.content
    assert final_scene.generation_llm_call_id == patch_draft.generation_llm_call_id
    assert state.current_style_draft_row_id == patch_draft.row_id
    assert state.current_final_scene_row_id == final_scene.row_id
    assert state.soft_patch_count == 1
    assert [attempt.step for attempt in attempts if attempt.step in {"style_draft", "soft_qc", "soft_patch", "finalize"}] == [
        "style_draft",
        "soft_qc",
        "soft_patch",
        "soft_qc",
        "finalize",
    ]
    patch_attempt = next(attempt for attempt in attempts if attempt.step == "soft_patch")
    assert patch_attempt.details_json["source_qc_report_id"] == reports[0].qc_report_id
    assert patch_attempt.details_json["source_style_draft_row_id"] == style_draft.row_id


def test_run_scene_soft_qc_patch_repeat_waives_with_carry_note(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening still feels flat."}],
                rewrite_brief=["Add sharper contrast in the first beats."],
            ),
        ],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    final_scene = session.execute(select(FinalScene)).scalars().one()
    events = session.execute(select(HumanReviewEvent)).scalars().all()
    reports = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc").order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())).scalars().all()
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()

    assert result["scene_status"] == "archived"
    assert result["soft_qc"]["branch"] == "waive"
    assert state.current_final_scene_row_id == final_scene.row_id
    assert events == []
    assert reports[-1].resolution_code == "soft_waive"
    assert reports[-1].next_action == "pass_with_notes"
    assert reports[-1].pass_flag == 1
    assert any(entry.get("kind") == "carry_forward_note" for entry in reports[-1].rewrite_brief_json)
    assert [attempt.step for attempt in attempts if attempt.step in {"style_draft", "soft_qc", "soft_patch"}] == [
        "style_draft",
        "soft_qc",
        "soft_patch",
        "soft_qc",
    ]


def test_run_scene_drops_llm_pronoun_findings_from_hard_qc(session) -> None:
    """质检模型的代词意见一律不收（LLM_PRONOUN_ISSUE_KEYS）：只剩它撑着的非 pass 结论改判 pass。"""
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.pov_character_id = "LIN_CEN"
    scene.onstage_chars_json = ["LIN_CEN", "许望", "幸存者阿砚"]
    scene.must_include_text = ""
    neutral_text = (
        "林岑把残片插入档案柜。许望站在她身后，记录潮声倒退的三秒。"
        "她按下播放键，听见幸存者阿砚的呼吸，然后把证据拆成两份。"
    )
    session.commit()

    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[
                {
                    "issue_key": "character_pronoun_ambiguity",
                    "message": "许望的代词未明确指定，可能导致角色身份混淆。",
                },
                {
                    "issue_key": "character_role_inconsistency",
                    "message": "幸存者阿砚的角色职责未在场景中体现，需补充其存在感或行动线索。",
                },
            ],
        ),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
        scene_client=FakeFixedSceneClient(
            [
                {"scene_text": neutral_text, "continuity_notes": []},
                {"scene_text": neutral_text, "style_notes": []},
            ]
        ),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    hard_report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert state.current_final_scene_row_id is not None
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.issues_json == []


def test_soft_qc_does_not_waive_a_repeat_patch_that_still_has_a_verified_issue(session) -> None:
    """Wave 2 语义：一次受控补丁之后软质检仍要补丁，而且问题是已证实的 Q1（正文里真有场景卡的禁用词）→ 阻断转人工，
    不豁免。（这条以前用确定性代词漂移造 Q1；那个检测器随声线卡删了，重评 R8。）"""
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.forbidden_text = "青花瓷"
    content = "林岑把青花瓷残片放在灯下。她没有回头。"
    session.add(
        SceneDraft(
            row_id="draft_patch_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="style_draft",
            content=content,
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    state = session.get(SceneRunState, "CH100_SC01")
    state.soft_patch_count = 1  # 已经补过一次
    state.active_execution_id = "exec-soft-repeat"
    state.run_execution_status = "active"
    session.commit()
    runner = _QcPayloadRunner(
        _base_soft_qc_payload(
            resolution_code="soft_patch",
            next_action="patch",
            issues=[{"issue_key": "forbidden_text", "message": "正文仍用了场景卡禁用的青花瓷。"}],
            rewrite_brief=["把青花瓷换掉。"],
        )
    )
    token = begin_llm_execution("exec-soft-repeat")
    try:
        decision = SoftQcEngine(session, llm_runner=runner).evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            source_draft_row_id="draft_patch_CH100_SC01",
            source_draft_content=content,
            execution_step_key="soft_qc:1",
        )
    finally:
        end_llm_execution(token)
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    qc_report = session.get(QcReport, decision.qc_report_id)
    assert decision.branch == "human_review_required"
    assert decision.stop_reason == "blocking_soft_qc_issue"
    assert QC_REPORT_ID_RE.match(decision.qc_report_id)
    assert state.scene_status == "human_review_required"
    assert state.current_qc_report_id == decision.qc_report_id
    assert len(session.execute(select(HumanReviewEvent)).scalars().all()) == 1
    assert qc_report.resolution_code == "soft_block_human"
    assert qc_report.next_action == "human_review_required"
    issue = next(item for item in qc_report.issues_json if item["issue_key"] == "forbidden_text")
    assert issue["quality_level"] == "Q1"
    assert issue["blocking"] is True
    assert issue["verified_by"] == "scene_card_forbidden_term"


def test_hard_qc_keeps_a_missing_required_group_claim(session) -> None:
    """批准#11（B04-04）：模型说「必写的第二组没写」，而正文确实只写了第一组——这不是「被场景卡否定」的误报，
    照常作为已证实的 Q1 退回改写（整段口径下它会被当成误报删掉）。"""
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.must_include_text = "主角交出钥匙，门外传来警笛"
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = "exec-hard-groups"
    state.run_execution_status = "active"
    session.commit()
    content = "他犹豫很久，最后主角交出钥匙。夜很静。"
    runner = _QcPayloadRunner(
        _base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "场景卡要求门外传来警笛，正文没有。"}],
        )
    )
    token = begin_llm_execution("exec-hard-groups")
    try:
        decision = HardQcEngine(session, llm_runner=runner).evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            neutral_draft_row_id="draft_neutral_CH100_SC01",
            neutral_content=content,
            execution_step_key="hard_qc:0",
        )
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.get(QcReport, decision.qc_report_id)
    assert decision.branch == "rewrite_partial"
    assert report.resolution_code == "hard_fail_partial"
    issue = report.issues_json[0]
    assert issue["issue_key"] == "missing_required_text"
    assert issue["quality_level"] == "Q1"
    assert issue["evidence_spans"] == [{"text": "门外传来警笛"}]


def test_soft_qc_patch_brief_carries_the_reviewers_located_style_deviations(session) -> None:
    """批准#13b（B04-20）：评审按维给的定位改法（style_deviations 的 patch_brief）写进修补简报——补丁读的是
    instruction 条目，以前这些改法校验完就丢了，补丁只拿到笼统的「这一维不像」。"""
    _seed_scene(session)
    content = "林岑像一只受惊的鸟，把旧信塞回案卷。她没有回头。"
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = "exec-soft-deviation"
    state.run_execution_status = "active"
    session.commit()
    payload = _base_soft_qc_payload(
        resolution_code="soft_patch",
        next_action="patch",
        issues=[{"issue_key": "style_reference.language.rhetoric", "message": "比喻太书面。"}],
        rewrite_brief=["language.rhetoric：比喻换成样例那种日常器物。"],
        style_deviations=[
            {
                "dimension": "language.rhetoric",
                "severity": "medium",
                "patch_brief": "第一句的比喻换成样例那种日常器物的比喻，别用鸟。",
                "evidence": "像一只受惊的鸟",
            },
            {"dimension": "language.rhetoric", "severity": "low", "patch_brief": ""},
        ],
    )
    token = begin_llm_execution("exec-soft-deviation")
    try:
        decision = SoftQcEngine(session, llm_runner=_QcPayloadRunner(payload)).evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            source_draft_row_id="draft_style_CH100_SC01",
            source_draft_content=content,
            execution_step_key="soft_qc:0",
        )
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.get(QcReport, decision.qc_report_id)
    assert decision.branch == "patch"
    instructions = [entry["instruction"] for entry in report.rewrite_brief_json if entry.get("instruction")]
    assert instructions == [
        "language.rhetoric：比喻换成样例那种日常器物。",
        "language.rhetoric：第一句的比喻换成样例那种日常器物的比喻，别用鸟。（原稿：「像一只受惊的鸟」）",
    ]
    deviation = report.rewrite_brief_json[1]
    assert deviation["kind"] == "style_deviation"
    assert deviation["dimension"] == "language.rhetoric" and deviation["severity"] == "medium"


def test_run_scene_hard_qc_rewrite_branch_updates_counters_and_stops_before_style_generation(
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            # Wave 2：阻断需 verified Q1——must_include 确实缺失（确定性复核成立）
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    report = session.execute(select(QcReport)).scalars().one()

    assert result["scene_status"] == "hard_qc_partial_rewrite_required"
    assert result["hard_qc"]["branch"] == "rewrite_partial"
    assert report.resolution_code == "hard_fail_partial"
    assert report.issues_json[0]["quality_level"] == "Q1"
    assert report.issues_json[0]["verified_by"] == "scene_card_required_text"
    assert state.hard_partial_rewrite_count == 1
    assert state.hard_full_rewrite_count == 0
    assert state.repeat_issue_key == "missing_required_text"
    assert state.repeat_issue_count == 1
    assert state.current_final_scene_row_id is None
    assert session.execute(select(SceneDraft).where(SceneDraft.stage == "style_draft")).scalars().all() == []
    assert session.execute(select(FinalScene)).scalars().all() == []


def test_hard_qc_report_adds_evidence_and_constraint_conflict_metadata(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.hook = "以死亡证明作为雨夜钩子。"
    scene.must_include_text = "死亡证明必须出现在开场。"
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="雨水打湿死亡证明，灯光忽然熄灭。",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient(
            _base_qc_payload(
                resolution_code="hard_fail_partial",
                next_action="partial_rewrite",
                issues=[
                    {
                        "issue_key": "unsafe_concrete_term",
                        "message": "Replace 死亡证明 with a neutral clue.",
                    }
                ],
            )
        ),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="雨水打湿死亡证明，灯光忽然熄灭。",
    )
    session.commit()

    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = report.issues_json[0]
    rewrite = report.rewrite_brief_json[0]

    assert decision.branch == "human_review_required"
    assert issue["severity"] == "high"
    assert issue["human_readable_reason"]
    assert issue["evidence_spans"][0]["text"] == "死亡证明"
    assert issue["conflicts_with"][0]["constraint_source"] == "scene_card.hook"
    assert issue["conflicts_with"][0]["term"] == "死亡证明"
    assert rewrite["constraint_source"] == "hard_qc"
    assert rewrite["conflicts_with"][0]["constraint_source"] == "scene_card.hook"


def test_hard_qc_required_term_evidence_does_not_force_human_review(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.must_include_text = "证人"
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="证人站在门边，主角做出了决定。",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient(
            _base_qc_payload(
                resolution_code="hard_fail_partial",
                next_action="partial_rewrite",
                issues=[
                    {
                        "issue_key": "missing_relation_digest_argument",
                        "message": "Add the evidence-vs-speed argument while protecting the 证人.",
                    }
                ],
            )
        ),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="证人站在门边，主角做出了决定。",
    )
    session.commit()

    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = report.issues_json[0]

    # Wave 2：must_include 已满足 → LLM 意见无确定性佐证 → 降 Q2 继续（不重写也不升审）
    assert decision.branch == "continue"
    assert report.next_action == "pass"
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["evidence_spans"][0]["text"] == "证人"
    assert issue["conflicts_with"] == []


def test_run_scene_repeated_hard_qc_rewrite_escalates_to_human_review(
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    first = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    first_result = first.run_scene("CH100_SC01")
    session.commit()

    second = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接仍未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    second_result = second.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    event = session.execute(select(HumanReviewEvent)).scalars().one()

    assert first_result["scene_status"] == "hard_qc_partial_rewrite_required"
    assert second_result["scene_status"] == "human_review_required"
    assert second_result["hard_qc"]["branch"] == "human_review_required"
    assert second_result["hard_qc"]["stop_reason"] == "repeat_issue_key_limit"
    assert state.repeat_issue_key == "missing_required_text"
    assert state.repeat_issue_count == 2
    assert state.hard_partial_rewrite_count == 2
    assert state.current_human_review_event_id == event.event_id
    assert state.current_final_scene_row_id is None


def test_run_scene_ignores_hard_qc_forbidden_false_positive_when_required_text_is_present(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[
                {
                    "issue_key": "forbidden_text",
                    "message": "Remove the forbidden text 'A red envelope changes hands.' from the draft.",
                }
            ],
        ),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
        scene_client=FakeSceneClient(satisfied_source=True),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    hard_report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.pass_flag == 1
    assert hard_report.issues_json == []
    assert final_scene.content


def test_run_scene_ignores_hard_qc_hook_and_style_false_positives_when_source_is_satisfied(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.hook = "red envelope changes hands"
    session.commit()
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[
                {
                    "issue_key": "unsupported_event",
                    "message": "The red envelope changes hands hook is unsupported by the bundle.",
                },
                {
                    "issue_key": "style_compliance",
                    "message": "The prose should be handled by soft QC instead.",
                },
            ],
        ),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
        scene_client=FakeSceneClient(satisfied_source=True),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    hard_report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.issues_json == []


def test_hard_qc_engine_escalates_repeated_issue_key_to_human_review(session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, "CH100_SC01")
    state.repeat_issue_key = "same_issue"
    state.repeat_issue_count = 1
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient(
            _base_qc_payload(
                resolution_code="hard_fail_partial",
                next_action="partial_rewrite",
                issues=[{"issue_key": "same_issue", "message": "The same blocker appeared again."}],
            )
        ),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Neutral draft under review.",
    )
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    event = session.execute(select(HumanReviewEvent)).scalars().one()

    assert decision.branch == "human_review_required"
    assert state.repeat_issue_key == "same_issue"
    assert state.repeat_issue_count == 2
    assert state.current_human_review_event_id == event.event_id
    assert event.event_source == "scene_generation"
    assert event.status == "needs_followup"
    assert event.details_json["trigger_reason"] == "repeat_issue_key_limit"
    assert event.details_json["recommended_action"] == "human_review_required"


def test_hard_qc_engine_sends_structured_response_schema(session) -> None:
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    hard_client = FakeQcClient(_base_qc_payload(resolution_code="hard_pass", next_action="pass"))
    engine = HardQcEngine(session, llm_client=hard_client)

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Neutral draft under review.",
    )
    session.commit()

    request = hard_client.requests[0]
    assert decision.branch == "continue"
    assert request.response_schema["name"] == "hard_qc"
    assert request.response_schema["schema"]["required"] == [
        "resolution_code",
        "pass_flag",
        "next_action",
        "issues",
        "rewrite_brief",
    ]
    assert (
        "Required top-level JSON keys: resolution_code, pass_flag, next_action, issues, rewrite_brief"
        in request.messages[1]["content"]
    )


def test_hard_qc_engine_degrades_malformed_payload_to_continue_with_warning(session) -> None:
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient({"passed": False, "issues": [{"severity": "hard", "message": "bad shape"}]}),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Neutral draft under review.",
    )
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "hard_qc")).scalars().one()
    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "hard_qc")).scalars().one()

    # Wave 2（§5.4/§7.7）：payload 无效 = QC 自身失败——降级续跑 + Q2 警告，不再断头
    assert decision.branch == "continue"
    assert decision.should_continue is True
    assert decision.stop_reason == "invalid_hard_qc_payload"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert llm_call.error_code is None
    assert report.resolution_code == "hard_pass"
    assert report.next_action == "pass"
    assert report.pass_flag == 1
    warning = next(issue for issue in report.issues_json if issue["issue_key"] == "invalid_hard_qc_payload")
    assert warning["quality_level"] == "Q2"
    assert warning["blocking"] is False
    assert "validation failed" in warning["message"]
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []


def test_run_scene_clears_stale_pointers_across_blocked_and_successful_reruns(
    client, session, monkeypatch
) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    state = session.get(SceneRunState, "CH100_SC01")
    state.current_style_draft_row_id = "draft_style_old"
    state.current_final_scene_row_id = "final_scene_old"
    state.current_human_review_event_id = "human_review_old"
    session.commit()

    blocked = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    blocked_result = blocked.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    blocked_event_id = state.current_human_review_event_id
    assert blocked_result["current_final_scene_row_id"] is None
    assert state.current_style_draft_row_id is None
    assert state.current_final_scene_row_id is None
    assert blocked_event_id is None

    state.current_human_review_event_id = "human_review_stale_from_previous_block"
    state.total_attempt_count = state.attempt_budget
    attempts_before_rerun = state.total_attempt_count
    session.commit()

    topup = client.post(
        "/api/v1/scenes/CH100_SC01/budget/topup",
        headers={"X-Idempotency-Key": "qc-stale-pointer-rerun-attempt-topup"},
        json={
            "extra_attempts": 10,
            "reason": "exercise the successful rerun after an exhausted lifecycle budget",
        },
    )
    assert topup.status_code == 200, topup.text
    session.expire_all()

    rerun = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
    )

    rerun_result = rerun.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    assert rerun_result["scene_status"] == "archived"
    assert rerun_result["current_final_scene_row_id"] == state.current_final_scene_row_id
    assert rerun_result["current_human_review_event_id"] is None
    assert state.current_style_draft_row_id.startswith("draft_style_CH100_SC01_v")
    assert state.current_final_scene_row_id.startswith("final_scene_CH100_SC01_v")
    assert state.current_human_review_event_id is None
    assert state.total_attempt_count == attempts_before_rerun + 1


def test_run_scene_resets_soft_patch_state_between_reruns(session) -> None:
    _seed_scene(session)
    first = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph.", "Move the red envelope beat earlier."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
            ),
        ],
    )

    first_result = first.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    first_neutral_row_id = state.current_neutral_draft_row_id
    first_qc_report_id = state.current_qc_report_id

    rerun = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening still needs work."}],
                rewrite_brief=["Tighten the first paragraph again."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
            ),
        ],
    )

    rerun_result = rerun.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()
    human_reviews = session.execute(select(HumanReviewEvent)).scalars().all()

    assert first_result["scene_status"] == "archived"
    assert rerun_result["scene_status"] == "archived"
    assert state.soft_patch_count == 1
    assert state.current_neutral_draft_row_id != first_neutral_row_id
    assert session.get(SceneDraft, first_neutral_row_id) is not None
    assert session.get(SceneDraft, state.current_neutral_draft_row_id) is not None
    assert state.current_qc_report_id != first_qc_report_id
    assert state.current_human_review_event_id is None
    assert len([attempt for attempt in attempts if attempt.step == "neutral_draft"]) == 2
    assert len([attempt for attempt in attempts if attempt.step == "soft_patch"]) == 2
    assert len([attempt for attempt in attempts if attempt.step == "finalize"]) == 2
    assert human_reviews == []
