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

**参考进提示(起草 / 改稿 / 评审 / 规划)同样按接收提示的节点判断**::func:`decide_reference_route` 是
注入渲染、规划参考块与本场预览共用的唯一判定——

- ``local_only``:接收提示的节点(可能是几个候选节点,要全部)都走本机模型 → 样例、文风卡、声音、红线照送;
  任一节点走云端、或说不出是哪个节点 → **一个字都不送**(没有样例、没有文风卡、没有声音、没有专名表),
  判定带 409 ``STYLE_REFERENCE_CLOUD_POLICY_BLOCKED``(注入适配器原样抛出,不降级成无参考的提示);
- ``segments_only`` / ``allow_full_cloud``:与节点无关;有严格发送权声明(且冻结时也允许云端)才送原文样例,
  否则只送文风卡与红线(``segments_only`` 由 ``binding_config.effective_reference_mode`` 压成只用文风卡);
- 未知 / 空策略:本机节点只送文风卡,云端节点 409 ``STYLE_REFERENCE_CLOUD_POLICY_INVALID``;
- 书已删除:不送原文;冻结快照里的策略决定文风卡还能不能送(说不清策略时什么都不送)。

契约冻结的 ``cloud_llm_allowed_at_freeze`` 是**与路由无关**的策略口径(:func:`book_allows_cloud`:非本地策略 +
严格发送权声明)。「仅本机」的书恒为 False,这个闩对它不起作用(本机节点照样能用它);对送云策略的书它是闩:
冻结时不许送云,这份契约之后也不送原文。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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


def _rights_declared(book: Any) -> bool:
    stats = getattr(book, "stats_json", None)
    rights = stats.get("rights_declaration") if isinstance(stats, Mapping) else None
    return (
        isinstance(rights, Mapping)
        and rights.get("declared") is True
        and rights.get("send_rights") is True
    )


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
    return _rights_declared(book)


def book_allows_cloud(book: Any) -> bool:
    """与节点路由无关的策略口径:这本书的策略本身允许云端模型读它(非本地策略 + 严格发送权声明)。

    「仅本机」、未知策略、没有书 → False。运行时契约冻结的 ``cloud_llm_allowed_at_freeze`` 用它——
    冻结时不看全局运行时模型(它说明不了哪个节点会收到提示),节点路由在每一次渲染时再判。
    """
    if book is None:
        return False
    policy = getattr(book, "cloud_policy", None) or ""
    return policy in _CLOUD_POLICIES and _rights_declared(book)


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


# ---------------------------------------------------------------------------
# 参考进提示:按接收提示的节点判(注入渲染 / 规划参考块 / 本场预览共用)
# ---------------------------------------------------------------------------

REASON_BOOK_MISSING = "book_missing"
REASON_CLOUD_POLICY_NOW = "cloud_policy_now"
REASON_CLOUD_POLICY_AT_FREEZE = "cloud_policy_at_freeze"
REASON_CLOUD_POLICY_INVALID = "cloud_policy_invalid"
REASON_LOCAL_ONLY_ROUTE = "local_only_route_not_local"
REASON_NODE_UNKNOWN = "node_unknown"

# 参考渲染里「判定为不许送、整份参考都不发」的错误:注入适配器必须原样抛出,不能降级成一份没有参考的提示
# (那等于悄悄换了一份提示去调用同一个云端节点,还让人以为参考只是「渲染失败」)。
STYLE_REFERENCE_FAIL_CLOSED_ERRORS: tuple[type[Exception], ...] = (
    CloudPolicyBlockedError,
    CloudPolicyInvalidError,
)


