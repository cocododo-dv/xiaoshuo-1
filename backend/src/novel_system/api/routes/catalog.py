"""FE-ALIGN Phase 3: 目录 API（v2）—— 章节/场景树的唯一真相源。

对应原型 WsCatalog（design/ws-catalog.jsx）；创建类端点（建章/建场景）经
idempotent_response 兑现幂等键（必填 + 同键重放同响应）；PATCH/软删
对旧调用方不强制键，但客户端给键时同样执行持久重放。import 端点仅供 localStorage 一次性迁移使用，admin token 保护
（loopback 免 token）。恢复走 /api/v2/trash 统一回收站端点。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field
from sqlalchemy.orm import Session

from novel_system.api.catalog_requests import (
    CatalogChapterCreateRequest,
    CatalogChapterUpdateRequest,
    CatalogImportRequest,
    CatalogSceneCreateRequest,
    CatalogSceneUpdateRequest,
)
from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.request_types import EmptyRequest, StrictRequestModel
from novel_system.api.response import ok
from novel_system.services.catalog import CatalogService
from novel_system.services.system_config import require_admin_token

router = APIRouter(tags=["catalog"])


class ChapterOrderRequest(StrictRequestModel):
    chapter_ids: list[
        Annotated[str, Field(min_length=1, max_length=255)]
    ] = Field(min_length=1, max_length=10_000)


def _req_id(request: Request):
    return getattr(request.state, "request_id", None)


def _operator(request: Request) -> str:
    return getattr(request.state, "operator_ref", None) or "operator"


@router.get("/api/v2/projects/{project_id}/catalog")
def get_catalog(project_id: str, request: Request, session: Session = Depends(get_session)):
    return ok(CatalogService(session).catalog(project_id), req_id=_req_id(request))


@router.post("/api/v2/projects/{project_id}/catalog/chapters")
def create_catalog_chapter(
    project_id: str,
    payload: CatalogChapterCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters",
        payload={"project_id": project_id, "body": body},
        action=lambda: CatalogService(session).create_chapter(project_id, body),
    )


@router.patch("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}")
def update_catalog_chapter(
    project_id: str,
    chapter_id: str,
    payload: CatalogChapterUpdateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: CatalogService(session).update_chapter(
            project_id, chapter_id, body, actor_ref=_operator(request)
        ),
    )


@router.post("/api/v2/projects/{project_id}/catalog/chapter-order")
def reorder_catalog_chapters(
    project_id: str,
    payload: ChapterOrderRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapter-order",
        payload={"project_id": project_id, **body},
        action=lambda: CatalogService(session).reorder_chapters(
            project_id,
            body["chapter_ids"],
        ),
    )


@router.post("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes")
def create_catalog_scene(
    project_id: str,
    chapter_id: str,
    payload: CatalogSceneCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: CatalogService(session).create_scene(project_id, chapter_id, body),
    )


@router.patch("/api/v2/projects/{project_id}/catalog/scenes/{scene_id}")
def update_catalog_scene(
    project_id: str,
    scene_id: str,
    payload: CatalogSceneUpdateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v2/projects/{project_id}/catalog/scenes/{scene_id}",
        payload={"project_id": project_id, "scene_id": scene_id, "body": body},
        action=lambda: CatalogService(session).update_scene(project_id, scene_id, body),
    )


@router.delete("/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}")
def trash_catalog_chapter(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """章级软删（桥接既有 AuthorLifecycleService trash 机制）。"""
    from novel_system.services.trash import TrashService

    return optional_idempotent_response(
        request,
        session,
        method="DELETE",
        path_template="/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
        payload={
            "project_id": project_id,
            "chapter_id": chapter_id,
            "body": payload.model_dump(mode="json") if payload else {},
        },
        action=lambda: TrashService(session).trash_chapter_in_project(
            project_id, chapter_id, actor_ref=_operator(request)
        ),
    )


@router.delete("/api/v2/projects/{project_id}/catalog/scenes/{scene_id}")
def trash_catalog_scene(
    project_id: str,
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """场景级软删（桥接既有 AuthorLifecycleService trash 机制）。"""
    from novel_system.services.trash import TrashService

    return optional_idempotent_response(
        request,
        session,
        method="DELETE",
        path_template="/api/v2/projects/{project_id}/catalog/scenes/{scene_id}",
        payload={
            "project_id": project_id,
            "scene_id": scene_id,
            "body": payload.model_dump(mode="json") if payload else {},
        },
        action=lambda: TrashService(session).trash_scene_in_project(
            project_id, scene_id, actor_ref=_operator(request)
        ),
    )


@router.post("/api/v2/projects/{project_id}/catalog/import")
def import_catalog(
    project_id: str,
    payload: CatalogImportRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    client_host = request.client.host if request.client else None
    require_admin_token(x_admin_token, client_host=client_host)
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/catalog/import",
        payload={"project_id": project_id, "body": body},
        action=lambda: CatalogService(session).import_catalog(project_id, body),
    )
