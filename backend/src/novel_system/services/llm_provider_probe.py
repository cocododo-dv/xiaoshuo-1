"""「测试连接」：模型列表探测与最小生成探测（从 system_config / llm_accounting 拆出，B09-11 / B09-12）。

``run_provider_probe``：连接 → 模型名 → 最小生成三项检查的编排（「测试连接」按钮与 test-provider 接口）；
``fetch_model_listing``：拉服务的模型列表（「从服务拉取模型」与前一项共用）；
``probe_completion``：最小生成探测，不走 ``LLMClient``（各家探测载荷由适配器单独构造），但仍然是一次真实的
供应商调用：父行 + 物理尝试行与正式调用同样的不变量（``execute_accounted_completion_probe``），结果只作诊断，
账本才是权威。探测的超时始终有限且短，与生成的超时设置无关。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import execute_accounted_call
from novel_system.services.llm_accounting_types import LLMCallContext, elapsed_ms, llm_failure_code
from novel_system.services.llm_audit import sanitize_audit_summary
from novel_system.services.llm_provider_config import (
    bool_value,
    coerce_api_payload,
    float_value,
    httpx_trust_env_for_base_url,
    normalize_provider_base_url,
    normalize_provider_model_id,
    normalize_provider_model_ids,
)
from novel_system.services.llm_providers import adapter_registry
from novel_system.services.llm_providers.base import (
    SUPPORTED_CREDENTIAL_MODES,
    LLMAttemptHook,
    LLMClientError,
    LLMHTTPError,
    LLMRequest,
    LLMResponse,
    OnlineAccountedExecution,
    ProviderAdapter,
)
from novel_system.services.llm_providers.usage import extract_raw_usage, normalize_raw_usage
from novel_system.services.llm_routing import SUPPORTED_PROVIDERS
from novel_system.services.system_config_secrets import LLM_API_KEY_SECRET_ID, llm_provider_api_key_secret_id
from novel_system.services.value_coercion import optional_text

# 探活调用向记账层申报的输出预算(详见 probe_completion 内注释)
PROBE_ACCOUNTING_OUTPUT_BUDGET = 1024
# 「测试连接」的超时与长文本生成上限无关，探测必须有限且短。
PROVIDER_PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AccountedCompletionProbeResult:
    llm_call_id: str
    response: httpx.Response | None
    error_code: str | None
    error_message: str | None


class _AccountedCompletionProbeExecution(OnlineAccountedExecution):
    """Raw provider probe transport that still obeys the physical-attempt ledger."""

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
        trust_env: bool,
    ) -> None:
        self.url = url
        self.headers = headers
        self.payload = payload
        self.timeout_seconds = timeout_seconds
        self.trust_env = trust_env
        self.response: httpx.Response | None = None

    def generate_accounted(
        self,
        request: LLMRequest,
        *,
        accounting_hook: LLMAttemptHook,
    ) -> LLMResponse:
        handle = accounting_hook.before_dispatch(
            request=request,
            dispatch_kind="system_probe",
        )
        started_at = time.perf_counter()
        try:
            response = httpx.post(
                self.url,
                headers=self.headers,
                json=self.payload,
                timeout=self.timeout_seconds,
                trust_env=self.trust_env,
            )
            self.response = response
        except httpx.RequestError as exc:
            error = LLMClientError(
                "LLM_PROVIDER_TRANSPORT_ERROR",
                str(exc),
                details={"exception_type": exc.__class__.__name__},
            )
            accounting_hook.after_error(
                handle,
                request=request,
                error=error,
                raw_response=None,
                provider_request_id=None,
                latency_ms=elapsed_ms(started_at),
            )
            raise error from exc

        body = _probe_response_body(response)
        request_id = _probe_request_id(response, body)
        if not response.is_success:
            error = LLMHTTPError(
                "LLM_PROVIDER_HTTP_ERROR",
                _probe_error_message(response, body),
                status_code=response.status_code,
                details={
                    "status_code": response.status_code,
                    "response_body": body,
                },
            )
            accounting_hook.after_error(
                handle,
                request=request,
                error=error,
                raw_response=body,
                provider_request_id=request_id,
                latency_ms=elapsed_ms(started_at),
            )
            raise error

        raw_usage = extract_raw_usage(body)
        normalized = normalize_raw_usage(raw_usage)
        usage = (
            {
                "input_tokens": normalized.prompt_tokens,
                "output_tokens": normalized.completion_tokens,
                "total_tokens": normalized.total_tokens,
            }
            if normalized is not None
            else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        llm_response = LLMResponse(
            request_id=request_id,
            provider=request.provider or "unknown",
            model=request.model,
            text=_probe_response_text(body),
            structured_output=None,
            response_format="text",
            raw_response=body,
            usage=usage,
            finish_reason=_probe_finish_reason(body),
            raw_usage=raw_usage,
            usage_present=raw_usage is not None,
            usage_complete=normalized is not None,
        )
        accounting_hook.after_response(
            handle,
            request=request,
            response=llm_response,
            latency_ms=elapsed_ms(started_at),
        )
        return llm_response


def execute_accounted_completion_probe(
    session: Session,
    request: LLMRequest,
    context: LLMCallContext,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    trust_env: bool,
) -> AccountedCompletionProbeResult:
    """Execute one raw completion probe with the same parent/attempt invariants."""

    call_id = f"llm_probe_{uuid.uuid4().hex}"
    execution = _AccountedCompletionProbeExecution(
        url=url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        trust_env=trust_env,
    )
    error_code: str | None = None
    error_message: str | None = None
    try:
        execute_accounted_call(
            session,
            execution,
            request,
            context,
            llm_call_id=call_id,
        )
    except Exception as exc:  # probe result is diagnostic; the ledger remains authoritative
        error_code = llm_failure_code(exc)
        error_message = str(exc)
    parent = session.get(LlmCall, call_id)
    if parent is not None:
        parent.request_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.request_payload_summary or {}),
                "probe_endpoint": url,
                "probe_method": "POST",
            }
        )
        parent.response_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.response_payload_summary or {}),
                "probe_status_code": (
                    execution.response.status_code
                    if execution.response is not None
                    else None
                ),
                "probe_error_code": error_code,
            }
        )
        session.commit()
    return AccountedCompletionProbeResult(
        llm_call_id=call_id,
        response=execution.response,
        error_code=error_code,
        error_message=error_message,
    )


def _probe_response_body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"text": response.text[:200]}
    return dict(payload) if isinstance(payload, dict) else {"data": payload}


def _probe_request_id(response: httpx.Response, body: dict[str, Any]) -> str | None:
    for value in (
        response.headers.get("x-request-id"),
        response.headers.get("request-id"),
        body.get("id"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _probe_response_text(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    for key in ("output_text", "text"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _probe_finish_reason(body: dict[str, Any]) -> str | None:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("finish_reason")
        if isinstance(value, str):
            return value
    value = body.get("status")
    return value if isinstance(value, str) else None


def _probe_error_message(response: httpx.Response, body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(body.get("message"), str):
        return str(body["message"])
    if isinstance(body.get("text"), str) and body["text"]:
        return str(body["text"])
    return f"provider returned status {response.status_code}"


def provider_error_summary(response: httpx.Response) -> str:
    """服务报错的一句话：error.message / message / 非 JSON 正文的前 200 字，都没有就报状态码。"""
    return _probe_error_message(response, _probe_response_body(response))


def completion_error_summary(response: httpx.Response, *, api_mode: str, endpoint: str) -> str:
    if response.status_code == 404 and api_mode == "responses" and endpoint == "/responses":
        return (
            "Responses API endpoint returned 404; this provider or relay may only support Chat Completions. "
            "Set api_mode to chat and sync node routes, or use a provider that supports the Responses API."
        )
    return provider_error_summary(response)


def probe_timeout_seconds(
    value: Any,
    *,
    default_seconds: float,
    maximum_seconds: float = PROVIDER_PROBE_TIMEOUT_SECONDS,
) -> float:
    """连通性探测始终有限:0(生成不限时)对"测试连接"没有意义,只会让按钮空转。

    探测问的是"这个地址通不通",不是"这个模型写得慢不慢"。
    """
    if value is None:
        return min(default_seconds, maximum_seconds)
    timeout_seconds = float_value(value, "timeout_seconds")
    if timeout_seconds <= 0:
        return min(default_seconds, maximum_seconds)
    return min(timeout_seconds, maximum_seconds)


def requested_probe_model(payload: dict[str, Any]) -> str | None:
    explicit_model = optional_text(payload.get("model"))
    if explicit_model:
        return explicit_model
    models = payload.get("models")
    if isinstance(models, list):
        for model in models:
            candidate = optional_text(model)
            if candidate:
                return candidate
    return None


def extract_model_ids(response: httpx.Response) -> list[str]:
    try:
        payload = response.json()
    except ValueError:
        return []
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        if not candidates:
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(payload)

    model_ids: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            normalized = normalize_provider_model_id(item)
            if normalized:
                model_ids.append(normalized)
        elif isinstance(item, dict):
            for key in ("id", "name", "model"):
                value = optional_text(item.get(key))
                if value:
                    normalized = normalize_provider_model_id(value)
                    if normalized:
                        model_ids.append(normalized)
                    break
    return list(dict.fromkeys(model_ids))


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """一次探测打到哪里：服务类型、规范化后的地址、密钥、端点模式与有限的超时。"""

    provider: str
    base_url: str
    api_key: str | None
    provider_options: dict[str, Any]
    api_mode: str
    timeout_seconds: float

    @property
    def trust_env(self) -> bool:
        return httpx_trust_env_for_base_url(self.base_url)


@dataclass(frozen=True, slots=True)
class ModelListing:
    """拉模型列表的结果：服务没有列表接口（``supported`` 为假）、传输失败（``error``）或一个 HTTP 响应。"""

    supported: bool
    response: httpx.Response | None = None
    error: str | None = None
    latency_ms: int = 0


def fetch_model_listing(adapter: ProviderAdapter, target: ProbeTarget) -> ModelListing:
    list_request = adapter.list_models_request(
        base_url=target.base_url, api_key=target.api_key, provider_options=target.provider_options
    )
    if list_request is None:
        return ModelListing(supported=False)
    started_at = time.perf_counter()
    try:
        response = httpx.get(
            list_request.url,
            headers=list_request.headers,
            timeout=target.timeout_seconds,
            trust_env=target.trust_env,
        )
    except httpx.RequestError as exc:
        return ModelListing(supported=True, error=str(exc), latency_ms=elapsed_ms(started_at))
    return ModelListing(supported=True, response=response, latency_ms=elapsed_ms(started_at))


def listed_model_ids(adapter: ProviderAdapter, response: httpx.Response) -> list[str]:
    return normalize_provider_model_ids(adapter.normalize_listed_model_ids(extract_model_ids(response)))


def probe_completion(session: Session, target: ProbeTarget, *, model: str) -> dict[str, Any]:
    adapter = adapter_registry().get(target.provider)
    probe = (
        adapter.completion_probe_request(
            base_url=target.base_url,
            model=model,
            api_mode=target.api_mode,
            api_key=target.api_key,
            provider_options=target.provider_options,
        )
        if adapter is not None
        else None
    )
    if probe is None:
        return {
            "ok": None,
            "status_code": None,
            "api_mode": target.api_mode,
            "endpoint": None,
            "message": f"completion check skipped for provider {target.provider}",
        }
    # 记账申报的输出预算必须 ≥ adapter 探活载荷真正允许的输出(各家 wire 上限 8),
    # 并给两类真实偏差留余量:厂商对话模板使 prompt 计数高于本地估算(实测 ping=11 vs 估 8)、
    # 思考型后端可能不按 max_tokens 截断 reasoning tokens。曾申报 1 → 预留 9 < 实际 19,
    # 探活必然触发 LLM_USAGE_EXCEEDS_RESERVATION 拦截(probe 是 system scope,不入场景预算,
    # 超配无成本)。
    request = LLMRequest(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        temperature=0.0,
        max_output_tokens=PROBE_ACCOUNTING_OUTPUT_BUDGET,
        response_format="text",
        provider=target.provider,
        timeout_seconds=target.timeout_seconds,
        api_mode=probe.api_mode,
        node_id="provider_probe",
        provider_options=target.provider_options or {},
    )
    accounted = execute_accounted_completion_probe(
        session,
        request,
        LLMCallContext(
            scope_type="system",
            scope_id="provider_probe",
            node_id="provider_probe",
            step="completion_probe",
        ),
        url=probe.url,
        headers=probe.headers,
        payload=probe.payload,
        timeout_seconds=target.timeout_seconds,
        trust_env=target.trust_env,
    )
    response = accounted.response
    if response is None:
        return {
            "ok": False,
            "status_code": None,
            "api_mode": probe.api_mode,
            "endpoint": probe.endpoint,
            "message": accounted.error_message or "completion probe failed",
            "error_code": accounted.error_code,
            "llm_call_id": accounted.llm_call_id,
        }
    result = {
        "ok": response.is_success and accounted.error_code is None,
        "status_code": response.status_code,
        "model": model,
        "api_mode": probe.api_mode,
        "endpoint": probe.endpoint,
        "message": (
            "minimal completion succeeded"
            if response.is_success and accounted.error_code is None
            else accounted.error_message or "completion probe accounting failed"
            if response.is_success
            else completion_error_summary(
                response,
                api_mode=probe.api_mode,
                endpoint=probe.endpoint,
            )
        ),
        "error_code": accounted.error_code,
        "llm_call_id": accounted.llm_call_id,
    }
    if response.status_code == 404 and probe.api_mode == "responses":
        result["next_action"] = "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider"
    return result


def probe_target(provider_payload: dict[str, Any], *, read_secret: Callable[[str], str | None]) -> ProbeTarget:
    """「测试连接」请求体 → 探测目标：校验服务类型、地址与凭据方式，取密钥（请求带的 > 这个服务存的 > 旧版单服务密钥）。"""
    provider = str(provider_payload.get("provider_type") or provider_payload.get("provider") or "openai_compatible")
    if provider not in SUPPORTED_PROVIDERS:
        raise DomainError("CONFIG_PROVIDER_UNSUPPORTED", f"unsupported provider {provider}", status_code=422)

    base_url = normalize_provider_base_url(provider_payload.get("base_url"), provider)
    if not base_url:
        raise DomainError("CONFIG_PROVIDER_INVALID", "provider base_url is required", status_code=422)

    provider_id = optional_text(provider_payload.get("provider_id"))
    default_credential_mode = "none" if provider_id and not provider_payload.get("api_key") else "api_key"
    credential_mode = str(provider_payload.get("credential_mode") or default_credential_mode)
    if credential_mode not in SUPPORTED_CREDENTIAL_MODES:
        raise DomainError(
            "CONFIG_PROVIDER_INVALID",
            f"unsupported credential_mode {credential_mode}",
            status_code=422,
        )
    api_key = None
    if credential_mode == "api_key":
        provider_secret = read_secret(llm_provider_api_key_secret_id(provider_id)) if provider_id else None
        api_key = provider_payload.get("api_key") or provider_secret or read_secret(LLM_API_KEY_SECRET_ID)
    return ProbeTarget(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        provider_options=dict(provider_payload.get("provider_options") or {}),
        api_mode=str(provider_payload.get("api_mode") or "chat"),
        timeout_seconds=probe_timeout_seconds(provider_payload.get("timeout_seconds"), default_seconds=10.0),
    )


def run_provider_probe(
    session: Session,
    payload: dict[str, Any],
    *,
    read_secret: Callable[[str], str | None],
) -> dict[str, Any]:
    """连接 → 模型名 → 最小生成（要求了才做）三项检查；返回 ``{ok, status_code, latency_ms, message,
    available_models, checks}``。列表接口不可用时，生成探测通过也算这个模型可用。"""
    provider_payload = coerce_api_payload(payload)
    target = probe_target(provider_payload, read_secret=read_secret)
    requested_model = requested_probe_model(provider_payload)
    should_check_completion = bool(requested_model) and bool_value(provider_payload.get("check_completion", False))
    adapter = adapter_registry()[target.provider]
    started_at = time.perf_counter()
    checks: dict[str, Any] = {}

    def result(
        ok: bool,
        status_code: int | None,
        message: str,
        *,
        available_models: list[str] | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "status_code": status_code,
            "latency_ms": elapsed_ms(started_at) if latency_ms is None else latency_ms,
            "message": message,
            "available_models": list(available_models or []),
            "checks": checks,
        }

    def completion() -> dict[str, Any]:
        checks["completion"] = probe_completion(session, target, model=str(requested_model))
        return checks["completion"]

    listing = fetch_model_listing(adapter, target)
    if not listing.supported:
        checks["connection"] = {"ok": None, "status_code": None, "message": "该服务不提供模型列表接口，跳过连接检查"}
        if should_check_completion:
            done = completion()
            return result(
                done["ok"] is True,
                done.get("status_code"),
                done["message"]
                if done["ok"] is not True
                else f"模型 {requested_model} 已通过最小生成探测（该服务不提供模型列表接口）",
            )
        return result(False, None, "该服务不提供模型列表接口；请填写模型名并勾选生成探测")
    if listing.response is None:
        checks["connection"] = {"ok": False, "status_code": None, "message": listing.error}
        return result(False, None, str(listing.error))

    response = listing.response
    latency_ms = elapsed_ms(started_at)
    checks["connection"] = {
        "ok": response.is_success,
        "status_code": response.status_code,
        "latency_ms": latency_ms,
        "message": "model list endpoint reached" if response.is_success else provider_error_summary(response),
    }
    if not response.is_success:
        if should_check_completion:
            done = completion()
            if done["ok"] is True:
                return result(
                    True,
                    done.get("status_code") or response.status_code,
                    f"模型 {requested_model} 已通过最小生成探测；/models 不可用：{provider_error_summary(response)}",
                )
        return result(False, response.status_code, provider_error_summary(response), latency_ms=latency_ms)

    model_ids = listed_model_ids(adapter, response)
    if requested_model:
        model_ok = requested_model in model_ids
        checks["model"] = {"ok": model_ok, "requested_model": requested_model, "available_models": model_ids}
        if not model_ok:
            available_hint = "、".join(model_ids[:5]) if model_ids else "未能从 /models 解析到模型列表"
            return result(
                False,
                response.status_code,
                f"模型 {requested_model} 未在服务返回的模型列表中出现。可用模型：{available_hint}",
                available_models=model_ids,
                latency_ms=latency_ms,
            )

    if should_check_completion:
        done = completion()
        if done["ok"] is not True:
            return result(
                False,
                done.get("status_code") or response.status_code,
                done["message"],
                available_models=model_ids,
            )

    message = (
        f"模型 {requested_model} 可用：连接、模型名、生成均通过"
        if requested_model and checks.get("completion", {}).get("ok") is True
        else (f"模型 {requested_model} 已在服务列表中找到" if requested_model else "provider probe succeeded")
    )
    return result(True, response.status_code, message, available_models=model_ids)
