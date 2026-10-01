"""启动对账（``llm_accounting_recovery``）：进程在派发前崩溃的预留放掉、可以重试；派发出去但结果未知的照实收费、
同一个调用不再重发；释放幂等不会掩盖另一笔非零的栅栏占用。"""

from __future__ import annotations

import httpx
import pytest

from novel_system.db.models import (
    LlmCall,
    LlmCallAttempt,
    SceneRunState,
)
from novel_system.services import llm_scene_fence
from novel_system.services.llm_client import LLMClient
from tests.support.accounting import (
    accounting_module as _accounting_module,
    accounting_request as _request,
    scene_call_context as _scene_context,
    scene_run_state as _scene_run_state,
)


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
    llm_scene_fence.release_scene_reservation(session, scene_id, attempt.reserved_tokens)

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
        llm_scene_fence.release_scene_reservation(session, scene_id, 123)
    assert getattr(conflict.value, "code", None) == "LLM_ACCOUNTING_SCENE_RESERVATION_CORRUPT"
    session.rollback()
    assert session.get(SceneRunState, scene_id).scene_tokens_reserved == 321
