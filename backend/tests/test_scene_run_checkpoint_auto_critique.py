"""Scene-run checkpoint resume · auto-critique sub-checkpoints: patch products, tombstones, tamper, mode snapshots."""

from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.llm_task_runner import LLMNodeExecutionError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService, StyleGenerationResult
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

# Importing the autouse fixture runs every test here against the accounted online fake provider.
from tests.support.checkpoint_fakes import _accounted_online_default_orchestrator_runner  # noqa: F401
from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _FailAutoCritiquePatchClient,
    _UnparseableAutoCritiquePatchClient,
    _HardPassClient,
    _FailAfterStyle,
    _seed_resume_scene,
)


def test_auto_critique_patch_is_durable_soft_input_subcheckpoint(session, monkeypatch) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        lambda *args, **kwargs: replace(
            auto_critique(args[0]),
            outcome="not_invoked",
            execution_id=kwargs["llm_context"].execution_id,
            execution_step_key=kwargs["llm_context"].execution_step_key,
            run_job_id=kwargs["llm_context"].run_job_id,
            reason="feature_disabled",
        ),
    )
    generation_client = _CountingGenerationClient()
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:auto-critique-input")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    input_draft = session.get(SceneDraft, refs["soft_input_draft_row_id"])
    assert state.run_checkpoint_json["sub_index"] == 0
    assert refs["soft_auto_critique_outcome"] == "patched"
    assert refs["soft_input_execution_step_key"] == "soft_patch:auto_critique:0"
    assert refs["soft_input_source_draft_row_id"] != refs["soft_input_draft_row_id"]
    assert input_draft.stage == "style_patch"
    assert len(generation_client.requests) == 3


def test_auto_critique_patch_control_plane_failure_is_not_degraded(session, monkeypatch) -> None:
    from novel_system.services.auto_critique import CritiqueResult

    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        lambda *args, **kwargs: CritiqueResult(
            should_rewrite=True,
            directives=["tighten the opening"],
            dimension_scores={"syntax_monotony": 0.1},
            flagged_dimensions=["syntax_monotony"],
            outcome="not_invoked",
            execution_id=kwargs["llm_context"].execution_id,
            execution_step_key=kwargs["llm_context"].execution_step_key,
            run_job_id=kwargs["llm_context"].run_job_id,
            reason="feature_disabled",
        ),
    )
    error = LLMNodeExecutionError(
        llm_call_id="llmcall_patch_conflict",
        error_code="LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
        message="conflict",
        request_summary={},
        response_summary={},
    )
    generation_client = _CountingGenerationClient()
    generation_service = SceneGenerationService(session, llm_client=generation_client)
    monkeypatch.setattr(
        generation_service,
        "generate_style_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(LLMNodeExecutionError) as raised:
        Orchestrator(
            session,
            scene_generation_service=generation_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:auto-critique-patch-conflict",
        )
    assert raised.value is error


@pytest.mark.parametrize(
    ("client_factory", "expected_outcome", "expected_error"),
    [
        (_FailAutoCritiquePatchClient, "provider_failed", "ValueError"),
        (
            _UnparseableAutoCritiquePatchClient,
            "parse_failed",
            "SCENE_GENERATION_RESPONSE_INVALID",
        ),
    ],
)
def test_auto_critique_patch_failure_product_is_durable_and_recoverable(
    session,
    monkeypatch,
    client_factory,
    expected_outcome: str,
    expected_error: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-failure:{expected_outcome}"
    critique_calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(
            session,
            critique_calls,
            should_rewrite=True,
        ),
    )
    generation_client = client_factory()

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    product = refs["soft_auto_critique_patch_failure"]
    assert refs["soft_auto_critique_outcome"] == "patch_failed"
    assert product["outcome"] == expected_outcome
    assert product["execution_id"] == execution_id
    assert product["execution_step_key"] == "soft_patch:auto_critique:0"
    assert product["error_code"] == expected_error

    _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )


def test_auto_critique_patch_generic_error_without_parent_is_not_degraded(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    critique_calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(
            session,
            critique_calls,
            should_rewrite=True,
        ),
    )
    generation_client = _CountingGenerationClient()
    generation_service = SceneGenerationService(session, llm_client=generation_client)
    monkeypatch.setattr(
        generation_service,
        "generate_style_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("untracked patch failure")
        ),
    )

    with pytest.raises(RuntimeError, match="untracked patch failure"):
        Orchestrator(
            session,
            scene_generation_service=generation_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:auto-patch-untracked",
        )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint != "soft_qc_ready"


