"""多供应商 LLM HTTP 客户端：一次 ``generate`` = 连通性降级阶梯 × 每跳的重试循环。

路由（节点 → 服务 / 模型）在 ``llm_routing``，降级阶梯与连通性能力缓存在 ``llm_degrade``，
用量字段的解析在 ``llm_providers.usage``；这里只管发请求、分类 HTTP 响应、解析正文。
本模块照旧转出路由与降级的公开名字（各服务、工具与测试从这里 import 它们）。
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
from dataclasses import dataclass
from typing import Any

import httpx

from novel_system.services.llm_degrade import (
    CONNECTIVITY_CAPS as _CONNECTIVITY_CAPS,
    MAX_DEGRADE_HOPS,
    MAX_OUTPUT_TOKENS_CEILING,
    apply_connectivity_caps,
    degrade_request_after_failure,
    normalize_api_mode,
    record_connectivity_caps,
    structured_output_rejected,
)
from novel_system.services.llm_providers import get_adapter
from novel_system.services.llm_providers.base import (
    LLMAttemptHook,
    LLMClientError,
    LLMConfigurationError,
    LLMDispatchKind,
    LLMHTTPError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    OnlineAccountedExecution,
    ProviderRuntimeConfig,
    SUPPORTED_API_MODES,
    SUPPORTED_CREDENTIAL_MODES,
    SUPPORTED_REASONING_LEVELS,
    SUPPORTED_RESPONSE_FORMATS,
)
from novel_system.services.llm_providers.usage import (
    extract_raw_usage,
    raw_usage_is_complete,
    usage_output_tokens,
    wire_usage,
)
from novel_system.services.llm_routing import (
    DEFAULT_PROVIDER_BASE_URLS,
    SUPPORTED_PROVIDERS,
    ModelRoutingConfig,
    TaskModelConfig,
    _load_task_model_config,
    build_llm_request,
    load_model_routing_config,
    parse_model_routing_config,
    reset_model_routing_cache,
    resolve_node_route,
)

__all__ = [
    "DEFAULT_PROVIDER_BASE_URLS",
    "LLMClient",
    "LLMAttemptHook",
    "LLMClientError",
    "LLMConfigurationError",
    "LLMHTTPError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "MAX_DEGRADE_HOPS",
    "MAX_OUTPUT_TOKENS_CEILING",
    "OnlineAccountedExecution",
    "ModelRoutingConfig",
    "ProviderRuntimeConfig",
    "RETRYABLE_RESPONSE_ERROR_CODES",
    "RETRYABLE_STATUS_CODES",
    "SUPPORTED_API_MODES",
    "SUPPORTED_CREDENTIAL_MODES",
    "SUPPORTED_PROVIDERS",
    "SUPPORTED_REASONING_LEVELS",
    "SUPPORTED_RESPONSE_FORMATS",
    "TaskModelConfig",
    "_CONNECTIVITY_CAPS",
    "_load_task_model_config",
    "build_llm_request",
    "load_model_routing_config",
    "parse_model_routing_config",
    "reset_model_routing_cache",
    "resolve_node_route",
]


logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRYABLE_RESPONSE_ERROR_CODES = {
    "LLM_RESPONSE_INVALID_JSON",
    "LLM_RESPONSE_MISSING_TEXT",
    "LLM_RESPONSE_TRUNCATED",
}
# provider 用来表示"输出被 max_tokens 砍断"的 finish_reason 别名(各家写法不同)。
TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "max_output_tokens", "model_length"}
# 这两类失败都是"输出预算不够"的症状,原样重发只会复现同一个结果——跳过重试,
# 直接走降级阶梯抬预算。
BUDGET_DEGRADE_ERROR_CODES = {"LLM_RESPONSE_MISSING_TEXT", "LLM_RESPONSE_TRUNCATED"}
# 2026-09-24:正文里出现 prompt 里没有的 U+FFFD = 模型与客户端之间丢了字节(本客户端按
# 严格 UTF-8 解析,只可能是中转 / 引擎那一侧替换的)。实测中转的 opus 4 次长稿 2 次带乱码
# (同一场的首稿与修复),那一场因此作废。同一请求重发即可换回干净正文——占用普通重试额度;
# 最后一次仍带乱码就照常交回,由调用方的文本完整性闸门决定(起草稿会被拒,不会带乱码入稿)。
CORRUPTED_OUTPUT_ERROR_CODE = "LLM_RESPONSE_CORRUPTED_TEXT"
_REPLACEMENT_CHARACTER = "\ufffd"
MAX_RETRY_BACKOFF_SECONDS = 30.0

# 建连/写入/连接池握手的超时上限。生成本身可以慢(大模型长任务是正常的),
# 但"连不上"必须快速失败——否则填错 base_url 只会让界面无限转圈。
# timeout_seconds <= 0 时只保留这一层,读取(等待模型出字)不设上限。
LLM_CONNECT_TIMEOUT_SECONDS = 30.0


def _corrupted_output_characters(response: LLMResponse, request: LLMRequest) -> int:
    """正文 / 结构化输出里 U+FFFD 的个数;prompt 自己带 U+FFFD(模型可能照抄)时记 0。"""
    count = (response.text or "").count(_REPLACEMENT_CHARACTER)
    if response.structured_output is not None:
        # 结构化正文可能把它写成 \ufffd 转义,解析后才现身
        count = max(
            count,
            json.dumps(response.structured_output, ensure_ascii=False).count(_REPLACEMENT_CHARACTER),
        )
    if not count:
        return 0
    if _REPLACEMENT_CHARACTER in json.dumps(request.messages, ensure_ascii=False):
        return 0
    return count


def resolve_request_timeout(timeout_seconds: float | None) -> httpx.Timeout:
    """把配置的秒数翻译成 httpx 超时:<=0 / None = **生成**不限时。

    不限时只解掉 ``read``:等不到"第一个字"和连不上是两回事,前者是慢任务(合法),
    后者是配置错误(必须立刻报错)。``write`` / ``pool`` 仍然有限——把请求体写出去、
    从连接池取一条连接,都不存在"合法的慢"; 让它们也变成 None 是把"模型在想"的豁免
    错发给了两个纯粹的传输动作,一次卡住就永久占着 worker 线程。
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return httpx.Timeout(
            None,
            connect=LLM_CONNECT_TIMEOUT_SECONDS,
            write=LLM_CONNECT_TIMEOUT_SECONDS,
            pool=LLM_CONNECT_TIMEOUT_SECONDS,
        )
    return httpx.Timeout(timeout_seconds)


