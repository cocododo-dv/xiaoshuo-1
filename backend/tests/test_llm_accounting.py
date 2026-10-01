from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from novel_system.db.models import (
    LlmCall,
    LlmCallAttempt,
    SceneDraft,
    SceneRunState,
)
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import LLMClient, LLMRequest, LLMResponse
from tests.support.accounting import (
    accounting_module as _accounting_module,
    accounting_request as _request,
    project_call_context as _context,
    scene_call_context as _scene_context,
    scene_run_state as _scene_run_state,
    seed_scene_parent as _seed_scene_parent,
)


@pytest.mark.parametrize(
    "ownership",
    [
        {"execution_id": "exec-only"},
        {"execution_step_key": "step-only"},
        {"execution_id": "exec", "execution_step_key": "   "},
        {"execution_id": "", "execution_step_key": ""},
        {"execution_id": "   ", "execution_step_key": "step"},
        {"run_job_id": "job-without-execution"},
        {"run_job_id": "job-without-step", "execution_id": "exec"},
        {"run_job_id": "", "execution_id": "exec", "execution_step_key": "step"},
    ],
)
def test_direct_execute_rejects_partial_execution_ownership_before_parent_or_provider(
    session,
    ownership: dict[str, str],
) -> None:
    accounting = _accounting_module()

    class NeverCalledClient(accounting.OnlineAccountedExecution):
        calls = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            self.calls += 1
            raise AssertionError("provider boundary must not be reached")

    client = NeverCalledClient()
    with pytest.raises(ValueError, match="LLMCallContext"):
        context = accounting.LLMCallContext(
            scope_type="project",
            scope_id="project-1",
            node_id="neutral_draft",
            step="draft",
            project_id="project-1",
            **ownership,
        )
        accounting.execute_accounted_call(session, client, _request(), context)

    assert client.calls == 0
    assert session.query(LlmCall).count() == 0
    assert session.query(LlmCallAttempt).count() == 0


@pytest.mark.parametrize(
    "ownership",
    [
        {"execution_id": "exec-only"},
        {"execution_step_key": "step-only"},
        {"execution_id": "", "execution_step_key": ""},
        {"run_job_id": "job-only"},
        {"run_job_id": "job-without-step", "execution_id": "exec"},
        {"run_job_id": "", "execution_id": "exec", "execution_step_key": "step"},
    ],
)
def test_record_rejected_call_rejects_partial_ownership_before_parent(
    session,
    ownership: dict[str, str],
) -> None:
    accounting = _accounting_module()
    rejection = accounting.LLMAccountingRejected("LOCAL_REJECTION", "local rejection")

    with pytest.raises(ValueError, match="LLMCallContext"):
        context = accounting.LLMCallContext(
            scope_type="project",
            scope_id="project-1",
            node_id="neutral_draft",
            step="draft",
            project_id="project-1",
            **ownership,
        )
        accounting.record_rejected_call(
            session,
            _request(),
            context,
            rejection,
        )

    assert session.query(LlmCall).count() == 0
    assert session.query(LlmCallAttempt).count() == 0


def test_public_local_rejection_records_zero_child_parent_without_budget_charge(session) -> None:
    accounting = _accounting_module()
    rejection = accounting.LLMAccountingRejected(
        "CONTINUITY_BUDGET_EXCEEDED",
        "prompt requires a scene split",
        details={"requires_scene_split": True},
    )

    call_id = accounting.record_rejected_call(
        session,
        _request(),
        _context(accounting),
        rejection,
        llm_call_id="local-rejection-call",
        request_payload_summary={"continuity_warning": {"requires_scene_split": True}},
        response_payload_summary={"attempt_count": 0, "retryable": False},
    )

    parent = session.get(LlmCall, call_id)
    assert parent.accounting_status == "rejected"
    assert parent.error_code == "CONTINUITY_BUDGET_EXCEEDED"
    assert parent.request_dispatched_at is None
    assert parent.latency_ms == 0
    assert (parent.estimated_tokens, parent.reserved_tokens, parent.budget_charged_tokens) == (0, 0, 0)
    assert (parent.prompt_tokens, parent.completion_tokens, parent.total_tokens) == (0, 0, 0)
    assert parent.request_payload_summary["continuity_warning"]["requires_scene_split"] is True
    assert parent.response_payload_summary["attempt_count"] == 0
    assert parent.response_payload_summary["retryable"] is False
    assert parent.response_payload_summary["_audit_schema_version"] == 2
    assert session.query(LlmCallAttempt).count() == 0


def test_retired_offline_context_cannot_even_be_constructed(session) -> None:
    """B09-04:离线确定性执行模式退役了——上下文的类型只剩 online，带着离线模式的上下文在构造时就被拒，
    到不了派发，账本里不会多出一行。"""
    accounting = _accounting_module()

    with pytest.raises(ValueError, match="must be online"):
        replace(_context(accounting), provider_execution_mode="offline_deterministic")

    assert session.query(LlmCall).count() == 0
    assert session.query(LlmCallAttempt).count() == 0


