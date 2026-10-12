"""章节编排的章节蓝图 API（v2，挂 catalog 路由族）：GET / PUT architecture + POST architecture/generate（幂等）。

章节蓝图是起草真正读的那一份（场景运行有生效的蓝图就用它，没有才现做一份）：作者可以看、改写，也可以让 AI 重新生成；
没有可用模型时生成回 409 + author_action（fail-closed）。作者改写的蓝图不因设计 / 绑定变化作废（B07-03）。

2026-10（round 2 · B2-5）：章节编排「AI 编排」的另外三条通道——方向（plan/candidates）、补全（plan/fill + plan/apply，
连同待补清单 plan/gaps）与 AI 体检（plan/review）——删了：模型账本里一次调用都没有，写操作记录里一次请求都没有；
出编排方向、补场景卡是构思 09 / 10 步的事（构思整理出来的场，补全的建议本来就全部被拒），整章挑毛病有成稿中心的
「AI 通读本章」。退役路由见 tests/test_retired_surface.py。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ChapterPlanService(session).generate_architecture(
            project_id,
            chapter_id,
            actor_ref=actor_ref_of(request),
        ),
    )