def _keepalive_socket_options() -> list[tuple[int, int, int]]:
    """TCP keepalive:让"对端已经死了"和"模型还在想"在传输层可区分。

    read 不限时是产品决定(长上下文生成慢是正常工作,不是故障),代价是半开连接
    ——中转接了 TCP、随后一个字节都不发、也不回 RST(路由被丢、网关过载)——会让
    ``client.post`` 永久阻塞,同步的雪花端点因此吃掉一个 worker 线程,作者重试几次
    就把线程池耗光。keepalive 探针由内核发,活着的对端(哪怕模型正在思考)一定会回
    ACK,所以慢生成完全不受影响;真死的对端在 ~150s 内被内核判定不可达,``post``
    抛连接错误而不是挂着。这是"不限时"唯一还能保留的活性检测。
    """
    options: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    # 平台差异:KEEPIDLE/KEEPINTVL/KEEPCNT 是 Linux 名字,macOS 只有 TCP_KEEPALIVE,
    # Windows 一个都没有——缺哪个跳哪个,退化成"只开 keepalive、用系统默认间隔"。
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPALIVE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 6)):
        option = getattr(socket, name, None)
        if option is not None:
            options.append((socket.IPPROTO_TCP, option, value))
    return options


def describe_timeout_failure(timeout_seconds: float | None) -> str:
    """不限时下唯一可能的超时是建连,消息必须说清楚,别谎报成"响应超时"。"""
    if timeout_seconds is None or timeout_seconds <= 0:
        return (
            "llm request timed out while connecting "
            f"(connect ceiling {LLM_CONNECT_TIMEOUT_SECONDS}s, no response ceiling)"
        )
    return f"llm request timed out after {timeout_seconds} seconds"


