from __future__ import annotations

import json
import logging
import random
import socket
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from novel_system.accounting_contract import DEFAULT_PROVIDER_ATTEMPT_BUDGET
from novel_system.services.llm_providers import (
    default_provider_base_urls,
    get_adapter,
    supported_provider_types,
)
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
    OfflineDeterministicExecution,
    ProviderRuntimeConfig,
    SUPPORTED_API_MODES,
    SUPPORTED_CREDENTIAL_MODES,
    SUPPORTED_REASONING_LEVELS,
    SUPPORTED_RESPONSE_FORMATS,
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
    "OnlineAccountedExecution",
    "OfflineDeterministicExecution",
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
    "build_llm_request",
    "load_model_routing_config",
    "parse_model_routing_config",
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
SUPPORTED_PROVIDERS = frozenset(supported_provider_types())
MAX_RETRY_BACKOFF_SECONDS = 30.0

# 建连/写入/连接池握手的超时上限。生成本身可以慢(大模型长任务是正常的),
# 但"连不上"必须快速失败——否则填错 base_url 只会让界面无限转圈。
# timeout_seconds <= 0 时只保留这一层,读取(等待模型出字)不设上限。
LLM_CONNECT_TIMEOUT_SECONDS = 30.0

# LLM 连通性降级阶梯:一次 generate 最多降级次数(api_mode 404 换 chat →
# 弃 json_schema → 弃 wire response_format),防御环路。
MAX_DEGRADE_HOPS = 3

# provider/后端引擎拒绝结构化输出约束的错误特征(中转常把推理引擎错误原样透传):
# lightllm/vllm 的 guided_grammar / guided_json、OpenAI 的 response_format 参数错、
# 各家 "does not support structured output" 变体。命中即走降级而非盲目重试。
_STRUCTURED_OUTPUT_ERROR_SIGNATURES = (
    "guided_grammar",
    "compile_grammar_error",
    "guided_json",
    "guided_decoding",
    "json_schema",
    "structured output",
    "structured_output",
    "structural_tag",
    "text.format",
    "response_format",
    "grammar",
)


