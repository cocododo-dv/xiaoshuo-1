"""风格参考 v3 —「学习文风」作业的模型调用零件（叶子模块）。

学习作业一共用 7 个节点：4 个分层抽取（沿用 ``style_ref_extract_<layer>`` 的节点 id，作者在系统配置里给它们
配的路由照旧生效）、文风卡合成（``style_ref_synthesize_profile``）、受保护专名（``style_ref_protected_terms``）、
窗口标签（``style_ref_tag_windows``）。

- 路由与提示词模板**每个作业载一次**（``load_learn_runtimes``），不在每次调用时重新解析；
- 一次调用 = 一次记账调用（``execute_accounted_call``），记账用**自己的会话**：并行的调用各用各的，作业的会话
  里没提交的写也不会被记账的 ``commit`` 顺手提交；
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
from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMCallContext,
    execute_accounted_call,
    is_llm_control_plane_failure,
)
from novel_system.services.llm_client import (
    build_llm_request,
    load_model_routing_config,
    resolve_node_route,
)
from novel_system.services.prompt_builder import load_prompt_templates
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

LEARN_CONFIG_MISSING_CODE = "STYLE_REFERENCE_LEARN_CONFIG_MISSING"
LEARN_CALL_FAILED_CODE = "STYLE_REFERENCE_LEARN_LLM_CALL_FAILED"

# 预算估计的安全余量（与旧合成同一口径：CJK 一字一 token 的上界 ×1.1 + schema 文本）
_ESTIMATE_MULTIPLIER = 1.1

# 模板契约：作业按这些 schema 字段解析输出。抽取 / 合成沿用旧节点 id，保存过提示词快照的安装里拿到的可能还是
# 旧模板（旧抽取 schema 是顶层 observations / forbidden_patterns）——发出去注定解析失败、白花调用，所以开工前查。
# 路径里遇到数组就进 items。
TEMPLATE_CONTRACT: dict[str, tuple[tuple[str, ...], ...]] = {
    **{node: (("dimensions", "observations"), ("dimensions", "avoid")) for node in EXTRACT_NODES.values()},
    NODE_SYNTHESIZE: (("dimensions", "do"), ("dimensions", "avoid"), ("temperament",)),
    NODE_PROTECTED_TERMS: (("terms",),),
    NODE_TAG_WINDOWS: (("windows", "situations"), ("windows", "gist")),
}


class LearnCallError(Exception):
    """一次学习调用失败（供应商报错、渲染失败、输出为空）——调用方可以重试。"""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class LearnNodeRuntime:
    """一个学习节点在这个作业里的路由与模板（作业开始时载一次）。"""

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


@dataclass(frozen=True)
class LearnCallResult:
    structured: dict[str, Any]
    llm_call_id: str
    usage: dict[str, Any]


def load_learn_runtimes(node_ids: Sequence[str] = LEARN_NODE_IDS) -> dict[str, LearnNodeRuntime]:
    """载入学习节点的路由（DB node_routing 优先、yaml 兜底）与提示词模板；缺哪个都报 409（中文 + 去系统配置）。"""
    try:
        routing = load_model_routing_config()
        templates = load_prompt_templates()
    except Exception as exc:  # noqa: BLE001 — 配置读不到按缺配置报
        raise DomainError(
            LEARN_CONFIG_MISSING_CODE,
            f"读不到模型路由或提示词模板:{exc}",
            status_code=409,
            details={"reason": "config_load_failed"},
        ) from exc
    runtimes: dict[str, LearnNodeRuntime] = {}
    missing_routes: list[str] = []
    missing_templates: list[str] = []
    stale_templates: list[str] = []
    for node_id in node_ids:
        try:
            route = resolve_node_route(routing, node_id)
        except KeyError:
            missing_routes.append(node_id)
            continue
        template = templates.get(node_id)
        if template is None:
            missing_templates.append(node_id)
            continue
        if not template_meets_contract(node_id, template):
            stale_templates.append(node_id)
            continue
        runtimes[node_id] = LearnNodeRuntime(node_id=node_id, route=route, template=template)
    if missing_routes or missing_templates or stale_templates:
        parts: list[str] = []
        if missing_routes:
            parts.append("没有路由的节点:" + "、".join(missing_routes) + "(到「设置 → 模型与接入」一键补齐)")
        if missing_templates or stale_templates:
            parts.append(
                "提示词模板缺失或还是旧版本:"
                + "、".join(missing_templates + stale_templates)
                + "(保存过提示词快照的安装要同步提示词模板:sync_prompt_templates --execute)"
            )
        raise DomainError(
            LEARN_CONFIG_MISSING_CODE,
            "学习文风要用的模型节点还没配好。" + ";".join(parts) + "。",
            status_code=409,
            details={
                "missing_routes": missing_routes,
                "missing_templates": missing_templates,
                "stale_templates": stale_templates,
                "author_action": {
                    "action": "configure_llm_nodes",
                    "view": "systemConfig",
                    "label": "前往系统配置补齐节点",
                },
            },
        )
    return runtimes


def _schema_path_present(schema: Any, path: Sequence[str]) -> bool:
    node: Any = schema
    for key in path:
        if isinstance(node, Mapping) and node.get("type") == "array":
            node = node.get("items")
        properties = node.get("properties") if isinstance(node, Mapping) else None
        if not isinstance(properties, Mapping) or key not in properties:
            return False
        node = properties[key]
    return True


def template_meets_contract(node_id: str, template: Any) -> bool:
    """模板的 structured_schema 带着作业解析要用的字段（``TEMPLATE_CONTRACT``）；不在契约里的节点不查。"""
    schema = getattr(template, "structured_schema", None)
    return all(_schema_path_present(schema, path) for path in TEMPLATE_CONTRACT.get(node_id, ()))


def _task_text(runtime: LearnNodeRuntime, extra_instruction: str | None) -> str:
    task = str(getattr(runtime.template, "task_prompt", "") or "")
    if extra_instruction and extra_instruction.strip():
        task = task.rstrip() + "\n\n" + extra_instruction.strip()
    return task


def render_messages(
    runtime: LearnNodeRuntime,
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
    runtime: LearnNodeRuntime,
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


def payload_fits(runtime: LearnNodeRuntime, payload: Mapping[str, Any], *, extra_instruction: str | None = None) -> bool:
    budget = runtime.input_token_budget
    return budget <= 0 or estimate_input_tokens(runtime, payload, extra_instruction=extra_instruction) <= budget


def call_structured(
    runtime: LearnNodeRuntime,
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
    from novel_system.db.session import SessionLocal

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
    try:
        with SessionLocal() as ledger_session:
            response = execute_accounted_call(
                ledger_session,
                llm_client,
                request,
                LLMCallContext(
                    scope_type="style_reference_book",
                    scope_id=scope_id,
                    node_id=runtime.node_id,
                    step=step,
                ),
                llm_call_id=llm_call_id,
            )
    except Exception as exc:  # noqa: BLE001 — 记账 / 控制面失败原样抛出,其余按可重试的调用失败报
        if isinstance(exc, LLMAccountingError) or is_llm_control_plane_failure(exc):
            raise
        raise LearnCallError(
            LEARN_CALL_FAILED_CODE,
            f"accounted LLM execution failed for node {runtime.node_id!r}: {exc}",
            details={"node_id": runtime.node_id, "error_type": type(exc).__name__},
        ) from exc
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
    "EXTRACT_NODES",
    "LAYERS",
    "LEARN_CALL_FAILED_CODE",
    "LEARN_CONFIG_MISSING_CODE",
    "LEARN_NODE_IDS",
    "LearnCallError",
    "LearnCallResult",
    "LearnNodeRuntime",
    "NODE_PROTECTED_TERMS",
    "NODE_SYNTHESIZE",
    "NODE_TAG_WINDOWS",
    "TEMPLATE_CONTRACT",
    "call_structured",
    "estimate_input_tokens",
    "load_learn_runtimes",
    "payload_fits",
    "render_messages",
    "template_meets_contract",
]
