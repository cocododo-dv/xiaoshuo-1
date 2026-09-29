"""质检节点的 LLM 调用与三级受控降级（continuity 预拒 / 已派发失败 / payload 非法）：降级只在账本证据成立时
发生，否则原异常照抛。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, LlmCallAttempt, SceneCard, SceneRunState
from novel_system.services.llm_task_runner import (
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    LLMNodeRunner,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.qc_engine.scores import _normalize_soft_qc_scores
from novel_system.services.qc_validator import QCValidationError, validate_qc_report


CONTINUITY_BUDGET_ISSUE_KEY = "continuity_budget_exceeded"
CONTINUITY_BUDGET_MESSAGE = "Prompt still exceeds the safe input budget after deterministic continuity compaction."

_QC_CONTROL_PLANE_ERROR_CODES = {
    "CONTINUITY_BUDGET_EXCEEDED",
    "LLM_USAGE_EXCEEDS_RESERVATION",
}


def _is_proven_dispatched_provider_failure(
    session: Session,
    *,
    error: LLMNodeExecutionError,
    scene: SceneCard,
    state: SceneRunState,
    expected_step: str,
    execution_step_key: str,
) -> bool:
    """Allow QC degradation only for an exact, durable provider-failure ledger."""
    error_code = str(error.error_code or "")
    if (
        not error_code
        or error_code.startswith("RUN_")
        or error_code.startswith("LLM_ACCOUNTING_")
        or error_code.startswith("LLM_SCENE_")
        or error_code.startswith("LLM_PROVIDER_ATTEMPT_")
        or error_code in _QC_CONTROL_PLANE_ERROR_CODES
    ):
        return False
    current_execution_id = str(state.active_execution_id or "").strip()
    if not current_execution_id:
        return False
    parent = session.get(LlmCall, error.llm_call_id)
    if parent is None:
        return False
    if (
        parent.scope_type != "scene"
        or parent.scope_id != scene.scene_id
        or parent.scene_id != scene.scene_id
        or parent.chapter_id != scene.chapter_id
        or parent.execution_id != current_execution_id
        or parent.execution_step_key != execution_step_key
        or parent.step != expected_step
        or parent.node_id != expected_step
        or parent.accounting_status != "failed"
        or parent.error_code != error_code
        or parent.request_dispatched_at is None
        or parent.settled_at is None
    ):
        return False
    attempts = list(
        session.scalars(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.llm_call_id == parent.llm_call_id)
            .order_by(LlmCallAttempt.provider_attempt_no)
        )
    )
    if not attempts or [row.provider_attempt_no for row in attempts] != list(
        range(len(attempts))
    ):
        return False

    aggregate_fields = (
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
    )
    for attempt in attempts:
        numeric_values = (
            attempt.provider_attempt_no,
            attempt.request_max_output_tokens,
            *(getattr(attempt, field) for field in aggregate_fields),
        )
        if any(not isinstance(value, int) or value < 0 for value in numeric_values):
            return False
        if (
            attempt.accounting_status != "failed"
            or attempt.request_dispatched_at is None
            or attempt.settled_at is None
            or attempt.estimated_tokens > attempt.reserved_tokens
            or attempt.budget_charged_tokens > attempt.reserved_tokens
            or attempt.budget_charged_tokens
            != min(attempt.total_tokens, attempt.reserved_tokens)
            or attempt.total_tokens != attempt.prompt_tokens + attempt.completion_tokens
            or not str(attempt.error_code or "").strip()
        ):
            return False

    for field in aggregate_fields:
        parent_value = getattr(parent, field)
        if (
            not isinstance(parent_value, int)
            or parent_value < 0
            or parent_value != sum(getattr(attempt, field) for attempt in attempts)
        ):
            return False
    if parent.usage_is_estimate != any(
        attempt.usage_is_estimate for attempt in attempts
    ):
        return False
    if (
        parent.estimated_tokens > parent.reserved_tokens
        or parent.budget_charged_tokens
        != min(parent.total_tokens, parent.reserved_tokens)
    ):
        return False

    final_attempt = attempts[-1]
    return bool(
        final_attempt.error_code == error_code
        and any(attempt.error_code == error_code for attempt in attempts)
    )


def _is_proven_undispatched_continuity_rejection(
    session: Session,
    *,
    error: LLMNodeContinuityError,
    scene: SceneCard,
    state: SceneRunState,
    expected_step: str,
    execution_step_key: str,
) -> bool:
    """Allow continuity degradation only from the exact pre-dispatch rejection ledger."""
    current_execution_id = str(state.active_execution_id or "").strip()
    if not current_execution_id or error.error_code != "CONTINUITY_BUDGET_EXCEEDED":
        return False
    parent = session.get(LlmCall, error.llm_call_id)
    if parent is None:
        return False
    if (
        parent.scope_type != "scene"
        or parent.scope_id != scene.scene_id
        or parent.scene_id != scene.scene_id
        or parent.chapter_id != scene.chapter_id
        or parent.execution_id != current_execution_id
        or parent.execution_step_key != execution_step_key
        or parent.step != expected_step
        or parent.node_id != expected_step
        or parent.accounting_status != "rejected"
        or parent.error_code != "CONTINUITY_BUDGET_EXCEEDED"
        or parent.request_dispatched_at is not None
        or parent.settled_at is None
        or parent.usage_is_estimate is not True
        or any(
            getattr(parent, field) != 0
            for field in (
                "estimated_tokens",
                "reserved_tokens",
                "budget_charged_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "latency_ms",
            )
        )
    ):
        return False
    return (
        session.scalar(
            select(LlmCallAttempt.attempt_id)
            .where(
                LlmCallAttempt.llm_call_id == parent.llm_call_id,
            )
            .limit(1)
        )
        is None
    )


def _continuity_warning_message(continuity_warning: Any) -> str:
    if isinstance(continuity_warning, dict):
        message = continuity_warning.get("message")
        if isinstance(message, str) and message:
            return message
    return CONTINUITY_BUDGET_MESSAGE


def _continuity_warning_issue_key(continuity_warning: Any) -> str:
    if isinstance(continuity_warning, dict):
        code = continuity_warning.get("code")
        if isinstance(code, str) and code:
            return code
    return CONTINUITY_BUDGET_ISSUE_KEY


def _qc_build_user_prompt(base_prompt: str, draft_content: str) -> str:
    return f"{base_prompt}\n\n## Draft Under Review\n{draft_content}".strip()


def _qc_run_node_with_degradation(
    session: Session,
    *,
    prompt_builder: PromptBuilder,
    llm_runner: LLMNodeRunner,
    scene: SceneCard,
    state: SceneRunState,
    scene_id: str,
    bundle: dict[str, Any],
    source_draft_row_id: str,
    source_draft_content: str,
    execution_step_key: str,
    step: str,
    message_prefix: str,
    degraded_payload_factory: Callable[..., dict[str, Any]],
    prompt_decorator: Callable[[dict[str, Any], str], dict[str, Any] | None]
    | None = None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """QC 节点执行 + 三级受控降级（continuity 预拒 / 已派发失败 / payload 非法）。

    降级只在对应账本证据成立时发生，否则原异常照抛；降级形状由各引擎的
    payload 工厂决定（hard=pass、soft=waive）。返回 (llm_call_id,
    degraded_reason, payload)。

    ``prompt_decorator(prompt, final_user_prompt)`` 在模板构建之后、派发之前对 prompt
    做一次可选改写（v2：soft_qc 阶段前置 ``[STYLE_REFERENCE]`` 前缀）；返回 ``None``
    视为不改写。
    """
    llm_call_id: str | None = None
    degraded_reason: str | None = None
    score_schema: Any = None
    try:
        prompt = prompt_builder.build(bundle["snapshot"], step)
        # 分数的刻度以这一次调用的模板声明为准（review_scores.declared_score_scale）
        score_schema = prompt.get("structured_schema")
        final_user_prompt = _qc_build_user_prompt(
            prompt["user_prompt"], source_draft_content
        )
        if prompt_decorator is not None:
            decorated = prompt_decorator(prompt, final_user_prompt)
            if isinstance(decorated, dict):
                prompt = decorated
        node_result = llm_runner.run(
            scene_id=scene_id,
            chapter_id=scene.chapter_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            node_id=step,
            step=step,
            prompt=prompt,
            user_prompt=final_user_prompt,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_draft_content,
            execution_step_key=execution_step_key,
        )
        llm_call_id = node_result.llm_call_id
        payload = node_result.response.structured_output or {}
    except LLMNodeContinuityError as exc:
        if not _is_proven_undispatched_continuity_rejection(
            session,
            error=exc,
            scene=scene,
            state=state,
            expected_step=step,
            execution_step_key=execution_step_key,
        ):
            raise
        llm_call_id = exc.llm_call_id
        degraded_reason = f"{step}_continuity_budget_exceeded"
        payload = degraded_payload_factory(
            issue_key=_continuity_warning_issue_key(exc.continuity_warning),
            message=_continuity_warning_message(exc.continuity_warning),
            continuity_warning=exc.continuity_warning,
        )
    except LLMNodeExecutionError as exc:
        if not _is_proven_dispatched_provider_failure(
            session,
            error=exc,
            scene=scene,
            state=state,
            expected_step=step,
            execution_step_key=execution_step_key,
        ):
            raise
        llm_call_id = exc.llm_call_id
        degraded_reason = f"{step}_execution_failed"
        payload = degraded_payload_factory(
            issue_key=f"{step}_execution_failed",
            message=f"{message_prefix} execution failed: {exc.message}",
        )
    if degraded_reason is None:
        try:
            # 只对真实 LLM payload 做 normalize（dump 会把 issue 重建为
            # issue_key+message）；此后管线内部字段（source/quality_level 等）
            # 不得再经 validate→dump 往返，否则分级契约被剥掉。
            # 风格参考 v3：软 QC 的分数先按模板声明的刻度换算再校验——此前 9.3 这类回答在这里就被 le=1 拒掉，
            # 整遍软 QC 被判 invalid 豁免（2026-09-22 的换算只在落库时做，从没轮上）。只在这里换算一次。
            if step == "soft_qc":
                payload = _normalize_soft_qc_scores(payload, schema=score_schema)
            report = validate_qc_report(step, payload)
            payload = report.model_dump()
        except (QCValidationError, ValidationError) as exc:
            degraded_reason = f"invalid_{step}_payload"
            payload = degraded_payload_factory(
                issue_key=f"invalid_{step}_payload",
                message=f"{message_prefix} payload validation failed: {exc}",
            )
    return llm_call_id, degraded_reason, payload
