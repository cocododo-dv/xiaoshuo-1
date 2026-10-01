"""记账里的用量：预留额怎么估（消息开销、输出上限、UTF-8 上界、线上 schema）、供应商报的用量什么时候算实数、缺了 /
不全 / 对不上时怎么保守估；用量读数（今日 / 本月 / 并发）与退役的额度环境变量不拦任何调用。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from novel_system.db.models import (
    LlmCall,
    LlmCallAttempt,
)
from novel_system.services.llm_client import LLMClient, LLMRequest, LLMResponse
from tests.support.accounting import (
    RETIRED_QUOTA_ENV,
    accounting_module as _accounting_module,
    accounting_request as _request,
    project_call_context as _context,
)


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

READING_KEYS = ("daily_tokens", "monthly_tokens", "project_daily_tokens", "daily_requests", "concurrent_requests")


def _settled_call(call_id: str, *, project_id: str, total_tokens: int, reserved_tokens: int, status: str = "settled"):
    now = datetime.now(UTC).isoformat()
    completion_tokens = min(50, total_tokens)
    parent = LlmCall(
        llm_call_id=call_id,
        scope_type="project",
        scope_id=project_id,
        project_id=project_id,
        node_id="neutral_draft",
        step="draft",
        prompt_tokens=total_tokens - completion_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_tokens=min(total_tokens, reserved_tokens),
        reserved_tokens=reserved_tokens,
        budget_charged_tokens=min(total_tokens, reserved_tokens),
        usage_is_estimate=False,
        accounting_status=status,
        request_dispatched_at=now,
        settled_at=now,
    )
    attempt = LlmCallAttempt(
        attempt_id=f"{call_id}-attempt",
        llm_call_id=call_id,
        provider_attempt_no=0,
        dispatch_kind="initial",
        request_max_output_tokens=50,
        prompt_tokens=total_tokens - completion_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_tokens=min(total_tokens, reserved_tokens),
        reserved_tokens=reserved_tokens,
        budget_charged_tokens=min(total_tokens, reserved_tokens),
        usage_is_estimate=False,
        accounting_status=status,
        request_dispatched_at=now,
        settled_at=now,
    )
    return parent, attempt


def _successful_client(accounting):
    class SuccessfulClient(accounting.OnlineAccountedExecution):
        physical_posts = 0

        def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            self.physical_posts += 1
            response = LLMResponse(
                request_id=f"unfenced-{self.physical_posts}",
                provider="fake",
                model=request.model,
                text="{}",
                structured_output={},
                response_format="json_object",
                raw_response={"id": f"unfenced-{self.physical_posts}"},
                usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                raw_usage={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                usage_present=True,
                usage_complete=True,
                finish_reason="stop",
            )
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
            return response

    return SuccessfulClient()


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


def test_retired_quota_env_vars_reject_nothing_and_readings_carry_no_limit(session, monkeypatch) -> None:
    """R3:额度变量还设着(连无效值、只设金额上限不设单价)也什么都不拦;读数没有上限、也没有金额一格。"""
    from novel_system.services.llm_usage_readings import usage_readings
    from novel_system.settings import get_settings

    accounting = _accounting_module()
    for env_name, value in RETIRED_QUOTA_ENV.items():
        monkeypatch.setenv(env_name, value)
    get_settings()  # 以前:金额上限没有单价 → ValueError,后端起不来
    spent, spent_attempt = _settled_call("spent-past-every-old-fence", project_id="project-1", total_tokens=5_000_000, reserved_tokens=5_000_000)
    session.add_all([spent, spent_attempt])
    session.commit()

    client = _successful_client(accounting)
    accounting.execute_accounted_call(session, client, _request(), _context(accounting))
    accounting.execute_accounted_call(
        session,
        client,
        _request(),
        replace(_context(accounting), execution_id="execution-2", execution_step_key="neutral_draft-2"),
    )

    assert client.physical_posts == 2
    assert session.query(LlmCall).filter_by(accounting_status="rejected").count() == 0
    readings = usage_readings(session, project_id="project-1")
    assert set(readings) == {"period_timezone", *READING_KEYS}
    for key in READING_KEYS:
        assert readings[key]["limit"] is None, key
        assert readings[key]["enforced"] is False, key
    # 账本照旧:看板仍有真实用量读数,只是没有上限可比
    assert readings["daily_tokens"]["used"] == 5_000_010
    assert readings["monthly_tokens"]["used"] == 5_000_010
    assert readings["project_daily_tokens"] == {"project_id": "project-1", "used": 5_000_010, "limit": None, "enforced": False}
    assert readings["daily_requests"]["used"] == 3
    assert readings["concurrent_requests"]["used"] == 0


def test_usage_readings_count_actual_usage_and_show_open_calls_only_as_concurrent(session) -> None:
    """用量按供应商实报的完整用量算(超出预留的部分也算);还在飞的调用只计「并发」,不预支进今日总量。"""
    from novel_system.services.llm_usage_readings import usage_readings

    parent, attempt = _settled_call(
        "provider-overage", project_id="project-1", total_tokens=250, reserved_tokens=10, status="usage_exceeds_reservation"
    )
    other, other_attempt = _settled_call("other-project-call", project_id="project-2", total_tokens=40, reserved_tokens=40)
    open_parent = LlmCall(
        llm_call_id="in-flight-call",
        scope_type="project",
        scope_id="project-1",
        node_id="neutral_draft",
        step="draft",
        project_id="project-1",
        estimated_tokens=100,
        reserved_tokens=100,
        budget_charged_tokens=0,
        accounting_status="reserved",
    )
    open_attempt = LlmCallAttempt(
        attempt_id="in-flight-attempt",
        llm_call_id="in-flight-call",
        provider_attempt_no=0,
        dispatch_kind="initial",
        request_max_output_tokens=64,
        estimated_tokens=100,
        reserved_tokens=100,
        budget_charged_tokens=0,
        accounting_status="reserved",
        request_dispatched_at=datetime.now(UTC).isoformat(),
    )
    session.add_all([parent, attempt, other, other_attempt, open_parent, open_attempt])
    session.commit()

    readings = usage_readings(session, project_id="project-1")

    assert readings["daily_tokens"]["used"] == 290
    assert readings["monthly_tokens"]["used"] == 290
    assert readings["project_daily_tokens"]["used"] == 250
    assert readings["daily_requests"]["used"] == 2
    assert readings["concurrent_requests"]["used"] == 1
    assert parent.budget_charged_tokens == 10  # 场景预算口径仍受预留额封顶


def test_usage_readings_window_starts_at_the_utc_day_and_month(session) -> None:
    from novel_system.services.llm_usage_readings import usage_readings

    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).isoformat()
    parent, attempt = _settled_call("yesterday-call", project_id="project-1", total_tokens=70, reserved_tokens=70)
    attempt.created_at = yesterday
    parent.created_at = yesterday
    session.add_all([parent, attempt])
    session.commit()

    readings = usage_readings(session, project_id="project-1")

    assert readings["daily_tokens"]["used"] == 0
    assert readings["daily_requests"]["used"] == 0
    assert readings["monthly_tokens"]["used"] == (70 if now.day > 1 else 0)
    assert readings["period_timezone"] == "UTC"