def normalize_node_ids(node_ids: Sequence[str] | str | None) -> tuple[str, ...]:
    """节点 id 规范化:去空白、去重、保序;单个字符串当一个节点。"""
    if node_ids is None:
        return ()
    items = [node_ids] if isinstance(node_ids, str) else list(node_ids)
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class ReferenceRouteDecision:
    """一次「参考能不能进这份提示」的判定(不含正文)。

    - ``send_book``:能不能送任何由这本书派生的东西(文风卡、声音、禁用词 / 专名表、结构画像……);
    - ``send_samples``:能不能送原文(样例窗、章首章尾样例、卡句例子、章题样例);
    - ``error``:判定为不许送、而且要让调用方失败时带的 409(``local_only`` 遇云端 / 说不清的节点,未知策略遇云端)。
    """

    node_ids: tuple[str, ...]
    # 接收提示的节点是否全走本机模型;``None`` = 没有判(送云策略的书与节点无关,不去解析路由)
    route_local: bool | None
    cloud_policy: str
    send_book: bool
    send_samples: bool
    reason: str | None = None
    error: Exception | None = field(default=None, compare=False, repr=False)

    @property
    def blocked(self) -> bool:
        return self.error is not None

    def raise_if_blocked(self) -> None:
        if self.error is not None:
            raise self.error

    @property
    def cache_token(self) -> str:
        """渲染缓存键的一段:接收提示的节点、路由是否本机、送什么。同一场换了节点路由不会拿到旧渲染。"""
        locality = "n/a" if self.route_local is None else ("local" if self.route_local else "remote")
        return "|".join(
            [
                ",".join(self.node_ids),
                locality,
                self.cloud_policy,
                "book" if self.send_book else "nobook",
                "samples" if self.send_samples else "nosamples",
            ]
        )

    def audit(self) -> dict[str, Any]:
        return {
            "node_ids": list(self.node_ids),
            "route_local": self.route_local,
            "cloud_policy": self.cloud_policy,
            "send_book": self.send_book,
            "send_samples": self.send_samples,
            "reason": self.reason,
            "error_code": getattr(self.error, "code", None),
        }


def _route_locality(nodes: Sequence[str], llm_client: Any | None) -> tuple[bool, NodeEndpoint | None]:
    """(全部节点都走本机?, 第一个云端节点的端点)。说不出节点 → (False, None)——不能证明是本机。"""
    if not nodes:
        return False, None
    for node_id in nodes:
        if node_route_is_local(node_id, llm_client=llm_client):
            continue
        try:
            endpoint = resolve_node_endpoint(node_id, llm_client=llm_client)
        except Exception:  # noqa: BLE001 — 端点细节只用来写错误信息
            endpoint = NodeEndpoint(node_id=node_id, provider_id=None, provider_type="", base_url="", model="")
        return False, endpoint
    return True, None


def _local_only_route_error(
    *,
    book_id: str,
    operation: str,
    nodes: Sequence[str],
    endpoint: NodeEndpoint | None,
) -> CloudPolicyBlockedError:
    if endpoint is not None:
        error = CloudPolicyBlockedError(
            book_id=book_id,
            operation=operation,
            provider=endpoint.provider_type or None,
            base_url=endpoint.base_url or None,
            node_id=endpoint.node_id,
            model=endpoint.model or None,
        )
        error.details["reason"] = REASON_LOCAL_ONLY_ROUTE
        error.details["node_ids"] = list(nodes)
        return error
    error = CloudPolicyBlockedError(book_id=book_id, operation=operation)
    message = (
        "这本参考书设为「仅本机」:只有本机模型能读它的正文,但这一步说不出由哪个模型节点接收提示,"
        "无法确认是本机模型,参考一个字都没有送。请在「设置 → 模型与接入」确认相关节点走本机模型(如 Ollama),"
        "或改用送云策略重新导入。"
    )
    error.message = message
    error.args = (message,)
    error.details["reason"] = REASON_NODE_UNKNOWN
    error.details["node_ids"] = []
    return error


