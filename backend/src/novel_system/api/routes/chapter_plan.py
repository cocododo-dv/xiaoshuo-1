"""章节编排 LLM 规划 API（v2，挂 catalog 路由族）。

设计文档：docs/chapter-arrangement-llm-design-2026-07-16.md §5。
- 蓝图三端点：GET/PUT architecture + POST architecture/generate（幂等）
- 三通道：POST plan/candidates | plan/fill | plan/review（单次结构化咨询调用；
  每次主动重生成会获得新键，同一网络请求重试则重放，避免重复计费；
  计量/审计仍由 execute_accounted_call 承担）。没有可用模型时生成 / 三通道回 409 + author_action（fail-closed）。
- GET plan/gaps：待补清单——按空槽算的，不是 AI 结果，不需要模型。
- POST plan/apply（幂等）：补丁经服务端 sanitize 后单事务原子回写目录。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.requests.chapter_plan import (
    ChapterPlanApplyRequest,
    ChapterPlanCandidatesRequest,
    ChapterPlanFillRequest,
)
from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.requests.common import BoundedJsonObject, EmptyRequest
from novel_system.api.response import respond
from novel_system.services.chapter_plan_llm import ChapterPlanService

router = APIRouter(tags=["chapter-plan"])


@router.get("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/architecture")
def get_chapter_architecture(
    project_id: str,
    chapter_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    result = ChapterPlanService(session).get_architecture(project_id, chapter_id)
    return respond(request, result)


@router.put("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/architecture")
def put_chapter_architecture(
    project_id: str,
    chapter_id: str,
    payload: BoundedJsonObject,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="PUT",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/architecture",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).put_architecture(
            project_id,
            chapter_id,
            body,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/architecture/generate")
def generate_chapter_architecture(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/architecture/generate",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).generate_architecture(
            project_id,
            chapter_id,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/candidates")
def chapter_plan_candidates(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ChapterPlanCandidatesRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/candidates",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).candidates(project_id, chapter_id, body),
    )


@router.post("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/fill")
def chapter_plan_fill(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ChapterPlanFillRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/fill",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).fill(project_id, chapter_id, body),
    )


@router.post("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/review")
def chapter_plan_review(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/review",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).review(project_id, chapter_id),
    )


@router.get("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/gaps")
def chapter_plan_gaps(
    project_id: str,
    chapter_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """待补清单（不是 AI）：这一章的戏剧卡与各场三拍 / 视角还空着哪些。"""
    result = ChapterPlanService(session).gaps(project_id, chapter_id)
    return respond(request, result)


@router.post("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/apply")
def chapter_plan_apply(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ChapterPlanApplyRequest,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/plan/apply",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).apply(
            project_id,
            chapter_id,
            body,
            actor_ref=actor_ref_of(request),
        ),
    )
