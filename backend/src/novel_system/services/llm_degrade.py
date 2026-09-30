"""LLM 连通性降级阶梯与连通性能力缓存（从 llm_client 拆出，2026-09-30）。

``LLMClient.generate`` 在一次请求因能力不匹配失败后问这里「能不能降一级再试」：
/responses 404 换 chat、结构化输出被拒先弃 json_schema 再弃 wire response_format、
空正文关 reasoning 并抬输出预算、截断抬输出预算。降级后成功学到的档位记进进程内的
能力缓存，同一服务 + 模型后续直接按学到的档位发，不再每次浪费一跳注定失败的探测。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from novel_system.cache_registry import register_cache_reset
from novel_system.services.llm_providers.base import (
    LLMClientError,
    LLMHTTPError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponseError,
    ProviderRuntimeConfig,
)

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


def structured_output_rejected(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(sig in lowered for sig in _STRUCTURED_OUTPUT_ERROR_SIGNATURES)


def thinking_with_forced_tool_rejected(text: str | None) -> bool:
    """Anthropic 400:「Thinking may not be enabled when tool_choice forces tool use」(中转实现 json_schema 的副作用)。"""
    lowered = (text or "").lower()
    return "thinking may not be enabled" in lowered or (
        "thinking" in lowered and "tool_choice" in lowered and "forces" in lowered
    )


MAX_OUTPUT_TOKENS_CEILING = 8192


def inline_schema_into_messages(request: LLMRequest) -> LLMRequest:
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


def thinking_disabled(request: LLMRequest) -> bool:
    extra = (request.provider_options or {}).get("extra_payload") or {}
    return extra.get("enable_thinking") is False


def with_thinking_disabled(request: LLMRequest) -> LLMRequest:
    """qwen/vllm/lightllm 系模型默认开思考且不认 OpenAI 的 reasoning 参数——
    发它们各自认识的关思考开关(未知键会被这类引擎忽略,不影响其它中转)。"""
    opts = dict(request.provider_options or {})
    extra = dict(opts.get("extra_payload") or {})
    extra.setdefault("chat_template_kwargs", {"enable_thinking": False})
    extra.setdefault("enable_thinking", False)
    opts["extra_payload"] = extra
    return replace(request, provider_options=opts)


def degrade_request_after_failure(
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
        if provider_config.provider_type == "openai_compatible" and not thinking_disabled(req):
            req = with_thinking_disabled(req)
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
    if thinking_with_forced_tool_rejected(detail_text) and request.reasoning_level != "off":
        return (
            replace(request, reasoning_level="off"),
            "reasoning 降级:中转把 json_schema 实现成强制工具调用,Anthropic 不允许同时开启思考",
        )
    if structured_output_rejected(detail_text):
        if request.response_schema is not None:
            return (
                inline_schema_into_messages(request),
                "结构化输出降级:弃 wire json_schema,退 json_object 并把 schema 内联进 prompt",
            )
        if request.response_format == "json_object" and request.wire_response_format:
            return (
                replace(request, wire_response_format=False),
                "结构化输出降级:不再发送 response_format,仅靠 prompt 约束 JSON",
            )
    return None


# 连通性能力缓存(进程内):记录某个服务端点 + 模型已实证的能力上限,
# 后续调用直接按学到的档位发请求,不再每次浪费一跳注定失败的探测
# (重分类一本书要打数百批次,不缓存 = 数百个多余 400)。
#   api_mode: 404 降级学到的端点模式
#   structured_tier: 2=json_schema / 1=json_object / 0=不发 response_format
# 键带上服务的类型、地址与声明的 api_mode(connectivity_caps_key):作者在设置里把同一个服务改到
# 另一个中转 / 另一种模式后,旧中转学到的降级不再套到新中转上(B09-22,以前只按 (provider_id, model)
# 记,要等后端重启才忘);多个后端进程各自学、各自忘,不需要跨进程失效通知。
CONNECTIVITY_CAPS: dict[tuple[str, ...], dict[str, Any]] = {}
register_cache_reset("llm_degrade.connectivity_caps", CONNECTIVITY_CAPS.clear)


def connectivity_caps_key(provider_config: ProviderRuntimeConfig, model: str) -> tuple[str, ...]:
    return (
        str(provider_config.provider_id or ""),
        str(provider_config.provider_type or ""),
        str(provider_config.base_url or "").rstrip("/"),
        str(getattr(provider_config, "api_mode", "") or ""),
        str(model or ""),
    )


def request_structured_tier(request: LLMRequest) -> int:
    if request.response_format != "json_object":
        return 0
    if not request.wire_response_format:
        return 0
    return 2 if request.response_schema is not None else 1


def apply_connectivity_caps(request: LLMRequest, provider_config: ProviderRuntimeConfig) -> LLMRequest:
    caps = CONNECTIVITY_CAPS.get(connectivity_caps_key(provider_config, request.model))
    if not caps:
        return request
    req = request
    cached_mode = caps.get("api_mode")
    if cached_mode in ("chat", "responses") and req.api_mode != cached_mode:
        req = replace(req, api_mode=cached_mode)
    tier = caps.get("structured_tier")
    if isinstance(tier, int) and tier < request_structured_tier(req):
        if tier <= 1 and req.response_schema is not None:
            req = inline_schema_into_messages(req)
        if tier <= 0 and req.wire_response_format:
            req = replace(req, wire_response_format=False)
    if caps.get("reasoning_level") == "off" and req.reasoning_level != "off":
        req = replace(req, reasoning_level="off")
    if caps.get("disable_thinking") and not thinking_disabled(req):
        req = with_thinking_disabled(req)
    return req


def record_connectivity_caps(
    original: LLMRequest, final: LLMRequest, provider_config: ProviderRuntimeConfig
) -> None:
    """降级后成功才记录(hops>0 时调用):只降不升,进程重启即重置。"""
    caps = CONNECTIVITY_CAPS.setdefault(connectivity_caps_key(provider_config, final.model), {})
    if final.api_mode != original.api_mode:
        caps["api_mode"] = final.api_mode
    final_tier = request_structured_tier(final)
    if final_tier < request_structured_tier(original):
        prev = caps.get("structured_tier")
        caps["structured_tier"] = min(prev, final_tier) if isinstance(prev, int) else final_tier
    # 空正文降级学到的 reasoning 关闭同样缓存(输出预算按节点各异,不缓存)
    if final.reasoning_level == "off" and original.reasoning_level != "off":
        caps["reasoning_level"] = "off"
    if thinking_disabled(final) and not thinking_disabled(original):
        caps["disable_thinking"] = True


def normalize_api_mode(request: LLMRequest, provider_config: ProviderRuntimeConfig) -> LLMRequest:
    """按 provider 声明的端点能力归一化 api_mode(chat-only 中转收到 /responses 必 404)。

    env 回退路径的 provider_config 由 request 反构(api_mode=request.api_mode),
    此归一化自动 no-op;运行时 provider 配置携带显式声明时以之为准。
    build 与响应解析(extract_output_text)共用 request.api_mode,必须在入口统一。
    """
    declared = getattr(provider_config, "api_mode", None)
    if declared in ("chat", "responses") and request.api_mode != declared:
        return replace(request, api_mode=declared)
    return request