def _structured_output_rejection_signature(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(sig in lowered for sig in _STRUCTURED_OUTPUT_ERROR_SIGNATURES)


def _thinking_with_forced_tool_rejection(text: str | None) -> bool:
    """Anthropic 400:「Thinking may not be enabled when tool_choice forces tool use」(中转实现 json_schema 的副作用)。"""
    lowered = (text or "").lower()
    return "thinking may not be enabled" in lowered or (
        "thinking" in lowered and "tool_choice" in lowered and "forces" in lowered
    )


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


MAX_OUTPUT_TOKENS_CEILING = 8192


def _inline_schema_into_messages(request: LLMRequest) -> LLMRequest:
    """把 response_schema 从 wire 层降级掉时,将 schema 内联进 system prompt。

    json_schema wire 模式下模型从 API 侧拿到输出形状;裸 json_object 模式模型
    看不到 schema,会自造字段名(实测把 statement 写成 description),下游
    Pydantic 校验全灭。内联后模型仍知道确切形状。
    """
    if not request.response_schema:
        return replace(request, response_schema=None)
    schema = request.response_schema
    schema_body = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
    hint = (
        "\n\n输出必须是**单个 JSON 对象**,且严格符合以下 JSON Schema"
        "(字段名一字不差,不要输出 Schema 之外的字段或任何非 JSON 文本):\n"
        + json.dumps(schema_body, ensure_ascii=False)
    )
    messages = [dict(m) for m in request.messages]
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = str(messages[0].get("content") or "") + hint
    else:
        messages.insert(0, {"role": "system", "content": hint.strip()})
    return replace(request, response_schema=None, messages=messages)


def _thinking_disabled(request: LLMRequest) -> bool:
    extra = (request.provider_options or {}).get("extra_payload") or {}
    return extra.get("enable_thinking") is False


def _with_thinking_disabled(request: LLMRequest) -> LLMRequest:
    """qwen/vllm/lightllm 系模型默认开思考且不认 OpenAI 的 reasoning 参数——
    发它们各自认识的关思考开关(未知键会被这类引擎忽略,不影响其它中转)。"""
    opts = dict(request.provider_options or {})
    extra = dict(opts.get("extra_payload") or {})
    extra.setdefault("chat_template_kwargs", {"enable_thinking": False})
    extra.setdefault("enable_thinking", False)
    opts["extra_payload"] = extra
    return replace(request, provider_options=opts)


def _degrade_request_after_failure(
    request: LLMRequest,
    exc: LLMClientError,
    provider_config: ProviderRuntimeConfig,
) -> tuple[LLMRequest, str] | None:
    """连通性失败后的降级决策;不可降级返 None(由调用方原样抛出)。

    - /responses 404:中转仅支持 Chat Completions → api_mode 换 chat 重试
    - 结构化输出被拒:先弃 json_schema(退 json_object 并内联 schema),再弃
      wire 层 response_format(prompt 本身要求 JSON,客户端解析不变)
    - 200 但正文为空(LLM_RESPONSE_MISSING_TEXT):典型是 reasoning 模型把
      max_tokens 烧在思考上——关 reasoning + 关引擎思考开关 + 输出预算×2
    - 输出被 max_tokens 砍断(LLM_RESPONSE_TRUNCATED):同样抬输出预算重试;
      已经顶到上限还截断就返 None,让上层如实报错——半截结构化输出没有可用形态。
    限流(429)不属于能力不匹配,不降级。
    """
    if isinstance(exc, LLMRateLimitError):
        return None
    if isinstance(exc, LLMResponseError):
        if exc.code == "LLM_RESPONSE_TRUNCATED":
            if request.max_output_tokens >= MAX_OUTPUT_TOKENS_CEILING:
                return None
            raised = min(request.max_output_tokens * 2, MAX_OUTPUT_TOKENS_CEILING)
            return (
                replace(request, max_output_tokens=raised),
                f"输出截断降级:输出预算 {request.max_output_tokens}→{raised}",
            )
        if exc.code != "LLM_RESPONSE_MISSING_TEXT":
            return None
        req = request
        reasons = []
        if request.reasoning_level != "off":
            req = replace(req, reasoning_level="off")
            reasons.append("关闭 reasoning 参数")
        # 严格官方 API(openai 等)不发未知参数;兼容中转/本地引擎发思考开关
        if provider_config.provider_type == "openai_compatible" and not _thinking_disabled(req):
            req = _with_thinking_disabled(req)
            reasons.append("发送 enable_thinking=false 思考开关")
        if request.max_output_tokens < MAX_OUTPUT_TOKENS_CEILING:
            req = replace(
                req,
                max_output_tokens=min(request.max_output_tokens * 2, MAX_OUTPUT_TOKENS_CEILING),
            )
            reasons.append(f"输出预算 {request.max_output_tokens}→{req.max_output_tokens}")
        if req is request:
            return None
        return req, "空正文降级(疑似思考吃满 max_tokens):" + "、".join(reasons)
    if not isinstance(exc, LLMHTTPError):
        return None
    status = getattr(exc, "status_code", None)
    if status == 404 and request.api_mode == "responses":
        return (
            replace(request, api_mode="chat"),
            "api_mode responses→chat(/responses 404,中转仅支持 chat completions)",
        )
    detail_text = f"{getattr(exc, 'message', '')} {json.dumps(getattr(exc, 'details', None) or {}, ensure_ascii=False, default=str)}"
    # 2026-09-22:Anthropic 系模型经中转走 json_schema 时,中转把结构化输出实现成强制工具调用,
    # 而 Anthropic 不允许「思考」与强制工具并存(400 "Thinking may not be enabled when tool_choice
    # forces tool use")。关掉 reasoning 重试一跳;成功后连通性缓存记住 reasoning off,后续不再浪费。
    if _thinking_with_forced_tool_rejection(detail_text) and request.reasoning_level != "off":
        return (
            replace(request, reasoning_level="off"),
            "reasoning 降级:中转把 json_schema 实现成强制工具调用,Anthropic 不允许同时开启思考",
        )
    if _structured_output_rejection_signature(detail_text):
        if request.response_schema is not None:
            return (
                _inline_schema_into_messages(request),
                "结构化输出降级:弃 wire json_schema,退 json_object 并把 schema 内联进 prompt",
            )
        if request.response_format == "json_object" and request.wire_response_format:
            return (
                replace(request, wire_response_format=False),
                "结构化输出降级:不再发送 response_format,仅靠 prompt 约束 JSON",
            )
    return None


# 连通性能力缓存(进程内):记录某 (provider_id, model) 已实证的能力上限,
# 后续调用直接按学到的档位发请求,不再每次浪费一跳注定失败的探测
# (重分类一本书要打数百批次,不缓存 = 数百个多余 400)。
#   api_mode: 404 降级学到的端点模式
#   structured_tier: 2=json_schema / 1=json_object / 0=不发 response_format
_CONNECTIVITY_CAPS: dict[tuple[str, str], dict[str, Any]] = {}


def _request_structured_tier(request: LLMRequest) -> int:
    if request.response_format != "json_object":
        return 0
    if not request.wire_response_format:
        return 0
    return 2 if request.response_schema is not None else 1


def _apply_connectivity_caps(request: LLMRequest, provider_config: ProviderRuntimeConfig) -> LLMRequest:
    caps = _CONNECTIVITY_CAPS.get((provider_config.provider_id, request.model))
    if not caps:
        return request
    req = request
    cached_mode = caps.get("api_mode")
    if cached_mode in ("chat", "responses") and req.api_mode != cached_mode:
        req = replace(req, api_mode=cached_mode)
    tier = caps.get("structured_tier")
    if isinstance(tier, int) and tier < _request_structured_tier(req):
        if tier <= 1 and req.response_schema is not None:
            req = _inline_schema_into_messages(req)
        if tier <= 0 and req.wire_response_format:
            req = replace(req, wire_response_format=False)
    if caps.get("reasoning_level") == "off" and req.reasoning_level != "off":
        req = replace(req, reasoning_level="off")
    if caps.get("disable_thinking") and not _thinking_disabled(req):
        req = _with_thinking_disabled(req)
    return req


def _record_connectivity_caps(
    original: LLMRequest, final: LLMRequest, provider_config: ProviderRuntimeConfig
) -> None:
    """降级后成功才记录(hops>0 时调用):只降不升,进程重启即重置。"""
    key = (provider_config.provider_id, final.model)
    caps = _CONNECTIVITY_CAPS.setdefault(key, {})
    if final.api_mode != original.api_mode:
        caps["api_mode"] = final.api_mode
    final_tier = _request_structured_tier(final)
    if final_tier < _request_structured_tier(original):
        prev = caps.get("structured_tier")
        caps["structured_tier"] = min(prev, final_tier) if isinstance(prev, int) else final_tier
    # 空正文降级学到的 reasoning 关闭同样缓存(输出预算按节点各异,不缓存)
    if final.reasoning_level == "off" and original.reasoning_level != "off":
        caps["reasoning_level"] = "off"
    if _thinking_disabled(final) and not _thinking_disabled(original):
        caps["disable_thinking"] = True


def _normalize_api_mode(request: LLMRequest, provider_config: ProviderRuntimeConfig) -> LLMRequest:
    """按 provider 声明的端点能力归一化 api_mode(chat-only 中转收到 /responses 必 404)。

    env 回退路径的 provider_config 由 request 反构(api_mode=request.api_mode),
    此归一化自动 no-op;运行时 provider 配置携带显式声明时以之为准。
    build 与响应解析(extract_output_text)共用 request.api_mode,必须在入口统一。
    """
    declared = getattr(provider_config, "api_mode", None)
    if declared in ("chat", "responses") and request.api_mode != declared:
        return replace(request, api_mode=declared)
    return request

DEFAULT_PROVIDER_BASE_URLS = default_provider_base_urls()


@dataclass(slots=True, frozen=True)
class TaskModelConfig:
    provider: str
    model: str
    temperature: float
    max_output_tokens: int
    response_format: str
    model_profile: str | None = None
    provider_id: str | None = None
    account_id: str | None = None
    reasoning_level: Literal["off", "low", "medium", "high"] = "medium"
    api_mode: Literal["responses", "chat"] = "responses"
    credential_mode: Literal["api_key", "none"] | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    # §7 anti-mean sampling — optional decoding-level penalties (OpenAI-compatible APIs)
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    top_p: float | None = None
    # 按节点覆盖 LLM 调用超时(秒);None 用 client 全局默认(默认不限时)。
    # 只有明确想给某个节点封顶时才填正数——填了就是给该节点重新装上闸门。
    timeout_seconds: float | None = None


@dataclass(slots=True, frozen=True)
class ModelRoutingConfig:
    node_routing: dict[str, TaskModelConfig]
    task_routing: dict[str, TaskModelConfig]
    model_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    retry_budget: dict[str, int] = field(default_factory=dict)
    job_runtime: dict[str, Any] = field(default_factory=dict)


def resolve_node_route(routing: Any, node_id: str) -> Any:
    """解析一个 LLM 节点的路由配置:DB node_routing 优先,yaml task_routing 兜底。

    优先级教训(不要改动顺序):``parse_model_routing_config`` 的合并是
    setdefault——yaml task 条目赢。历史上风格参考的单次节点调用只读
    task_routing,导致用户在系统设置「模型与接入」角色槽配好的
    provider/model/api_mode 被 yaml 占位(gpt-5/responses)遮蔽,风格抽取对
    chat-only 中转直接 404(真实回归;那个调用层已随旧学习链路删除,现在风格参考的
    分类 / 学习 / 对照检查节点都直接调这里)。
    因此所有调用点必须先查 node_routing(系统设置同步的 DB 节点路由),
    再退回 task_routing(config/models.yaml 的 task 默认);两处皆缺 →
    ``KeyError(node_id)``,由调用方翻译成各自的引导错误。
    """
    node_routing = getattr(routing, "node_routing", None)
    if isinstance(node_routing, dict) and node_id in node_routing:
        return node_routing[node_id]
    task_routing = getattr(routing, "task_routing", {})
    if node_id in task_routing:
        return task_routing[node_id]
    raise KeyError(node_id)


def build_llm_request(
    task_config: Any,
    *,
    node_id: str,
    messages: list[dict[str, str]],
    response_schema: dict[str, Any] | None = None,
    temperature_override: float | None = None,
) -> LLMRequest:
    """把一条节点路由(TaskModelConfig 或运行时等价物)翻译成 ``LLMRequest``。

    单一出口的意义:历史上 5 个调用点各抄一份构造块并逐渐漂移——per-node
    ``timeout_seconds`` 与 §7 采样字段(``frequency_penalty`` /
    ``presence_penalty`` / ``top_p``)只有 llm_task_runner 传了,其余路径把
    作者在节点路由里配置的值静默丢弃。所有可选字段一律 ``getattr(..., None)``
    读取:路由未配置 → ``None`` → client / provider 使用全局默认
    (timeout 默认不限时;只有路由显式配置正数时才给该节点重新封顶)。
    """
    return LLMRequest(
        model=task_config.model,
        messages=messages,
        temperature=(
            temperature_override
            if temperature_override is not None
            else task_config.temperature
        ),
        max_output_tokens=task_config.max_output_tokens,
        response_format=task_config.response_format,
        provider=task_config.provider,
        timeout_seconds=getattr(task_config, "timeout_seconds", None),
        node_id=node_id,
        provider_id=getattr(task_config, "provider_id", None),
        account_id=getattr(task_config, "account_id", None),
        reasoning_level=getattr(task_config, "reasoning_level", "medium"),
        response_schema=response_schema,
        api_mode=getattr(task_config, "api_mode", "responses"),
        credential_mode=getattr(task_config, "credential_mode", None),
        provider_options=getattr(task_config, "provider_options", {}),
        # §7 anti-mean sampling — decoding-level penalties from the node route
        frequency_penalty=getattr(task_config, "frequency_penalty", None),
        presence_penalty=getattr(task_config, "presence_penalty", None),
        top_p=getattr(task_config, "top_p", None),
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
        req = _normalize_api_mode(request, provider_config)
        if req is not request:
            logger.info(
                "llm api_mode normalized to provider declaration: node=%s provider=%s %s -> %s",
                request.node_id, provider_config.provider_id, request.api_mode, req.api_mode,
            )
        req = _apply_connectivity_caps(req, provider_config)
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
                    _record_connectivity_caps(entry_req, req, provider_config)
                return response
            except (LLMHTTPError, LLMResponseError) as exc:
                if hops >= MAX_DEGRADE_HOPS:
                    raise
                degraded = _degrade_request_after_failure(req, exc, provider_config)
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
                started_at = time.perf_counter()
                try:
                    response = client.post(endpoint, json=payload, headers=headers)
                except httpx.TimeoutException as exc:
                    error = LLMTimeoutError(
                        "LLM_REQUEST_TIMEOUT",
                        timeout_message,
                        retryable=True,
                        details=_with_attempt_metadata({}, attempt=attempt, max_retries=self._max_retries),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        started_at=started_at,
                    )
                    if attempt < self._max_retries:
                        self._sleep_before_retry(attempt)
                        dispatch_kind = "transport_retry"
                        continue
                    raise error from exc
                except httpx.RequestError as exc:
                    error = LLMHTTPError(
                        "LLM_HTTP_REQUEST_FAILED",
                        f"llm request failed: {exc}",
                        retryable=True,
                        details=_with_attempt_metadata({}, attempt=attempt, max_retries=self._max_retries),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        started_at=started_at,
                    )
                    if attempt < self._max_retries:
                        self._sleep_before_retry(attempt)
                        dispatch_kind = "transport_retry"
                        continue
                    raise error from exc
                except Exception as exc:
                    error = LLMHTTPError(
                        "LLM_HTTP_CLIENT_EXCEPTION",
                        "llm HTTP client raised an unexpected exception",
                        details=_with_attempt_metadata(
                            {
                                "original_error_type": exc.__class__.__name__,
                                "original_error_message": str(exc),
                            },
                            attempt=attempt,
                            max_retries=self._max_retries,
                        ),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        started_at=started_at,
                    )
                    raise error from exc

                if response.status_code == 429:
                    error = LLMRateLimitError(
                        "LLM_RATE_LIMITED",
                        _error_message_for_status(response, request=request, provider_config=provider_config, endpoint=endpoint),
                        status_code=429,
                        retryable=True,
                        details=_http_error_details(
                            response,
                            request=request,
                            provider_config=provider_config,
                            endpoint=endpoint,
                            attempt=attempt,
                            max_retries=self._max_retries,
                        ),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        response=response,
                        started_at=started_at,
                    )
                    if attempt < self._max_retries:
                        self._sleep_before_retry(attempt, response=response)
                        dispatch_kind = "transport_retry"
                        continue
                    raise error

                if response.status_code in RETRYABLE_STATUS_CODES:
                    # 引擎级结构化输出错误(guided_grammar 等)重试不会自愈——立刻
                    # 抛给 generate 的降级阶梯,不浪费重试预算三连击同一错误
                    if _structured_output_rejection_signature(response.text):
                        error = LLMHTTPError(
                            "LLM_HTTP_STRUCTURED_OUTPUT_REJECTED",
                            _error_message_for_status(response, request=request, provider_config=provider_config, endpoint=endpoint),
                            status_code=response.status_code,
                            details=_http_error_details(
                                response,
                                request=request,
                                provider_config=provider_config,
                                endpoint=endpoint,
                                attempt=attempt,
                                max_retries=self._max_retries,
                            ),
                        )
                        _notify_attempt_error(
                            accounting_hook,
                            hook_handle,
                            request=request,
                            error=error,
                            response=response,
                            started_at=started_at,
                        )
                        raise error
                    error = LLMHTTPError(
                        "LLM_HTTP_RETRYABLE_FAILURE",
                        _error_message_for_status(response, request=request, provider_config=provider_config, endpoint=endpoint),
                        status_code=response.status_code,
                        retryable=True,
                        details=_http_error_details(
                            response,
                            request=request,
                            provider_config=provider_config,
                            endpoint=endpoint,
                            attempt=attempt,
                            max_retries=self._max_retries,
                        ),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        response=response,
                        started_at=started_at,
                    )
                    if attempt < self._max_retries:
                        self._sleep_before_retry(attempt, response=response)
                        dispatch_kind = "transport_retry"
                        continue
                    raise error

                if response.is_error:
                    error = LLMHTTPError(
                        "LLM_HTTP_FAILURE",
                        _error_message_for_status(response, request=request, provider_config=provider_config, endpoint=endpoint),
                        status_code=response.status_code,
                        details=_http_error_details(
                            response,
                            request=request,
                            provider_config=provider_config,
                            endpoint=endpoint,
                            attempt=attempt,
                            max_retries=self._max_retries,
                        ),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        response=response,
                        started_at=started_at,
                    )
                    raise error

                try:
                    body = response.json()
                except ValueError as exc:
                    error = LLMResponseError(
                        "LLM_RESPONSE_INVALID",
                        "llm provider returned invalid JSON",
                        details=_with_attempt_metadata({}, attempt=attempt, max_retries=self._max_retries),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        response=response,
                        started_at=started_at,
                    )
                    raise error from exc
                if not isinstance(body, dict):
                    error = LLMResponseError(
                        "LLM_RESPONSE_INVALID",
                        "llm provider returned a non-object JSON response",
                        details=_with_attempt_metadata({}, attempt=attempt, max_retries=self._max_retries),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        started_at=started_at,
                    )
                    raise error

                try:
                    parsed_response = self._parse_response(
                        body,
                        request,
                        provider_config,
                        native_reasoning=native_reasoning,
                        attempt_count=attempt + 1,
                        max_retries=self._max_retries,
                    )
                except LLMResponseError as exc:
                    exc.details = _with_attempt_metadata(
                        exc.details,
                        attempt=attempt,
                        max_retries=self._max_retries,
                    )
                    exc.retryable = exc.code in RETRYABLE_RESPONSE_ERROR_CODES
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=exc,
                        raw_response=body,
                        provider_request_id=_extract_request_id(body),
                        started_at=started_at,
                    )
                    # 空正文/截断不是瞬时故障:同 prompt 同预算重发,大概率同样为空、
                    # 同样在原地被砍断,每次白等一整个生成时长——直接交给降级阶梯
                    # (关 reasoning / 扩输出预算),别浪费重试次数。
                    if (
                        exc.code in RETRYABLE_RESPONSE_ERROR_CODES
                        and exc.code not in BUDGET_DEGRADE_ERROR_CODES
                        and attempt < self._max_retries
                    ):
                        self._sleep_before_retry(attempt)
                        dispatch_kind = "response_parse_retry"
                        continue
                    raise
                except Exception as exc:
                    error = LLMResponseError(
                        "LLM_RESPONSE_INVALID",
                        "llm provider response could not be parsed",
                        details=_with_attempt_metadata(
                            {"parser_error_type": exc.__class__.__name__},
                            attempt=attempt,
                            max_retries=self._max_retries,
                        ),
                    )
                    _notify_attempt_error(
                        accounting_hook,
                        hook_handle,
                        request=request,
                        error=error,
                        raw_response=body,
                        provider_request_id=_extract_request_id(body),
                        started_at=started_at,
                    )
                    raise error from exc
                if accounting_hook is not None:
                    accounting_hook.after_response(
                        hook_handle,
                        request=request,
                        response=parsed_response,
                        latency_ms=_elapsed_ms(started_at),
                    )
                return parsed_response

        raise LLMHTTPError(
            "LLM_HTTP_FAILURE",
            "llm request failed without a response",
            retryable=True,
            details=_with_attempt_metadata({}, attempt=self._max_retries, max_retries=self._max_retries),
        )

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
        raw_usage = _extract_raw_usage(body)
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
                output_tokens = _usage_output_tokens(raw_usage)
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
            usage=normalized_usage or _normalize_usage(raw_usage),
            finish_reason=finish_reason,
            native_reasoning=native_reasoning,
            attempt_count=attempt_count,
            max_retries=max_retries,
            retryable=False,
            raw_usage=raw_usage,
            usage_present=raw_usage is not None,
            usage_complete=_raw_usage_is_complete(raw_usage),
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


def load_model_routing_config(path: str | Path | None = None) -> ModelRoutingConfig:
    if path is None:
        from novel_system.services.config_snapshot_reader import load_active_config_payload

        raw_payload = load_active_config_payload("models")
        if raw_payload is None:
            config_path = _default_models_config_path()
            raw_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    else:
        config_path = Path(path)
        raw_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return parse_model_routing_config(raw_payload)


def parse_model_routing_config(raw_payload: Any) -> ModelRoutingConfig:
    if raw_payload is None:
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            "models config must decode to a mapping",
        )

    raw_node_routing = _require_mapping(raw_payload, "node_routing")
    raw_task_routing = _require_mapping(raw_payload, "task_routing")
    raw_model_profiles = _require_mapping(raw_payload, "model_profiles")
    retry_budget = dict(_require_mapping(raw_payload, "retry_budget"))
    provider_attempt_budget = retry_budget.get("provider_attempt_budget")
    if (
        not isinstance(provider_attempt_budget, int)
        or isinstance(provider_attempt_budget, bool)
        or provider_attempt_budget <= 0
    ):
        provider_attempt_budget = DEFAULT_PROVIDER_ATTEMPT_BUDGET
    retry_budget["provider_attempt_budget"] = provider_attempt_budget
    job_runtime = _require_mapping(raw_payload, "job_runtime")

    node_routing = {
        node_name: _load_task_model_config(node_name, node_payload)
        for node_name, node_payload in raw_node_routing.items()
    }
    task_routing = {
        task_name: _load_task_model_config(task_name, task_payload)
        for task_name, task_payload in raw_task_routing.items()
    }

    for task_name, task_config in task_routing.items():
        if task_name == "stylize":
            continue
        else:
            node_routing.setdefault(task_name, task_config)

    for node_name, node_config in node_routing.items():
        task_routing.setdefault(node_name, node_config)
    if "style_draft" in node_routing:
        task_routing.setdefault("stylize", node_routing["style_draft"])

    return ModelRoutingConfig(
        node_routing=node_routing,
        task_routing=task_routing,
        model_profiles=_normalize_model_profiles(raw_model_profiles),
        retry_budget=retry_budget,
        job_runtime=dict(job_runtime),
    )


