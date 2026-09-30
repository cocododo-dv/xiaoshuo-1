"""风格参考 · 模型节点的运行时与记账调用（段落分类 / 学习文风 / 对照检查三种作业共用）。

- :class:`NodeRuntime`：一个节点在一个作业里的路由与提示词模板——**每个作业载一次**，不在每次调用时重新解析
  （旧实现每一批重新解析一次路由与模板，实测 447 ms / 批）；
- :func:`load_node_runtimes`：路由（DB ``node_routing`` 优先、yaml 兜底）+ 模板，缺路由 / 缺模板 / 模板不合作业的
  解析契约的节点按节点顺序列在 :class:`NodeLoad` 里，**调用方按自己的错误码报**（分类、学习、对照检查的 409
  各说各的话）；配置本身读不出来抛 :class:`NodeConfigUnavailable`；
- :func:`accounted_call`：一次记账调用。记账用调用方给的会话，或自己开一个——并行的调用各用各的，作业会话里没提交
  的写不会被记账的 ``commit`` 顺手提交；记账 / 控制面失败原样抛出（不重试、不降级），其余失败交给调用方的
  ``wrap_error`` 换成它自己的错误。``execute`` 由各作业模块传入它自己模块里的 ``execute_accounted_call``
  （测试在那里打桩）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMCallContext,
    is_llm_control_plane_failure,
)
from novel_system.services.llm_client import load_model_routing_config, resolve_node_route
from novel_system.services.prompt_builder import load_prompt_templates

PROBLEM_ROUTE = "route"
PROBLEM_TEMPLATE = "template"
PROBLEM_STALE = "stale"


@dataclass(frozen=True)
class NodeRuntime:
    """一个节点在这个作业里的路由与模板（作业开始时载一次）。"""

    node_id: str
    route: Any
    template: Any

    @property
    def prompt_version(self) -> str:
        return str(getattr(self.template, "version", "") or "")

    @property
    def input_token_budget(self) -> int:
        return int(getattr(self.template, "input_token_budget", 0) or 0)

    @property
    def model(self) -> str:
        return str(getattr(self.route, "model", "") or "")

    @property
    def route_key(self) -> tuple[str, str]:
        """(provider_id 或 provider, model)：两个节点的这一对相同 = 同一个模型。"""
        provider = getattr(self.route, "provider_id", None) or getattr(self.route, "provider", None) or ""
        return (str(provider), self.model)


@dataclass(frozen=True)
class NodeLoad:
    """一组节点的载入结果：能用的运行时 + 按节点顺序的问题（``(node_id, route | template | stale)``）。"""

    runtimes: dict[str, NodeRuntime] = field(default_factory=dict)
    problems: tuple[tuple[str, str], ...] = ()

    def _ids(self, kind: str) -> list[str]:
        return [node_id for node_id, problem in self.problems if problem == kind]

    @property
    def missing_routes(self) -> list[str]:
        return self._ids(PROBLEM_ROUTE)

    @property
    def missing_templates(self) -> list[str]:
        return self._ids(PROBLEM_TEMPLATE)

    @property
    def stale_templates(self) -> list[str]:
        return self._ids(PROBLEM_STALE)


class NodeConfigUnavailable(Exception):
    """模型路由或提示词模板本身读不出来（``cause`` 是原来的异常）。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def load_node_runtimes(
    node_ids: Sequence[str],
    *,
    template_names: Mapping[str, str] | None = None,
    template_ok: Callable[[str, Any], bool] | None = None,
) -> NodeLoad:
    """载入这些节点的路由与模板。``template_names`` 给「节点用别的名字的模板」（对照检查走 soft_qc 的路由、用
    ``style_ref_check_judge`` 模板）；``template_ok(node_id, template)`` 为假 = 模板还是旧版本（``stale``）。
    缺路由的节点不再看模板。"""
    try:
        routing = load_model_routing_config()
        templates = load_prompt_templates()
    except Exception as exc:  # noqa: BLE001 — 读不到配置由调用方按缺配置报
        raise NodeConfigUnavailable(exc) from exc
    names = dict(template_names or {})
    runtimes: dict[str, NodeRuntime] = {}
    problems: list[tuple[str, str]] = []
    for node_id in node_ids:
        try:
            route = resolve_node_route(routing, node_id)
        except KeyError:
            problems.append((node_id, PROBLEM_ROUTE))
            continue
        template = templates.get(names.get(node_id, node_id))
        if template is None:
            problems.append((node_id, PROBLEM_TEMPLATE))
            continue
        if template_ok is not None and not template_ok(node_id, template):
            problems.append((node_id, PROBLEM_STALE))
            continue
        runtimes[node_id] = NodeRuntime(node_id=node_id, route=route, template=template)
    return NodeLoad(runtimes=runtimes, problems=tuple(problems))


def accounted_call(
    llm_client: Any,
    request: Any,
    *,
    context: LLMCallContext,
    llm_call_id: str,
    execute: Callable[..., Any],
    wrap_error: Callable[[Exception], Exception],
    session: Session | None = None,
) -> Any:
    """一次记账调用（见模块说明）；返回模型的回复。"""
    try:
        if session is not None:
            return execute(session, llm_client, request, context, llm_call_id=llm_call_id)
        from novel_system.db.session import SessionLocal

        with SessionLocal() as ledger_session:
            return execute(ledger_session, llm_client, request, context, llm_call_id=llm_call_id)
    except Exception as exc:  # noqa: BLE001 — 记账 / 控制面失败原样抛出，其余换成调用方的错误
        if isinstance(exc, LLMAccountingError) or is_llm_control_plane_failure(exc):
            raise
        raise wrap_error(exc) from exc


__all__ = [
    "NodeConfigUnavailable",
    "NodeLoad",
    "NodeRuntime",
    "PROBLEM_ROUTE",
    "PROBLEM_STALE",
    "PROBLEM_TEMPLATE",
    "accounted_call",
    "load_node_runtimes",
]