@dataclass(slots=True)
class _AttemptFailure:
    """一次物理尝试失败后的去向：``retry`` 且还有额度时退避后重发，否则抛出 ``error``。"""

    error: BaseException
    retry: bool = False
    dispatch_kind: LLMDispatchKind = "transport_retry"
    # 429 / 可重试状态码：退避时优先尊重它的 Retry-After
    backoff_response: httpx.Response | None = None
    # 抛出时的 ``from``（传输层异常、JSON 解析异常……）
    cause: BaseException | None = None


@dataclass(slots=True)
class _AttemptContext:
    """一次物理尝试的上下文：错误详情里的尝试序号、失败通知记账钩子。"""

    request: LLMRequest
    provider_config: ProviderRuntimeConfig
    endpoint: str
    attempt: int
    max_retries: int
    hook: LLMAttemptHook | None
    hook_handle: object | None
    started_at: float

    def with_attempt_metadata(self, details: dict[str, Any]) -> dict[str, Any]:
        return _with_attempt_metadata(details, attempt=self.attempt, max_retries=self.max_retries)

    def http_error_details(self, response: httpx.Response) -> dict[str, Any]:
        return _http_error_details(
            response,
            request=self.request,
            provider_config=self.provider_config,
            endpoint=self.endpoint,
            attempt=self.attempt,
            max_retries=self.max_retries,
        )

    def status_message(self, response: httpx.Response) -> str:
        return _error_message_for_status(
            response,
            request=self.request,
            provider_config=self.provider_config,
            endpoint=self.endpoint,
        )

    def notify(
        self,
        error: BaseException,
        *,
        response: httpx.Response | None = None,
        raw_response: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        _notify_attempt_error(
            self.hook,
            self.hook_handle,
            request=self.request,
            error=error,
            started_at=self.started_at,
            response=response,
            raw_response=raw_response,
            provider_request_id=provider_request_id,
        )


class LLMClient(OnlineAccountedExecution):
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.0,
        transport: httpx.BaseTransport | None = None,
        provider_configs: dict[str, ProviderRuntimeConfig] | None = None,
    ) -> None:
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds or 0.0))
        self._transport = transport
        self._provider_configs = provider_configs or {}

    def _sleep_before_retry(self, attempt: int, *, response: httpx.Response | None = None) -> None:
        """限流/瞬时故障重试前指数退避(默认 0 = 关闭,生产由 runner 开启)。

        立即重试对容量受限的中转(如 gcli2api 的 No capacity)只会三连击同一错误;
        退避给服务端腾出恢复窗口,429 时优先尊重 Retry-After。
        """
        if self._retry_backoff_seconds <= 0:
            return
        delay = self._retry_backoff_seconds * (2 ** attempt)
        if response is not None:
            retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After"))
            if retry_after is not None:
                delay = max(delay, retry_after)
        delay = min(delay, MAX_RETRY_BACKOFF_SECONDS)
        delay *= 0.8 + 0.4 * random.random()
        time.sleep(delay)

    def generate_accounted(
        self,
        request: LLMRequest,
        *,
        accounting_hook: LLMAttemptHook,
    ) -> LLMResponse:
        return self.generate(request, accounting_hook=accounting_hook)

    def generate(
        self,
        request: LLMRequest,
        *,
        accounting_hook: LLMAttemptHook | None = None,
    ) -> LLMResponse:
        """入口:api_mode 按 provider 声明归一化 + 连通性降级阶梯。

        降级阶梯(最多 MAX_DEGRADE_HOPS 跳,每跳记 warning):
        1. /responses 404 → api_mode 换 chat(中转仅支持 chat completions)
        2. json_schema 被拒(guided_grammar/compile_grammar_error/…) → 退 json_object
        3. json_object 也被拒 → 不发 response_format,仅靠 prompt 约束 JSON
        解析行为不变:response_format=json_object 的请求仍强制解析 JSON 文本。
        """
        self._validate_request(request)
        provider_config = self._resolve_provider_config(request)
        self._validate_provider(provider_config.provider_type)
        req = normalize_api_mode(request, provider_config)
        if req is not request:
            logger.info(
                "llm api_mode normalized to provider declaration: node=%s provider=%s %s -> %s",
                request.node_id, provider_config.provider_id, request.api_mode, req.api_mode,
            )
        req = apply_connectivity_caps(req, provider_config)
        entry_req = req
        hops = 0
        dispatch_kind: LLMDispatchKind = "initial"
        while True:
            try:
                response = self._generate_once(
                    req,
                    provider_config,
                    accounting_hook=accounting_hook,
                    initial_dispatch_kind=dispatch_kind,
                )
                if hops:
                    record_connectivity_caps(entry_req, req, provider_config)
                return response
            except (LLMHTTPError, LLMResponseError) as exc:
                if hops >= MAX_DEGRADE_HOPS:
                    raise
                degraded = degrade_request_after_failure(req, exc, provider_config)
                if degraded is None:
                    raise
                previous_req = req
                req, reason = degraded
                hops += 1
                dispatch_kind = (
                    "missing_text_degrade"
                    if isinstance(exc, LLMResponseError) and exc.code == "LLM_RESPONSE_MISSING_TEXT"
                    else (
                        "api_mode_degrade"
                        if previous_req.api_mode != req.api_mode
                        else "structured_output_degrade"
                    )
                )
                logger.warning(
                    "llm connectivity degrade [%d/%d] node=%s provider=%s status=%s: %s",
                    hops, MAX_DEGRADE_HOPS, req.node_id, provider_config.provider_id,
                    getattr(exc, "status_code", None), reason,
                )

    def _generate_once(
        self,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
        *,
        accounting_hook: LLMAttemptHook | None,
        initial_dispatch_kind: LLMDispatchKind,
    ) -> LLMResponse:
        """同一份请求的重试循环：每次物理尝试先过记账钩子，再发 POST、分类响应。

        可重试的失败（超时 / 连接错误 / 429 / 可重试状态码 / 可重试的解析错误 / 正文带乱码）在还有额度时
        退避后重发；其余失败与额度用尽时原样抛出，交给 ``generate`` 的降级阶梯或调用方。
        """
        endpoint, payload, headers, native_reasoning = self._build_http_request(request, provider_config)
        timeout_seconds = request.timeout_seconds or self._timeout_seconds
        timeout_message = describe_timeout_failure(timeout_seconds)

        # 调用方给了 transport(测试的 MockTransport)就用它;没给才自己造——
        # 造的时候一定带上 keepalive,否则 read 不限时下半开连接会永久挂住 worker。
        transport = self._transport or httpx.HTTPTransport(socket_options=_keepalive_socket_options())
        with httpx.Client(
            base_url=provider_config.base_url.rstrip("/"),
            timeout=resolve_request_timeout(timeout_seconds),
            transport=transport,
        ) as client:
            dispatch_kind = initial_dispatch_kind
            for attempt in range(self._max_retries + 1):
                hook_handle: object | None = None
                if accounting_hook is not None:
                    hook_handle = accounting_hook.before_dispatch(
                        request=request,
                        dispatch_kind=dispatch_kind,
                    )
                context = _AttemptContext(
                    request=request,
                    provider_config=provider_config,
                    endpoint=endpoint,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    hook=accounting_hook,
                    hook_handle=hook_handle,
                    started_at=time.perf_counter(),
                )
                outcome = self._dispatch_attempt(
                    client,
                    payload=payload,
                    headers=headers,
                    context=context,
                    timeout_message=timeout_message,
                )
                if isinstance(outcome, httpx.Response):
                    outcome = self._classify_http_response(
                        outcome,
                        context=context,
                        native_reasoning=native_reasoning,
                    )
                if isinstance(outcome, LLMResponse):
                    if accounting_hook is not None:
                        accounting_hook.after_response(
                            hook_handle,
                            request=request,
                            response=outcome,
                            latency_ms=_elapsed_ms(context.started_at),
                        )
                    return outcome
                if outcome.retry and attempt < self._max_retries:
                    self._sleep_before_retry(attempt, response=outcome.backoff_response)
                    dispatch_kind = outcome.dispatch_kind
                    continue
                if outcome.cause is not None:
                    raise outcome.error from outcome.cause
                raise outcome.error

        raise LLMHTTPError(
            "LLM_HTTP_FAILURE",
            "llm request failed without a response",
            retryable=True,
            details=_with_attempt_metadata({}, attempt=self._max_retries, max_retries=self._max_retries),
        )

    def _dispatch_attempt(
        self,
        client: httpx.Client,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        context: _AttemptContext,
        timeout_message: str,
    ) -> httpx.Response | _AttemptFailure:
        """发一次 POST；传输层失败翻译成带去向的 ``_AttemptFailure``（超时与连接错误可重试）。"""
        try:
            return client.post(context.endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            error: LLMClientError = LLMTimeoutError(
                "LLM_REQUEST_TIMEOUT",
                timeout_message,
                retryable=True,
                details=context.with_attempt_metadata({}),
            )
            context.notify(error)
            return _AttemptFailure(error, retry=True, cause=exc)
        except httpx.RequestError as exc:
            error = LLMHTTPError(
                "LLM_HTTP_REQUEST_FAILED",
                f"llm request failed: {exc}",
                retryable=True,
                details=context.with_attempt_metadata({}),
            )
            context.notify(error)
            return _AttemptFailure(error, retry=True, cause=exc)
        except Exception as exc:
            error = LLMHTTPError(
                "LLM_HTTP_CLIENT_EXCEPTION",
                "llm HTTP client raised an unexpected exception",
                details=context.with_attempt_metadata(
                    {
                        "original_error_type": exc.__class__.__name__,
                        "original_error_message": str(exc),
                    }
                ),
            )
            context.notify(error)
            return _AttemptFailure(error, cause=exc)

    def _classify_http_response(
        self,
        response: httpx.Response,
        *,
        context: _AttemptContext,
        native_reasoning: dict[str, Any] | None,
    ) -> LLMResponse | _AttemptFailure:
        """一次 HTTP 响应 → 可交付的 ``LLMResponse``，或带去向的 ``_AttemptFailure``。"""
        request = context.request
        if response.status_code == 429:
            error: LLMClientError = LLMRateLimitError(
                "LLM_RATE_LIMITED",
                context.status_message(response),
                status_code=429,
                retryable=True,
                details=context.http_error_details(response),
            )
            context.notify(error, response=response)
            return _AttemptFailure(error, retry=True, backoff_response=response)

        if response.status_code in RETRYABLE_STATUS_CODES:
            # 引擎级结构化输出错误(guided_grammar 等)重试不会自愈——立刻
            # 抛给 generate 的降级阶梯,不浪费重试预算三连击同一错误
            if structured_output_rejected(response.text):
                error = LLMHTTPError(
                    "LLM_HTTP_STRUCTURED_OUTPUT_REJECTED",
                    context.status_message(response),
                    status_code=response.status_code,
                    details=context.http_error_details(response),
                )
                context.notify(error, response=response)
                return _AttemptFailure(error)
            error = LLMHTTPError(
                "LLM_HTTP_RETRYABLE_FAILURE",
                context.status_message(response),
                status_code=response.status_code,
                retryable=True,
                details=context.http_error_details(response),
            )
            context.notify(error, response=response)
            return _AttemptFailure(error, retry=True, backoff_response=response)

        if response.is_error:
            error = LLMHTTPError(
                "LLM_HTTP_FAILURE",
                context.status_message(response),
                status_code=response.status_code,
                details=context.http_error_details(response),
            )
            context.notify(error, response=response)
            return _AttemptFailure(error)

        try:
            body = response.json()
        except ValueError as exc:
            error = LLMResponseError(
                "LLM_RESPONSE_INVALID",
                "llm provider returned invalid JSON",
                details=context.with_attempt_metadata({}),
            )
            context.notify(error, response=response)
            return _AttemptFailure(error, cause=exc)
        if not isinstance(body, dict):
            error = LLMResponseError(
                "LLM_RESPONSE_INVALID",
                "llm provider returned a non-object JSON response",
                details=context.with_attempt_metadata({}),
            )
            context.notify(error)
            return _AttemptFailure(error)

        try:
            parsed_response = self._parse_response(
                body,
                request,
                context.provider_config,
                native_reasoning=native_reasoning,
                attempt_count=context.attempt + 1,
                max_retries=context.max_retries,
            )
        except LLMResponseError as exc:
            exc.details = context.with_attempt_metadata(exc.details)
            exc.retryable = exc.code in RETRYABLE_RESPONSE_ERROR_CODES
            context.notify(exc, raw_response=body, provider_request_id=_extract_request_id(body))
            # 空正文/截断不是瞬时故障:同 prompt 同预算重发,大概率同样为空、
            # 同样在原地被砍断,每次白等一整个生成时长——直接交给降级阶梯
            # (关 reasoning / 扩输出预算),别浪费重试次数。
            return _AttemptFailure(
                exc,
                retry=exc.retryable and exc.code not in BUDGET_DEGRADE_ERROR_CODES,
                dispatch_kind="response_parse_retry",
            )
        except Exception as exc:
            error = LLMResponseError(
                "LLM_RESPONSE_INVALID",
                "llm provider response could not be parsed",
                details=context.with_attempt_metadata({"parser_error_type": exc.__class__.__name__}),
            )
            context.notify(error, raw_response=body, provider_request_id=_extract_request_id(body))
            return _AttemptFailure(error, cause=exc)

        corrupted = _corrupted_output_characters(parsed_response, request)
        if corrupted and context.attempt < context.max_retries:
            error = LLMResponseError(
                CORRUPTED_OUTPUT_ERROR_CODE,
                "llm output carried U+FFFD replacement characters the prompt did not contain "
                "(bytes were lost between the model and this client); re-sending the request",
                retryable=True,
                details=context.with_attempt_metadata({"replacement_characters": corrupted}),
            )
            context.notify(error, raw_response=body, provider_request_id=_extract_request_id(body))
            logger.warning(
                "llm output corrupted (%d U+FFFD) node=%s provider=%s model=%s; retry %d/%d",
                corrupted, request.node_id, context.provider_config.provider_id, request.model,
                context.attempt + 1, context.max_retries,
            )
            return _AttemptFailure(error, retry=True, dispatch_kind="response_parse_retry")
        if corrupted:
            logger.warning(
                "llm output still corrupted (%d U+FFFD) after %d attempts node=%s provider=%s model=%s; "
                "handing it to the caller's integrity gate",
                corrupted, context.attempt + 1, request.node_id, context.provider_config.provider_id, request.model,
            )
        return parsed_response

    def _resolve_provider_config(self, request: LLMRequest) -> ProviderRuntimeConfig:
        provider_id = request.provider_id or request.provider
        if provider_id and provider_id in self._provider_configs:
            config = self._provider_configs[provider_id]
            if not config.enabled:
                raise LLMConfigurationError(
                    "LLM_PROVIDER_DISABLED",
                    f"llm provider {provider_id} is disabled",
                )
            return config

        provider_type = request.provider or self._provider
        return ProviderRuntimeConfig(
            provider_id=provider_id or provider_type,
            provider_type=provider_type,
            base_url=self._base_url,
            api_key=self._api_key,
            credential_mode=request.credential_mode or ("api_key" if self._api_key else "none"),
            api_mode=request.api_mode,
        )

    def _build_http_request(
        self,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
    ) -> tuple[str, dict[str, Any], dict[str, str], dict[str, Any] | None]:
        adapter = get_adapter(provider_config.provider_type)
        built = adapter.build_request(request, provider_config)
        return built.endpoint, built.payload, built.headers, built.native_reasoning

    def _parse_response(
        self,
        body: dict[str, Any],
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
        *,
        native_reasoning: dict[str, Any] | None,
        attempt_count: int,
        max_retries: int,
    ) -> LLMResponse:
        adapter = get_adapter(provider_config.provider_type)
        raw_usage = extract_raw_usage(body)
        text = adapter.extract_output_text(body, request=request)
        finish_reason = adapter.extract_finish_reason(body, api_mode=request.api_mode)
        structured_output: dict[str, Any] | None = None
        if request.response_format == "json_object":
            # 先解析、再判截断——顺序很关键:
            # 1) 一份正好在 max_tokens 处收尾的**完整** JSON(finish_reason=length)仍然是
            #    可用结果,不能因为 finish_reason 就丢弃(否则顶到上限时会把好答案误杀)。
            # 2) _loads_json_object_text 只救援**顶层**配平对象,被砍断的顶层对象括号配不平
            #    → 解析必然失败,绝不会像旧救援那样捞出内层第一个场景对象当整份结果
            #    (真实故障:场景规划每次只推进一场、草稿半新半旧)。
            # 3) 只有解析失败时才区分:provider 报了截断(chat 的 finish_reason=length /
            #    Responses 的 status=incomplete)→ 可抬预算重试的 TRUNCATED;否则是真坏 JSON。
            try:
                structured_output = _loads_json_object_text(text)
            except json.JSONDecodeError as exc:
                # 2026-09-22:中转(Responses 模式)常不报 finish_reason;输出 token 数顶到
                # max_output_tokens 同样说明被砍断——按 TRUNCATED 抬预算重试,而不是原样重发
                # 三次同一个 2600 上限(真实运行:soft_qc / 验收评审各浪费 18 万 token 后作废)。
                output_tokens = usage_output_tokens(raw_usage)
                hit_ceiling = (
                    output_tokens is not None
                    and request.max_output_tokens > 0
                    and output_tokens >= request.max_output_tokens
                )
                if str(finish_reason or "").strip().lower() in TRUNCATED_FINISH_REASONS or hit_ceiling:
                    raise LLMResponseError(
                        "LLM_RESPONSE_TRUNCATED",
                        "llm output hit the max output token ceiling before the JSON was complete "
                        f"(finish_reason={finish_reason}, output_tokens={output_tokens}, "
                        f"max_output_tokens={request.max_output_tokens})",
                    ) from exc
                raise LLMResponseError(
                    "LLM_RESPONSE_INVALID_JSON",
                    "llm provider returned malformed JSON content",
                ) from exc
            if not isinstance(structured_output, dict):
                raise LLMResponseError(
                    "LLM_RESPONSE_INVALID_JSON_OBJECT",
                    "llm provider returned a non-object JSON payload for json_object mode",
                )

        try:
            normalized_usage = adapter.normalize_usage(body)
        except (TypeError, ValueError, OverflowError):
            normalized_usage = None

        return LLMResponse(
            request_id=_extract_request_id(body),
            provider=provider_config.provider_type,
            model=str(body.get("model", request.model)),
            text=text,
            structured_output=structured_output,
            response_format=request.response_format,
            raw_response=body,
            usage=normalized_usage or wire_usage(raw_usage),
            finish_reason=finish_reason,
            native_reasoning=native_reasoning,
            attempt_count=attempt_count,
            max_retries=max_retries,
            retryable=False,
            raw_usage=raw_usage,
            usage_present=raw_usage is not None,
            usage_complete=raw_usage_is_complete(raw_usage),
        )

    def _validate_provider(self, provider: str) -> None:
        if provider not in SUPPORTED_PROVIDERS:
            raise LLMConfigurationError(
                "LLM_PROVIDER_UNSUPPORTED",
                f"unsupported llm provider {provider}",
            )

    def _validate_request(self, request: LLMRequest) -> None:
        if request.response_format not in SUPPORTED_RESPONSE_FORMATS:
            raise LLMConfigurationError(
                "LLM_REQUEST_INVALID",
                f"unsupported response_format {request.response_format}",
            )
        if request.api_mode not in SUPPORTED_API_MODES:
            raise LLMConfigurationError(
                "LLM_REQUEST_INVALID",
                f"unsupported api_mode {request.api_mode}",
            )
        if request.reasoning_level not in SUPPORTED_REASONING_LEVELS:
            raise LLMConfigurationError(
                "LLM_REQUEST_INVALID",
                f"unsupported reasoning_level {request.reasoning_level}",
            )


def _loads_json_object_text(text: str) -> Any:
    """从正文里取出 JSON 对象;正文带 markdown 围栏或前后缀说明时做救援解析。

    救援**只认顶层配平的对象**——嵌在一个尚未闭合的外层对象里的片段一概不认。
    这条限制是有意的:正文被 max_tokens 砍断时,最外层大括号永远配不平,但内层成员
    (比如场景数组里的第一个场景)自身是配平的;救援若捞出它当整份结果,接口报成功、
    内容却是错的(真实故障:场景规划每次只推进一场、草稿半新半旧)。静默的错答比报错
    糟得多,所以配不平就抛错,交给上层按 TRUNCATED / INVALID_JSON 重试。

    「顶层」是结构判据,不是位置判据。曾经用「只看第一个 `{`」来近似它,但那会误伤
    另一类完全正常的回复:``按 {step_key} 的要求,输出如下:\\n{"scenes": [...]}``——
    第一个配平片段是提示词占位符,不是 JSON,于是整份可用的回复被判成坏 JSON,还要
    白烧一轮重试预算。按深度判定则两种情况都对:占位符和真正的载荷都是顶层候选,
    依次尝试;而被砍断的正文里内层成员的深度≥1,永远进不了候选。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as original_exc:
        for candidate in _iter_json_object_candidates(text):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        raise original_exc


def _iter_json_object_candidates(text: str) -> list[str]:
    """正文里所有**顶层**配平的 `{...}` 片段,按出现顺序。

    单趟扫描,全程跟踪字符串与转义(免得正文里的 `{` / `"` 把深度算歪)。深度回到 0
    才收一个候选;外层始终没闭合(截断)时一个候选都不产出——这正是要的行为。
    """
    candidates: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    start = -1
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : index + 1])
                    start = -1
    return candidates


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _notify_attempt_error(
    hook: LLMAttemptHook | None,
    handle: object | None,
    *,
    request: LLMRequest,
    error: BaseException,
    started_at: float,
    response: httpx.Response | None = None,
    raw_response: dict[str, Any] | None = None,
    provider_request_id: str | None = None,
) -> None:
    if hook is None:
        return
    if raw_response is None and response is not None:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            raw_response = parsed
    if provider_request_id is None and raw_response is not None:
        provider_request_id = _extract_request_id(raw_response)
    hook.after_error(
        handle,
        request=request,
        error=error,
        raw_response=raw_response,
        provider_request_id=provider_request_id,
        latency_ms=_elapsed_ms(started_at),
    )


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None  # HTTP-date 形式的 Retry-After 不解析,走指数退避
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_BACKOFF_SECONDS)


def _extract_request_id(body: dict[str, Any]) -> str | None:
    request_id = body.get("id")
    if isinstance(request_id, str) and request_id:
        return request_id
    return None


def _extract_error_details(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        if response.text:
            return {"message": response.text}
        return {}

    if not isinstance(body, dict):
        return {"body": body}

    error = body.get("error")
    if isinstance(error, dict):
        return error
    return body


def _with_attempt_metadata(details: dict[str, Any], *, attempt: int, max_retries: int) -> dict[str, Any]:
    return {
        **dict(details),
        "attempt_count": attempt + 1,
        "max_retries": max_retries,
    }


def _http_error_details(
    response: httpx.Response,
    *,
    request: LLMRequest,
    provider_config: ProviderRuntimeConfig,
    endpoint: str,
    attempt: int,
    max_retries: int,
) -> dict[str, Any]:
    details = _with_attempt_metadata(
        _extract_error_details(response),
        attempt=attempt,
        max_retries=max_retries,
    )
    hint = _provider_protocol_hint(response, request=request, provider_config=provider_config, endpoint=endpoint)
    if hint:
        details.update(
            {
                "status_code": response.status_code,
                "provider_id": provider_config.provider_id,
                "provider_type": provider_config.provider_type,
                "model": request.model,
                "node_id": request.node_id,
                "api_mode": request.api_mode,
                "endpoint": endpoint,
            }
        )
        details.setdefault("hint", hint["message"])
        details.setdefault("next_action", hint["next_action"])
    return details


def _error_message_for_status(
    response: httpx.Response,
    *,
    request: LLMRequest | None = None,
    provider_config: ProviderRuntimeConfig | None = None,
    endpoint: str | None = None,
) -> str:
    details = _extract_error_details(response)
    detail_message = details.get("message")
    if isinstance(detail_message, str) and detail_message:
        message = detail_message
    else:
        message = f"llm request failed with status {response.status_code}"
    if request is not None and provider_config is not None and endpoint is not None:
        hint = _provider_protocol_hint(response, request=request, provider_config=provider_config, endpoint=endpoint)
        if hint:
            return f"{message}; {hint['message']}"
    return message


def _provider_protocol_hint(
    response: httpx.Response,
    *,
    request: LLMRequest,
    provider_config: ProviderRuntimeConfig,
    endpoint: str,
) -> dict[str, str] | None:
    try:
        adapter = get_adapter(provider_config.provider_type)
    except LLMConfigurationError:
        return None
    return adapter.protocol_hint(
        status_code=response.status_code,
        endpoint=endpoint,
        request=request,
        provider_config=provider_config,
    )
