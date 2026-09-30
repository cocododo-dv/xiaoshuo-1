"""一次结构化 LLM 调用的共用骨架（B07-10）：组请求 → 计量调用 → 补审计摘要 → 归一输出 → 各服务自己的错误码。

章节编排（``chapter_plan_llm``）与雪花工作区（``snowflake_workspace_llm._run_structured_task``）都走这里。雪花多出来的
几样经参数交进来：输入预算削过的载荷与预算留痕（``extra_request_summary``）、按编辑器模板补全过的 schema
（``structured_schema``）、按异常说话的失败句子（``failure_message`` 可以是函数）、计数 / 稀疏重试要的错误细节
（``invalid_message`` / ``invalid_details``）。

打桩：``execute`` / ``mark_failure`` / ``supplement`` 三个钩子缺省时在**调用时**取本模块的同名属性；调用方可以把
它自己模块命名空间里的同名函数交进来（雪花运行器就这样做），按模块属性替换它们的测试照旧生效。

错误码按调用方给的前缀拼（``CHAPTER_PLAN`` → ``CHAPTER_PLAN_LLM_CALL_FAILED`` / ``…_LLM_RESPONSE_INVALID_SCHEMA``）：
前端按码分支，各服务的码不变。

审计摘要补写后**当场提交**：调用失败 / 输出不合格时调用方会抛领域错误、整个请求回滚，不先提交的话这一次花了钱的
调用在审计里就只剩一个空壳。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import normalize
from novel_system.services.llm_accounting import LLMCallContext, execute_accounted_call, mark_postprocess_failure
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.llm_client import build_llm_request
from novel_system.services.llm_service_base import structured_prompt_hash


@dataclass(slots=True)
class StructuredCallResult:
    llm_call_id: str
    output: dict[str, Any]


def run_structured_call(
    session: Session,
    client: Any,
    *,
    task_config: Any,
    template: Any,
    node_id: str,
    project_id: str,
    step_ref: str,
    user_prompt: str,
    prompt_payload: dict[str, Any],
    normalize_output: Callable[[dict[str, Any]], dict[str, Any]],
    error_prefix: str,
    failure_message: str | Callable[[Exception], str],
    invalid_message: str | Callable[[Exception], str],
    invalid_details: Callable[[Exception, Any], dict[str, Any]] | None = None,
    structured_schema: dict[str, Any] | None = None,
    extra_request_summary: Mapping[str, Any] | None = None,
    execute: Callable[..., Any] | None = None,
    mark_failure: Callable[..., Any] | None = None,
    supplement: Callable[..., Any] | None = None,
) -> StructuredCallResult:
    """发一次结构化调用（项目作用域计量），返回归一后的输出。

    ``failure_message`` / ``invalid_message``：调用失败 / 输出不合格时给作者看的句子（或按异常给句子的函数）。
    ``invalid_details(exc, response)``：输出不合格时 ``details`` 里 ``error_code`` 之后的键；缺省
    ``{"next_action": "retry_or_adjust_prompt_schema", "reason": str(exc)}``。
    ``structured_schema``：下发并计入提示指纹的 schema（缺省是模板自己的）。
    ``extra_request_summary``：请求审计摘要里接在 ``step_key`` 之后、载荷之前的键。
    """
    execute = execute or execute_accounted_call
    mark_failure = mark_failure or mark_postprocess_failure
    supplement = supplement or supplement_accounted_call
    schema = template.structured_schema if structured_schema is None else structured_schema
    prompt_hash = structured_prompt_hash(
        template.name,
        template.version,
        template.system_prompt,
        user_prompt,
        schema,
    )
    llm_call_id = f"llm_call_project_{node_id}_{uuid.uuid4().hex[:12]}"
    request = build_llm_request(
        task_config,
        node_id=node_id,
        messages=[
            {"role": "system", "content": template.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_schema={"name": template.name, "schema": schema},
    )
    request_summary = sanitize_audit_summary(
        {
            "task_key": node_id,
            "template_name": template.name,
            "template_version": template.version,
            "step_key": step_ref,
            **dict(extra_request_summary or {}),
            **normalize(prompt_payload),
        }
    )
    try:
        response = execute(
            session,
            client,
            request,
            LLMCallContext(
                scope_type="project",
                scope_id=project_id,
                project_id=project_id,
                node_id=node_id,
                step=step_ref,
            ),
            llm_call_id=llm_call_id,
        )
    except Exception as exc:  # noqa: BLE001 — 任何调用失败都记进审计，再翻成领域错误
        supplement(
            session,
            llm_call_id,
            request_summary=request_summary,
            prompt_hash=prompt_hash,
            response_summary=error_audit_summary(exc),
        )
        raise DomainError(
            f"{error_prefix}_LLM_CALL_FAILED",
            failure_message(exc) if callable(failure_message) else failure_message,
            status_code=409,
            details={
                "llm_call_id": llm_call_id,
                "node_id": node_id,
                "error_code": getattr(exc, "code", exc.__class__.__name__),
                "next_action": "check_provider_route_model_and_retry",
                "response_summary": error_audit_summary(exc),
            },
        ) from exc

    try:
        raw_output = response.structured_output or {}
        if not isinstance(raw_output, dict):
            raise ValueError("structured output must be an object")
        normalized_output = normalize_output(raw_output)
    except Exception as exc:  # noqa: BLE001 — 归一器里的任何失败都当作输出不合格
        mark_failure(session, llm_call_id, error_code="LLM_RESPONSE_INVALID_SCHEMA", error_text=str(exc))
        supplement(
            session,
            llm_call_id,
            request_summary=request_summary,
            prompt_hash=prompt_hash,
            response_summary={
                "message": str(exc),
                "structured_output": response.structured_output,
                "request_id": response.request_id,
            },
        )
        details = (
            invalid_details(exc, response)
            if invalid_details is not None
            else {"next_action": "retry_or_adjust_prompt_schema", "reason": str(exc)}
        )
        raise DomainError(
            f"{error_prefix}_LLM_RESPONSE_INVALID_SCHEMA",
            invalid_message(exc) if callable(invalid_message) else invalid_message,
            status_code=409,
            details={
                "llm_call_id": llm_call_id,
                "node_id": node_id,
                "error_code": "LLM_RESPONSE_INVALID_SCHEMA",
                **details,
            },
        ) from exc

    supplement(
        session,
        llm_call_id,
        request_summary=request_summary,
        prompt_hash=prompt_hash,
        response_summary={
            "request_id": response.request_id,
            "response_format": response.response_format,
            "structured_output": response.structured_output,
        },
    )
    return StructuredCallResult(llm_call_id=response.llm_call_id or llm_call_id, output=normalized_output)


def supplement_accounted_call(
    session: Session,
    llm_call_id: str,
    *,
    request_summary: dict[str, Any],
    prompt_hash: str,
    response_summary: dict[str, Any],
) -> None:
    """计量层已经落好的那一行 ``llm_calls`` 补上提示指纹与请求 / 回包摘要（有界指纹，不是原文），并提交。"""
    parent = session.get(LlmCall, llm_call_id)
    if parent is None:
        raise RuntimeError(f"accounted structured call {llm_call_id} is missing")
    parent.prompt_hash = prompt_hash
    parent.request_payload_summary = sanitize_audit_summary(
        {**dict(parent.request_payload_summary or {}), **request_summary}
    )
    parent.response_payload_summary = sanitize_audit_summary(
        {**dict(parent.response_payload_summary or {}), **response_summary}
    )
    session.commit()
