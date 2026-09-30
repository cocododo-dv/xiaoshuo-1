from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.response import respond
from novel_system.services.chapter_manuscripts import ChapterManuscriptService

router = APIRouter(tags=["chapter-manuscripts"])


@router.get("/api/v1/chapter-manuscripts")
def list_chapter_manuscripts(request: Request, session: Session = Depends(get_session)):
    return respond(request, {"items": ChapterManuscriptService(session).list_manuscripts()})


@router.get("/api/v1/chapter-manuscripts/{chapter_id}")
def chapter_manuscript_detail(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    return respond(request, ChapterManuscriptService(session).manuscript_detail(chapter_id))
