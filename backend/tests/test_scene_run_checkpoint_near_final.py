"""Scene-run checkpoint resume · near-final eval / rewrite sub-checkpoints."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    LlmCall,
    QcReport,
    RevisionCandidate,
    SceneDraft,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService

# Importing the autouse fixture runs every test here against the accounted online fake provider.
from tests.support.checkpoint_fakes import _accounted_online_default_orchestrator_runner  # noqa: F401
from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _HardPassClient,
    _PassSoftQc,
    _PassNearFinal,
    _SequencedNearFinal,
    _FailArchiveOnce,
    _seed_resume_scene,
)


def test_near_final_checkpoint_resume_archives_without_repeating_prior_nodes(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-final-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "near_final_ready"
    final_row_id = state.run_checkpoint_json["artifact_refs"]["final_scene_row_id"]
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:near-final-resume",
    )

    assert result["scene_status"] == "archived"
    session.refresh(state)
    assert state.run_checkpoint == "archived"
    assert state.current_final_scene_row_id == final_row_id
    assert soft_qc.calls == 1
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_near_eval0_checkpoint_resumes_at_rewrite_without_replaying_eval0(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_rewrite(step_key: str) -> None:
        if step_key == "near_final_rewrite:0":
            raise RuntimeError("stop after near eval0 checkpoint")
        original_reconcile(step_key)

    first._reconcile_execution_step = stop_before_rewrite
    with pytest.raises(RuntimeError, match="stop after near eval0 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval0-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.run_checkpoint_json["artifact_refs"]["near_eval0_evaluation_id"]
    provider_calls = len(generation_client.requests)

    resumed = orchestrator()
    resumed.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval0-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert near_final.calls == ["near_final_acceptance:0", "near_final_acceptance:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "near_final_acceptance:0",
            )
        ).scalars().all()
    ) == 1


def test_near_rewrite_checkpoint_resumes_at_eval1_without_replaying_rewrite(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_eval1(step_key: str) -> None:
        if step_key == "near_final_acceptance:1":
            raise RuntimeError("stop after near rewrite checkpoint")
        original_reconcile(step_key)

    first._reconcile_execution_step = stop_before_eval1
    with pytest.raises(RuntimeError, match="stop after near rewrite checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-rewrite-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    rewrite_row_id = state.run_checkpoint_json["artifact_refs"]["near_rewrite_draft_row_id"]
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    resumed = orchestrator()
    resumed.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-rewrite-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["near_final_source_draft_row_id"] == rewrite_row_id
    assert near_final.calls == ["near_final_acceptance:0", "near_final_acceptance:1"]
    assert len(generation_client.requests) == provider_calls
    assert state.scene_tokens_used == tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "near_final_rewrite:0",
            )
        ).scalars().all()
    ) == 1


def test_near_eval1_checkpoint_resumes_at_finalization_without_replaying_eval1(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()

    def stop_after_eval1(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("stop after near eval1 checkpoint")

    first._near_final_warning_findings = stop_after_eval1
    with pytest.raises(RuntimeError, match="stop after near eval1 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval1-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 2
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    resumed = orchestrator()
    resumed.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval1-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_near_final_budget_skip_completes_without_rewrite_or_eval_replay(session, monkeypatch) -> None:
    from novel_system.services import scene_budget

    _seed_resume_scene(session)
    monkeypatch.setattr(scene_budget, "can_spend", lambda *args, **kwargs: False)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(session, {"near_final_acceptance:0": "rewrite"})
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_PassSoftQc(session),
        near_final_service=near_final,
    )
    orchestrator.archiver = _FailArchiveOnce()

    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-budget-skip")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["near_final_rewrite_count"] == 0
    assert refs["near_final_skip_reason"] == "budget_or_candidate_cap"
    assert refs.get("near_rewrite_draft_row_id") is None
    assert near_final.calls == ["near_final_acceptance:0"]
    assert len(generation_client.requests) == 2


def test_near_final_human_review_proposal_archives_without_eval_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(session, {"near_final_acceptance:0": "human"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-human-proposal")
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:near-human-proposal",
    )

    assert result["scene_status"] == "archived"
    assert result["near_final"]["requires_human_review"] is True
    assert near_final.calls == ["near_final_acceptance:0"]
    assert len(generation_client.requests) == provider_calls


def test_strict_near_final_warning_resume_does_not_replay_eval(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(session, {"near_final_acceptance:0": "human"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator().run_scene(
        "CH_RESUME_SC01",
        run_policy="strict",
        execution_id="idempotency:near-strict-warning",
    )
    provider_calls = len(generation_client.requests)
    second = orchestrator().run_scene(
        "CH_RESUME_SC01",
        run_policy="strict",
        execution_id="idempotency:near-strict-warning",
    )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert first["scene_status"] == second["scene_status"] == "quality_warning_pending_acceptance"
    assert state.run_execution_status == "completed"
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.current_final_scene_row_id is None
    assert near_final.calls == ["near_final_acceptance:0"]
    assert len(generation_client.requests) == provider_calls


def _completed_near_rewrite_checkpoint(session, execution_id: str):  # noqa: ANN201
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    return generation_client, near_final, orchestrator


def test_complete_near_prefix_missing_eval0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client, near_final, orchestrator = _completed_near_rewrite_checkpoint(
        session,
        "idempotency:near-prefix-eval-missing",
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    eval0_id = state.run_checkpoint_json["artifact_refs"]["near_eval0_evaluation_id"]
    session.delete(session.get(WriterEvaluation, eval0_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:near-prefix-eval-missing",
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_near_prefix_tampered_rewrite_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client, near_final, orchestrator = _completed_near_rewrite_checkpoint(
        session,
        "idempotency:near-prefix-rewrite-tamper",
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    rewrite_id = state.run_checkpoint_json["artifact_refs"]["near_rewrite_draft_row_id"]
    rewrite = session.get(SceneDraft, rewrite_id)
    rewrite.content += " tampered"
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:near-prefix-rewrite-tamper",
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_near_prefix_missing_candidate_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client, near_final, orchestrator = _completed_near_rewrite_checkpoint(
        session,
        "idempotency:near-prefix-candidate-missing",
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    candidate_id = state.run_checkpoint_json["artifact_refs"]["near_eval0_revision_candidate_id"]
    session.delete(session.get(RevisionCandidate, candidate_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:near-prefix-candidate-missing",
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_near_final_resume_revalidates_complete_soft_prefix(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-soft-prefix")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    soft_qc0_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    session.delete(session.get(QcReport, soft_qc0_id))
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:near-soft-prefix")

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls
