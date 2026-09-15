from __future__ import annotations

import importlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import LLMClient, LLMRequest, LLMResponse


def _accounting_module():
    return importlib.import_module("novel_system.services.llm_accounting")


def _request(*, max_output_tokens: int = 64) -> LLMRequest:
    return LLMRequest(
        model="test-model",
        messages=[
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "写一个短场景。"},
        ],
        temperature=0,
        max_output_tokens=max_output_tokens,
        response_format="json_object",
        provider="openai_compatible",
        node_id="neutral_draft",
    )


def _context(accounting):
    return accounting.LLMCallContext(
        scope_type="project",
        scope_id="project-1",
        node_id="neutral_draft",
        step="draft",
        project_id="project-1",
        execution_id="execution-1",
        execution_step_key="neutral_draft",
    )


def _scene_context(accounting, scene_id: str):
    return accounting.LLMCallContext(
        scope_type="scene",
        scope_id=scene_id,
        node_id="neutral_draft",
        step="draft",
        project_id="project-1",
        chapter_id="chapter-1",
        scene_id=scene_id,
        execution_id="execution-1",
        execution_step_key="neutral_draft",
    )


def _seed_scene_parent(session, scene_id: str) -> None:
    """Seed the project/chapter/scene authority chain for scene accounting."""
    if session.get(StoryProject, "project-1") is None:
        session.add(
            StoryProject(
                project_id="project-1",
                title="LLM accounting integration",
                outline_text="Test-owned outline",
            )
        )
        session.flush()
    if session.get(ChapterGoal, "chapter-1") is None:
        session.add(
            ChapterGoal(
                chapter_id="chapter-1",
                project_id="project-1",
                planned_scene_count=1,
                chapter_goal="Exercise scene accounting",
            )
        )
        session.flush()
    if session.get(SceneCard, scene_id) is None:
        next_scene_seq = int(
            session.scalar(
                select(func.max(SceneCard.scene_seq)).where(
                    SceneCard.chapter_id == "chapter-1"
                )
            )
            or 0
        ) + 1
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id="chapter-1",
                project_id="project-1",
                scene_seq=next_scene_seq,
                scene_goal="Exercise one accounted provider call",
                onstage_chars_json=[],
                beats_json=[],
            )
        )
        session.flush()


def _scene_run_state(session, *, scene_id: str, **kwargs) -> SceneRunState:
    _seed_scene_parent(session, scene_id)
    return SceneRunState(scene_id=scene_id, **kwargs)


def test_request_estimate_includes_message_overhead_output_and_utf8_reservation() -> None:
    accounting = _accounting_module()

    estimate = accounting.estimate_request_usage(_request(max_output_tokens=80))

    assert estimate.estimated_input_tokens > 0
    assert estimate.estimated_output_tokens == 80
    assert estimate.estimated_tokens == estimate.estimated_input_tokens + 80
    assert estimate.reserved_tokens >= estimate.estimated_tokens
    assert estimate.reserved_tokens >= (
        sum(len(message["content"].encode("utf-8")) for message in _request().messages)
        + accounting.MESSAGE_TOKEN_OVERHEAD * len(_request().messages)
        + 80
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


def test_request_estimate_includes_wire_response_schema_in_first_reservation() -> None:
    accounting = _accounting_module()
    schema = {
        "name": "large_payload",
        "schema": {
            "type": "object",
            "properties": {"scene_text": {"type": "string", "description": "正文约束" * 100}},
        },
    }
    request = replace(_request(max_output_tokens=80), response_schema=schema)

    estimate = accounting.estimate_request_usage(request)
    schema_bytes = len(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    message_bytes = sum(len(message["content"].encode("utf-8")) for message in request.messages)

    assert estimate.reserved_tokens >= (
        schema_bytes
        + message_bytes
        + accounting.MESSAGE_TOKEN_OVERHEAD * len(request.messages)
        + request.max_output_tokens
    )


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


def test_real_client_missing_raw_usage_is_estimated_and_never_charged_as_zero(session) -> None:
    accounting = _accounting_module()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-without-usage",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
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

    response = accounting.execute_accounted_call(
        session,
        client,
        _request(),
        _context(accounting),
    )

    session.expire_all()
    call = session.query(LlmCall).one()
    attempt = session.query(LlmCallAttempt).one()
    assert response.usage_present is False
    assert response.usage_complete is False
    assert call.usage_is_estimate is True
    assert call.total_tokens and call.total_tokens > 0
    assert call.budget_charged_tokens > 0
    assert attempt.usage_is_estimate is True
    assert attempt.total_tokens > 0


def test_explicit_offline_zero_usage_settles_parent_without_physical_attempt(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-offline"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=100,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    class OfflineClient(accounting.OfflineDeterministicExecution):
        def generate_offline_deterministic(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                request_id="offline-1",
                provider="offline_deterministic",
                model=request.model,
                text='{"scene_text":"offline"}',
                structured_output={"scene_text": "offline"},
                response_format=request.response_format,
                raw_response={"id": "offline-1"},
                usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                finish_reason="offline_fallback",
            )

    response = accounting.execute_accounted_call(
        session,
        OfflineClient(),
        _request(),
        replace(
            _scene_context(accounting, scene_id),
            provider_execution_mode="offline_deterministic",
        ),
    )

    session.expire_all()
    call = session.query(LlmCall).one()
    assert response.provider == "offline_deterministic"
    assert call.accounting_status == "settled"
    assert call.total_tokens == 0
    assert call.budget_charged_tokens == 0
    assert call.latency_ms == 0
    assert call.usage_is_estimate is False
    assert session.query(LlmCallAttempt).count() == 0
    run_state = session.get(SceneRunState, scene_id)
    assert run_state.provider_attempts_used == 0
    assert run_state.scene_tokens_reserved == 0
    assert run_state.scene_tokens_used == 0


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
    cached_state = session.get(SceneRunState, scene_id)
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


@pytest.mark.parametrize(
    ("provider", "usage"),
    [
        (
            "openai_compatible",
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        ),
        (
            "offline_deterministic",
            {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
        ),
    ],
)
def test_offline_mode_rejects_non_offline_or_nonzero_response(
    session,
    provider: str,
    usage: dict[str, int],
) -> None:
    accounting = _accounting_module()

    class InvalidOfflineClient(accounting.OfflineDeterministicExecution):
        def generate_offline_deterministic(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                request_id="invalid-offline",
                provider=provider,
                model=request.model,
                text="not offline",
                structured_output=None,
                response_format=request.response_format,
                raw_response={},
                usage=usage,
            )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            InvalidOfflineClient(),
            _request(),
            replace(
                _context(accounting),
                provider_execution_mode="offline_deterministic",
            ),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_OFFLINE_RESPONSE_INVALID"
    assert session.query(LlmCallAttempt).count() == 0
    assert session.query(LlmCall).one().accounting_status == "rejected"


def test_offline_method_name_without_explicit_capability_is_rejected_before_call(session) -> None:
    accounting = _accounting_module()

    class MethodNameOnlyOfflineClient:
        called = 0

        def generate_offline_deterministic(self, request: LLMRequest) -> LLMResponse:
            self.called += 1
            raise AssertionError("method-name-only client must not execute")

    client = MethodNameOnlyOfflineClient()
    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            replace(
                _context(accounting),
                provider_execution_mode="offline_deterministic",
            ),
        )

    assert getattr(exc_info.value, "code", None) == "LLM_OFFLINE_CAPABILITY_UNSUPPORTED"
    assert client.called == 0
    assert session.query(LlmCallAttempt).count() == 0
    assert session.query(LlmCall).one().accounting_status == "rejected"


def _response_with_raw_usage(raw_usage, *, complete: bool) -> LLMResponse:
    return LLMResponse(
        request_id="usage-case",
        provider="openai_compatible",
        model="test-model",
        text='{"scene_text":"some generated text"}',
        structured_output={"scene_text": "some generated text"},
        response_format="json_object",
        raw_response={"usage": raw_usage} if raw_usage is not None else {},
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        raw_usage=raw_usage,
        usage_present=raw_usage is not None,
        usage_complete=complete,
    )


def test_complete_provider_usage_is_actual_and_persisted_without_estimate(session) -> None:
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
                    "id": "actual-usage",
                    "model": "test-model",
                    "output_text": '{"scene_text":"ok"}',
                    "usage": {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
                },
            )
        ),
    )

    accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    session.expire_all()
    parent = session.query(LlmCall).one()
    attempt = session.query(LlmCallAttempt).one()
    assert (attempt.prompt_tokens, attempt.completion_tokens, attempt.total_tokens) == (13, 7, 20)
    assert attempt.usage_is_estimate is False
    assert (parent.prompt_tokens, parent.completion_tokens, parent.total_tokens) == (13, 7, 20)
    assert parent.usage_is_estimate is False


@pytest.mark.parametrize(
    ("raw_usage", "complete"),
    [
        (None, False),
        ({"input_tokens": 9}, False),
        ({"output_tokens": 3}, False),
        ({"input_tokens": 9, "output_tokens": 3, "total_tokens": 99}, False),
        ({"input_tokens": "nine", "output_tokens": 3, "total_tokens": 3}, False),
        ({"input_tokens": -1, "output_tokens": 3, "total_tokens": 2}, False),
    ],
)
def test_missing_partial_inconsistent_or_invalid_usage_falls_back_conservatively(
    raw_usage,
    complete: bool,
) -> None:
    accounting = _accounting_module()

    usage = accounting.normalize_response_usage(
        _response_with_raw_usage(raw_usage, complete=complete),
        _request(),
    )

    assert usage.usage_is_estimate is True
    assert usage.prompt_tokens > 0
    assert usage.completion_tokens > 0
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens


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


@pytest.mark.parametrize(
    ("env_name", "error_code"),
    [
        ("NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT", "LLM_DAILY_TOKEN_LIMIT"),
        ("NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT", "LLM_MONTHLY_TOKEN_LIMIT"),
        ("NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT", "LLM_PROJECT_DAILY_TOKEN_LIMIT"),
    ],
)
def test_global_token_quotas_reject_before_physical_provider_io(
    session,
    monkeypatch,
    env_name: str,
    error_code: str,
) -> None:
    accounting = _accounting_module()
    monkeypatch.setenv(env_name, "1")

    class QuotaClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            raise AssertionError("quota rejection must happen before provider I/O")

    client = QuotaClient()
    with pytest.raises(accounting.LLMAccountingRejected) as exc_info:
        accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    assert exc_info.value.code == error_code
    assert client.physical_posts == 0
    assert session.query(LlmCallAttempt).count() == 0
    parent = session.query(LlmCall).one()
    assert parent.accounting_status == "rejected"
    assert parent.error_code == error_code
    assert parent.budget_charged_tokens == 0


def test_global_quotas_charge_terminal_provider_overage_at_actual_total_tokens(
    session,
    monkeypatch,
) -> None:
    accounting = _accounting_module()
    now = datetime.now(UTC).isoformat()
    parent = LlmCall(
        llm_call_id="provider-overage-for-global-quota",
        scope_type="project",
        scope_id="project-1",
        project_id="project-1",
        node_id="neutral_draft",
        step="draft",
        prompt_tokens=200,
        completion_tokens=50,
        total_tokens=250,
        estimated_tokens=10,
        reserved_tokens=10,
        budget_charged_tokens=10,
        usage_is_estimate=False,
        accounting_status="usage_exceeds_reservation",
        request_dispatched_at=now,
        settled_at=now,
    )
    session.add(parent)
    session.add(
        LlmCallAttempt(
            attempt_id="provider-overage-attempt-for-global-quota",
            llm_call_id=parent.llm_call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=50,
            prompt_tokens=200,
            completion_tokens=50,
            total_tokens=250,
            estimated_tokens=10,
            reserved_tokens=10,
            budget_charged_tokens=10,
            usage_is_estimate=False,
            accounting_status="usage_exceeds_reservation",
            request_dispatched_at=now,
            settled_at=now,
        )
    )
    session.commit()
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT", "250")

    snapshot = accounting.llm_quota_snapshot(session, project_id="project-1")

    assert snapshot["daily_tokens"]["used"] == 250
    assert snapshot["monthly_tokens"]["used"] == 250
    assert snapshot["project_daily_tokens"]["used"] == 250
    assert parent.budget_charged_tokens == 10  # scene-budget caliber remains bounded

    class QuotaClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            raise AssertionError("actual provider usage must reject before provider I/O")

    client = QuotaClient()
    with pytest.raises(accounting.LLMAccountingRejected) as exc_info:
        accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    assert exc_info.value.code == "LLM_DAILY_TOKEN_LIMIT"
    assert exc_info.value.details["used"] == 250
    assert client.physical_posts == 0


def test_daily_request_quota_counts_dispatched_attempts_and_rejects_next_call(
    session,
    monkeypatch,
) -> None:
    accounting = _accounting_module()
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT", "1")

    class SuccessfulClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            response = LLMResponse(
                request_id=f"quota-{self.physical_posts}",
                provider="fake",
                model=request.model,
                text="{}",
                structured_output={},
                response_format="json_object",
                raw_response={"id": f"quota-{self.physical_posts}"},
                usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                raw_usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                usage_present=True,
                usage_complete=True,
                finish_reason="stop",
            )
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
            return response

    client = SuccessfulClient()
    accounting.execute_accounted_call(session, client, _request(), _context(accounting))
    second_context = replace(
        _context(accounting),
        execution_id="execution-2",
        execution_step_key="neutral_draft-2",
    )
    with pytest.raises(accounting.LLMAccountingRejected) as exc_info:
        accounting.execute_accounted_call(session, client, _request(), second_context)

    assert exc_info.value.code == "LLM_DAILY_REQUEST_LIMIT"
    assert client.physical_posts == 1
    assert session.query(LlmCallAttempt).count() == 1
    assert session.query(LlmCall).filter_by(accounting_status="rejected").count() == 1


def test_global_concurrency_quota_counts_open_reservations(session, monkeypatch) -> None:
    accounting = _accounting_module()
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS", "1")
    parent = LlmCall(
        llm_call_id="existing-open-call",
        scope_type="project",
        scope_id="other-project",
        node_id="neutral_draft",
        step="draft",
        project_id="other-project",
        estimated_tokens=100,
        reserved_tokens=100,
        budget_charged_tokens=0,
        accounting_status="reserved",
    )
    session.add(parent)
    session.add(
        LlmCallAttempt(
            attempt_id="existing-open-attempt",
            llm_call_id=parent.llm_call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=64,
            estimated_tokens=100,
            reserved_tokens=100,
            budget_charged_tokens=0,
            accounting_status="reserved",
        )
    )
    session.commit()

    class QuotaClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            raise AssertionError("concurrency gate must reject before provider I/O")

    client = QuotaClient()
    with pytest.raises(accounting.LLMAccountingRejected) as exc_info:
        accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    assert exc_info.value.code == "LLM_GLOBAL_CONCURRENCY_LIMIT"
    assert client.physical_posts == 0
    assert session.query(LlmCallAttempt).count() == 1


def test_daily_money_quota_requires_prices_and_rejects_conservatively(session, monkeypatch) -> None:
    accounting = _accounting_module()
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD", "0.000001")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_INPUT_COST_PER_MILLION_USD", "10")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_OUTPUT_COST_PER_MILLION_USD", "20")

    class QuotaClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            raise AssertionError("money gate must reject before provider I/O")

    client = QuotaClient()
    with pytest.raises(accounting.LLMAccountingRejected) as exc_info:
        accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    assert exc_info.value.code == "LLM_DAILY_COST_LIMIT"
    assert client.physical_posts == 0
    assert session.query(LlmCallAttempt).count() == 0


def test_global_fences_ship_disarmed_and_never_reject_a_default_install(
    session,
    monkeypatch,
) -> None:
    """A default install arms no fence: a heavy ledger still dispatches."""

    accounting = _accounting_module()
    for env_name in (
        "NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT",
        "NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT",
        "NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT",
        "NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT",
        "NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS",
        "NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD",
    ):
        monkeypatch.delenv(env_name, raising=False)
    now = datetime.now(UTC).isoformat()
    # Spend that would have tripped every fence this build used to ship with
    # (1M daily / 20M monthly / 250K project-daily tokens).
    spent = LlmCall(
        llm_call_id="already-spent-past-every-legacy-fence",
        scope_type="project",
        scope_id="project-1",
        project_id="project-1",
        node_id="neutral_draft",
        step="draft",
        prompt_tokens=4_000_000,
        completion_tokens=1_000_000,
        total_tokens=5_000_000,
        estimated_tokens=5_000_000,
        reserved_tokens=5_000_000,
        budget_charged_tokens=5_000_000,
        usage_is_estimate=False,
        accounting_status="settled",
        request_dispatched_at=now,
        settled_at=now,
    )
    session.add(spent)
    session.add(
        LlmCallAttempt(
            attempt_id="already-spent-attempt",
            llm_call_id=spent.llm_call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=64,
            prompt_tokens=4_000_000,
            completion_tokens=1_000_000,
            total_tokens=5_000_000,
            estimated_tokens=5_000_000,
            reserved_tokens=5_000_000,
            budget_charged_tokens=5_000_000,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at=now,
            settled_at=now,
        )
    )
    session.commit()

    class SuccessfulClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            response = LLMResponse(
                request_id="unfenced-call",
                provider="fake",
                model=request.model,
                text="{}",
                structured_output={},
                response_format="json_object",
                raw_response={"id": "unfenced-call"},
                usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                raw_usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                usage_present=True,
                usage_complete=True,
                finish_reason="stop",
            )
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
            return response

    client = SuccessfulClient()
    accounting.execute_accounted_call(session, client, _request(), _context(accounting))

    assert client.physical_posts == 1
    snapshot = accounting.llm_quota_snapshot(session, project_id="project-1")
    assert snapshot["any_enforced"] is False
    for meter_key in (
        "daily_tokens",
        "monthly_tokens",
        "project_daily_tokens",
        "daily_requests",
        "concurrent_requests",
        "daily_cost_usd",
    ):
        assert snapshot[meter_key]["limit"] is None, meter_key
        assert snapshot[meter_key]["enforced"] is False, meter_key
    # Disarming the fences must not stop the ledger: the dashboard still reads
    # real consumption, it just has no ceiling to plot it against.
    assert snapshot["daily_tokens"]["used"] == 5_000_005
    assert snapshot["monthly_tokens"]["used"] == 5_000_005
    assert snapshot["project_daily_tokens"]["used"] == 5_000_005
    assert snapshot["daily_requests"]["used"] == 2


@pytest.mark.parametrize(
    ("env_name", "settings_attr"),
    [
        ("NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT", "llm_daily_token_limit"),
        ("NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT", "llm_monthly_token_limit"),
        ("NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT", "llm_project_daily_token_limit"),
        ("NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT", "llm_daily_request_limit"),
        ("NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS", "llm_max_concurrent_requests"),
    ],
)
def test_quota_env_accepts_zero_as_disabled_and_still_rejects_negative(
    monkeypatch,
    env_name: str,
    settings_attr: str,
) -> None:
    """``0`` is the documented "no ceiling" value; a negative stays a config error.

    The disarmed-install test above exercises the dataclass default, so without
    this the parser swap (`_get_positive_int_env` -> `_get_quota_int_env`) has no
    coverage at all — and `_get_positive_int_env` rejects the very ``0`` that
    docs/runtime-safety.md tells the operator to set.
    """

    from novel_system.settings import get_settings

    monkeypatch.setenv(env_name, "0")
    assert getattr(get_settings(include_runtime_config=False), settings_attr) == 0

    monkeypatch.setenv(env_name, "7")
    assert getattr(get_settings(include_runtime_config=False), settings_attr) == 7

    monkeypatch.setenv(env_name, "-1")
    with pytest.raises(ValueError, match=env_name):
        get_settings(include_runtime_config=False)


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