def _default_models_config_path() -> Path:
    return Path(__file__).resolve().parents[4] / "config" / "models.yaml"


def _load_task_model_config(task_name: str, payload: Any) -> TaskModelConfig:
    if not isinstance(payload, dict):
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} must be a mapping",
        )

    try:
        return TaskModelConfig(
            provider=_parse_provider(task_name, payload),
            model_profile=_optional_str(payload.get("model_profile")),
            provider_id=_optional_str(payload.get("provider_id")),
            account_id=_optional_str(payload.get("account_id")),
            model=str(payload["model"]),
            temperature=_parse_float_config_value(task_name, payload, "temperature"),
            max_output_tokens=_parse_int_config_value(task_name, payload, "max_output_tokens"),
            response_format=_parse_response_format(task_name, payload),
            reasoning_level=_parse_reasoning_level(task_name, payload),
            api_mode=_parse_api_mode(task_name, payload),
            credential_mode=_parse_credential_mode(task_name, payload),
            provider_options=_parse_provider_options(task_name, payload),
            frequency_penalty=_optional_sampling_value(task_name, payload, "frequency_penalty"),
            presence_penalty=_optional_sampling_value(task_name, payload, "presence_penalty"),
            top_p=_optional_sampling_value(task_name, payload, "top_p"),
            timeout_seconds=(
                _parse_float_config_value(task_name, payload, "timeout_seconds")
                if payload.get("timeout_seconds") is not None
                else None
            ),
        )
    except KeyError as exc:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} is missing {exc.args[0]}",
        ) from exc


