"""用量超出预留额：有栅栏武装（场景预算）时按实收费并拦住后续派发、超额细节指出是哪一次尝试；没有武装时照实结算、
回包照常交付（2026-09-15，思考型中转把推理 token 算进输出、又不受上限约束）。"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from novel_system.db.models import (
    LlmCall,
    LlmCallAttempt,
    SceneRunState,
)
from novel_system.services.llm_client import LLMClient
from tests.support.accounting import (
    RETIRED_QUOTA_ENV,
    accounting_module as _accounting_module,
    accounting_request as _request,
    project_call_context as _context,
    scene_call_context as _scene_context,
    scene_run_state as _scene_run_state,
    seed_scene_parent as _seed_scene_parent,
)


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


def test_retired_quota_env_vars_do_not_make_a_usage_overage_blocking(session, monkeypatch) -> None:
    """R3 复核补充 3:超预留是否拦截只看场景预算有没有武装;退役的全局额度变量设着也照常交付、按实际用量结算。"""
    accounting = _accounting_module()
    posts = [0]
    for env_name, value in RETIRED_QUOTA_ENV.items():
        monkeypatch.setenv(env_name, value)

    response = accounting.execute_accounted_call(
        session,
        _overage_client(posts),
        _request(),
        _context(accounting),
        llm_call_id="retired-fence-overage",
    )

    assert posts == [1]
    assert response.llm_call_id == "retired-fence-overage"
    session.expire_all()
    parent = session.get(LlmCall, "retired-fence-overage")
    attempt = session.query(LlmCallAttempt).one()
    assert parent.accounting_status == "settled"
    assert attempt.accounting_status == "settled"
    assert parent.total_tokens == attempt.total_tokens == 20_000
    assert parent.response_payload_summary["usage_overage_tokens"] == 20_000 - attempt.reserved_tokens


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