class _SimulatedProcessCrash(BaseException):
    pass


def test_recovery_releases_reserved_but_undispatched_attempt_and_allows_retry(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-reservation-crash"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    cached_state = session.get(SceneRunState, scene_id)
    post_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        return httpx.Response(
            200,
            json={
                "id": "after-recovery",
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

    def crash_after_reservation(stage: str, _attempt_id: str) -> None:
        if stage == "reservation_committed":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="reservation-crash-call",
            _lifecycle_observer=crash_after_reservation,
        )

    assert cached_state.scene_tokens_reserved > 0
    assert cached_state.provider_attempts_used == 0
    assert cached_state.scene_tokens_used == 0
    session.expire_all()
    attempt = session.query(LlmCallAttempt).one()
    assert post_count == 0
    assert attempt.accounting_status == "reserved"
    assert attempt.request_dispatched_at is None
    assert session.get(SceneRunState, scene_id).scene_tokens_reserved == attempt.reserved_tokens
    cached_state = session.get(SceneRunState, scene_id)

    result = accounting.recover_incomplete_call(session, "reservation-crash-call")

    assert cached_state.scene_tokens_reserved == 0
    assert cached_state.provider_attempts_used == 0
    assert cached_state.scene_tokens_used == 0
    session.expire_all()
    assert result.status == "released"
    assert result.error_code is None
    assert result.may_retry is True
    assert session.get(LlmCall, "reservation-crash-call").accounting_status == "released"
    assert session.query(LlmCallAttempt).one().accounting_status == "released"
    assert session.get(SceneRunState, scene_id).scene_tokens_reserved == 0
    assert session.get(SceneRunState, scene_id).provider_attempts_used == 0
    accounting._release_scene_reservation(session, scene_id, attempt.reserved_tokens)

    accounting.execute_accounted_call(
        session,
        client,
        _request(),
        _scene_context(accounting, scene_id),
        llm_call_id="reservation-crash-retry",
    )
    assert post_count == 1


def test_recovery_charges_dispatched_unknown_attempt_and_same_call_is_not_resent(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-dispatch-crash"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    cached_state = session.get(SceneRunState, scene_id)
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

    def crash_after_dispatch(stage: str, _attempt_id: str) -> None:
        if stage == "dispatch_committed":
            raise _SimulatedProcessCrash()

    with pytest.raises(_SimulatedProcessCrash):
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="dispatch-crash-call",
            _lifecycle_observer=crash_after_dispatch,
        )

    assert cached_state.scene_tokens_reserved > 0
    assert cached_state.provider_attempts_used == 1
    assert cached_state.scene_tokens_used == 0
    session.expire_all()
    attempt = session.query(LlmCallAttempt).one()
    assert post_count == 0
    assert attempt.accounting_status == "reserved"
    assert attempt.request_dispatched_at is not None
    assert session.get(SceneRunState, scene_id).provider_attempts_used == 1
    cached_state = session.get(SceneRunState, scene_id)

    result = accounting.recover_incomplete_call(session, "dispatch-crash-call")

    assert cached_state.scene_tokens_reserved == 0
    assert cached_state.provider_attempts_used == 1
    assert cached_state.scene_tokens_used > 0
    session.expire_all()
    parent = session.get(LlmCall, "dispatch-crash-call")
    attempt = session.query(LlmCallAttempt).one()
    run_state = session.get(SceneRunState, scene_id)
    assert result.status == "failed"
    assert result.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert result.may_retry is False
    assert parent.accounting_status == "failed"
    assert parent.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert attempt.accounting_status == "failed"
    assert attempt.budget_charged_tokens == attempt.estimated_tokens > 0
    assert parent.budget_charged_tokens == attempt.budget_charged_tokens
    used_after_recovery = run_state.scene_tokens_used
    with pytest.raises(Exception) as repeated_recovery:
        accounting.recover_incomplete_call(session, "dispatch-crash-call")
    assert getattr(repeated_recovery.value, "code", None) == "LLM_ACCOUNTING_CALL_NOT_RECOVERABLE"
    session.expire_all()
    assert session.get(SceneRunState, scene_id).scene_tokens_used == used_after_recovery
    run_state = session.get(SceneRunState, scene_id)
    assert run_state.scene_tokens_reserved == 0
    assert run_state.scene_tokens_used == attempt.budget_charged_tokens

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="dispatch-crash-new-call-same-execution-step",
        )
    assert getattr(exc_info.value, "code", None) == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert post_count == 0


def test_release_idempotence_does_not_hide_a_different_nonzero_fence(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-release-fence-conflict"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=10_000,
            scene_tokens_reserved=321,
            provider_attempt_budget=5,
        )
    )
    session.commit()

    with pytest.raises(Exception) as conflict:
        accounting._release_scene_reservation(session, scene_id, 123)
    assert getattr(conflict.value, "code", None) == "LLM_ACCOUNTING_SCENE_RESERVATION_CORRUPT"
    session.rollback()
    assert session.get(SceneRunState, scene_id).scene_tokens_reserved == 321


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