def test_auto_critique_patch_failure_child_tamper_blocks_without_replay(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-failure-child-tamper"
    critique_calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(
            session,
            critique_calls,
            should_rewrite=True,
        ),
    )
    generation_client = _FailAutoCritiquePatchClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    product = refs["soft_auto_critique_patch_failure"]
    child = session.scalar(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == product["llm_call_id"]
        )
    )
    session.delete(child)
    session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_patch_parse_failure_parent_owner_tamper_blocks(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-parse-owner-tamper"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_UnparseableAutoCritiquePatchClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    failure = refs["soft_auto_critique_patch_failure"]
    parent = session.get(LlmCall, failure["llm_call_id"])
    parent.scope_id = "SC_DETACHED"
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize("gate_open", [False, True])
def test_auto_critique_patch_rejected_tombstone_recovers_before_gate_without_replay(
    session,
    monkeypatch,
    gate_open: bool,
) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    class _CrashAfterRejectedPatch(BaseException):
        pass

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-rejected-recover:{gate_open}:online"

    def rule_no_call(content, *_args, **kwargs):
        context = kwargs["llm_context"]
        return replace(
            auto_critique(content),
            outcome="not_invoked",
            reason="feature_disabled",
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
        )

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        rule_no_call,
    )
    generation_client = _CountingGenerationClient()
    first_service = SceneGenerationService(session, llm_client=generation_client)

    def reject_then_crash(scene_id, *_args, **_kwargs):
        scene = session.get(SceneCard, scene_id)
        chapter = session.get(ChapterGoal, scene.chapter_id)
        session.add(
            LlmCall(
                llm_call_id="llmcall_auto_patch_rejected_before_sub0",
                provider="fake",
                model="fake",
                node_id="style_patch",
                step="soft_patch",
                project_id=scene.project_id or chapter.project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene_id,
                scope_type="scene",
                scope_id=scene_id,
                execution_id=execution_id,
                execution_step_key="soft_patch:auto_critique:0",
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                request_dispatched_at=None,
                settled_at="2026-07-14T00:00:01Z",
                error_code="LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
            )
        )
        session.commit()
        raise _CrashAfterRejectedPatch()

    monkeypatch.setattr(first_service, "generate_style_patch", reject_then_crash)
    with pytest.raises(_CrashAfterRejectedPatch):
        Orchestrator(
            session,
            scene_generation_service=first_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    if not gate_open:
        state.scene_tokens_used = state.scene_token_budget
        session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before = _total_llm_budget_charged(session)

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    failure = refs["soft_auto_critique_patch_failure"]
    assert refs["soft_auto_critique_outcome"] == "patch_failed"
    assert failure["outcome"] == "rejected_before_dispatch"
    assert failure["llm_call_id"] == "llmcall_auto_patch_rejected_before_sub0"
    assert failure["provider_execution_mode"] == "online"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before


@pytest.mark.parametrize("gate_open", [False, True])
def test_auto_critique_patch_reserved_crash_reconciles_before_gate(
    session,
    monkeypatch,
    gate_open: bool,
) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    class _CrashAfterPatchReservation(BaseException):
        pass

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-reserved-recover:{gate_open}"

    def rule_no_call(content, *_args, **kwargs):
        context = kwargs["llm_context"]
        return replace(
            auto_critique(content),
            outcome="not_invoked",
            reason="feature_disabled",
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
        )

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        rule_no_call,
    )
    generation_client = _CountingGenerationClient()
    first_service = SceneGenerationService(session, llm_client=generation_client)

    def reserve_then_crash(scene_id, *_args, **_kwargs):
        scene = session.get(SceneCard, scene_id)
        chapter = session.get(ChapterGoal, scene.chapter_id)
        call_id = "llmcall_auto_patch_reserved_before_sub0"
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id="style_patch",
                step="soft_patch",
                project_id=scene.project_id or chapter.project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene_id,
                scope_type="scene",
                scope_id=scene_id,
                execution_id=execution_id,
                execution_step_key="soft_patch:auto_critique:0",
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                usage_is_estimate=True,
                accounting_status="reserved",
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_auto_patch_reserved_before_sub0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=10,
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                usage_is_estimate=True,
                accounting_status="reserved",
                request_dispatched_at=None,
            )
        )
        state = session.get(SceneRunState, scene_id)
        state.scene_tokens_reserved = 20
        session.commit()
        raise _CrashAfterPatchReservation()

    monkeypatch.setattr(first_service, "generate_style_patch", reserve_then_crash)
    with pytest.raises(_CrashAfterPatchReservation):
        Orchestrator(
            session,
            scene_generation_service=first_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    if not gate_open:
        state.scene_tokens_used = state.scene_token_budget
        session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))

    retry = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session,
            llm_client=generation_client,
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    if gate_open:
        with pytest.raises(RuntimeError, match="fail after style checkpoint"):
            retry.run_scene("CH_RESUME_SC01", execution_id=execution_id)
        assert len(generation_client.requests) == provider_calls + 1
        assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count + 1
    else:
        from novel_system.services.scene_criticality import classify_scene

        direct = _activate_checkpoint_orchestrator(session, execution_id)
        scene = session.get(SceneCard, "CH_RESUME_SC01")
        draft = session.get(SceneDraft, state.current_style_draft_row_id)
        draft_parent = session.get(LlmCall, draft.generation_llm_call_id)
        selected = StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=draft.generation_llm_call_id,
            bundle_id=draft.source_bundle_id,
            bundle_hash=draft.source_bundle_hash,
            execution_step_key=draft_parent.execution_step_key,
            artifact_execution_id=draft_parent.execution_id,
        )
        with pytest.raises(DomainError) as blocked:
            direct._ensure_soft_qc_subcheckpoints(
                scene=scene,
                contract=direct.execution_contract_service.get_or_create(
                    scene.scene_id,
                    actor_ref="orchestrator",
                ),
                bundle=direct._load_checkpoint_bundle(scene.scene_id),
                criticality=classify_scene(scene),
                selected_style_generation=selected,
                optional_spend_allowed=lambda: False,
            )
        assert blocked.value.code == "RUN_CHECKPOINT_CORRUPT"
        assert len(generation_client.requests) == provider_calls
        assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    session.refresh(state)
    released = session.get(LlmCall, "llmcall_auto_patch_reserved_before_sub0")
    assert released.accounting_status == "released"
    assert state.scene_tokens_reserved == 0


