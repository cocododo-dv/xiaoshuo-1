"""Scene-run checkpoint resume · fail-closed QC, selection hand-off, planning sub-checkpoints, drafting and Best-of-N."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    GenerationPlanningArtifact,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    QcReport,
    SceneCard,
    SceneBlueprint,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.near_final import NearFinalPlanningService
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

# Importing the autouse fixture runs every test here against the accounted online fake provider.
from tests.support.checkpoint_fakes import _accounted_online_default_orchestrator_runner  # noqa: F401
from tests.support.checkpoint_fakes import (
    _durable_scene_text,
    _CountingGenerationClient,
    _PlanningCheckpointClient,
    _FailBundleAfterPlanning,
    _planning_checkpoint_orchestrator,
    _FailSecondCandidateOnceClient,
    _SettledButUnparseableGenerationClient,
    _HardPassClient,
    _FailAfterStyle,
    _UnexpectedHardPromptBuilder,
    _UnexpectedSoftQcRunner,
    _PassSoftQc,
    _FailNearFinal,
    _PassNearFinal,
    _SequencedNearFinal,
    _response,
    _seed_resume_scene,
    _select_first_checkpoint_candidate,
    _selection_resume_orchestrator,
)


def test_unexpected_hard_qc_prompt_failure_is_fail_closed_without_report_or_checkpoint(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    hard_qc = HardQcEngine(session, llm_client=_HardPassClient())
    hard_qc.prompt_builder = _UnexpectedHardPromptBuilder()
    planning_client = _PlanningCheckpointClient()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=hard_qc,
        soft_qc_engine=_FailAfterStyle(),
        planning_service=NearFinalPlanningService(session, llm_client=planning_client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=planning_client)

    with pytest.raises(RuntimeError, match="unexpected hard QC prompt failure"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:hard-qc-unexpected")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "neutral_ready"
    assert "qc_report_id" not in (state.run_checkpoint_json.get("artifact_refs") or {})
    assert session.scalar(
        select(func.count()).select_from(QcReport).where(
            QcReport.scene_id == "CH_RESUME_SC01",
            QcReport.qc_type == "hard_qc",
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.step == "hard_qc",
        )
    ) == 0
    assert len(generation_client.requests) == 1


def test_unexpected_soft_qc_runner_failure_is_fail_closed_without_report_checkpoint(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = SoftQcEngine(session, llm_runner=_UnexpectedSoftQcRunner())
    planning_client = _PlanningCheckpointClient()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=_FailNearFinal(),
        planning_service=NearFinalPlanningService(session, llm_client=planning_client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=planning_client)

    with pytest.raises(RuntimeError, match="unexpected soft QC runner failure"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc-unexpected")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    refs = state.run_checkpoint_json.get("artifact_refs") or {}
    assert "soft_qc_report_id" not in refs
    assert "soft_qc_llm_call_id" not in refs
    assert session.scalar(
        select(func.count()).select_from(QcReport).where(
            QcReport.scene_id == "CH_RESUME_SC01",
            QcReport.qc_type == "soft_qc",
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.step == "soft_qc",
        )
    ) == 0
    assert len(generation_client.requests) == 2


def test_failure_audit_snapshot_fault_persists_unrecoverable_fence_in_file_database(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    execution_id = "idempotency:audit-snapshot-fault"
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=late_failure,
    )

    def fail_snapshot(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("failure audit snapshot exploded")

    first._capture_failure_audits = fail_snapshot
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        first.run_scene(scene_id, execution_id=execution_id)

    verifier = SessionLocal()
    try:
        state = verifier.get(SceneRunState, scene_id)
        assert state is not None
        assert state.active_execution_id == execution_id
        assert state.run_execution_status == "cancelled"
        assert state.run_checkpoint == "cancelled"
        fence = state.run_checkpoint_json["unrecoverable_failure_audit"]
        assert fence["phase"] == "snapshot"
        assert fence["error_type"] == "RuntimeError"
    finally:
        verifier.close()

    provider_calls = len(generation_client.requests)
    session.expire_all()
    with pytest.raises(DomainError) as retry:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        ).run_scene(scene_id, execution_id=execution_id)
    assert retry.value.code == "RUN_EXECUTION_CANCELLED"
    assert len(generation_client.requests) == provider_calls


def test_selection_resume_audit_restore_fault_persists_unrecoverable_fence_in_file_database(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    scene = session.get(SceneCard, scene_id)
    scene.constraint_intensity = 0.9
    session.commit()
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    failing_near_final = _FailNearFinal()
    paused = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=failing_near_final,
    ).run_scene(scene_id, execution_id="idempotency:audit-restore-origin")
    assert paused["scene_status"] == "awaiting_candidate_selection"
    _select_first_checkpoint_candidate(session, scene_id)
    execution_id = "idempotency:audit-restore-resume"
    first = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=failing_near_final,
    )

    def fail_restore(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("failure audit restore exploded")

    first._restore_failure_audits = fail_restore
    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        first.resume_after_selection(scene_id, execution_id=execution_id)

    verifier = SessionLocal()
    try:
        state = verifier.get(SceneRunState, scene_id)
        assert state is not None
        assert state.active_execution_id == execution_id
        assert state.run_execution_status == "cancelled"
        assert state.run_checkpoint == "cancelled"
        fence = state.run_checkpoint_json["unrecoverable_failure_audit"]
        assert fence["phase"] == "restore"
        assert fence["error_type"] == "RuntimeError"
    finally:
        verifier.close()

    provider_calls = len(generation_client.requests)
    soft_calls = soft_qc.calls
    near_calls = failing_near_final.calls
    session.expire_all()
    with pytest.raises(DomainError) as retry:
        _selection_resume_orchestrator(
            session,
            generation_client=generation_client,
            soft_qc=soft_qc,
            near_final=failing_near_final,
        ).resume_after_selection(scene_id, execution_id=execution_id)
    assert retry.value.code == "RUN_EXECUTION_CANCELLED"
    assert len(generation_client.requests) == provider_calls
    assert soft_qc.calls == soft_calls
    assert failing_near_final.calls == near_calls


def test_selection_resume_fresh_idempotency_execution_continues_after_complete_soft_subcursor(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    scene = session.get(SceneCard, scene_id)
    scene.constraint_intensity = 0.9
    session.commit()
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    failing_near_final = _FailNearFinal()
    origin = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=failing_near_final,
    )
    paused = origin.run_scene(scene_id, execution_id="idempotency:selection-soft-origin")
    assert paused["scene_status"] == "awaiting_candidate_selection"
    gate_id, selected_row_id = _select_first_checkpoint_candidate(session, scene_id)
    resume_execution_id = "idempotency:selection-soft-resume"

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _selection_resume_orchestrator(
            session,
            generation_client=generation_client,
            soft_qc=soft_qc,
            near_final=failing_near_final,
        ).resume_after_selection(scene_id, execution_id=resume_execution_id)

    state = session.get(SceneRunState, scene_id)
    assert state.run_execution_status == "failed"
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 3
    handoff_refs = state.run_checkpoint_json["artifact_refs"]
    gate = session.get(HumanReviewEvent, gate_id)
    assert handoff_refs["selected_row_id"] == selected_row_id
    assert gate.status == "resolved"
    assert gate.details_json["resumed"] is True
    assert handoff_refs["soft_input_source_draft_row_id"] == selected_row_id
    provider_calls = len(generation_client.requests)
    style_call_ids = list(
        session.execute(
            select(LlmCall.llm_call_id)
            .where(LlmCall.scene_id == scene_id, LlmCall.step.in_(("style_draft", "de_template")))
            .order_by(LlmCall.llm_call_id)
        ).scalars()
    )

    # A real strict run publishes the recoverable author-facing state before a
    # later provider call reaches the lifecycle boundary.  The durable failed
    # soft checkpoint, rather than the old selection-wait label, owns resume.
    state.scene_status = "soft_qc_patch_required"
    session.commit()

    retry_execution_id = "idempotency:selection-soft-retry"
    result = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=_PassNearFinal(session),
    ).resume_after_selection(scene_id, execution_id=retry_execution_id)

    assert result["scene_status"] == "archived"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls
    assert list(
        session.execute(
            select(LlmCall.llm_call_id)
            .where(LlmCall.scene_id == scene_id, LlmCall.step.in_(("style_draft", "de_template")))
            .order_by(LlmCall.llm_call_id)
        ).scalars()
    ) == style_call_ids
    assert session.scalar(
        select(func.count()).select_from(HumanReviewEvent).where(
            HumanReviewEvent.scene_id == scene_id,
            HumanReviewEvent.event_source == "candidate_selection",
        )
    ) == 1
    assert session.get(HumanReviewEvent, gate_id).details_json["resumed"] is True


def test_selection_resume_same_execution_continues_after_partial_near_final_subcursor(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    scene = session.get(SceneCard, scene_id)
    scene.constraint_intensity = 0.9
    session.commit()
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )
    origin = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=near_final,
    )
    paused = origin.run_scene(scene_id, execution_id="idempotency:selection-near-origin")
    assert paused["scene_status"] == "awaiting_candidate_selection"
    gate_id, selected_row_id = _select_first_checkpoint_candidate(session, scene_id)
    resume_execution_id = "idempotency:selection-near-resume"
    first = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=near_final,
    )
    original_reconcile = first._reconcile_execution_step

    def stop_before_rewrite(step_key: str) -> None:
        if step_key == "near_final_rewrite:0":
            raise RuntimeError("stop selection resume after near eval0")
        original_reconcile(step_key)

    first._reconcile_execution_step = stop_before_rewrite
    with pytest.raises(RuntimeError, match="stop selection resume after near eval0"):
        first.resume_after_selection(scene_id, execution_id=resume_execution_id)

    state = session.get(SceneRunState, scene_id)
    assert state.run_execution_status == "failed"
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    handoff_refs = state.run_checkpoint_json["artifact_refs"]
    gate = session.get(HumanReviewEvent, gate_id)
    assert handoff_refs["selected_row_id"] == selected_row_id
    assert gate.status == "resolved"
    assert gate.details_json["resumed"] is True
    assert handoff_refs["soft_input_source_draft_row_id"] == selected_row_id
    provider_calls = len(generation_client.requests)

    result = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=near_final,
    ).resume_after_selection(scene_id, execution_id=resume_execution_id)

    assert result["scene_status"] == "archived"
    assert soft_qc.calls == 1
    assert near_final.calls == ["near_final_acceptance:0", "near_final_acceptance:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert session.scalar(
        select(func.count()).select_from(HumanReviewEvent).where(
            HumanReviewEvent.scene_id == scene_id,
            HumanReviewEvent.event_source == "candidate_selection",
        )
    ) == 1


def test_committed_neutral_and_style_checkpoint_resume_without_new_call_or_charge(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-one")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    first_used = state.scene_tokens_used
    first_attempts = state.total_attempt_count
    assert len(session.execute(select(SceneDraft)).scalars().all()) == 2
    assert len(generation_client.requests) == 2
    execution_calls = session.execute(
        select(LlmCall).where(LlmCall.execution_id == "idempotency:resume-one")
    ).scalars().all()
    assert {call.execution_step_key for call in execution_calls} >= {
        "neutral_draft",
        "hard_qc:0",
        "style_draft:0",
    }

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-one")

    session.refresh(state)
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.scene_tokens_used == first_used
    assert state.total_attempt_count == first_attempts
    assert len(session.execute(select(SceneDraft)).scalars().all()) == 2
    assert len(session.execute(select(LlmCall)).scalars().all()) >= 2
    assert len(generation_client.requests) == 2


class _RepairedFirstDraftClient(_CountingGenerationClient):
    """第一次起草给一份不合格的稿（太短），单次确定性修复给出合格稿；之后照常。"""

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = "短。" if len(self.requests) == 1 else _durable_scene_text(len(self.requests))
        return _response({"scene_text": text}, f"generation-{len(self.requests)}")


def test_neutral_checkpoint_after_an_accepted_repair_resumes_without_corruption(session) -> None:
    """首稿不合格、修复稿被采用时，中性步位的检查点记的是**修复那次调用**的步键（neutral_draft_repair）——
    以前记成 neutral_draft，而调用 id 是修复那次的，续跑的账本校验（调用的 execution_step_key 对不上）报
    RUN_CHECKPOINT_CORRUPT。"""
    _seed_resume_scene(session)
    generation_client = _RepairedFirstDraftClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-repaired")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["neutral_execution_step_key"] == "neutral_draft_repair"
    repair_call = session.get(LlmCall, refs["neutral_llm_call_id"])
    assert repair_call.execution_step_key == "neutral_draft_repair"
    assert len(generation_client.requests) == 3, "首稿 + 修复 + 风格稿"

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-repaired")

    assert len(generation_client.requests) == 3, "续跑不重放首稿、修复与风格稿"
    assert late_failure.calls == 2


@pytest.mark.parametrize(
    ("fail_before_step", "first_sub_index"),
    [
        ("planning:chapter_architecture", 0),
        ("planning:character_pressure", 1),
        ("bundle", 3),
    ],
)
def test_planning_subcheckpoints_resume_from_next_provider_without_replay(
    session,
    monkeypatch,
    fail_before_step: str,
    first_sub_index: int,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:planning-substep-{first_sub_index}"
    client = _PlanningCheckpointClient()
    planning_indices: list[int] = []
    original_save = SceneRunCheckpointService.save_checkpoint

    def observe_save(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if kwargs.get("node_key") == "planning_ready":
            planning_indices.append(kwargs.get("sub_index"))
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(SceneRunCheckpointService, "save_checkpoint", observe_save)
    first = _planning_checkpoint_orchestrator(session, client)
    if fail_before_step == "bundle":
        first.bundle_builder = _FailBundleAfterPlanning()
    else:
        original_reconcile = first._reconcile_execution_step
        failed = False

        def fail_once(step_key: str) -> None:
            nonlocal failed
            if step_key == fail_before_step and not failed:
                failed = True
                raise RuntimeError(f"stop before {step_key}")
            original_reconcile(step_key)

        first._reconcile_execution_step = fail_once

    expected_error = "stop after planning checkpoint" if fail_before_step == "bundle" else f"stop before {fail_before_step}"
    with pytest.raises(RuntimeError, match=expected_error):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "planning_ready"
    assert state.run_checkpoint_json["sub_index"] == first_sub_index
    requests_after_failure = [request.node_id for request in client.requests]

    resumed = _planning_checkpoint_orchestrator(session, client)
    resumed.bundle_builder = _FailBundleAfterPlanning()
    with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    assert state.run_checkpoint == "planning_ready"
    assert state.run_checkpoint_json["sub_index"] == 3
    assert [request.node_id for request in client.requests] == [
        "scene_blueprint",
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]
    assert [request.node_id for request in client.requests[: len(requests_after_failure)]] == requests_after_failure
    assert planning_indices == [0, 1, 2, 3]
    refs = state.run_checkpoint_json["artifact_refs"]
    hashes = state.run_checkpoint_json["artifact_hashes"]
    for prefix, step_key in (
        ("planning_scene_blueprint", "scene_blueprint"),
        ("planning_chapter_architecture", "planning:chapter_architecture"),
        ("planning_character_pressure", "planning:character_pressure"),
    ):
        assert refs[f"{prefix}_row_id"]
        assert refs[f"{prefix}_execution_step_key"] == step_key
        assert refs[f"{prefix}_llm_call_id"]
        assert refs[f"{prefix}_artifact_execution_id"] == execution_id
        assert hashes[prefix]
    assert hashes["planning"]
    assert refs["planning"]["chapter_architecture"]["row_id"] == refs["planning_chapter_architecture_row_id"]
    assert refs["planning"]["character_pressure"]["row_id"] == refs["planning_character_pressure_row_id"]


def test_missing_partial_planning_blueprint_blocks_before_next_provider(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:planning-partial-missing"
    client = _PlanningCheckpointClient()
    first = _planning_checkpoint_orchestrator(session, client)
    original_reconcile = first._reconcile_execution_step

    def fail_before_architecture(step_key: str) -> None:
        if step_key == "planning:chapter_architecture":
            raise RuntimeError("stop before architecture")
        original_reconcile(step_key)

    first._reconcile_execution_step = fail_before_architecture
    with pytest.raises(RuntimeError, match="stop before architecture"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    row_id = state.run_checkpoint_json["artifact_refs"]["planning_scene_blueprint_row_id"]
    session.delete(session.get(SceneBlueprint, row_id))
    session.commit()
    provider_count = len(client.requests)

    with pytest.raises(DomainError) as missing:
        _planning_checkpoint_orchestrator(session, client).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert len(client.requests) == provider_count


def test_tampered_partial_chapter_architecture_blocks_before_character_provider(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:planning-partial-corrupt"
    client = _PlanningCheckpointClient()
    first = _planning_checkpoint_orchestrator(session, client)
    original_reconcile = first._reconcile_execution_step

    def fail_before_character(step_key: str) -> None:
        if step_key == "planning:character_pressure":
            raise RuntimeError("stop before character pressure")
        original_reconcile(step_key)

    first._reconcile_execution_step = fail_before_character
    with pytest.raises(RuntimeError, match="stop before character pressure"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    row_id = state.run_checkpoint_json["artifact_refs"]["planning_chapter_architecture_row_id"]
    artifact = session.get(GenerationPlanningArtifact, row_id)
    artifact.payload_json = {"ending_question": "tampered"}
    session.commit()
    provider_count = len(client.requests)

    with pytest.raises(DomainError) as corrupt:
        _planning_checkpoint_orchestrator(session, client).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(client.requests) == provider_count


def test_new_execution_reuses_previous_active_planning_artifacts_with_fenced_provenance(session) -> None:
    _seed_resume_scene(session)
    client = _PlanningCheckpointClient()
    old_execution = "idempotency:planning-origin"
    first = _planning_checkpoint_orchestrator(session, client)
    first.bundle_builder = _FailBundleAfterPlanning()
    with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=old_execution)
    assert [request.node_id for request in client.requests] == [
        "scene_blueprint",
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]

    new_execution = "idempotency:planning-reuser"
    for _attempt in range(2):
        reused = _planning_checkpoint_orchestrator(session, client)
        reused.bundle_builder = _FailBundleAfterPlanning()
        with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
            reused.run_scene("CH_RESUME_SC01", execution_id=new_execution)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    hashes = state.run_checkpoint_json["artifact_hashes"]
    assert state.active_execution_id == new_execution
    assert state.run_checkpoint_json["sub_index"] == 3
    assert len(client.requests) == 3
    for prefix in (
        "planning_scene_blueprint",
        "planning_chapter_architecture",
        "planning_character_pressure",
    ):
        assert refs[f"{prefix}_reused"] is True
        assert refs[f"{prefix}_artifact_execution_id"] == old_execution
        assert hashes[f"{prefix}_provenance"]


def test_partial_planning_resume_prefers_checkpoint_row_over_newer_active_artifact(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:planning-checkpoint-row-wins"
    client = _PlanningCheckpointClient()
    first = _planning_checkpoint_orchestrator(session, client)
    original_reconcile = first._reconcile_execution_step

    def fail_before_character(step_key: str) -> None:
        if step_key == "planning:character_pressure":
            raise RuntimeError("stop before character pressure")
        original_reconcile(step_key)

    first._reconcile_execution_step = fail_before_character
    with pytest.raises(RuntimeError, match="stop before character pressure"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    checkpoint_row_id = state.run_checkpoint_json["artifact_refs"]["planning_chapter_architecture_row_id"]
    checkpoint_row = session.get(GenerationPlanningArtifact, checkpoint_row_id)
    checkpoint_row.status = "superseded"
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_chapter_story_architecture_CH_RESUME_zzzzzzzzzz",
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id="CH_RESUME",
            chapter_id="CH_RESUME",
            scene_id=None,
            payload_json={"ending_question": "newer unrelated architecture"},
            llm_call_id=None,
            source_bundle_id="newer-source",
            source_bundle_hash="newer-hash",
            status="active",
            created_by="other-scene",
            created_at="2099-01-01T00:00:00+00:00",
        )
    )
    session.commit()

    resumed = _planning_checkpoint_orchestrator(session, client)
    resumed.bundle_builder = _FailBundleAfterPlanning()
    with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    assert state.run_checkpoint_json["artifact_refs"]["planning"]["chapter_architecture"]["row_id"] == checkpoint_row_id
    assert [request.node_id for request in client.requests] == [
        "scene_blueprint",
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]


def test_settled_provider_parse_failure_restores_ledger_and_blocks_same_execution_retry(session) -> None:
    _seed_resume_scene(session)
    generation_client = _SettledButUnparseableGenerationClient()
    execution_id = "idempotency:settled-before-product"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    with pytest.raises(ValueError, match="missing scene_text"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    calls = session.execute(
        select(LlmCall).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "neutral_draft",
        )
    ).scalars().all()
    assert len(calls) == 1
    assert calls[0].accounting_status == "settled"
    assert calls[0].request_dispatched_at is not None
    assert session.execute(
        select(SceneDraft).where(SceneDraft.generation_llm_call_id == calls[0].llm_call_id)
    ).scalars().all() == []
    assert state.scene_tokens_used == session.scalar(
        select(func.sum(LlmCall.budget_charged_tokens)).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.execution_id == execution_id,
        )
    )
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert len(generation_client.requests) == provider_calls == 1


def test_same_execution_retry_before_first_checkpoint_is_resumed_and_preserves_current_pointer(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:failed-before-first-checkpoint"

    def fail_before_checkpoint(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("failed before first checkpoint")

    monkeypatch.setattr(Orchestrator, "_run_scene_pipeline", fail_before_checkpoint)
    with pytest.raises(RuntimeError, match="failed before first checkpoint"):
        Orchestrator(session).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint is None
    state.current_neutral_draft_row_id = "preserve-on-same-execution-retry"
    session.commit()

    observed: list[str | None] = []

    def observe_then_fail(self, scene_id, **kwargs):  # noqa: ANN001, ANN003
        observed.append(self.session.get(SceneRunState, scene_id).current_neutral_draft_row_id)
        raise RuntimeError("same execution retry")

    monkeypatch.setattr(Orchestrator, "_run_scene_pipeline", observe_then_fail)
    with pytest.raises(RuntimeError, match="same execution retry"):
        Orchestrator(session).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert observed == ["preserve-on-same-execution-retry"]


def test_best_of_n_blocks_dispatched_missing_second_candidate_without_repeating_provider(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    generation_client = _FailSecondCandidateOnceClient()
    late_failure = _FailAfterStyle()
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(ValueError, match="candidate two failed once"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-candidates")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "hard_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    assert state.run_checkpoint_json["artifact_refs"]["style_candidate_row_ids"] == [
        "draft_style_cand_CH_RESUME_SC01_v1_0"
    ]
    assert len(generation_client.requests) == 3

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-candidates")

    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    session.refresh(state)
    assert state.run_checkpoint == "hard_qc_ready"
    assert state.run_checkpoint_json["artifact_refs"]["style_candidate_row_ids"] == [
        "draft_style_cand_CH_RESUME_SC01_v1_0"
    ]
    assert len(generation_client.requests) == 3
    draft_ids = session.execute(select(SceneDraft.row_id).order_by(SceneDraft.row_id)).scalars().all()
    assert draft_ids == [
        "draft_neutral_CH_RESUME_SC01_v1",
        "draft_style_cand_CH_RESUME_SC01_v1_0",
    ]
    candidate_steps = session.execute(
        select(LlmCall.execution_step_key).where(
            LlmCall.execution_id == "idempotency:resume-candidates",
            LlmCall.step == "style_draft",
        )
    ).scalars().all()
    assert sorted(candidate_steps) == ["style_draft:0", "style_draft:1"]


def test_best_of_n_releases_undispatched_second_candidate_reservation_then_retries_once(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()
    execution_id = "idempotency:resume-undispatched-candidate"
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def crash_after_second_candidate_reservation(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key != "style_draft:1":
            return
        state = session.get(SceneRunState, "CH_RESUME_SC01")
        state.scene_tokens_reserved += 17
        session.add(
            LlmCall(
                llm_call_id="call-undispatched-style-candidate-1",
                provider="fake",
                model="fake",
                step="style_draft",
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                scope_type="scene",
                scope_id="CH_RESUME_SC01",
                execution_id=execution_id,
                execution_step_key="style_draft:1",
                estimated_tokens=17,
                reserved_tokens=17,
                budget_charged_tokens=0,
                accounting_status="reserved",
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id="attempt-undispatched-style-candidate-1",
                llm_call_id="call-undispatched-style-candidate-1",
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=10,
                estimated_tokens=17,
                reserved_tokens=17,
                budget_charged_tokens=0,
                accounting_status="reserved",
            )
        )
        session.commit()
        raise RuntimeError("crash after candidate reservation")

    first._reconcile_execution_step = crash_after_second_candidate_reservation
    with pytest.raises(RuntimeError, match="crash after candidate reservation"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert len(generation_client.requests) == 2
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    released = session.get(LlmCall, "call-undispatched-style-candidate-1")
    assert released.accounting_status == "released"
    assert state.scene_tokens_reserved == 0
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert len(generation_client.requests) == 3
    assert set(state.run_checkpoint_json["artifact_refs"]["candidate_row_ids"]) == {
        "draft_style_cand_CH_RESUME_SC01_v1_1",
        "draft_style_cand_CH_RESUME_SC01_v1_0",
    }


def test_missing_settled_style_output_blocks_before_any_new_provider_call(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:missing-output")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    style_row = session.get(SceneDraft, state.run_checkpoint_json["artifact_refs"]["style_draft_row_id"])
    assert style_row is not None
    session.delete(style_row)
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))
    before_tokens = state.scene_tokens_used
    before_provider = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:missing-output")

    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    session.refresh(state)
    assert len(generation_client.requests) == before_provider
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls
    assert state.scene_tokens_used == before_tokens


def test_successful_run_commits_terminal_macro_checkpoint(session) -> None:
    _seed_resume_scene(session)
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    )
    result = orchestrator.run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:terminal-checkpoint",
    )

    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert state.run_checkpoint_json["artifact_refs"]["final_scene_row_id"] == state.current_final_scene_row_id

    with pytest.raises(DomainError) as exc_info:
        Orchestrator(session).run_scene(
            "CH_RESUME_SC01",
            author_note="换掉已经归档运行的作者指令",
            execution_id="idempotency:terminal-checkpoint",
        )
    assert exc_info.value.code == "RUN_INPUT_MISMATCH"
