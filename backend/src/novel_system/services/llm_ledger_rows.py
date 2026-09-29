"""记账行的共用操作：认领事务、父行按物理尝试汇总、超预留 / 未结算尝试的查询（从 llm_accounting 拆出，B09-12）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, LlmCallAttempt


def begin_claim_transaction(session: Session) -> None:
    """Serialize the absent-row execution-step claim on file-backed SQLite."""

    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def aggregate_parent(session: Session, call_id: str) -> None:
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


def call_usage_overage_tokens(session: Session, call_id: str) -> int:
    return sum(
        max(0, total_tokens - reserved_tokens)
        for total_tokens, reserved_tokens in session.execute(
            select(LlmCallAttempt.total_tokens, LlmCallAttempt.reserved_tokens).where(
                LlmCallAttempt.llm_call_id == call_id
            )
        )
    )


def has_exceeded_attempt(session: Session, call_id: str) -> bool:
    return session.scalar(
        select(LlmCallAttempt.attempt_id)
        .where(
            LlmCallAttempt.llm_call_id == call_id,
            LlmCallAttempt.accounting_status == "usage_exceeds_reservation",
        )
        .limit(1)
    ) is not None


def has_open_attempt(session: Session, call_id: str) -> bool:
    return session.scalar(
        select(LlmCallAttempt.attempt_id)
        .where(
            LlmCallAttempt.llm_call_id == call_id,
            LlmCallAttempt.accounting_status == "reserved",
        )
        .limit(1)
    ) is not None
