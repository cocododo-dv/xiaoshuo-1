"""Base adapter for providers that only speak OpenAI Chat Completions.

Covers the Bearer-auth + ``/chat/completions`` + ``response_format json_object``
protocol shared by DeepSeek, Zhipu GLM and most mainstream Chinese providers.
Subclasses override :meth:`chat_extra_payload` to add provider-native knobs
(thinking flags etc.).
"""

from __future__ import annotations

from typing import Any, ClassVar

from novel_system.services.llm_providers.base import (
    AdapterHTTPRequest,
    CompletionProbeRequest,
    LLMRequest,
    ProviderAdapter,
    ProviderRuntimeConfig,
)
from novel_system.services.llm_providers.openai_common import (
    bearer_generate_headers,
    extract_chat_finish_reason,
    extract_openai_output_text,
)


class OpenAIChatFamilyAdapter(ProviderAdapter):
    default_api_mode: ClassVar[str] = "chat"
    # MiniMax's compatible endpoint documents no response_format support; the
    # JSON contract then rides on prompts + the client-side JSON extractor.
    supports_json_response_format: ClassVar[bool] = True
    # Some providers (MiniMax, xAI) deprecate max_tokens for max_completion_tokens.
    max_tokens_param: ClassVar[str] = "max_tokens"

    def chat_extra_payload(
        self,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Provider-specific payload additions; returns (extra_fields, native_reasoning)."""
        return {}, None

    def build_request(self, request: LLMRequest, provider_config: ProviderRuntimeConfig) -> AdapterHTTPRequest:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            self.max_tokens_param: request.max_output_tokens,
        }
        # §7 anti-mean sampling — inject decoding-level penalties when set
        if request.frequency_penalty is not None:
            payload["frequency_penalty"] = request.frequency_penalty
        if request.presence_penalty is not None:
            payload["presence_penalty"] = request.presence_penalty
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.response_format == "json_object" and self.supports_json_response_format:
            payload["response_format"] = {"type": "json_object"}

        extra_payload, native_reasoning = self.chat_extra_payload(request, provider_config)
        payload.update(extra_payload)

        return AdapterHTTPRequest(
            endpoint="/chat/completions",
            payload=payload,
            headers=bearer_generate_headers(provider_config),
            native_reasoning=native_reasoning,
        )

    def extract_output_text(self, body: dict[str, Any], *, request: LLMRequest) -> str:
        return extract_openai_output_text(body, api_mode="chat")

    def extract_finish_reason(self, body: dict[str, Any], *, api_mode: str) -> str | None:
        return extract_chat_finish_reason(body)

    def completion_probe_request(
        self,
        *,
        base_url: str,
        model: str,
        api_mode: str,
        api_key: str | None,
        provider_options: dict[str, Any] | None = None,
    ) -> CompletionProbeRequest | None:
        return CompletionProbeRequest(
            url=f"{base_url}/chat/completions",
            payload={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "temperature": 0,
                "max_tokens": 8,
                "stream": False,
            },
            headers=self.auth_headers(api_key=api_key, provider_options=provider_options),
            api_mode="chat",
            endpoint="/chat/completions",
        )


def thinking_enabled_on_demand(request: LLMRequest) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``thinking: {"type": "enabled"}`` only for medium / high reasoning; nothing otherwise
    (DeepSeek, Kimi — some of their models reject an explicit ``disabled``)."""
    if request.reasoning_level in {"medium", "high"}:
        native_reasoning = {"type": "enabled"}
        return {"thinking": native_reasoning}, native_reasoning
    return {}, None


def thinking_enabled_or_disabled(request: LLMRequest) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Always send ``thinking``: ``enabled`` for medium / high reasoning, else ``disabled`` (GLM, Doubao)."""
    native_reasoning = {"type": "enabled" if request.reasoning_level in {"medium", "high"} else "disabled"}
    return {"thinking": native_reasoning}, native_reasoning
