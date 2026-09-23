"""风格参考 v3（P5b）—「像不像」读数与对照检查的接口。

- ``GET /api/v1/scenes/{scene_id}/style-fidelity``：这一场每个阶段最新的读数（首稿 / 定向修改 / 补丁 / 终稿 / 对照检查）、
  风格步与补丁的决定、最近的参考评审分；
- ``GET /api/v1/projects/{project_id}/style-fidelity``：作品的读数走势、近期常见偏差、按维平均（确定性分与评审分分开）；
- ``GET /api/v2/style-reference/readings/{reading_id}``：一条读数；
- ``POST /api/v2/style-reference/checks``：对照检查——建一个作业（作业表 kind=check），读数 + 参考评审 + 抄袭门；
- ``GET /api/v2/style-reference/checks/{job_id}``：对照检查作业的进度与结果读数。

读数入库只有 ``services.style_reference.readings.record_fidelity_reading`` 一个入口；这里的读接口都不写库。
旧的「回测」接口（``/profiles/{id}/validate``、``/reports``）在 ``style_reference.py`` 里，调用即 410，由路由拆分包删除。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.response import ok
from novel_system.db.models import SceneCard, StyleFidelityReading, StyleReferenceJob
from novel_system.services.errors import DomainError
from novel_system.services.style_fidelity_view import (
    project_style_fidelity,
    scene_style_fidelity,
)
from novel_system.services.style_reference.check_job import (
    CHECK_MAX_TEXT_CHARS,
    check_job_payload,
    resolve_check_client,
    start_check_job,
)
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.jobs import JOB_KIND_CHECK, dispatch_job
from novel_system.services.style_reference.readings import reading_payload

router = APIRouter(tags=["style_fidelity"])

STYLE_REFERENCE_PREFIX = "/api/v2/style-reference"


def _req_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


class CheckRequest(BaseModel):
    """对照检查：``text`` 与 ``scene_id`` 恰好给一个；文字要说对照哪份参考（``profile_id`` 或 ``project_id``）。"""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=CHECK_MAX_TEXT_CHARS)
    scene_id: str | None = Field(default=None, max_length=255)
    profile_id: str | None = Field(default=None, max_length=255)
    project_id: str | None = Field(default=None, max_length=255)


@router.get("/api/v1/scenes/{scene_id}/style-fidelity")
def get_scene_style_fidelity(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    scene = session.get(SceneCard, scene_id)
    if scene is None:
        raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
    return ok(scene_style_fidelity(session, scene), req_id=_req_id(request))


@router.get("/api/v1/projects/{project_id}/style-fidelity")
def get_project_style_fidelity(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    from novel_system.db.models import StoryProject

    if session.get(StoryProject, project_id) is None:
        raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
    return ok(project_style_fidelity(session, project_id), req_id=_req_id(request))


@router.get(f"{STYLE_REFERENCE_PREFIX}/readings/{{reading_id}}")
def get_fidelity_reading(
    reading_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    row = session.get(StyleFidelityReading, reading_id)
    if row is None:
        raise DomainError(
            "STYLE_REFERENCE_READING_NOT_FOUND",
            f"reading {reading_id!r} not found",
            status_code=404,
        )
    return ok({"reading": reading_payload(row)}, req_id=_req_id(request))


@router.post(f"{STYLE_REFERENCE_PREFIX}/checks")
def create_style_check(
    request: Request,
    payload: CheckRequest,
    session: Session = Depends(get_session),
):
    """建一个对照检查作业（事务提交后派发）。没有模型 409 ``STYLE_REFERENCE_LLM_REQUIRED``；没有可对照的参考
    409 ``STYLE_REFERENCE_CHECK_NOT_BOUND``；参数不对 400 ``STYLE_REFERENCE_CHECK_TARGET_INVALID``。"""
    body = payload.model_dump(mode="json")
    client, enabled = resolve_check_client()
    if not enabled or client is None:
        raise LLMRequiredError(operation="style_check")
    op_key = request.headers.get("X-Idempotency-Key")

    def _do() -> dict[str, Any]:
        job = start_check_job(
            session,
            text=body.get("text"),
            scene_id=body.get("scene_id"),
            profile_id=body.get("profile_id"),
            project_id=body.get("project_id"),
            op_key=op_key,
            llm_client=client,
            llm_enabled=enabled,
        )
        return {"job_id": job.job_id, "state": job.state, **check_job_payload(session, job)}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{STYLE_REFERENCE_PREFIX}/checks",
        payload=body,
        action=_do,
        after_commit=_dispatch_check,
    )


def _dispatch_check(result: dict[str, Any]) -> None:
    """事务提交后把对照检查作业投给工人（认领是条件写，重复投递无害；漏投的由清扫线程补派）。"""
    job_id = str((result or {}).get("job_id") or "")
    if job_id:
        dispatch_job(job_id)


@router.get(f"{STYLE_REFERENCE_PREFIX}/checks/{{job_id}}")
def get_style_check(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    job = session.get(StyleReferenceJob, job_id)
    if job is None or job.kind != JOB_KIND_CHECK:
        raise DomainError(
            "STYLE_REFERENCE_CHECK_NOT_FOUND",
            f"style check {job_id!r} not found",
            status_code=404,
        )
    return ok(check_job_payload(session, job), req_id=_req_id(request))
