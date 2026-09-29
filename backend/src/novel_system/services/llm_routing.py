"""LLM 模型路由：节点 → 服务 / 模型 / 采样参数（从 llm_client 拆出，2026-09-30）。

路由来源：库里有活动 models 快照读快照（系统配置保存的节点路由），否则读 ``config/models.yaml``。
``resolve_node_route`` 是运行时查路由的唯一入口，``build_llm_request`` 把一条路由翻译成 ``LLMRequest``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from novel_system.accounting_contract import DEFAULT_PROVIDER_ATTEMPT_BUDGET
from novel_system.services.config_cache import ContentKeyedCache, safe_load_yaml
from novel_system.services.llm_providers import default_provider_base_urls, supported_provider_types
from novel_system.services.llm_providers.base import (
    LLMConfigurationError,
    LLMRequest,
    SUPPORTED_API_MODES,
    SUPPORTED_CREDENTIAL_MODES,
    SUPPORTED_REASONING_LEVELS,
    SUPPORTED_RESPONSE_FORMATS,
)
from novel_system.services.value_coercion import optional_text

SUPPORTED_PROVIDERS = frozenset(supported_provider_types())
DEFAULT_PROVIDER_BASE_URLS = default_provider_base_urls()


@dataclass(slots=True, frozen=True)
class TaskModelConfig:
    provider: str
    model: str
    temperature: float
    max_output_tokens: int
    response_format: str
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



# 解析好的路由按来源内容记忆（活动 models 快照的原文 / 文件正文，见 config_cache）：同一份内容只解析一次，
# 内容一变（系统配置保存路由 / 切换快照、raise_llm_output_budget、改文件）下一次读取就重新解析。
_MODEL_ROUTING_CACHE = ContentKeyedCache(maxsize=4)


def load_model_routing_config(path: str | Path | None = None) -> ModelRoutingConfig:
    """模型路由：库里有活动 models 快照读快照，否则读 ``config/models.yaml``；给 ``path`` 就读那份文件。

    返回的 ``ModelRoutingConfig`` 由缓存共享、只读（调用方都只查表；要改先拷贝）。
    """
    if path is None:
        from novel_system.services.config_snapshot_reader import load_active_config_parsed

        routing = load_active_config_parsed(
            "models", parse_model_routing_config, cache=_MODEL_ROUTING_CACHE
        )
        if routing is not None:
            return routing
        config_path = _default_models_config_path()
    else:
        config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    return _MODEL_ROUTING_CACHE.get_or_build(
        ("file", text), lambda: parse_model_routing_config(safe_load_yaml(text))
    )


def reset_model_routing_cache() -> None:
    """清空模型路由的解析缓存（缓存按内容取键，本身不会过期；给测试 / 释放内存用）。"""
    _MODEL_ROUTING_CACHE.clear()


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
            provider_id=optional_text(payload.get("provider_id")),
            account_id=optional_text(payload.get("account_id")),
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
