"""风格参考 · 三种作业共用的模型节点载入与记账调用（``style_reference/llm_nodes.py``）。

- 载入按节点顺序列出问题（缺路由 / 缺模板 / 模板不合作业的解析契约），缺路由的节点不再看模板；配置本身读不出来
  抛 ``NodeConfigUnavailable``——各作业按自己的错误码报：分类报第一个问题、对照检查缺路由与缺模板说法不同；
- 记账调用：记账 / 控制面失败原样抛出，其余失败换成调用方的错误；给了会话就用它，没给就自己开一个。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingError, LLMCallContext
from novel_system.services.style_reference import check_job, llm_nodes
from novel_system.services.style_reference.segmentation import llm as seg


def _install_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    routes: dict[str, Any],
    templates: dict[str, Any],
) -> None:
    def resolve(_routing: Any, node_id: str) -> Any:
        if node_id not in routes:
            raise KeyError(node_id)
        return routes[node_id]

    monkeypatch.setattr(llm_nodes, "load_model_routing_config", lambda: object())
    monkeypatch.setattr(llm_nodes, "load_prompt_templates", lambda: dict(templates))
    monkeypatch.setattr(llm_nodes, "resolve_node_route", resolve)


def _route(model: str = "m", provider_id: str = "p") -> SimpleNamespace:
    return SimpleNamespace(provider_id=provider_id, provider="openai_compatible", model=model)


def _template(version: str = "2026-09-23.v1", budget: int = 8000) -> SimpleNamespace:
    return SimpleNamespace(version=version, input_token_budget=budget, structured_schema={})


def test_problems_are_listed_in_node_order_and_a_missing_route_skips_the_template(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        routes={"a": _route(), "c": _route(), "d": _route()},
        templates={"a": _template(), "b": _template(), "d": _template("old")},
    )
    load = llm_nodes.load_node_runtimes(
        ("a", "b", "c", "d"),
        template_ok=lambda _node, template: template.version != "old",
    )
    assert list(load.runtimes) == ["a"]
    assert load.problems == (
        ("b", llm_nodes.PROBLEM_ROUTE),
        ("c", llm_nodes.PROBLEM_TEMPLATE),
        ("d", llm_nodes.PROBLEM_STALE),
    )
    assert load.missing_routes == ["b"] and load.missing_templates == ["c"] and load.stale_templates == ["d"]
    runtime = load.runtimes["a"]
    assert runtime.prompt_version == "2026-09-23.v1" and runtime.input_token_budget == 8000
    assert runtime.model == "m" and runtime.route_key == ("p", "m")


def test_a_node_may_use_a_template_with_another_name(monkeypatch) -> None:
    judge = _template("judge")
    _install_config(monkeypatch, routes={"soft_qc": _route()}, templates={"style_ref_check_judge": judge})
    load = llm_nodes.load_node_runtimes(("soft_qc",), template_names={"soft_qc": "style_ref_check_judge"})
    assert load.problems == () and load.runtimes["soft_qc"].template is judge


def test_unreadable_config_raises_with_the_cause(monkeypatch) -> None:
    def boom() -> Any:
        raise RuntimeError("snapshot is corrupt")

    monkeypatch.setattr(llm_nodes, "load_model_routing_config", boom)
    with pytest.raises(llm_nodes.NodeConfigUnavailable) as excinfo:
        llm_nodes.load_node_runtimes(("a",))
    assert isinstance(excinfo.value.cause, RuntimeError)


def test_classification_reports_the_first_problem_with_its_own_codes(monkeypatch) -> None:
    anchor, bulk = seg.NODE_ANCHOR, seg.NODE_BULK
    _install_config(monkeypatch, routes={bulk: _route()}, templates={anchor: _template()})
    with pytest.raises(seg.SegmentationLLMError) as excinfo:
        seg.load_classification_runtimes()
    assert excinfo.value.code == "STYLE_REFERENCE_CLASSIFY_ROUTE_MISSING"
    assert excinfo.value.details == {"node_id": anchor}

    _install_config(monkeypatch, routes={anchor: _route(), bulk: _route()}, templates={anchor: _template()})
    with pytest.raises(seg.SegmentationLLMError) as excinfo:
        seg.load_classification_runtimes()
    assert excinfo.value.code == "STYLE_REFERENCE_CLASSIFY_PROMPT_MISSING"
    assert excinfo.value.details == {"node_id": bulk}

    monkeypatch.setattr(llm_nodes, "load_prompt_templates", lambda: (_ for _ in ()).throw(OSError("gone")))
    with pytest.raises(seg.SegmentationLLMError) as excinfo:
        seg.load_classification_runtimes()
    assert excinfo.value.code == "STYLE_REFERENCE_CLASSIFY_CONFIG_LOAD_FAILED"
    assert "gone" in excinfo.value.message


def test_check_judge_config_errors_say_which_part_is_missing(monkeypatch) -> None:
    _install_config(monkeypatch, routes={}, templates={check_job.CHECK_TEMPLATE: _template()})
    with pytest.raises(DomainError) as excinfo:
        check_job._judge_runtime()
    assert excinfo.value.code == check_job.CHECK_CONFIG_MISSING_CODE and excinfo.value.status_code == 409
    assert excinfo.value.details["reason"] == "route_missing"
    assert "author_action" not in excinfo.value.details

    _install_config(monkeypatch, routes={check_job.CHECK_NODE_ID: _route()}, templates={})
    with pytest.raises(DomainError) as excinfo:
        check_job._judge_runtime()
    assert excinfo.value.code == check_job.CHECK_CONFIG_MISSING_CODE
    assert excinfo.value.details["author_action"]["action"] == "sync_prompt_templates"

    judge = _template("judge")
    _install_config(
        monkeypatch, routes={check_job.CHECK_NODE_ID: _route()}, templates={check_job.CHECK_TEMPLATE: judge}
    )
    assert check_job._judge_runtime().template is judge


class _WrappedError(Exception):
    pass


def _context() -> LLMCallContext:
    return LLMCallContext(scope_type="style_reference_book", scope_id="book-1", node_id="n", step="s")


def test_accounted_call_uses_the_given_session_and_wraps_ordinary_failures() -> None:
    seen: list[Any] = []

    def execute(session: Any, client: Any, request: Any, context: Any, *, llm_call_id: str) -> Any:
        seen.append((session, client, request, context.scope_id, llm_call_id))
        return "reply"

    session = object()
    result = llm_nodes.accounted_call(
        "client", "request", context=_context(), llm_call_id="call-1", execute=execute,
        wrap_error=_WrappedError, session=session,
    )
    assert result == "reply" and seen == [(session, "client", "request", "book-1", "call-1")]

    def fails(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("provider said no")

    with pytest.raises(_WrappedError) as excinfo:
        llm_nodes.accounted_call(
            "client", "request", context=_context(), llm_call_id="call-2", execute=fails,
            wrap_error=lambda exc: _WrappedError(str(exc)), session=session,
        )
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_accounted_call_lets_accounting_failures_through_and_opens_its_own_ledger_session() -> None:
    sessions: list[Any] = []

    def accounting_fails(session: Any, *_args: Any, **_kwargs: Any) -> Any:
        sessions.append(session)
        raise LLMAccountingError("LLM_ACCOUNTING_FAILED", "ledger is gone")

    with pytest.raises(LLMAccountingError):
        llm_nodes.accounted_call(
            "client", "request", context=_context(), llm_call_id="call-3", execute=accounting_fails,
            wrap_error=_WrappedError,
        )
    # 没给会话：自己开一个记账会话（不是 None，也不借调用方的）
    assert len(sessions) == 1 and sessions[0] is not None
