from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.chapter_requests import ChapterIdsRequest, ChapterSceneOrderRequest, ChapterUpsertRequest
from novel_system.api.deps import actor_ref_of, get_session, request_id_of
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.catalog import CatalogService
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.chapter_upsert import upsert_chapter
from novel_system.services.writer_briefs import normalize_chapter_writer_brief

router = APIRouter(tags=["chapters"])


@router.get("/api/v1/chapters")
def list_chapters(request: Request, session: Session = Depends(get_session)):
    return ok(
        {"items": AuthorLifecycleService(session).list_active_chapters()},
        req_id=request_id_of(request),
    )


@router.post("/api/v1/chapters")
def create_chapter(
    payload: ChapterUpsertRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(exclude_unset=True)
    # Reject/normalize the domain payload before claiming an idempotency key.
    # Invalid requests must never poison a key that the author can correct and
    # retry, while semantically equivalent briefs should hash identically.
    body["writer_brief_json"] = normalize_chapter_writer_brief(
        body.get("writer_brief_json")
    )
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters",
        payload=body,
        action=lambda: upsert_chapter(session, body),
    )


@router.post("/api/v1/chapters/trash")
def trash_chapters(payload: ChapterIdsRequest, request: Request, session: Session = Depends(get_session)):
    body = payload.model_dump(mode="json")
    actor_ref = actor_ref_of(request)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/trash",
        payload=body,
        action=lambda: AuthorLifecycleService(session).trash_chapters(body["chapter_ids"], actor_ref),
    )


@router.post("/api/v1/chapters/{chapter_id}/run/full")
def run_chapter_full(
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    AuthorLifecycleService(session).require_active_chapter(chapter_id)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/{chapter_id}/run/full",
        payload={"chapter_id": chapter_id},
        action=lambda lease: ChapterRunnerService(session).run_full(chapter_id, request_lease=lease),
    )


@router.get("/api/v1/chapters/{chapter_id}/run-status")
def chapter_run_status(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    AuthorLifecycleService(session).require_active_chapter(chapter_id)
    payload = ChapterRunnerService(session).run_status(chapter_id)
    return ok(payload, req_id=request_id_of(request))


@router.post("/api/v1/chapters/{chapter_id}/scene-order")
def reorder_chapter_scenes(
    chapter_id: str,
    payload: ChapterSceneOrderRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/{chapter_id}/scene-order",
        payload={"chapter_id": chapter_id, **body},
        action=lambda: CatalogService(session).reorder_scenes(chapter_id, body),
    )
