from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.literary_quality import (
    LiteraryQualityAnalyzeTextRequest,
    LiteraryQualityChapterSetRequest,
)
from novel_system.api.response import respond
from novel_system.services.literary_quality import LiteraryQualityService
from novel_system.services.scene_diagnosis import SceneDiagnosisService


def _quality_service(session: Session) -> LiteraryQualityService:
    """文学质量视图与写作台深改面板读同一份参考书校准（2026-09-22 第三轮）。"""

    return LiteraryQualityService(session, rule_calibration_resolver=SceneDiagnosisService(session).rule_calibration_for_scene)

router = APIRouter(tags=["literary_quality"])


@router.get("/api/v1/literary-quality/overview")
def literary_quality_overview(
    request: Request,
    text_layer: str = "author_draft_preferred",
    chapter_id: str | None = None,
    risk_type: str | None = None,
    min_severity: str | None = None,
    project_id: str | None = None,
    session: Session = Depends(get_session),
):
    payload = _quality_service(session).overview(
        text_layer=text_layer,
        chapter_id=chapter_id,
        risk_type=risk_type,
        min_severity=min_severity,
        project_id=project_id,
    )
    return respond(request, payload)


@router.post("/api/v1/literary-quality/analyze-text")
def literary_quality_analyze_text(
    payload: LiteraryQualityAnalyzeTextRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: _quality_service(session).analyze_text(body),
    )


@router.post("/api/v1/literary-quality/chapter-set-review")
def literary_quality_chapter_set_review(
    payload: LiteraryQualityChapterSetRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: _quality_service(session).chapter_set_review(body),
    )