def _completed_auto_critique_stub(
    session,
    calls: list[str],
    *,
    should_rewrite: bool = False,
):
    from novel_system.services.auto_critique import (
        CritiqueResult,
        auto_critique,
        critique_llm_contribution_hash,
    )

    def run(*_args, **kwargs):
        context = kwargs["llm_context"]
        rule = auto_critique(str(_args[0]))
        calls.append(context.execution_step_key)
        call_id = "llmcall_auto_checkpoint"
        contribution = {
            "should_rewrite": should_rewrite,
            "issues": (
                [
                    {
                        "dimension": "pacing",
                        "directive": "tighten the opening",
                        "evidence": "delayed turn",
                    }
                ]
                if should_rewrite
                else []
            ),
        }
        if session.get(LlmCall, call_id) is None:
            session.add(
                LlmCall(
                    llm_call_id=call_id,
                    provider="fake",
                    model="fake",
                    node_id=context.node_id,
                    step=context.step,
                    project_id=context.project_id,
                    chapter_id=context.chapter_id,
                    scene_id=context.scene_id,
                    scope_type=context.scope_type,
                    scope_id=context.scope_id,
                    run_job_id=context.run_job_id,
                    execution_id=context.execution_id,
                    execution_step_key=context.execution_step_key,
                    request_payload_summary={
                        "_accounting_provider_execution_mode": "online"
                    },
                    response_payload_summary={
                        "auto_critique_parsed_llm_hash":
                        critique_llm_contribution_hash(contribution)
                    },
                    prompt_tokens=17,
                    completion_tokens=0,
                    total_tokens=17,
                    estimated_tokens=17,
                    reserved_tokens=17,
                    budget_charged_tokens=17,
                    latency_ms=3,
                    usage_is_estimate=True,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-14T00:00:00Z",
                    settled_at="2026-07-14T00:00:01Z",
                )
            )
            session.add(
                LlmCallAttempt(
                    attempt_id="attempt_auto_checkpoint_0",
                    llm_call_id=call_id,
                    provider_attempt_no=0,
                    dispatch_kind="initial",
                    request_max_output_tokens=0,
                    prompt_tokens=17,
                    completion_tokens=0,
                    total_tokens=17,
                    estimated_tokens=17,
                    reserved_tokens=17,
                    budget_charged_tokens=17,
                    latency_ms=3,
                    usage_is_estimate=True,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-14T00:00:00Z",
                    settled_at="2026-07-14T00:00:01Z",
                )
            )
            session.commit()
        return CritiqueResult(
            should_rewrite=rule.should_rewrite or should_rewrite,
            directives=list(rule.directives)
            + (
                ["[LLM路pacing] tighten the opening (evidence: delayed turn)"]
                if should_rewrite
                else []
            ),
            dimension_scores=dict(rule.dimension_scores),
            flagged_dimensions=list(rule.flagged_dimensions)
            + (["pacing"] if should_rewrite and "pacing" not in rule.flagged_dimensions else []),
            outcome="completed",
            rule_should_rewrite=rule.should_rewrite,
            rule_directives=list(rule.directives),
            rule_dimension_scores=dict(rule.dimension_scores),
            rule_flagged_dimensions=list(rule.flagged_dimensions),
            llm_contribution=contribution,
            llm_call_id=call_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
        )

    return run


