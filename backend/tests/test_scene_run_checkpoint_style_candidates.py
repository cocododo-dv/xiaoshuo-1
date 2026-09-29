"""Scene-run checkpoint resume · de-template, candidate and progressive top-up checkpoints."""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    SceneCard,
    SceneDraft,
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
    _FailDeTemplateClient,
    _FailFourthGenerationClient,
    _HardPassClient,
    _FailAfterStyle,
    _PassSoftQc,
    _FailNearFinal,
    _seed_resume_scene,
)


def test_de_template_selected_soft_input_resumes_from_sub0(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:test"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:de-template-soft-input")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    selected = session.get(SceneDraft, refs["soft_input_draft_row_id"])
    assert state.run_checkpoint_json["sub_index"] == 0
    assert selected.stage == "de_template"
    provider_calls = len(generation_client.requests)

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=_FailNearFinal(),
        ).run_scene("CH_RESUME_SC01", execution_id="idempotency:de-template-soft-input")

    assert len(generation_client.requests) == provider_calls


def test_style_base_checkpoint_resumes_only_de_template_after_interruption(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:resume-base"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:style-base-de-template-resume"
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    original_reconcile = first._reconcile_execution_step

    def interrupt_before_de_template(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key == "style_draft:0:de_template":
            raise RuntimeError("interrupt after durable style base")

    first._reconcile_execution_step = interrupt_before_de_template
    with pytest.raises(RuntimeError, match="interrupt after durable style base"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    work_items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint == "hard_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert len(work_items) == 1
    assert work_items[0]["slot_key"] == "initial:0"
    assert work_items[0]["base"]["row_id"] == "draft_style_CH_RESUME_SC01_v1"
    assert work_items[0]["final"] is None
    assert [request.node_id for request in generation_client.requests] == ["neutral_draft", "style_draft"]
    base_call = session.get(LlmCall, work_items[0]["base"]["llm_call_id"])
    base_accounting = (
        base_call.accounting_status,
        base_call.reserved_tokens,
        base_call.budget_charged_tokens,
        base_call.total_tokens,
    )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    work_items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert work_items[0]["gate_decision"]["triggered"] is True
    assert work_items[0]["final"]["stage"] == "de_template"
    assert work_items[0]["final"]["source_base_row_id"] == work_items[0]["base"]["row_id"]
    assert [request.node_id for request in generation_client.requests].count("style_draft") == 1
    assert [request.node_id for request in generation_client.requests].count("style_patch") == 1
    session.refresh(base_call)
    assert (
        base_call.accounting_status,
        base_call.reserved_tokens,
        base_call.budget_charged_tokens,
        base_call.total_tokens,
    ) == base_accounting
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(SceneDraft).where(
            SceneDraft.row_id == work_items[0]["base"]["row_id"]
        )
    ) == 1


def test_failed_de_template_is_a_durable_final_outcome_and_is_not_replayed(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:failed-de-template"],
            "findings": [],
        },
    )
    generation_client = _FailDeTemplateClient()
    execution_id = "idempotency:failed-de-template-final"

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    item = state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]
    outcome = item["de_template_outcome"]
    assert outcome["status"] == "failed"
    assert outcome["execution_step_key"] == "style_draft:0:de_template"
    assert outcome["accounting_status"] == "failed"
    assert item["gate_decision"]["triggered"] is True
    assert item["final"]["row_id"] == item["base"]["row_id"]
    provider_calls = len(generation_client.requests)

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=_FailNearFinal(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "de_template",
            AttemptTracker.status == "failed",
        )
    ) == 1


