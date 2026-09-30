"""进程中断后的记账对账：启动时收回没有主人的陈旧预留、按调用恢复卡在 reserved 的父行（从 llm_accounting 拆出，B09-12）。

已经派发出去的尝试结果不可知：按估算记账、错误码 ``RUN_CHECKPOINT_OUTPUT_MISSING``，阻止自动重发；
还没派发的直接释放。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, LlmCallAttempt, utcnow
from novel_system.env_config import reservation_recovery_ttl_seconds
from novel_system.services.llm_accounting_types import AccountingRecoveryResult, LLMAccountingError
from novel_system.services.llm_ledger_rows import aggregate_parent, begin_claim_transaction
from novel_system.services.llm_scene_fence import (
    expire_cached_scene_accounting_state,
    release_scene_reservation,
    settle_scene_usage,
)


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


def recover_stale_unowned_reservations(
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
        reservation_recovery_ttl_seconds()
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
        begin_claim_transaction(session)
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

        aggregate_parent(session, call_id)
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
            release_scene_reservation(session, parent.scene_id, attempt.reserved_tokens)
        aggregate_parent(session, llm_call_id)
        parent.accounting_status = "released"
        parent.error_code = None
        parent.settled_at = utcnow()
        session.commit()
        expire_cached_scene_accounting_state(session, parent.scene_id)
        return AccountingRecoveryResult(status="released", error_code=None, may_retry=True)

    for attempt in attempts:
        if attempt.accounting_status != "reserved":
            continue
        if attempt.request_dispatched_at is None:
            attempt.accounting_status = "released"
            attempt.budget_charged_tokens = 0
            attempt.settled_at = utcnow()
            release_scene_reservation(session, parent.scene_id, attempt.reserved_tokens)
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
        settle_scene_usage(
            session,
            parent.scene_id,
            reserved_tokens=attempt.reserved_tokens,
            actual_tokens=attempt.estimated_tokens,
            usage_exceeds_reservation=False,
        )

    aggregate_parent(session, llm_call_id)
    parent.accounting_status = "failed"
    parent.error_code = "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent.settled_at = utcnow()
    session.commit()
    expire_cached_scene_accounting_state(session, parent.scene_id)
    return AccountingRecoveryResult(
        status="failed",
        error_code="RUN_CHECKPOINT_OUTPUT_MISSING",
        may_retry=False,
    )


# 旧名（启动恢复与测试还在用）：收回的是「没有主人」的非场景预留，不只是旧版遗留的
recover_stale_legacy_reservations = recover_stale_unowned_reservations
