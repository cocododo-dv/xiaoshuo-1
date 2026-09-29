"""Environment-only LLM accounting limits for low-level ledger code."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMAccountingRuntime:
    daily_token_limit: int
    monthly_token_limit: int
    project_daily_token_limit: int
    daily_request_limit: int
    max_concurrent_requests: int
    # Startup reconciliation only touches unowned, non-scene reservations
    # older than this conservative TTL.  It must comfortably exceed normal
    # provider retries so a live legacy request is not mistaken for a crash.
    reservation_recovery_ttl_seconds: int
    daily_cost_limit_usd: float
    input_cost_per_million_usd: float
    output_cost_per_million_usd: float

    # Preserve the Settings attribute names consumed by accounting helpers.
    @property
    def llm_daily_token_limit(self) -> int:
        return self.daily_token_limit

    @property
    def llm_monthly_token_limit(self) -> int:
        return self.monthly_token_limit

    @property
    def llm_project_daily_token_limit(self) -> int:
        return self.project_daily_token_limit

    @property
    def llm_daily_request_limit(self) -> int:
        return self.daily_request_limit

    @property
    def llm_max_concurrent_requests(self) -> int:
        return self.max_concurrent_requests

    @property
    def llm_daily_cost_limit_usd(self) -> float:
        return self.daily_cost_limit_usd

    @property
    def llm_input_cost_per_million_usd(self) -> float:
        return self.input_cost_per_million_usd

    @property
    def llm_output_cost_per_million_usd(self) -> float:
        return self.output_cost_per_million_usd


def load_llm_accounting_runtime() -> LLMAccountingRuntime:
    runtime = LLMAccountingRuntime(
        daily_token_limit=_quota_int("NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT", 0),
        monthly_token_limit=_quota_int("NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT", 0),
        project_daily_token_limit=_quota_int(
            "NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT", 0
        ),
        daily_request_limit=_quota_int("NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT", 0),
        max_concurrent_requests=_quota_int(
            "NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS", 0
        ),
        reservation_recovery_ttl_seconds=_positive_int(
            "NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS", 3_600
        ),
        daily_cost_limit_usd=_non_negative_float(
            "NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD", 0.0
        ),
        input_cost_per_million_usd=_non_negative_float(
            "NOVEL_SYSTEM_LLM_INPUT_COST_PER_MILLION_USD", 0.0
        ),
        output_cost_per_million_usd=_non_negative_float(
            "NOVEL_SYSTEM_LLM_OUTPUT_COST_PER_MILLION_USD", 0.0
        ),
    )
    if runtime.daily_cost_limit_usd > 0 and max(
        runtime.input_cost_per_million_usd,
        runtime.output_cost_per_million_usd,
    ) <= 0:
        raise ValueError(
            "NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD requires at least one configured token price"
        )
    return runtime


def _quota_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    message = f"{name} must be a non-negative integer (0 disables the limit)"
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if value < 0:
        raise ValueError(message)
    return value


def _positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid number") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value
