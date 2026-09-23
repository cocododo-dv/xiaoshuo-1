"""风格参考的单次 LLM 节点调用(预览 / 回测等仍在请求或回测 worker 里调用的节点用)。

学习文风作业不走这里:它每个作业载一次路由与模板、记账用自己的会话(``learn_llm``)。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall
from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMAccountingRejected,
    LLMCallContext,
    execute_accounted_call,
    is_llm_control_plane_failure,
)
from novel_system.services.llm_audit import sanitize_audit_summary
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


class LLMNodeError(Exception):
    """LLM 调用 / 解析失败的统一异常。

    caller 应捕获并按业务降级(如 validation/semantic 单调用失败时
    semantic_json=[],而非阻塞 sync_only 路径)。
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str | None = None,
        llm_call_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.llm_call_id = llm_call_id
        self.error_code = error_code


def call_llm_node(
    node_id: str,
    payload: UntrustedPayload,
    llm_client: Any,
    *,
    session: Session,
    context: LLMCallContext,
) -> dict[str, Any]:
    """对指定 task_name 节点发起一次 LLM 调用,返回 structured_output 字典。

    provider / 业务失败 raise LLMNodeError；账本控制面失败保持原异常向上冒泡。
    """
    if not isinstance(payload, UntrustedPayload):
        raise LLMNodeError(
            f"node {node_id!r} requires UntrustedPayload",
            node_id=node_id,
        )
    if context.node_id != node_id:
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_CONTEXT_INVALID",
            f"accounting context node {context.node_id!r} does not match {node_id!r}",
        )

    try:
        routing = load_model_routing_config()
        # DB 节点路由优先、yaml task 默认兜底——顺序教训见 resolve_node_route
        # 的 docstring(曾因只读 task_routing 引发 chat-only 中转 404 回归)。
        task_config = resolve_node_route(routing, node_id)
        template = load_prompt_templates()[node_id]
    except KeyError as exc:
        raise LLMNodeError(
            f"task routing / prompt template missing for {node_id!r}: {exc}",
            node_id=node_id,
        ) from exc

    try:
        system_prompt = render_untrusted_system_prompt(template.system_prompt)
        user_prompt = render_untrusted_user_prompt(
            template.task_prompt,
            payload,
            kind=node_id,
        )
    except Exception:  # pylint: disable=broad-except
        raise LLMNodeError(
            f"failed to render untrusted payload for node {node_id!r}",
            node_id=node_id,
        ) from None
    # style_ref 节点吃长 prompt(20 段原文 + schema)+ 长输出,本来就该慢。
    # build_llm_request 只在路由显式配置 timeout_seconds 时封顶,否则交给
    # client 全局设置(默认不限时)。
    request = build_llm_request(
        task_config,
        node_id=node_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_schema=template.structured_schema,
    )
    llm_call_id = f"llm_style_{uuid.uuid4().hex}"
    try:
        response = execute_accounted_call(
            session,
            llm_client,
            request,
            context,
            llm_call_id=llm_call_id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        if isinstance(exc, LLMAccountingError) or is_llm_control_plane_failure(exc):
            raise
        raise LLMNodeError(
            f"accounted LLM execution failed for {node_id!r}: {exc}",
            node_id=node_id,
            llm_call_id=llm_call_id,
            error_code=str(getattr(exc, "code", exc.__class__.__name__)),
        ) from exc
    structured = getattr(response, "structured_output", None) or {}
    parent = session.get(LlmCall, response.llm_call_id or llm_call_id)
    if parent is None:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PARENT_ID_MISSING",
            f"accounted LLM parent disappeared for {node_id!r}",
        )
    parent.response_payload_summary = sanitize_audit_summary(
        {
            **dict(parent.response_payload_summary or {}),
            "structured_output_present": bool(structured),
            "structured_output_keys": sorted(str(key) for key in structured),
        }
    )
    session.commit()
    return structured
