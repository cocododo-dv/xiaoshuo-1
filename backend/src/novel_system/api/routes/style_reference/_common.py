"""Style Reference 路由包的共用部分：路径前缀、序列化、请求辅助、运行时模型客户端。

测试在**包**上打桩运行时模型客户端（``novel_system.api.routes.style_reference._get_llm_client_and_enabled``），
所以各子模块一律经 :func:`llm_client_and_enabled` 取——它在调用时回到包上找这个名字。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from novel_system.services.style_reference.import_job import (
    classification_payload,
    classification_provenance,
    latest_classification_job,
)
from novel_system.services.style_reference.learn_job import latest_learn_job, learn_payload

PATH_PREFIX = "/api/v2/style-reference"
ROUTE_TAGS = ["style_reference"]

_NO_JOB = object()


def serialize_book(book, *, classification_job: Any = _NO_JOB, learn_job: Any = _NO_JOB) -> dict[str, Any]:
    """书的载荷。``classification`` = 最近一个分类作业的摘要;``classification_provenance`` = 段落类型
    的来源(新作业写的,老书按校准信息推出来),带一致率;``learn`` = 最近一个学习文风作业的摘要。
    两个作业由列表端点批量传入。"""
    stats = book.stats_json or {}
    session = Session.object_session(book)
    if classification_job is _NO_JOB:
        classification_job = (
            latest_classification_job(session, book.book_id) if session is not None else None
        )
    if learn_job is _NO_JOB:
        learn_job = latest_learn_job(session, book.book_id) if session is not None else None
    return {
        "book_id": book.book_id,
        "title": book.title,
        "author_label": book.author_label,
        "source_kind": book.source_kind,
        "source_path": book.source_path,
        "cloud_policy": book.cloud_policy,
        "text_checksum": book.text_checksum,
        "total_chars": book.total_chars,
        "status": book.status,
        "stats_json": stats,
        "classification": classification_payload(classification_job),
        "classification_provenance": classification_provenance(stats),
        "paragraph_types_revision": int(stats.get("paragraph_types_revision") or 0),
        "learn": learn_payload(learn_job),
        "created_at": book.created_at,
        "updated_at": book.updated_at,
    }


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


def serialize_profile(profile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "book_id": profile.book_id,
        "run_id": profile.run_id,
        "title": profile.title,
        "status": profile.status,
        "profile_json": profile.profile_json or {},
        "coverage_json": profile.coverage_json or {},
        "version_tag": profile.version_tag,
        "source_finding_ids_json": profile.source_finding_ids_json or [],
    }


def serialize_binding(binding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "profile_id": binding.profile_id,
        "scope": binding.scope,
        "scope_ref_id": binding.scope_ref_id,
        "task_type": binding.task_type,
        "strategy": binding.strategy,
        "status": binding.status,
        "config_json": binding.config_json or {},
    }


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
    "serialize_binding",
    "serialize_book",
    "serialize_finding",
    "serialize_profile",
    "serialize_run",
]
