from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import optional_idempotent_response
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.writer_deep_review import (
    ChapterReviewRequest,
    PassagePatchAcceptRequest,
    PassagePatchCreateRequest,
    PassagePatchRejectRequest,
    PassageReviewRequest,
    SceneDeepReviewPreferencesSaveRequest,
)
from novel_system.api.response import respond
from novel_system.services.scene_deep_review_preferences import SceneDeepReviewPreferencesService
from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_lookup import require_scene
from novel_system.services.writer_deep_review import WriterDeepReviewService

router = APIRouter(tags=["writer-deep-review"])


@router.get("/api/v1/scenes/{scene_id}/deep-review/preferences")
def get_scene_deep_review_preferences(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    payload = SceneDeepReviewPreferencesService(session).get(scene_id)
    return respond(request, payload)


@router.patch("/api/v1/scenes/{scene_id}/deep-review/preferences")
def save_scene_deep_review_preferences(
    scene_id: str,
    payload: SceneDeepReviewPreferencesSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v1/scenes/{scene_id}/deep-review/preferences",
        payload={"scene_id": scene_id, "body": body},
        action=lambda: SceneDeepReviewPreferencesService(session).save(
            scene_id,
            decision_log=body["decision_log"],
            ignored_issue_keys=body["ignored_issue_keys"],
            base_revision_no=body["base_revision_no"],
        ),
    )


@router.get("/api/v1/scenes/{scene_id}/deep-review")
def get_scene_deep_review(scene_id: str, request: Request, session: Session = Depends(get_session)):
    """写作台深改面板的载荷：统一的场景诊断（规则 / 节奏 / 评审 / AI 深评）。只读——不建 LLM 节点的服务。"""

    payload = SceneDiagnosisService(session).payload(scene_id)
    return respond(request, payload)


@router.post("/api/v1/scenes/{scene_id}/deep-review")
def run_scene_deep_review(
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/deep-review",
        payload={"scene_id": scene_id},
        action=lambda: WriterDeepReviewService(session).run_scene_review(scene_id, actor_ref=actor_ref),
    )


@router.post("/api/v1/scenes/{scene_id}/deep-review/passage")
def run_scene_passage_review(
    scene_id: str,
    payload: PassageReviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/deep-review/passage",
        payload={"scene_id": scene_id, "body": body},
        action=lambda: WriterDeepReviewService(session).run_passage_review(
            scene_id,
            signal_id=body.get("signal_id"),
            paragraph_index=body.get("paragraph_index"),
            paragraph_start=body.get("paragraph_start"),
            paragraph_end=body.get("paragraph_end"),
            excerpt=body.get("excerpt"),
            question=body.get("question"),
            actor_ref=actor_ref,
        ),
    )


@router.get("/api/v1/projects/{project_id}/diagnosis-summary")
def get_project_diagnosis_summary(project_id: str, request: Request, session: Session = Depends(get_session)):
    """一本书每一场 / 每一章开着的发现数——视图挂载 / 换作品时读一次；之后的变化随各写入的响应回传
    （``diagnosis_rollup``），见 GET …/scenes/{id}/diagnosis-rollup。"""

    payload = SceneDiagnosisService(session).project_summary(project_id)
    return respond(request, payload)


@router.get("/api/v1/scenes/{scene_id}/diagnosis-rollup")
def get_scene_diagnosis_rollup(scene_id: str, request: Request, session: Session = Depends(get_session)):
    """这一场所在那一章的计数（章条目 + 章里每一场的条目）：起草台归档终稿之后前端据此更新角标，不必拉整本书。"""

    service = SceneDiagnosisService(session)
    scene = require_scene(session, scene_id, trashed_as_conflict=True)
    return respond(request, service.scene_rollup(scene))


@router.get("/api/v1/chapters/{chapter_id}/diagnosis-rollup")
def get_chapter_diagnosis_rollup(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    """这一章的计数（章条目 + 章里每一场的条目）：章运行 / 场景运行在服务端归档了终稿之后前端据此更新角标。"""

    payload = SceneDiagnosisService(session).chapter_rollup(chapter_id)
    return respond(request, payload)


@router.get("/api/v1/chapters/{chapter_id}/deep-review")
def get_chapter_deep_review(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    """成稿中心「AI 通读本章」的载荷：章级判断 + 各场的诊断计数 + 落到各场的通读发现。只读。"""

    payload = SceneDiagnosisService(session).chapter_payload(chapter_id)
    return respond(request, payload)


@router.post("/api/v1/chapters/{chapter_id}/deep-review")
def run_chapter_deep_review(
    chapter_id: str,
    request: Request,
    payload: ChapterReviewRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    scope = (payload.scope if payload is not None else None) or "all"
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/{chapter_id}/deep-review",
        payload={"chapter_id": chapter_id, "scope": scope},
        action=lambda: WriterDeepReviewService(session).run_chapter_review(chapter_id, actor_ref=actor_ref, scope=scope),
    )


@router.post("/api/v1/passages/patch-candidates")
def create_passage_patch_candidate(
    payload: PassagePatchCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/passages/patch-candidates",
        payload=body,
        action=lambda: WriterDeepReviewService(session).create_patch_candidate(body, actor_ref=actor_ref),
    )


@router.post("/api/v1/passage-patch-candidates/{patch_id}/accept")
def accept_passage_patch_candidate(
    patch_id: str,
    request: Request,
    payload: PassagePatchAcceptRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/passage-patch-candidates/{patch_id}/accept",
        payload={"patch_id": patch_id, "body": body},
        action=lambda: WriterDeepReviewService(session).accept_patch_candidate(
            patch_id, body, actor_ref=actor_ref
        ),
    )


@router.post("/api/v1/passage-patch-candidates/{patch_id}/reject")
def reject_passage_patch_candidate(
    patch_id: str,
    request: Request,
    payload: PassagePatchRejectRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/passage-patch-candidates/{patch_id}/reject",
        payload={"patch_id": patch_id, "body": body},
        action=lambda: WriterDeepReviewService(session).reject_patch_candidate(
            patch_id, body, actor_ref=actor_ref
        ),
    )
