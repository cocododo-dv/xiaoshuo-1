from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
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
from novel_system.services.projects import ProjectChapterFlowService, ProjectService, start_project_chapter_run_job_worker

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
    job_to_start: str | None = None

    def prepare() -> dict[str, Any]:
        nonlocal job_to_start
        result = ProjectChapterFlowService(session).prepare_chapter_run_job(project_id, chapter_id)
        if bool(result.pop("_start_worker", False)):
            job_to_start = result["run"]["job_id"]
        return result

    response = mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=prepare,
    )
    # The closure is populated only when this request executed the action. A
    # durable replay returns the cached response without launching another worker.
    if job_to_start is not None:
        start_project_chapter_run_job_worker(project_id, chapter_id, job_to_start)
    return response


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


