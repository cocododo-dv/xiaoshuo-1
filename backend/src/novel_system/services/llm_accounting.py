"""Durable accounting boundary for provider-backed LLM calls.

The caller session is deliberately committed before provider work.  Every
physical POST then uses short reservation, dispatch, and settlement
transactions; no database write transaction is held while waiting on the
network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterRunJob, LlmCall, LlmCallAttempt, SceneRunState, utcnow
from novel_system.llm_accounting_runtime import load_llm_accounting_runtime
from novel_system.services.context_budget import estimate_tokens
from novel_system.services.llm_audit import (
    audit_error_text,
    fingerprint_identifier,
    sanitize_audit_summary,
)
from novel_system.services.llm_client import (
    LLMClientError,
    LLMHTTPError,
    LLMRequest,
    LLMResponse,
    LLMAttemptHook,
    OfflineDeterministicExecution,
    OnlineAccountedExecution,
)
from novel_system.services.llm_providers.base import LLMDispatchKind


logger = logging.getLogger(__name__)

MESSAGE_TOKEN_OVERHEAD = 4
ACCOUNTING_EXECUTION_MODE_KEY = "_accounting_provider_execution_mode"
ACCOUNTING_INTEGRITY_BLOCKED_STATUS = "accounting_integrity_blocked"
ACCOUNTING_INTEGRITY_ERROR_CODES = {
    "LLM_ACCOUNTING_HOOK_NOT_INVOKED",
    "LLM_ACCOUNTING_UNKNOWN_DISPATCH",
    "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE",
}
_CONTROL_PLANE_ERROR_CODES = ACCOUNTING_INTEGRITY_ERROR_CODES | {
    "RUN_CHECKPOINT_OUTPUT_MISSING",
    "RUN_OWNER_LEASE_LOST",
    "LLM_USAGE_EXCEEDS_RESERVATION",
    "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
    "LLM_ACCOUNTING_EXECUTION_STEP_IN_PROGRESS",
    "LLM_ACCOUNTING_ATTEMPT_CALLBACK_CONFLICT",
    "LLM_ACCOUNTING_CONTEXT_REQUIRED",
    "LLM_ACCOUNTING_SESSION_REQUIRED",
    "LLM_ACCOUNTING_ADVISORY_FAILURE_UNTRACKED",
    "LLM_ACCOUNTING_PARENT_ID_MISSING",
    "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
    "LLM_OFFLINE_RESPONSE_INVALID",
    "LLM_OFFLINE_CAPABILITY_UNSUPPORTED",
    "LLM_ACCOUNTING_HOOK_UNSUPPORTED",
    "LLM_ACCOUNTING_CONTEXT_INVALID",
    "LLM_ACCOUNTING_INTEGRITY_BLOCKED",
    "LLM_ACCOUNTING_SCENE_RESERVATION_CORRUPT",
    "LLM_ACCOUNTING_SCENE_SETTLEMENT_CORRUPT",
    "LLM_ACCOUNTING_CALL_EXISTS",
    "LLM_ACCOUNTING_CALL_NOT_RECOVERABLE",
}


@dataclass(frozen=True, slots=True)
class LLMCallContext:
    """Explicit durable ownership for one logical call; IDs are never guessed."""

    scope_type: str
    scope_id: str
    node_id: str
    step: str
    project_id: str | None = None
    scene_id: str | None = None
    chapter_id: str | None = None
    run_job_id: str | None = None
    execution_id: str | None = None
    execution_step_key: str | None = None
    provider_execution_mode: Literal["online", "offline_deterministic"] = "online"

    def __post_init__(self) -> None:
        required = {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "node_id": self.node_id,
            "step": self.step,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"LLMCallContext requires explicit {', '.join(missing)}")
        optional_ownership = {
            "project_id": self.project_id,
            "scene_id": self.scene_id,
            "chapter_id": self.chapter_id,
            "run_job_id": self.run_job_id,
            "execution_id": self.execution_id,
            "execution_step_key": self.execution_step_key,
        }
        invalid_optional = [
            name
            for name, value in optional_ownership.items()
            if value is not None
            and (type(value) is not str or not value.strip())
        ]
        if invalid_optional:
            raise ValueError(
                "LLMCallContext optional ownership fields must be None or non-empty strings: "
                + ", ".join(invalid_optional)
            )
        has_execution_id = bool(str(self.execution_id or "").strip())
        has_execution_step = bool(str(self.execution_step_key or "").strip())
        if has_execution_id != has_execution_step:
            raise ValueError(
                "LLMCallContext execution_id and execution_step_key must be provided together"
            )
        if str(self.run_job_id or "").strip() and not (
            has_execution_id and has_execution_step
        ):
            raise ValueError(
                "LLMCallContext run_job_id requires execution_id and execution_step_key"
            )
        if self.provider_execution_mode not in {"online", "offline_deterministic"}:
            raise ValueError(
                "LLMCallContext.provider_execution_mode must be online or offline_deterministic"
            )


@dataclass(frozen=True, slots=True)
class RequestUsageEstimate:
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_tokens: int
    reserved_tokens: int


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage_is_estimate: bool


@dataclass(frozen=True, slots=True)
class AccountingRecoveryResult:
    status: Literal["released", "failed"]
    error_code: str | None
    may_retry: bool


@dataclass(frozen=True, slots=True)
class AccountedCompletionProbeResult:
    llm_call_id: str
    response: httpx.Response | None
    error_code: str | None
    error_message: str | None


class LLMAccountingError(LLMClientError):
    pass


class LLMAccountingRejected(LLMAccountingError):
    pass


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
                latency_ms=_elapsed_ms(started_at),
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
                latency_ms=_elapsed_ms(started_at),
            )
            raise error

        raw_usage = _extract_raw_usage(body)
        normalized = _normalize_raw_usage(raw_usage)
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
            latency_ms=_elapsed_ms(started_at),
        )
        return llm_response


def llm_failure_code(error: BaseException) -> str:
    """Return the stable failure code exposed by the runner/accounting boundary."""

    return str(
        getattr(error, "error_code", None)
        or getattr(error, "code", None)
        or error.__class__.__name__
    )


def is_llm_control_plane_failure(error: BaseException) -> bool:
    """Failures whose integrity/exactly-once semantics must never be degraded."""

    candidates = (error, getattr(error, "original_error", None))
    for candidate in candidates:
        if not isinstance(candidate, BaseException):
            continue
        if isinstance(candidate, LLMAccountingError) and not isinstance(
            candidate, LLMAccountingRejected
        ):
            return True
        if llm_failure_code(candidate) in _CONTROL_PLANE_ERROR_CODES:
            return True
    return False


def validate_product_call_ledger(
    session: Session,
    parent: LlmCall,
    *,
    expected_outcome: Literal[
        "completed",
        "parse_failed",
        "rejected_before_dispatch",
        "provider_failed",
    ],
    expected_error_code: str | None = None,
) -> None:
    """Validate a checkpointable advisory product against its physical-attempt ledger."""

    attempts = list(
        session.scalars(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.llm_call_id == parent.llm_call_id)
            .order_by(LlmCallAttempt.provider_attempt_no, LlmCallAttempt.attempt_id)
        )
    )
    execution_mode = (
        parent.request_payload_summary.get(ACCOUNTING_EXECUTION_MODE_KEY)
        if isinstance(parent.request_payload_summary, dict)
        else None
    )

    def invalid(message: str) -> None:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
            message,
            details={
                "llm_call_id": parent.llm_call_id,
                "expected_outcome": expected_outcome,
            },
        )

    parent_numeric_fields = (
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
    )
    if any(
        type(getattr(parent, field_name)) is not int or getattr(parent, field_name) < 0
        for field_name in parent_numeric_fields
    ):
        invalid("parent accounting counters are not non-negative integers")
    if (
        parent.total_tokens != parent.prompt_tokens + parent.completion_tokens
        or parent.estimated_tokens > parent.reserved_tokens
        or parent.budget_charged_tokens > parent.reserved_tokens
        or parent.budget_charged_tokens != min(parent.total_tokens, parent.reserved_tokens)
        or not parent.settled_at
    ):
        invalid("parent accounting totals or settlement marker are invalid")

    if expected_outcome == "rejected_before_dispatch":
        if (
            parent.accounting_status != "rejected"
            or parent.request_dispatched_at is not None
            or parent.error_code != expected_error_code
        ):
            invalid("rejected product parent status, dispatch, or error is invalid")
        if not attempts:
            zero_fields = (
                parent.estimated_tokens,
                parent.reserved_tokens,
                parent.budget_charged_tokens,
                parent.prompt_tokens,
                parent.completion_tokens,
                parent.total_tokens,
                parent.latency_ms,
            )
            if any(value != 0 for value in zero_fields):
                invalid("local rejected product must have a zero-attempt, zero-token parent")
            return

    if (
        expected_outcome in {"completed", "parse_failed"}
        and execution_mode == "offline_deterministic"
    ):
        offline_zero_fields = (
            parent.estimated_tokens,
            parent.reserved_tokens,
            parent.budget_charged_tokens,
            parent.prompt_tokens,
            parent.completion_tokens,
            parent.total_tokens,
            parent.latency_ms,
        )
        if (
            attempts
            or parent.provider != "offline_deterministic"
            or parent.accounting_status != "settled"
            or parent.request_dispatched_at is not None
            or parent.error_code is not None
            or any(value != 0 for value in offline_zero_fields)
        ):
            invalid("offline product violates zero-attempt deterministic settlement")
        return

    if not attempts:
        invalid("provider-backed product is missing physical attempts")
    ordinals = [attempt.provider_attempt_no for attempt in attempts]
    if ordinals != list(range(len(attempts))):
        invalid("physical attempt ordinals are not contiguous")
    child_numeric_fields = (
        "provider_attempt_no",
        "request_max_output_tokens",
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
    )
    for attempt in attempts:
        if (
            not isinstance(attempt.attempt_id, str)
            or not attempt.attempt_id
            or attempt.llm_call_id != parent.llm_call_id
            or any(
                type(getattr(attempt, field_name)) is not int
                or getattr(attempt, field_name) < 0
                for field_name in child_numeric_fields
            )
            or attempt.total_tokens != attempt.prompt_tokens + attempt.completion_tokens
            or attempt.estimated_tokens > attempt.reserved_tokens
            or attempt.budget_charged_tokens > attempt.reserved_tokens
            or attempt.budget_charged_tokens != min(
                attempt.total_tokens, attempt.reserved_tokens
            )
            or not attempt.settled_at
            or (
                attempt.provider_attempt_no == 0
                and attempt.dispatch_kind != "initial"
            )
            or (
                attempt.provider_attempt_no > 0
                and attempt.dispatch_kind == "initial"
            )
        ):
            invalid("physical attempt identity, counters, or settlement marker is invalid")

    aggregate_fields = (
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
    )
    invalid_aggregate = any(
        (getattr(parent, field_name) or 0)
        != sum((getattr(attempt, field_name) or 0) for attempt in attempts)
        for field_name in aggregate_fields
    )
    invalid_usage_provenance = bool(parent.usage_is_estimate) != any(
        bool(attempt.usage_is_estimate) for attempt in attempts
    )
    if invalid_aggregate or invalid_usage_provenance:
        invalid("parent token accounting does not equal the physical-attempt aggregate")

    if expected_outcome == "rejected_before_dispatch":
        parent_actual_fields = (
            parent.budget_charged_tokens,
            parent.prompt_tokens,
            parent.completion_tokens,
            parent.total_tokens,
            parent.latency_ms,
        )
        if len(attempts) != 1 or any(value != 0 for value in parent_actual_fields) or any(
            attempt.accounting_status != "rejected"
            or attempt.request_dispatched_at is not None
            or attempt.provider_request_id is not None
            or attempt.estimated_tokens <= 0
            or attempt.reserved_tokens <= 0
            or attempt.error_code != expected_error_code
            or any(
                value != 0
                for value in (
                    attempt.budget_charged_tokens,
                    attempt.prompt_tokens,
                    attempt.completion_tokens,
                    attempt.total_tokens,
                    attempt.latency_ms,
                )
            )
            for attempt in attempts
        ):
            invalid("physical-gate rejection contains dispatch, usage, charge, or mixed status")
        return

    dispatched = [attempt for attempt in attempts if attempt.request_dispatched_at is not None]
    if parent.request_dispatched_at is None or not dispatched:
        invalid("provider-backed product has no dispatched physical attempt")
    if expected_outcome in {"completed", "parse_failed"}:
        settled_ordinals = [
            attempt.provider_attempt_no
            for attempt in dispatched
            if attempt.accounting_status == "settled"
        ]
        if (
            parent.accounting_status != "settled"
            or parent.error_code is not None
            or settled_ordinals != [len(attempts) - 1]
            or any(
                attempt.request_dispatched_at is None
                or attempt.accounting_status != "failed"
                or not attempt.error_code
                for attempt in attempts[:-1]
            )
            or attempts[-1].request_dispatched_at is None
            or attempts[-1].accounting_status != "settled"
            or attempts[-1].error_code is not None
        ):
            invalid("completed product does not resolve to a settled parent and child")
        if any(attempt.request_dispatched_at is None for attempt in attempts):
            invalid("completed product contains an undispatched child")
        return
    if expected_outcome == "provider_failed":
        terminal_attempt = attempts[-1]
        terminal_is_failed_dispatch = (
            terminal_attempt.request_dispatched_at is not None
            and terminal_attempt.accounting_status == "failed"
        )
        terminal_is_undispatched_rejection = (
            terminal_attempt.request_dispatched_at is None
            and terminal_attempt.accounting_status == "rejected"
            and terminal_attempt.provider_request_id is None
            and terminal_attempt.estimated_tokens > 0
            and terminal_attempt.reserved_tokens > 0
            and all(
                value == 0
                for value in (
                    terminal_attempt.budget_charged_tokens,
                    terminal_attempt.prompt_tokens,
                    terminal_attempt.completion_tokens,
                    terminal_attempt.total_tokens,
                    terminal_attempt.latency_ms,
                )
            )
        )
        if (
            parent.accounting_status != "failed"
            or parent.error_code != expected_error_code
            or any(
                attempt.request_dispatched_at is None
                or attempt.accounting_status != "failed"
                or not attempt.error_code
                for attempt in attempts[:-1]
            )
            or not (terminal_is_failed_dispatch or terminal_is_undispatched_rejection)
            or terminal_attempt.error_code != expected_error_code
        ):
            invalid("provider-failed product does not resolve to a failed parent and dispatched child")
        return
    invalid("unsupported advisory product outcome")


def validate_product_call(
    session: Session,
    call_id: str,
    context: LLMCallContext,
    *,
    expected_outcome: Literal[
        "completed",
        "parse_failed",
        "rejected_before_dispatch",
        "provider_failed",
    ],
    expected_error_code: str | None = None,
) -> LlmCall:
    """Bind a product to its exact logical parent and validate physical attempts."""

    parent = session.get(LlmCall, call_id)
    execution_mode = (
        parent.request_payload_summary.get(ACCOUNTING_EXECUTION_MODE_KEY)
        if parent is not None and isinstance(parent.request_payload_summary, dict)
        else None
    )
    if (
        parent is None
        or parent.scope_type != context.scope_type
        or parent.scope_id != context.scope_id
        or parent.node_id != context.node_id
        or parent.step != context.step
        or parent.project_id != context.project_id
        or parent.chapter_id != context.chapter_id
        or parent.scene_id != context.scene_id
        or parent.run_job_id != context.run_job_id
        or parent.execution_id != context.execution_id
        or parent.execution_step_key != context.execution_step_key
        or execution_mode != context.provider_execution_mode
        or (expected_error_code is not None and parent.error_code != expected_error_code)
    ):
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
            "advisory product is detached from its durable accounting parent "
            f"(actual mode={execution_mode!r}, node={getattr(parent, 'node_id', None)!r}, "
            f"step={getattr(parent, 'step', None)!r}; expected "
            f"mode={context.provider_execution_mode!r}, node={context.node_id!r}, "
            f"step={context.step!r})",
            details={
                "llm_call_id": call_id,
                "expected_outcome": expected_outcome,
                "actual": (
                    {
                        "scope_type": parent.scope_type,
                        "scope_id": parent.scope_id,
                        "node_id": parent.node_id,
                        "step": parent.step,
                        "project_id": parent.project_id,
                        "chapter_id": parent.chapter_id,
                        "scene_id": parent.scene_id,
                        "run_job_id": parent.run_job_id,
                        "execution_id": parent.execution_id,
                        "execution_step_key": parent.execution_step_key,
                        "provider_execution_mode": execution_mode,
                        "error_code": parent.error_code,
                    }
                    if parent is not None
                    else None
                ),
                "expected": {
                    "scope_type": context.scope_type,
                    "scope_id": context.scope_id,
                    "node_id": context.node_id,
                    "step": context.step,
                    "project_id": context.project_id,
                    "chapter_id": context.chapter_id,
                    "scene_id": context.scene_id,
                    "run_job_id": context.run_job_id,
                    "execution_id": context.execution_id,
                    "execution_step_key": context.execution_step_key,
                    "provider_execution_mode": context.provider_execution_mode,
                    "error_code": expected_error_code,
                },
            },
        )
    validate_product_call_ledger(
        session,
        parent,
        expected_outcome=expected_outcome,
        expected_error_code=expected_error_code,
    )
    return parent


def classify_advisory_failure(
    session: Session | None,
    error: BaseException,
    context: LLMCallContext,
) -> tuple[Literal["rejected_before_dispatch", "provider_failed"], str, str]:
    """Classify a degradable failure from durable parent/child evidence, never its name."""

    if is_llm_control_plane_failure(error):
        raise error
    call_id = getattr(error, "llm_call_id", None)
    reported_error_code = llm_failure_code(error)
    if session is None:
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_SESSION_REQUIRED",
            "advisory failure classification requires a durable accounting session",
        ) from error
    if not isinstance(call_id, str) or not call_id:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_ADVISORY_FAILURE_UNTRACKED",
            "advisory provider failure has no durable parent call id",
            details={
                "original_error_type": error.__class__.__name__,
                "original_error_code": reported_error_code,
            },
        ) from error
    parent = session.get(LlmCall, call_id)
    if parent is None:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
            "advisory failure parent is missing",
            details={"llm_call_id": call_id, "error_code": reported_error_code},
        ) from error
    if parent.accounting_status == "rejected":
        outcome: Literal["rejected_before_dispatch", "provider_failed"] = "rejected_before_dispatch"
    elif parent.accounting_status == "failed":
        outcome = "provider_failed"
    else:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
            "advisory failure parent status cannot produce a degraded product",
            details={"llm_call_id": call_id, "accounting_status": parent.accounting_status},
        ) from error
    error_code = parent.error_code
    if not isinstance(error_code, str) or not error_code:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
            "advisory failure parent is missing its durable terminal error code",
            details={"llm_call_id": call_id, "accounting_status": parent.accounting_status},
        ) from error
    try:
        validate_product_call(
            session,
            call_id,
            context,
            expected_outcome=outcome,
            expected_error_code=error_code,
        )
    except LLMAccountingError as integrity_error:
        raise integrity_error from error
    return outcome, call_id, error_code


def estimate_request_usage(request: LLMRequest) -> RequestUsageEstimate:
    contents = [str(message.get("content") or "") for message in request.messages]
    wire_segments = list(contents)
    if request.wire_response_format and request.response_schema is not None:
        wire_segments.append(
            json.dumps(
                request.response_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    overhead = MESSAGE_TOKEN_OVERHEAD * len(contents)
    estimated_input = sum(estimate_tokens(content) for content in wire_segments) + overhead
    estimated_output = max(0, int(request.max_output_tokens))
    estimated_total = estimated_input + estimated_output
    # One token cannot contain less than one UTF-8 byte.  This intentionally
    # over-reserves multilingual prompts while still being deterministic.
    utf8_upper_bound = sum(len(content.encode("utf-8")) for content in wire_segments) + overhead
    reserved = max(estimated_total, utf8_upper_bound + estimated_output)
    return RequestUsageEstimate(
        estimated_input_tokens=estimated_input,
        estimated_output_tokens=estimated_output,
        estimated_tokens=estimated_total,
        reserved_tokens=reserved,
    )


def normalize_response_usage(response: LLMResponse, request: LLMRequest) -> NormalizedUsage:
    if response.provider == "offline_deterministic" and _is_explicit_zero_usage(response.usage):
        return NormalizedUsage(0, 0, 0, False)

    if response.usage_complete is True:
        actual = _normalize_raw_usage(response.raw_usage)
        if actual is not None:
            return actual

    request_estimate = estimate_request_usage(request)
    completion_estimate = estimate_tokens(response.text)
    total = request_estimate.estimated_input_tokens + completion_estimate
    return NormalizedUsage(
        prompt_tokens=request_estimate.estimated_input_tokens,
        completion_tokens=completion_estimate,
        total_tokens=max(1, total),
        usage_is_estimate=True,
    )


def _begin_claim_transaction(session: Session) -> None:
    """Serialize the absent-row execution-step claim on file-backed SQLite."""

    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _assert_run_job_running(session: Session, context: LLMCallContext) -> None:
    """Fence a new durable node against the cross-process job state."""

    if context.run_job_id is None:
        return
    row = session.execute(
        select(
            ChapterRunJob.status,
            ChapterRunJob.job_type,
            ChapterRunJob.scene_id,
            ChapterRunJob.chapter_id,
            ChapterRunJob.payload_json,
        ).where(
            ChapterRunJob.job_id == context.run_job_id,
        )
    ).one_or_none()
    payload = row.payload_json if row is not None and isinstance(row.payload_json, dict) else {}
    ownership_matches = bool(
        row is not None
        and (
            (
                row.job_type == "scene_run_full"
                and (context.scene_id is None or row.scene_id == context.scene_id)
            )
            or (
                row.job_type == "chapter_run_full"
                and row.chapter_id == context.chapter_id
                and (
                    context.scene_id is None
                    or payload.get("current_scene_id") == context.scene_id
                )
            )
        )
    )
    if (
        row is not None
        and row.status == "running"
        and ownership_matches
    ):
        return
    session.rollback()
    if row is not None and row.status in {"cancel_requested", "cancelled"}:
        raise LLMAccountingRejected(
            "RUN_JOB_CANCELLED_BY_AUTHOR",
            "scene run cancellation prevents the next provider node",
            details={"job_id": context.run_job_id, "status": row.status},
        )
    raise LLMAccountingRejected(
        "LLM_ACCOUNTING_CONTEXT_INVALID",
        "scene run job is not the active running owner for this node",
        details={
            "job_id": context.run_job_id,
            "status": row.status if row is not None else None,
        },
    )


def _execution_step_conflict(existing_call: LlmCall) -> LLMAccountingError:
    details = {
        "llm_call_id": existing_call.llm_call_id,
        "execution_id": existing_call.execution_id,
        "execution_step_key": existing_call.execution_step_key,
        "accounting_status": existing_call.accounting_status,
    }
    if (
        existing_call.request_dispatched_at is not None
        and existing_call.accounting_status in {"reserved", "failed"}
    ) or existing_call.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING":
        return LLMAccountingError(
            "RUN_CHECKPOINT_OUTPUT_MISSING",
            "this execution step may already have reached the provider; automatic resend is blocked",
            details=details,
        )
    if existing_call.accounting_status == "usage_exceeds_reservation":
        return LLMAccountingError(
            "LLM_USAGE_EXCEEDS_RESERVATION",
            "this execution step exceeded its reservation; automatic resend is blocked",
            details=details,
        )
    if existing_call.accounting_status == "reserved":
        return LLMAccountingError(
            "LLM_ACCOUNTING_EXECUTION_STEP_IN_PROGRESS",
            "this execution step already has an active accounting claim",
            details=details,
        )
    return LLMAccountingError(
        "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
        f"this execution step already has a {existing_call.accounting_status} provider call",
        details=details,
    )


def _global_fences_armed(settings: Any) -> bool:
    """Whether any singleton-wide provider fence is configured (``0`` = off)."""

    return (
        settings.llm_max_concurrent_requests > 0
        or settings.llm_daily_request_limit > 0
        or settings.llm_daily_token_limit > 0
        or settings.llm_monthly_token_limit > 0
        or settings.llm_project_daily_token_limit > 0
        or settings.llm_daily_cost_limit_usd > 0
    )


def _usage_overage_is_fenced(session: Session, scene_id: str | None) -> bool:
    """Whether provider usage beyond the reservation must block delivery.

    The reservation is the pre-dispatch fence input, not a promise the provider
    keeps: thinking backends report reasoning tokens inside ``completion_tokens``
    and relays routinely do not cap them under ``max_tokens`` (a 2,000-token
    classification answer came back with 11,776 completion tokens), so actual
    usage can legitimately exceed any deterministic estimate.  While a fence is
    armed — any singleton-wide quota, or an armed scene token budget — the
    overage means that fence was checked against too small a number, and the
    response stays blocked exactly as before.  With nothing armed (the
    single-author default) there is no fence to protect: the tokens are already
    spent, so the response is delivered and settled at actual usage, with the
    overage kept in the audit summary as ``usage_overage_tokens``.
    """

    if _global_fences_armed(load_llm_accounting_runtime()):
        return True
    if scene_id is None:
        return False
    state = session.get(SceneRunState, scene_id)
    if state is None or state.scene_token_budget is None:
        return False
    from novel_system.services import scene_budget

    return not scene_budget.is_scene_budget_disarmed(state)


def _enforce_global_llm_quotas(
    session: Session,
    context: LLMCallContext,
    estimate: RequestUsageEstimate,
) -> None:
    """Atomically fence singleton-wide, monthly, and project provider spend.

    Every fence is opt-in and ships disabled: a limit of ``0`` means "no
    ceiling", and an install with nothing armed returns before running any of
    the counting scans below.  The caller has already opened an immediate claim
    transaction on SQLite.  Open attempts count at their full reservation;
    terminal attempts count at actual provider usage (or the conservative usage
    estimate persisted for a failed response).  ``budget_charged_tokens``
    intentionally remains capped by the reservation for scene-budget semantics
    and must not be reused as a singleton-wide spend counter.
    """

    settings = load_llm_accounting_runtime()
    if not _global_fences_armed(settings):
        return
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    if settings.llm_max_concurrent_requests > 0:
        concurrent_count = len(
            list(
                session.scalars(
                    select(LlmCallAttempt.attempt_id)
                    .where(LlmCallAttempt.accounting_status == "reserved")
                    .limit(settings.llm_max_concurrent_requests)
                )
            )
        )
        if concurrent_count >= settings.llm_max_concurrent_requests:
            _reject_global_quota(
                session,
                "LLM_GLOBAL_CONCURRENCY_LIMIT",
                "global provider concurrency limit reached",
                used=concurrent_count,
                requested=1,
                limit=settings.llm_max_concurrent_requests,
                period="concurrent",
            )

    daily_tokens = 0
    daily_requests = 0
    daily_cost = 0.0
    if (
        settings.llm_daily_request_limit > 0
        or settings.llm_daily_token_limit > 0
        or settings.llm_daily_cost_limit_usd > 0
    ):
        daily_rows = _quota_attempt_rows(session, created_after=day_start)
        daily_tokens, daily_requests, daily_cost = _quota_usage(daily_rows, settings=settings)
    if (
        settings.llm_daily_request_limit > 0
        and daily_requests + 1 > settings.llm_daily_request_limit
    ):
        _reject_global_quota(
            session,
            "LLM_DAILY_REQUEST_LIMIT",
            "daily provider request limit reached",
            used=daily_requests,
            requested=1,
            limit=settings.llm_daily_request_limit,
            period="utc_day",
        )
    if (
        settings.llm_daily_token_limit > 0
        and daily_tokens + estimate.reserved_tokens > settings.llm_daily_token_limit
    ):
        _reject_global_quota(
            session,
            "LLM_DAILY_TOKEN_LIMIT",
            "daily provider token limit reached",
            used=daily_tokens,
            requested=estimate.reserved_tokens,
            limit=settings.llm_daily_token_limit,
            period="utc_day",
        )

    if settings.llm_monthly_token_limit > 0:
        monthly_rows = _quota_attempt_rows(session, created_after=month_start)
        monthly_tokens, _, _ = _quota_usage(monthly_rows, settings=settings)
        if monthly_tokens + estimate.reserved_tokens > settings.llm_monthly_token_limit:
            _reject_global_quota(
                session,
                "LLM_MONTHLY_TOKEN_LIMIT",
                "monthly provider token limit reached",
                used=monthly_tokens,
                requested=estimate.reserved_tokens,
                limit=settings.llm_monthly_token_limit,
                period="utc_month",
            )

    if context.project_id and settings.llm_project_daily_token_limit > 0:
        project_rows = _quota_attempt_rows(
            session,
            created_after=day_start,
            project_id=context.project_id,
        )
        project_tokens, _, _ = _quota_usage(project_rows, settings=settings)
        if project_tokens + estimate.reserved_tokens > settings.llm_project_daily_token_limit:
            _reject_global_quota(
                session,
                "LLM_PROJECT_DAILY_TOKEN_LIMIT",
                "project daily provider token limit reached",
                used=project_tokens,
                requested=estimate.reserved_tokens,
                limit=settings.llm_project_daily_token_limit,
                period="utc_day",
                project_id=context.project_id,
            )

    if settings.llm_daily_cost_limit_usd > 0:
        max_rate = max(
            settings.llm_input_cost_per_million_usd,
            settings.llm_output_cost_per_million_usd,
        )
        requested_cost = estimate.reserved_tokens * max_rate / 1_000_000
        if daily_cost + requested_cost > settings.llm_daily_cost_limit_usd:
            _reject_global_quota(
                session,
                "LLM_DAILY_COST_LIMIT",
                "daily provider cost limit reached",
                used=round(daily_cost, 8),
                requested=round(requested_cost, 8),
                limit=settings.llm_daily_cost_limit_usd,
                period="utc_day",
            )


def _quota_attempt_rows(
    session: Session,
    *,
    created_after: str,
    project_id: str | None = None,
) -> list[tuple[LlmCallAttempt, str | None]]:
    query = (
        select(LlmCallAttempt, LlmCall.project_id)
        .join(LlmCall, LlmCall.llm_call_id == LlmCallAttempt.llm_call_id)
        .where(LlmCallAttempt.created_at >= created_after)
    )
    if project_id is not None:
        query = query.where(LlmCall.project_id == project_id)
    return list(session.execute(query).all())


def _quota_usage(
    rows: list[tuple[LlmCallAttempt, str | None]],
    *,
    settings: Any,
) -> tuple[int, int, float]:
    tokens = 0
    request_count = 0
    cost = 0.0
    max_rate = max(
        settings.llm_input_cost_per_million_usd,
        settings.llm_output_cost_per_million_usd,
    )
    for attempt, _project_id in rows:
        is_open = attempt.accounting_status == "reserved"
        if is_open:
            charged = int(attempt.reserved_tokens or 0)
            cost += charged * max_rate / 1_000_000
        else:
            # A provider may report more tokens than the pre-dispatch
            # reservation.  The scene budget records only the bounded
            # ``budget_charged_tokens`` amount, while global/project quotas
            # must retain the complete provider liability.
            charged = int(attempt.total_tokens or 0)
            cost += (
                int(attempt.prompt_tokens or 0) * settings.llm_input_cost_per_million_usd
                + int(attempt.completion_tokens or 0) * settings.llm_output_cost_per_million_usd
            ) / 1_000_000
        tokens += charged
        if is_open or attempt.request_dispatched_at is not None:
            request_count += 1
    return tokens, request_count, cost


def _reject_global_quota(
    session: Session,
    code: str,
    message: str,
    *,
    used: int | float,
    requested: int | float,
    limit: int | float,
    period: str,
    project_id: str | None = None,
) -> None:
    session.rollback()
    raise LLMAccountingRejected(
        code,
        message,
        details={
            "used": used,
            "requested": requested,
            "limit": limit,
            "period": period,
            "project_id": project_id,
            "retryable": period in {"concurrent", "utc_day", "utc_month"},
        },
    )


def llm_quota_snapshot(session: Session, *, project_id: str | None = None) -> dict[str, Any]:
    """Return the same quota counters used by the pre-dispatch hard gate."""

    settings = load_llm_accounting_runtime()
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    daily_tokens, daily_requests, daily_cost = _quota_usage(
        _quota_attempt_rows(session, created_after=day_start),
        settings=settings,
    )
    monthly_tokens, _, _ = _quota_usage(
        _quota_attempt_rows(session, created_after=month_start),
        settings=settings,
    )
    project_tokens = None
    if project_id:
        project_tokens, _, _ = _quota_usage(
            _quota_attempt_rows(session, created_after=day_start, project_id=project_id),
            settings=settings,
        )
    concurrent = len(
        list(
            session.scalars(
                select(LlmCallAttempt.attempt_id).where(
                    LlmCallAttempt.accounting_status == "reserved"
                )
            )
        )
    )
    return {
        "period_timezone": "UTC",
        "any_enforced": _global_fences_armed(settings),
        "daily_tokens": _quota_meter(daily_tokens, settings.llm_daily_token_limit),
        "monthly_tokens": _quota_meter(monthly_tokens, settings.llm_monthly_token_limit),
        "project_daily_tokens": {
            "project_id": project_id,
            **_quota_meter(project_tokens, settings.llm_project_daily_token_limit),
        },
        "daily_requests": _quota_meter(daily_requests, settings.llm_daily_request_limit),
        "concurrent_requests": _quota_meter(concurrent, settings.llm_max_concurrent_requests),
        "daily_cost_usd": _quota_meter(
            round(daily_cost, 8), settings.llm_daily_cost_limit_usd
        ),
    }


def _quota_meter(used: int | float | None, limit: int | float) -> dict[str, Any]:
    """Shape one dashboard meter; a ``0`` limit reports the fence as disarmed.

    ``used`` is always reported so the dashboard keeps its consumption reading
    after every fence is turned off.
    """

    return {"used": used, "limit": limit or None, "enforced": limit > 0}


def _reserve_scene_capacity(
    session: Session,
    scene_id: str | None,
    reserved_tokens: int,
) -> int | None:
    if scene_id is None:
        return None
    result = session.execute(
        update(SceneRunState)
        .where(
            SceneRunState.scene_id == scene_id,
            SceneRunState.scene_token_budget.is_not(None),
            SceneRunState.scene_tokens_reserved == 0,
            SceneRunState.total_attempt_count < SceneRunState.attempt_budget,
            SceneRunState.provider_attempts_used < SceneRunState.provider_attempt_budget,
            SceneRunState.scene_tokens_used + reserved_tokens
            <= SceneRunState.scene_token_budget,
            or_(
                SceneRunState.run_execution_status.is_(None),
                SceneRunState.run_execution_status.not_in(
                    {"usage_exceeds_reservation", ACCOUNTING_INTEGRITY_BLOCKED_STATUS}
                ),
            ),
        )
        .values(scene_tokens_reserved=reserved_tokens)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        if reserved_tokens <= 0:
            session.rollback()
            raise LLMAccountingError(
                "LLM_ACCOUNTING_SCENE_RESERVATION_CORRUPT",
                "scene token fence was not durably reserved",
            )
        return int(reserved_tokens)
    session.rollback()
    error = _scene_gate_error(session, scene_id, reserved_tokens=reserved_tokens)
    session.rollback()
    raise error


def _scene_gate_error(
    session: Session,
    scene_id: str,
    *,
    reserved_tokens: int,
    allow_existing_fence: bool = False,
) -> LLMAccountingRejected:
    state = session.execute(
        select(
            SceneRunState.provider_attempts_used,
            SceneRunState.provider_attempt_budget,
            SceneRunState.total_attempt_count,
            SceneRunState.attempt_budget,
            SceneRunState.scene_token_budget,
            SceneRunState.scene_tokens_used,
            SceneRunState.scene_tokens_reserved,
            SceneRunState.run_execution_status,
        ).where(SceneRunState.scene_id == scene_id)
    ).one_or_none()
    if state is None or state.scene_token_budget is None:
        return LLMAccountingRejected(
            "LLM_SCENE_TOKEN_BUDGET_UNINITIALIZED",
            "online scene provider calls require an initialized finite scene token budget",
        )
    if state.run_execution_status == "usage_exceeds_reservation":
        return LLMAccountingRejected(
            "LLM_USAGE_EXCEEDS_RESERVATION",
            "scene accounting is blocked after provider usage exceeded its reservation",
        )
    if state.run_execution_status == ACCOUNTING_INTEGRITY_BLOCKED_STATUS:
        return LLMAccountingRejected(
            "LLM_ACCOUNTING_INTEGRITY_BLOCKED",
            "scene provider execution is blocked after an untracked dispatch",
        )
    if state.scene_tokens_reserved > 0 and not allow_existing_fence:
        return LLMAccountingRejected(
            "LLM_SCENE_CALL_IN_FLIGHT",
            "another provider call already holds the scene token fence",
        )
    if state.total_attempt_count >= state.attempt_budget:
        return LLMAccountingRejected(
            "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
            "scene business attempt budget exhausted before provider dispatch",
        )
    if state.provider_attempts_used >= state.provider_attempt_budget:
        return LLMAccountingRejected(
            "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
            "provider attempt budget exhausted before dispatch",
        )
    if (
        state.scene_token_budget is not None
        and state.scene_tokens_used + reserved_tokens > state.scene_token_budget
    ):
        return LLMAccountingRejected(
            "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
            "scene token budget exhausted before dispatch",
        )
    return LLMAccountingRejected(
        "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
        "provider attempt claim lost a concurrent budget race",
    )


def _consume_scene_provider_attempt(session: Session, scene_id: str | None) -> bool:
    if scene_id is None:
        return False
    result = session.execute(
        update(SceneRunState)
        .where(
            SceneRunState.scene_id == scene_id,
            SceneRunState.total_attempt_count < SceneRunState.attempt_budget,
            SceneRunState.provider_attempts_used < SceneRunState.provider_attempt_budget,
            or_(
                SceneRunState.run_execution_status.is_(None),
                SceneRunState.run_execution_status.not_in(
                    {"usage_exceeds_reservation", ACCOUNTING_INTEGRITY_BLOCKED_STATUS}
                ),
            ),
        )
        .values(provider_attempts_used=SceneRunState.provider_attempts_used + 1)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _release_scene_reservation(session: Session, scene_id: str | None, reserved_tokens: int) -> None:
    if scene_id is None:
        return
    result = session.execute(
        update(SceneRunState)
        .where(
            SceneRunState.scene_id == scene_id,
            SceneRunState.scene_token_budget.is_not(None),
            SceneRunState.scene_tokens_reserved == reserved_tokens,
        )
        .values(scene_tokens_reserved=0)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        current_fence = session.scalar(
            select(SceneRunState.scene_tokens_reserved).where(
                SceneRunState.scene_id == scene_id
            )
        )
        if current_fence is not None and current_fence != 0:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_SCENE_RESERVATION_CORRUPT",
                "scene reservation does not match the token fence being released",
            )
    if result.rowcount not in {0, 1}:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_SCENE_RESERVATION_CORRUPT",
            "scene reservation release affected an unexpected number of rows",
        )


def _settle_scene_usage(
    session: Session,
    scene_id: str | None,
    *,
    reserved_tokens: int,
    actual_tokens: int,
    usage_exceeds_reservation: bool,
) -> None:
    if scene_id is None:
        return
    values: dict[str, Any] = {
        "scene_tokens_reserved": 0,
        "scene_tokens_used": SceneRunState.scene_tokens_used + actual_tokens,
    }
    if usage_exceeds_reservation:
        values["run_execution_status"] = "usage_exceeds_reservation"
    result = session.execute(
        update(SceneRunState)
        .where(
            SceneRunState.scene_id == scene_id,
            SceneRunState.scene_token_budget.is_not(None),
            SceneRunState.scene_tokens_reserved == reserved_tokens,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0 and session.scalar(
        select(SceneRunState.scene_id).where(SceneRunState.scene_id == scene_id)
    ) is not None:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_SCENE_SETTLEMENT_CORRUPT",
            "scene reservation does not match the token fence being settled",
        )
    if result.rowcount not in {0, 1}:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_SCENE_SETTLEMENT_CORRUPT",
            "scene settlement affected an unexpected number of rows",
        )


def _validate_offline_response(response: object) -> LLMResponse:
    if (
        not isinstance(response, LLMResponse)
        or response.provider != "offline_deterministic"
        or not _is_explicit_zero_usage(response.usage)
    ):
        raise LLMAccountingRejected(
            "LLM_OFFLINE_RESPONSE_INVALID",
            "offline deterministic execution requires an offline_deterministic response with complete zero usage",
        )
    return response


def _reject_integrity_blocked_scene(session: Session, scene_id: str | None) -> None:
    if scene_id is None:
        return
    run_status = session.scalar(
        select(SceneRunState.run_execution_status).where(
            SceneRunState.scene_id == scene_id
        )
    )
    integrity_tombstone = session.scalar(
        select(LlmCall.llm_call_id)
        .where(
            LlmCall.scene_id == scene_id,
            LlmCall.request_dispatched_at.is_not(None),
            LlmCall.error_code.in_(ACCOUNTING_INTEGRITY_ERROR_CODES),
        )
        .limit(1)
    )
    session.rollback()
    if (
        run_status == ACCOUNTING_INTEGRITY_BLOCKED_STATUS
        or integrity_tombstone is not None
    ):
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_INTEGRITY_BLOCKED",
            "scene provider execution is blocked after an untracked dispatch",
        )


def _expire_cached_scene_accounting_state(session: Session, scene_id: str | None) -> None:
    if scene_id is None:
        return
    identity_key = session.identity_key(SceneRunState, scene_id)
    state = session.identity_map.get(identity_key)
    if state is None:
        return
    session.expire(
        state,
        attribute_names=[
            "scene_token_budget",
            "scene_tokens_used",
            "scene_tokens_reserved",
            "provider_attempts_used",
            "provider_attempt_budget",
            "run_execution_status",
        ],
    )


def _record_unknown_dispatch(
    session: Session,
    *,
    call_id: str,
    context: LLMCallContext,
    request: LLMRequest,
    request_estimate: RequestUsageEstimate,
    response: object | None,
    error: BaseException,
    latency_ms: int,
) -> LLMAccountingError:
    if (
        isinstance(error, LLMAccountingError)
        and error.code == "LLM_ACCOUNTING_HOOK_NOT_INVOKED"
    ):
        audit_error = error
    else:
        audit_error = LLMAccountingError(
            "LLM_ACCOUNTING_UNKNOWN_DISPATCH",
            "accounted online execution failed without reporting a physical attempt",
            details={
                "original_error_type": error.__class__.__name__,
                "original_error_code": getattr(error, "code", None),
                "original_error_message": str(error),
            },
        )

    if isinstance(response, LLMResponse):
        usage = normalize_response_usage(response, request)
        provider_request_id = response.request_id
    else:
        usage = NormalizedUsage(
            prompt_tokens=request_estimate.estimated_input_tokens,
            completion_tokens=request_estimate.estimated_output_tokens,
            total_tokens=request_estimate.estimated_tokens,
            usage_is_estimate=True,
        )
        provider_request_id = None
    now = utcnow()
    exceeds = usage.total_tokens > request_estimate.reserved_tokens and _usage_overage_is_fenced(
        session, context.scene_id
    )
    attempt = LlmCallAttempt(
        attempt_id=f"llmattempt_{uuid.uuid4().hex}",
        llm_call_id=call_id,
        provider_attempt_no=0,
        dispatch_kind="initial",
        request_max_output_tokens=request.max_output_tokens,
        provider_request_id=fingerprint_identifier(provider_request_id),
        estimated_tokens=request_estimate.estimated_tokens,
        reserved_tokens=request_estimate.reserved_tokens,
        budget_charged_tokens=min(usage.total_tokens, request_estimate.reserved_tokens),
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        usage_is_estimate=usage.usage_is_estimate,
        accounting_status="usage_exceeds_reservation" if exceeds else "failed",
        request_dispatched_at=now,
        settled_at=now,
        latency_ms=max(0, latency_ms),
        error_code=audit_error.code,
        error_text=audit_error_text(str(audit_error), error_code=audit_error.code),
    )
    session.add(attempt)
    parent = session.get(LlmCall, call_id)
    assert parent is not None
    parent.request_dispatched_at = now
    if context.scene_id is not None:
        session.execute(
            update(SceneRunState)
            .where(SceneRunState.scene_id == context.scene_id)
            .values(
                provider_attempts_used=SceneRunState.provider_attempts_used + 1,
                scene_tokens_used=SceneRunState.scene_tokens_used + usage.total_tokens,
                run_execution_status=ACCOUNTING_INTEGRITY_BLOCKED_STATUS,
            )
            .execution_options(synchronize_session=False)
        )
    session.flush()
    _aggregate_parent(session, call_id)
    session.commit()
    _expire_cached_scene_accounting_state(session, context.scene_id)
    return audit_error


def _has_open_attempt(session: Session, call_id: str) -> bool:
    return session.scalar(
        select(LlmCallAttempt.attempt_id)
        .where(
            LlmCallAttempt.llm_call_id == call_id,
            LlmCallAttempt.accounting_status == "reserved",
        )
        .limit(1)
    ) is not None


def _lifecycle_incomplete_error(
    *,
    call_id: str,
    hook: _LedgerAttemptHook,
) -> LLMAccountingError:
    return LLMAccountingError(
        "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE",
        "accounted online execution returned or failed with an unsettled physical attempt",
        details={
            "llm_call_id": call_id,
            "before_dispatch_count": hook.before_dispatch_count,
            "dispatched_attempt_count": hook.attempt_count,
        },
    )


def _mark_scene_integrity_blocked(session: Session, scene_id: str | None) -> None:
    if scene_id is None:
        return
    session.execute(
        update(SceneRunState)
        .where(SceneRunState.scene_id == scene_id)
        .values(run_execution_status=ACCOUNTING_INTEGRITY_BLOCKED_STATUS)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    _expire_cached_scene_accounting_state(session, scene_id)


def _assert_execution_step_available(session: Session, context: LLMCallContext) -> None:
    if not (context.execution_id and context.execution_step_key):
        return
    existing_step_calls = list(
        session.scalars(
            select(LlmCall)
            .where(
                LlmCall.execution_id == context.execution_id,
                LlmCall.execution_step_key == context.execution_step_key,
            )
            .order_by(LlmCall.created_at)
        )
    )
    for existing_call in existing_step_calls:
        if (
            existing_call.accounting_status in {"released", "rejected"}
            and existing_call.request_dispatched_at is None
        ):
            continue
        session.rollback()
        raise _execution_step_conflict(existing_call)


def record_rejected_call(
    session: Session,
    request: LLMRequest | None,
    context: LLMCallContext,
    rejection: LLMAccountingRejected,
    *,
    llm_call_id: str | None = None,
    prompt_hash: str | None = None,
    request_payload_summary: dict[str, Any] | None = None,
    response_payload_summary: dict[str, Any] | None = None,
) -> str:
    """Persist a local, provably pre-dispatch rejection with zero child attempts."""
    if not isinstance(rejection, LLMAccountingRejected):
        raise TypeError("record_rejected_call requires LLMAccountingRejected")
    session.commit()
    call_id = llm_call_id or f"llmcall_{uuid.uuid4().hex}"
    _begin_claim_transaction(session)
    _assert_run_job_running(session, context)
    _assert_execution_step_available(session, context)
    if session.get(LlmCall, call_id) is not None:
        session.rollback()
        raise LLMAccountingError(
            "LLM_ACCOUNTING_CALL_EXISTS",
            f"llm call {call_id} already exists",
        )
    request_summary = _request_summary(request) if request is not None else {}
    if request_payload_summary:
        request_summary.update(request_payload_summary)
    request_summary[ACCOUNTING_EXECUTION_MODE_KEY] = context.provider_execution_mode
    request_summary = sanitize_audit_summary(request_summary)
    response_summary = sanitize_audit_summary(response_payload_summary or {
        "message": str(rejection),
        "details": dict(rejection.details or {}),
        "retryable": False,
    })
    session.add(
        LlmCall(
            llm_call_id=call_id,
            provider=request.provider if request is not None else None,
            provider_id=request.provider_id if request is not None else None,
            account_id=request.account_id if request is not None else None,
            model=request.model if request is not None else None,
            node_id=context.node_id,
            reasoning_level=request.reasoning_level if request is not None else None,
            credential_mode=request.credential_mode if request is not None else None,
            prompt_hash=_prompt_hash(request) if request is not None else prompt_hash,
            step=context.step,
            project_id=context.project_id,
            scene_id=context.scene_id,
            chapter_id=context.chapter_id,
            request_payload_summary=request_summary,
            response_payload_summary=response_summary,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0,
            error_code=rejection.code,
            scope_type=context.scope_type,
            scope_id=context.scope_id,
            run_job_id=context.run_job_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            estimated_tokens=0,
            reserved_tokens=0,
            budget_charged_tokens=0,
            usage_is_estimate=True,
            accounting_status="rejected",
            settled_at=utcnow(),
        )
    )
    session.commit()
    return call_id


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


def execute_accounted_call(
    session: Session,
    client: object,
    request: LLMRequest,
    context: LLMCallContext,
    *,
    llm_call_id: str | None = None,
    _lifecycle_observer: Callable[[str, str], None] | None = None,
) -> LLMResponse:
    """Execute the single production provider boundary and durably account it."""

    # Pending business state is the prerequisite for the call and must survive
    # any later provider or post-processing failure.
    session.commit()
    call_id = llm_call_id or f"llmcall_{uuid.uuid4().hex}"
    request_estimate = estimate_request_usage(request)
    if context.provider_execution_mode == "online" and context.scope_type == "scene":
        from novel_system.services import scene_budget

        scene_budget.ensure_scene_budget_initialized(
            session,
            context.scene_id or context.scope_id,
        )
    _begin_claim_transaction(session)
    _assert_run_job_running(session, context)
    _assert_execution_step_available(session, context)
    if session.get(LlmCall, call_id) is not None:
        session.rollback()
        raise LLMAccountingError(
            "LLM_ACCOUNTING_CALL_EXISTS",
            f"llm call {call_id} already exists",
        )
    parent = LlmCall(
        llm_call_id=call_id,
        provider=request.provider,
        provider_id=request.provider_id,
        account_id=request.account_id,
        model=request.model,
        node_id=context.node_id,
        reasoning_level=request.reasoning_level,
        credential_mode=request.credential_mode,
        prompt_hash=_prompt_hash(request),
        step=context.step,
        project_id=context.project_id,
        scene_id=context.scene_id,
        chapter_id=context.chapter_id,
        request_payload_summary=sanitize_audit_summary(
            {
                **_request_summary(request),
                ACCOUNTING_EXECUTION_MODE_KEY: context.provider_execution_mode,
            }
        ),
        scope_type=context.scope_type,
        scope_id=context.scope_id,
        run_job_id=context.run_job_id,
        execution_id=context.execution_id,
        execution_step_key=context.execution_step_key,
        estimated_tokens=0,
        reserved_tokens=0,
        budget_charged_tokens=0,
        usage_is_estimate=True,
        accounting_status="reserved",
    )
    session.add(parent)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if context.execution_id and context.execution_step_key:
            _assert_execution_step_available(session, context)
            raise LLMAccountingError(
                "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
                "this execution step lost a concurrent durable claim",
                details={
                    "execution_id": context.execution_id,
                    "execution_step_key": context.execution_step_key,
                },
            ) from exc
        if session.get(LlmCall, call_id) is not None:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_CALL_EXISTS",
                f"llm call {call_id} already exists",
            ) from exc
        raise

    hook = _LedgerAttemptHook(
        session,
        call_id=call_id,
        context=context,
        lifecycle_observer=_lifecycle_observer,
    )
    started_at = time.perf_counter()
    online_capability_invoked = False
    response: object | None = None
    try:
        if context.provider_execution_mode == "offline_deterministic":
            if not isinstance(client, OfflineDeterministicExecution):
                raise LLMAccountingRejected(
                    "LLM_OFFLINE_CAPABILITY_UNSUPPORTED",
                    "offline deterministic mode requires the explicit offline execution capability",
                )
            response = _validate_offline_response(
                client.generate_offline_deterministic(request)
            )
        else:
            if not isinstance(client, OnlineAccountedExecution):
                raise LLMAccountingRejected(
                    "LLM_ACCOUNTING_HOOK_UNSUPPORTED",
                    "online provider clients must implement the explicit accounted execution capability",
                )
            _reject_integrity_blocked_scene(session, context.scene_id)
            online_capability_invoked = True
            response = client.generate_accounted(request, accounting_hook=hook)
            if hook.attempt_count == 0:
                raise LLMAccountingError(
                    "LLM_ACCOUNTING_HOOK_NOT_INVOKED",
                    "online provider client returned without forwarding the accounting hook",
                )
            if _has_open_attempt(session, call_id):
                raise _lifecycle_incomplete_error(call_id=call_id, hook=hook)
        response = replace(response, llm_call_id=call_id)

        if context.provider_execution_mode == "offline_deterministic":
            _settle_without_physical_attempt(
                session,
                call_id=call_id,
                request=request,
                response=response,
                request_estimate=request_estimate,
            )
        _finalize_parent_success(
            session,
            call_id=call_id,
            response=response,
            latency_ms=_elapsed_ms(started_at),
        )
        parent = session.get(LlmCall, call_id)
        assert parent is not None
        if parent.accounting_status == "usage_exceeds_reservation":
            offending_attempt = session.scalar(
                select(LlmCallAttempt)
                .where(
                    LlmCallAttempt.llm_call_id == call_id,
                    LlmCallAttempt.accounting_status == "usage_exceeds_reservation",
                )
                .order_by(LlmCallAttempt.provider_attempt_no.desc())
                .limit(1)
            )
            assert offending_attempt is not None
            usage_overage_tokens = (
                offending_attempt.total_tokens - offending_attempt.reserved_tokens
            )
            raise LLMAccountingError(
                "LLM_USAGE_EXCEEDS_RESERVATION",
                "provider usage exceeded the durable reservation; response delivery is blocked",
                details={
                    "llm_call_id": call_id,
                    "execution_id": parent.execution_id,
                    "execution_step_key": parent.execution_step_key,
                    "actual_tokens": offending_attempt.total_tokens,
                    "reserved_tokens": offending_attempt.reserved_tokens,
                    "attempt_id": offending_attempt.attempt_id,
                    "provider_attempt_no": offending_attempt.provider_attempt_no,
                    "attempt_actual_tokens": offending_attempt.total_tokens,
                    "attempt_reserved_tokens": offending_attempt.reserved_tokens,
                    "usage_overage_tokens": usage_overage_tokens,
                    "parent_actual_tokens": parent.total_tokens,
                    "parent_reserved_tokens": parent.reserved_tokens,
                },
            )
        return response
    except Exception as exc:
        error = exc
        lifecycle_incomplete = (
            context.provider_execution_mode == "online"
            and online_capability_invoked
            and hook.before_dispatch_count > 0
            and _has_open_attempt(session, call_id)
        )
        if lifecycle_incomplete:
            if not (
                isinstance(exc, LLMAccountingError)
                and exc.code == "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE"
            ):
                error = _lifecycle_incomplete_error(call_id=call_id, hook=hook)
        elif (
            context.provider_execution_mode == "online"
            and online_capability_invoked
            and hook.before_dispatch_count == 0
        ):
            error = _record_unknown_dispatch(
                session,
                call_id=call_id,
                context=context,
                request=request,
                request_estimate=request_estimate,
                response=response,
                error=exc,
                latency_ms=_elapsed_ms(started_at),
            )
        _finalize_parent_failure(
            session,
            call_id=call_id,
            request_estimate=request_estimate,
            error=error,
            latency_ms=_elapsed_ms(started_at),
            response=response if lifecycle_incomplete and isinstance(response, LLMResponse) else None,
            request=request if lifecycle_incomplete else None,
        )
        if lifecycle_incomplete:
            _mark_scene_integrity_blocked(session, context.scene_id)
        if error is not exc:
            raise error from exc
        raise


class _LedgerAttemptHook:
    def __init__(
        self,
        session: Session,
        *,
        call_id: str,
        context: LLMCallContext,
        lifecycle_observer: Callable[[str, str], None] | None = None,
    ) -> None:
        self._session = session
        self._call_id = call_id
        self._context = context
        self._lifecycle_observer = lifecycle_observer
        self.before_dispatch_count = 0
        self.attempt_count = 0

    def before_dispatch(self, *, request: LLMRequest, dispatch_kind: LLMDispatchKind) -> object:
        self.before_dispatch_count += 1
        estimate = estimate_request_usage(request)
        ordinal = self.attempt_count
        attempt_id = f"llmattempt_{uuid.uuid4().hex}"
        _begin_claim_transaction(self._session)
        _assert_run_job_running(self._session, self._context)
        _enforce_global_llm_quotas(self._session, self._context, estimate)
        scene_fence_tokens = _reserve_scene_capacity(
            self._session,
            self._context.scene_id,
            estimate.reserved_tokens,
        )
        attempt_reserved_tokens = scene_fence_tokens or estimate.reserved_tokens

        attempt = LlmCallAttempt(
            attempt_id=attempt_id,
            llm_call_id=self._call_id,
            provider_attempt_no=ordinal,
            dispatch_kind=dispatch_kind,
            request_max_output_tokens=request.max_output_tokens,
            estimated_tokens=estimate.estimated_tokens,
            reserved_tokens=attempt_reserved_tokens,
            budget_charged_tokens=0,
            usage_is_estimate=True,
            accounting_status="reserved",
        )
        self._session.add(attempt)
        parent = self._session.get(LlmCall, self._call_id)
        assert parent is not None
        parent.estimated_tokens += estimate.estimated_tokens
        parent.reserved_tokens += attempt_reserved_tokens
        # This commit is the durable node-claim linearization point.  If it wins
        # before an author cancellation, this one provider attempt is allowed to
        # settle; cancellation then fences the next node.  The later dispatched
        # marker intentionally remains a separate crash-recovery phase.
        self._session.commit()  # reservation transaction
        _expire_cached_scene_accounting_state(self._session, self._context.scene_id)
        self._observe("reservation_committed", attempt_id)

        if scene_fence_tokens is not None and not _consume_scene_provider_attempt(
            self._session,
            self._context.scene_id,
        ):
            self._session.rollback()
            gate_error = _scene_gate_error(
                self._session,
                str(self._context.scene_id),
                reserved_tokens=0,
                allow_existing_fence=True,
            )
            _release_scene_reservation(
                self._session,
                self._context.scene_id,
                scene_fence_tokens,
            )
            attempt = self._session.get(LlmCallAttempt, attempt_id)
            assert attempt is not None
            attempt.accounting_status = "rejected"
            attempt.settled_at = utcnow()
            attempt.error_code = gate_error.code
            attempt.error_text = audit_error_text(str(gate_error), error_code=gate_error.code)
            _aggregate_parent(self._session, self._call_id)
            self._session.commit()
            _expire_cached_scene_accounting_state(self._session, self._context.scene_id)
            raise gate_error

        dispatched_at = utcnow()
        attempt = self._session.get(LlmCallAttempt, attempt_id)
        assert attempt is not None
        attempt.request_dispatched_at = dispatched_at
        parent = self._session.get(LlmCall, self._call_id)
        assert parent is not None
        parent.request_dispatched_at = parent.request_dispatched_at or dispatched_at
        self._session.commit()  # dispatch transaction, immediately before POST
        _expire_cached_scene_accounting_state(self._session, self._context.scene_id)
        self._observe("dispatch_committed", attempt_id)
        self.attempt_count += 1
        return attempt_id

    def after_response(
        self,
        handle: object,
        *,
        request: LLMRequest,
        response: LLMResponse,
        latency_ms: int,
    ) -> None:
        usage = normalize_response_usage(response, request)
        self._settle_attempt(
            str(handle),
            usage=usage,
            provider_request_id=response.request_id,
            latency_ms=latency_ms,
            error_code=None,
            error_text=None,
            succeeded=True,
        )

    def after_error(
        self,
        handle: object,
        *,
        request: LLMRequest,
        error: BaseException,
        raw_response: dict[str, Any] | None,
        provider_request_id: str | None,
        latency_ms: int,
    ) -> None:
        usage = _usage_for_failed_attempt(request, raw_response)
        self._settle_attempt(
            str(handle),
            usage=usage,
            provider_request_id=provider_request_id,
            latency_ms=latency_ms,
            error_code=getattr(error, "code", error.__class__.__name__),
            error_text=str(error),
            succeeded=False,
        )

    def _observe(self, stage: str, attempt_id: str) -> None:
        if self._lifecycle_observer is not None:
            self._lifecycle_observer(stage, attempt_id)

    def _settle_attempt(
        self,
        attempt_id: str,
        *,
        usage: NormalizedUsage,
        provider_request_id: str | None,
        latency_ms: int,
        error_code: str | None,
        error_text: str | None,
        succeeded: bool,
    ) -> None:
        error_text = audit_error_text(error_text, error_code=error_code)
        provider_request_id = fingerprint_identifier(provider_request_id)
        attempt = self._session.get(LlmCallAttempt, attempt_id)
        assert attempt is not None
        reserved_tokens = int(attempt.reserved_tokens or 0)
        charged = min(usage.total_tokens, reserved_tokens)
        overage_tokens = usage.total_tokens - reserved_tokens
        exceeds = overage_tokens > 0 and _usage_overage_is_fenced(
            self._session, self._context.scene_id
        )
        if overage_tokens > 0 and not exceeds:
            logger.warning(
                "llm accounting: provider usage %d exceeded the reservation %d by %d tokens "
                "on call %s (attempt %s); no fence is armed, so the response is delivered "
                "and settled at actual usage",
                usage.total_tokens,
                reserved_tokens,
                overage_tokens,
                self._call_id,
                attempt_id,
            )
        target_status = (
            "usage_exceeds_reservation" if exceeds else ("settled" if succeeded else "failed")
        )

        def callback_matches(row: LlmCallAttempt) -> bool:
            return bool(
                row.accounting_status == target_status
                and row.provider_request_id == provider_request_id
                and row.prompt_tokens == usage.prompt_tokens
                and row.completion_tokens == usage.completion_tokens
                and row.total_tokens == usage.total_tokens
                and row.budget_charged_tokens == charged
                and row.usage_is_estimate == usage.usage_is_estimate
                and row.latency_ms == max(0, latency_ms)
                and row.error_code == error_code
                and row.error_text == error_text
            )

        if attempt.accounting_status != "reserved":
            if callback_matches(attempt):
                return
            raise LLMAccountingError(
                "LLM_ACCOUNTING_ATTEMPT_CALLBACK_CONFLICT",
                "provider callback conflicts with the durable terminal attempt",
                details={
                    "attempt_id": attempt_id,
                    "existing_status": attempt.accounting_status,
                    "callback_status": target_status,
                },
            )

        changed = self._session.execute(
            update(LlmCallAttempt)
            .where(
                LlmCallAttempt.attempt_id == attempt_id,
                LlmCallAttempt.accounting_status == "reserved",
            )
            .values(
                provider_request_id=provider_request_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                budget_charged_tokens=charged,
                usage_is_estimate=usage.usage_is_estimate,
                accounting_status=target_status,
                settled_at=utcnow(),
                latency_ms=max(0, latency_ms),
                error_code=error_code,
                error_text=error_text,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            # A concurrent startup reconciler or duplicate provider callback
            # won the terminal transition.  Re-read after rollback and accept
            # only a byte-for-byte equivalent callback.
            self._session.rollback()
            terminal = self._session.get(LlmCallAttempt, attempt_id)
            if terminal is not None and callback_matches(terminal):
                return
            raise LLMAccountingError(
                "LLM_ACCOUNTING_ATTEMPT_CALLBACK_CONFLICT",
                "provider callback lost the durable terminal transition",
                details={
                    "attempt_id": attempt_id,
                    "existing_status": (
                        terminal.accounting_status if terminal is not None else None
                    ),
                    "callback_status": target_status,
                },
            )
        self._session.expire(attempt)
        _settle_scene_usage(
            self._session,
            self._context.scene_id,
            reserved_tokens=reserved_tokens,
            actual_tokens=usage.total_tokens,
            usage_exceeds_reservation=exceeds,
        )
        _aggregate_parent(self._session, self._call_id)
        self._session.commit()
        _expire_cached_scene_accounting_state(self._session, self._context.scene_id)


def _settle_without_physical_attempt(
    session: Session,
    *,
    call_id: str,
    request: LLMRequest,
    response: LLMResponse,
    request_estimate: RequestUsageEstimate,
) -> None:
    usage = normalize_response_usage(response, request)
    parent = session.get(LlmCall, call_id)
    assert parent is not None
    parent.prompt_tokens = usage.prompt_tokens
    parent.completion_tokens = usage.completion_tokens
    parent.total_tokens = usage.total_tokens
    parent.estimated_tokens = 0 if response.provider == "offline_deterministic" else request_estimate.estimated_tokens
    parent.reserved_tokens = 0 if response.provider == "offline_deterministic" else max(
        request_estimate.reserved_tokens,
        usage.total_tokens,
    )
    parent.budget_charged_tokens = usage.total_tokens
    parent.latency_ms = 0
    parent.usage_is_estimate = usage.usage_is_estimate
    session.commit()


def _aggregate_parent(session: Session, call_id: str) -> None:
    parent = session.get(LlmCall, call_id)
    assert parent is not None
    attempts = list(
        session.scalars(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.llm_call_id == call_id)
            .order_by(LlmCallAttempt.provider_attempt_no)
        )
    )
    parent.estimated_tokens = sum(row.estimated_tokens for row in attempts)
    parent.reserved_tokens = sum(row.reserved_tokens for row in attempts)
    parent.budget_charged_tokens = sum(row.budget_charged_tokens for row in attempts)
    parent.prompt_tokens = sum(row.prompt_tokens for row in attempts)
    parent.completion_tokens = sum(row.completion_tokens for row in attempts)
    parent.total_tokens = sum(row.total_tokens for row in attempts)
    parent.latency_ms = sum(row.latency_ms for row in attempts)
    parent.usage_is_estimate = any(row.usage_is_estimate for row in attempts)


def _usage_overage_tokens(session: Session, call_id: str) -> int:
    return sum(
        max(0, total_tokens - reserved_tokens)
        for total_tokens, reserved_tokens in session.execute(
            select(LlmCallAttempt.total_tokens, LlmCallAttempt.reserved_tokens).where(
                LlmCallAttempt.llm_call_id == call_id
            )
        )
    )


def _finalize_parent_success(
    session: Session,
    *,
    call_id: str,
    response: LLMResponse,
    latency_ms: int,
) -> None:
    parent = session.get(LlmCall, call_id)
    assert parent is not None
    parent.provider = response.provider
    parent.model = response.model
    parent.native_reasoning_json = (
        sanitize_audit_summary(response.native_reasoning)
        if response.native_reasoning is not None
        else None
    )
    usage_overage_tokens = _usage_overage_tokens(session, call_id)
    parent.response_payload_summary = sanitize_audit_summary(
        {
            "request_id": response.request_id,
            "finish_reason": response.finish_reason,
            "text_chars": len(response.text),
            "usage_present": response.usage_present,
            "usage_complete": response.usage_complete,
            "usage_overage_tokens": usage_overage_tokens,
        }
    )
    parent.finish_reason = response.finish_reason
    # Parent latency is the strict aggregate of physical-attempt rows. Wall-clock
    # orchestration overhead is not a provider attempt and must not distort lineage.
    parent.settled_at = utcnow()
    parent.accounting_status = (
        "usage_exceeds_reservation"
        if _has_exceeded_attempt(session, call_id)
        else "settled"
    )
    session.commit()


def _finalize_parent_failure(
    session: Session,
    *,
    call_id: str,
    request_estimate: RequestUsageEstimate,
    error: BaseException,
    latency_ms: int,
    response: LLMResponse | None = None,
    request: LLMRequest | None = None,
) -> None:
    session.rollback()
    parent = session.get(LlmCall, call_id)
    if parent is None:
        return
    _settle_open_attempts_for_failure(
        session,
        parent=parent,
        error=error,
        response=response,
        request=request,
    )
    attempts = list(session.scalars(select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == call_id)))
    rejected_before_dispatch = isinstance(error, LLMAccountingRejected) and not any(
        attempt.request_dispatched_at is not None for attempt in attempts
    )
    if attempts:
        _aggregate_parent(session, call_id)
    elif rejected_before_dispatch:
        parent.estimated_tokens = 0
        parent.reserved_tokens = 0
        parent.budget_charged_tokens = 0
        parent.prompt_tokens = 0
        parent.completion_tokens = 0
        parent.total_tokens = 0
        parent.usage_is_estimate = True
    else:
        parent.estimated_tokens = request_estimate.estimated_tokens
        parent.reserved_tokens = request_estimate.reserved_tokens
        parent.budget_charged_tokens = min(
            request_estimate.estimated_tokens,
            request_estimate.reserved_tokens,
        )
        parent.prompt_tokens = request_estimate.estimated_input_tokens
        parent.completion_tokens = request_estimate.estimated_output_tokens
        parent.total_tokens = request_estimate.estimated_tokens
        parent.usage_is_estimate = True
    parent.error_code = getattr(error, "code", error.__class__.__name__)
    if not attempts:
        parent.latency_ms = (
            0
            if rejected_before_dispatch
            else max(parent.latency_ms or 0, latency_ms)
        )
    parent.settled_at = utcnow()
    usage_overage_tokens = _usage_overage_tokens(session, call_id)
    if usage_overage_tokens:
        summary = dict(parent.response_payload_summary or {})
        summary["usage_overage_tokens"] = usage_overage_tokens
        parent.response_payload_summary = sanitize_audit_summary(summary)
    # The blocking status follows the attempts (fenced overage only); an
    # unfenced overage is audit data on an otherwise ordinary failure.
    if _has_exceeded_attempt(session, call_id):
        parent.accounting_status = "usage_exceeds_reservation"
    else:
        parent.accounting_status = "rejected" if rejected_before_dispatch else "failed"
    session.commit()
    _expire_cached_scene_accounting_state(session, parent.scene_id)


def _settle_open_attempts_for_failure(
    session: Session,
    *,
    parent: LlmCall,
    error: BaseException,
    response: LLMResponse | None,
    request: LLMRequest | None,
) -> None:
    open_attempts = list(
        session.scalars(
            select(LlmCallAttempt).where(
                LlmCallAttempt.llm_call_id == parent.llm_call_id,
                LlmCallAttempt.accounting_status == "reserved",
            )
        )
    )
    dispatched_attempts = [
        attempt for attempt in open_attempts if attempt.request_dispatched_at is not None
    ]
    response_attempt_id = (
        max(dispatched_attempts, key=lambda attempt: attempt.provider_attempt_no).attempt_id
        if response is not None and request is not None and dispatched_attempts
        else None
    )
    response_usage = (
        normalize_response_usage(response, request)
        if response is not None and request is not None and response_attempt_id is not None
        else None
    )
    for attempt in open_attempts:
        if attempt.request_dispatched_at is None:
            attempt.accounting_status = "released"
            attempt.settled_at = utcnow()
            _release_scene_reservation(session, parent.scene_id, attempt.reserved_tokens)
            continue
        if response_usage is not None and attempt.attempt_id == response_attempt_id:
            usage = response_usage
        else:
            completion_tokens = min(attempt.request_max_output_tokens, attempt.estimated_tokens)
            usage = NormalizedUsage(
                prompt_tokens=max(0, attempt.estimated_tokens - completion_tokens),
                completion_tokens=completion_tokens,
                total_tokens=attempt.estimated_tokens,
                usage_is_estimate=True,
            )
        exceeds = usage.total_tokens > attempt.reserved_tokens and _usage_overage_is_fenced(
            session, parent.scene_id
        )
        attempt.prompt_tokens = usage.prompt_tokens
        attempt.completion_tokens = usage.completion_tokens
        attempt.total_tokens = usage.total_tokens
        attempt.budget_charged_tokens = min(usage.total_tokens, attempt.reserved_tokens)
        attempt.usage_is_estimate = usage.usage_is_estimate
        attempt.accounting_status = "usage_exceeds_reservation" if exceeds else "failed"
        attempt.error_code = getattr(error, "code", error.__class__.__name__)
        attempt.error_text = audit_error_text(
            str(error),
            error_code=getattr(error, "code", error.__class__.__name__),
        )
        attempt.settled_at = utcnow()
        _settle_scene_usage(
            session,
            parent.scene_id,
            reserved_tokens=attempt.reserved_tokens,
            actual_tokens=usage.total_tokens,
            usage_exceeds_reservation=exceeds,
        )


def mark_postprocess_failure(
    session: Session,
    llm_call_id: str,
    *,
    error_code: str,
    error_text: str | None = None,
) -> None:
    """Mark caller-side parsing/validation failure on the existing parent row."""

    parent = session.get(LlmCall, llm_call_id)
    if parent is None:
        raise KeyError(f"unknown llm call {llm_call_id}")
    usage_overage_tokens = _usage_overage_tokens(session, llm_call_id)
    parent.accounting_status = (
        "usage_exceeds_reservation"
        if _has_exceeded_attempt(session, llm_call_id)
        else "failed"
    )
    parent.error_code = error_code
    summary = dict(parent.response_payload_summary or {})
    if usage_overage_tokens:
        summary["usage_overage_tokens"] = usage_overage_tokens
    if error_text:
        summary["postprocess_error"] = error_text
    parent.response_payload_summary = sanitize_audit_summary(summary)
    parent.settled_at = parent.settled_at or utcnow()
    session.commit()


def _parse_accounting_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def recover_stale_legacy_reservations(
    session: Session,
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, list[str]]:
    """Reconcile abandoned non-scene reservations after process startup.

    Scene/chapter execution has its own durable job lease and checkpoint
    recovery, so this sweep deliberately excludes every scene-scoped or
    run-job-owned call.  The remaining legacy calls have no heartbeat; a
    conservative configurable age is therefore the only safe crash signal.

    Each candidate parent is locked after discovery.  SQLite uses the same
    ``BEGIN IMMEDIATE`` serialization as the provider claim path, while
    databases with row locks use ``FOR UPDATE``.  Re-reading all open attempts
    under that lock makes duplicate/concurrent startup sweeps idempotent and
    prevents a fresh retry on the same parent from being reclaimed.
    """

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    configured_ttl = (
        load_llm_accounting_runtime().reservation_recovery_ttl_seconds
        if ttl_seconds is None
        else int(ttl_seconds)
    )
    ttl = max(1, configured_ttl)
    cutoff = current - timedelta(seconds=ttl)
    cutoff_iso = cutoff.isoformat()

    candidate_ids = list(
        session.scalars(
            select(LlmCall.llm_call_id)
            .join(LlmCallAttempt, LlmCallAttempt.llm_call_id == LlmCall.llm_call_id)
            .where(
                LlmCall.accounting_status == "reserved",
                LlmCall.scene_id.is_(None),
                LlmCall.run_job_id.is_(None),
                LlmCall.scope_type != "scene",
                LlmCallAttempt.accounting_status == "reserved",
                LlmCallAttempt.created_at <= cutoff_iso,
            )
            .distinct()
            .order_by(LlmCall.llm_call_id)
        )
    )
    session.rollback()

    released: list[str] = []
    failed: list[str] = []
    skipped_fresh: list[str] = []
    for call_id in candidate_ids:
        session.commit()
        _begin_claim_transaction(session)
        # Settlement transitions lock an attempt before aggregating its
        # parent.  Preserve that lock order here to avoid a PostgreSQL
        # attempt<->parent deadlock with a late provider callback.
        attempts = list(
            session.scalars(
                select(LlmCallAttempt)
                .where(LlmCallAttempt.llm_call_id == call_id)
                .order_by(LlmCallAttempt.provider_attempt_no)
                .with_for_update()
            )
        )
        open_attempts = [
            attempt for attempt in attempts if attempt.accounting_status == "reserved"
        ]
        if not open_attempts:
            session.rollback()
            continue
        parent = session.scalar(
            select(LlmCall)
            .where(
                LlmCall.llm_call_id == call_id,
                LlmCall.accounting_status == "reserved",
                LlmCall.scene_id.is_(None),
                LlmCall.run_job_id.is_(None),
                LlmCall.scope_type != "scene",
            )
            .with_for_update()
        )
        if parent is None:
            session.rollback()
            continue
        open_timestamps = [
            _parse_accounting_timestamp(attempt.created_at) for attempt in open_attempts
        ]
        if any(timestamp is None or timestamp > cutoff for timestamp in open_timestamps):
            skipped_fresh.append(call_id)
            session.rollback()
            continue

        terminal_at = utcnow()
        any_dispatched = any(
            attempt.request_dispatched_at is not None for attempt in attempts
        )
        for attempt in open_attempts:
            if attempt.request_dispatched_at is None:
                attempt.accounting_status = "released"
                attempt.budget_charged_tokens = 0
                attempt.settled_at = terminal_at
                continue

            estimated = max(0, int(attempt.estimated_tokens or 0))
            completion = min(
                max(0, int(attempt.request_max_output_tokens or 0)),
                estimated,
            )
            attempt.prompt_tokens = max(0, estimated - completion)
            attempt.completion_tokens = completion
            attempt.total_tokens = estimated
            attempt.budget_charged_tokens = min(
                estimated,
                max(0, int(attempt.reserved_tokens or 0)),
            )
            attempt.usage_is_estimate = True
            attempt.accounting_status = "failed"
            attempt.error_code = "RUN_CHECKPOINT_OUTPUT_MISSING"
            attempt.error_text = (
                "provider request was dispatched but no durable output checkpoint exists"
            )
            attempt.settled_at = terminal_at

        _aggregate_parent(session, call_id)
        parent.settled_at = terminal_at
        if any_dispatched:
            parent.accounting_status = "failed"
            parent.error_code = "RUN_CHECKPOINT_OUTPUT_MISSING"
            failed.append(call_id)
        else:
            parent.accounting_status = "released"
            parent.error_code = None
            released.append(call_id)
        session.commit()

    return {
        "released_call_ids": released,
        "failed_call_ids": failed,
        "fresh_call_ids_skipped": skipped_fresh,
    }


def recover_incomplete_call(session: Session, llm_call_id: str) -> AccountingRecoveryResult:
    """Resolve a call left ``reserved`` by process interruption.

    An undispatched reservation is free to release.  Once dispatch was durably
    recorded, provider outcome is unknowable, so recovery charges the estimate
    and blocks automatic resend.
    """

    session.commit()
    parent = session.get(LlmCall, llm_call_id)
    if parent is None:
        raise KeyError(f"unknown llm call {llm_call_id}")
    attempts = list(
        session.scalars(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.llm_call_id == llm_call_id)
            .order_by(LlmCallAttempt.provider_attempt_no)
        )
    )
    if parent.accounting_status != "reserved":
        raise LLMAccountingError(
            "LLM_ACCOUNTING_CALL_NOT_RECOVERABLE",
            f"llm call {llm_call_id} is already {parent.accounting_status}",
        )

    dispatched = any(attempt.request_dispatched_at is not None for attempt in attempts)
    if not dispatched:
        for attempt in attempts:
            if attempt.accounting_status != "reserved":
                continue
            attempt.accounting_status = "released"
            attempt.budget_charged_tokens = 0
            attempt.settled_at = utcnow()
            _release_scene_reservation(session, parent.scene_id, attempt.reserved_tokens)
        _aggregate_parent(session, llm_call_id)
        parent.accounting_status = "released"
        parent.error_code = None
        parent.settled_at = utcnow()
        session.commit()
        _expire_cached_scene_accounting_state(session, parent.scene_id)
        return AccountingRecoveryResult(status="released", error_code=None, may_retry=True)

    for attempt in attempts:
        if attempt.accounting_status != "reserved":
            continue
        if attempt.request_dispatched_at is None:
            attempt.accounting_status = "released"
            attempt.budget_charged_tokens = 0
            attempt.settled_at = utcnow()
            _release_scene_reservation(session, parent.scene_id, attempt.reserved_tokens)
            continue
        charged = min(attempt.estimated_tokens, attempt.reserved_tokens)
        completion_tokens = min(attempt.request_max_output_tokens, attempt.estimated_tokens)
        attempt.prompt_tokens = max(0, attempt.estimated_tokens - completion_tokens)
        attempt.completion_tokens = completion_tokens
        attempt.total_tokens = attempt.estimated_tokens
        attempt.budget_charged_tokens = charged
        attempt.usage_is_estimate = True
        attempt.accounting_status = "failed"
        attempt.error_code = "RUN_CHECKPOINT_OUTPUT_MISSING"
        attempt.error_text = "provider request was dispatched but no durable output checkpoint exists"
        attempt.settled_at = utcnow()
        _settle_scene_usage(
            session,
            parent.scene_id,
            reserved_tokens=attempt.reserved_tokens,
            actual_tokens=attempt.estimated_tokens,
            usage_exceeds_reservation=False,
        )

    _aggregate_parent(session, llm_call_id)
    parent.accounting_status = "failed"
    parent.error_code = "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent.settled_at = utcnow()
    session.commit()
    _expire_cached_scene_accounting_state(session, parent.scene_id)
    return AccountingRecoveryResult(
        status="failed",
        error_code="RUN_CHECKPOINT_OUTPUT_MISSING",
        may_retry=False,
    )


def _usage_for_failed_attempt(
    request: LLMRequest,
    raw_response: dict[str, Any] | None,
) -> NormalizedUsage:
    raw_usage = _extract_raw_usage(raw_response)
    actual = _normalize_raw_usage(raw_usage)
    if actual is not None:
        return actual
    estimate = estimate_request_usage(request)
    return NormalizedUsage(
        prompt_tokens=estimate.estimated_input_tokens,
        completion_tokens=estimate.estimated_output_tokens,
        total_tokens=estimate.estimated_tokens,
        usage_is_estimate=True,
    )


def _normalize_raw_usage(raw_usage: dict[str, Any] | None) -> NormalizedUsage | None:
    if raw_usage is None:
        return None
    key_sets = (
        ("input_tokens", "output_tokens", "total_tokens"),
        ("prompt_tokens", "completion_tokens", "total_tokens"),
        ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"),
        ("prompt_eval_count", "eval_count", None),
    )
    for prompt_key, completion_key, total_key in key_sets:
        if prompt_key not in raw_usage and completion_key not in raw_usage:
            continue
        prompt = _usage_int(raw_usage.get(prompt_key))
        completion = _usage_int(raw_usage.get(completion_key))
        if prompt is None or completion is None:
            return None
        expected_total = prompt + completion
        if total_key is not None and total_key in raw_usage:
            total = _usage_int(raw_usage.get(total_key))
            if total is None or total != expected_total:
                return None
        return NormalizedUsage(prompt, completion, expected_total, False)
    return None


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or int(value) != value:
        return None
    return int(value)


def _extract_raw_usage(body: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    for key in ("usage", "usageMetadata"):
        value = body.get(key)
        if isinstance(value, dict):
            return dict(value)
    if "prompt_eval_count" in body or "eval_count" in body:
        return {
            key: body.get(key)
            for key in ("prompt_eval_count", "eval_count")
            if key in body
        }
    return None


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


def _is_explicit_zero_usage(usage: dict[str, Any]) -> bool:
    return usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _has_exceeded_attempt(session: Session, call_id: str) -> bool:
    return session.scalar(
        select(LlmCallAttempt.attempt_id)
        .where(
            LlmCallAttempt.llm_call_id == call_id,
            LlmCallAttempt.accounting_status == "usage_exceeds_reservation",
        )
        .limit(1)
    ) is not None


def _prompt_hash(request: LLMRequest) -> str:
    canonical = json.dumps(request.messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_summary(request: LLMRequest) -> dict[str, Any]:
    return {
        "message_count": len(request.messages),
        "message_chars": sum(len(str(message.get("content") or "")) for message in request.messages),
        "max_output_tokens": request.max_output_tokens,
        "response_format": request.response_format,
        "api_mode": request.api_mode,
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))
