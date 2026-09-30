"""Shared LLM provider primitives.

This module hosts the request/response dataclasses, error hierarchy and the
``ProviderAdapter`` contract. ``llm_client`` re-exports the public names so
existing importers (`from novel_system.services.llm_client import LLMRequest`)
keep working unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol


SUPPORTED_RESPONSE_FORMATS = {"json_object", "text"}
SUPPORTED_API_MODES = {"responses", "chat"}
SUPPORTED_REASONING_LEVELS = {"off", "low", "medium", "high"}
SUPPORTED_CREDENTIAL_MODES = {"api_key", "none"}


@dataclass(slots=True, frozen=True)
class ProviderRuntimeConfig:
    provider_id: str
    provider_type: str
    base_url: str
    api_key: str | None = None
    account_id: str | None = None
    enabled: bool = True
    credential_mode: Literal["api_key", "none"] = "api_key"
    api_mode: Literal["responses", "chat"] = "chat"
    models: tuple[str, ...] = ()
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class LLMRequest:
    model: str
    messages: list[dict[str, str]]
    temperature: float
    max_output_tokens: int
    response_format: str
    provider: str | None = None
    timeout_seconds: float | None = None
    api_mode: Literal["responses", "chat"] = "responses"
    node_id: str | None = None
    provider_id: str | None = None
    account_id: str | None = None
    reasoning_level: Literal["off", "low", "medium", "high"] = "medium"
    response_schema: dict[str, Any] | None = None
    credential_mode: Literal["api_key", "none"] | None = None
    provider_options: dict[str, Any] | None = None
    # §7 anti-mean sampling — decoding-level levers available on OpenAI-compatible APIs
    frequency_penalty: float | None = None   # 0.0–2.0; penalise repeated tokens
    presence_penalty: float | None = None    # 0.0–2.0; penalise topic repetition
    top_p: float | None = None               # nucleus sampling; None = provider default
    # 结构化输出降级阶梯(LLMClient 连通性加固):False 时不向 provider 发送
    # response_format 约束(部分中转/本地引擎连 json_object 模式都不支持),
    # 客户端仍按 response_format=json_object 解析文本——prompt 本身要求输出 JSON。
    wire_response_format: bool = True


@dataclass(slots=True, frozen=True)
class LLMResponse:
    request_id: str | None
    provider: str
    model: str
    text: str
    structured_output: dict[str, Any] | None
    response_format: str
    raw_response: dict[str, Any]
    usage: dict[str, int]
    finish_reason: str | None = None
    native_reasoning: dict[str, Any] | None = None
    attempt_count: int = 1
    max_retries: int = 0
    retryable: bool = False
    # ``usage`` is normalized for callers.  These fields preserve whether the
    # provider actually supplied the source values, so accounting never treats
    # normalizer-added zeroes as measured usage.  ``None`` is the safe legacy
    # default for existing fake clients whose provenance is unknown.
    raw_usage: dict[str, Any] | None = None
    usage_present: bool | None = None
    usage_complete: bool | None = None
    llm_call_id: str | None = None


class LLMClientError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}


class LLMConfigurationError(LLMClientError):
    pass


class LLMTimeoutError(LLMClientError):
    pass


class LLMHTTPError(LLMClientError):
    pass


class LLMRateLimitError(LLMHTTPError):
    pass


class LLMResponseError(LLMClientError):
    pass


LLMDispatchKind = Literal[
    "initial",
    "transport_retry",
    "response_parse_retry",
    "api_mode_degrade",
    "structured_output_degrade",
    "missing_text_degrade",
    "system_probe",
]


class LLMAttemptHook(Protocol):
    """Internal lifecycle hook around every physical provider POST."""

    def before_dispatch(self, *, request: LLMRequest, dispatch_kind: LLMDispatchKind) -> object:
        """Reserve and mark one attempt dispatched; raising prevents the POST."""

    def after_response(
        self,
        handle: object,
        *,
        request: LLMRequest,
        response: LLMResponse,
        latency_ms: int,
    ) -> None:
        """Settle a successfully parsed provider response."""

    def after_error(
        self,
        handle: object,
        *,
        request: LLMRequest,
        error: BaseException,
        raw_response: dict[str, Any] | None,
        provider_request_id: str | None,
        latency_ms: int,
    ) -> None:
        """Settle a transport, HTTP, or response-parse failure."""


class OnlineAccountedExecution(ABC):
    """Explicit capability for online execution with physical-attempt accounting."""

    @abstractmethod
    def generate_accounted(
        self,
        request: LLMRequest,
        *,
        accounting_hook: LLMAttemptHook,
    ) -> LLMResponse:
        """Execute online generation while forwarding the required attempt hook."""


@dataclass(slots=True, frozen=True)
class AdapterHTTPRequest:
    """A fully built provider HTTP call for one generate() attempt."""

    endpoint: str
    payload: dict[str, Any]
    headers: dict[str, str]
    native_reasoning: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class ModelListRequest:
    """How to ask the provider for its model catalog (GET)."""

    url: str
    headers: dict[str, str]


@dataclass(slots=True, frozen=True)
class CompletionProbeRequest:
    """A minimal generation call used by connection tests (POST)."""

    url: str
    payload: dict[str, Any]
    headers: dict[str, str]
    api_mode: str
    endpoint: str


class ProviderAdapter(ABC):
    """Per-provider protocol knowledge: request building, parsing, probing.

    Adapters are stateless singletons registered in ``registry``; every hook
    receives the request/config explicitly.
    """

    provider_type: ClassVar[str]
    label_zh: ClassVar[str]
    default_base_url: ClassVar[str]
    credential_modes: ClassVar[tuple[str, ...]] = ("api_key",)
    default_api_mode: ClassVar[str] = "chat"
    # Mirrors the legacy hardcoded set in system_config._normalize_provider_base_url:
    # bare-host base URLs get "/v1" appended when True.
    appends_v1_to_bare_host: ClassVar[bool] = False
    # Provider catalog grouping for the frontend preset picker.
    category: ClassVar[str] = "international"
    docs_url: ClassVar[str | None] = None

    # -- generate path -----------------------------------------------------
    @abstractmethod
    def build_request(self, request: LLMRequest, provider_config: ProviderRuntimeConfig) -> AdapterHTTPRequest:
        """Build endpoint/payload/headers for one generation call."""

    @abstractmethod
    def extract_output_text(self, body: dict[str, Any], *, request: LLMRequest) -> str:
        """Extract the text completion; raise LLMResponseError(LLM_RESPONSE_MISSING_TEXT) when absent."""

    @abstractmethod
    def extract_finish_reason(self, body: dict[str, Any], *, api_mode: str) -> str | None:
        """Extract the provider finish/stop reason if present."""

    def normalize_usage(self, body: dict[str, Any]) -> dict[str, int] | None:
        """Provider-specific usage extraction; None = use the generic normalizer."""
        return None

    def protocol_hint(
        self,
        *,
        status_code: int,
        endpoint: str,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
    ) -> dict[str, str] | None:
        """Optional actionable hint for protocol-shaped HTTP failures."""
        return None

    # -- probe path (connection test / model listing) ----------------------
    def auth_headers(self, *, api_key: str | None, provider_options: dict[str, Any] | None = None) -> dict[str, str]:
        if api_key:
            return {"Authorization": f"Bearer {api_key}"}
        return {}

    def list_models_request(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider_options: dict[str, Any] | None = None,
    ) -> ModelListRequest | None:
        """Return how to fetch the live model list; None = unsupported."""
        return ModelListRequest(
            url=f"{base_url}/models",
            headers=self.auth_headers(api_key=api_key, provider_options=provider_options),
        )

    def normalize_listed_model_ids(self, model_ids: list[str]) -> list[str]:
        return model_ids

    def completion_probe_request(
        self,
        *,
        base_url: str,
        model: str,
        api_mode: str,
        api_key: str | None,
        provider_options: dict[str, Any] | None = None,
    ) -> CompletionProbeRequest | None:
        """Return a minimal generation probe; None = completion check skipped."""
        return None

    # -- catalog ------------------------------------------------------------
    def catalog_entry(self) -> dict[str, Any]:
        return {
            "label": self.label_zh,
            "credential_modes": list(self.credential_modes),
            "default_base_url": self.default_base_url,
            "default_api_mode": self.default_api_mode,
            "category": self.category,
            "docs_url": self.docs_url,
        }
