"""Environment-only core runtime values shared below the service layer."""

from __future__ import annotations

import os
from dataclasses import dataclass

from novel_system.env_parsing import bool_env, non_negative_float_env
from novel_system.runtime_defaults import DEFAULT_LLM_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class CoreRuntime:
    llm_provider: str
    llm_base_url: str
    llm_api_key: str | None
    llm_timeout_seconds: float
    llm_enabled: bool
    admin_token: str | None
    config_secret: str | None


def load_core_runtime() -> CoreRuntime:
    return CoreRuntime(
        llm_provider=os.environ.get("NOVEL_SYSTEM_LLM_PROVIDER", "openai_compatible"),
        llm_base_url=os.environ.get("NOVEL_SYSTEM_LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=os.environ.get("NOVEL_SYSTEM_LLM_API_KEY"),
        llm_timeout_seconds=non_negative_float_env(
            "NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS",
            DEFAULT_LLM_TIMEOUT_SECONDS,
        ),
        llm_enabled=bool_env("NOVEL_SYSTEM_LLM_ENABLED", False),
        admin_token=os.environ.get("NOVEL_SYSTEM_ADMIN_TOKEN"),
        config_secret=os.environ.get("NOVEL_SYSTEM_CONFIG_SECRET"),
    )
