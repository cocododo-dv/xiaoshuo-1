"""硬 / 软质检调用失败时能不能降级（``qc_engine/degradation.py``）：只有记账台账证明「请求根本没派发」或「供应商真的失败了」
才降级成带警告继续；台账对不上、被篡改、控制面错误、租约续约失败一律原样抛出，且不留副作用。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    QcReport,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMRequest, LLMResponse, OnlineAccountedExecution
from novel_system.services.llm_task_runner import (
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from tests.support.qc import (
    FakeQcClient,
    FakeSoftQcClient,
    qc_payload as _base_qc_payload,
    seed_qc_scene as _seed_scene,
    soft_qc_payload as _base_soft_qc_payload,
)


class FakeAccountedQcRuntimeFailureClient(OnlineAccountedExecution):
    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:  # noqa: ANN001
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        error = RuntimeError("qc transport timed out before a response was returned")
        accounting_hook.after_error(
            handle,
            request=request,
            error=error,
            raw_response=None,
            provider_request_id=None,
            latency_ms=7,
        )
        raise error


class _RaisingQcRunner:
    def __init__(self, error: LLMNodeExecutionError) -> None:
        self.error = error

    def run(self, **_kwargs):  # noqa: ANN003, ANN201
        raise self.error


def _seed_qc_ledger_case(session, *, qc_type: str) -> tuple[dict, str, str]:
    _seed_scene(session)
    draft_row_id = f"draft_{qc_type}_CH100_SC01"
    session.add(
        SceneDraft(
            row_id=draft_row_id,
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft" if qc_type == "hard_qc" else "style_draft",
            content="Draft under ledger review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = "exec-qc-ledger"
    state.run_execution_status = "active"
    state.run_checkpoint = "neutral_ready" if qc_type == "hard_qc" else "style_ready"
    state.run_checkpoint_json = {
        "execution_id": "exec-qc-ledger",
        "node_key": state.run_checkpoint,
        "artifact_refs": {},
        "artifact_hashes": {},
    }
    session.commit()
    return (
        {
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {
                "scene_id": "CH100_SC01",
                "chapter_id": "CH100",
                "inline_digests": {"scene_card": "Goal"},
            },
        },
        draft_row_id,
        f"{qc_type}:0",
    )


def _qc_execution_error(*, call_id: str, error_code: str) -> LLMNodeExecutionError:
    return LLMNodeExecutionError(
        llm_call_id=call_id,
        error_code=error_code,
        message=f"{error_code} from injected QC runner",
        request_summary={},
        response_summary={"error_code": error_code},
    )


def _qc_continuity_error(*, call_id: str) -> LLMNodeContinuityError:
    warning = {
        "code": "continuity_budget_exceeded",
        "message": "Prompt still exceeds the safe input budget after deterministic continuity compaction.",
        "recommended_action": "split_scene",
        "requires_scene_split": True,
    }
    return LLMNodeContinuityError(
        llm_call_id=call_id,
        request_summary={},
        response_summary={"error_code": "CONTINUITY_BUDGET_EXCEEDED"},
        continuity_warning=warning,
    )


def _add_qc_failure_parent(
    session,
    *,
    qc_type: str,
    call_id: str,
    error_code: str,
    accounting_status: str = "failed",
    dispatched: bool = True,
    scene_id: str = "CH100_SC01",
    execution_id: str = "exec-qc-ledger",
    execution_step_key: str | None = None,
) -> LlmCall:
    parent = LlmCall(
        llm_call_id=call_id,
        provider="fake-provider",
        model="fake-model",
        node_id=qc_type,
        step=qc_type,
        scene_id=scene_id,
        chapter_id="CH100",
        scope_type="scene",
        scope_id=scene_id,
        execution_id=execution_id,
        execution_step_key=execution_step_key or f"{qc_type}:0",
        estimated_tokens=0,
        reserved_tokens=0,
        budget_charged_tokens=0,
        usage_is_estimate=True,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        latency_ms=0,
        accounting_status=accounting_status,
        error_code=error_code,
        request_dispatched_at="2026-07-14T00:00:00+00:00" if dispatched else None,
        settled_at="2026-07-14T00:00:01+00:00",
    )
    session.add(parent)
    session.commit()
    return parent


def _add_valid_qc_provider_failure_ledger(
    session,
    *,
    qc_type: str,
    call_id: str,
    error_code: str = "LLM_HTTP_REQUEST_FAILED",
) -> tuple[LlmCall, LlmCallAttempt]:
    parent = _add_qc_failure_parent(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code=error_code,
        dispatched=True,
    )
    child = LlmCallAttempt(
        attempt_id=f"attempt-{call_id}",
        llm_call_id=call_id,
        provider_attempt_no=0,
        dispatch_kind="initial",
        request_max_output_tokens=32,
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_tokens=24,
        reserved_tokens=24,
        budget_charged_tokens=20,
        accounting_status="failed",
        request_dispatched_at="2026-07-14T00:00:00+00:00",
        settled_at="2026-07-14T00:00:01+00:00",
        latency_ms=7,
        error_code=error_code,
        error_text="provider failed",
    )
    session.add(child)
    parent.estimated_tokens = child.estimated_tokens
    parent.reserved_tokens = child.reserved_tokens
    parent.budget_charged_tokens = child.budget_charged_tokens
    parent.prompt_tokens = child.prompt_tokens
    parent.completion_tokens = child.completion_tokens
    parent.total_tokens = child.total_tokens
    parent.latency_ms = child.latency_ms
    session.commit()
    return parent, child


def _evaluate_raising_qc(
    session,
    *,
    qc_type: str,
    error: LLMNodeExecutionError,
    bundle: dict,
    draft_row_id: str,
    execution_step_key: str,
):
    engine_cls = HardQcEngine if qc_type == "hard_qc" else SoftQcEngine
    engine = engine_cls(session, llm_runner=_RaisingQcRunner(error))
    if qc_type == "hard_qc":
        return engine.evaluate(
            scene_id="CH100_SC01",
            bundle=bundle,
            neutral_draft_row_id=draft_row_id,
            neutral_content="Draft under ledger review.",
            execution_step_key=execution_step_key,
        )
    return engine.evaluate(
        scene_id="CH100_SC01",
        bundle=bundle,
        source_draft_row_id=draft_row_id,
        source_draft_content="Draft under ledger review.",
        execution_step_key=execution_step_key,
    )


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
def test_qc_continuity_budget_rejection_degrades_only_from_exact_undispatched_ledger(
    session,
    qc_type: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-continuity"
    _add_qc_failure_parent(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code="CONTINUITY_BUDGET_EXCEEDED",
        accounting_status="rejected",
        dispatched=False,
    )
    error = _qc_continuity_error(call_id=call_id)

    decision = _evaluate_raising_qc(
        session,
        qc_type=qc_type,
        error=error,
        bundle=bundle,
        draft_row_id=draft_row_id,
        execution_step_key=step_key,
    )

    assert decision.branch == ("continue" if qc_type == "hard_qc" else "waive")
    assert decision.should_continue is True
    assert decision.stop_reason == f"{qc_type}_continuity_budget_exceeded"
    assert decision.llm_call_id == call_id
    report = session.execute(select(QcReport)).scalar_one()
    issue = report.issues_json[0]
    assert issue["issue_key"] == "continuity_budget_exceeded"
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["continuity_warning"] == error.continuity_warning
    assert session.execute(select(LlmCallAttempt)).scalars().all() == []


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize(
    "tamper",
    [
        "missing_parent",
        "scene",
        "chapter",
        "execution",
        "step",
        "node",
        "execution_step_key",
        "error_code",
        "status",
        "parent_dispatched",
        "parent_settled_missing",
        "estimated_nonzero",
        "reserved_nonzero",
        "charged_nonzero",
        "prompt_nonzero",
        "completion_nonzero",
        "total_nonzero",
        "latency_nonzero",
        "usage_false",
        "child_undispatched",
        "child_dispatched",
    ],
)
def test_qc_continuity_budget_ledger_tampering_rethrows_without_side_effects(
    session,
    qc_type: str,
    tamper: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-continuity-{tamper}"
    if tamper != "missing_parent":
        parent = _add_qc_failure_parent(
            session,
            qc_type=qc_type,
            call_id=call_id,
            error_code="CONTINUITY_BUDGET_EXCEEDED",
            accounting_status="rejected",
            dispatched=False,
        )
        if tamper == "scene":
            parent.scene_id = "OTHER_SCENE"
        elif tamper == "chapter":
            parent.chapter_id = "OTHER_CHAPTER"
        elif tamper == "execution":
            parent.execution_id = "other-execution"
        elif tamper == "step":
            parent.step = "other-step"
        elif tamper == "node":
            parent.node_id = "other-node"
        elif tamper == "execution_step_key":
            parent.execution_step_key = "other-step-key"
        elif tamper == "error_code":
            parent.error_code = "LLM_HTTP_REQUEST_FAILED"
        elif tamper == "status":
            parent.accounting_status = "failed"
        elif tamper == "parent_dispatched":
            parent.request_dispatched_at = "2026-07-14T00:00:00+00:00"
        elif tamper == "parent_settled_missing":
            parent.settled_at = None
        elif tamper == "estimated_nonzero":
            parent.estimated_tokens = 1
        elif tamper == "reserved_nonzero":
            parent.reserved_tokens = 1
        elif tamper == "charged_nonzero":
            parent.reserved_tokens = 1
            parent.budget_charged_tokens = 1
        elif tamper == "prompt_nonzero":
            parent.prompt_tokens = 1
        elif tamper == "completion_nonzero":
            parent.completion_tokens = 1
        elif tamper == "total_nonzero":
            parent.total_tokens = 1
        elif tamper == "latency_nonzero":
            parent.latency_ms = 1
        elif tamper == "usage_false":
            parent.usage_is_estimate = False
        elif tamper in {"child_undispatched", "child_dispatched"}:
            session.add(
                LlmCallAttempt(
                    attempt_id=f"attempt-{call_id}",
                    llm_call_id=call_id,
                    provider_attempt_no=0,
                    dispatch_kind="initial",
                    request_max_output_tokens=0,
                    accounting_status="rejected",
                    request_dispatched_at=(
                        "2026-07-14T00:00:00+00:00" if tamper == "child_dispatched" else None
                    ),
                    settled_at="2026-07-14T00:00:01+00:00",
                    error_code="CONTINUITY_BUDGET_EXCEEDED",
                )
            )
        session.commit()
    error = _qc_continuity_error(call_id=call_id)

    with pytest.raises(LLMNodeContinuityError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []
    assert session.get(SceneRunState, "CH100_SC01").current_qc_report_id is None


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize(
    ("error_code", "ledger_mode"),
    [
        ("RUN_OWNER_LEASE_LOST", "none"),
        ("RUN_CHECKPOINT_OUTPUT_MISSING", "dispatched"),
        ("LLM_ACCOUNTING_HOOK_UNSUPPORTED", "rejected"),
        ("LLM_USAGE_EXCEEDS_RESERVATION", "overage"),
        ("LLM_HTTP_REQUEST_FAILED", "none"),
    ],
)
def test_qc_control_plane_or_unproven_failures_rethrow_without_side_effects(
    session,
    qc_type: str,
    error_code: str,
    ledger_mode: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-{ledger_mode}"
    if ledger_mode != "none":
        _add_qc_failure_parent(
            session,
            qc_type=qc_type,
            call_id=call_id,
            error_code=error_code,
            accounting_status=(
                "rejected"
                if ledger_mode == "rejected"
                else "usage_exceeds_reservation"
                if ledger_mode == "overage"
                else "failed"
            ),
            dispatched=ledger_mode != "rejected",
        )
    error = _qc_execution_error(call_id=call_id, error_code=error_code)
    state = session.get(SceneRunState, "CH100_SC01")
    checkpoint_before = (state.run_checkpoint, dict(state.run_checkpoint_json or {}))

    with pytest.raises(LLMNodeExecutionError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []
    state = session.get(SceneRunState, "CH100_SC01")
    assert (state.run_checkpoint, dict(state.run_checkpoint_json or {})) == checkpoint_before
    assert state.current_qc_report_id is None


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize("mismatch", ["scene", "execution", "step", "error_code"])
def test_qc_failure_ledger_binding_mismatch_never_degrades(
    session,
    qc_type: str,
    mismatch: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-wrong-{mismatch}"
    exception_code = "LLM_HTTP_REQUEST_FAILED"
    _add_qc_failure_parent(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code="LLM_TIMEOUT" if mismatch == "error_code" else exception_code,
        scene_id="OTHER_SCENE" if mismatch == "scene" else "CH100_SC01",
        execution_id="other-execution" if mismatch == "execution" else "exec-qc-ledger",
        execution_step_key="other-step" if mismatch == "step" else step_key,
    )
    error = _qc_execution_error(call_id=call_id, error_code=exception_code)

    with pytest.raises(LLMNodeExecutionError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
def test_qc_true_dispatched_provider_failure_degrades_from_child_ledger_fact(
    session,
    qc_type: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-provider-failure"
    error_code = "LLM_HTTP_REQUEST_FAILED"
    _add_valid_qc_provider_failure_ledger(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code=error_code,
    )

    decision = _evaluate_raising_qc(
        session,
        qc_type=qc_type,
        error=_qc_execution_error(call_id=call_id, error_code=error_code),
        bundle=bundle,
        draft_row_id=draft_row_id,
        execution_step_key=step_key,
    )

    assert decision.should_continue is True
    assert decision.stop_reason == f"{qc_type}_execution_failed"
    report = session.execute(select(QcReport)).scalar_one()
    attempt = session.execute(select(AttemptTracker)).scalar_one()
    assert report.scene_id == "CH100_SC01"
    assert attempt.details_json["llm_call_id"] == call_id
    assert attempt.details_json["execution_step_key"] == step_key


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize(
    "tamper",
    [
        "no_child",
        "child_undispatched",
        "child_bad_status",
        "child_bad_error",
        "ordinal_gap",
        "child_total_mismatch",
        "final_settled",
        "parent_estimated",
        "parent_reserved",
        "parent_charged",
        "parent_prompt",
        "parent_completion",
        "parent_total",
        "parent_latency",
        "parent_dispatch_missing",
        "parent_settled_missing",
        "parent_usage_flag",
        "settled_then_failed",
        "child_estimated_exceeds_reserved",
        "child_charged_mismatch",
    ],
)
def test_qc_provider_failure_ledger_tampering_never_degrades(
    session,
    qc_type: str,
    tamper: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-provider-tamper-{tamper}"
    error_code = "LLM_HTTP_REQUEST_FAILED"
    parent, child = _add_valid_qc_provider_failure_ledger(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code=error_code,
    )
    if tamper == "no_child":
        session.delete(child)
    elif tamper == "child_undispatched":
        child.request_dispatched_at = None
    elif tamper == "child_bad_status":
        child.accounting_status = "released"
    elif tamper == "child_bad_error":
        child.error_code = "LLM_TIMEOUT"
    elif tamper == "ordinal_gap":
        child.provider_attempt_no = 1
    elif tamper == "child_total_mismatch":
        child.total_tokens += 1
        parent.total_tokens += 1
    elif tamper == "final_settled":
        final_child = LlmCallAttempt(
            attempt_id=f"attempt-{call_id}-final",
            llm_call_id=call_id,
            provider_attempt_no=1,
            dispatch_kind="transport_retry",
            request_max_output_tokens=8,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            estimated_tokens=2,
            reserved_tokens=2,
            budget_charged_tokens=2,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:02+00:00",
            settled_at="2026-07-14T00:00:03+00:00",
            latency_ms=3,
        )
        session.add(final_child)
        parent.estimated_tokens += 2
        parent.reserved_tokens += 2
        parent.budget_charged_tokens += 2
        parent.prompt_tokens += 1
        parent.completion_tokens += 1
        parent.total_tokens += 2
        parent.latency_ms += 3
    elif tamper == "parent_estimated":
        parent.estimated_tokens += 1
    elif tamper == "parent_reserved":
        parent.reserved_tokens += 1
    elif tamper == "parent_charged":
        parent.budget_charged_tokens += 1
    elif tamper == "parent_prompt":
        parent.prompt_tokens += 1
    elif tamper == "parent_completion":
        parent.completion_tokens += 1
    elif tamper == "parent_total":
        parent.total_tokens += 1
    elif tamper == "parent_latency":
        parent.latency_ms += 1
    elif tamper == "parent_dispatch_missing":
        parent.request_dispatched_at = None
    elif tamper == "parent_settled_missing":
        parent.settled_at = None
    elif tamper == "parent_usage_flag":
        parent.usage_is_estimate = not child.usage_is_estimate
    elif tamper == "settled_then_failed":
        child.accounting_status = "settled"
        child.error_code = None
        child.error_text = None
        final_child = LlmCallAttempt(
            attempt_id=f"attempt-{call_id}-final-failure",
            llm_call_id=call_id,
            provider_attempt_no=1,
            dispatch_kind="transport_retry",
            request_max_output_tokens=8,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            estimated_tokens=2,
            reserved_tokens=2,
            budget_charged_tokens=2,
            accounting_status="failed",
            request_dispatched_at="2026-07-14T00:00:02+00:00",
            settled_at="2026-07-14T00:00:03+00:00",
            latency_ms=3,
            error_code=error_code,
            error_text="final provider failure",
        )
        session.add(final_child)
        parent.estimated_tokens += 2
        parent.reserved_tokens += 2
        parent.budget_charged_tokens += 2
        parent.prompt_tokens += 1
        parent.completion_tokens += 1
        parent.total_tokens += 2
        parent.latency_ms += 3
    elif tamper == "child_estimated_exceeds_reserved":
        child.estimated_tokens = child.reserved_tokens + 1
        parent.estimated_tokens = child.estimated_tokens
    elif tamper == "child_charged_mismatch":
        child.budget_charged_tokens -= 1
        parent.budget_charged_tokens = child.budget_charged_tokens
    session.commit()
    error = _qc_execution_error(call_id=call_id, error_code=error_code)

    with pytest.raises(LLMNodeExecutionError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
def test_qc_owner_lease_prerenewal_failure_rethrows_before_provider_and_side_effects(
    session,
    qc_type: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    if qc_type == "hard_qc":
        client = FakeQcClient(_base_qc_payload(resolution_code="hard_pass", next_action="pass"))
        engine = HardQcEngine(session, llm_client=client)
    else:
        client = FakeSoftQcClient(
            [_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")]
        )
        engine = SoftQcEngine(session, llm_client=client)

    def lose_lease_before_provider(*, lease_seconds: int) -> None:
        del lease_seconds
        raise DomainError("RUN_OWNER_LEASE_LOST", "lost before QC provider", status_code=409)

    token = begin_llm_execution("exec-qc-ledger", lease_renewer=lose_lease_before_provider)
    try:
        with pytest.raises(LLMNodeExecutionError) as raised:
            if qc_type == "hard_qc":
                engine.evaluate(
                    scene_id="CH100_SC01",
                    bundle=bundle,
                    neutral_draft_row_id=draft_row_id,
                    neutral_content="Draft under ledger review.",
                    execution_step_key=step_key,
                )
            else:
                engine.evaluate(
                    scene_id="CH100_SC01",
                    bundle=bundle,
                    source_draft_row_id=draft_row_id,
                    source_draft_content="Draft under ledger review.",
                    execution_step_key=step_key,
                )
    finally:
        end_llm_execution(token)

    assert raised.value.error_code == "RUN_OWNER_LEASE_LOST"
    assert client.requests == []
    assert session.execute(select(LlmCall)).scalars().all() == []
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []


def test_hard_qc_engine_degrades_runtime_failure_to_continue_with_warning(session) -> None:
    _seed_scene(session)
    session.get(ChapterGoal, "CH100").project_id = "PROJECT_QC"
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
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = "exec-hard-qc-runtime-failure"
    state.run_execution_status = "active"
    state.scene_token_budget = 100_000
    session.commit()

    engine = HardQcEngine(session, llm_client=FakeAccountedQcRuntimeFailureClient())

    token = begin_llm_execution("exec-hard-qc-runtime-failure")
    try:
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
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "hard_qc")).scalars().one()
    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "hard_qc")).scalars().one()
    llm_attempt = session.execute(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == llm_call.llm_call_id)
    ).scalar_one()

    # Wave 2（§5.4/§7.7）：QC 运行时失败降级续跑；LlmCall 仍留错误审计
    assert decision.branch == "continue"
    assert decision.should_continue is True
    assert decision.stop_reason == "hard_qc_execution_failed"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert llm_call.error_code == "RuntimeError"
    assert llm_attempt.accounting_status == "failed"
    assert llm_attempt.request_dispatched_at is not None
    assert llm_attempt.error_code == llm_call.error_code
    assert llm_call.estimated_tokens == llm_attempt.estimated_tokens
    assert llm_call.reserved_tokens == llm_attempt.reserved_tokens
    assert llm_call.budget_charged_tokens == llm_attempt.budget_charged_tokens
    assert llm_call.prompt_tokens == llm_attempt.prompt_tokens
    assert llm_call.completion_tokens == llm_attempt.completion_tokens
    assert llm_call.total_tokens == llm_attempt.total_tokens
    assert llm_call.latency_ms == llm_attempt.latency_ms
    assert report.resolution_code == "hard_pass"
    warning = next(issue for issue in report.issues_json if issue["issue_key"] == "hard_qc_execution_failed")
    assert warning["quality_level"] == "Q2"
    assert "timed out" in warning["message"]
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []
