from __future__ import annotations

from typing import Any, ClassVar

from novel_system.services.llm_providers.base import (
    AdapterHTTPRequest,
    CompletionProbeRequest,
    LLMRequest,
    LLMResponseError,
    ProviderAdapter,
    ProviderRuntimeConfig,
)


CLAUDE_THINKING_BUDGETS = {"medium": 4096, "high": 10000}
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


def anthropic_reasoning(reasoning_level: str, max_output_tokens: int) -> dict[str, Any] | None:
    if reasoning_level not in CLAUDE_THINKING_BUDGETS:
        return None
    budget = min(CLAUDE_THINKING_BUDGETS[reasoning_level], max(1024, max_output_tokens - 1))
    return {"type": "enabled", "budget_tokens": budget}


class AnthropicAdapter(ProviderAdapter):
    provider_type: ClassVar[str] = "anthropic"
    label_zh: ClassVar[str] = "Claude / Anthropic"
    default_base_url: ClassVar[str] = "https://api.anthropic.com/v1"
    docs_url: ClassVar[str | None] = "https://docs.anthropic.com"

    def build_request(self, request: LLMRequest, provider_config: ProviderRuntimeConfig) -> AdapterHTTPRequest:
        system_prompts = [message["content"] for message in request.messages if message.get("role") == "system"]
        messages = [
            {"role": "assistant" if message.get("role") == "assistant" else "user", "content": message["content"]}
            for message in request.messages
            if message.get("role") != "system"
        ]
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages or [{"role": "user", "content": ""}],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if system_prompts:
            payload["system"] = "\n\n".join(system_prompts)
        native_reasoning = anthropic_reasoning(request.reasoning_level, request.max_output_tokens)
        if native_reasoning:
            payload["thinking"] = native_reasoning
        # §7 anti-mean sampling: the Anthropic Messages API supports top_p but NOT
        # frequency_penalty/presence_penalty (those are OpenAI-only) — forward only the
        # legal parameter so a configured top_p is no longer silently dropped here.
        if request.top_p is not None:
            payload["top_p"] = request.top_p

        headers = {"Content-Type": "application/json"}
        headers["anthropic-version"] = str(
            provider_config.provider_options.get("anthropic_version", DEFAULT_ANTHROPIC_VERSION)
        )
        if provider_config.api_key:
            headers["x-api-key"] = provider_config.api_key

        return AdapterHTTPRequest(
            endpoint="/messages",
            payload=payload,
            headers=headers,
            native_reasoning=native_reasoning,
        )

    def extract_output_text(self, body: dict[str, Any], *, request: LLMRequest) -> str:
        content_parts = []
        for content in body.get("content", []):
            if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                content_parts.append(content["text"])
        if content_parts:
            return "".join(content_parts)
        raise LLMResponseError(
            "LLM_RESPONSE_MISSING_TEXT",
            "llm provider response did not include text output",
        )

    def extract_finish_reason(self, body: dict[str, Any], *, api_mode: str) -> str | None:
        stop_reason = body.get("stop_reason")
        return stop_reason if isinstance(stop_reason, str) else None

    def auth_headers(self, *, api_key: str | None, provider_options: dict[str, Any] | None = None) -> dict[str, str]:
        options = provider_options or {}
        headers = {"anthropic-version": str(options.get("anthropic_version", DEFAULT_ANTHROPIC_VERSION))}
        if api_key:
            headers["x-api-key"] = api_key
        return headers

    # list_models_request: the base ``GET {base}/models`` with the headers above.

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
            url=f"{base_url}/messages",
            payload={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
                "temperature": 0,
            },
            headers={
                "Content-Type": "application/json",
                **self.auth_headers(api_key=api_key, provider_options=provider_options),
            },
            api_mode="chat",
            endpoint="/messages",
        )