def test_failed_de_template_recovery_rejects_error_code_detached_from_parent_call(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:failed-de-template-error-code"],
            "findings": [],
        },
    )
    generation_client = _FailDeTemplateClient()
    execution_id = "idempotency:failed-de-template-error-code"
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    item = payload["artifact_refs"]["style_work_items"][0]
    outcome = item["de_template_outcome"]
    parent_call = session.get(LlmCall, outcome["llm_call_id"])
    assert parent_call.error_code == outcome["error_code"]
    tampered_error_code = "TAMPERED_DE_TEMPLATE_ERROR"
    outcome["error_code"] = tampered_error_code
    payload["artifact_hashes"]["style_work_items"] = Orchestrator._json_hash(
        payload["artifact_refs"]["style_work_items"]
    )
    state.run_checkpoint_json = payload
    failed_attempt = next(
        attempt
        for attempt in session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == "CH_RESUME_SC01",
                AttemptTracker.step == "de_template",
                AttemptTracker.status == "failed",
            )
        ).scalars()
        if (attempt.details_json or {}).get("llm_call_id") == parent_call.llm_call_id
    )
    failed_attempt.details_json = {
        **(failed_attempt.details_json or {}),
        "error_code": tampered_error_code,
    }
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=_FailNearFinal(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert exc_info.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls


def test_best_of_n_resumes_candidate_de_template_without_replaying_its_base(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:candidate-resume-base"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:candidate-base-de-template-resume"
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    original_reconcile = first._reconcile_execution_step

    def interrupt_first_candidate_de_template(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key == "style_draft:0:de_template":
            raise RuntimeError("interrupt candidate after base")

    first._reconcile_execution_step = interrupt_first_candidate_de_template
    with pytest.raises(RuntimeError, match="interrupt candidate after base"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint_json["sub_index"] == 0
    assert [(item["slot_key"], item["final"]) for item in items] == [("initial:0", None)]

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert [item["slot_key"] for item in items] == ["initial:0", "initial:1"]
    assert all(item["de_template_outcome"]["status"] == "completed" for item in items)
    assert [request.node_id for request in generation_client.requests].count("style_draft") == 2
    assert [request.node_id for request in generation_client.requests].count("style_patch") == 2


def test_completed_candidate_de_template_survives_next_candidate_failure(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:next-candidate-failure"],
            "findings": [],
        },
    )
    generation_client = _FailFourthGenerationClient()
    execution_id = "idempotency:de-template-then-next-candidate-fails"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        )

    with pytest.raises(ValueError, match="next candidate failed after de-template"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint_json["sub_index"] == 1
    assert len(items) == 1
    assert items[0]["de_template_outcome"]["status"] == "completed"
    completed_row_id = items[0]["final"]["row_id"]
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert len(generation_client.requests) == provider_calls
    session.refresh(state)
    assert state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]["final"]["row_id"] == completed_row_id


def test_progressive_topup_resumes_its_locked_base_without_replay(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.constraint_intensity = 0.5
    session.commit()
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))
    monkeypatch.setattr("novel_system.services.scene_generation._candidate_dispersion", lambda contents: 0.0)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:topup-resume"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:topup-base-de-template-resume"
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    original_reconcile = first._reconcile_execution_step

    def interrupt_topup_de_template(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key == "style_draft:topup:1:de_template":
            raise RuntimeError("interrupt topup after base")

    first._reconcile_execution_step = interrupt_topup_de_template
    with pytest.raises(RuntimeError, match="interrupt topup after base"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint_json["sub_index"] == 4
    assert [item["slot_key"] for item in items] == ["initial:0", "initial:1", "topup:1"]
    assert items[-1]["final"] is None

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert items[-1]["de_template_outcome"]["status"] == "completed"
    assert [request.node_id for request in generation_client.requests].count("style_draft") == 3
    assert [request.node_id for request in generation_client.requests].count("style_patch") == 3


def test_no_anti_template_trigger_persists_base_equals_final(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": False,
            "rewrite_pass": 0,
            "score": 1.0,
            "risk_dimensions": [],
            "quality_signal_ids": [],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id="idempotency:no-de-template-required")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    item = state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]
    assert item["gate_decision"]["triggered"] is False
    assert item["de_template_outcome"] == {"status": "not_required"}
    assert item["base"]["row_id"] == item["final"]["row_id"]
    assert item["base"]["content_hash"] == item["final"]["content_hash"]
    assert item["base"]["llm_call_id"] == item["final"]["llm_call_id"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [("delete", "RUN_CHECKPOINT_OUTPUT_MISSING"), ("tamper", "RUN_CHECKPOINT_CORRUPT")],
)
def test_completed_de_template_recovery_validates_its_base_lineage(
    session,
    monkeypatch,
    mutation: str,
    expected_code: str,
) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:lineage-validation"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = f"idempotency:de-template-lineage-{mutation}"

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    item = state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]
    assert item["base"]["row_id"] != item["final"]["row_id"]
    base = session.get(SceneDraft, item["base"]["row_id"])
    if mutation == "delete":
        session.delete(base)
    else:
        base.content = "tampered durable style base"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert exc_info.value.code == expected_code
    assert len(generation_client.requests) == provider_calls
