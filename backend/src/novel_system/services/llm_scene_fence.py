"""场景级记账闸：每次物理尝试前按场景预算做 CAS 预留、按尝试计数，结算时放回（从 llm_accounting 拆出，B09-12）。

场景 token 预算默认解除武装（哨兵额度，CAS 永远过）；设 ``NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER`` 为正数重新武装。
武装时超出预留的供应商用量会阻断交付（``usage_overage_is_fenced``）——这是 2026-09-30 之后唯一还在的闸。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, SceneRunState
from novel_system.services.llm_accounting_types import (
    ACCOUNTING_INTEGRITY_BLOCKED_STATUS,
    ACCOUNTING_INTEGRITY_ERROR_CODES,
    LLMAccountingError,
    LLMAccountingRejected,
)


def usage_overage_is_fenced(session: Session, scene_id: str | None) -> bool:
    """Whether provider usage beyond the reservation must block delivery.

    The reservation is the pre-dispatch fence input, not a promise the provider
    keeps: thinking backends report reasoning tokens inside ``completion_tokens``
    and relays routinely do not cap them under ``max_tokens`` (a 2,000-token
    classification answer came back with 11,776 completion tokens), so actual
    usage can legitimately exceed any deterministic estimate.  The only fence
    left is an armed per-scene token budget (the six env-only global quotas were
    retired on 2026-09-30, 重评 R3): while it is armed the overage means the
    fence was checked against too small a number, and the response stays
    blocked exactly as before.  With nothing armed (the single-author default)
    there is no fence to protect: the tokens are already spent, so the response
    is delivered and settled at actual usage, with the overage kept in the audit
    summary as ``usage_overage_tokens``.
    """

    if scene_id is None:
        return False
    state = session.get(SceneRunState, scene_id)
    if state is None or state.scene_token_budget is None:
        return False
    from novel_system.services import scene_budget

    return not scene_budget.is_scene_budget_disarmed(state)


def reserve_scene_capacity(
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
    error = scene_gate_error(session, scene_id, reserved_tokens=reserved_tokens)
    session.rollback()
    raise error


def scene_gate_error(
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


def consume_scene_provider_attempt(session: Session, scene_id: str | None) -> bool:
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


def release_scene_reservation(session: Session, scene_id: str | None, reserved_tokens: int) -> None:
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


def settle_scene_usage(
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


def reject_integrity_blocked_scene(session: Session, scene_id: str | None) -> None:
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


def expire_cached_scene_accounting_state(session: Session, scene_id: str | None) -> None:
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


def mark_scene_integrity_blocked(session: Session, scene_id: str | None) -> None:
    if scene_id is None:
        return
    session.execute(
        update(SceneRunState)
        .where(SceneRunState.scene_id == scene_id)
        .values(run_execution_status=ACCOUNTING_INTEGRITY_BLOCKED_STATUS)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    expire_cached_scene_accounting_state(session, scene_id)
