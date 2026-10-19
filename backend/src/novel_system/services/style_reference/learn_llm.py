"""风格参考 v3 —「学习文风」作业的模型调用零件（叶子模块）。

学习作业一共用 7 个节点：4 个分层抽取（沿用 ``style_ref_extract_<layer>`` 的节点 id，作者在系统配置里给它们
配的路由照旧生效）、文风卡合成（``style_ref_synthesize_profile``）、受保护专名（``style_ref_protected_terms``）、
窗口标签（``style_ref_tag_windows``）。通读全书的归并（模板 ``style_ref_consolidate_findings``）走文风卡合成节点的
路由（:func:`load_consolidate_runtime`），不另设节点。

- 路由与提示词模板**每个作业载一次**（``load_learn_runtimes``，经 ``llm_nodes.load_node_runtimes``），不在每次
  调用时重新解析；
- 一次调用 = 一次记账调用（``llm_nodes.accounted_call`` → 本模块的 ``execute_accounted_call``），记账用**自己的
  会话**：并行的调用各用各的，作业的会话里没提交的写也不会被记账的 ``commit`` 顺手提交；
- 载荷放在唯一的不可信数据边界里（``render_untrusted_user_prompt``）；重试时的修正说明是我们自己的话，接在
  任务文本后面（边界之外）；
- 输入预算真执行：``estimate_input_tokens`` 与发送时同一种渲染，调用方据此在发送前缩载荷（不再有写在模板里却
  从不执行的预算）。

记账 / 控制面失败原样抛出（不重试、不降级）；其余调用失败是 ``LearnCallError``，由调用方决定是否重试。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novel_system.services.context_budget import estimate_tokens
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMCallContext, execute_accounted_call
from novel_system.services.llm_client import build_llm_request
from novel_system.services.style_reference.llm_nodes import (
    NodeConfigUnavailable,
    NodeRuntime,
    accounted_call,
    load_node_runtimes,
)
from novel_system.services.style_reference.untrusted_data import (
    UntrustedPayload,
    render_untrusted_system_prompt,
    render_untrusted_user_prompt,
)

LAYERS: tuple[str, ...] = ("language", "narrative", "scene", "theme")
EXTRACT_NODES: dict[str, str] = {layer: f"style_ref_extract_{layer}" for layer in LAYERS}
NODE_SYNTHESIZE = "style_ref_synthesize_profile"
NODE_PROTECTED_TERMS = "style_ref_protected_terms"
NODE_TAG_WINDOWS = "style_ref_tag_windows"
LEARN_NODE_IDS: tuple[str, ...] = (
    *EXTRACT_NODES.values(),
    NODE_SYNTHESIZE,
    NODE_PROTECTED_TERMS,
    NODE_TAG_WINDOWS,
)

# 通读全书的归并模板：在文风卡合成节点的路由上跑
CONSOLIDATE_TEMPLATE = "style_ref_consolidate_findings"

LEARN_CONFIG_MISSING_CODE = "STYLE_REFERENCE_LEARN_CONFIG_MISSING"
LEARN_CALL_FAILED_CODE = "STYLE_REFERENCE_LEARN_LLM_CALL_FAILED"

# 预算估计的安全余量（与旧合成同一口径：CJK 一字一 token 的上界 ×1.1 + schema 文本）
_ESTIMATE_MULTIPLIER = 1.1


class LearnCallError(Exception):
    """一次学习调用失败（供应商报错、渲染失败、输出为空）——调用方可以重试。"""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class LearnCallResult:
    structured: dict[str, Any]
    llm_call_id: str
    usage: dict[str, Any]


def load_learn_runtimes(
    node_ids: Sequence[str] = LEARN_NODE_IDS,
    *,
    template_names: Mapping[str, str] | None = None,
) -> dict[str, NodeRuntime]:
    """载入学习节点的路由（DB node_routing 优先、yaml 兜底）与提示词模板；缺哪个都报 409（中文 + 去系统配置）。

    模板的输出字段与作业的解析代码随同一次发布到达（提示词只读仓库的 config/prompts.yaml），两者对得上由
    ``tests/test_style_reference_learn_units.py`` 的模板契约守着，开工时不再逐个查。"""
    try:
        load = load_node_runtimes(node_ids, template_names=template_names)
    except NodeConfigUnavailable as exc:
        raise DomainError(
            LEARN_CONFIG_MISSING_CODE,
            f"读不到模型路由或提示词模板:{exc.cause}",
            status_code=409,
            details={"reason": "config_load_failed"},
        ) from exc.cause
    missing_routes = load.missing_routes
    missing_templates = load.missing_templates
    if missing_routes or missing_templates:
        parts: list[str] = []
        if missing_routes:
            parts.append("没有路由的节点:" + "、".join(missing_routes) + "(到「设置 → 模型与接入」一键补齐)")
        if missing_templates:
            parts.append("提示词模板缺失:" + "、".join(missing_templates))
        raise DomainError(
            LEARN_CONFIG_MISSING_CODE,
            "学习文风要用的模型节点还没配好。" + ";".join(parts) + "。",
            status_code=409,
            details={
                "missing_routes": missing_routes,
                "missing_templates": missing_templates,
                "author_action": {
                    "action": "configure_llm_nodes",
                    "view": "systemConfig",
                    "label": "前往系统配置补齐节点",
                },
            },
        )
    return dict(load.runtimes)


def load_consolidate_runtime() -> NodeRuntime:
    """通读全书的归并：文风卡合成节点的路由 + 归并模板（缺路由 / 模板同样 409 ``…_LEARN_CONFIG_MISSING``）。"""
    return load_learn_runtimes((NODE_SYNTHESIZE,), template_names={NODE_SYNTHESIZE: CONSOLIDATE_TEMPLATE})[NODE_SYNTHESIZE]


def _task_text(runtime: NodeRuntime, extra_instruction: str | None) -> str:
    task = str(getattr(runtime.template, "task_prompt", "") or "")
    if extra_instruction and extra_instruction.strip():
        task = task.rstrip() + "\n\n" + extra_instruction.strip()
    return task


def render_messages(
    runtime: NodeRuntime,
    payload: Mapping[str, Any],
    *,
    extra_instruction: str | None = None,
) -> list[dict[str, str]]:
    """system + user 两条消息（载荷在不可信数据边界里；``extra_instruction`` 在边界之外）。"""
    system_prompt = render_untrusted_system_prompt(str(runtime.template.system_prompt or ""))
    user_prompt = render_untrusted_user_prompt(
        _task_text(runtime, extra_instruction),
        UntrustedPayload(dict(payload)),
        kind=runtime.node_id,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def estimate_input_tokens(
    runtime: NodeRuntime,
    payload: Mapping[str, Any],
    *,
    extra_instruction: str | None = None,
) -> int:
    """与发送时同一种渲染的输入 token 估计（CJK 一字一 token 的上界 + schema 文本）。"""
    messages = render_messages(runtime, payload, extra_instruction=extra_instruction)
    schema_text = json.dumps(
        getattr(runtime.template, "structured_schema", {}) or {}, ensure_ascii=False, separators=(",", ":")
    )
    total = sum(estimate_tokens(message["content"]) for message in messages) + estimate_tokens(schema_text)
    return int(total * _ESTIMATE_MULTIPLIER) + 1


def payload_fits(runtime: NodeRuntime, payload: Mapping[str, Any], *, extra_instruction: str | None = None) -> bool:
    budget = runtime.input_token_budget
    return budget <= 0 or estimate_input_tokens(runtime, payload, extra_instruction=extra_instruction) <= budget


def call_structured(
    runtime: NodeRuntime,
    payload: Mapping[str, Any],
    llm_client: Any,
    *,
    scope_id: str,
    step: str,
    extra_instruction: str | None = None,
) -> LearnCallResult:
    """一次记账调用（记账用自己的会话），返回结构化输出。

    记账 / 控制面失败原样抛出；供应商调用失败或输出不是对象 → ``LearnCallError``（可重试）。
    可以在工人线程里调用：不碰调用方的会话。
    """
    try:
        messages = render_messages(runtime, payload, extra_instruction=extra_instruction)
    except Exception as exc:  # noqa: BLE001 — 渲染失败按调用失败报
        raise LearnCallError(
            LEARN_CALL_FAILED_CODE,
            f"failed to render the prompt for node {runtime.node_id!r}",
            details={"node_id": runtime.node_id, "reason": "render_failed"},
        ) from exc
    request = build_llm_request(
        runtime.route,
        node_id=runtime.node_id,
        messages=messages,
        response_schema=getattr(runtime.template, "structured_schema", None),
    )
    llm_call_id = f"llm_style_learn_{uuid.uuid4().hex}"
    response = accounted_call(
        llm_client,
        request,
        context=LLMCallContext(
            scope_type="style_reference_book",
            scope_id=scope_id,
            node_id=runtime.node_id,
            step=step,
        ),
        llm_call_id=llm_call_id,
        execute=execute_accounted_call,
        wrap_error=lambda exc: LearnCallError(
            LEARN_CALL_FAILED_CODE,
            f"accounted LLM execution failed for node {runtime.node_id!r}: {exc}",
            details={"node_id": runtime.node_id, "error_type": type(exc).__name__},
        ),
    )
    structured = getattr(response, "structured_output", None)
    if not isinstance(structured, Mapping):
        raise LearnCallError(
            LEARN_CALL_FAILED_CODE,
            f"node {runtime.node_id!r} returned no structured object",
            details={"node_id": runtime.node_id, "reason": "no_structured_output"},
        )
    usage = getattr(response, "usage", None)
    return LearnCallResult(
        structured=dict(structured),
        llm_call_id=str(getattr(response, "llm_call_id", None) or llm_call_id),
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


__all__ = [
    "CONSOLIDATE_TEMPLATE",
    "EXTRACT_NODES",
    "LAYERS",
    "LEARN_CALL_FAILED_CODE",
    "LEARN_CONFIG_MISSING_CODE",
    "LEARN_NODE_IDS",
    "LearnCallError",
    "LearnCallResult",
    "NODE_PROTECTED_TERMS",
    "NODE_SYNTHESIZE",
    "NODE_TAG_WINDOWS",
    "call_structured",
    "estimate_input_tokens",
    "load_consolidate_runtime",
    "load_learn_runtimes",
    "payload_fits",
    "render_messages",
]
