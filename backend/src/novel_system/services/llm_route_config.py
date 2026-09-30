"""LLM 节点路由的设置页视图与写路由的守门（从 system_config 拆出，B09-13）。

overview 每个节点的就绪判断（服务在不在、启用没有、密钥能不能解开、模型在不在服务列表里）、三个分工槽的当前
绑定（从节点路由反推）、退役节点路由的识别与剪枝、一键补齐 / 分工写快照之前的激活守门。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.errors import DomainError
from novel_system.services.llm_node_registry import active_llm_node_ids, llm_node_catalog, role_slot_catalog
from novel_system.services.llm_provider_config import normalize_provider_model_ids
from novel_system.services.llm_routing import LEGACY_TASK_ALIASES, parse_model_routing_config, parse_node_route
from novel_system.services.llm_providers.base import LLMConfigurationError, SUPPORTED_API_MODES
from novel_system.services.value_coercion import optional_text


def serialize_task_config(node_id: str, task_config, spec: dict[str, Any] | None) -> dict[str, Any]:
    payload = {
        "node_id": node_id,
        "status": "active",
        "configured": True,
        "provider": task_config.provider,
        "provider_id": task_config.provider_id,
        "account_id": task_config.account_id,
        "model": task_config.model,
        "temperature": task_config.temperature,
        "max_output_tokens": task_config.max_output_tokens,
        "response_format": task_config.response_format,
        "reasoning_level": task_config.reasoning_level,
        "api_mode": task_config.api_mode,
        "credential_mode": task_config.credential_mode,
        "provider_options": task_config.provider_options,
    }
    if spec:
        payload.update(
            {
                "status": spec["status"],
                "group": spec["group"],
                "label": spec["label"],
                "requires_llm": spec["requires_llm"],
                "template_name": spec["template_name"],
                "order": spec["order"],
            }
        )
    else:
        payload.setdefault("requires_llm", True)
    return payload


def provider_view_ready(provider: dict[str, Any]) -> bool:
    if provider.get("enabled") is False:
        return False
    credential_mode = str(provider.get("credential_mode") or "api_key")
    if credential_mode == "none":
        return True
    secret = provider.get("secret") if isinstance(provider.get("secret"), dict) else {}
    # BUG-001: 必须"可解密"才算就绪——仅"存在"会在 config.secret 轮换后假阳性。
    return secret.get("decryptable") is True


def route_readiness(route: dict[str, Any], providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    provider_id = optional_text(route.get("provider_id"))
    model = optional_text(route.get("model"))
    configured = bool(route.get("configured") or provider_id or model)
    if not configured:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": False,
            "model_missing": False,
            "readiness_reason": "not_configured",
        }

    if not provider_id:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": True,
            "model_missing": False,
            "readiness_reason": "provider_id_missing",
        }
    provider = providers.get(provider_id)
    if provider is None:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": True,
            "model_missing": False,
            "readiness_reason": f"provider_not_found:{provider_id}",
        }

    provider_ready = provider_view_ready(provider)
    if not model:
        return {
            "ready": False,
            "provider_ready": provider_ready,
            "provider_missing": False,
            "model_missing": True,
            "readiness_reason": "model_missing",
        }
    models = normalize_provider_model_ids(provider.get("models") or [])
    model_missing = bool(models and model not in models)
    ready = provider_ready and not model_missing
    reason = "ready"
    if not provider_ready:
        # BUG-001: 区分"未配置密钥" vs "密文无法解密(config.secret 轮换)",别压成一态。
        secret = provider.get("secret") if isinstance(provider.get("secret"), dict) else {}
        credential_mode = str(provider.get("credential_mode") or "api_key")
        if provider.get("enabled") is False:
            reason = "provider_disabled"
        elif credential_mode != "none" and secret.get("configured") and not secret.get("decryptable"):
            reason = "secret_decrypt_failed"
        elif credential_mode != "none" and not secret.get("configured"):
            reason = "secret_missing"
        else:
            reason = "provider_not_ready"
    elif model_missing:
        reason = f"model_not_listed:{model}"
    return {
        "ready": ready,
        "provider_ready": provider_ready,
        "provider_missing": False,
        "model_missing": model_missing,
        "readiness_reason": reason,
    }


def annotate_node_route_readiness(
    *,
    node_routes: dict[str, dict[str, Any]],
    providers: dict[str, dict[str, Any]],
) -> None:
    for route in node_routes.values():
        route.update(route_readiness(route, providers))


def llm_readiness_summary(
    *,
    providers: dict[str, dict[str, Any]],
    node_routes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    provider_count = len(providers)
    active_provider_count = sum(1 for provider in providers.values() if provider_view_ready(provider))
    active_routes = list(node_routes.values())
    configured_routes = [
        route
        for route in active_routes
        if route.get("configured") or route.get("provider_id") or route.get("model")
    ]
    ready_routes = [route for route in active_routes if route.get("ready") is True]
    blocked_routes = [route for route in active_routes if route.get("ready") is not True]
    return {
        "provider_count": provider_count,
        "active_provider_count": active_provider_count,
        "configured_route_count": len(configured_routes),
        "active_route_count": len(active_routes),
        "ready_route_count": len(ready_routes),
        "blocked_route_count": len(blocked_routes),
        "blocked_routes": [
            {
                "node_id": route.get("node_id"),
                "provider_id": route.get("provider_id"),
                "model": route.get("model"),
                "reason": route.get("readiness_reason"),
            }
            for route in blocked_routes
        ],
        "ready": active_provider_count > 0 and len(ready_routes) > 0 and not blocked_routes,
    }


def role_slot_overview(node_routes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """槽位目录 + 从 node_routes 反推当前生效绑定(槽内全节点一致才算)。"""
    slots: list[dict[str, Any]] = []
    for entry in role_slot_catalog():
        bindings = {
            (
                optional_text((node_routes.get(node_id) or {}).get("provider_id")),
                optional_text((node_routes.get(node_id) or {}).get("model")),
            )
            for node_id in entry["node_ids"]
        }
        if len(bindings) == 1:
            provider_id, model = next(iter(bindings))
            current = (
                {"provider_id": provider_id, "model": model, "mixed": False}
                if provider_id or model
                else None
            )
        else:
            current = {"provider_id": None, "model": None, "mixed": True}
        slots.append({**entry, "current": current})
    return slots


def retired_route_ids(*routing_tables: dict[str, Any]) -> list[str]:
    """路由表里已退役的键:不在节点注册表,也不是 task 别名。"""
    node_catalog = llm_node_catalog()
    return sorted(
        {
            str(key)
            for table in routing_tables
            for key in table
            if key not in node_catalog and key not in LEGACY_TASK_ALIASES
        }
    )


def stale_route_ids(models_payload: Any) -> list[str]:
    """一份 models 配置(快照内容)里退役节点的路由 id,按原始的两张表算:解析时放在一边的退役路由也在内。"""
    if not isinstance(models_payload, dict):
        return []
    tables = [models_payload.get(name) for name in ("node_routing", "task_routing")]
    return retired_route_ids(*(table for table in tables if isinstance(table, dict)))


def nodes_needing_route(node_routing: dict[str, Any], providers: dict[str, dict[str, Any]]) -> list[str]:
    """一键补齐要重绑的注册表节点(按注册表顺序):没有路由、自己的路由解析不了、没绑服务或模型、服务或模型
    不就绪。逐条解析 ``node_routing``(写路由的起点,见 ``writable_models_payload``),不看整份解析的 overview
    ——整份快照读不懂时 overview 说「全部未配」,拿它决定重写谁会冲掉作者每一条解析得了的路由。"""
    needed: list[str] = []
    for node_id in active_llm_node_ids():
        route = node_routing.get(node_id)
        try:
            view = serialize_task_config(node_id, parse_node_route(node_id, route), None) if route is not None else None
        except LLMConfigurationError:
            view = None
        if (
            view is None
            or not optional_text(view.get("provider_id"))
            or not optional_text(view.get("model"))
            or route_readiness(view, providers).get("ready") is not True
        ):
            needed.append(node_id)
    return needed


def prune_retired_routes(config_payload: dict[str, Any]) -> list[str]:
    """就地剪掉 node_routing / task_routing 里退役节点的路由,返回剪掉的 id(已排序)。

    parse_model_routing_config 会把 task_routing 并入 node_routing,所以两张表都得剪,
    否则退役条目会在下一次解析时重新冒出来;在解析之前剪,退役条目自身的字段错误也
    不会再阻塞保存。
    """
    pruned: set[str] = set()
    for table_name in ("node_routing", "task_routing"):
        table = config_payload.get(table_name)
        if not isinstance(table, dict):
            continue
        for key in retired_route_ids(table):
            del table[key]
            pruned.add(key)
    return sorted(pruned)


def writable_models_payload(models_category: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """写路由(一键补齐 / 分工)的起点:当前活动 models 快照里只留 ``node_routing``,返回 (新快照内容, 剪掉的退役节点)。

    老快照 ``task_routing`` 里还在起作用的条目(node_routing 里没有的节点)并进 node_routing,解析结果不变;
    其余各段不再抄进新快照——task_routing 是节点注册表的旧抄本,retry_budget / job_runtime 以仓库 models.yaml
    为准,model_profiles / role_assignments 没人读(2026-09-30 重评 R4、批准#5a)。没有活动快照时从空表开始:
    注册表默认路由只给「API 配置来自环境变量」的安装用,不写进作者的快照。
    """
    current = dict(models_category.get("parsed") or {}) if models_category.get("active_snapshot") else {}
    node_routing = dict(current.get("node_routing") or {})
    legacy_task_routing = current.get("task_routing")
    if isinstance(legacy_task_routing, dict):
        for key, route in legacy_task_routing.items():
            if key not in LEGACY_TASK_ALIASES and key not in node_routing:
                node_routing[key] = route
    config_payload: dict[str, Any] = {"node_routing": node_routing}
    return config_payload, prune_retired_routes(config_payload)


def parse_route_config_or_raise(config_payload: dict[str, Any]):
    """路由解析错误(某节点 api_mode / response_format 写错等)是作者可修的配置问题,
    必须以 422 + 点名节点的消息回到设置页,而不是漏成 500 INTERNAL_ERROR。"""
    try:
        return parse_model_routing_config(config_payload)
    except LLMConfigurationError as exc:
        raise DomainError(
            "CONFIG_ROUTE_INVALID",
            f"LLM node route config is invalid: {exc.message}",
            status_code=422,
            details={"llm_error_code": exc.code},
        ) from exc


def provider_route_api_mode(provider_id: str, provider: dict[str, Any]) -> str:
    """把服务声明的 api_mode 展开到节点路由前先校验:老快照里的非法值会让路由解析
    在下游炸成 500,这里改为 422 并点名该服务。"""
    api_mode = optional_text(provider.get("api_mode")) or "responses"
    if api_mode not in SUPPORTED_API_MODES:
        raise DomainError(
            "CONFIG_PROVIDER_INVALID",
            f"provider {provider_id} has unsupported api_mode {api_mode}; "
            f"set it to one of {', '.join(sorted(SUPPORTED_API_MODES))} and save the provider again",
            status_code=422,
        )
    return api_mode


def validate_activating_node_route_bindings(
    *,
    node_routing: dict[str, Any],
    providers: dict[str, dict[str, Any]],
) -> None:
    missing_bindings: list[str] = []
    missing_models: list[str] = []
    not_ready_providers: list[str] = []
    node_catalog = llm_node_catalog()
    for node_id, task_config in node_routing.items():
        if node_id not in node_catalog:
            # 退役节点的残留路由是惰性的:不参与激活校验(它常指向已删除的服务),
            # 由写路径剪枝、overview 的 stale_routes 展示。
            continue
        provider_id = task_config.provider_id
        if not provider_id or provider_id not in providers:
            missing_bindings.append(f"{node_id}:{provider_id or 'missing_provider_id'}")
            continue
        if not provider_view_ready(providers[provider_id]):
            not_ready_providers.append(f"{node_id}:{provider_id}")
        models = normalize_provider_model_ids(providers[provider_id].get("models") or [])
        if not optional_text(task_config.model):
            missing_models.append(f"{node_id}:{provider_id}:missing_model")
        elif models and task_config.model not in models:
            missing_models.append(f"{node_id}:{provider_id}:{task_config.model}")

    if missing_bindings:
        raise DomainError(
            "CONFIG_ROUTE_PROVIDER_MISSING",
            "active LLM node routes must reference an existing provider_id: " + ", ".join(missing_bindings),
            status_code=422,
        )
    if not_ready_providers:
        raise DomainError(
            "CONFIG_ROUTE_PROVIDER_NOT_READY",
            "active LLM node routes must reference an enabled provider with configured credentials: "
            + ", ".join(not_ready_providers),
            status_code=422,
        )
    if missing_models:
        raise DomainError(
            "CONFIG_ROUTE_MODEL_MISSING",
            "active LLM node routes must use a model listed by their provider config: " + ", ".join(missing_models),
            status_code=422,
        )
