"""一次结构化 LLM 调用的共用骨架（B07-10）：组请求 → 计量调用 → 补审计摘要 → 归一输出 → 各服务自己的错误码。

章节编排（``chapter_plan_llm``）走这里。雪花工作区的运行器（``snowflake_workspace_llm._run_structured_task``）
还是它自己的一份——它多了输入预算、按编辑器模板补全 schema、计数 / 稀疏重试的细节，而且好几个测试按模块属性
替换它的 ``execute_accounted_call`` / ``mark_postprocess_failure``；合并留给它的主人（见 P04 交接）。

错误码按调用方给的前缀拼（``CHAPTER_PLAN`` → ``CHAPTER_PLAN_LLM_CALL_FAILED`` / ``…_LLM_RESPONSE_INVALID_SCHEMA``）：
前端按码分支，各服务的码不变。给作者看的话是中文，不带异常原文（原文在 ``details.response_summary`` 与审计里）。

审计摘要补写后**当场提交**（与雪花运行器同一个做法）：调用失败 / 输出不合格时调用方会抛领域错误、整个请求回滚，
不先提交的话这一次花了钱的调用在审计里就只剩一个空壳。
"""

from __future__ import annotations

import uuid
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
    failure_message: str,
    invalid_message: str,
) -> StructuredCallResult:
    """发一次结构化调用（项目作用域计量），返回归一后的输出。

    ``failure_message`` / ``invalid_message``：调用失败 / 输出不合格时给作者看的中文句子。
    """
    prompt_hash = structured_prompt_hash(
        template.name,
        template.version,
        template.system_prompt,
        user_prompt,
        template.structured_schema,
    )
    llm_call_id = f"llm_call_project_{node_id}_{uuid.uuid4().hex[:12]}"
    request = build_llm_request(
        task_config,
        node_id=node_id,
        messages=[
            {"role": "system", "content": template.system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_schema={"name": template.name, "schema": template.structured_schema},
    )
    request_summary = sanitize_audit_summary(
        {
            "task_key": node_id,
            "template_name": template.name,
            "template_version": template.version,
            "step_key": step_ref,
            **normalize(prompt_payload),
        }
    )
    try:
        response = execute_accounted_call(
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
        supplement_accounted_call(
            session,
            llm_call_id,
            request_summary=request_summary,
            prompt_hash=prompt_hash,
            response_summary=error_audit_summary(exc),
        )
        raise DomainError(
            f"{error_prefix}_LLM_CALL_FAILED",
            failure_message,
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
        mark_postprocess_failure(session, llm_call_id, error_code="LLM_RESPONSE_INVALID_SCHEMA", error_text=str(exc))
        supplement_accounted_call(
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
        raise DomainError(
            f"{error_prefix}_LLM_RESPONSE_INVALID_SCHEMA",
            invalid_message,
            status_code=409,
            details={
                "llm_call_id": llm_call_id,
                "node_id": node_id,
                "error_code": "LLM_RESPONSE_INVALID_SCHEMA",
                "next_action": "retry_or_adjust_prompt_schema",
                "reason": str(exc),
            },
        ) from exc

    supplement_accounted_call(
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
