"""服务（provider）的 API 配置：规范化、叠到环境设置上、变成运行时配置与客户端（从 system_config 拆出，B09-13）。

api 快照的形状：``llm.providers.<provider_id>``（地址、类型、凭据方式、端点模式、模型列表……）、
``default_provider_id``、``enabled``、``timeout_seconds``；最早的单服务写法（``llm.provider`` /
``llm.base_url``，没有 providers）照旧读。密钥不在快照里（见 ``system_config_secrets``）。

读库的两个入口 ``apply_active_api_config`` / ``load_llm_provider_runtime_configs`` 把快照与它用到的
密钥放在同一个只读事务里读；``build_llm_client`` 是运行时 LLMClient 的唯一构造点。
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from novel_system.db.session import SessionLocal
from novel_system.env_config import DEFAULT_LLM_TIMEOUT_SECONDS, load_env_settings
from novel_system.services.config_snapshot_reader import active_config_payload, read_with_transient_retry
from novel_system.services.llm_client import (
    LLMClient,
    ProviderRuntimeConfig,
    SUPPORTED_API_MODES,
    SUPPORTED_CREDENTIAL_MODES,
)
from novel_system.services.llm_providers import adapter_registry, provider_catalog as adapter_provider_catalog
from novel_system.services.llm_routing import DEFAULT_PROVIDER_BASE_URLS, SUPPORTED_PROVIDERS
from novel_system.services.system_config_secrets import (
    LLM_API_KEY_SECRET_ID,
    llm_provider_api_key_secret_id,
    secret_value,
)
from novel_system.services.value_coercion import optional_text


def coerce_api_payload(payload: dict[str, Any]) -> dict[str, Any]:
    llm = payload.get("llm") if isinstance(payload.get("llm"), dict) else payload
    if not isinstance(llm, dict):
        raise ValueError("api config must include an llm mapping")
    return llm


def provider_payloads_from_llm(llm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = llm.get("providers")
    if isinstance(providers, dict):
        return {
            str(provider_id): dict(provider_payload)
            for provider_id, provider_payload in providers.items()
            if isinstance(provider_payload, dict)
        }
    return {}


def normalize_provider_base_url(value: Any, provider: Any | None = None) -> str:
    base_url = str(value or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses", "/models"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)].rstrip("/")
            break
    provider_name = str(provider or "").strip()
    adapter = adapter_registry().get(provider_name)
    if adapter is not None and adapter.appends_v1_to_bare_host:
        try:
            parts = urlsplit(base_url)
        except ValueError:
            parts = None
        if parts is not None and parts.scheme and parts.netloc and parts.path in {"", "/"}:
            base_url = f"{base_url}/v1"
    return base_url


def httpx_trust_env_for_base_url(base_url: str) -> bool:
    try:
        hostname = urlsplit(base_url).hostname
    except ValueError:
        return True
    if not hostname:
        return True
    if hostname.lower() == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def normalize_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = required_text(payload.get("provider_id"), "provider_id")
    provider_type = str(payload.get("provider_type") or payload.get("provider") or "").strip()
    if provider_type not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider {provider_type}")
    credential_mode = str(payload.get("credential_mode") or "api_key")
    if credential_mode not in SUPPORTED_CREDENTIAL_MODES:
        raise ValueError(f"unsupported credential_mode {credential_mode}")
    base_url = normalize_provider_base_url(payload.get("base_url") or DEFAULT_PROVIDER_BASE_URLS.get(provider_type), provider_type)
    if not base_url:
        raise ValueError("provider base_url is required")
    models = payload.get("models") if isinstance(payload.get("models"), list) else []
    provider_options = payload.get("provider_options") if isinstance(payload.get("provider_options"), dict) else {}
    # api_mode 决定节点路由的端点(chat/responses);写错的值会在之后的
    # role-routes / sync-missing 解析路由时才炸成 LLMConfigurationError,这里就拦住。
    api_mode = str(payload.get("api_mode") or adapter_registry()[provider_type].default_api_mode)
    if api_mode not in SUPPORTED_API_MODES:
        raise ValueError(
            f"provider {provider_id} has unsupported api_mode {api_mode}; "
            f"expected one of {', '.join(sorted(SUPPORTED_API_MODES))}"
        )
    return {
        "provider_id": provider_id,
        "provider_type": provider_type,
        "account_id": optional_text(payload.get("account_id")),
        "base_url": base_url,
        "enabled": bool_value(payload.get("enabled", True)),
        "credential_mode": credential_mode,
        "api_mode": api_mode,
        "models": normalize_provider_model_ids(models),
        "provider_options": dict(provider_options),
    }


def validate_api_config(parsed: dict[str, Any]) -> dict[str, Any]:
    llm = coerce_api_payload(parsed)
    providers = provider_payloads_from_llm(llm)
    if providers:
        normalized_providers = {
            provider_id: normalize_provider_payload({"provider_id": provider_id, **provider_payload})
            for provider_id, provider_payload in providers.items()
        }
        timeout_seconds = api_timeout_seconds(llm)
        return {
            "enabled": bool_value(llm.get("enabled", True)),
            "timeout_seconds": timeout_seconds,
            "default_provider_id": llm.get("default_provider_id") or next(iter(normalized_providers.keys())),
            "providers": normalized_providers,
        }

    provider = str(llm.get("provider") or "openai_compatible")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider {provider}")

    base_url = normalize_provider_base_url(llm.get("base_url"), provider)
    if not base_url:
        raise ValueError("llm.base_url is required")

    timeout_seconds = api_timeout_seconds(llm)

    return {
        "provider": provider,
        "base_url": base_url,
        "enabled": bool_value(llm.get("enabled", False)),
        "timeout_seconds": timeout_seconds,
    }


def api_timeout_seconds(llm: dict[str, Any]) -> float:
    """解析 llm.timeout_seconds：缺省使用安全上限，显式 0 表示不限时。

    15 分钟不会重引入历史上的 30 秒误杀，同时避免静默上游永久占用 worker。
    负数仍然拒绝：那是笔误，不是“不限”的写法。
    """
    timeout_seconds = float_value(
        llm.get("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS),
        "llm.timeout_seconds",
    )
    if timeout_seconds < 0:
        raise ValueError("llm.timeout_seconds must not be negative (0 = no ceiling)")
    return timeout_seconds


def required_text(value: Any, field: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{field} is required")


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def float_value(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid number") from exc


def _contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
        for char in value
    )


def _looks_like_provider_model_id(value: str) -> bool:
    text = value.strip()
    return bool(text) and not _contains_cjk(text) and not any(char.isspace() for char in text) and any(char.isalnum() for char in text)


def normalize_provider_model_id(value: str) -> str:
    text = value.strip()
    if "/" not in text:
        return text
    prefix, suffix = text.split("/", 1)
    suffix = suffix.strip()
    if _contains_cjk(prefix) and _looks_like_provider_model_id(suffix):
        return suffix
    return text


def normalize_provider_model_ids(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            normalized
            for model in values
            if isinstance(model, str)
            for normalized in [normalize_provider_model_id(model)]
            if normalized
        )
    )


def provider_catalog() -> dict[str, dict[str, Any]]:
    return adapter_provider_catalog()


def overlay_api_config(settings, *, payload, read_secret):
    api_key = read_secret(LLM_API_KEY_SECRET_ID)
    if not payload and not api_key:
        return settings

    llm_payload = coerce_api_payload(payload or {})
    providers = provider_payloads_from_llm(llm_payload)
    if providers:
        provider_id = str(llm_payload.get("default_provider_id") or next(iter(providers.keys())))
        provider_payload = providers.get(provider_id) or next(iter(providers.values()))
        provider_secret = read_secret(llm_provider_api_key_secret_id(provider_id))
        return replace(
            settings,
            llm_provider=provider_payload.get("provider_type", provider_payload.get("provider", settings.llm_provider)),
            llm_base_url=normalize_provider_base_url(
                provider_payload.get("base_url", settings.llm_base_url),
                provider_payload.get("provider_type", provider_payload.get("provider", settings.llm_provider)),
            ),
            llm_enabled=llm_payload.get("enabled", provider_payload.get("enabled", settings.llm_enabled)),
            llm_timeout_seconds=llm_payload.get("timeout_seconds", settings.llm_timeout_seconds),
            llm_api_key=provider_secret or api_key or settings.llm_api_key,
        )
    return replace(
        settings,
        llm_provider=llm_payload.get("provider", settings.llm_provider),
        llm_base_url=normalize_provider_base_url(
            llm_payload.get("base_url", settings.llm_base_url),
            llm_payload.get("provider", settings.llm_provider),
        ),
        llm_enabled=llm_payload.get("enabled", settings.llm_enabled),
        llm_timeout_seconds=llm_payload.get("timeout_seconds", settings.llm_timeout_seconds),
        llm_api_key=api_key or settings.llm_api_key,
    )


def provider_runtime_configs(payload, *, read_secret) -> dict[str, ProviderRuntimeConfig]:
    llm_payload = coerce_api_payload(payload) if payload else {}
    providers = provider_payloads_from_llm(llm_payload)
    if not providers:
        settings = load_env_settings()
        provider_id = settings.llm_provider
        providers = {
            provider_id: {
                "provider_id": provider_id,
                "provider_type": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "credential_mode": "api_key" if settings.llm_api_key else "none",
                "enabled": settings.llm_enabled,
                "timeout_seconds": settings.llm_timeout_seconds,
            }
        }

    runtime_configs: dict[str, ProviderRuntimeConfig] = {}
    for provider_id, provider_payload in providers.items():
        credential_mode = str(provider_payload.get("credential_mode") or "api_key")
        if credential_mode not in SUPPORTED_CREDENTIAL_MODES:
            continue
        provider_secret = read_secret(llm_provider_api_key_secret_id(provider_id))
        legacy_api_key = read_secret(LLM_API_KEY_SECRET_ID) if provider_id in {"openai_compatible", "openai"} else None
        runtime_configs[provider_id] = ProviderRuntimeConfig(
            provider_id=provider_id,
            provider_type=str(provider_payload.get("provider_type") or provider_payload.get("provider") or provider_id),
            account_id=optional_text(provider_payload.get("account_id")),
            base_url=normalize_provider_base_url(
                provider_payload.get("base_url") or DEFAULT_PROVIDER_BASE_URLS.get(str(provider_payload.get("provider_type")), ""),
                provider_payload.get("provider_type") or provider_payload.get("provider") or provider_id,
            ),
            api_key=(provider_secret or legacy_api_key) if credential_mode == "api_key" else None,
            credential_mode=credential_mode if credential_mode in SUPPORTED_CREDENTIAL_MODES else "api_key",
            api_mode=str(provider_payload.get("api_mode") or "chat"),  # type: ignore[arg-type]
            enabled=bool_value(provider_payload.get("enabled", True)),
            models=tuple(str(item) for item in provider_payload.get("models", []) if isinstance(item, str)),
            provider_options=dict(provider_payload.get("provider_options") or {}),
        )
    return runtime_configs

def _read_with_transient_retry(reader):
    return read_with_transient_retry(reader, sleep=time.sleep)


def apply_active_api_config(settings):
    """把活动 api 快照与它用到的密钥叠到环境设置上。

    快照与密钥在同一个只读事务里读（原来是三个会话各读一样，``get_settings()`` 在一次请求里会被调很多次）；
    SQLite 忙时整个事务按 ``read_with_transient_retry`` 重试。
    """
    return _read_with_transient_retry(lambda: _apply_active_api_config(settings))


def _apply_active_api_config(settings):
    with SessionLocal() as session:
        return overlay_api_config(
            settings,
            payload=active_config_payload(session, "api"),
            read_secret=lambda secret_id: secret_value(session, secret_id),
        )


def load_llm_provider_runtime_configs() -> dict[str, ProviderRuntimeConfig]:
    """每个服务商的运行时配置（地址、模式、解密后的密钥）；快照与密钥在同一个只读事务里读。"""
    return _read_with_transient_retry(_load_llm_provider_runtime_configs)


def _load_llm_provider_runtime_configs() -> dict[str, ProviderRuntimeConfig]:
    with SessionLocal() as session:
        return provider_runtime_configs(
            active_config_payload(session, "api") or {},
            read_secret=lambda secret_id: secret_value(session, secret_id),
        )


def build_llm_client(
    settings: Any,
    *,
    provider_configs: dict[str, ProviderRuntimeConfig] | None = None,
    retry_backoff_seconds: float | None = None,
) -> LLMClient:
    """运行时 LLMClient 的唯一构造点:按设置建客户端,不管启用与否(是否可用由调用方先判)。

    settings 必传,由调用方 get_settings() 取得:settings 要读本模块的活动快照,
    本模块反向 import settings(哪怕函数内延迟)会构成依赖环,被全包环守卫拒绝。
    retry_backoff_seconds 为 None 时用 LLMClient 的默认值(不退避);现状只有
    场景运行器传(B09-09)。
    """
    if provider_configs is None:
        provider_configs = load_llm_provider_runtime_configs()
    extra: dict[str, Any] = {}
    if retry_backoff_seconds is not None:
        extra["retry_backoff_seconds"] = retry_backoff_seconds
    return LLMClient(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
        provider_configs=provider_configs,
        **extra,
    )


def build_runtime_llm_client(
    *,
    settings: Any,
    provider_configs: dict[str, ProviderRuntimeConfig] | None = None,
) -> tuple[LLMClient | None, bool]:
    """fail-closed 版工厂:LLM 未启用返回 (None, False),否则 (build_llm_client(...), True)。"""
    if not settings.llm_enabled:
        return None, False
    return build_llm_client(settings, provider_configs=provider_configs), True
