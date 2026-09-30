"""FE-ALIGN Phase 4: 回收站端点（v2）。

- DELETE /api/v2/projects/{id}            整部软删（进回收站）
- GET    /api/v2/trash?project_id=…       三级统一列表（全局作品桶 + 作品内章/场景桶）
- POST   /api/v2/trash/{entry_id}/restore 按条目恢复（整部作品是 ``work:{id}``）
- DELETE /api/v2/trash/{entry_id}         永久清除（D3：仅手动）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.response import respond
from novel_system.services.trash import TrashService

router = APIRouter(tags=["trash"])


@router.delete("/api/v2/projects/{project_id}")
def trash_project(
    project_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    return mutate(
        request,
        session,
        payload={"project_id": project_id},
        action=lambda: TrashService(session).trash_project(project_id, actor_ref=actor_ref_of(request)),
    )


@router.get("/api/v2/trash")
def list_trash(request: Request, project_id: str | None = None, session: Session = Depends(get_session)):
    return respond(request, TrashService(session).list_trash(project_id))


@router.post("/api/v2/trash/{entry_id}/restore")
def restore_trash_entry(
    entry_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    return mutate(
        request,
        session,
        payload={"entry_id": entry_id},
        action=lambda: TrashService(session).restore_entry(entry_id, actor_ref=actor_ref_of(request)),
    )


@router.delete("/api/v2/trash/{entry_id}")
def purge_trash_entry(
    entry_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    return mutate(
        request,
        session,
        payload={"entry_id": entry_id},
        action=lambda: TrashService(session).purge_entry(entry_id),
    )