def decide_reference_route(
    book: Any,
    *,
    node_ids: Sequence[str] | str | None,
    frozen_book: Mapping[str, Any] | None = None,
    book_missing: bool | None = None,
    operation: str = "style_reference_injection",
    llm_client: Any | None = None,
) -> ReferenceRouteDecision:
    """参考(样例 / 文风卡 / 声音 / 红线 / 结构画像)能不能进**这些节点**要收的提示。

    ``book``:参考书的当前行(``None`` = 书已删除,或调用方手里没有会话——后者传 ``book_missing=False``,
    只按冻结快照判);``frozen_book``:契约冻结的书快照(``cloud_policy`` / ``cloud_llm_allowed_at_freeze``);
    ``node_ids``:接收这份提示的节点(模板可能按几个节点的路由派发时全部列上,要求每一个都满足)。
    """
    nodes = normalize_node_ids(node_ids)
    frozen = frozen_book if isinstance(frozen_book, Mapping) else {}
    live = book is not None
    missing = (not live) if book_missing is None else bool(book_missing)
    policy = str((getattr(book, "cloud_policy", None) if live else frozen.get("cloud_policy")) or "")
    book_id = str((getattr(book, "book_id", None) if live else None) or frozen.get("book_id") or "unknown")
    # 路由只在它决定结果时才解析(「仅本机」与未知策略);送云策略的书与节点无关,不为每次渲染去读路由与供应商配置
    route_local: bool | None = None
    endpoint: NodeEndpoint | None = None
    if policy not in _CLOUD_POLICIES:
        route_local, endpoint = _route_locality(nodes, llm_client)

    def _decision(send_book: bool, send_samples: bool, reason: str | None, error: Exception | None = None):
        return ReferenceRouteDecision(
            node_ids=nodes,
            route_local=route_local,
            cloud_policy=policy,
            send_book=send_book,
            send_samples=send_samples,
            reason=reason,
            error=error,
        )

    if policy == CloudPolicy.LOCAL_ONLY.value:
        if route_local:
            return _decision(True, not missing, REASON_BOOK_MISSING if missing else None)
        error = _local_only_route_error(book_id=book_id, operation=operation, nodes=nodes, endpoint=endpoint)
        return _decision(False, False, REASON_LOCAL_ONLY_ROUTE if nodes else REASON_NODE_UNKNOWN, error)
    if policy in _CLOUD_POLICIES:
        if missing:
            return _decision(True, False, REASON_BOOK_MISSING)
        if live and not _rights_declared(book):
            return _decision(True, False, REASON_CLOUD_POLICY_NOW)
        if frozen.get("cloud_llm_allowed_at_freeze") is False:
            return _decision(True, False, REASON_CLOUD_POLICY_AT_FREEZE)
        return _decision(True, True, None)
    # 未知 / 空策略:fail-closed。书都不在了(旧契约的快照里又没有策略)→ 什么都不送,也不报错(书是作者删的)
    if missing:
        return _decision(False, False, REASON_BOOK_MISSING)
    if route_local:
        return _decision(True, False, REASON_CLOUD_POLICY_INVALID)
    return _decision(
        False,
        False,
        REASON_CLOUD_POLICY_INVALID,
        CloudPolicyInvalidError(book_id=book_id, operation=operation, cloud_policy=policy),
    )


__all__ = [
    "NodeEndpoint",
    "REASON_BOOK_MISSING",
    "REASON_CLOUD_POLICY_AT_FREEZE",
    "REASON_CLOUD_POLICY_INVALID",
    "REASON_CLOUD_POLICY_NOW",
    "REASON_LOCAL_ONLY_ROUTE",
    "REASON_NODE_UNKNOWN",
    "ReferenceRouteDecision",
    "STYLE_REFERENCE_FAIL_CLOSED_ERRORS",
    "book_allows_cloud",
    "cloud_llm_allowed",
    "decide_reference_route",
    "default_cloud_policy",
    "ensure_cloud_llm_allowed",
    "ensure_local_only_llm",
    "node_route_is_local",
    "normalize_node_ids",
    "resolve_node_endpoint",
    "runtime_llm_is_local",
]
