"""场景 token 预算（结果闭环治理 §4.6/§5.5/§5.8/§7.12，Wave 3）。

单发基线（§4.6）：对同一冻结 Bundle、同一 writer 路由执行一次 N=1 正文生成，
再执行一次确定性 Q0/Q1 与来源安全检查（本地，0 token）。武装态下关键场景端到端
上限 = ``N × 基线``（N = ``settings.scene_token_budget_multiplier``，武装默认 5）。

**默认解除武装（单作者装机）**：这道硬闸门是最后一道还常开的闸门，现已并入其余
额度族——``NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER=0``（默认）时三道额度落到有限
哨兵（见 ``UNARMED_*``），预留 CAS 闸门恒不触发，单场 run 永不被中途拦停。记账完全
不变（账本与成本看板照旧逐 token 记账）。设正整数即重新武装 ``N × 基线`` + 配置重试上限。

硬行为（§5.8，仅在武装态生效）：
- 预算耗尽后停止**新**调用，返回已有最佳稿——基线必经调用不拦但照常计数；
  可选支出（补候选 / LLM 批判 / 补丁 / near-final 重写）过 ``can_spend`` 闸。
- 预算按场景生命周期累计；自动流程不得重置（§7.12），扩容唯一入口是作者
  显式 topup（路由层留 OperationLog 审计）。
- token 口径：本 Wave 落「实际 usage 累计 + 估算判定」最小闭环；估算/实际/
  计费三口径与跨 provider 分槽归 Wave 6 成本聚合。
- 顺序管线内「先预留后发起」退化为逐次前置检查（无并发候选生成）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novel_system.accounting_contract import (
    DEFAULT_PROVIDER_ATTEMPT_BUDGET,
    PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY,
)
from novel_system.db.models import LlmCall, OperationLog, SceneRunState

_LOGGER = logging.getLogger(__name__)

BUDGET_MULTIPLIER = 5
# PromptBuilder / task_config 不可得时的保守回退（≈短场景输入 + stylize 输出上限）
FALLBACK_INPUT_TOKENS = 4000
FALLBACK_OUTPUT_TOKENS = 2400
BASELINE_WRITER_NODE = "style_draft"
LEGACY_INITIAL_ATTEMPT_BUDGET = 4

# 场景生命周期预算「解除武装」哨兵（单作者默认，见 settings.scene_token_budget_multiplier）。
# 三道额度（token / 业务重试 / provider 重试）全部落到远超任何真实场景的**有限**值——
# 预留 CAS 闸门因此恒不触发，但「有限正值」不变量与审计回放的 int64 上限（1<<63-1）仍成立。
# 记账完全不受影响：账本照旧逐次累计真实 usage，只是「拦截」退化为空操作。
# 想重新武装：设 NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER=正数（恢复 N×基线 + 配置重试上限）。
UNARMED_SCENE_TOKEN_BUDGET = 1 << 52  # ~4.5e15 token
UNARMED_ATTEMPT_BUDGET = 1 << 30  # ~1.07e9 次；远超任何真实重试，且给 topup/int64 留足余量


def _armed_scene_budget_multiplier() -> int:
    """武装态倍率（>0）；返回 0 表示解除武装（默认）。

    单作者本地装机默认不设这道硬闸门。settings 读不到时保守回退到历史默认（武装 5×），
    绝不因一次读配置失败就把安全闸门静默变成「不限」。
    """
    try:
        from novel_system.settings import get_settings

        value = int(get_settings(include_runtime_config=False).scene_token_budget_multiplier)
    except Exception:  # noqa: BLE001 — 保守回退到武装态，见 docstring
        return BUDGET_MULTIPLIER
    return value if value > 0 else 0


def _insert_scene_run_state_if_missing(session: Session, scene_id: str) -> None:
    values = {"scene_id": scene_id, "scene_status": "ready"}
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

        statement = dialect_insert(SceneRunState).values(**values).on_conflict_do_nothing(
            index_elements=[SceneRunState.scene_id]
        )
        session.execute(statement)
        session.commit()
        return
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert

        statement = dialect_insert(SceneRunState).values(**values).on_conflict_do_nothing(
            index_elements=[SceneRunState.scene_id]
        )
        session.execute(statement)
        session.commit()
        return
    try:
        with session.begin_nested():
            session.execute(insert(SceneRunState).values(**values))
        session.commit()
    except IntegrityError:
        session.rollback()


def ensure_scene_budget_initialized(
    session: Session,
    scene_id: str,
) -> SceneRunState:
    """Atomically establish and validate the immutable scene lifecycle budget.

    The estimate and provider-attempt limit are deliberately resolved here so
    callers cannot initialize the same scene with competing budget candidates.
    """
    if not str(scene_id or "").strip():
        raise ValueError("scene budget initialization requires scene_id")
    session.commit()
    # SessionLocal intentionally keeps identities across commits. Canonical
    # validation must nevertheless observe topups committed by another request.
    session.expire_all()
    _insert_scene_run_state_if_missing(session, scene_id)
    session.expire_all()

    state = session.get(SceneRunState, scene_id)
    if state is None:
        raise RuntimeError(f"scene run state disappeared during budget initialization: {scene_id}")

    if state.scene_budget_basis_json is not None:
        # 依据里的预算必须是正整数（坏数据 fail-closed，抛 corrupt budget state）
        _basis_scene_token_budget(state.scene_budget_basis_json)
        if state.scene_token_budget is None:
            effective_budget = audited_scene_budget_prefixes(session, state)[-1]
            _validate_scene_budget_state(session, state, effective_budget=effective_budget)
            session.execute(
                update(SceneRunState)
                .where(
                    SceneRunState.scene_id == scene_id,
                    SceneRunState.scene_token_budget.is_(None),
                )
                .values(scene_token_budget=effective_budget)
            )
            session.commit()
            session.expire_all()
            state = session.get(SceneRunState, scene_id)
            assert state is not None
        _validate_scene_budget_state(session, state)
        return state

    if state.scene_token_budget is not None:
        _validate_scene_budget_state(session, state)
        existing_budget = int(state.scene_token_budget)
        existing_provider_budget = int(state.provider_attempt_budget)
        basis = {
            "basis_type": "legacy_existing_scene_token_budget",
            "scene_token_budget": existing_budget,
            "topup_audit_cutoff_operation_id": _current_operation_id(session),
            "token_budget_basis": {
                "reconstructable": False,
                "reason": "legacy_scene_token_budget_without_basis",
            },
            "provider_attempt_budget": {
                "config_key": PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY,
                "value": existing_provider_budget,
            },
            "attempt_budget": {
                "source": "scene_run_states.initial",
                "value": int(state.attempt_budget),
            },
        }
        session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.scene_budget_basis_json.is_(None),
            )
            .values(scene_budget_basis_json=basis)
        )
    else:
        _validate_uninitialized_scene_budget_state(state)
        effective_baseline, armed_provider_budget = _canonical_budget_candidate(session)
        multiplier = _armed_scene_budget_multiplier()
        if multiplier > 0:
            scene_budget = multiplier * effective_baseline
            effective_provider_budget = armed_provider_budget
            effective_attempt_budget = int(state.attempt_budget)
        else:
            # 解除武装（单作者默认）：三道额度落到有限哨兵，预留闸门恒不触发；记账照旧。
            scene_budget = UNARMED_SCENE_TOKEN_BUDGET
            effective_provider_budget = UNARMED_ATTEMPT_BUDGET
            effective_attempt_budget = UNARMED_ATTEMPT_BUDGET
        basis = {
            "baseline_tokens": effective_baseline,
            "budget_multiplier": multiplier,
            "scene_token_budget": scene_budget,
            "scene_budget_armed": multiplier > 0,
            "topup_audit_cutoff_operation_id": _current_operation_id(session),
            "provider_attempt_budget": {
                "config_key": PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY,
                "value": effective_provider_budget,
            },
            "attempt_budget": {
                "source": "scene_run_states.initial" if multiplier > 0 else "scene_lifecycle_disarmed",
                "value": effective_attempt_budget,
            },
        }
        session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.scene_budget_basis_json.is_(None),
                SceneRunState.scene_token_budget.is_(None),
                SceneRunState.scene_tokens_used == 0,
                SceneRunState.scene_tokens_reserved == 0,
                SceneRunState.provider_attempts_used == 0,
                SceneRunState.total_attempt_count == 0,
            )
            .values(
                scene_token_budget=scene_budget,
                scene_budget_basis_json=basis,
                provider_attempt_budget=effective_provider_budget,
                attempt_budget=effective_attempt_budget,
            )
        )
    session.commit()
    session.expire_all()
    state = session.get(SceneRunState, scene_id)
    assert state is not None
    if state.scene_budget_basis_json is None or state.scene_token_budget is None:
        raise RuntimeError(f"scene budget initialization lost its CAS: {scene_id}")
    _validate_scene_budget_state(session, state)
    return state


def _current_operation_id(session: Session) -> int:
    value = session.execute(select(func.max(OperationLog.operation_id))).scalar_one()
    return int(value or 0)


def audited_scene_budget_prefixes(
    session: Session,
    state: SceneRunState,
) -> tuple[int, ...]:
    """Return every legal effective budget represented by the immutable basis + audit."""

    return tuple(snapshot[0] for snapshot in _audited_scene_budget_snapshots(session, state))


def _audited_scene_budget_snapshots(
    session: Session,
    state: SceneRunState,
) -> tuple[tuple[int, int, int], ...]:
    """Replay token, business-attempt and provider-attempt budgets together.

    Old topup rows only carried ``extra_tokens``.  They remain readable with
    zero increments for the two attempt dimensions.  New initial bases record
    all three starting values so later topups are fully reconstructable.
    """

    basis = state.scene_budget_basis_json
    basis_budget = _basis_scene_token_budget(basis)
    assert isinstance(basis, dict)
    cutoff = basis.get("topup_audit_cutoff_operation_id", 0)
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 0:
        raise _corrupt_budget_state("topup audit cutoff must be a non-negative integer")
    topups = session.execute(
        select(OperationLog)
        .where(
            OperationLog.operation_id > cutoff,
            OperationLog.event_type == "scene_budget_topup",
            OperationLog.object_type == "scene",
            OperationLog.object_ref == state.scene_id,
        )
        .order_by(OperationLog.operation_id.asc())
    ).scalars().all()
    normalized_topups: list[tuple[OperationLog, dict[str, Any], int, int, int]] = []
    for topup in topups:
        payload = topup.payload_json
        if not isinstance(payload, dict):
            raise _corrupt_budget_state("topup audit payload must be an object")
        extras: list[int] = []
        for field in ("extra_tokens", "extra_attempts", "extra_provider_attempts"):
            raw = payload.get(field, 0)
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise _corrupt_budget_state(f"topup audit {field} must be non-negative")
            extras.append(raw)
        if not any(extras):
            raise _corrupt_budget_state("topup audit must expand at least one lifecycle budget")
        normalized_topups.append((topup, payload, extras[0], extras[1], extras[2]))

    initial_attempt_budget = _basis_attempt_budget(basis)
    if initial_attempt_budget is None:
        # Compatibility for immutable bases written before the business-attempt
        # dimension was captured.  The current value and the durable suffix are
        # sufficient to recover the historical prefix without changing it.
        initial_attempt_budget = int(state.attempt_budget) - sum(
            row[3] for row in normalized_topups
        )
        if initial_attempt_budget != LEGACY_INITIAL_ATTEMPT_BUDGET:
            raise _corrupt_budget_state(
                "legacy initial attempt_budget differs from the canonical pre-topup value"
            )
    initial_provider_budget = _basis_provider_attempt_budget(basis)
    snapshots = [(basis_budget, initial_attempt_budget, initial_provider_budget)]
    for _topup, payload, extra_tokens, extra_attempts, extra_provider_attempts in normalized_topups:
        previous = snapshots[-1]
        effective = (
            previous[0] + extra_tokens,
            previous[1] + extra_attempts,
            previous[2] + extra_provider_attempts,
        )
        if any(value > (1 << 63) - 1 for value in effective):
            raise _corrupt_budget_state("topup audit exceeds the signed 64-bit limit")
        reported_budget = payload.get("scene_token_budget")
        if (
            not isinstance(reported_budget, int)
            or isinstance(reported_budget, bool)
            or reported_budget != effective[0]
        ):
            raise _corrupt_budget_state("topup audit scene_token_budget prefix is invalid")
        for payload_field, expected_value, required in (
            ("attempt_budget", effective[1], "extra_attempts" in payload),
            (
                "provider_attempt_budget",
                effective[2],
                "extra_provider_attempts" in payload,
            ),
        ):
            reported = payload.get(payload_field)
            if reported is None and not required:
                continue
            if (
                not isinstance(reported, int)
                or isinstance(reported, bool)
                or reported != expected_value
            ):
                raise _corrupt_budget_state(
                    f"topup audit {payload_field} prefix is invalid"
                )
        snapshots.append(effective)
    return tuple(snapshots)


def _canonical_budget_candidate(session: Session) -> tuple[int, int]:
    from novel_system.services.llm_client import load_model_routing_config

    baseline = max(
        estimate_baseline_tokens(session, {}),
        FALLBACK_INPUT_TOKENS + FALLBACK_OUTPUT_TOKENS,
    )
    routing = load_model_routing_config()
    retry_budget = getattr(routing, "retry_budget", {})
    configured_attempts = (
        retry_budget.get("provider_attempt_budget", DEFAULT_PROVIDER_ATTEMPT_BUDGET)
        if isinstance(retry_budget, dict)
        else DEFAULT_PROVIDER_ATTEMPT_BUDGET
    )
    return int(baseline), max(1, int(configured_attempts))


def _basis_scene_token_budget(basis: object) -> int:
    if not isinstance(basis, dict):
        raise _corrupt_budget_state("scene_budget_basis_json must be an object")
    budget = basis.get("scene_token_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise _corrupt_budget_state("basis scene_token_budget must be a positive integer")
    return budget


def _basis_provider_attempt_budget(basis: object) -> int:
    if not isinstance(basis, dict):
        raise _corrupt_budget_state("scene_budget_basis_json must be an object")
    provider_basis = basis.get("provider_attempt_budget")
    if not isinstance(provider_basis, dict):
        raise _corrupt_budget_state("basis provider_attempt_budget must be an object")
    value = provider_basis.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _corrupt_budget_state("basis provider_attempt_budget.value must be a positive integer")
    return value


def _basis_attempt_budget(basis: object) -> int | None:
    if not isinstance(basis, dict):
        raise _corrupt_budget_state("scene_budget_basis_json must be an object")
    attempt_basis = basis.get("attempt_budget")
    if attempt_basis is None:
        return None
    if not isinstance(attempt_basis, dict):
        raise _corrupt_budget_state("basis attempt_budget must be an object")
    value = attempt_basis.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _corrupt_budget_state("basis attempt_budget.value must be a positive integer")
    return value


def _counter(value: object, field: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _corrupt_budget_state(f"{field} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise _corrupt_budget_state(f"{field} must be {qualifier}")
    return value


def _validate_scene_budget_state(
    session: Session,
    state: SceneRunState,
    *,
    effective_budget: int | None = None,
) -> None:
    budget = effective_budget
    if budget is None:
        budget = _counter(state.scene_token_budget, "scene_token_budget", positive=True)
    if state.scene_budget_basis_json is not None:
        effective = _audited_scene_budget_snapshots(session, state)[-1]
        if effective[0] != budget:
            raise _corrupt_budget_state("audited effective budget does not match scene_token_budget")
    used = _counter(state.scene_tokens_used, "scene_tokens_used")
    reserved = _counter(state.scene_tokens_reserved, "scene_tokens_reserved")
    if used + reserved > budget:
        usage_overage_tombstone = session.scalar(
                select(LlmCall.llm_call_id).where(
                    LlmCall.scene_id == state.scene_id,
                    LlmCall.accounting_status == "usage_exceeds_reservation",
                    LlmCall.request_dispatched_at.is_not(None),
                ).limit(1)
            ) is not None
        integrity_tombstone = session.scalar(
            select(LlmCall.llm_call_id).where(
                LlmCall.scene_id == state.scene_id,
                LlmCall.request_dispatched_at.is_not(None),
                LlmCall.error_code.in_(
                    {
                        "LLM_ACCOUNTING_HOOK_NOT_INVOKED",
                        "LLM_ACCOUNTING_UNKNOWN_DISPATCH",
                        "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE",
                    }
                ),
            ).limit(1)
        ) is not None
        known_blocked_overage = reserved == 0 and (
            (
                state.run_execution_status == "usage_exceeds_reservation"
                and usage_overage_tombstone
            )
            or (
                state.run_execution_status == "accounting_integrity_blocked"
                and integrity_tombstone
            )
        )
        if not known_blocked_overage:
            raise _corrupt_budget_state("used plus reserved exceeds scene_token_budget")
    provider_used = _counter(state.provider_attempts_used, "provider_attempts_used")
    provider_budget = _counter(
        state.provider_attempt_budget,
        "provider_attempt_budget",
        positive=True,
    )
    if state.scene_budget_basis_json is not None and effective[2] != provider_budget:
        raise _corrupt_budget_state(
            "audited provider_attempt_budget does not match provider_attempt_budget"
        )
    if provider_used > provider_budget:
        raise _corrupt_budget_state("provider attempts used exceeds its lifecycle budget")
    total_attempts = _counter(state.total_attempt_count, "total_attempt_count")
    attempt_budget = _counter(state.attempt_budget, "attempt_budget", positive=True)
    if state.scene_budget_basis_json is not None and effective[1] != attempt_budget:
        raise _corrupt_budget_state("audited attempt_budget does not match attempt_budget")
    if total_attempts > attempt_budget:
        raise _corrupt_budget_state("total attempts used exceeds its business attempt budget")


def _validate_uninitialized_scene_budget_state(state: SceneRunState) -> None:
    used = _counter(state.scene_tokens_used, "scene_tokens_used")
    reserved = _counter(state.scene_tokens_reserved, "scene_tokens_reserved")
    provider_used = _counter(state.provider_attempts_used, "provider_attempts_used")
    _counter(state.provider_attempt_budget, "provider_attempt_budget", positive=True)
    total_attempts = _counter(state.total_attempt_count, "total_attempt_count")
    _counter(state.attempt_budget, "attempt_budget", positive=True)
    if used or reserved or provider_used or total_attempts:
        raise _corrupt_budget_state(
            "budget and basis are absent after lifecycle accounting has already started"
        )


def _corrupt_budget_state(reason: str) -> ValueError:
    return ValueError(f"scene budget state is corrupt: {reason}")


def estimate_baseline_tokens(session: Session, snapshot: dict[str, Any]) -> int:
    """确定性估算单发基线：writer 路由的估算输入 + 配置输出上限。"""
    input_tokens = FALLBACK_INPUT_TOKENS
    output_tokens = FALLBACK_OUTPUT_TOKENS
    try:
        from novel_system.services.prompt_builder import PromptBuilder

        prompt = PromptBuilder().build(snapshot, BASELINE_WRITER_NODE)
        estimated = (prompt.get("token_budget") or {}).get("estimated_input_tokens")
        if isinstance(estimated, (int, float)) and estimated > 0:
            input_tokens = int(estimated)
    except Exception:
        _LOGGER.debug("baseline input estimation degraded; using fallback", exc_info=True)
    try:
        from novel_system.services.llm_task_runner import LLMNodeRunner

        task_config = LLMNodeRunner(session).task_config(BASELINE_WRITER_NODE)
        cap = getattr(task_config, "max_output_tokens", None)
        if isinstance(cap, (int, float)) and cap > 0:
            output_tokens = int(cap)
    except Exception:
        _LOGGER.debug("baseline output cap lookup degraded; using fallback", exc_info=True)
    return input_tokens + output_tokens


def budget_unit(state: SceneRunState) -> int:
    """一个「生成调用当量」的估算值 = 基线（预算/5）；未初始化回退常量。"""
    if state.scene_token_budget:
        return max(1, int(state.scene_token_budget) // BUDGET_MULTIPLIER)
    return FALLBACK_INPUT_TOKENS + FALLBACK_OUTPUT_TOKENS


def can_spend(state: SceneRunState | None, estimated_tokens: int) -> bool:
    """可选支出的前置预留检查；预算未初始化不拦（渐进迁移）。"""
    if state is None or state.scene_token_budget is None:
        return True
    used = int(state.scene_tokens_used or 0)
    return used + max(0, int(estimated_tokens)) <= int(state.scene_token_budget)


def is_scene_budget_disarmed(state: SceneRunState | None) -> bool:
    """场景 token 闸门是否处于解除武装态（哨兵额度，无真实上限）。

    优先看不可变依据里的 ``scene_budget_armed`` 标记（新场景都带）；旧场景无此键时
    退回哨兵阈值判断。成本看板据此把「预算」显示为「不限」而非天文数字。
    """
    if state is None:
        return False
    basis = state.scene_budget_basis_json
    if isinstance(basis, dict) and "scene_budget_armed" in basis:
        return basis.get("scene_budget_armed") is False
    budget = state.scene_token_budget
    return (
        isinstance(budget, int)
        and not isinstance(budget, bool)
        and budget >= UNARMED_SCENE_TOKEN_BUDGET
    )
