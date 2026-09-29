"""LLM 节点失败 → 领域错误：全系统唯一的一份翻译（审计 B09-24）。

没有可用的模型能力（路由没配、服务停用、配置不合法、记账上下文缺失）是作者能处理的配置问题 → 409 +
调用方给的 ``capability_code``；其余（传输、服务报错、超时、输出解析）是上游模型失败 → 502 +
``failure_code``；记账拒绝（配额、预算闸）→ 409，码就是记账层的码。``details`` 的形状处处一样：
``{llm_call_id, node_id, error_code, retryable, next_action, response_summary}``，调用方可用
``extra_details`` 追加自己的键（不能改这六个）。前端按 ``code`` 分支，不按状态码。

每个调用 LLM 的服务都应经这里翻译 ``LLMNodeExecutionError``，不要自己再写一份（以前写作台深评把
服务 5xx / 超时也翻成 409）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_client import LLMConfigurationError
from novel_system.services.llm_task_runner import LLMNodeExecutionError


# These failures mean that the requested LLM capability is unavailable in the
# current runtime.  They are actionable configuration/conflict responses, not
# upstream model failures.  Everything else (transport, provider, timeout and
# response parsing failures) is a bad-gateway failure.
_CAPABILITY_ERROR_CODES = frozenset(
    {
        "LLM_ROUTE_NOT_CONFIGURED",
        "LLM_PROVIDER_DISABLED",
        "LLM_PROVIDER_UNSUPPORTED",
        "LLM_MODEL_CONFIG_INVALID",
        "LLM_ACCOUNTING_CONTEXT_INVALID",
        "LLM_ACCOUNTING_CONTEXT_REQUIRED",
        "LLM_ACCOUNTING_SESSION_REQUIRED",
    }
)


def is_llm_capability_error(exc: LLMNodeExecutionError) -> bool:
    return exc.error_code in _CAPABILITY_ERROR_CODES or isinstance(
        exc.original_error,
        LLMConfigurationError,
    )


def raise_llm_domain_error(
    exc: LLMNodeExecutionError,
    *,
    capability_code: str,
    failure_code: str,
    operation: str,
    node_id: str,
    next_action: str,
    extra_details: Mapping[str, Any] | None = None,
) -> NoReturn:
    capability_error = is_llm_capability_error(exc)

    def details(action: str) -> dict[str, Any]:
        return {
            **dict(extra_details or {}),
            "llm_call_id": exc.llm_call_id,
            "node_id": node_id,
            "error_code": exc.error_code,
            "retryable": exc.retryable,
            "next_action": action,
            "response_summary": exc.response_summary,
        }

    if isinstance(exc.original_error, LLMAccountingRejected) and not capability_error:
        raise DomainError(
            exc.error_code,
            exc.message,
            status_code=409,
            details=details("restore_llm_accounting_capacity_and_retry"),
        ) from exc
    raise DomainError(
        capability_code if capability_error else failure_code,
        (
            f"{operation} requires a configured live LLM capability: {exc.message}"
            if capability_error
            else f"{operation} failed: {exc.message}"
        ),
        status_code=409 if capability_error else 502,
        details=details(next_action),
    ) from exc


__all__ = ["is_llm_capability_error", "raise_llm_domain_error"]