def _run_to_completed_auto_critique_sub0(session, monkeypatch, *, execution_id: str):
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls),
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    return generation_client, calls


def _run_to_patched_auto_critique_sub0(session, monkeypatch, *, execution_id: str):
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.run_checkpoint_json["artifact_refs"]["soft_auto_critique_outcome"] == "patched"
    return generation_client, calls


def _activate_checkpoint_orchestrator(session, execution_id: str) -> Orchestrator:
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session,
            llm_client=_CountingGenerationClient(),
        ),
    )
    orchestrator._execution_id = execution_id
    orchestrator._run_job_id = None
    orchestrator._checkpoint_service = SceneRunCheckpointService(session)
    return orchestrator


def _total_llm_budget_charged(session) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(LlmCall.budget_charged_tokens), 0))
        )
        or 0
    )


def test_completed_auto_critique_sub0_resumes_without_replay(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-envelope-resume"
    generation_client, critique_calls = _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    resumed = _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )

    assert critique_calls == ["soft_qc:auto_critique:0"]
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert resumed.row_id == refs["soft_input_draft_row_id"]


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "ordinal",
        "status",
        "tokens",
        "parent_scope",
        "parent_node",
        "parent_chapter",
        "parent_run_job",
    ],
)
def test_auto_critique_patch_generation_child_tamper_blocks_without_replay(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-patch-child:{tamper}"
    generation_client, _critique_calls = _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    parent = session.get(LlmCall, refs["soft_input_llm_call_id"])
    child = session.scalar(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == parent.llm_call_id
        )
    )
    if tamper == "parent_scope":
        parent.scope_id = "SC_DETACHED"
    elif tamper == "parent_node":
        parent.node_id = "soft_qc"
    elif tamper == "parent_chapter":
        parent.chapter_id = "CH_DETACHED"
    elif tamper == "parent_run_job":
        parent.run_job_id = "job-detached"
    elif tamper == "missing":
        session.delete(child)
    elif tamper == "ordinal":
        child.provider_attempt_no = 4
    elif tamper == "status":
        child.accounting_status = "failed"
        child.error_code = "LLM_HTTP_REQUEST_FAILED"
    else:
        child.prompt_tokens += 1
        child.total_tokens += 1
    session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before_resume