def _require_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"{key} must be a mapping",
        )
    return value


def _normalize_model_profiles(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for profile_key, profile_payload in payload.items():
        if not isinstance(profile_key, str):
            raise LLMConfigurationError(
                "LLM_MODEL_CONFIG_INVALID",
                "model profile names must be strings",
            )
        if not isinstance(profile_payload, dict):
            raise LLMConfigurationError(
                "LLM_MODEL_CONFIG_INVALID",
                f"model_profiles.{profile_key} must be a mapping",
            )
        profiles[profile_key] = dict(profile_payload)
    return profiles


def _parse_float_config_value(task_name: str, payload: dict[str, Any], field: str) -> float:
    value = payload[field]
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name}.{field} must be a valid float",
        ) from exc


def _optional_sampling_value(task_name: str, payload: dict[str, Any], field: str) -> float | None:
    """§7: parse an optional decoding-level sampling knob; absent key → None (provider default)."""
    if field not in payload or payload[field] is None:
        return None
    try:
        return float(payload[field])
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name}.{field} must be a valid float",
        ) from exc


def _parse_int_config_value(task_name: str, payload: dict[str, Any], field: str) -> int:
    value = payload[field]
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name}.{field} must be a valid integer",
        ) from exc


def _parse_response_format(task_name: str, payload: dict[str, Any]) -> str:
    value = str(payload["response_format"])
    if value not in SUPPORTED_RESPONSE_FORMATS:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} has unsupported response_format {value}",
        )
    return value


