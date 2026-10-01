"""FE-ALIGN Phase 2: v2 作品域端点 — profile / writing-stats / dashboard。

原型对应：profile = WsWorks 作品档案；dashboard = 主页（续写卡/GOS/雪花进度/近章）；
writing-stats = 今日字数/streak（D2 服务端计算）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.projects import ProjectProfileUpdateRequest
from novel_system.api.response import respond
from novel_system.services.project_overview import ProjectOverviewService
from novel_system.services.projects import ProjectService

router = APIRouter(tags=["project-overview"])


@router.patch("/api/v2/projects/{project_id}/profile")
def update_project_profile(
    project_id: str,
    payload: ProjectProfileUpdateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "body": body},
        action=lambda: ProjectService(session).update_profile(project_id, body),
    )


@router.get("/api/v2/projects/{project_id}/writing-stats")
def project_writing_stats(project_id: str, request: Request, session: Session = Depends(get_session)):
    result = ProjectOverviewService(session).writing_stats(project_id)
    return respond(request, result)


@router.get("/api/v2/projects/{project_id}/dashboard")
def project_dashboard_v2(project_id: str, request: Request, session: Session = Depends(get_session)):
    result = ProjectOverviewService(session).dashboard(project_id)
    return respond(request, result)
