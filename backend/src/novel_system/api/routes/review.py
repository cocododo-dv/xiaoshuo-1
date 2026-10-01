from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.review import (
    ReviewCardCreateRequest,
    ReviewCardProjectRequest,
    ReviewCardResolveRequest,
)
from novel_system.api.response import respond
from novel_system.services.errors import DomainError
from novel_system.services.review_cards import ReviewCardService

router = APIRouter(tags=["review"])


@router.get("/api/v1/review-items")
def list_review_items(
    request: Request,
    session: Session = Depends(get_session),
    state: str | None = None,
    project_id: str | None = None,
):
    """待办收件箱：``?state=open|snoozed&project_id=…`` → 这部作品的卡片 ∪ 实时派生项（统一形状）。
    两个参数都要（旧的不带 state 的整表分页列表只列退役生产者的行、没有调用方，已删）。"""

    if not project_id:
        raise DomainError("REVIEW_PROJECT_REQUIRED", "project_id is required with state filter", status_code=400)
    result = ReviewCardService(session).list_cards(project_id, state=state or "")
    return respond(request, result)


@router.post("/api/v1/review-items")
def create_review_item(
    payload: ReviewCardCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    actor_ref = actor_ref_of(request)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: ReviewCardService(session).create_card(body, actor_ref=actor_ref),
    )

@router.post("/api/v1/review-items/{review_id}/resolve")
def resolve_review_card(
    review_id: str,
    request: Request,
    payload: ReviewCardResolveRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return mutate(
        request,
        session,
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).resolve(
            review_id,
            action_index=body.get("action_index"),
            project_id=body.get("project_id"),
            actor_ref=actor_ref,
        ),
    )

@router.post("/api/v1/review-items/{review_id}/unresolve")
def unresolve_review_card(
    review_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    return mutate(
        request,
        session,
        payload={"review_id": review_id},
        action=lambda: ReviewCardService(session).unresolve(review_id),
    )

@router.post("/api/v1/review-items/{review_id}/snooze")
def snooze_review_card(
    review_id: str,
    request: Request,
    payload: ReviewCardProjectRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return mutate(
        request,
        session,
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).snooze(review_id, project_id=body.get("project_id")),
    )

@router.post("/api/v1/review-items/{review_id}/unsnooze")
def unsnooze_review_card(
    review_id: str,
    request: Request,
    payload: ReviewCardProjectRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return mutate(
        request,
        session,
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).unsnooze(review_id, project_id=body.get("project_id")),
    )