def test_historical_offline_product_no_longer_validates(session) -> None:
    """账本里历史的离线确定性产品(零尝试、零用量)过不了产品校验:检查点不能靠它续跑。"""
    accounting = _accounting_module()
    now = datetime.now(UTC).isoformat()
    parent = LlmCall(
        llm_call_id="historical-offline-product",
        provider="offline_deterministic",
        scope_type="project",
        scope_id="project-1",
        project_id="project-1",
        node_id="neutral_draft",
        step="draft",
        request_payload_summary={"_accounting_provider_execution_mode": "offline_deterministic"},
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        estimated_tokens=0,
        reserved_tokens=0,
        budget_charged_tokens=0,
        latency_ms=0,
        usage_is_estimate=False,
        accounting_status="settled",
        settled_at=now,
    )
    session.add(parent)
    session.commit()

    with pytest.raises(accounting.LLMAccountingError) as exc_info:
        accounting.validate_product_call_ledger(session, parent, expected_outcome="completed")

    assert exc_info.value.code == "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID"


def test_product_hash_is_stamped_on_the_ledger_row_by_the_accounting_module(session) -> None:
    """产品哈希记在账本行的回包摘要里（B01-23：账本格式只有计量模块一个主人）：并进已有的摘要、不提交；
    账本行不在时返回 False，由调用方报它自己的错。"""
    accounting = _accounting_module()
    now = datetime.now(UTC).isoformat()
    session.add(
        LlmCall(
            llm_call_id="product-hash-parent",
            provider="openai_compatible",
            scope_type="project",
            scope_id="project-1",
            project_id="project-1",
            node_id="neutral_draft",
            step="draft",
            request_payload_summary={},
            response_payload_summary={"request_id": "r-1"},
            accounting_status="settled",
            settled_at=now,
        )
    )
    session.commit()

    assert accounting.stamp_product_hash(session, "product-hash-parent", "archive_prose_product_hash", "h" * 64) is True
    summary = session.get(LlmCall, "product-hash-parent").response_payload_summary
    # 并进已有的摘要（摘要照旧过审计的有界指纹，已有的键还在）
    assert summary["archive_prose_product_hash"] == "h" * 64 and "request_id" in summary
    session.rollback()
    assert "archive_prose_product_hash" not in session.get(LlmCall, "product-hash-parent").response_payload_summary
    assert accounting.stamp_product_hash(session, "no-such-call", "archive_prose_product_hash", "h" * 64) is False


