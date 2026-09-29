"""LLM 记账的类型、错误、错误码与用量估算（从 llm_accounting 拆出，2026-09-30，B09-12）。

不碰数据库：调用上下文 ``LLMCallContext``、预留估算 ``estimate_request_usage``、响应用量归一
``normalize_response_usage``、两种记账错误与控制面错误码（完整性 / 恰好一次语义绝不能被降级的失败）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from novel_system.services.context_budget import estimate_tokens
from novel_system.services.hash_engine import sha256_json_plain
from novel_system.services.llm_providers.base import LLMClientError, LLMRequest, LLMResponse
from novel_system.services.llm_providers.usage import NormalizedUsage, normalize_raw_usage


MESSAGE_TOKEN_OVERHEAD = 4
ACCOUNTING_EXECUTION_MODE_KEY = "_accounting_provider_execution_mode"
ACCOUNTING_INTEGRITY_BLOCKED_STATUS = "accounting_integrity_blocked"
ACCOUNTING_INTEGRITY_ERROR_CODES = {
    "LLM_ACCOUNTING_HOOK_NOT_INVOKED",
    "LLM_ACCOUNTING_UNKNOWN_DISPATCH",
    "LLM_ACCOUNTING_LIFECYCLE_INCOMPLETE",
}
CONTROL_PLANE_ERROR_CODES = ACCOUNTING_INTEGRITY_ERROR_CODES | {
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
    # 只有 online 会执行（B09-04：离线确定性执行模式已退役，没有任何生产客户端实现它）。类型里暂留这个旧字面值，
    # 是为了让自动批评 / 事件抽取 / 检查点校验里还没删的旧分支（归别的包）照旧能构造上下文——带着它进账本的调用在
    # 派发前被拒（LLM_ACCOUNTING_CONTEXT_INVALID），账本里历史的离线行过不了产品校验。
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
class AccountingRecoveryResult:
    status: Literal["released", "failed"]
    error_code: str | None
    may_retry: bool


class LLMAccountingError(LLMClientError):
    pass


class LLMAccountingRejected(LLMAccountingError):
    pass


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
        if llm_failure_code(candidate) in CONTROL_PLANE_ERROR_CODES:
            return True
    return False


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
    if response.usage_complete is True:
        actual = normalize_raw_usage(response.raw_usage)
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


def request_prompt_hash(request: LLMRequest) -> str:
    return sha256_json_plain(request.messages)


def summarize_request(request: LLMRequest) -> dict[str, Any]:
    return {
        "message_count": len(request.messages),
        "message_chars": sum(len(str(message.get("content") or "")) for message in request.messages),
        "max_output_tokens": request.max_output_tokens,
        "response_format": request.response_format,
        "api_mode": request.api_mode,
    }


def elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))
