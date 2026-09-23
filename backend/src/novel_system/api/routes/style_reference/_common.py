"""Style Reference 路由包的共用部分：路径前缀、序列化、请求辅助、运行时模型客户端与作业派发。

书库 / 画像 / 绑定的载荷由服务层的读模型给出（``services/style_reference/summaries.py``、
``binding_apply.binding_payload``），这里只剩几个行级序列化。

测试在**包**上打桩运行时模型客户端（``novel_system.api.routes.style_reference._get_llm_client_and_enabled``），
所以各子模块一律经 :func:`llm_client_and_enabled` 取——它在调用时回到包上找这个名字。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

PATH_PREFIX = "/api/v2/style-reference"
ROUTE_TAGS = ["style_reference"]


def serialize_run(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "book_id": run.book_id,
        "status": run.status,
        "phase": run.phase,
        "dispatch_state": run.dispatch_state,
        "requested_layers": list(run.requested_layers_json or []),
        "coverage_json": run.coverage_json or {},
        "heartbeat_at": run.heartbeat_at,
        "error_code": run.error_code,
        "error_text": run.error_text,
        "retryable": bool(run.retryable),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def serialize_finding(finding, *, evidence: list | None = None) -> dict[str, Any]:
    """一条抽取发现(文风卡行的依据;``?include=evidence`` 时带证据引文)。v3 起发现不再单独审核 / 投票:
    作者在文风卡上逐句 ✓ / ✗(``POST /profiles/{id}/card-lines/{line_id}``)。"""
    payload = {
        "finding_id": finding.finding_id,
        "book_id": finding.book_id,
        "run_id": finding.run_id,
        "extraction_id": finding.extraction_id,
        "sub_dimension": finding.sub_dimension,
        "finding_kind": finding.finding_kind,
        "statement": finding.statement,
        "confidence": finding.confidence,
    }
    if evidence is not None:
        payload["evidence"] = evidence
    return payload


def serialize_banned_term(term) -> dict[str, Any]:
    return {
        "term_id": term.term_id,
        "profile_id": term.profile_id,
        "term": term.term,
        "replacement_hint": term.replacement_hint,
        "source": term.source,
        "scope": term.scope,
        "created_at": term.created_at,
    }


def req_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _get_llm_client_and_enabled():
    """委托统一工厂 build_runtime_llm_client;包上保留同名属性供路由测试打桩。"""
    from novel_system.services.system_config import build_runtime_llm_client
    from novel_system.settings import get_settings

    return build_runtime_llm_client(settings=get_settings())


def llm_client_and_enabled():
    """运行时模型客户端:回到包上取 ``_get_llm_client_and_enabled``(测试在包上打桩)。"""
    from novel_system.api.routes import style_reference as package

    return package._get_llm_client_and_enabled()


def dispatch(job_id: str) -> None:
    """事务提交后把作业投给工人:回到包上取 ``dispatch_job``(测试在包上打桩,记下派发而不真跑)。"""
    from novel_system.api.routes import style_reference as package

    package.dispatch_job(job_id)


__all__ = [
    "PATH_PREFIX",
    "ROUTE_TAGS",
    "client_host",
    "dispatch",
    "llm_client_and_enabled",
    "req_id",
    "serialize_banned_term",
    "serialize_finding",
    "serialize_run",
]
