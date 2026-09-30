"""Shared fakes and seeders of the scene-run checkpoint resume tests (``test_scene_run_checkpoint_*.py``).

Accounted generation clients, soft-QC / near-final / archive stubs, the resume-scene seeder and the autouse
fixture that routes default pipeline nodes through the accounted online fake (a test module enables it by
importing it). ``test_style_fidelity_pipeline_v3.py`` reuses the fakes. Never import a ``test_*.py`` module here.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterState,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    RevisionCandidate,
    SceneCard,
    SceneRunState,
    StoryProject,
    WriterEvaluation,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse, OnlineAccountedExecution
from novel_system.services.llm_task_runner import LLMNodeRunner
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.near_final import NearFinalPlanningService
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.qc_engine import SoftQcDecision
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_blueprint import SceneBlueprintService
from tests.real_llm_fakes import ScenePipelineOnlineFake


@pytest.fixture(autouse=True)
def _accounted_online_default_orchestrator_runner(monkeypatch) -> None:
    """Exercise default pipeline nodes through an accounted online test provider."""

    monkeypatch.setattr(
        "novel_system.services.orchestrator.LLMNodeRunner",
        lambda session: LLMNodeRunner(
            session,
            llm_client=ScenePipelineOnlineFake(),
        ),
    )


class _AccountedTestClient(OnlineAccountedExecution):
    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        try:
            response = self.generate(request)
        except Exception as exc:
            accounting_hook.after_error(
                handle,
                request=request,
                error=exc,
                raw_response=None,
                provider_request_id=None,
                latency_ms=1,
            )
            raise
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


_DURABLE_SCENE_VARIANTS = (
    "门轴在雨声里轻响，值夜人收起账册，把最后一盏灯推到窗前。",
    "她没有立刻回答，只用指尖抹去杯沿的水痕，等走廊重新安静。",
    "钟声落下时，他已越过空院；纸页贴在胸前，被冷风吹得发颤。",
    "先传来钥匙碰撞，随后黑暗里亮起火星，照见墙角未干的泥印。",
    "旧信压在石块下面，孩子绕开积水，将约定的红绳系回门环。",
    "炉灰忽然塌陷。两个人同时停手，谁也没有去碰露出的铜片。",
)


def _durable_scene_text(request_count: int) -> str:
    return _DURABLE_SCENE_VARIANTS[(request_count - 1) % len(_DURABLE_SCENE_VARIANTS)]


class _CountingGenerationClient(_AccountedTestClient):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        payload = {"scene_text": _durable_scene_text(len(self.requests))}
        return _response(payload, f"generation-{len(self.requests)}")


class _PlanningCheckpointClient(_AccountedTestClient):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "scene_blueprint":
            payload = {
                "visible_desire": "prove the checkpoint",
                "forced_choice": "continue or retreat",
                "price_paid": "lose time",
                "information_release": "the ledger is durable",
                "relationship_turn": "trust shifts",
                "image_anchor": "a checkpoint lamp",
                "ending_action": "the lamp turns green",
                "next_scene_pull": "what survives the retry",
                "anti_summary_rule": "end on the lamp",
            }
        elif request.node_id == "chapter_story_architecture":
            payload = {
                "chapter_promise": "the checkpoint must preserve the accepted plan",
                "escalation_path": ["record the plan", "interrupt the run", "resume without replay"],
                "reveal_plan": ["the durable artifact survives the interruption"],
                "payoff_target": "resume from the next provider call",
                "character_shift": "the operator trusts durable state",
                "ending_question": "does the checkpoint survive",
            }
        elif request.node_id == "character_pressure_blueprint":
            payload = {
                "surface_goal": "finish the interrupted scene",
                "hidden_fear": "the accepted plan was lost",
                "wrong_belief": "a retry must start over",
                "shame_point": "replaying work would hide a broken checkpoint",
                "avoidance_strategy": "restart instead of checking durable state",
                "relationship_debt": "preserve the prior operator's accepted work",
                "current_mask": "calm operational certainty",
            }
        else:
            raise AssertionError(f"unexpected planning request {request.node_id}")
        return _response(payload, f"planning-{request.node_id}-{len(self.requests)}")


class _FailBundleAfterPlanning:
    def build(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("stop after planning checkpoint")


def _planning_checkpoint_orchestrator(session, client: _PlanningCheckpointClient) -> Orchestrator:
    orchestrator = Orchestrator(
        session,
        scene_generation_service=_FailBeforeNeutral(),
        planning_service=NearFinalPlanningService(session, llm_client=client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=client)
    return orchestrator


class _FailDeTemplateClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            raise ValueError("de-template provider failed")
        payload = {"scene_text": _durable_scene_text(len(self.requests))}
        return _response(payload, f"generation-{len(self.requests)}")


class _SettledButUnparseableGenerationClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return _response({"unexpected": "provider succeeded without scene_text"}, "generation-unparseable")


class _FailAutoCritiquePatchClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            raise ValueError("auto-critique patch provider failed")
        return _response(
            {"scene_text": _durable_scene_text(len(self.requests))},
            f"generation-{len(self.requests)}",
        )


class _UnparseableAutoCritiquePatchClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            return _response(
                {"unexpected": "provider succeeded without scene_text"},
                "auto-patch-unparseable",
            )
        return _response(
            {"scene_text": _durable_scene_text(len(self.requests))},
            f"generation-{len(self.requests)}",
        )


class _HardPassClient(_AccountedTestClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return _response(
            {
                "resolution_code": "hard_pass",
                "pass_flag": True,
                "next_action": "pass",
                "issues": [],
                "rewrite_brief": [],
            },
            "hard-pass",
        )


class _FailAfterStyle:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        raise RuntimeError("fail after style checkpoint")


class _UnexpectedHardPromptBuilder:
    def build(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("unexpected hard QC prompt failure")


class _UnexpectedSoftQcRunner:
    def run(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("unexpected soft QC runner failure")


class _PassSoftQc:
    def __init__(self, session) -> None:
        self.session = session
        self.calls = 0

    def evaluate(
        self,
        *,
        scene_id,
        bundle,
        source_draft_row_id,
        source_draft_content,
        execution_step_key="soft_qc:0",
    ):  # noqa: ANN001, ANN201
        self.calls += 1
        report_id = f"qc_{scene_id}_soft_resume"
        report = self.session.get(QcReport, report_id)
        if report is None:
            report = QcReport(
                qc_report_id=report_id,
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                qc_type="soft_qc",
                source_draft_row_id=source_draft_row_id,
                source_bundle_id=bundle["bundle_id"],
                resolution_code="soft_pass",
                pass_flag=1,
                next_action="pass",
                issues_json=[],
                rewrite_brief_json=[],
            )
            self.session.add(report)
        state = self.session.get(SceneRunState, scene_id)
        state.current_qc_report_id = report_id
        llm_call_id = f"llm_{scene_id}_{execution_step_key}"
        if self.session.get(LlmCall, llm_call_id) is None:
            self.session.add(
                LlmCall(
                    llm_call_id=llm_call_id,
                    provider="fake",
                    model="fake",
                    step="soft_qc",
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    scope_type="scene",
                    scope_id=scene_id,
                    execution_id=state.active_execution_id,
                    execution_step_key=execution_step_key,
                    estimated_tokens=0,
                    reserved_tokens=0,
                    budget_charged_tokens=0,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-13T00:00:00Z",
                    settled_at="2026-07-13T00:00:01Z",
                )
            )
            self.session.add(
                AttemptTracker(
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    step="soft_qc",
                    status="continue",
                    source_bundle_id=bundle["bundle_id"],
                    details_json={
                        "qc_report_id": report_id,
                        "resolution_code": "soft_pass",
                        "next_action": "pass",
                        "source_draft_row_id": source_draft_row_id,
                        "human_review_event_id": None,
                        "rewrite_brief": [],
                        "llm_call_id": llm_call_id,
                        "execution_step_key": execution_step_key,
                    },
                )
            )
        return SoftQcDecision(
            branch="continue",
            qc_report_id=report_id,
            human_review_event_id=None,
            resolution_code="soft_pass",
            next_action="pass",
            should_continue=True,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )


class _SequencedSoftQc:
    def __init__(self, session, branches: dict[str, str]) -> None:
        self.session = session
        self.branches = branches
        self.calls: list[str] = []

    def evaluate(
        self,
        *,
        scene_id,
        bundle,
        source_draft_row_id,
        source_draft_content,
        execution_step_key="soft_qc:0",
    ):  # noqa: ANN001, ANN201
        del source_draft_content
        self.calls.append(execution_step_key)
        branch = self.branches[execution_step_key]
        values = {
            "patch": ("soft_patch", 0, "patch", [{"instruction": "tighten the checkpoint"}]),
            "continue": ("soft_pass", 1, "pass", []),
            "waive": (
                "soft_waive",
                1,
                "pass_with_notes",
                [{"kind": "carry_forward_note", "note_scope": "scene_memory", "carry_note_text": "keep note"}],
            ),
            "human_review_required": (
                "soft_block_human",
                0,
                "human_review_required",
                [{"instruction": "author must review"}],
            ),
        }
        resolution_code, pass_flag, next_action, rewrite_brief = values[branch]
        suffix = execution_step_key.replace(":", "_")
        report_id = f"qc_{scene_id}_{suffix}"
        llm_call_id = f"llm_{scene_id}_{suffix}"
        state = self.session.get(SceneRunState, scene_id)
        if self.session.get(QcReport, report_id) is None:
            self.session.add(
                QcReport(
                    qc_report_id=report_id,
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    qc_type="soft_qc",
                    source_draft_row_id=source_draft_row_id,
                    source_bundle_id=bundle["bundle_id"],
                    resolution_code=resolution_code,
                    pass_flag=pass_flag,
                    next_action=next_action,
                    issues_json=[],
                    rewrite_brief_json=rewrite_brief,
                )
            )
            self.session.add(
                LlmCall(
                    llm_call_id=llm_call_id,
                    provider="fake",
                    model="fake",
                    step="soft_qc",
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    scope_type="scene",
                    scope_id=scene_id,
                    execution_id=state.active_execution_id,
                    execution_step_key=execution_step_key,
                    estimated_tokens=0,
                    reserved_tokens=0,
                    budget_charged_tokens=0,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-13T00:00:00Z",
                    settled_at="2026-07-13T00:00:01Z",
                )
            )
            self.session.add(
                AttemptTracker(
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    step="soft_qc",
                    status=branch,
                    source_bundle_id=bundle["bundle_id"],
                    details_json={
                        "qc_report_id": report_id,
                        "resolution_code": resolution_code,
                        "next_action": next_action,
                        "source_draft_row_id": source_draft_row_id,
                        "human_review_event_id": None,
                        "rewrite_brief": rewrite_brief,
                        "llm_call_id": llm_call_id,
                        "execution_step_key": execution_step_key,
                    },
                )
            )
        human_review_event_id = f"review_{scene_id}" if branch == "human_review_required" else None
        if human_review_event_id is not None and self.session.get(HumanReviewEvent, human_review_event_id) is None:
            self.session.add(
                HumanReviewEvent(
                    event_id=human_review_event_id,
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    object_ref=source_draft_row_id,
                    event_source="scene_generation",
                    priority="high",
                    status="needs_followup",
                    allowed_actions_json=["inspect"],
                    result_status_map_json={"inspect": "needs_followup"},
                    details_json={
                        "replay_context": {
                            "current_qc_report_id": report_id,
                            "source_draft_row_id": source_draft_row_id,
                            "source_bundle_id": bundle["bundle_id"],
                        }
                    },
                    default_action="inspect",
                )
            )
        state.current_qc_report_id = report_id
        state.current_human_review_event_id = human_review_event_id
        if branch == "human_review_required":
            state.scene_status = "human_review_required"
        return SoftQcDecision(
            branch=branch,
            qc_report_id=report_id,
            human_review_event_id=human_review_event_id,
            resolution_code=resolution_code,
            next_action=next_action,
            should_continue=branch in {"continue", "waive"},
            stop_reason="blocking_soft_qc_issue" if branch == "human_review_required" else None,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )


class _FailNearFinal:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        raise RuntimeError("fail after soft checkpoint")


class _PassNearFinal:
    def __init__(self, session) -> None:
        self.session = session
        self.calls = 0

    def evaluate_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        scene_id = args[0]
        bundle = kwargs["bundle"]
        source_draft_row_id = kwargs["source_draft_row_id"]
        execution_step_key = kwargs["execution_step_key"]
        state = self.session.get(SceneRunState, scene_id)
        llm_call_id = f"llm_{scene_id}_{execution_step_key}"
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                provider="fake",
                model="fake",
                step="near_final_acceptance_review",
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                scope_type="scene",
                scope_id=scene_id,
                execution_id=state.active_execution_id,
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="settled",
                request_dispatched_at="2026-07-13T00:00:00Z",
                settled_at="2026-07-13T00:00:01Z",
            )
        )
        self.session.add(
            WriterEvaluation(
                evaluation_id="near-final-resume-eval",
                object_type="scene",
                object_id=scene_id,
                chapter_id="CH_RESUME",
                scene_id=scene_id,
                rubric_id="near_final_acceptance_v1",
                source_text_ref=f"source_draft:{source_draft_row_id}",
                source_bundle_id=bundle["bundle_id"],
                evaluator_llm_call_id=llm_call_id,
                lens="near_final_acceptance",
                overall_score=1.0,
                scores_json={},
                findings_json=[],
                revision_brief_json=[],
                status="completed",
            )
        )
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                step="near_final_acceptance_review",
                status="near_final_ready",
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "evaluation_id": "near-final-resume-eval",
                    "revision_candidate_id": None,
                    "source_draft_row_id": source_draft_row_id,
                    "llm_call_id": llm_call_id,
                    "failure_class": None,
                    "execution_step_key": execution_step_key,
                },
            )
        )
        return {
            "near_final_status": "near_final_ready",
            "pass_flag": True,
            "overall_score": 1.0,
            "failure_class": None,
            "requires_human_review": False,
            "evaluation_id": "near-final-resume-eval",
            "revision_candidate_id": None,
            "should_rewrite": False,
            "findings": [],
            "revision_brief": [],
        }


class _SequencedNearFinal:
    def __init__(self, session, outcomes: dict[str, str]) -> None:
        self.session = session
        self.outcomes = outcomes
        self.calls: list[str] = []

    def evaluate_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        scene_id = args[0]
        bundle = kwargs["bundle"]
        source_draft_row_id = kwargs["source_draft_row_id"]
        source_content = kwargs["source_content"]
        execution_step_key = kwargs["execution_step_key"]
        self.calls.append(execution_step_key)
        outcome = self.outcomes[execution_step_key]
        suffix = execution_step_key.replace(":", "_")
        evaluation_id = f"near_final_eval_{scene_id}_{suffix}"
        llm_call_id = f"llm_{scene_id}_{suffix}"
        candidate_id = None if outcome == "pass" else f"revision_{scene_id}_{suffix}"
        if outcome == "pass":
            near_final_status = "near_final_ready"
            pass_flag = True
            failure_class = None
            requires_human_review = False
            should_rewrite = False
            findings = []
            revision_brief = []
            overall_score = 0.9
        else:
            near_final_status = "human_review_required" if outcome == "human" else "revision_required"
            pass_flag = False
            failure_class = "reference_safety" if outcome == "human" else "prose_model_voice"
            requires_human_review = outcome == "human"
            should_rewrite = outcome == "rewrite"
            findings = [{"dimension": "prose_freshness", "issue": "needs revision"}]
            revision_brief = [{"dimension": "prose_freshness", "action": "rewrite once"}]
            overall_score = 0.5
        state = self.session.get(SceneRunState, scene_id)
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                provider="fake",
                model="fake",
                step="near_final_acceptance_review",
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                scope_type="scene",
                scope_id=scene_id,
                execution_id=state.active_execution_id,
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="settled",
                request_dispatched_at="2026-07-13T00:00:00Z",
                settled_at="2026-07-13T00:00:01Z",
            )
        )
        self.session.add(
            WriterEvaluation(
                evaluation_id=evaluation_id,
                object_type="scene",
                object_id=scene_id,
                chapter_id="CH_RESUME",
                scene_id=scene_id,
                rubric_id="near_final_acceptance_v1",
                source_text_ref=f"source_draft:{source_draft_row_id}",
                source_bundle_id=bundle["bundle_id"],
                evaluator_llm_call_id=llm_call_id,
                lens="near_final_acceptance",
                overall_score=overall_score,
                scores_json={"prose_freshness": overall_score},
                findings_json=findings,
                failure_class=failure_class,
                auto_rewrite_eligible=1 if should_rewrite else 0,
                contract_field_refs_json={},
                promotion_blockers_json=[] if should_rewrite or pass_flag else [failure_class],
                revision_brief_json=revision_brief,
                requires_human_review=1 if requires_human_review else 0,
                status="completed",
            )
        )
        if candidate_id is not None:
            self.session.add(
                RevisionCandidate(
                    revision_id=candidate_id,
                    evaluation_id=evaluation_id,
                    object_type="scene",
                    object_id=scene_id,
                    chapter_id="CH_RESUME",
                    scene_id=scene_id,
                    revision_type="near_final_scene_rewrite",
                    source_text_ref=f"source_draft:{source_draft_row_id}",
                    proposed_text=source_content,
                    instruction_json=revision_brief,
                    diff_summary_json={"failure_class": failure_class},
                    patches_json=[],
                    apply_mode="manual_or_regenerate",
                    target_text_ref=f"source_draft:{source_draft_row_id}",
                    status="candidate",
                    created_by="near_final_acceptance",
                )
            )
        if outcome == "pass":
            for candidate in self.session.execute(
                select(RevisionCandidate).where(
                    RevisionCandidate.scene_id == scene_id,
                    RevisionCandidate.status == "candidate",
                )
            ).scalars().all():
                candidate.status = "superseded"
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                step="near_final_acceptance_review",
                status=near_final_status,
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "evaluation_id": evaluation_id,
                    "revision_candidate_id": candidate_id,
                    "source_draft_row_id": source_draft_row_id,
                    "llm_call_id": llm_call_id,
                    "failure_class": failure_class,
                    "execution_step_key": execution_step_key,
                },
            )
        )
        return {
            "near_final_status": near_final_status,
            "pass_flag": pass_flag,
            "overall_score": overall_score,
            "scores": {"prose_freshness": overall_score},
            "failure_class": failure_class,
            "requires_human_review": requires_human_review,
            "evaluation_id": evaluation_id,
            "revision_candidate_id": candidate_id,
            "should_rewrite": should_rewrite,
            "findings": findings,
            "revision_brief": revision_brief,
        }


class _FailArchiveOnce:
    def archive_final_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("fail after near-final checkpoint")


class _FailBeforeNeutral:
    def generate_neutral_draft(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("stop at bundle checkpoint")


class _FailBeforeHardQc:
    def evaluate(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("stop after neutral retry")


def _response(payload: dict, request_id: str) -> LLMResponse:
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model="fake-model",
        text=json.dumps(payload),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": "fake-model",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "finish_reason": "stop",
        },
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        finish_reason="stop",
    )


def _seed_resume_scene(session) -> None:
    session.add(
        StoryProject(
            project_id="P_RESUME",
            title="Resume Project",
            outline_text="resume safely",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id="CH_RESUME",
            project_id="P_RESUME",
            planned_scene_count=1,
            chapter_goal="resume safely",
        )
    )
    session.add(ChapterState(chapter_id="CH_RESUME", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            project_id="P_RESUME",
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="checkpoint room",
            scene_goal="prove the checkpoint",
            beats_json=["write", "fail", "resume"],
            must_include_text="",
            target_length_band="short",
            scene_type="transition",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id="CH_RESUME_SC01", scene_status="ready"))
    session.commit()


def _select_first_checkpoint_candidate(session, scene_id: str) -> tuple[str, str]:
    gate = session.execute(
        select(HumanReviewEvent)
        .where(
            HumanReviewEvent.scene_id == scene_id,
            HumanReviewEvent.event_source == "candidate_selection",
        )
        .order_by(HumanReviewEvent.created_at.desc(), HumanReviewEvent.event_id.desc())
    ).scalars().first()
    assert gate is not None
    details = dict(gate.details_json or {})
    selected_row_id = details["candidate_row_ids"][0]
    gate.details_json = {
        **details,
        "decision_status": "selected",
        "selected_row_id": selected_row_id,
    }
    state = session.get(SceneRunState, scene_id)
    state.current_style_draft_row_id = selected_row_id
    state.latest_valid_draft_row_id = selected_row_id
    session.commit()
    return gate.event_id, selected_row_id


def _selection_resume_orchestrator(
    session,
    *,
    generation_client,
    soft_qc,
    near_final,
) -> Orchestrator:
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=near_final,
    )
    # These tests exercise the selection hand-off itself.  Production Best-of-N
    # remains evidence-gated and default-off; this dedicated harness explicitly
    # models an already-authorized two-candidate cell.
    orchestrator._best_of_n_count = lambda _contract, *, criticality=None: 2
    return orchestrator