@pytest.mark.parametrize(
    "tamper",
    [
        "hash",
        "product_rehash",
        "semantic_rule_rehash",
        "field_matrix",
        "call_id",
        "owner",
        "step",
        "run_job",
        "parent_scope",
        "parent_node",
        "parent_scene",
        "parent_chapter",
        "parent_run_job",
        "child_missing",
        "child_ordinal",
        "child_dispatch",
        "child_status",
        "child_error_code",
        "child_tokens",
    ],
)
def test_auto_critique_sub0_tamper_blocks_before_provider_replay(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-tamper:{tamper}"
    generation_client, critique_calls = _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    product = refs["soft_auto_critique_decision"]
    parent = session.get(LlmCall, product["llm_call_id"])
    attempt = session.get(LlmCallAttempt, "attempt_auto_checkpoint_0")
    if tamper == "hash":
        product["should_rewrite"] = not product["should_rewrite"]
    elif tamper == "product_rehash":
        product["directives"] = ["tampered but locally rehashed"]
    elif tamper == "semantic_rule_rehash":
        product["rule_directives"] = ["tampered deterministic rule"]
    elif tamper == "field_matrix":
        product.pop("rule_directives")
    elif tamper == "call_id":
        product["llm_call_id"] = "llmcall_detached"
    elif tamper == "owner":
        product["execution_id"] = "exec-detached"
    elif tamper == "step":
        product["execution_step_key"] = "soft_qc:auto_critique:9"
    elif tamper == "run_job":
        product["run_job_id"] = "job-detached"
    elif tamper == "parent_scope":
        parent.scope_id = "SC_DETACHED"
    elif tamper == "parent_node":
        parent.node_id = "hard_qc"
    elif tamper == "parent_scene":
        parent.scene_id = "SC_DETACHED"
    elif tamper == "parent_chapter":
        parent.chapter_id = "CH_DETACHED"
    elif tamper == "parent_run_job":
        parent.run_job_id = "job-detached"
    elif tamper == "child_missing":
        session.delete(attempt)
    elif tamper == "child_ordinal":
        attempt.provider_attempt_no = 3
    elif tamper == "child_dispatch":
        attempt.request_dispatched_at = None
    elif tamper == "child_status":
        attempt.accounting_status = "failed"
        attempt.error_code = "LLM_HTTP_REQUEST_FAILED"
    elif tamper == "child_error_code":
        attempt.error_code = "LLM_HTTP_REQUEST_FAILED"
    elif tamper == "child_tokens":
        attempt.prompt_tokens += 1
        attempt.total_tokens += 1
        attempt.estimated_tokens += 1
        attempt.reserved_tokens += 1
        attempt.budget_charged_tokens += 1
    if tamper != "hash":
        payload["artifact_hashes"]["soft_auto_critique_decision"] = Orchestrator._json_hash(product)
    if tamper == "semantic_rule_rehash":
        parent.response_payload_summary = {
            **dict(parent.response_payload_summary or {}),
            "auto_critique_product_hash": Orchestrator._json_hash(product),
        }
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)
    charged_before_resume = _total_llm_budget_charged(session)

    refs = state.run_checkpoint_json["artifact_refs"]
    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert critique_calls == ["soft_qc:auto_critique:0"]
    assert len(generation_client.requests) == provider_calls
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_patch_semantic_tamper_with_synchronized_hashes_is_rejected(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-semantic-tamper"
    generation_client, _calls = _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    product = refs["soft_auto_critique_decision"]
    product["should_rewrite"] = False
    payload["artifact_hashes"]["soft_auto_critique_decision"] = Orchestrator._json_hash(
        product
    )
    critique_parent = session.get(LlmCall, product["llm_call_id"])
    critique_parent.response_payload_summary = {
        **dict(critique_parent.response_payload_summary or {}),
        "auto_critique_product_hash": Orchestrator._json_hash(product),
    }
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)
    charged_before_resume = _total_llm_budget_charged(session)

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_creation_rejects_invalid_llm_contribution_before_sub0_save(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-invalid-contribution-create"
    calls: list[str] = []
    completed = _completed_auto_critique_stub(
        session,
        calls,
        should_rewrite=True,
    )

    def invalid_contribution(*args, **kwargs):
        result = completed(*args, **kwargs)
        result.llm_contribution["issues"][0]["directive"] = ""
        return result

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        invalid_contribution,
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(DomainError) as corrupt:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert not (
        state.run_checkpoint == "soft_qc_ready"
        and state.run_checkpoint_json.get("sub_index") == 0
    )


@pytest.mark.parametrize(
    "tamper",
    ["parent_scope", "parent_node", "parent_chapter", "parent_run_job"],
)
def test_auto_critique_patch_creation_rejects_detached_generation_parent_before_sub0(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-create-owner:{tamper}"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    generation_client = _CountingGenerationClient()
    service = SceneGenerationService(session, llm_client=generation_client)
    original_patch = service.generate_style_patch

    def detached_parent(*args, **kwargs):
        result = original_patch(*args, **kwargs)
        parent = session.get(LlmCall, result.llm_call_id)
        if tamper == "parent_scope":
            parent.scope_id = "SC_DETACHED"
        elif tamper == "parent_node":
            parent.node_id = "soft_qc"
        elif tamper == "parent_chapter":
            parent.chapter_id = "CH_DETACHED"
        else:
            parent.run_job_id = "job-detached"
        session.flush()
        return result

    monkeypatch.setattr(service, "generate_style_patch", detached_parent)
    with pytest.raises(LLMAccountingError) as invalid:
        Orchestrator(
            session,
            scene_generation_service=service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert invalid.value.code == "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert not (
        state.run_checkpoint == "soft_qc_ready"
        and state.run_checkpoint_json.get("sub_index") == 0
    )


def test_auto_critique_parsed_llm_anchor_blocks_synchronized_product_tamper(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-parsed-anchor"
    _generation_client, _calls = _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    product = payload["artifact_refs"]["soft_auto_critique_decision"]
    product["llm_contribution"]["issues"][0]["directive"] = "tampered directive"
    product["directives"][-1] = (
        "[LLM路pacing] tampered directive (evidence: delayed turn)"
    )
    product_hash = Orchestrator._json_hash(product)
    payload["artifact_hashes"]["soft_auto_critique_decision"] = product_hash
    parent = session.get(LlmCall, product["llm_call_id"])
    parent.response_payload_summary = {
        **dict(parent.response_payload_summary or {}),
        "auto_critique_product_hash": product_hash,
        "auto_critique_llm_merge_hash": "synchronized-obsolete-mirror",
    }
    state.run_checkpoint_json = payload
    session.commit()

    refs = payload["artifact_refs"]
    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_checkpoint_output_rejects_online_parent_forged_as_zero_attempt_offline(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:forged-offline-parent"
    _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    product = refs["soft_auto_critique_decision"]
    parent = session.get(LlmCall, product["llm_call_id"])
    attempt = session.scalar(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == parent.llm_call_id)
    )
    session.delete(attempt)
    parent.provider = "offline_deterministic"
    parent.request_payload_summary = {
        **dict(parent.request_payload_summary or {}),
        "_accounting_provider_execution_mode": "offline_deterministic",
    }
    parent.request_dispatched_at = None
    parent.prompt_tokens = 0
    parent.completion_tokens = 0
    parent.total_tokens = 0
    parent.estimated_tokens = 0
    parent.reserved_tokens = 0
    parent.budget_charged_tokens = 0
    parent.latency_ms = 0
    parent.usage_is_estimate = False
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize(
    "tamper",
    ["snapshot", "marker", "coordinated_zero_attempt_offline"],
)
def test_auto_critique_patch_generation_mode_snapshot_blocks_tamper(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-mode-tamper:{tamper}"
    _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    parent = session.get(LlmCall, refs["soft_input_llm_call_id"])

    if tamper == "snapshot":
        refs["soft_input_provider_execution_mode"] = "offline_deterministic"
        payload["artifact_hashes"]["soft_input_provider_execution_mode"] = (
            Orchestrator._text_hash("offline_deterministic")
        )
        state.run_checkpoint_json = payload
    else:
        parent.request_payload_summary = {
            **dict(parent.request_payload_summary or {}),
            "_accounting_provider_execution_mode": "offline_deterministic",
        }
        if tamper == "coordinated_zero_attempt_offline":
            attempts = session.scalars(
                select(LlmCallAttempt).where(
                    LlmCallAttempt.llm_call_id == parent.llm_call_id
                )
            ).all()
            for attempt in attempts:
                session.delete(attempt)
            parent.provider = "offline_deterministic"
            parent.request_dispatched_at = None
            for field_name in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "estimated_tokens",
                "reserved_tokens",
                "budget_charged_tokens",
                "latency_ms",
            ):
                setattr(parent, field_name, 0)
            parent.error_code = None
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_auto_critique_patch_success_resumes_with_a_fresh_online_runner(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-fresh-online-runner"
    _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_input_provider_execution_mode"] == "online"

    resumed = _activate_checkpoint_orchestrator(session, execution_id)
    generation = resumed._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


def test_auto_critique_unchanged_resumes_with_a_fresh_online_runner(
    session,
    monkeypatch,
) -> None:
    from novel_system.services.auto_critique import CritiqueResult

    _seed_resume_scene(session)
    execution_id = "idempotency:auto-unchanged-mode-switch"
    monkeypatch.setattr(
        "novel_system.services.auto_critique.auto_critique",
        lambda *_args, **_kwargs: CritiqueResult(should_rewrite=False),
    )
    _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_auto_critique_outcome"] == "unchanged"
    assert refs["soft_input_provider_execution_mode"] == "online"

    resumed = _activate_checkpoint_orchestrator(session, execution_id)
    generation = resumed._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


def test_auto_critique_patch_success_resumes_after_online_client_switch(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-online-client-switch"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    first_online_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=first_online_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_auto_critique_outcome"] == "patched"
    assert refs["soft_input_provider_execution_mode"] == "online"
    assert session.get(LlmCall, refs["soft_input_llm_call_id"]).provider == "fake-provider"

    generation = _activate_checkpoint_orchestrator(
        session,
        execution_id,
    )._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


@pytest.mark.parametrize("tamper", ["snapshot", "marker"])
def test_auto_critique_patch_failure_mode_snapshot_blocks_tamper(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-failure-mode-tamper:{tamper}"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_FailAutoCritiquePatchClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    product = refs["soft_auto_critique_patch_failure"]
    parent = session.get(LlmCall, product["llm_call_id"])
    if tamper == "snapshot":
        product["provider_execution_mode"] = "offline_deterministic"
        product_hash = Orchestrator._json_hash(product)
        payload["artifact_hashes"]["soft_auto_critique_patch_failure"] = product_hash
        parent.response_payload_summary = {
            **dict(parent.response_payload_summary or {}),
            "auto_critique_patch_failure_hash": product_hash,
        }
        state.run_checkpoint_json = payload
    else:
        parent.request_payload_summary = {
            **dict(parent.request_payload_summary or {}),
            "_accounting_provider_execution_mode": "offline_deterministic",
        }
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_auto_critique_patch_failure_resumes_after_online_to_offline_config_switch(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-failure-mode-switch"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_FailAutoCritiquePatchClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    resumed = _activate_checkpoint_orchestrator(session, execution_id)
    monkeypatch.setattr(
        resumed.scene_generation_service._llm_runner,
        "_provider_execution_mode",
        lambda: "offline_deterministic",
    )
    generation = resumed._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


def test_auto_critique_provider_success_without_sub0_blocks_resend(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-output-missing"
    calls: list[str] = []
    completed = _completed_auto_critique_stub(session, calls)

    def crash_after_provider(*args, **kwargs):
        completed(*args, **kwargs)
        raise RuntimeError("crash before auto critique checkpoint")

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        crash_after_provider,
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="crash before auto critique checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    provider_calls = len(generation_client.requests)
    charged_before_resume = _total_llm_budget_charged(session)

    orchestrator = _activate_checkpoint_orchestrator(session, execution_id)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    state = session.get(SceneRunState, scene.scene_id)
    draft = session.get(SceneDraft, state.current_style_draft_row_id)
    parent = session.get(LlmCall, draft.generation_llm_call_id)
    selected = StyleGenerationResult(
        row_id=draft.row_id,
        content=draft.content,
        llm_call_id=draft.generation_llm_call_id,
        bundle_id=draft.source_bundle_id,
        bundle_hash=draft.source_bundle_hash,
        execution_step_key=parent.execution_step_key,
        artifact_execution_id=parent.execution_id,
    )
    from novel_system.services.scene_criticality import classify_scene

    with pytest.raises(DomainError) as missing:
        orchestrator._ensure_soft_qc_subcheckpoints(
            scene=scene,
            contract=orchestrator.execution_contract_service.get_or_create(
                scene.scene_id,
                actor_ref="orchestrator",
            ),
            bundle=orchestrator._load_checkpoint_bundle(scene.scene_id),
            criticality=classify_scene(scene),
            selected_style_generation=selected,
            optional_spend_allowed=lambda: True,
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert calls == ["soft_qc:auto_critique:0"]
    assert len(generation_client.requests) == provider_calls
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_released_online_tombstone_blocks_a_no_call_resume(
    session,
    monkeypatch,
) -> None:
    """首跑为自动批评预留了一次在线调用、还没派发就崩了（续跑时它成了 released 墓碑）；续跑时自动批评已关
    （没有批评节点），就不能用「没调用」的产品顶替那次调用。离线确定性执行模式退役之前，这里用一个离线替身
    制造「没调用」；现在没有离线模式，关掉批评节点是唯一的「没调用」。"""
    from dataclasses import replace

    from novel_system.services.auto_critique import (
        llm_auto_critique as real_llm_auto_critique,
    )
    from novel_system.services.scene_criticality import classify_scene as real_classify_scene

    class _CrashAfterCritiqueReservation(BaseException):
        pass

    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_criticality.classify_scene",
        lambda *args, **kwargs: replace(
            real_classify_scene(*args, **kwargs),
            skip_critique=False,
        ),
    )
    execution_id = "idempotency:auto-critique-released-online-to-no-call"
    call_id = "llmcall_auto_critique_released_before_sub0"

    def reserve_then_crash(*_args, **kwargs):
        context = kwargs["llm_context"]
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="reserved",
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_auto_critique_released_before_sub0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=10,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="reserved",
                request_dispatched_at=None,
            )
        )
        state = session.get(SceneRunState, context.scene_id)
        state.scene_tokens_reserved = 20
        session.commit()
        raise _CrashAfterCritiqueReservation()

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        reserve_then_crash,
    )
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        )

    with pytest.raises(_CrashAfterCritiqueReservation):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        real_llm_auto_critique,
    )
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_auto_critique_runner",
        lambda _self: None,
    )

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    parent = session.get(LlmCall, call_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert "released accounting tombstone" in corrupt.value.message
    assert parent.accounting_status == "released"
    assert state.scene_tokens_reserved == 0
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before_resume


@pytest.mark.parametrize("gate_open", [False, True])
def test_auto_critique_rejected_parent_rebuilds_product_without_replay(
    session,
    monkeypatch,
    gate_open: bool,
) -> None:
    from novel_system.services.auto_critique import llm_auto_critique as real_llm_auto_critique

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-rejected-recover:{gate_open}"
    call_id = "llmcall_auto_rejected_before_sub0"
    error_code = "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"

    def rejected_then_crash(*_args, **kwargs):
        context = kwargs["llm_context"]
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                request_dispatched_at=None,
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
        session.commit()
        raise RuntimeError("crash after rejected critique before sub0")

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        rejected_then_crash,
    )
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        )

    with pytest.raises(RuntimeError, match="crash after rejected critique before sub0"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        real_llm_auto_critique,
    )
    recovered_runner_calls: list[str] = []

    class _MustNotRunRecoveredCritique:
        def run_task(self, **_kwargs):
            recovered_runner_calls.append("provider")
            raise AssertionError("recovered rejected product must prevent critique resend")

    recovered_runner = _MustNotRunRecoveredCritique() if gate_open else None
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_auto_critique_runner",
        lambda _self: recovered_runner,
    )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    product = state.run_checkpoint_json["artifact_refs"]["soft_auto_critique_decision"]
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert product["outcome"] == "rejected_before_dispatch"
    assert product["llm_call_id"] == call_id
    assert product["reason"] == "pre_dispatch_rejection"
    assert product["error_code"] == error_code

    refs = state.run_checkpoint_json["artifact_refs"]
    _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )

    # The rejected critic is not replayed. Its deterministic rule product may still
    # authorize the first (non-replay) style patch.
    assert len(generation_client.requests) == provider_calls + 1
    assert recovered_runner_calls == []
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count + 1
    assert _total_llm_budget_charged(session) > charged_before_resume


