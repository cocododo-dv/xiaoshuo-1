"""场景接口：作者笔记、删场、v1 场景卡建 / 改、运行（同步 run/full · 后台任务 · 取消 · 查询）、运行态与状态投影、
关键场景的候选终选与选后续跑、生命周期预算追加、采纳归档、AI 起草台的工作台载荷。

路由只做请求校验、幂等与信封；业务都在服务里（B12-01）：工作台载荷 ``services/scene_workbench.py``、候选终选
``services/candidate_selection.py``、采纳归档 ``services/scene_adoption.py``、v1 场景卡 ``services/scene_upsert.py``、
预算追加 ``services/scene_budget.py``、运行态视图 ``services/author_state.py``。

``Orchestrator`` 与 ``start_scene_run_job_worker`` 是这个模块上的名字，由这里调用——测试在这里替换它们。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.scenes import (
    AdoptCurrentRequest,
    SceneAuthorNotesSaveRequest,
    SceneBudgetTopupRequest,
    SceneIdsRequest,
    SceneRunCancelRequest,
    SceneRunCommandRequest,
    SceneRunJobRequest,
    SceneUpsertRequest,
    StyleCandidateSelectRequest,
)
from novel_system.api.response import respond
from novel_system.db.models import ChapterRunJob
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_state import project_run_states, scene_status_payload
from novel_system.services.candidate_selection import candidates_view, select_candidate
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.run_job_leases import STATUS_QUEUED
from novel_system.services.scene_adoption import adopt_current
from novel_system.services.scene_budget import apply_topup, validated_topup
from novel_system.services.scene_notes import SceneNotesService
from novel_system.services.scene_run_jobs import (
    SceneRunJobService,
    start_scene_run_job_worker,
)
from novel_system.services.scene_upsert import upsert_scene
from novel_system.services.scene_workbench import (
    INCLUDE_DIAGNOSTICS,
    SceneWorkbenchService,
    attach_style_notices,
)
from novel_system.services.writer_briefs import normalize_scene_writer_brief

router = APIRouter(tags=["scenes"])


@router.get("/api/v1/scenes/{scene_id}/author-notes")
def get_scene_author_notes(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    result = SceneNotesService(session).get(scene_id)
    return respond(request, result)


@router.patch("/api/v1/scenes/{scene_id}/author-notes")
def save_scene_author_notes(
    scene_id: str,
    payload: SceneAuthorNotesSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return mutate(
        request,
        session,
        payload={"scene_id": scene_id, "body": body},
        action=lambda: SceneNotesService(session).save(
            scene_id,
            body["notes"],
            base_revision_no=body["base_revision_no"],
        ),
    )


@router.post("/api/v1/scenes/trash")
def trash_scenes(
    payload: SceneIdsRequest, request: Request, session: Session = Depends(get_session)
):
    body = payload.model_dump(mode="json")
    actor_ref = actor_ref_of(request)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: AuthorLifecycleService(session).trash_scenes(
            body["scene_ids"], actor_ref
        ),
    )


@router.post("/api/v1/scenes")
def create_scene(
    payload: SceneUpsertRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(exclude_unset=True)
    # Domain validation precedes the durable idempotency claim.  A malformed
    # brief therefore cannot reserve a key needed by the corrected request.
    body["writer_brief_json"] = normalize_scene_writer_brief(
        body.get("writer_brief_json")
    )
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: upsert_scene(session, body),
    )


def _parse_run_policy(payload: dict | None) -> str:
    # 「auto」从没有调用方传过、编排器也只按 reliable 处理，已不再收（B01-09）
    run_policy = (
        str((payload or {}).get("run_policy") or "reliable").strip() or "reliable"
    )
    if run_policy not in {"reliable", "strict"}:
        raise DomainError(
            "INVALID_RUN_POLICY",
            "run_policy must be one of reliable|strict",
            status_code=422,
            details={"run_policy": run_policy},
        )
    return run_policy


def _reject_manual_checkpoint_controls(payload: dict | None) -> None:
    supplied = sorted({"from_step", "resume"}.intersection((payload or {}).keys()))
    if supplied:
        raise DomainError(
            "RUN_CHECKPOINT_CONTROL_FORBIDDEN",
            "scene runs resume only from the server-owned durable checkpoint",
            status_code=422,
            details={"unsupported_fields": supplied},
        )


@router.post("/api/v1/scenes/{scene_id}/run/full")
def run_scene(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: SceneRunCommandRequest | None = Body(default=None),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    AuthorLifecycleService(session).require_active_scene(scene_id)
    _reject_manual_checkpoint_controls(body)
    # FE-ALIGN G3：作者改写指令随请求下发（注入风格生成提示词；幂等键随 note 变化）
    author_note = normalize_author_note(body.get("author_note"))
    # Wave 2（治理 §6.3）：run_policy 请求级参数（reliable|strict；列属 Wave 3）
    run_policy = _parse_run_policy(body)
    return mutate(
        request,
        session,
        payload={
            "scene_id": scene_id,
            **({"author_note": author_note} if author_note else {}),
            **({"run_policy": run_policy} if run_policy != "reliable" else {}),
        },
        action=lambda lease: attach_style_notices(
            session,
            scene_id,
            Orchestrator(session).run_scene(
                scene_id,
                author_note=author_note,
                run_policy=run_policy,
                execution_id=lease.execution_id,
                lease_renewer=lease.renew,
            ),
        ),
    )


@router.post("/api/v1/scenes/{scene_id}/run/jobs")
def create_scene_run_job(
    scene_id: str,
    request: Request,
    start: bool = True,
    session: Session = Depends(get_session),
    payload: SceneRunJobRequest | None = Body(default=None),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    _reject_manual_checkpoint_controls(body)

    def create_job() -> dict:
        service = SceneRunJobService(session)
        budget_resume_parent_execution_id = (
            service.resolve_budget_resume_execution_id(scene_id)
            if body.get("resume_budget") is True
            else None
        )
        job = service.create_job(
            scene_id,
            actor_ref=actor_ref,
            author_note=body.get("author_note"),
            run_policy=_parse_run_policy(body),
            budget_resume_parent_execution_id=budget_resume_parent_execution_id,
        )
        return service.serialize_job(job)

    def dispatch_if_queued(result: dict) -> None:
        # 提交之后派发（B12-07）；同一个幂等键重放时也走这里：任务还在排队（提交与派发之间进程退出、--reload）
        # 就由这次重试接上，已经在跑 / 已结束的不动。工人的认领是条件写，重复派发无害。
        job_id = str((result or {}).get("job_id") or "")
        if start and job_id and _run_job_status(session, job_id) == STATUS_QUEUED:
            start_scene_run_job_worker(job_id)

    return mutate(
        request,
        session,
        payload={"scene_id": scene_id, "start": start, "body": body},
        action=create_job,
        after_commit=dispatch_if_queued,
    )


def _run_job_status(session: Session, job_id: str) -> str | None:
    """任务当前在库里的状态（列查询，不读会话里可能过期的对象）。"""
    return session.scalar(select(ChapterRunJob.status).where(ChapterRunJob.job_id == job_id))


@router.get("/api/v1/run-jobs/{job_id}")
def get_run_job(job_id: str, request: Request, session: Session = Depends(get_session)):
    service = SceneRunJobService(session)
    job = service.get_job(job_id)
    return respond(request, service.serialize_job(job))


@router.post("/api/v1/run-jobs/{job_id}/cancel")
def cancel_run_job(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: SceneRunCancelRequest | None = Body(default=None),
):
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}

    def cancel() -> dict:
        service = SceneRunJobService(session)
        job = service.request_cancel(
            job_id, actor_ref=actor_ref, reason=body.get("reason")
        )
        return service.serialize_job(job)

    return mutate(
        request,
        session,
        payload={"job_id": job_id, "body": body},
        action=cancel,
    )


@router.get("/api/v1/scenes/{scene_id}/run/jobs/latest")
def get_latest_scene_run_job(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    AuthorLifecycleService(session).require_active_scene(scene_id)
    service = SceneRunJobService(session)
    return respond(request, service.serialize_job(service.latest_job(scene_id)))


@router.get("/api/v1/scene-run-states")
def list_scene_run_states(
    project_id: str, request: Request, session: Session = Depends(get_session)
):
    """项目内离开过 ready 的场景运行态：起草台换浏览器后据此恢复队列（见 ``author_state.project_run_states``）。"""
    return respond(request, project_run_states(session, project_id))


@router.get("/api/v1/scenes/{scene_id}/status")
def scene_status(
    scene_id: str, request: Request, session: Session = Depends(get_session)
):
    return respond(request, scene_status_payload(session, scene_id))


@router.get("/api/v1/scenes/{scene_id}/style-candidates")
def get_scene_style_candidates(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    include_scores: bool = False,
    diagnostic: bool = False,
):
    """候选终选取数——默认盲化视图（``include_scores`` 附分数不重排；``diagnostic`` 取无门的旧诊断形状）。"""
    return respond(
        request,
        candidates_view(session, scene_id, include_scores=include_scores, diagnostic=diagnostic),
    )


@router.post("/api/v1/scenes/{scene_id}/style-candidates/{row_id}/select")
def select_style_candidate(
    scene_id: str,
    row_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: StyleCandidateSelectRequest | None = Body(default=None),
):
    """作者终选——一次写入 + 锁定：同选幂等返回，换一份 409 SELECTION_LOCKED（想换一稿就重新起草这一场）。"""
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return mutate(
        request,
        session,
        payload={"scene_id": scene_id, "row_id": row_id, **body},
        action=lambda: select_candidate(session, scene_id, row_id, actor_ref=actor_ref, body=body),
    )


@router.post("/api/v1/scenes/{scene_id}/resume-after-selection")
def resume_after_selection(
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """Wave 3（§5.5/§6.3）：作者终选后从批判修订/QC 续跑到归档。"""
    AuthorLifecycleService(session).require_active_scene(scene_id)
    return mutate(
        request,
        session,
        payload={"scene_id": scene_id},
        action=lambda lease: Orchestrator(session).resume_after_selection(
            scene_id,
            execution_id=lease.execution_id,
            lease_renewer=lease.renew,
        ),
    )


@router.post("/api/v1/scenes/{scene_id}/budget/topup")
def topup_scene_budget(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: SceneBudgetTopupRequest | None = Body(default=None),
):
    """作者显式追加 token/业务尝试/provider 尝试预算；唯一扩容入口，留审计。"""
    actor_ref = actor_ref_of(request)
    topup = validated_topup(payload.model_dump(mode="json") if payload else {})

    def _topup() -> dict:
        AuthorLifecycleService(session).require_active_scene(scene_id)
        return apply_topup(session, scene_id, **topup, actor_ref=actor_ref)

    return mutate(
        request,
        session,
        payload={"scene_id": scene_id, **topup},
        action=_topup,
    )


@router.post("/api/v1/scenes/{scene_id}/adopt-current")
def adopt_current_scene(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: AdoptCurrentRequest | None = Body(default=None),
):
    """治理 §5.2：作者采纳归档的单一服务入口（见 ``services/scene_adoption.py``）。"""
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json") if payload is not None else {}
    return mutate(
        request,
        session,
        payload={"scene_id": scene_id, **body},
        action=lambda: adopt_current(
            session,
            scene_id,
            actor_ref=actor_ref,
            accepted_warning_codes=body.get("accepted_warning_codes") or [],
            exact_author_draft=body.get("exact_author_draft"),
        ),
    )


@router.get("/api/v1/scenes/{scene_id}/workbench")
def scene_workbench(
    scene_id: str,
    request: Request,
    include: Literal["diagnostics"] | None = None,
    session: Session = Depends(get_session),
):
    """AI 起草台一场的工作台载荷；``?include=diagnostics`` 连同诊断部分（见 ``services/scene_workbench.py``）。"""
    return respond(
        request,
        SceneWorkbenchService(session).payload(scene_id, diagnostics=include == INCLUDE_DIAGNOSTICS),
    )