def _parse_provider(task_name: str, payload: dict[str, Any]) -> str:
    value = str(payload.get("provider") or payload.get("provider_type") or "openai_compatible")
    if value not in SUPPORTED_PROVIDERS:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} has unsupported provider {value}",
        )
    return value


def _parse_reasoning_level(task_name: str, payload: dict[str, Any]) -> Literal["off", "low", "medium", "high"]:
    value = str(payload.get("reasoning_level", "medium"))
    if value not in SUPPORTED_REASONING_LEVELS:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} has unsupported reasoning_level {value}",
        )
    return value  # type: ignore[return-value]


def _parse_api_mode(task_name: str, payload: dict[str, Any]) -> Literal["responses", "chat"]:
    value = str(payload.get("api_mode", "responses"))
    if value not in SUPPORTED_API_MODES:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} has unsupported api_mode {value}",
        )
    return value  # type: ignore[return-value]


def _parse_credential_mode(task_name: str, payload: dict[str, Any]) -> Literal["api_key", "none"] | None:
    if payload.get("credential_mode") is None:
        return None
    value = str(payload.get("credential_mode"))
    if value not in SUPPORTED_CREDENTIAL_MODES:
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name} has unsupported credential_mode {value}",
        )
    return value  # type: ignore[return-value]


def _parse_provider_options(task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider_options = payload.get("provider_options", {})
    if not isinstance(provider_options, dict):
        raise LLMConfigurationError(
            "LLM_MODEL_CONFIG_INVALID",
            f"task_routing.{task_name}.provider_options must be a mapping",
        )
    return dict(provider_options)


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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


def _extract_raw_usage(body: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("usage", "usageMetadata"):
        value = body.get(key)
        if isinstance(value, dict):
            return dict(value)
    ollama_keys = ("prompt_eval_count", "eval_count")
    if any(key in body for key in ollama_keys):
        return {key: body.get(key) for key in ollama_keys if key in body}
    return None


def _usage_output_tokens(raw_usage: dict[str, Any] | None) -> int | None:
    """各家用法里的「输出 token 数」(OpenAI completion_tokens / Responses output_tokens /
    Gemini candidatesTokenCount / Ollama eval_count);取不到返 None。"""
    if not isinstance(raw_usage, dict):
        return None
    for key in ("completion_tokens", "output_tokens", "candidatesTokenCount", "eval_count"):
        number = _usage_number(raw_usage.get(key))
        if number is not None:
            return number
    return None


def _usage_number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or int(value) != value:
        return None
    return int(value)


def _raw_usage_is_complete(usage: dict[str, Any] | None) -> bool:
    if usage is None:
        return False
    key_sets = (
        ("input_tokens", "output_tokens", "total_tokens"),
        ("prompt_tokens", "completion_tokens", "total_tokens"),
        ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"),
        ("prompt_eval_count", "eval_count", None),
    )
    for input_key, output_key, total_key in key_sets:
        if input_key not in usage and output_key not in usage:
            continue
        input_tokens = _usage_number(usage.get(input_key))
        output_tokens = _usage_number(usage.get(output_key))
        if input_tokens is None or output_tokens is None:
            return False
        if total_key is None or total_key not in usage:
            return True
        total_tokens = _usage_number(usage.get(total_key))
        return total_tokens is not None and total_tokens == input_tokens + output_tokens
    return False


def _safe_usage_int(value: Any) -> int:
    parsed = _usage_number(value)
    return parsed if parsed is not None else 0


def _normalize_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    if "input_tokens" in usage or "output_tokens" in usage:
        input_tokens = _safe_usage_int(usage.get("input_tokens", 0))
        output_tokens = _safe_usage_int(usage.get("output_tokens", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": _safe_usage_int(usage.get("total_tokens", input_tokens + output_tokens)),
        }

    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        input_tokens = _safe_usage_int(usage.get("promptTokenCount", 0))
        output_tokens = _safe_usage_int(usage.get("candidatesTokenCount", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": _safe_usage_int(usage.get("totalTokenCount", input_tokens + output_tokens)),
        }

    if "prompt_eval_count" in usage or "eval_count" in usage:
        input_tokens = _safe_usage_int(usage.get("prompt_eval_count", 0))
        output_tokens = _safe_usage_int(usage.get("eval_count", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    return {
        "input_tokens": _safe_usage_int(usage.get("prompt_tokens", 0)),
        "output_tokens": _safe_usage_int(usage.get("completion_tokens", 0)),
        "total_tokens": _safe_usage_int(usage.get("total_tokens", 0)),
    }


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
