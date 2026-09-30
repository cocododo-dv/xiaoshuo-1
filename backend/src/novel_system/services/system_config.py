"""系统配置：五类配置快照（api / models / prompts / allowlists / hash_contract）与「设置 → AI 模型」的服务。

2026-09-30 拆开（B09-13 / B09-11）：
* ``system_config_secrets``：密钥的加密存取与状态；
* ``llm_provider_config``：服务（provider）配置的规范化、叠到环境设置上、运行时配置与客户端工厂；
* ``llm_provider_probe``：「测试连接」与拉模型列表；
* ``llm_route_config``：节点路由的就绪视图、分工槽、退役路由剪枝、写路由的激活守门；
本模块留 ``SystemConfigService``（快照的草稿 / 激活，设置页的各个动作）、配置校验与默认值、管理令牌，
并转出拆出去的公开名字（路由、工具、各服务从这里 import）。

``load_active_config_payload`` / ``load_secret_value`` 在本模块经 ``SessionLocal`` 与 ``time.sleep``
读库（测试在这里打桩）。没有界面能重新激活旧快照：运维回滚用 ``SystemConfigService(session).activate(
<snapshot_id>, actor_ref=...)``（``sync_prompt_templates`` / ``raise_llm_output_budget`` 也是这样写快照的）。
"""

from __future__ import annotations

import hmac
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import OperationLog, SystemConfigSnapshot, SystemSecret, utcnow
from novel_system.db.session import SessionLocal
from novel_system.env_config import DEFAULT_LLM_TIMEOUT_SECONDS, env_admin_token, env_config_secret, load_env_settings
from novel_system.services.config_snapshot_reader import active_snapshot, read_with_transient_retry
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import normalize
from novel_system.services.llm_client import LLMConfigurationError, ProviderRuntimeConfig
from novel_system.services.llm_node_registry import (
    active_llm_node_ids,
    default_task_config_payload,
    get_role_slot_spec,
    llm_node_catalog,
    role_slot_node_ids,
)
from novel_system.services.llm_provider_config import (
    apply_active_api_config,
    bool_value,
    build_llm_client,
    build_runtime_llm_client,
    coerce_api_payload,
    load_llm_provider_runtime_configs,
    normalize_provider_base_url,
    normalize_provider_model_ids,
    normalize_provider_payload,
    provider_catalog,
    provider_payloads_from_llm,
    required_text,
    validate_api_config,
)
from novel_system.services.llm_provider_probe import (
    PROVIDER_PROBE_TIMEOUT_SECONDS,
    ProbeTarget,
    fetch_model_listing,
    listed_model_ids,
    probe_timeout_seconds,
    provider_error_summary,
    run_provider_probe,
)
from novel_system.services.llm_providers import adapter_registry, get_provider_preset, provider_preset_payloads
from novel_system.services.llm_route_config import (
    annotate_node_route_readiness,
    llm_readiness_summary,
    nodes_needing_route,
    parse_route_config_or_raise,
    provider_route_api_mode,
    provider_view_ready,
    role_slot_overview,
    serialize_task_config,
    stale_route_ids,
    validate_activating_node_route_bindings,
    writable_models_payload,
)
from novel_system.services.llm_routing import parse_model_routing_config, routing_for_models_payload
from novel_system.services.prompt_builder import PromptConfigurationError, parse_prompt_templates
from novel_system.services.system_config_secrets import (
    LLM_API_KEY_SECRET_ID,
    LLM_PROVIDER_SECRET_PREFIX,
    llm_provider_api_key_secret_id,
    none_secret_status,
    save_secret_value,
    secret_status,
    secret_value,
)
from novel_system.services.value_coercion import optional_text

__all__ = [
    "CONFIG_CATEGORIES",
    "LLM_API_KEY_SECRET_ID",
    "LLM_PROVIDER_SECRET_PREFIX",
    "ProviderRuntimeConfig",
    "SystemConfigService",
    "YAML_CONFIG_FILES",
    "apply_active_api_config",
    "build_llm_client",
    "build_runtime_llm_client",
    "default_config_payload",
    "llm_provider_api_key_secret_id",
    "load_active_config_payload",
    "load_llm_provider_runtime_configs",
    "load_secret_value",
    "repo_config_dir",
    "require_admin_token",
    "validate_config",
]


CONFIG_CATEGORIES = ("api", "models", "prompts", "allowlists", "hash_contract")
YAML_CONFIG_FILES = {
    "models": "models.yaml",
    "prompts": "prompts.yaml",
    "allowlists": "allowlists.yaml",
    "hash_contract": "hash_contract.yaml",
}


def repo_config_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "config"


