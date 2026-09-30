from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.projects import (
    ProjectChapterApproveFinalRequest,
    ProjectChapterReadConfirmRequest,
    ProjectChapterReopenFinalRequest,
    ProjectChapterRunJobRequest,
    ProjectCreateRequest,
)
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.response import respond
from novel_system.db.models import ChapterRunJob
from novel_system.services.projects import ProjectChapterFlowService, ProjectService, start_project_chapter_run_job_worker
from novel_system.services.run_job_leases import STATUS_PENDING

router = APIRouter(tags=["projects"])


@router.post("/api/v1/projects")
def create_project(payload: ProjectCreateRequest, request: Request, session: Session = Depends(get_session)):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: ProjectService(session).create(body),
    )


@router.get("/api/v1/projects")
def list_projects(request: Request, session: Session = Depends(get_session)):
    return respond(request, ProjectService(session).list())


@router.get("/api/v1/projects/{project_id}/dashboard")
def project_dashboard(project_id: str, request: Request, session: Session = Depends(get_session)):
    return respond(request, ProjectService(session).dashboard(project_id))


@router.post("/api/v1/projects/{project_id}/outline-plan/{plan_id}/approve")
def approve_outline_plan(
    project_id: str,
    plan_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {}
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "plan_id": plan_id, **body},
        action=lambda: ProjectService(session).approve_outline_plan(project_id, plan_id),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/run")
def run_project_chapter(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {}
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, **body},
        action=lambda: ProjectChapterFlowService(session).run_chapter(project_id, chapter_id),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job")
def run_project_chapter_job(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ProjectChapterRunJobRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {}

    def prepare() -> dict[str, Any]:
        result = ProjectChapterFlowService(session).prepare_chapter_run_job(project_id, chapter_id)
        # 要不要拉起工人以库里任务的状态为准（见 dispatch_if_pending）：服务给的旗子只在还是 pending 时为真
        result.pop("_start_worker", None)
        return result

    def dispatch_if_pending(result: dict[str, Any]) -> None:
        # 提交之后派发（B12-07）；同一个幂等键重放时也走这里：任务还在等（提交与派发之间进程退出）就由这次重试
        # 接上，已经在跑 / 已结束的不动（工人拉起之前会先把作品标成运行中，所以不能给不在等的任务派发）。
        job_id = str(((result or {}).get("run") or {}).get("job_id") or "")
        if job_id and session.scalar(select(ChapterRunJob.status).where(ChapterRunJob.job_id == job_id)) == STATUS_PENDING:
            start_project_chapter_run_job_worker(project_id, chapter_id, job_id)

    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=prepare,
        after_commit=dispatch_if_pending,
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/approve-final")
def approve_project_chapter_final(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ProjectChapterApproveFinalRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    actor_ref = actor_ref_of(request)
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, **body},
        action=lambda: ProjectChapterFlowService(session).approve_final(project_id, chapter_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/reopen-final")
def reopen_project_chapter_final(
    project_id: str,
    chapter_id: str,
    payload: ProjectChapterReopenFinalRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    actor_ref = actor_ref_of(request)
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, **body},
        action=lambda: ProjectChapterFlowService(session).reopen_final(
            project_id,
            chapter_id,
            reason=body["reason"],
            actor_ref=actor_ref,
        ),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/read-confirm")
def confirm_project_chapter_read(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ProjectChapterReadConfirmRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    actor_ref = actor_ref_of(request)
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ProjectChapterFlowService(session).confirm_read(
            project_id, chapter_id, body, actor_ref=actor_ref
        ),
    )