@pytest.mark.parametrize("tamper", ["rule_product", "reason"])
def test_auto_critique_no_call_sub0_has_reason_and_revalidates_rule_product(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-no-call"
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    product = payload["artifact_refs"]["soft_auto_critique_decision"]
    assert product["outcome"] == "not_invoked"
    assert product["reason"]
    assert product["llm_call_id"] is None
    assert product["execution_id"] == execution_id
    assert product["execution_step_key"] == "soft_qc:auto_critique:0"

    if tamper == "rule_product":
        product["directives"] = ["tampered no-call rule result"]
    else:
        product["reason"] = "arbitrary_no_call_reason"
    payload["artifact_hashes"]["soft_auto_critique_decision"] = Orchestrator._json_hash(product)
    state.run_checkpoint_json = payload
    session.commit()
    refs = payload["artifact_refs"]
    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize("outcome", ["rejected_before_dispatch", "provider_failed"])
def test_auto_critique_degraded_sub0_validates_outcome_specific_attempt_ledger(
    session,
    monkeypatch,
    outcome: str,
) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-{outcome}"
    call_id = f"llmcall_auto_{outcome}"
    error_code = (
        "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
        if outcome == "rejected_before_dispatch"
        else "LLM_HTTP_REQUEST_FAILED"
    )

    def degraded(*_args, **kwargs):
        context = kwargs["llm_context"]
        dispatched = outcome == "provider_failed"
        tokens = 19 if dispatched else 0
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=tokens,
                completion_tokens=0,
                total_tokens=tokens,
                estimated_tokens=tokens,
                reserved_tokens=tokens,
                budget_charged_tokens=tokens,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="failed" if dispatched else "rejected",
                request_dispatched_at="2026-07-14T00:00:00Z" if dispatched else None,
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
        if dispatched:
            session.add(
                LlmCallAttempt(
                    attempt_id=f"attempt_auto_{outcome}_0",
                    llm_call_id=call_id,
                    provider_attempt_no=0,
                    dispatch_kind="initial",
                    request_max_output_tokens=0,
                    prompt_tokens=tokens,
                    completion_tokens=0,
                    total_tokens=tokens,
                    estimated_tokens=tokens,
                    reserved_tokens=tokens,
                    budget_charged_tokens=tokens,
                    latency_ms=0,
                    usage_is_estimate=True,
                    accounting_status="failed",
                    request_dispatched_at="2026-07-14T00:00:00Z",
                    settled_at="2026-07-14T00:00:01Z",
                    error_code=error_code,
                )
            )
        session.commit()
        return replace(
            auto_critique(_args[0]),
            outcome=outcome,
            llm_call_id=call_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
            reason=(
                "pre_dispatch_rejection"
                if outcome == "rejected_before_dispatch"
                else "provider_call_failed"
            ),
            error_code=error_code,
        )

    monkeypatch.setattr("novel_system.services.auto_critique.llm_auto_critique", degraded)
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )

    if outcome == "rejected_before_dispatch":
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_illegal_rejected_0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
    else:
        session.delete(session.get(LlmCallAttempt, f"attempt_auto_{outcome}_0"))
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
