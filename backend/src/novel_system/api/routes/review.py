from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session, request_id_of
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest, StrictRequestModel
from novel_system.api.response import ok
from novel_system.services.errors import DomainError
from novel_system.services.review_cards import ReviewCardService

router = APIRouter(tags=["review"])

OptionalIdentifier = Annotated[str, Field(max_length=255)]
CardListItem = Annotated[str, Field(max_length=4000)]


class ReviewCardCreateRequest(StrictRequestModel):
    project_id: OptionalIdentifier | None = None
    scene_id: OptionalIdentifier | None = None
    chapter_id: OptionalIdentifier | None = None
    # Values remain domain-validated for REVIEW_CARD_KIND_INVALID.
    kind: str = Field(max_length=64)
    priority: int | None = Field(default=None, ge=1, le=10)
    title: str | None = Field(default=None, max_length=10_000)
    source: str | None = Field(default=None, max_length=255)
    where: str | None = Field(default=None, max_length=1000)
    occurred_at: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=100_000)
    preview: str | None = Field(default=None, max_length=100_000)
    checklist: list[CardListItem] | None = Field(default=None, max_length=500)
    options: list[CardListItem] | None = Field(default=None, max_length=500)
    actions: list[BoundedJsonObject] | None = Field(default=None, max_length=100)
    dedupe_key: str | None = Field(default=None, max_length=512)

class ReviewCardResolveRequest(StrictRequestModel):
    action_index: int | None = Field(default=None, ge=0, le=10_000)
    project_id: OptionalIdentifier | None = None

class ReviewCardProjectRequest(StrictRequestModel):
    project_id: OptionalIdentifier | None = None

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
    return ok(result, req_id=request_id_of(request))


@router.post("/api/v1/review-items")
def create_review_item(
    payload: ReviewCardCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    actor_ref = actor_ref_of(request)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items",
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
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/resolve",
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
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/unresolve",
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
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/snooze",
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
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/unsnooze",
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).unsnooze(review_id, project_id=body.get("project_id")),
    )
