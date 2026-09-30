"""可检查点化的咨询类产品（自动批评、事件抽取、检查点续跑……）对照它的物理尝试账本做校验（从 llm_accounting 拆出，B09-12）。

产品只认它自己那一条父调用：所有权字段逐项相同、执行模式是 online、父行数目等于物理尝试之和、
终态与期望的结局（完成 / 解析失败 / 派发前被拒 / 供应商失败）一致；失败归类只看账本证据，不看异常名。
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, LlmCallAttempt
from novel_system.services.llm_accounting_types import (
    ACCOUNTING_EXECUTION_MODE_KEY,
    LLMAccountingError,
    LLMAccountingRejected,
    LLMCallContext,
    is_llm_control_plane_failure,
    llm_failure_code,
)


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

    if expected_outcome in {"completed", "parse_failed"} and execution_mode != "online":
        # 历史的离线确定性产品（零尝试、零用量）不再是合法产品：那个执行模式已退役（B09-04）
        invalid("product was not produced by online provider execution")

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
