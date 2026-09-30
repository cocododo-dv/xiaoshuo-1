from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.requests.author_drafts import (
    AuthorDraftSaveRequest,
    CanonicalPromotionRequest,
    ProposalGenerateSetRequest,
)
from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.response import respond
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.canonical_manuscripts import CanonicalSceneService

router = APIRouter(tags=["author-drafts"])


@router.get("/api/v1/author-drafts/{object_type}/{object_id}/current")
def get_current_author_draft(object_type: str, object_id: str, request: Request, session: Session = Depends(get_session)):
    payload = AuthorDraftService(session).current(object_type, object_id)
    return respond(request, payload)


@router.post("/api/v1/author-drafts/{object_type}/{object_id}/ensure")
def ensure_author_draft(
    object_type: str,
    object_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{object_type}/{object_id}/ensure",
        payload={"object_type": object_type, "object_id": object_id},
        action=lambda: AuthorDraftService(session).ensure(object_type, object_id, actor_ref=actor_ref),
    )


@router.patch("/api/v1/author-drafts/{draft_id}")
def save_author_draft(
    draft_id: str,
    payload: AuthorDraftSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v1/author-drafts/{draft_id}",
        payload={"draft_id": draft_id, "body": body},
        action=lambda: AuthorDraftService(session).save(draft_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-drafts/{draft_id}/promote-canonical")
def promote_author_draft_canonical(
    draft_id: str,
    request: Request,
    payload: CanonicalPromotionRequest | None = None,
    session: Session = Depends(get_session),
):
    """Promote one saved scene AuthorDraft revision into canonical FinalScene.

    ``requires_reconcile`` publishes the exact author revision while keeping its
    canon ledger pending. ``facts_unchanged`` is accepted only when the previous
    final already has a complete, hash-matched canon commit.
    """

    actor_ref = actor_ref_of(request)
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{draft_id}/promote-canonical",
        payload={"draft_id": draft_id, **body},
        action=lambda: CanonicalSceneService(session).promote_author_draft(
            draft_id,
            body,
            actor_ref=actor_ref,
        ),
    )


@router.get("/api/v1/author-drafts/{draft_id}/revisions")
def list_author_draft_revisions(
    draft_id: str,
    request: Request,
    page: int | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    session: Session = Depends(get_session),
):
    result = AuthorDraftService(session).revisions(
        draft_id, page=page, page_size=page_size, cursor=cursor, limit=limit
    )
    return respond(request, result)


@router.get("/api/v1/author-drafts/{draft_id}/revisions/{revision_no}")
def get_author_draft_revision(draft_id: str, revision_no: int, request: Request, session: Session = Depends(get_session)):
    result = AuthorDraftService(session).revision(draft_id, revision_no)
    return respond(request, result)


@router.post("/api/v1/author-drafts/{draft_id}/proposals/generate-set")
def generate_author_draft_proposal_set(
    draft_id: str,
    request: Request,
    payload: ProposalGenerateSetRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{draft_id}/proposals/generate-set",
        payload={"draft_id": draft_id, "body": body},
        action=lambda: AuthorDraftService(session).generate_proposal_set(draft_id, body, actor_ref=actor_ref),
    )
