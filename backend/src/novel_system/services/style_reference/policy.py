"""cloud_policy 执行单点(附录 B 数据安全契约;2026-09-23 风格参考 v3 口径)。

三档语义(以代码事实 ``CloudPolicy`` 枚举为准):

- ``local_only``       — **只有本机模型能看到这本书的正文**。任何要把正文(段落 / 引文 / 派生发现)
                         交给模型的操作,只在这一步**实际调用的节点路由**是本机模型(``ollama`` 适配器,
                         或 base_url 指向回环地址的任何 OpenAI 兼容服务)时放行;云端接入一律 409
                         ``STYLE_REFERENCE_CLOUD_POLICY_BLOCKED``(中文原因 + author_action)。
                         严格 LLM(2026-09-15):没有启发式兜底——配一个本机模型,或换策略重新导入。
- ``segments_only``    — 云端模型**可以**读正文来分类、学习(抽取 / 合成 / 标签 / 评审);但起草时只把
                         **文风卡**发给模型,不发原文样例窗(``binding_config.effective_reference_mode``
                         把这类书强制为 ``card_only``)。
- ``allow_full_cloud`` — 无限制:起草时也可以把原文样例窗发给云端模型。

非本地策略还必须有严格的 ``declared=True`` 与 ``send_rights=True`` 持久化声明;未知 / 空策略一律
fail-closed。

**「本机」按节点判断(v3 I7)**:分类、抽取、合成等节点在系统配置里各有路由(provider / base_url /
model),全局 provider 是本机不代表某个节点走本机。调用方传 ``node_ids``(或已经解析好的 ``routes``,
例如分类作业在开始时载入的那一份)时按这些节点的实际路由判断;不传时退回全局运行时模型(尚未迁移
的旧调用点)。需要错误反馈的调用方用 ``ensure_cloud_llm_allowed(book, operation=..., node_ids=...)``,
只需分支判定的用 ``cloud_llm_allowed(book, node_ids=...)``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from novel_system.services.style_reference.errors import (
    CloudPolicyBlockedError,
    CloudPolicyInvalidError,
    CloudSendRightsBlockedError,
)
from novel_system.services.style_reference.schemas import CloudPolicy


_CLOUD_POLICIES = {
    CloudPolicy.SEGMENTS_ONLY.value,
    CloudPolicy.ALLOW_FULL_CLOUD.value,
}

_LOCAL_PROVIDERS = {"ollama"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def _endpoint_is_local(provider_type: str, base_url: str) -> bool:
    if str(provider_type or "").strip().lower() in _LOCAL_PROVIDERS:
        return True
    try:
        host = (urlparse(str(base_url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def runtime_llm_is_local(settings: Any | None = None) -> bool:
    """全局运行时 LLM 是否是本机模型(没有传节点的旧调用点用;测试可打桩)。"""
    if settings is None:
        from novel_system.settings import get_settings

        settings = get_settings()
    return _endpoint_is_local(
        str(getattr(settings, "llm_provider", "") or ""),
        str(getattr(settings, "llm_base_url", "") or ""),
    )


def _runtime_llm_identity() -> tuple[str, str]:
    from novel_system.settings import get_settings

    settings = get_settings()
    return (
        str(getattr(settings, "llm_provider", "") or ""),
        str(getattr(settings, "llm_base_url", "") or ""),
    )


@dataclass(frozen=True)
class NodeEndpoint:
    """一个 LLM 节点的调用实际会去的地方(与 ``LLMClient._resolve_provider_config`` 同一解析顺序)。"""

    node_id: str
    provider_id: str | None
    provider_type: str
    base_url: str
    model: str

    @property
    def is_local(self) -> bool:
        return _endpoint_is_local(self.provider_type, self.base_url)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "provider_id": self.provider_id,
            "provider": self.provider_type,
            "base_url": self.base_url,
            "model": self.model,
            "local": self.is_local,
        }


def _provider_configs(llm_client: Any | None) -> Mapping[str, Any]:
    configs = getattr(llm_client, "_provider_configs", None) if llm_client is not None else None
    if isinstance(configs, Mapping) and configs:
        return configs
    try:
        from novel_system.services.system_config import load_llm_provider_runtime_configs

        return load_llm_provider_runtime_configs()
    except Exception:  # noqa: BLE001 — 读不到供应商配置时按路由本身 + 全局设置判断
        return {}


def resolve_node_endpoint(
    node_id: str,
    *,
    route: Any | None = None,
    llm_client: Any | None = None,
) -> NodeEndpoint:
    """解析一个节点的实际调用端点:节点路由(DB node_routing 优先、yaml 兜底)→ 路由指定的
    供应商配置(provider_id / provider)→ 找不到时退回全局 base_url(与客户端同一顺序)。"""
    if route is None:
        try:
            from novel_system.services.llm_client import load_model_routing_config, resolve_node_route

            route = resolve_node_route(load_model_routing_config(), node_id)
        except Exception:  # noqa: BLE001 — 路由缺失:按全局运行时模型判断
            route = None
    from novel_system.settings import get_settings

    settings = get_settings()
    provider_id = None
    model = ""
    route_provider = None
    if route is not None:
        provider_id = getattr(route, "provider_id", None) or getattr(route, "provider", None)
        route_provider = getattr(route, "provider", None)
        model = str(getattr(route, "model", "") or "")
    config = _provider_configs(llm_client).get(str(provider_id)) if provider_id else None
    if config is not None:
        return NodeEndpoint(
            node_id=node_id,
            provider_id=str(provider_id),
            provider_type=str(getattr(config, "provider_type", "") or route_provider or ""),
            base_url=str(getattr(config, "base_url", "") or ""),
            model=model,
        )
    return NodeEndpoint(
        node_id=node_id,
        provider_id=str(provider_id) if provider_id else None,
        provider_type=str(route_provider or getattr(settings, "llm_provider", "") or ""),
        base_url=str(getattr(settings, "llm_base_url", "") or ""),
        model=model,
    )


def node_route_is_local(
    node_id: str,
    *,
    route: Any | None = None,
    llm_client: Any | None = None,
) -> bool:
    """这个节点的调用是否落在本机模型上(测试可打桩)。"""
    return resolve_node_endpoint(node_id, route=route, llm_client=llm_client).is_local


def default_cloud_policy(node_ids: Sequence[str], *, llm_client: Any | None = None) -> str:
    """导入对话框的默认策略:这些节点(分类)全走本机模型时 ``local_only``,否则 ``allow_full_cloud``。

    旧前端默认 ``local_only``,在云端模型下导入必然 409——默认值要跟着当前模型走。
    """
    if node_ids and all(node_route_is_local(node_id, llm_client=llm_client) for node_id in node_ids):
        return CloudPolicy.LOCAL_ONLY.value
    return CloudPolicy.ALLOW_FULL_CLOUD.value


def _local_nodes_ok(
    node_ids: Sequence[str] | None,
    routes: Mapping[str, Any] | None,
    llm_client: Any | None,
) -> tuple[bool, NodeEndpoint | None]:
    """(全部是本机?, 第一个云端节点的端点)。不给节点时看全局运行时模型。"""
    wanted = list(node_ids or ()) or list((routes or {}).keys())
    if not wanted:
        return runtime_llm_is_local(), None
    for node_id in wanted:
        route = (routes or {}).get(node_id)
        if not node_route_is_local(node_id, route=route, llm_client=llm_client):
            return False, resolve_node_endpoint(node_id, route=route, llm_client=llm_client)
    return True, None


def cloud_llm_allowed(
    book: Any,
    *,
    node_ids: Sequence[str] | None = None,
    routes: Mapping[str, Any] | None = None,
    llm_client: Any | None = None,
) -> bool:
    """显式非本地策略 + 严格发送权声明允许云端 LLM;「仅本机」只允许(这些节点的)本机模型。"""
    if book is None:
        return True
    policy = getattr(book, "cloud_policy", None) or ""
    if policy == CloudPolicy.LOCAL_ONLY.value:
        return _local_nodes_ok(node_ids, routes, llm_client)[0]
    if policy not in _CLOUD_POLICIES:
        return False
    stats = getattr(book, "stats_json", None)
    rights = stats.get("rights_declaration") if isinstance(stats, dict) else None
    return (
        isinstance(rights, dict)
        and rights.get("declared") is True
        and rights.get("send_rights") is True
    )


def ensure_cloud_llm_allowed(
    book: Any,
    *,
    operation: str,
    node_ids: Sequence[str] | None = None,
    routes: Mapping[str, Any] | None = None,
    llm_client: Any | None = None,
) -> None:
    """按本地策略、未知策略、发送权缺失三类原因拒绝(中文原因 + author_action)。

    book 为 None 时放行(调用方各自负责 NOT_FOUND 校验,policy 层不重复)。
    """
    if book is None:
        return
    policy = getattr(book, "cloud_policy", None) or ""
    if policy == CloudPolicy.LOCAL_ONLY.value:
        ensure_local_only_llm(
            operation=operation,
            book_id=getattr(book, "book_id", "unknown"),
            node_ids=node_ids,
            routes=routes,
            llm_client=llm_client,
        )
        return
    if policy not in _CLOUD_POLICIES:
        raise CloudPolicyInvalidError(
            book_id=getattr(book, "book_id", "unknown"),
            operation=operation,
            cloud_policy=policy,
        )
    if not cloud_llm_allowed(book):
        raise CloudSendRightsBlockedError(
            book_id=getattr(book, "book_id", "unknown"),
            operation=operation,
            cloud_policy=policy,
        )


def ensure_local_only_llm(
    *,
    operation: str,
    book_id: str = "pending",
    node_ids: Sequence[str] | None = None,
    routes: Mapping[str, Any] | None = None,
    llm_client: Any | None = None,
) -> None:
    """「仅本机」书的 LLM 操作:这一步调用的节点必须全是本机模型,否则 409
    ``STYLE_REFERENCE_CLOUD_POLICY_BLOCKED``(不给节点时检查全局运行时模型)。"""
    ok, endpoint = _local_nodes_ok(node_ids, routes, llm_client)
    if ok:
        return
    if endpoint is None:
        provider, base_url = _runtime_llm_identity()
        raise CloudPolicyBlockedError(
            book_id=book_id, operation=operation, provider=provider, base_url=base_url
        )
    raise CloudPolicyBlockedError(
        book_id=book_id,
        operation=operation,
        provider=endpoint.provider_type,
        base_url=endpoint.base_url,
        node_id=endpoint.node_id,
        model=endpoint.model,
    )


__all__ = [
    "NodeEndpoint",
    "cloud_llm_allowed",
    "default_cloud_policy",
    "ensure_cloud_llm_allowed",
    "ensure_local_only_llm",
    "node_route_is_local",
    "resolve_node_endpoint",
    "runtime_llm_is_local",
]