def test_usage_over_reservation_charges_actual_to_scene_and_blocks_later_dispatch(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-usage-over-reservation"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=50_000,
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
                "id": "over-reservation",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10_000, "output_tokens": 10_000, "total_tokens": 20_000},
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
    first_context = _scene_context(accounting, scene_id)

    with pytest.raises(Exception) as overage_error:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            first_context,
            llm_call_id="overage-call",
        )
    assert getattr(overage_error.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"

    session.expire_all()
    parent = session.get(LlmCall, "overage-call")
    attempt = session.query(LlmCallAttempt).one()
    run_state = session.get(SceneRunState, scene_id)
    request_reservation = accounting.estimate_request_usage(_request()).reserved_tokens
    assert parent.accounting_status == "usage_exceeds_reservation"
    assert attempt.accounting_status == "usage_exceeds_reservation"
    assert attempt.reserved_tokens == request_reservation
    assert attempt.total_tokens == 20_000
    assert attempt.budget_charged_tokens == attempt.reserved_tokens < attempt.total_tokens
    assert parent.response_payload_summary["usage_overage_tokens"] == (
        attempt.total_tokens - attempt.reserved_tokens
    )
    assert overage_error.value.details == {
        "llm_call_id": "overage-call",
        "execution_id": first_context.execution_id,
        "execution_step_key": first_context.execution_step_key,
        "actual_tokens": parent.total_tokens,
        "reserved_tokens": parent.reserved_tokens,
        "attempt_id": attempt.attempt_id,
        "provider_attempt_no": attempt.provider_attempt_no,
        "attempt_actual_tokens": attempt.total_tokens,
        "attempt_reserved_tokens": attempt.reserved_tokens,
        "usage_overage_tokens": attempt.total_tokens - attempt.reserved_tokens,
        "parent_actual_tokens": parent.total_tokens,
        "parent_reserved_tokens": parent.reserved_tokens,
    }
    assert run_state.scene_tokens_used == 20_000

    accounting.mark_postprocess_failure(
        session,
        "overage-call",
        error_code="CALLER_SCHEMA_INVALID",
        error_text="scene_text failed caller validation",
    )
    session.expire_all()
    parent = session.get(LlmCall, "overage-call")
    assert parent.accounting_status == "usage_exceeds_reservation"
    assert parent.error_code == "CALLER_SCHEMA_INVALID"
    assert parent.response_payload_summary["postprocess_error"]["kind"] == "text_fingerprint"
    assert parent.response_payload_summary["postprocess_error"]["char_count"] == len(
        "scene_text failed caller validation"
    )

    second_context = replace(first_context, execution_id="execution-2")
    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            second_context,
            llm_call_id="blocked-after-overage",
        )
    assert getattr(exc_info.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"
    assert post_count == 1


def test_known_usage_overage_past_budget_still_blocks_later_dispatch_with_stable_code(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-known-overage-past-budget"
    request = _request()
    reservation = accounting.estimate_request_usage(request).reserved_tokens
    budget = 50_000
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=budget,
            scene_tokens_used=budget - reservation,
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
                "id": "known-overage-past-budget",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 10_000, "output_tokens": 10_000, "total_tokens": 20_000},
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
    first_context = _scene_context(accounting, scene_id)
    with pytest.raises(Exception) as first_error:
        accounting.execute_accounted_call(
            session,
            client,
            request,
            first_context,
            llm_call_id="known-overage-past-budget-first",
        )
    assert getattr(first_error.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"
    session.expire_all()
    assert session.get(SceneRunState, scene_id).scene_tokens_used > budget

    with pytest.raises(Exception) as blocked:
        accounting.execute_accounted_call(
            session,
            client,
            request,
            replace(first_context, execution_id="known-overage-second-execution"),
            llm_call_id="known-overage-past-budget-second",
        )
    assert getattr(blocked.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"
    assert post_count == 1

    # A tombstone must never mask a still-live/different reservation fence.
    from novel_system.services.scene_budget import ensure_scene_budget_initialized

    run_state = session.get(SceneRunState, scene_id)
    run_state.scene_tokens_reserved = 1
    session.commit()
    with pytest.raises(ValueError, match="scene budget state is corrupt"):
        ensure_scene_budget_initialized(session, scene_id)


def test_success_overage_details_identify_offending_attempt_after_retry(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-retry-success-overage"
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
        if post_count == 1:
            return httpx.Response(
                500,
                json={
                    "id": "first-failed-attempt",
                    "error": {"message": "retryable provider failure"},
                    "usage": {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "second-overage-attempt",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                "usage": {"input_tokens": 500, "output_tokens": 495, "total_tokens": 995},
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
    context = _scene_context(accounting, scene_id)

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            context,
            llm_call_id="retry-success-overage",
        )

    session.expire_all()
    parent = session.get(LlmCall, "retry-success-overage")
    attempts = session.query(LlmCallAttempt).order_by(LlmCallAttempt.provider_attempt_no).all()
    offending_attempt = attempts[1]
    assert [attempt.accounting_status for attempt in attempts] == [
        "failed",
        "usage_exceeds_reservation",
    ]
    assert parent.total_tokens == 1_005
    assert parent.reserved_tokens == sum(attempt.reserved_tokens for attempt in attempts)
    assert getattr(exc_info.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"
    assert exc_info.value.details == {
        "llm_call_id": "retry-success-overage",
        "execution_id": context.execution_id,
        "execution_step_key": context.execution_step_key,
        "actual_tokens": 995,
        "reserved_tokens": offending_attempt.reserved_tokens,
        "attempt_id": offending_attempt.attempt_id,
        "provider_attempt_no": 1,
        "attempt_actual_tokens": 995,
        "attempt_reserved_tokens": offending_attempt.reserved_tokens,
        "usage_overage_tokens": offending_attempt.total_tokens - offending_attempt.reserved_tokens,
        "parent_actual_tokens": 1_005,
        "parent_reserved_tokens": parent.reserved_tokens,
    }


def test_failed_provider_response_with_overage_keeps_parent_child_audit_consistent(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-failed-overage"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=1_000,
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
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                500,
                json={
                    "id": "failed-overage",
                    "error": {"message": "provider failed after consuming tokens"},
                    "usage": {"input_tokens": 12_000, "output_tokens": 8_000, "total_tokens": 20_000},
                },
            )
        ),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="failed-overage-call",
        )
    assert getattr(exc_info.value, "code", None) == "LLM_HTTP_RETRYABLE_FAILURE"

    session.expire_all()
    parent = session.get(LlmCall, "failed-overage-call")
    attempt = session.query(LlmCallAttempt).one()
    run_state = session.get(SceneRunState, scene_id)
    assert attempt.accounting_status == "usage_exceeds_reservation"
    assert parent.accounting_status == "usage_exceeds_reservation"
    assert parent.error_code == "LLM_HTTP_RETRYABLE_FAILURE"
    assert parent.total_tokens == attempt.total_tokens == 20_000
    assert parent.response_payload_summary["usage_overage_tokens"] == (
        attempt.total_tokens - attempt.reserved_tokens
    )
    assert run_state.scene_tokens_used == 20_000
    assert run_state.run_execution_status == "usage_exceeds_reservation"


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


# ---------- 2026-09-15：预留额被超出只在「有栅栏武装」时拦截响应 ----------
#
# 真实故障：参考书导入的段落分类节点路由到 gemini-*-high 类中转，中转把思考 token 计入
# completion_tokens 且不受 max_tokens 封顶——2000 上限的分类应答回报 11776 个 completion
# token，预留额（输入 UTF-8 上界 + max_output_tokens）被超出，整本导入在这一批 500。
# 预留额是派发前配额闸门的输入，不是供应商的承诺；没有任何栅栏武装时，超出只是估算失准，
# 已付费的响应必须照常交付并按真实用量落账（超出量留在审计摘要）。武装态行为逐字节不变。


def _overage_transport(post_counter: list[int]) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        post_counter[0] += 1
        return httpx.Response(
            200,
            json={
                "id": "thinking-relay",
                "model": "test-model",
                "output_text": '{"scene_text":"ok"}',
                # 思考型中转：输出 token 远超 max_output_tokens，也远超预留额
                "usage": {"input_tokens": 10_000, "output_tokens": 10_000, "total_tokens": 20_000},
            },
        )

    return httpx.MockTransport(handler)


def _overage_client(post_counter: list[int]) -> LLMClient:
    return LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=_overage_transport(post_counter),
    )


def test_unfenced_usage_overage_delivers_response_and_settles_at_actual_usage(session) -> None:
    accounting = _accounting_module()
    posts = [0]
    request = _request()
    reservation = accounting.estimate_request_usage(request).reserved_tokens
    assert reservation < 20_000

    response = accounting.execute_accounted_call(
        session,
        _overage_client(posts),
        request,
        _context(accounting),
        llm_call_id="unfenced-overage",
    )

    assert posts == [1]
    assert response.llm_call_id == "unfenced-overage"
    assert response.text == '{"scene_text":"ok"}'
    session.expire_all()
    parent = session.get(LlmCall, "unfenced-overage")
    attempt = session.query(LlmCallAttempt).one()
    assert parent.accounting_status == "settled"
    assert attempt.accounting_status == "settled"
    assert parent.error_code is None
    # 账本按真实用量记录；场景口径的 budget_charged 仍受预留额封顶（不变量不变）。
    assert attempt.total_tokens == parent.total_tokens == 20_000
    assert attempt.reserved_tokens == reservation
    assert attempt.budget_charged_tokens == reservation < attempt.total_tokens
    assert parent.response_payload_summary["usage_overage_tokens"] == 20_000 - reservation
    # 交付后的调用方解析失败照旧是 failed，不会追溯成「超预留」。
    accounting.mark_postprocess_failure(
        session,
        "unfenced-overage",
        error_code="CALLER_SCHEMA_INVALID",
        error_text="scene_text failed caller validation",
    )
    session.expire_all()
    parent = session.get(LlmCall, "unfenced-overage")
    assert parent.accounting_status == "failed"
    assert parent.error_code == "CALLER_SCHEMA_INVALID"
    assert parent.response_payload_summary["usage_overage_tokens"] == 20_000 - reservation


def test_armed_global_fence_keeps_usage_overage_blocking(session, monkeypatch) -> None:
    accounting = _accounting_module()
    posts = [0]
    # 任一全局配额武装 → 预留额是被真实检查过的数字，超出仍然拦截（历史契约）。
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT", "1000000")

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            _overage_client(posts),
            _request(),
            _context(accounting),
            llm_call_id="fenced-overage",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"
    assert posts == [1]
    session.expire_all()
    parent = session.get(LlmCall, "fenced-overage")
    attempt = session.query(LlmCallAttempt).one()
    assert parent.accounting_status == "usage_exceeds_reservation"
    assert attempt.accounting_status == "usage_exceeds_reservation"
    assert exc_info.value.details["usage_overage_tokens"] == 20_000 - attempt.reserved_tokens


def test_disarmed_scene_usage_overage_delivers_and_keeps_scene_dispatchable(
    session, monkeypatch
) -> None:
    from novel_system.services.scene_budget import (
        ensure_scene_budget_initialized,
        is_scene_budget_disarmed,
    )

    accounting = _accounting_module()
    scene_id = "scene-disarmed-overage"
    # 单作者默认：场景 token 预算解除武装（哨兵额度）。
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    _seed_scene_parent(session, scene_id)
    session.commit()
    state = ensure_scene_budget_initialized(session, scene_id)
    assert is_scene_budget_disarmed(state) is True
    posts = [0]
    client = _overage_client(posts)
    first_context = _scene_context(accounting, scene_id)

    first = accounting.execute_accounted_call(
        session, client, _request(), first_context, llm_call_id="disarmed-overage-1"
    )
    second = accounting.execute_accounted_call(
        session,
        client,
        _request(),
        replace(first_context, execution_id="execution-2"),
        llm_call_id="disarmed-overage-2",
    )

    assert first.llm_call_id == "disarmed-overage-1"
    assert second.llm_call_id == "disarmed-overage-2"
    assert posts == [2]
    session.expire_all()
    run_state = session.get(SceneRunState, scene_id)
    # 场景没有被打上 usage_exceeds_reservation 的运行状态，下一节点照常派发；记账照旧累计。
    assert run_state.run_execution_status is None
    assert run_state.scene_tokens_reserved == 0
    assert run_state.scene_tokens_used == 40_000
    for call_id in ("disarmed-overage-1", "disarmed-overage-2"):
        parent = session.get(LlmCall, call_id)
        assert parent.accounting_status == "settled"
        assert parent.total_tokens == 20_000
        assert parent.response_payload_summary["usage_overage_tokens"] > 0
    assert {
        attempt.accounting_status for attempt in session.query(LlmCallAttempt).all()
    } == {"settled"}


def test_armed_scene_budget_keeps_usage_overage_blocking(session) -> None:
    accounting = _accounting_module()
    scene_id = "scene-armed-overage"
    session.add(
        _scene_run_state(
            session,
            scene_id=scene_id,
            scene_token_budget=50_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()
    posts = [0]

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            _overage_client(posts),
            _request(),
            _scene_context(accounting, scene_id),
            llm_call_id="armed-scene-overage",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_USAGE_EXCEEDS_RESERVATION"
    session.expire_all()
    run_state = session.get(SceneRunState, scene_id)
    assert run_state.run_execution_status == "usage_exceeds_reservation"
    assert session.get(LlmCall, "armed-scene-overage").accounting_status == (
        "usage_exceeds_reservation"
    )


def test_unfenced_failed_provider_response_with_overage_settles_as_failed(session) -> None:
    accounting = _accounting_module()
    client = LLMClient(
        provider="openai_compatible",
        base_url="https://example.test/v1",
        api_key="test-key",
        timeout_seconds=5,
        max_retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                500,
                json={
                    "id": "failed-unfenced-overage",
                    "error": {"message": "provider failed after consuming tokens"},
                    "usage": {"input_tokens": 12_000, "output_tokens": 8_000, "total_tokens": 20_000},
                },
            )
        ),
    )

    with pytest.raises(Exception) as exc_info:
        accounting.execute_accounted_call(
            session,
            client,
            _request(),
            _context(accounting),
            llm_call_id="failed-unfenced-overage",
        )

    assert getattr(exc_info.value, "code", None) == "LLM_HTTP_RETRYABLE_FAILURE"
    session.expire_all()
    parent = session.get(LlmCall, "failed-unfenced-overage")
    attempt = session.query(LlmCallAttempt).one()
    # 失败仍是失败；超出量只是审计数据，不升级成会阻断后续派发的「超预留」状态。
    assert attempt.accounting_status == "failed"
    assert parent.accounting_status == "failed"
    assert parent.error_code == "LLM_HTTP_RETRYABLE_FAILURE"
    assert parent.total_tokens == attempt.total_tokens == 20_000
    assert parent.response_payload_summary["usage_overage_tokens"] == (
        attempt.total_tokens - attempt.reserved_tokens
    )