def test_online_wrapper_forwards_attempt_hook_and_is_fully_accounted(session) -> None:
    accounting = _accounting_module()
    inner = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "wrapped-online",
                    "model": "test-model",
                    "output_text": '{"scene_text":"ok"}',
                    "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                },
            )
        ),
    )

    class HookForwardingWrapper(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            return inner.generate_accounted(request, accounting_hook=accounting_hook)

    response = accounting.execute_accounted_call(
        session,
        HookForwardingWrapper(),
        _request(),
        _context(accounting),
    )

    assert response.request_id == "wrapped-online"
    assert session.query(LlmCallAttempt).count() == 1
    assert session.query(LlmCall).one().accounting_status == "settled"


def test_scene_online_call_initializes_missing_budget_before_parent_and_dispatch(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-budget-auto-init"
    _seed_scene_parent(session, scene_id)

    class AccountedClient(accounting.OnlineAccountedExecution):
        post_count = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.post_count += 1
            response = LLMResponse(
                request_id="auto-budget-1",
                provider="fake-provider",
                model=request.model,
                text='{"scene_text":"ok"}',
                structured_output={"scene_text": "ok"},
                response_format=request.response_format,
                raw_response={"id": "auto-budget-1"},
                usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                raw_usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                usage_present=True,
                usage_complete=True,
            )
            accounting_hook.after_response(
                handle,
                request=request,
                response=response,
                latency_ms=1,
            )
            return response

    client = AccountedClient()
    response = accounting.execute_accounted_call(
        session,
        client,
        _request(),
        _scene_context(accounting, scene_id),
    )

    state = session.get(SceneRunState, scene_id)
    assert response.request_id == "auto-budget-1"
    assert client.post_count == 1
    assert state is not None
    assert state.scene_token_budget and state.scene_token_budget > 0
    assert state.scene_budget_basis_json["scene_token_budget"] == state.scene_token_budget
    assert state.provider_attempts_used == 1
    assert state.scene_tokens_used == 14
    assert session.query(LlmCall).count() == 1
    assert session.query(LlmCallAttempt).count() == 1


def test_duplicate_identical_after_response_callback_is_idempotent(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-duplicate-response-callback"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    class DuplicateResponseClient(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            response = LLMResponse(
                request_id="duplicate-response",
                provider="fake-provider",
                model=request.model,
                text='{"scene_text":"ok"}',
                structured_output={"scene_text": "ok"},
                response_format=request.response_format,
                raw_response={"id": "duplicate-response"},
                usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                raw_usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                usage_present=True,
                usage_complete=True,
            )
            for _ in range(2):
                accounting_hook.after_response(
                    handle,
                    request=request,
                    response=response,
                    latency_ms=3,
                )
            return response

    accounting.execute_accounted_call(
        session,
        DuplicateResponseClient(),
        _request(),
        _scene_context(accounting, scene_id),
    )
    session.expire_all()
    assert session.get(SceneRunState, scene_id).scene_tokens_used == 14
    assert session.query(LlmCallAttempt).one().total_tokens == 14


def test_conflicting_duplicate_provider_callback_fails_stably_without_double_charge(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-conflicting-response-callback"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    class ConflictingResponseClient(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            first = LLMResponse(
                request_id="first-callback",
                provider="fake-provider",
                model=request.model,
                text='{"scene_text":"ok"}',
                structured_output={"scene_text": "ok"},
                response_format=request.response_format,
                raw_response={"id": "first-callback"},
                usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                raw_usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                usage_present=True,
                usage_complete=True,
            )
            accounting_hook.after_response(handle, request=request, response=first, latency_ms=3)
            conflicting = replace(
                first,
                request_id="conflicting-callback",
                usage={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
                raw_usage={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
            )
            accounting_hook.after_response(
                handle,
                request=request,
                response=conflicting,
                latency_ms=3,
            )
            return first

    with pytest.raises(Exception) as conflict:
        accounting.execute_accounted_call(
            session,
            ConflictingResponseClient(),
            _request(),
            _scene_context(accounting, scene_id),
        )
    assert getattr(conflict.value, "code", None) == "LLM_ACCOUNTING_ATTEMPT_CALLBACK_CONFLICT"
    session.expire_all()
    assert session.get(SceneRunState, scene_id).scene_tokens_used == 14


def test_duplicate_identical_after_error_callback_is_idempotent(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-duplicate-error-callback"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    class DuplicateErrorClient(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            error = RuntimeError("provider failed")
            for _ in range(2):
                accounting_hook.after_error(
                    handle,
                    request=request,
                    error=error,
                    raw_response={
                        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
                    },
                    provider_request_id="duplicate-error",
                    latency_ms=5,
                )
            raise error

    with pytest.raises(RuntimeError, match="provider failed"):
        accounting.execute_accounted_call(
            session,
            DuplicateErrorClient(),
            _request(),
            _scene_context(accounting, scene_id),
        )
    session.expire_all()
    assert session.get(SceneRunState, scene_id).scene_tokens_used == 14
    assert session.query(LlmCallAttempt).one().accounting_status == "failed"


def test_online_client_without_hook_contract_is_rejected_before_generate(
    session, monkeypatch
) -> None:
    accounting = _accounting_module()
    monkeypatch.setattr(accounting, "_elapsed_ms", lambda _started: 17)

    class HooklessOnlineClient:
        called = 0

        def generate(self, request: LLMRequest, *, accounting_hook=None) -> LLMResponse:
            self.called += 1
            return LLMResponse(
                request_id="must-not-run",
                provider="openai_compatible",
                model=request.model,
                text="ok",
                structured_output=None,
                response_format=request.response_format,
                raw_response={},
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    client = HooklessOnlineClient()
    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _context(accounting),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_HOOK_UNSUPPORTED"
    assert client.called == 0
    assert session.query(LlmCallAttempt).count() == 0
    parent = session.query(LlmCall).one()
    assert parent.accounting_status == "rejected"
    assert parent.latency_ms == 0


def test_online_wrapper_that_drops_hook_never_leaves_a_live_parent(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-wrapper-drops-hook"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    session.get(SceneRunState, scene_id)  # 行进会话的身份表（缓存态）
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(
            200,
            json={
                "id": "unaccounted-online",
                "model": "test-model",
                "output_text": '{"scene_text":"must not be returned"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    inner = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    class HookDroppingWrapper(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            return inner.generate(request)

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            HookDroppingWrapper(),
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="wrapper-drops-hook",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_HOOK_NOT_INVOKED"
    assert post_count == 1
    attempt = session.query(LlmCallAttempt).one()
    parent = session.get(LlmCall, "wrapper-drops-hook")
    run_state = session.get(SceneRunState, scene_id)
    assert attempt.provider_attempt_no == 0
    assert attempt.request_dispatched_at is not None
    assert attempt.accounting_status == "failed"
    assert attempt.total_tokens == 14
    assert parent.accounting_status == "failed"
    assert run_state.provider_attempts_used == 1
    assert run_state.scene_tokens_used == 14
    assert run_state.scene_tokens_reserved == 0
    assert run_state.run_execution_status == "accounting_integrity_blocked"

    # Even if the conservatively reconstructed usage crosses the total budget,
    # the durable blocked status + dispatched error tombstone must win over a
    # generic corruption ValueError on every later provider attempt.
    run_state.scene_tokens_used = run_state.scene_token_budget + 1
    session.commit()

    with pytest.raises(Exception) as blocked_error:
        accounting.execute_accounted_call(
            session,
            HookDroppingWrapper(),
            _request(),
            replace(
                _scene_context(accounting, scene_id),
                execution_id="blocked-after-hook-drop",
            ),
            llm_call_id="blocked-after-hook-drop",
        )
    assert getattr(blocked_error.value, "code", None) == "LLM_ACCOUNTING_INTEGRITY_BLOCKED"
    assert post_count == 1
    assert session.query(LlmCallAttempt).count() == 1


def test_online_capability_exception_without_attempt_is_conservatively_audited(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-capability-exception"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    class ExplodingCapability(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            raise RuntimeError("wrapper failed after accepting online execution")

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            ExplodingCapability(),
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="capability-exception",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_UNKNOWN_DISPATCH"
    attempt = session.query(LlmCallAttempt).one()
    parent = session.get(LlmCall, "capability-exception")
    run_state = session.get(SceneRunState, scene_id)
    assert attempt.request_dispatched_at is not None
    assert attempt.total_tokens == attempt.estimated_tokens > 0
    assert attempt.accounting_status == "failed"
    assert attempt.error_code == "LLM_ACCOUNTING_UNKNOWN_DISPATCH"
    assert parent.accounting_status == "failed"
    assert run_state.provider_attempts_used == 1
    assert run_state.scene_tokens_used == attempt.total_tokens
    assert run_state.run_execution_status == "accounting_integrity_blocked"


def test_online_capability_return_with_dispatched_but_unsettled_attempt_is_blocked(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-missing-after-response"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(
            200,
            json={
                "id": "missing-after-response",
                "model": "test-model",
                "output_text": '{"scene_text":"must not return"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    inner = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    class MissingAfterResponse(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            return inner.generate(request)

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            MissingAfterResponse(),
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="missing-after-response",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE"
    parent = session.get(LlmCall, "missing-after-response")
    attempt = session.query(LlmCallAttempt).one()
    run_state = session.get(SceneRunState, scene_id)
    assert post_count == 1
    assert parent.accounting_status == "failed"
    assert attempt.accounting_status == "failed"
    assert attempt.total_tokens == 14
    assert attempt.error_code == "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE"
    assert run_state.provider_attempts_used == 1
    assert run_state.scene_tokens_used == 14
    assert run_state.scene_tokens_reserved == 0
    assert run_state.run_execution_status == "accounting_integrity_blocked"

    with pytest.raises(Exception) as blocked_error:
        accounting.execute_accounted_call(
            session,
            MissingAfterResponse(),
            _request(),
            replace(
                _scene_context(accounting, scene_id),
                execution_id="blocked-missing-after-response",
            ),
            llm_call_id="blocked-missing-after-response",
        )
    assert getattr(blocked_error.value, "code", None) == "LLM_ACCOUNTING_INTEGRITY_BLOCKED"
    assert post_count == 1
    assert session.query(LlmCallAttempt).count() == 1


def test_online_capability_exception_with_dispatched_but_unsettled_attempt_is_blocked(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-missing-after-error"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    class MissingAfterError(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            raise RuntimeError("wrapper lost after_error callback")

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            MissingAfterError(),
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="missing-after-error",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE"
    parent = session.get(LlmCall, "missing-after-error")
    attempt = session.query(LlmCallAttempt).one()
    run_state = session.get(SceneRunState, scene_id)
    assert parent.accounting_status == "failed"
    assert attempt.accounting_status == "failed"
    assert attempt.total_tokens == attempt.estimated_tokens > 0
    assert attempt.error_code == "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE"
    assert run_state.provider_attempts_used == 1
    assert run_state.scene_tokens_used == attempt.total_tokens
    assert run_state.scene_tokens_reserved == 0
    assert run_state.run_execution_status == "accounting_integrity_blocked"


@pytest.mark.parametrize("state_kind", ["missing", "null_budget"])
def test_untracked_dispatch_keeps_child_audit_when_scene_budget_is_uninitialized(
    session,
    state_kind: str,
) -> None:
    accounting = _accounting_module()
    scene_id = f"scene-untracked-{state_kind}"
    _seed_scene_parent(session, scene_id)
    if state_kind == "null_budget":
        session.add(
            _scene_run_state(
                session,
                scene_id=scene_id,
                scene_token_budget=None,
                provider_attempt_budget=5,
            )
        )
        session.commit()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(
            200,
            json={
                "id": f"untracked-{state_kind}",
                "model": "test-model",
                "output_text": '{"scene_text":"untracked"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    inner = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    class HookDroppingWrapper(accounting.OnlineAccountedExecution):
        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            return inner.generate(request)

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            HookDroppingWrapper(),
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id=f"untracked-{state_kind}",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_HOOK_NOT_INVOKED"
    attempt = session.query(LlmCallAttempt).one()
    assert post_count == 1
    assert attempt.total_tokens == 14
    assert attempt.request_dispatched_at is not None
    if state_kind == "null_budget":
        run_state = session.get(SceneRunState, scene_id)
        assert run_state.provider_attempts_used == 1
        assert run_state.scene_tokens_used == 14
        assert run_state.run_execution_status == "accounting_integrity_blocked"

    with pytest.raises(Exception):
        accounting.execute_accounted_call(
            session,
            HookDroppingWrapper(),
            _request(),
            replace(
                _scene_context(accounting, scene_id),
                execution_id=f"blocked-{state_kind}",
            ),
            llm_call_id=f"blocked-{state_kind}",
        )
    assert post_count == 1
    assert session.query(LlmCallAttempt).count() == 1


def test_provider_failure_persists_failed_attempt_and_parent_with_conservative_charge(session) -> None:
    accounting = _accounting_module()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception, match="timed out"):
        accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    session.expire_all()
    parent = session.query(LlmCall).one()
    attempt = session.query(LlmCallAttempt).one()
    assert attempt.accounting_status == "failed"
    assert attempt.error_code == "LLM_REQUEST_TIMEOUT"
    assert attempt.usage_is_estimate is True
    assert attempt.total_tokens > 0
    assert attempt.budget_charged_tokens == attempt.total_tokens
    assert parent.accounting_status == "failed"
    assert parent.error_code == "LLM_REQUEST_TIMEOUT"
    assert parent.total_tokens == attempt.total_tokens
    assert parent.budget_charged_tokens == attempt.budget_charged_tokens


def test_unexpected_post_exception_settles_accounting_without_reservation_leak(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-post-runtime-error"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    cached_state = session.get(SceneRunState, scene_id)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport implementation exploded")

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="post-runtime-error",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_HTTP_CLIENT_EXCEPTION"
    parent = session.get(LlmCall, "post-runtime-error")
    attempt = session.query(LlmCallAttempt).one()
    assert parent.accounting_status == "failed"
    assert parent.error_code == "LLM_HTTP_CLIENT_EXCEPTION"
    assert attempt.accounting_status == "failed"
    assert attempt.error_code == "LLM_HTTP_CLIENT_EXCEPTION"
    assert attempt.request_dispatched_at is not None
    assert cached_state.scene_tokens_reserved == 0
    assert cached_state.provider_attempts_used == 1
    assert cached_state.scene_tokens_used == attempt.total_tokens > 0


def test_transport_retry_aggregates_each_physical_attempt_exactly_once(session) -> None:
    accounting = _accounting_module()
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(
            200,
            json={
                "id": "retry-ok",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )

    accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    session.expire_all()
    parent = session.query(LlmCall).one()
    attempts = session.query(LlmCallAttempt).order_by(LlmCallAttempt.provider_attempt_no).all()
    assert post_count == 2
    assert [row.dispatch_kind for row in attempts] == ["initial", "transport_retry"]
    assert [row.accounting_status for row in attempts] == ["failed", "settled"]
    assert parent.total_tokens == sum(row.total_tokens for row in attempts)
    assert parent.budget_charged_tokens == sum(row.budget_charged_tokens for row in attempts)
    assert parent.reserved_tokens == sum(row.reserved_tokens for row in attempts)
    assert parent.usage_is_estimate is True
    assert parent.accounting_status == "settled"


def test_missing_text_degrade_uses_new_larger_reservation_and_aggregates_actual_usage(session) -> None:
    accounting = _accounting_module()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            return httpx.Response(
                200,
                json={
                    "id": "missing-text",
                    "model": "test-model",
                    "usage": {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "degrade-ok",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    session.expire_all()
    parent = session.query(LlmCall).one()
    attempts = session.query(LlmCallAttempt).order_by(LlmCallAttempt.provider_attempt_no).all()
    assert [row.dispatch_kind for row in attempts] == ["initial", "missing_text_degrade"]
    assert [row.request_max_output_tokens for row in attempts] == [64, 128]
    assert attempts[1].reserved_tokens > attempts[0].reserved_tokens
    assert [row.total_tokens for row in attempts] == [11, 14]
    assert parent.total_tokens == 25
    assert parent.budget_charged_tokens == 25
    assert parent.usage_is_estimate is False


def test_structured_degrade_recomputes_reservation_for_rewritten_messages(session) -> None:
    accounting = _accounting_module()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            return httpx.Response(500, text="guided_grammar compile error")
        return httpx.Response(
            200,
            json={
                "id": "schema-degrade-ok",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    request = replace(
        _request(),
        response_schema={
            "name": "scene_payload",
            "schema": {
                "type": "object",
                "properties": {"scene_text": {"type": "string", "description": "正文" * 40}},
                "required": ["scene_text"],
            },
        },
    )

    accounting.execute_accounted_call(session, client, request, _context(accounting))

    attempts = session.query(LlmCallAttempt).order_by(LlmCallAttempt.provider_attempt_no).all()
    assert post_count == 2
    assert [row.dispatch_kind for row in attempts] == [
        "initial",
        "structured_output_degrade",
    ]
    assert attempts[1].reserved_tokens > attempts[0].reserved_tokens


def test_responses_to_chat_degrade_has_a_distinct_durable_attempt_kind(session) -> None:
    accounting = _accounting_module()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(404, json={"error": {"message": "responses unsupported"}})
        return httpx.Response(
            200,
            json={
                "id": "chat-degrade-ok",
                "model": "api-mode-degrade-model",
                "choices": [
                    {
                        "message": {"content": '{"scene_text":"ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    accounting.execute_accounted_call(
        session,
        client,
        replace(_request(), model="api-mode-degrade-model"),
        _context(accounting),
    )

    attempts = session.query(LlmCallAttempt).order_by(LlmCallAttempt.provider_attempt_no).all()
    assert paths == ["/v1/responses", "/v1/chat/completions"]
    assert [row.dispatch_kind for row in attempts] == ["initial", "api_mode_degrade"]


def test_provider_attempt_budget_rejects_second_post_without_erasing_first_charge(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-attempt-budget"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=1,
        )
    )
    session.commit()
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED"
    session.expire_all()
    run_state = session.get(SceneRunState, scene_id)
    parent = session.query(LlmCall).one()
    attempts = session.query(LlmCallAttempt).all()
    assert post_count == 1
    assert run_state.provider_attempts_used == 1
    assert len(attempts) == 1
    assert attempts[0].accounting_status == "failed"
    assert parent.accounting_status == "failed"
    assert parent.budget_charged_tokens == attempts[0].budget_charged_tokens > 0


def test_token_budget_rejection_before_dispatch_has_no_post_attempt_or_charge(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-token-budget"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(500)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
    session.expire_all()
    run_state = session.get(SceneRunState, scene_id)
    parent = session.query(LlmCall).one()
    assert post_count == 0
    assert run_state.provider_attempts_used == 0
    assert run_state.scene_tokens_reserved == 0
    assert session.query(LlmCallAttempt).count() == 0
    assert parent.accounting_status == "rejected"
    assert parent.budget_charged_tokens == 0


def test_business_attempt_budget_rejection_is_distinct_and_has_zero_provider_io(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-business-attempt-budget"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            total_attempt_count=4,
            attempt_budget=4,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(500)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED"
    state = session.get(SceneRunState, scene_id)
    parent = session.query(LlmCall).one()
    assert post_count == 0
    assert state.total_attempt_count == 4
    assert state.provider_attempts_used == 0
    assert state.scene_tokens_reserved == 0
    assert session.query(LlmCallAttempt).count() == 0
    assert parent.accounting_status == "rejected"


def test_business_attempt_budget_race_after_reservation_releases_fence_before_provider_io(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-business-attempt-race"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            total_attempt_count=0,
            attempt_budget=1,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    reservation_barrier = threading.Barrier(2)
    mutation_barrier = threading.Barrier(2)
    worker_errors: list[BaseException] = []

    def exhaust_business_budget() -> None:
        worker = SessionLocal()
        try:
            reservation_barrier.wait(timeout=10)
            state = worker.get(SceneRunState, scene_id)
            assert state is not None
            state.total_attempt_count = state.attempt_budget
            worker.commit()
            mutation_barrier.wait(timeout=10)
        except BaseException as exc:  # pragma: no cover - surfaced below
            worker_errors.append(exc)
        finally:
            worker.close()

    worker_thread = threading.Thread(target=exhaust_business_budget)
    worker_thread.start()

    def lifecycle_observer(stage: str, _attempt_id: str) -> None:
        if stage == "reservation_committed":
            reservation_barrier.wait(timeout=10)
            mutation_barrier.wait(timeout=10)

    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(500)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
            _lifecycle_observer=lifecycle_observer,
        )
    worker_thread.join(timeout=10)

    assert not worker_thread.is_alive()
    assert worker_errors == []
    assert getattr(exc_info.value, "code", None) == "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED"
    session.expire_all()
    state = session.get(SceneRunState, scene_id)
    assert post_count == 0
    assert state.total_attempt_count == 1
    assert state.provider_attempts_used == 0
    assert state.scene_tokens_reserved == 0
    attempt = session.query(LlmCallAttempt).one()
    assert attempt.accounting_status == "rejected"
    assert attempt.request_dispatched_at is None


def test_scene_accounting_refreshes_cached_run_state_after_settlement(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-cached-settlement"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    cached_state = session.get(SceneRunState, scene_id)
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "cached-settlement",
                    "model": "test-model",
                    "output_text": '{"scene_text":"ok"}',
                    "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                },
            )
        ),
    )

    accounting.execute_accounted_call(
        session,
        client,
        _request(),
        _scene_context(accounting, scene_id),
    )

    assert cached_state.scene_tokens_used == 14
    assert cached_state.scene_tokens_reserved == 0
    assert cached_state.provider_attempts_used == 1


def test_pending_business_write_is_precommitted_and_survives_provider_failure(session) -> None:
    accounting = _accounting_module()
    _seed_scene_parent(session, "scene-1")
    session.add(
        SceneDraft(
            row_id="business-before-provider",
            scene_id="scene-1",
            chapter_id="chapter-1",
            stage="neutral",
            content="already generated business text",
            source_bundle_id="bundle-1",
            source_bundle_hash="hash-1",
        )
    )
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception):
        accounting.execute_accounted_call(session, client, _request(), _context(accounting))
    session.rollback()

    with SessionLocal() as verifier:
        assert verifier.get(SceneDraft, "business-before-provider") is not None
        assert verifier.query(LlmCall).one().accounting_status == "failed"


def test_network_wait_holds_no_caller_write_lock_and_second_session_can_commit(session) -> None:
    accounting = _accounting_module()
    _seed_scene_parent(session, "scene-1")
    _seed_scene_parent(session, "scene-2")
    session.add(
        SceneDraft(
            row_id="caller-pending",
            scene_id="scene-1",
            chapter_id="chapter-1",
            stage="neutral",
            content="caller text",
            source_bundle_id="bundle-1",
            source_bundle_hash="hash-1",
        )
    )
    session.flush()

    def handler(_request: httpx.Request) -> httpx.Response:
        with SessionLocal() as concurrent:
            concurrent.add(
                SceneDraft(
                    row_id="concurrent-during-provider",
                    scene_id="scene-2",
                    chapter_id="chapter-1",
                    stage="neutral",
                    content="concurrent text",
                    source_bundle_id="bundle-2",
                    source_bundle_hash="hash-2",
                )
            )
            concurrent.commit()
        return httpx.Response(
            200,
            json={
                "id": "lock-free-ok",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    with SessionLocal() as verifier:
        assert verifier.get(SceneDraft, "caller-pending") is not None
        assert verifier.get(SceneDraft, "concurrent-during-provider") is not None
        assert verifier.query(LlmCall).one().accounting_status == "settled"


def test_response_exposes_stable_call_id_and_postprocess_failure_reuses_parent(session) -> None:
    accounting = _accounting_module()
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "postprocess-ok",
                    "model": "test-model",
                    "output_text": '{"scene_text":"ok"}',
                    "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                },
            )
        ),
    )

    response = accounting.execute_accounted_call(
        session,
        client,
        _request(),
        _context(accounting),
        llm_call_id="stable-call-id",
    )
    accounting.mark_postprocess_failure(
        session,
        response.llm_call_id,
        error_code="CALLER_SCHEMA_INVALID",
        error_text="missing scene field",
    )

    session.expire_all()
    parent = session.query(LlmCall).one()
    assert response.llm_call_id == "stable-call-id"
    assert parent.llm_call_id == "stable-call-id"
    assert parent.accounting_status == "failed"
    assert parent.error_code == "CALLER_SCHEMA_INVALID"
    assert parent.budget_charged_tokens == 14
    assert session.query(LlmCallAttempt).count() == 1


def test_non_object_provider_response_conservatively_settles_child_without_reservation_leak(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-invalid-provider-response"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=["invalid"])),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_RESPONSE_INVALID"
    session.expire_all()
    parent = session.query(LlmCall).one()
    attempt = session.query(LlmCallAttempt).one()
    run_state = session.get(SceneRunState, scene_id)
    assert parent.accounting_status == "failed"
    assert attempt.accounting_status == "failed"
    assert attempt.error_code == "LLM_RESPONSE_INVALID"
    assert attempt.budget_charged_tokens == attempt.estimated_tokens > 0
    assert run_state.scene_tokens_reserved == 0
    assert run_state.scene_tokens_used == attempt.total_tokens


def test_settled_execution_step_cannot_create_a_second_parent_or_post_again(session) -> None:
    accounting = _accounting_module()
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(
            200,
            json={
                "id": f"success-{post_count}",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    context = _context(accounting)

    accounting.execute_accounted_call(session, client, _request(), context, llm_call_id="first-parent")
    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(session, client, _request(), context, llm_call_id="second-parent")

    assert getattr(exc_info.value, "code", None) == "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS"
    assert post_count == 1
    assert session.query(LlmCall).count() == 1


def test_scene_budget_fence_allows_one_inflight_post_and_loser_retries_after_settlement(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-concurrent-budget"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
            scene_tokens_used=100,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    entered_provider = threading.Event()
    release_provider = threading.Event()
    post_count = 0
    first_errors: list[BaseException] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if post_count == 1:
            entered_provider.set()
            assert release_provider.wait(timeout=10)
        return httpx.Response(
            200,
            json={
                "id": "concurrent-winner",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    winner_context = replace(
        _scene_context(accounting, scene_id),
        execution_id="fence-winner-execution",
    )
    loser_context = replace(
        _scene_context(accounting, scene_id),
        execution_id="fence-loser-execution",
    )
    request_reservation = accounting.estimate_request_usage(_request()).reserved_tokens

    def winner_worker() -> None:
        with SessionLocal() as worker_session:
            try:
                accounting.execute_accounted_call(
                    worker_session,
                    client,
                    _request(),
                    winner_context,
                    llm_call_id="fence-winner-call",
                )
            except BaseException as exc:  # noqa: BLE001 - forwarded to main assertion
                first_errors.append(exc)

    thread = threading.Thread(target=winner_worker)
    thread.start()
    assert entered_provider.wait(timeout=10)

    with SessionLocal() as verifier:
        inflight_state = verifier.get(SceneRunState, scene_id)
        assert inflight_state.scene_tokens_reserved == request_reservation

    with SessionLocal() as loser_session:
        with pytest.raises(Exception) as exc_info:
            accounting.execute_accounted_call(
                loser_session,
                client,
                _request(),
                loser_context,
                llm_call_id="fence-loser-rejected",
            )
    assert getattr(exc_info.value, "code", None) == "LLM_SCENE_CALL_IN_FLIGHT"
    assert post_count == 1
    with SessionLocal() as verifier:
        assert verifier.query(LlmCallAttempt).count() == 1
        assert verifier.get(LlmCall, "fence-loser-rejected").accounting_status == "rejected"

    release_provider.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert first_errors == []

    with SessionLocal() as retry_session:
        accounting.execute_accounted_call(
            retry_session,
            client,
            _request(),
            loser_context,
            llm_call_id="fence-loser-retry",
        )

    session.expire_all()
    run_state = session.get(SceneRunState, scene_id)
    assert post_count == 2
    assert run_state.provider_attempts_used == 2
    assert run_state.scene_tokens_reserved == 0
    assert run_state.scene_tokens_used == 128
    attempts = session.query(LlmCallAttempt).all()
    assert len(attempts) == 2
    assert [attempt.reserved_tokens for attempt in attempts] == [
        request_reservation,
        request_reservation,
    ]
    assert all(attempt.total_tokens <= attempt.reserved_tokens for attempt in attempts)


def test_null_scene_token_budget_is_initialized_before_online_attempt(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-null-budget-compat"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=None,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(500)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
        )

    session.expire_all()
    run_state = session.get(SceneRunState, scene_id)
    assert getattr(exc_info.value, "code", None) == "LLM_HTTP_RETRYABLE_FAILURE"
    assert post_count == 1
    assert run_state.scene_token_budget is not None
    assert run_state.scene_budget_basis_json is not None
    assert run_state.scene_tokens_reserved == 0
    assert run_state.scene_tokens_used > 0
    assert run_state.provider_attempts_used == 1
    assert session.query(LlmCallAttempt).count() == 1
    assert session.query(LlmCall).one().accounting_status == "failed"


def test_missing_scene_run_state_is_initialized_before_online_attempt(session) -> None:
    accounting = _accounting_module()
    post_count = 0
    _seed_scene_parent(session, "missing-scene-state")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(500)

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, "missing-scene-state"),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_HTTP_RETRYABLE_FAILURE"
    assert post_count == 1
    state = session.get(SceneRunState, "missing-scene-state")
    assert state.scene_token_budget is not None
    assert state.scene_budget_basis_json is not None
    assert state.provider_attempts_used == 1
    assert session.query(LlmCallAttempt).count() == 1
    assert session.query(LlmCall).one().accounting_status == "failed"


def test_concurrent_same_execution_step_has_one_parent_and_never_double_posts(session) -> None:
    accounting = _accounting_module()
    entered_provider = threading.Event()
    release_provider = threading.Event()
    post_count = 0
    first_errors: list[BaseException] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        entered_provider.set()
        assert release_provider.wait(timeout=10)
        return httpx.Response(
            200,
            json={
                "id": "execution-winner",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=12,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    context = _context(accounting)

    def first_worker() -> None:
        with SessionLocal() as worker_session:
            try:
                accounting.execute_accounted_call(
                    worker_session,
                    client,
                    _request(),
                    context,
                    llm_call_id="execution-first",
                )
            except BaseException as exc:  # noqa: BLE001 - forwarded to main assertion
                first_errors.append(exc)

    thread = threading.Thread(target=first_worker)
    thread.start()
    assert entered_provider.wait(timeout=10)

    with SessionLocal() as competing_session:
        with pytest.raises(Exception) as exc_info:
            accounting.execute_accounted_call(
                competing_session,
                client,
                _request(),
                context,
                llm_call_id="execution-second",
            )
    assert getattr(exc_info.value, "code", None) == "RUN_CHECKPOINT_OUTPUT_MISSING"
    release_provider.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert first_errors == []

    session.expire_all()
    assert post_count == 1
    assert session.query(LlmCall).count() == 1
    assert session.query(LlmCallAttempt).count() == 1
