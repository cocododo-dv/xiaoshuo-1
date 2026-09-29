from __future__ import annotations

from novel_system.services.llm_client import DEFAULT_PROVIDER_BASE_URLS, SUPPORTED_PROVIDERS
from novel_system.services.llm_providers import (
    adapter_registry,
    get_adapter,
    provider_catalog,
    provider_presets,
    supported_provider_types,
)
from novel_system.services.llm_providers.base import LLMConfigurationError, ProviderAdapter
from novel_system.services.llm_provider_config import normalize_provider_base_url as _normalize_provider_base_url


EXPECTED_PROVIDER_TYPES = {
    "openai_compatible",
    "openai",
    "anthropic",
    "deepseek",
    "zhipu_glm",
    "gemini",
    "qwen_dashscope",
    "moonshot",
    "minimax",
    "doubao_ark",
    "xai",
    "ollama",
}


def test_registry_contains_all_expected_provider_types() -> None:
    assert supported_provider_types() == frozenset(EXPECTED_PROVIDER_TYPES)
    assert SUPPORTED_PROVIDERS == frozenset(EXPECTED_PROVIDER_TYPES)


def test_legacy_default_base_urls_preserved() -> None:
    assert DEFAULT_PROVIDER_BASE_URLS["openai"] == "https://api.openai.com/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["openai_compatible"] == "http://127.0.0.1:11434/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["anthropic"] == "https://api.anthropic.com/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["deepseek"] == "https://api.deepseek.com/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["zhipu_glm"] == "https://open.bigmodel.cn/api/paas/v4"
    assert DEFAULT_PROVIDER_BASE_URLS["gemini"] == "https://generativelanguage.googleapis.com/v1beta"


def test_new_adapter_default_base_urls() -> None:
    assert DEFAULT_PROVIDER_BASE_URLS["qwen_dashscope"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["moonshot"] == "https://api.moonshot.cn/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["minimax"] == "https://api.minimaxi.com/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["doubao_ark"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert DEFAULT_PROVIDER_BASE_URLS["xai"] == "https://api.x.ai/v1"
    assert DEFAULT_PROVIDER_BASE_URLS["ollama"] == "http://127.0.0.1:11434"


def test_every_adapter_exposes_complete_metadata() -> None:
    for provider_type, adapter in adapter_registry().items():
        assert isinstance(adapter, ProviderAdapter)
        assert adapter.provider_type == provider_type
        assert adapter.label_zh
        assert adapter.default_api_mode in {"chat", "responses"}
        assert adapter.credential_modes
        assert set(adapter.credential_modes) <= {"api_key", "none"}


def test_get_adapter_raises_for_unknown_provider() -> None:
    try:
        get_adapter("definitely_not_registered")
    except LLMConfigurationError as exc:
        assert exc.code == "LLM_PROVIDER_UNSUPPORTED"
    else:
        raise AssertionError("expected LLMConfigurationError")


def test_provider_catalog_derived_from_registry() -> None:
    catalog = provider_catalog()
    assert set(catalog) == EXPECTED_PROVIDER_TYPES
    assert catalog["openai_compatible"]["label"] == "本地 / OpenAI 兼容"
    assert "none" in catalog["openai_compatible"]["credential_modes"]
    assert catalog["zhipu_glm"]["label"] == "智谱 GLM"
    assert catalog["qwen_dashscope"]["category"] == "cn"


def test_presets_reference_registered_provider_types_and_unique_ids() -> None:
    presets = provider_presets()
    seen_ids: set[str] = set()
    registered = supported_provider_types()
    for preset in presets:
        assert preset.preset_id not in seen_ids
        seen_ids.add(preset.preset_id)
        assert preset.provider_type in registered
        assert preset.category in {"cn", "international", "relay", "local", "custom"}
    # 主流厂商与中转都必须在预设里
    assert {"openai", "anthropic", "gemini", "deepseek", "zhipu_glm", "qwen_dashscope", "moonshot", "minimax", "doubao_ark", "xai", "ollama"} <= seen_ids
    assert {"openrouter", "siliconflow", "oneapi", "custom"} <= seen_ids


def test_relay_presets_ride_openai_compatible() -> None:
    for preset in provider_presets():
        if preset.is_relay:
            assert preset.provider_type == "openai_compatible"


def test_base_url_v1_appending_follows_adapter_flags() -> None:
    # 与旧硬编码集合保持一致的三家 + 新增声明 True 的三家
    assert _normalize_provider_base_url("https://api.openai.com", "openai") == "https://api.openai.com/v1"
    assert _normalize_provider_base_url("https://api.deepseek.com", "deepseek") == "https://api.deepseek.com/v1"
    assert _normalize_provider_base_url("http://127.0.0.1:11434", "openai_compatible") == "http://127.0.0.1:11434/v1"
    assert _normalize_provider_base_url("https://api.moonshot.cn", "moonshot") == "https://api.moonshot.cn/v1"
    assert _normalize_provider_base_url("https://api.x.ai", "xai") == "https://api.x.ai/v1"
    assert _normalize_provider_base_url("https://api.minimaxi.com", "minimax") == "https://api.minimaxi.com/v1"
    # 不追加的:anthropic / gemini / 通义 / 豆包 / ollama
    assert _normalize_provider_base_url("https://api.anthropic.com", "anthropic") == "https://api.anthropic.com"
    assert _normalize_provider_base_url("https://ark.cn-beijing.volces.com/api/v3", "doubao_ark") == "https://ark.cn-beijing.volces.com/api/v3"
    assert _normalize_provider_base_url("http://127.0.0.1:11434", "ollama") == "http://127.0.0.1:11434"
