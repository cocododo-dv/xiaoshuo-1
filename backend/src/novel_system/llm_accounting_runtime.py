"""Environment-only LLM accounting settings for low-level ledger code."""

from __future__ import annotations

from dataclasses import dataclass

from novel_system.env_parsing import positive_int_env


@dataclass(frozen=True, slots=True)
class LLMAccountingRuntime:
    # Startup reconciliation only touches unowned, non-scene reservations
    # older than this conservative TTL.  It must comfortably exceed normal
    # provider retries so a live legacy request is not mistaken for a crash.
    reservation_recovery_ttl_seconds: int


def load_llm_accounting_runtime() -> LLMAccountingRuntime:
    return LLMAccountingRuntime(
        reservation_recovery_ttl_seconds=positive_int_env(
            "NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS", 3_600
        ),
    )
