"""FE-ALIGN Phase 3: 目录 API（v2）—— 章节/场景树的唯一真相源。

对应原型 WsCatalog（design/ws-catalog.jsx）；每个写接口都经 mutate 兑现幂等键（必填 + 同键重放同响应）。
删章 / 删场走 v1 的 ``/api/v1/{chapters,scenes}/trash``，恢复走 /api/v2/trash 统一回收站端点（这里的两个 DELETE 与 localStorage 一次性迁移用的 import 端点
没有界面调用，已删：批准 #24a / #25）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.requests.catalog import (
    CatalogChapterCreateRequest,
    CatalogChapterUpdateRequest,
    CatalogSceneCreateRequest,
    CatalogSceneUpdateRequest,
    ChapterOrderRequest,
)
from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.response import respond
from novel_system.services.catalog import CatalogService

router = APIRouter(tags=["catalog"])


@router.get("/api/v2/projects/{project_id}/catalog")
def get_catalog(project_id: str, request: Request, session: Session = Depends(get_session)):
    return respond(request, CatalogService(session).catalog(project_id))


@router.post("/api/v2/projects/{project_id}/catalog/chapters")
def create_catalog_chapter(
    project_id: str,
    payload: CatalogChapterCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: CatalogService(session).update_chapter(
            project_id, chapter_id, body, actor_ref=actor_ref_of(request)
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "scene_id": scene_id, "body": body},
        action=lambda: CatalogService(session).update_scene(project_id, scene_id, body),
    )
