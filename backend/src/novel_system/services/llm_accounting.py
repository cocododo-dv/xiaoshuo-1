"""Durable accounting boundary for provider-backed LLM calls.

The caller session is deliberately committed before provider work.  Every
physical POST then uses short reservation, dispatch, and settlement
transactions; no database write transaction is held while waiting on the
network.

This module keeps the single production boundary ``execute_accounted_call``,
its physical-attempt hook, settlement and the pre-dispatch rejection record.
The rest of the ledger lives in flat modules (2026-09-30, B09-12) and is
re-exported here for the callers that import it from this path:

* ``llm_accounting_types``: call context, usage estimates, errors, codes;
* ``llm_ledger_rows``: claim transaction, parent aggregation, row queries;
* ``llm_scene_fence``: the per-scene CAS token / attempt fence;
* ``llm_ledger_validation``: advisory-product validation and classification;
* ``llm_accounting_recovery``: startup / per-call recovery of open reservations;
* ``llm_provider_probe`` (not re-exported: it builds on this module): the
  accounted "test connection" completion probe.

``_consume_scene_provider_attempt`` and ``_elapsed_ms`` stay module globals
here on purpose: the hook and the boundary resolve them through this module,
so tests patch them on ``llm_accounting``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterRunJob, LlmCall, LlmCallAttempt, SceneRunState, utcnow
from novel_system.services.llm_accounting_recovery import (
    recover_incomplete_call,
    recover_stale_legacy_reservations,
    recover_stale_unowned_reservations,
)
from novel_system.services.llm_accounting_types import (
    ACCOUNTING_EXECUTION_MODE_KEY,
    ACCOUNTING_INTEGRITY_BLOCKED_STATUS,
    ACCOUNTING_INTEGRITY_ERROR_CODES,
    CONTROL_PLANE_ERROR_CODES,
    MESSAGE_TOKEN_OVERHEAD,
    AccountingRecoveryResult,
    LLMAccountingError,
    LLMAccountingRejected,
    LLMCallContext,
    RequestUsageEstimate,
    elapsed_ms as _elapsed_ms,
    estimate_request_usage,
    is_llm_control_plane_failure,
    llm_failure_code,
    normalize_response_usage,
    request_prompt_hash,
    summarize_request,
)
from novel_system.services.llm_audit import (
    audit_error_text,
    fingerprint_identifier,
    sanitize_audit_summary,
)
from novel_system.services.llm_ledger_rows import (
    aggregate_parent,
    begin_claim_transaction,
    call_usage_overage_tokens,
    has_exceeded_attempt,
    has_open_attempt,
)
from novel_system.services.llm_ledger_validation import (
    classify_advisory_failure,
    validate_product_call,
    validate_product_call_ledger,
)
from novel_system.services.llm_providers.base import (
    LLMDispatchKind,
    LLMRequest,
    LLMResponse,
    OnlineAccountedExecution,
)
from novel_system.services.llm_providers.usage import (
    NormalizedUsage,
    extract_raw_usage,
    normalize_raw_usage,
)
from novel_system.services.llm_scene_fence import (
    consume_scene_provider_attempt as _consume_scene_provider_attempt,
    expire_cached_scene_accounting_state,
    mark_scene_integrity_blocked,
    reject_integrity_blocked_scene,
    release_scene_reservation,
    reserve_scene_capacity,
    scene_gate_error,
    settle_scene_usage,
    usage_overage_is_fenced,
)

__all__ = [
    "ACCOUNTING_EXECUTION_MODE_KEY",
    "ACCOUNTING_INTEGRITY_BLOCKED_STATUS",
    "ACCOUNTING_INTEGRITY_ERROR_CODES",
    "CONTROL_PLANE_ERROR_CODES",
    "MESSAGE_TOKEN_OVERHEAD",
    "AccountingRecoveryResult",
    "LLMAccountingError",
    "LLMAccountingRejected",
    "LLMCallContext",
    "NormalizedUsage",
    "OnlineAccountedExecution",
    "RequestUsageEstimate",
    "classify_advisory_failure",
    "estimate_request_usage",
    "execute_accounted_call",
    "is_llm_control_plane_failure",
    "llm_failure_code",
    "mark_postprocess_failure",
    "normalize_response_usage",
    "record_rejected_call",
    "recover_incomplete_call",
    "recover_stale_legacy_reservations",
    "recover_stale_unowned_reservations",
    "validate_product_call",
    "validate_product_call_ledger",
]


logger = logging.getLogger(__name__)


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
    exceeds = usage.total_tokens > request_estimate.reserved_tokens and usage_overage_is_fenced(
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
    aggregate_parent(session, call_id)
    session.commit()
    expire_cached_scene_accounting_state(session, context.scene_id)
    return audit_error


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
    begin_claim_transaction(session)
    _assert_run_job_running(session, context)
    _assert_execution_step_available(session, context)
    if session.get(LlmCall, call_id) is not None:
        session.rollback()
        raise LLMAccountingError(
            "LLM_ACCOUNTING_CALL_EXISTS",
            f"llm call {call_id} already exists",
        )
    request_summary = summarize_request(request) if request is not None else {}
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
            prompt_hash=request_prompt_hash(request) if request is not None else prompt_hash,
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
    begin_claim_transaction(session)
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
        prompt_hash=request_prompt_hash(request),
        step=context.step,
        project_id=context.project_id,
        scene_id=context.scene_id,
        chapter_id=context.chapter_id,
        request_payload_summary=sanitize_audit_summary(
            {
                **summarize_request(request),
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
        # 上下文只能是 online（LLMCallContext 构造时就拒绝别的执行模式），这里不再复查。
        if not isinstance(client, OnlineAccountedExecution):
            raise LLMAccountingRejected(
                "LLM_ACCOUNTING_HOOK_UNSUPPORTED",
                "online provider clients must implement the explicit accounted execution capability",
            )
        reject_integrity_blocked_scene(session, context.scene_id)
        online_capability_invoked = True
        response = client.generate_accounted(request, accounting_hook=hook)
        if hook.attempt_count == 0:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_HOOK_NOT_INVOKED",
                "online provider client returned without forwarding the accounting hook",
            )
        if has_open_attempt(session, call_id):
            raise _lifecycle_incomplete_error(call_id=call_id, hook=hook)
        response = replace(response, llm_call_id=call_id)

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
            online_capability_invoked
            and hook.before_dispatch_count > 0
            and has_open_attempt(session, call_id)
        )
        if lifecycle_incomplete:
            if not (
                isinstance(exc, LLMAccountingError)
                and exc.code == "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE"
            ):
                error = _lifecycle_incomplete_error(call_id=call_id, hook=hook)
        elif online_capability_invoked and hook.before_dispatch_count == 0:
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
            mark_scene_integrity_blocked(session, context.scene_id)
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
        begin_claim_transaction(self._session)
        _assert_run_job_running(self._session, self._context)
        scene_fence_tokens = reserve_scene_capacity(
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
        expire_cached_scene_accounting_state(self._session, self._context.scene_id)
        self._observe("reservation_committed", attempt_id)

        if scene_fence_tokens is not None and not _consume_scene_provider_attempt(
            self._session,
            self._context.scene_id,
        ):
            self._session.rollback()
            gate_error = scene_gate_error(
                self._session,
                str(self._context.scene_id),
                reserved_tokens=0,
                allow_existing_fence=True,
            )
            release_scene_reservation(
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
            aggregate_parent(self._session, self._call_id)
            self._session.commit()
            expire_cached_scene_accounting_state(self._session, self._context.scene_id)
            raise gate_error

        dispatched_at = utcnow()
        attempt = self._session.get(LlmCallAttempt, attempt_id)
        assert attempt is not None
        attempt.request_dispatched_at = dispatched_at
        parent = self._session.get(LlmCall, self._call_id)
        assert parent is not None
        parent.request_dispatched_at = parent.request_dispatched_at or dispatched_at
        self._session.commit()  # dispatch transaction, immediately before POST
        expire_cached_scene_accounting_state(self._session, self._context.scene_id)
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
        exceeds = overage_tokens > 0 and usage_overage_is_fenced(
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
        settle_scene_usage(
            self._session,
            self._context.scene_id,
            reserved_tokens=reserved_tokens,
            actual_tokens=usage.total_tokens,
            usage_exceeds_reservation=exceeds,
        )
        aggregate_parent(self._session, self._call_id)
        self._session.commit()
        expire_cached_scene_accounting_state(self._session, self._context.scene_id)


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
    usage_overage_tokens = call_usage_overage_tokens(session, call_id)
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
        if has_exceeded_attempt(session, call_id)
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
        aggregate_parent(session, call_id)
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
    usage_overage_tokens = call_usage_overage_tokens(session, call_id)
    if usage_overage_tokens:
        summary = dict(parent.response_payload_summary or {})
        summary["usage_overage_tokens"] = usage_overage_tokens
        parent.response_payload_summary = sanitize_audit_summary(summary)
    # The blocking status follows the attempts (fenced overage only); an
    # unfenced overage is audit data on an otherwise ordinary failure.
    if has_exceeded_attempt(session, call_id):
        parent.accounting_status = "usage_exceeds_reservation"
    else:
        parent.accounting_status = "rejected" if rejected_before_dispatch else "failed"
    session.commit()
    expire_cached_scene_accounting_state(session, parent.scene_id)


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
            release_scene_reservation(session, parent.scene_id, attempt.reserved_tokens)
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
        exceeds = usage.total_tokens > attempt.reserved_tokens and usage_overage_is_fenced(
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
        settle_scene_usage(
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
    usage_overage_tokens = call_usage_overage_tokens(session, llm_call_id)
    parent.accounting_status = (
        "usage_exceeds_reservation"
        if has_exceeded_attempt(session, llm_call_id)
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


def _usage_for_failed_attempt(
    request: LLMRequest,
    raw_response: dict[str, Any] | None,
) -> NormalizedUsage:
    raw_usage = extract_raw_usage(raw_response)
    actual = normalize_raw_usage(raw_usage)
    if actual is not None:
        return actual
    estimate = estimate_request_usage(request)
    return NormalizedUsage(
        prompt_tokens=estimate.estimated_input_tokens,
        completion_tokens=estimate.estimated_output_tokens,
        total_tokens=estimate.estimated_tokens,
        usage_is_estimate=True,
    )
