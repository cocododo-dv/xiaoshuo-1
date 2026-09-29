"""Scene-run checkpoint resume · soft QC / soft patch sub-checkpoints."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    QcReport,
    SceneRunState,
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
    _SequencedSoftQc,
    _FailNearFinal,
    _seed_resume_scene,
)


def test_soft_qc_checkpoint_resume_does_not_repeat_qc_or_generation(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
            orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "soft_qc_ready"
    assert soft_qc.calls == 1
    assert near_final.calls == 2
    assert len(generation_client.requests) == 2


def test_soft_qc0_checkpoint_resumes_at_patch_without_replaying_qc0(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

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

    def stop_before_patch(execution_step_key: str) -> None:
        if execution_step_key == "soft_patch:soft_qc:0":
            raise RuntimeError("stop after soft QC0 checkpoint")
        original_reconcile(execution_step_key)

    first._reconcile_execution_step = stop_before_patch
    with pytest.raises(RuntimeError, match="stop after soft QC0 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc0-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    assert state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"] == "qc_CH_RESUME_SC01_soft_qc_0"
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc0-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["soft_final_qc_round"] == 1
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert state.scene_tokens_used > tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "soft_qc:0",
            )
        ).scalars().all()
    ) == 1
    assert len(
        [
            attempt
            for attempt in session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == "CH_RESUME_SC01",
                    AttemptTracker.step == "soft_qc",
                )
            ).scalars().all()
            if (attempt.details_json or {}).get("execution_step_key") == "soft_qc:0"
        ]
    ) == 1


def test_soft_patch_checkpoint_resumes_at_qc1_without_replaying_patch(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

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

    def stop_before_qc1(execution_step_key: str) -> None:
        if execution_step_key == "soft_qc:1":
            raise RuntimeError("stop after soft patch checkpoint")
        original_reconcile(execution_step_key)

    first._reconcile_execution_step = stop_before_qc1
    with pytest.raises(RuntimeError, match="stop after soft patch checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-patch-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 2
    patch_row_id = state.run_checkpoint_json["artifact_refs"]["soft_patch_draft_row_id"]
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-patch-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["soft_final_draft_row_id"] == patch_row_id
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls
    assert state.scene_tokens_used == tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "soft_patch:soft_qc:0",
            )
        ).scalars().all()
    ) == 1
    assert len(
        [
            attempt
            for attempt in session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == "CH_RESUME_SC01",
                    AttemptTracker.step == "soft_patch",
                )
            ).scalars().all()
            if (attempt.details_json or {}).get("row_id") == patch_row_id
        ]
    ) == 1


def test_soft_qc_without_patch_advances_directly_to_complete_subcursor(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "continue"})
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
            orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-no-patch")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_completion_skip_reason"] == "no_patch_requested"
    assert refs["soft_final_draft_row_id"] == refs["soft_input_draft_row_id"]
    assert soft_qc.calls == ["soft_qc:0"]
    assert len(generation_client.requests) == 2


def test_soft_qc_budget_skip_persists_branch_without_patch_call(session, monkeypatch) -> None:
    from novel_system.services import scene_budget

    _seed_resume_scene(session)
    monkeypatch.setattr(scene_budget, "can_spend", lambda *args, **kwargs: False)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "patch"})
    near_final = _FailNearFinal()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=near_final,
    )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-budget-skip")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_qc0_control"] == {
        "patch_allowed": False,
        "skip_reason": "budget_or_candidate_cap",
    }
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_qc_branch"] == "patch"
    assert refs.get("soft_patch_draft_row_id") is None
    assert len(generation_client.requests) == 2


def test_soft_qc_human_review_branch_is_complete_and_resume_safe(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "human_review_required"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
        )

    first = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:soft-human-review",
    )
    provider_calls = len(generation_client.requests)
    second = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:soft-human-review",
    )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert first["scene_status"] == second["scene_status"] == "human_review_required"
    assert first["soft_qc"]["branch"] == second["soft_qc"]["branch"] == "human_review_required"
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_completion_skip_reason"] == "human_review_required"
    assert soft_qc.calls == ["soft_qc:0"]
    assert len(generation_client.requests) == provider_calls


def test_complete_soft_prefix_missing_qc0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-missing")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    session.delete(session.get(QcReport, report_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(soft_qc.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-missing")

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert soft_qc.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_soft_prefix_tampered_qc0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-tamper")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    report = session.get(QcReport, report_id)
    report.rewrite_brief_json = [{"instruction": "tampered QC0"}]
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(soft_qc.calls)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-tamper")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == calls
    assert len(generation_client.requests) == provider_calls
