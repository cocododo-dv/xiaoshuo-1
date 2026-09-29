"""「测试连接」的最小生成探测：一次原始 POST，照样过物理尝试账本（从 llm_accounting 拆出，B09-12 / B09-11）。

探测不走 ``LLMClient``（各家探测载荷由适配器单独构造），但仍然是一次真实的供应商调用：父行 + 物理尝试行
与正式调用同样的不变量，结果只作诊断，账本才是权威。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall
from novel_system.services.llm_accounting import execute_accounted_call
from novel_system.services.llm_accounting_types import LLMCallContext, elapsed_ms, llm_failure_code
from novel_system.services.llm_audit import sanitize_audit_summary
from novel_system.services.llm_providers.base import (
    LLMAttemptHook,
    LLMClientError,
    LLMHTTPError,
    LLMRequest,
    LLMResponse,
    OnlineAccountedExecution,
)
from novel_system.services.llm_providers.usage import extract_raw_usage, normalize_raw_usage


@dataclass(frozen=True, slots=True)
class AccountedCompletionProbeResult:
    llm_call_id: str
    response: httpx.Response | None
    error_code: str | None
    error_message: str | None


class _AccountedCompletionProbeExecution(OnlineAccountedExecution):
    """Raw provider probe transport that still obeys the physical-attempt ledger."""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
        trust_env: bool,
    ) -> None:
        self.url = url
        self.headers = headers
        self.payload = payload
        self.timeout_seconds = timeout_seconds
        self.trust_env = trust_env
        self.response: httpx.Response | None = None

    def generate_accounted(
        self,
        request: LLMRequest,
        *,
        accounting_hook: LLMAttemptHook,
    ) -> LLMResponse:
        handle = accounting_hook.before_dispatch(
            request=request,
            dispatch_kind="system_probe",
        )
        started_at = time.perf_counter()
        try:
            response = httpx.post(
                self.url,
                headers=self.headers,
                json=self.payload,
                timeout=self.timeout_seconds,
                trust_env=self.trust_env,
            )
            self.response = response
        except httpx.RequestError as exc:
            error = LLMClientError(
                "LLM_PROVIDER_TRANSPORT_ERROR",
                str(exc),
                details={"exception_type": exc.__class__.__name__},
            )
            accounting_hook.after_error(
                handle,
                request=request,
                error=error,
                raw_response=None,
                provider_request_id=None,
                latency_ms=elapsed_ms(started_at),
            )
            raise error from exc

        body = _probe_response_body(response)
        request_id = _probe_request_id(response, body)
        if not response.is_success:
            error = LLMHTTPError(
                "LLM_PROVIDER_HTTP_ERROR",
                _probe_error_message(response, body),
                status_code=response.status_code,
                details={
                    "status_code": response.status_code,
                    "response_body": body,
                },
            )
            accounting_hook.after_error(
                handle,
                request=request,
                error=error,
                raw_response=body,
                provider_request_id=request_id,
                latency_ms=elapsed_ms(started_at),
            )
            raise error

        raw_usage = extract_raw_usage(body)
        normalized = normalize_raw_usage(raw_usage)
        usage = (
            {
                "input_tokens": normalized.prompt_tokens,
                "output_tokens": normalized.completion_tokens,
                "total_tokens": normalized.total_tokens,
            }
            if normalized is not None
            else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        llm_response = LLMResponse(
            request_id=request_id,
            provider=request.provider or "unknown",
            model=request.model,
            text=_probe_response_text(body),
            structured_output=None,
            response_format="text",
            raw_response=body,
            usage=usage,
            finish_reason=_probe_finish_reason(body),
            raw_usage=raw_usage,
            usage_present=raw_usage is not None,
            usage_complete=normalized is not None,
        )
        accounting_hook.after_response(
            handle,
            request=request,
            response=llm_response,
            latency_ms=elapsed_ms(started_at),
        )
        return llm_response


def execute_accounted_completion_probe(
    session: Session,
    request: LLMRequest,
    context: LLMCallContext,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    trust_env: bool,
) -> AccountedCompletionProbeResult:
    """Execute one raw completion probe with the same parent/attempt invariants."""

    call_id = f"llm_probe_{uuid.uuid4().hex}"
    execution = _AccountedCompletionProbeExecution(
        url=url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        trust_env=trust_env,
    )
    error_code: str | None = None
    error_message: str | None = None
    try:
        execute_accounted_call(
            session,
            execution,
            request,
            context,
            llm_call_id=call_id,
        )
    except Exception as exc:  # probe result is diagnostic; the ledger remains authoritative
        error_code = llm_failure_code(exc)
        error_message = str(exc)
    parent = session.get(LlmCall, call_id)
    if parent is not None:
        parent.request_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.request_payload_summary or {}),
                "probe_endpoint": url,
                "probe_method": "POST",
            }
        )
        parent.response_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.response_payload_summary or {}),
                "probe_status_code": (
                    execution.response.status_code
                    if execution.response is not None
                    else None
                ),
                "probe_error_code": error_code,
            }
        )
        session.commit()
    return AccountedCompletionProbeResult(
        llm_call_id=call_id,
        response=execution.response,
        error_code=error_code,
        error_message=error_message,
    )


def _probe_response_body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"text": response.text[:200]}
    return dict(payload) if isinstance(payload, dict) else {"data": payload}


def _probe_request_id(response: httpx.Response, body: dict[str, Any]) -> str | None:
    for value in (
        response.headers.get("x-request-id"),
        response.headers.get("request-id"),
        body.get("id"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _probe_response_text(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    for key in ("output_text", "text"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _probe_finish_reason(body: dict[str, Any]) -> str | None:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("finish_reason")
        if isinstance(value, str):
            return value
    value = body.get("status")
    return value if isinstance(value, str) else None


def _probe_error_message(response: httpx.Response, body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(body.get("message"), str):
        return str(body["message"])
    if isinstance(body.get("text"), str) and body["text"]:
        return str(body["text"])
    return f"provider returned status {response.status_code}"