def _read_with_transient_retry(reader):
    return read_with_transient_retry(reader, sleep=time.sleep)


def load_active_config_payload(category: str) -> dict[str, Any] | None:
    from novel_system.services.config_snapshot_reader import load_active_config_payload as read_payload

    return read_payload(category, session_factory=SessionLocal, sleep=time.sleep)


def load_secret_value(secret_id: str) -> str | None:
    def _read():
        with SessionLocal() as session:
            return secret_value(session, secret_id)

    return _read_with_transient_retry(_read)


class SystemConfigService:
    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        self.session = session
        self.auto_commit = auto_commit

    def _finish_mutation(self) -> None:
        """Commit direct calls, or only flush when an API wrapper owns commit."""

        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

    def overview(self, *, include_content: bool = True) -> dict[str, Any]:
        """运行时状态与五类配置的现状。

        ``include_content`` 时每类带 YAML 正文与解析结果（运维工具要读）；``GET /api/v1/system-config``
        只要摘要（来源、校验结果、活动快照的版本，api 的密钥状态）——以前它把每一份历史快照的正文都带上，
        MB 级，而唯一的调用方（开发启动脚本的探活）只看 runtime（2026-09-30 重评 R15a）。
        """
        categories = {}
        for category in CONFIG_CATEGORIES:
            payload = self._category_payload(category)
            if not include_content:
                payload = _category_summary(payload)
            categories[category] = payload
        return {
            "runtime": {
                "admin_configured": bool(_admin_token()),
                "secret_configured": bool(_config_secret()),
                "supported_categories": list(CONFIG_CATEGORIES),
            },
            "categories": categories,
        }

    def create_draft(
        self,
        *,
        category: str,
        yaml_raw: str,
        secrets: dict[str, str] | None,
        actor_ref: str,
    ) -> dict[str, Any]:
        _ensure_category(category)
        parsed, validation = validate_config(category, yaml_raw)
        if not validation["ok"]:
            raise DomainError(
                "CONFIG_VALIDATION_FAILED",
                validation["message"],
                status_code=422,
                details=validation,
            )

        version = self._next_version(category)
        snapshot = SystemConfigSnapshot(
            snapshot_id=f"config_{category}_{uuid.uuid4().hex[:12]}",
            category=category,
            version=version,
            yaml_raw=yaml_raw,
            parsed_json=parsed,
            validation_json=validation,
            status="draft",
            active_flag=0,
            created_by=actor_ref,
        )
        self.session.add(snapshot)
        secret_payload = self._save_secrets(category=category, secrets=secrets or {}, actor_ref=actor_ref)
        self._finish_mutation()
        return {
            "snapshot": _serialize_snapshot(snapshot),
            "secrets": secret_payload,
        }

    def activate(self, snapshot_id: str, *, actor_ref: str) -> dict[str, Any]:
        snapshot = self.session.get(SystemConfigSnapshot, snapshot_id)
        if snapshot is None:
            raise DomainError("CONFIG_SNAPSHOT_NOT_FOUND", "config snapshot was not found", status_code=404)
        validation = dict(snapshot.validation_json or {})
        if validation.get("ok") is not True:
            raise DomainError(
                "CONFIG_VALIDATION_FAILED",
                validation.get("message") or "config snapshot is not valid",
                status_code=422,
                details=validation,
            )

        previous = active_snapshot(self.session, snapshot.category)
        if previous is not None and previous.snapshot_id != snapshot.snapshot_id:
            previous.active_flag = 0
            previous.status = "superseded"

        snapshot.active_flag = 1
        snapshot.status = "active"
        snapshot.activated_at = utcnow()
        self.session.add(
            OperationLog(
                event_type="system_config_activated",
                object_type="system_config",
                object_ref=snapshot.snapshot_id,
                payload_json={
                    "actor_ref": actor_ref,
                    "category": snapshot.category,
                    "version": snapshot.version,
                    "previous_snapshot_id": previous.snapshot_id if previous is not None else None,
                    "validation": validation,
                },
            )
        )
        self._finish_mutation()
        return {"snapshot": _serialize_snapshot(snapshot)}

    def test_provider(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        """「测试连接」：连接 → 模型名 → 最小生成三项检查（编排在 ``llm_provider_probe.run_provider_probe``）。"""
        return run_provider_probe(self.session, payload, read_secret=load_secret_value)

    def llm_overview(self) -> dict[str, Any]:
        api_payload = self._category_payload("api")
        models_payload = self._category_payload("models")
        llm_payload = coerce_api_payload(dict(api_payload.get("parsed") or {}))
        providers = {
            provider_id: self._serialize_provider(provider_id, provider_payload)
            for provider_id, provider_payload in provider_payloads_from_llm(llm_payload).items()
        }
        node_catalog = llm_node_catalog()
        try:
            # 与运行时同一条规则(llm_routing.load_model_routing_config):有快照只看快照;没有快照时
            # 库里没有服务 → 注册表默认路由,已保存过服务 → 一个节点也不配(显示未指派,等一键补齐)
            routing = routing_for_models_payload(
                models_payload.get("parsed") or {},
                from_snapshot=models_payload.get("active_snapshot") is not None,
                api_has_providers=bool(providers),
            )
            node_routes = {
                node_id: serialize_task_config(node_id, task_config, node_catalog.get(node_id))
                for node_id, task_config in routing.node_routing.items()
            }
        except LLMConfigurationError:
            node_routes = {}
        # 存量 models 快照的路由只前滚不剪枝：节点从注册表退役后，老安装的快照仍带着它的路由。这类目录外条目
        # 没有 spec，不该渲染成无名路由行或计入就绪统计，单列为 stale_routes 供排查、由一键补齐 / 分工剪掉。
        # 按快照的原始键算（解析时放在一边的、整份读不懂时的退役条目也照样列出）。
        stale_routes = stale_route_ids(models_payload.get("parsed"))
        for node_id in [node_id for node_id in node_routes if node_id not in node_catalog]:
            del node_routes[node_id]
        for node_id, spec in node_catalog.items():
            node_routes.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "status": spec["status"],
                    "configured": False,
                },
            )
            node_routes[node_id].update(
                {
                    "status": spec["status"],
                    "group": spec["group"],
                    "label": spec["label"],
                    "requires_llm": spec["requires_llm"],
                    "template_name": spec["template_name"],
                    "order": spec["order"],
                }
            )
        annotate_node_route_readiness(node_routes=node_routes, providers=providers)
        missing_active_routes = [
            node_id
            for node_id in active_llm_node_ids()
            if node_id in node_routes and not bool(node_routes[node_id].get("configured"))
        ]
        blocked_routes = [
            {
                "node_id": route.get("node_id"),
                "group": route.get("group"),
                "provider_id": route.get("provider_id"),
                "model": route.get("model"),
                "reason": route.get("readiness_reason"),
            }
            for route in node_routes.values()
            if route.get("configured") and route.get("ready") is not True
        ]
        return {
            "provider_catalog": provider_catalog(),
            "default_provider_id": llm_payload.get("default_provider_id") or next(iter(providers.keys()), None),
            # runtime 随 overview 带出,管理面前端无需再拉全量 /system-config(含全部历史快照)
            "runtime": {
                "admin_configured": bool(_admin_token()),
                "secret_configured": bool(_config_secret()),
            },
            "providers": providers,
            "node_catalog": node_catalog,
            "node_routes": node_routes,
            "missing_active_routes": missing_active_routes,
            "blocked_routes": blocked_routes,
            "stale_routes": stale_routes,
            "readiness": llm_readiness_summary(providers=providers, node_routes=node_routes),
            "role_slots": role_slot_overview(node_routes),
            "api_snapshot": api_payload.get("active_snapshot"),
            "models_snapshot": models_payload.get("active_snapshot"),
        }

    def save_llm_provider(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        try:
            provider = normalize_provider_payload(payload)
        except ValueError as exc:
            raise DomainError("CONFIG_PROVIDER_INVALID", str(exc), status_code=422) from exc
        provider_id = provider["provider_id"]
        api_key = optional_text(payload.get("api_key"))
        llm_payload = self._current_api_llm_payload()
        providers = provider_payloads_from_llm(llm_payload)
        providers[provider_id] = {key: value for key, value in provider.items() if key != "api_key"}
        llm_payload["providers"] = providers
        llm_payload["default_provider_id"] = llm_payload.get("default_provider_id") or provider_id
        llm_payload["enabled"] = True if provider["enabled"] else bool_value(llm_payload.get("enabled", True))
        llm_payload.setdefault("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
        snapshot = self._store_config_snapshot(
            category="api",
            parsed={"llm": llm_payload},
            validation={"ok": True, "message": "api config is valid"},
            status="active",
            active=True,
            actor_ref=actor_ref,
        )
        secret_status = self._secret_status(llm_provider_api_key_secret_id(provider_id))
        if provider["credential_mode"] == "none":
            existing_secret = self.session.get(SystemSecret, llm_provider_api_key_secret_id(provider_id))
            if existing_secret is not None:
                self.session.delete(existing_secret)
            secret_status = none_secret_status()
        elif api_key:
            secret_status = self._save_secret_value(
                secret_id=llm_provider_api_key_secret_id(provider_id),
                raw_value=api_key,
                actor_ref=actor_ref,
                secret_type="api_key",
                metadata={
                    "provider_id": provider_id,
                    "provider_type": provider["provider_type"],
                    "account_id": provider.get("account_id"),
                },
            )
        provider_view = self._serialize_provider(provider_id, provider)
        provider_view["secret"] = secret_status
        self._finish_mutation()
        return {
            "provider": provider_view,
            "snapshot": _serialize_snapshot(snapshot),
        }

    def set_default_llm_provider(self, *, provider_id: str, actor_ref: str) -> dict[str, Any]:
        provider_id = required_text(provider_id, "provider_id")
        llm_payload = self._current_api_llm_payload()
        providers = provider_payloads_from_llm(llm_payload)
        if provider_id not in providers:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)
        llm_payload["providers"] = providers
        llm_payload["default_provider_id"] = provider_id
        llm_payload["enabled"] = bool_value(llm_payload.get("enabled", True))
        llm_payload.setdefault("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
        snapshot = self._store_config_snapshot(
            category="api",
            parsed={"llm": llm_payload},
            validation={"ok": True, "message": "api config is valid"},
            status="active",
            active=True,
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {
            "default_provider_id": provider_id,
            "provider": self._serialize_provider(provider_id, providers[provider_id]),
            "snapshot": _serialize_snapshot(snapshot),
        }

    def delete_llm_provider(self, *, provider_id: str, actor_ref: str) -> dict[str, Any]:
        provider_id = required_text(provider_id, "provider_id")
        llm_payload = self._current_api_llm_payload()
        providers = provider_payloads_from_llm(llm_payload)
        if provider_id not in providers:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)
        providers.pop(provider_id)
        llm_payload["providers"] = providers
        if llm_payload.get("default_provider_id") == provider_id:
            next_default = next(iter(providers.keys()), None)
            if next_default is None:
                llm_payload.pop("default_provider_id", None)
            else:
                llm_payload["default_provider_id"] = next_default
        llm_payload["enabled"] = bool_value(llm_payload.get("enabled", True))
        llm_payload.setdefault("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
        snapshot = self._store_config_snapshot(
            category="api",
            parsed={"llm": llm_payload},
            validation={"ok": True, "message": "api config is valid"},
            status="active",
            active=True,
            actor_ref=actor_ref,
        )
        secret = self.session.get(SystemSecret, llm_provider_api_key_secret_id(provider_id))
        if secret is not None:
            self.session.delete(secret)
        # 节点路由不随删而清:仍指向该服务的路由会被就绪检查标为 blocked,
        # 这里带回清单,前端可提示「一键补齐路由」切到默认服务。
        # 退役节点(不在注册表)的残留路由无需重绑,不列入清单——它们由
        # sync-missing / role-routes 写路径剪掉。
        current_models = dict(self._category_payload("models").get("parsed") or {})
        node_catalog = llm_node_catalog()
        orphaned_route_node_ids = sorted(
            node_id
            for node_id, route in dict(current_models.get("node_routing") or {}).items()
            if node_id in node_catalog
            and isinstance(route, dict)
            and str(route.get("provider_id") or "") == provider_id
        )
        self.session.add(
            OperationLog(
                event_type="system_config_llm_provider_deleted",
                object_type="system_config",
                object_ref=snapshot.snapshot_id,
                payload_json={
                    "actor_ref": actor_ref,
                    "provider_id": provider_id,
                    "default_provider_id": llm_payload.get("default_provider_id"),
                    "orphaned_route_node_ids": orphaned_route_node_ids,
                },
            )
        )
        self._finish_mutation()
        return {
            "deleted_provider_id": provider_id,
            "default_provider_id": llm_payload.get("default_provider_id"),
            "orphaned_route_node_ids": orphaned_route_node_ids,
            "snapshot": _serialize_snapshot(snapshot),
        }

    def sync_missing_llm_node_routes(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        overview = self.llm_overview()
        providers = overview["providers"]
        llm_payload = self._current_api_llm_payload()
        provider_id = optional_text(payload.get("provider_id")) or optional_text(llm_payload.get("default_provider_id"))
        if provider_id is None:
            provider_id = next(iter(providers.keys()), None)
        if provider_id is None or provider_id not in providers:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", "configure a provider before syncing node routes", status_code=422)

        provider = providers[provider_id]
        if not provider_view_ready(provider):
            raise DomainError(
                "CONFIG_ROUTE_PROVIDER_NOT_READY",
                f"provider {provider_id} is not ready; enable it and configure credentials first",
                status_code=422,
            )
        models = normalize_provider_model_ids(provider.get("models") or [])
        model = optional_text(payload.get("model")) or (models[0] if models else None)
        if not model:
            raise DomainError(
                "CONFIG_ROUTE_MODEL_MISSING",
                f"provider {provider_id} has no models; probe or fill the model list first",
                status_code=422,
            )
        if models and model not in models:
            raise DomainError(
                "CONFIG_ROUTE_MODEL_MISSING",
                f"model {model} is not listed by provider {provider_id}",
                status_code=422,
            )

        # 老安装的快照里可能还带着已退役节点的路由(常指向早已删除的服务)。
        # 这里只补齐目录内节点,退役条目不会被重绑,却会被激活校验拦成 422,
        # 让「一键补齐路由」永远失败——先剪掉,并在响应里告知剪了什么。
        config_payload, pruned_stale_routes = writable_models_payload(self._category_payload("models"))
        node_routing = config_payload["node_routing"]
        provider_type = str(provider.get("provider_type") or provider.get("provider") or "openai_compatible")
        account_id = optional_text(provider.get("account_id"))
        api_mode = provider_route_api_mode(provider_id, provider)
        credential_mode = optional_text(provider.get("credential_mode"))
        # 逐个节点看它自己存着的路由:只重绑没配、配坏了、或服务 / 模型不就绪的节点,作者解析得了且就绪的路由一条不动
        synced_node_ids = nodes_needing_route(node_routing, providers)
        for node_id in synced_node_ids:
            node_routing[node_id] = default_task_config_payload(
                node_id,
                provider_id=provider_id,
                provider=provider_type,
                model=model,
                account_id=account_id,
                api_mode=api_mode,
                credential_mode=credential_mode,
            )

        routing_config = parse_route_config_or_raise(config_payload)
        activate = bool_value(payload.get("activate", False))
        if activate:
            validate_activating_node_route_bindings(
                node_routing=routing_config.node_routing,
                providers=providers,
            )
        snapshot = self._store_config_snapshot(
            category="models",
            parsed=config_payload,
            validation={"ok": True, "message": "models config is valid"},
            status="active" if activate else "draft",
            active=activate,
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {
            "snapshot": _serialize_snapshot(snapshot),
            "synced_node_ids": synced_node_ids,
            "pruned_stale_routes": pruned_stale_routes,
            "overview": self.llm_overview(),
        }

    def probe_llm_provider(self, *, provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        llm_payload = self._current_api_llm_payload()
        provider = self.llm_overview()["providers"].get(provider_id)
        if provider is None:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)
        probe_payload = dict(provider)
        probe_payload["timeout_seconds"] = probe_timeout_seconds(
            llm_payload.get("timeout_seconds"), default_seconds=PROVIDER_PROBE_TIMEOUT_SECONDS
        )
        probe_payload.update(payload or {})
        return self.test_provider(payload=probe_payload)

    def llm_provider_presets(self) -> dict[str, Any]:
        return {
            "presets": provider_preset_payloads(),
            "provider_catalog": provider_catalog(),
        }

    def list_llm_provider_models(self, *, provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        llm_payload = self._current_api_llm_payload()
        provider = self.llm_overview()["providers"].get(provider_id)
        if provider is None:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)

        provider_type = str(provider.get("provider_type") or provider.get("provider") or "openai_compatible")
        adapter = adapter_registry().get(provider_type)
        if adapter is None:
            raise DomainError("CONFIG_PROVIDER_UNSUPPORTED", f"unsupported provider {provider_type}", status_code=422)
        api_key = None
        if str(provider.get("credential_mode") or "api_key") == "api_key":
            api_key = load_secret_value(llm_provider_api_key_secret_id(provider_id)) or load_secret_value(LLM_API_KEY_SECRET_ID)
        target = ProbeTarget(
            provider=provider_type,
            base_url=normalize_provider_base_url(provider.get("base_url"), provider_type),
            api_key=api_key,
            provider_options=dict(provider.get("provider_options") or {}),
            api_mode=str(provider.get("api_mode") or "chat"),
            timeout_seconds=probe_timeout_seconds(
                payload.get("timeout_seconds") or llm_payload.get("timeout_seconds"), default_seconds=10.0
            ),
        )

        configured_models = normalize_provider_model_ids(provider.get("models") or [])
        preset = get_provider_preset(provider_type)
        preset_models = list(preset.common_models) if preset is not None else []
        fallback_models = list(dict.fromkeys([*configured_models, *preset_models]))

        def _fallback(message: str, *, status_code: int | None = None) -> dict[str, Any]:
            return {
                "provider_id": provider_id,
                "provider_type": provider_type,
                "ok": bool(fallback_models),
                "source": "preset",
                "available_models": fallback_models,
                "status_code": status_code,
                "message": message,
            }

        listing = fetch_model_listing(adapter, target)
        if not listing.supported:
            return _fallback("该服务不提供模型列表接口，已返回预设/已配置模型")
        if listing.response is None:
            return _fallback(f"模型列表拉取失败：{listing.error}")
        response = listing.response
        if not response.is_success:
            return _fallback(f"模型列表拉取失败：{provider_error_summary(response)}", status_code=response.status_code)

        model_ids = listed_model_ids(adapter, response)
        return {
            "provider_id": provider_id,
            "provider_type": provider_type,
            "ok": True,
            "source": "live",
            "available_models": model_ids,
            "status_code": response.status_code,
            "latency_ms": listing.latency_ms,
            "message": f"已从服务实时拉取 {len(model_ids)} 个模型",
        }

    def save_llm_role_routes(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        assignments = payload.get("assignments")
        if not isinstance(assignments, dict) or not assignments:
            raise DomainError("CONFIG_ROLE_ASSIGNMENTS_REQUIRED", "assignments must be a non-empty mapping", status_code=422)

        overview = self.llm_overview()
        providers = overview["providers"]

        # 同 sync-missing:退役节点的残留路由不随分工前滚,剪掉并回报。
        config_payload, pruned_stale_routes = writable_models_payload(self._category_payload("models"))
        node_routing = config_payload["node_routing"]

        applied: dict[str, dict[str, Any]] = {}
        for slot_id, binding in assignments.items():
            slot = get_role_slot_spec(str(slot_id))
            if slot is None:
                raise DomainError("CONFIG_ROLE_SLOT_UNKNOWN", f"unknown role slot {slot_id}", status_code=422)
            if not isinstance(binding, dict):
                raise DomainError("CONFIG_ROLE_ASSIGNMENT_INVALID", f"assignment for {slot_id} must be a mapping", status_code=422)
            provider_id = optional_text(binding.get("provider_id"))
            if not provider_id or provider_id not in providers:
                raise DomainError(
                    "CONFIG_PROVIDER_NOT_FOUND",
                    f"role slot {slot_id} references unknown provider {provider_id or '(missing)'}",
                    status_code=404 if provider_id else 422,
                )
            provider = providers[provider_id]
            if not provider_view_ready(provider):
                raise DomainError(
                    "CONFIG_ROUTE_PROVIDER_NOT_READY",
                    f"provider {provider_id} is not ready; enable it and configure credentials first",
                    status_code=422,
                )
            models = normalize_provider_model_ids(provider.get("models") or [])
            model = optional_text(binding.get("model")) or (models[0] if models else None)
            if not model:
                raise DomainError(
                    "CONFIG_ROUTE_MODEL_MISSING",
                    f"provider {provider_id} has no models; probe or fill the model list first",
                    status_code=422,
                )
            if models and model not in models:
                raise DomainError(
                    "CONFIG_ROUTE_MODEL_MISSING",
                    f"model {model} is not listed by provider {provider_id}",
                    status_code=422,
                )

            provider_type = str(provider.get("provider_type") or provider.get("provider") or "openai_compatible")
            account_id = optional_text(provider.get("account_id"))
            api_mode = provider_route_api_mode(provider_id, provider)
            credential_mode = optional_text(provider.get("credential_mode"))
            slot_node_ids = role_slot_node_ids(slot.slot_id)
            for node_id in slot_node_ids:
                node_routing[node_id] = default_task_config_payload(
                    node_id,
                    provider_id=provider_id,
                    provider=provider_type,
                    model=model,
                    account_id=account_id,
                    api_mode=api_mode,
                    credential_mode=credential_mode,
                )
            applied[slot.slot_id] = {
                "provider_id": provider_id,
                "model": model,
                "node_ids": slot_node_ids,
            }

        routing_config = parse_route_config_or_raise(config_payload)
        activate = bool_value(payload.get("activate", True))
        if activate:
            # 只校验本次触达的槽内节点:允许「先把写作主力分出去」的渐进配置,
            # 未触达节点保持原状(与今日的默认 repo 路由同等待遇)。
            touched_node_ids = {node_id for entry in applied.values() for node_id in entry["node_ids"]}
            validate_activating_node_route_bindings(
                node_routing={
                    node_id: task_config
                    for node_id, task_config in routing_config.node_routing.items()
                    if node_id in touched_node_ids
                },
                providers=providers,
            )
        snapshot = self._store_config_snapshot(
            category="models",
            parsed=config_payload,
            validation={"ok": True, "message": "models config is valid"},
            status="active" if activate else "draft",
            active=activate,
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {
            "snapshot": _serialize_snapshot(snapshot),
            "applied": applied,
            "pruned_stale_routes": pruned_stale_routes,
            "overview": self.llm_overview(),
        }

    def _category_payload(self, category: str) -> dict[str, Any]:
        active = active_snapshot(self.session, category)
        if active is not None:
            payload = {
                "category": category,
                "source": "database_active",
                "yaml_raw": active.yaml_raw,
                "parsed": active.parsed_json,
                "validation": active.validation_json,
                "active_snapshot": _serialize_snapshot(active),
            }
        else:
            yaml_raw, parsed, validation, source = default_config_payload(category)
            payload = {
                "category": category,
                "source": source,
                "yaml_raw": yaml_raw,
                "parsed": parsed,
                "validation": validation,
                "active_snapshot": None,
            }
        if category == "api":
            payload["secrets"] = {LLM_API_KEY_SECRET_ID: self._secret_status(LLM_API_KEY_SECRET_ID)}
        return payload

    def _current_api_llm_payload(self) -> dict[str, Any]:
        active = active_snapshot(self.session, "api")
        if active is not None:
            return coerce_api_payload(dict(active.parsed_json or {}))
        _, parsed, _, _ = default_config_payload("api")
        return coerce_api_payload(parsed)

    def _store_config_snapshot(
        self,
        *,
        category: str,
        parsed: dict[str, Any],
        validation: dict[str, Any],
        status: str,
        active: bool,
        actor_ref: str,
    ) -> SystemConfigSnapshot:
        if active:
            previous = active_snapshot(self.session, category)
            if previous is not None:
                previous.active_flag = 0
                previous.status = "superseded"
        yaml_raw = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
        snapshot = SystemConfigSnapshot(
            snapshot_id=f"config_{category}_{uuid.uuid4().hex[:12]}",
            category=category,
            version=self._next_version(category),
            yaml_raw=yaml_raw,
            parsed_json=parsed,
            validation_json=validation,
            status=status,
            active_flag=1 if active else 0,
            activated_at=utcnow() if active else None,
            created_by=actor_ref,
        )
        self.session.add(snapshot)
        return snapshot

    def _next_version(self, category: str) -> int:
        current = self.session.execute(
            select(func.max(SystemConfigSnapshot.version)).where(SystemConfigSnapshot.category == category)
        ).scalar_one_or_none()
        return int(current or 0) + 1

    def _save_secrets(self, *, category: str, secrets: dict[str, str], actor_ref: str) -> dict[str, Any]:
        if category != "api" or LLM_API_KEY_SECRET_ID not in secrets:
            return {LLM_API_KEY_SECRET_ID: self._secret_status(LLM_API_KEY_SECRET_ID)} if category == "api" else {}
        raw_value = str(secrets.get(LLM_API_KEY_SECRET_ID) or "").strip()
        if not raw_value:
            return {LLM_API_KEY_SECRET_ID: self._secret_status(LLM_API_KEY_SECRET_ID)}
        status = self._save_secret_value(
            secret_id=LLM_API_KEY_SECRET_ID,
            raw_value=raw_value,
            actor_ref=actor_ref,
            secret_type="api_key",
            metadata={"provider_id": "legacy", "provider_type": "openai_compatible"},
        )
        return {LLM_API_KEY_SECRET_ID: status}

    def _save_secret_value(
        self,
        *,
        secret_id: str,
        raw_value: str,
        actor_ref: str,
        secret_type: str,
        metadata: dict[str, Any],
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        return save_secret_value(
            self.session,
            secret_id=secret_id,
            raw_value=raw_value,
            actor_ref=actor_ref,
            secret_type=secret_type,
            metadata=metadata,
            expires_at=expires_at,
        )

    def _secret_status(self, secret_id: str, *, secret: SystemSecret | None = None) -> dict[str, Any]:
        return secret_status(self.session, secret_id, secret=secret)

    def _serialize_provider(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        credential_mode = str(payload.get("credential_mode") or "api_key")
        secret_id = llm_provider_api_key_secret_id(provider_id)
        secret_status = none_secret_status() if credential_mode == "none" else self._secret_status(secret_id)
        provider_type = payload.get("provider_type") or payload.get("provider")
        return {
            "provider_id": provider_id,
            "provider_type": provider_type,
            "account_id": payload.get("account_id"),
            "base_url": normalize_provider_base_url(payload.get("base_url"), provider_type),
            "enabled": bool_value(payload.get("enabled", True)),
            "credential_mode": credential_mode,
            "api_mode": payload.get("api_mode", "chat"),
            "models": normalize_provider_model_ids(payload.get("models") or []),
            "provider_options": dict(payload.get("provider_options") or {}),
            "secret": secret_status,
        }


def validate_config(category: str, yaml_raw: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        parsed = _parse_yaml_mapping(yaml_raw)
        if category == "api":
            normalized_api = {"llm": validate_api_config(parsed)}
            return normalized_api, {"ok": True, "message": "api config is valid"}
        if category == "models":
            parse_model_routing_config(parsed)
            return parsed, {"ok": True, "message": "models config is valid"}
        if category == "prompts":
            parse_prompt_templates(parsed)
            return parsed, {"ok": True, "message": "prompts config is valid"}
        if category in {"allowlists", "hash_contract"}:
            return parsed, {"ok": True, "message": f"{category} config is valid"}
    except (yaml.YAMLError, ValueError, LLMConfigurationError, PromptConfigurationError) as exc:
        return {}, {"ok": False, "message": str(exc)}

    raise DomainError("CONFIG_CATEGORY_UNSUPPORTED", f"unsupported config category {category}", status_code=404)


def default_config_payload(category: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    _ensure_category(category)
    if category == "api":
        settings = load_env_settings()
        parsed = {
            "llm": {
                "provider": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "enabled": settings.llm_enabled,
                "timeout_seconds": settings.llm_timeout_seconds,
            }
        }
        yaml_raw = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
        return yaml_raw, parsed, {"ok": True, "message": "api config is valid"}, "env_default"

    path = repo_config_dir() / YAML_CONFIG_FILES[category]
    yaml_raw = path.read_text(encoding="utf-8")
    parsed, validation = validate_config(category, yaml_raw)
    return yaml_raw, parsed, validation, "repo_default"


def require_admin_token(header_value: str | None, *, client_host: str | None = None) -> None:
    token = _admin_token()
    if token:
        # 常数时间比较，避免逐字符短路的时序侧信道（审计 P-16）
        if header_value is not None and hmac.compare_digest(
            header_value.encode("utf-8"), token.encode("utf-8")
        ):
            return
        raise DomainError("ADMIN_TOKEN_REQUIRED", "valid X-Admin-Token is required", status_code=403)
    if _is_loopback_client(client_host):
        return
    raise DomainError(
        "ADMIN_TOKEN_REQUIRED",
        "valid X-Admin-Token is required; local setup mode only accepts loopback requests",
        status_code=403,
    )


def _is_loopback_client(client_host: str | None) -> bool:
    return str(client_host or "").strip().lower() in {"127.0.0.1", "::1", "localhost"}


def _serialize_snapshot(snapshot: SystemConfigSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "category": snapshot.category,
        "version": snapshot.version,
        "yaml_raw": snapshot.yaml_raw,
        "parsed": snapshot.parsed_json,
        "validation": snapshot.validation_json,
        "status": snapshot.status,
        "active": bool(snapshot.active_flag),
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at,
        "activated_at": snapshot.activated_at,
    }


_SNAPSHOT_SUMMARY_KEYS = ("snapshot_id", "category", "version", "status", "active", "created_by", "created_at", "activated_at")


def _category_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """一类配置的摘要：去掉 YAML 正文与解析结果（活动快照也只留版本信息）。"""
    summary = {key: payload[key] for key in ("category", "source", "validation") if key in payload}
    active = payload.get("active_snapshot")
    summary["active_snapshot"] = (
        {key: active.get(key) for key in _SNAPSHOT_SUMMARY_KEYS} if isinstance(active, dict) else None
    )
    if "secrets" in payload:
        summary["secrets"] = payload["secrets"]
    return summary


def _ensure_category(category: str) -> None:
    if category not in CONFIG_CATEGORIES:
        raise DomainError("CONFIG_CATEGORY_UNSUPPORTED", f"unsupported config category {category}", status_code=404)


def _parse_yaml_mapping(yaml_raw: str) -> dict[str, Any]:
    # 作者在系统配置里提交的 YAML 用纯 Python 解析器校验：libyaml 比它宽（值里夹制表符、流式列表里的 ``?``、
    # ``|#`` 块头都放行，后者还会把映射悄悄读成字符串），这里要的是原来那套严格的接受范围与报错原文。
    # 只在保存 / 校验草稿时走，冷路径，慢一点无妨。
    payload = yaml.safe_load(yaml_raw) if yaml_raw.strip() else {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("config YAML must decode to a mapping")
    normalized = normalize(payload)
    if not isinstance(normalized, dict):
        raise ValueError("config YAML must normalize to a mapping")
    return normalized


def _admin_token() -> str | None:
    return env_admin_token()


def _config_secret() -> str | None:
    return env_config_secret()


