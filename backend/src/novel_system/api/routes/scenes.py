from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest, INT64_MAX
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
from novel_system.db.models import (
    AuthorDraft,
    ChapterRunJob,
    FinalScene,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.archiver import Archiver
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_state import compute_author_state
from novel_system.services.candidate_selection import candidates_view, select_candidate
from novel_system.services.canonical_manuscripts import CanonicalSceneService
from novel_system.services.chapter_approval import is_chapter_approved, require_chapter_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.projects import ProjectService
from novel_system.services.reference_copy_gate import (
    check_reference_copy_for_scope,
    copy_block_author_action,
)
from novel_system.services.run_job_leases import STATUS_QUEUED
from novel_system.services.scene_budget import apply_topup
from novel_system.services.scene_notes import SceneNotesService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService
from novel_system.services.scene_run_jobs import SceneRunJobService, start_scene_run_job_worker
from novel_system.services.scene_workbench import (
    INCLUDE_DIAGNOSTICS,
    SceneWorkbenchService,
    attach_style_notices,
)
from novel_system.services.text_input import validate_user_text_payload
from novel_system.services.writer_briefs import normalize_scene_writer_brief

router = APIRouter(tags=["scenes"])
_LOGGER = logging.getLogger(__name__)
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
        action=lambda: _create_scene(session, body),
    )


def _create_scene(session: Session, payload: dict) -> dict:
    validate_user_text_payload(payload, field_prefix="scene")
    payload = {
        **payload,
        "writer_brief_json": normalize_scene_writer_brief(
            payload.get("writer_brief_json")
        ),
    }
    lifecycle = AuthorLifecycleService(session)
    chapter_id = payload.get("chapter_id")
    if not isinstance(chapter_id, str) or not chapter_id:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)

    chapter = lifecycle.require_active_chapter(chapter_id)

    scene = session.get(SceneCard, payload["scene_id"])
    created = scene is None
    effective_scene_seq = (
        payload.get("scene_seq")
        if payload.get("scene_seq") is not None
        else (
            scene.scene_seq
            if scene is not None
            else _next_scene_seq(session, chapter_id)
        )
    )
    _assert_scene_seq_available(
        session,
        scene_id=payload["scene_id"],
        chapter_id=chapter_id,
        scene_seq=int(effective_scene_seq),
    )
    if scene is None:
        require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=["scenes.create"],
            operation="scenes.upsert_create",
        )
        if payload.get("scene_seq") is None:
            payload = {
                **payload,
                "scene_seq": _next_scene_seq(session, chapter_id),
            }
        scene = SceneCard(**payload)
        session.add(scene)
        session.flush()
        changed = True
    else:
        if scene.trashed_flag == 1:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash")
        if payload["chapter_id"] != scene.chapter_id:
            raise DomainError(
                "SCENE_IDENTITY_IMMUTABLE",
                "an existing scene cannot be moved to another chapter",
                status_code=409,
            )
        if "project_id" in payload:
            requested_project_id = payload["project_id"]
            may_bind_from_chapter = (
                scene.project_id is None
                and requested_project_id is not None
                and requested_project_id == chapter.project_id
            )
            if requested_project_id != scene.project_id and not may_bind_from_chapter:
                raise DomainError(
                    "SCENE_IDENTITY_IMMUTABLE",
                    "an existing scene cannot be moved to another project",
                    status_code=409,
                )
        if "outline_plan_id" in payload:
            requested_outline_id = payload["outline_plan_id"]
            may_bind_from_chapter = (
                scene.outline_plan_id is None
                and requested_outline_id is not None
                and requested_outline_id == chapter.outline_plan_id
            )
            if (
                requested_outline_id != scene.outline_plan_id
                and not may_bind_from_chapter
            ):
                raise DomainError(
                    "SCENE_IDENTITY_IMMUTABLE",
                    "an existing scene cannot be rebound to another outline plan",
                    status_code=409,
                )
        if payload.get("scene_seq") is None:
            payload = {
                **payload,
                "scene_seq": scene.scene_seq,
            }
        changed_fields = [
            key
            for key, value in payload.items()
            if key not in {"scene_id", "chapter_id"} and getattr(scene, key) != value
        ]
        changed = require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=changed_fields,
            operation="scenes.upsert_update",
        )
        if changed:
            for key, value in payload.items():
                setattr(scene, key, value)

    state = session.get(SceneRunState, payload["scene_id"])
    should_create_state = state is None and (
        created or not is_chapter_approved(session, chapter)
    )
    if should_create_state:
        state = SceneRunState(scene_id=payload["scene_id"], scene_status="ready")
        session.add(state)
        changed = True
    session.flush()
    return {"scene_id": scene.scene_id, "changed": changed}


def _assert_scene_seq_available(
    session: Session,
    *,
    scene_id: str,
    chapter_id: str,
    scene_seq: int,
) -> None:
    conflict = session.execute(
        select(SceneCard.scene_id).where(
            SceneCard.chapter_id == chapter_id,
            SceneCard.scene_seq == scene_seq,
            SceneCard.trashed_flag == 0,
            SceneCard.scene_id != scene_id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise DomainError(
            "SCENE_SEQUENCE_CONFLICT",
            "another active scene already uses this scene_seq",
            status_code=409,
            details={
                "chapter_id": chapter_id,
                "scene_seq": scene_seq,
                "conflicting_scene_id": conflict,
            },
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

    response = mutate(
        request,
        session,
        payload={"job_id": job_id, "body": body},
        action=cancel,
    )
    return response


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
    """项目内全部场景运行态（管线真相）。

    起草台队列成员的后端派生源：换浏览器后 FE 据此恢复「哪些场进过管线」，
    localStorage 队列退化为这份真相的读缓存（贯通轮遗留项 ①）。
    只返回有运行态行且离开过 ready 的场——ready/无行 = 从未进管线，不参与恢复。
    """
    ProjectService(session).require_project(project_id)
    rows = session.execute(
        select(SceneRunState, SceneCard)
        .join(SceneCard, SceneCard.scene_id == SceneRunState.scene_id)
        .where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
        .order_by(SceneRunState.updated_at.desc())
    ).all()
    items = [
        {
            "scene_id": state.scene_id,
            "chapter_id": card.chapter_id,
            "scene_status": state.scene_status,
            # 治理 §5.3：列表恢复面也带作者可见态（枚举），FE 不再从 scene_status 猜
            "author_state": compute_author_state(session, state.scene_id, state)[
                "author_state"
            ],
            "total_attempt_count": state.total_attempt_count,
            "updated_at": state.updated_at,
        }
        for state, card in rows
        if state.scene_status != "ready"
    ]
    return respond(request, {"items": items, "count": len(items)})


@router.get("/api/v1/scenes/{scene_id}/status")
def scene_status(
    scene_id: str, request: Request, session: Session = Depends(get_session)
):
    AuthorLifecycleService(session).require_active_scene(scene_id)
    state = session.get(SceneRunState, scene_id)
    if state is None:
        # 经目录新建、从未 run 的有效场景没有运行态行——返回 ready 空态投影，
        # 与只读 workbench 一致；GET 不为查看动作补建持久行。
        return respond(
            request,
            {
                "scene_status": "ready",
                "current_bundle_id": None,
                "current_bundle_hash": None,
                "current_neutral_draft_row_id": None,
                "current_style_draft_row_id": None,
                "current_final_scene_row_id": None,
                "repeat_issue_key": None,
                "repeat_issue_count": 0,
                # 治理 §5.3：作者可见状态投影（React 只消费这层字段）
                **compute_author_state(session, scene_id, None),
            },
        )
    return respond(
        request,
        {
            "scene_status": state.scene_status,
            "current_bundle_id": state.current_bundle_id,
            "current_bundle_hash": state.current_bundle_hash,
            "current_neutral_draft_row_id": state.current_neutral_draft_row_id,
            "current_style_draft_row_id": state.current_style_draft_row_id,
            "current_final_scene_row_id": state.current_final_scene_row_id,
            "repeat_issue_key": state.repeat_issue_key,
            "repeat_issue_count": state.repeat_issue_count,
            # 治理 §5.3：作者可见状态投影（React 只消费这层字段）
            **compute_author_state(session, scene_id, state),
        },
    )


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
    body = payload.model_dump(mode="json") if payload else {}
    raw_extras = {
        "extra_tokens": body.get("extra_tokens", 0),
        "extra_attempts": body.get("extra_attempts", 0),
        "extra_provider_attempts": body.get("extra_provider_attempts", 0),
    }
    invalid_fields = {
        field: value
        for field, value in raw_extras.items()
        if type(value) is not int or value < 0 or value > INT64_MAX
    }
    if invalid_fields or not any(
        value > 0 for value in raw_extras.values() if type(value) is int
    ):
        raise DomainError(
            "INVALID_BUDGET_TOPUP",
            "topup values must be non-negative integers and at least one must be positive",
            status_code=422,
            details={**raw_extras, "max_lifecycle_budget": INT64_MAX},
        )
    extra_tokens = raw_extras["extra_tokens"]
    extra_attempts = raw_extras["extra_attempts"]
    extra_provider_attempts = raw_extras["extra_provider_attempts"]
    reason = str(body.get("reason") or "").strip()[:300]

    def _topup(session: Session) -> dict[str, Any]:
        AuthorLifecycleService(session).require_active_scene(scene_id)
        return apply_topup(
            session,
            scene_id,
            extra_tokens=extra_tokens,
            extra_attempts=extra_attempts,
            extra_provider_attempts=extra_provider_attempts,
            reason=reason,
            actor_ref=actor_ref,
        )

    return mutate(
        request,
        session,
        payload={
            "scene_id": scene_id,
            "extra_tokens": extra_tokens,
            "extra_attempts": extra_attempts,
            "extra_provider_attempts": extra_provider_attempts,
            "reason": reason,
        },
        action=lambda: _topup(session),
    )


def _author_draft_plain_text(html: str | None) -> str:
    """author-draft 存 HTML（<p> 分段）；归档正文按段落还原为纯文本。"""
    import re

    if not html:
        return ""
    text = re.sub(r"</p\s*>|<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


@router.post("/api/v1/scenes/{scene_id}/adopt-current")
def adopt_current_scene(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: AdoptCurrentRequest | None = Body(default=None),
):
    """治理 §5.2：作者采纳归档的单一服务入口。

    前端「归档/置 done」动作必须打到这里——携带 exact_author_draft 时，
    作者稿 CAS 保存与 CanonicalScene 提升在同一个幂等事务中完成，浏览器正文
    不再与 FinalScene 分裂。兼容调用未携带 exact_author_draft 时，内容源优先级
    仍为未归档 current_final_scene → 管线草稿（latest_valid > style > neutral）→
    author-draft 人工稿兜底。守卫：无任何有效稿 409 NO_VALID_DRAFT；
    确定性来源安全扫描命中 409 SOURCE_SAFETY_BLOCKED（草稿保留可重试，
    设计红线 8：来源安全未通过可保存草稿但不能标记为已安全归档）。
    """
    actor_ref = actor_ref_of(request)
    body = payload.model_dump(mode="json") if payload is not None else {}
    accepted_warning_codes = body.get("accepted_warning_codes") or []
    exact_author_draft = body.get("exact_author_draft")

    def _adopt(session: Session) -> dict[str, Any]:
        from uuid import uuid4

        scene = AuthorLifecycleService(session).require_active_scene(scene_id)
        state = session.get(SceneRunState, scene_id)
        if state is None:
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            session.add(state)
            session.flush()

        # 兼容旧调用的已归档幂等返回。精确作者稿可能是在已归档版本之上的
        # 新修订，必须继续走 revision + FinalScene 双 CAS，不能在这里吞掉。
        # （如 C2 真实库中 failed@soft_qc_ready 的历史残留，作者重点一次即自愈）
        if (
            exact_author_draft is None
            and state.scene_status == "archived"
            and state.current_final_scene_row_id
        ):
            current_final = session.get(FinalScene, state.current_final_scene_row_id)
            if current_final is None or current_final.scene_id != scene_id:
                raise DomainError(
                    "FINAL_SCENE_NOT_FOUND",
                    "archived scene points to a missing final manuscript",
                    status_code=409,
                    details={"scene_id": scene_id},
                )
            current_memory = (
                session.execute(
                    select(SceneMemory).where(
                        SceneMemory.scene_id == scene_id,
                        SceneMemory.final_scene_row_id == current_final.row_id,
                        SceneMemory.active_flag == 1,
                    )
                )
                .scalars()
                .first()
            )
            confirmation = Archiver(session).archive_final_scene(
                scene_id,
                current_final.row_id,
                carry_notes_json=(
                    list(current_memory.carry_notes_json or [])
                    if current_memory is not None
                    else []
                ),
                author_confirmed_final=True,
                accepted_warning_codes=accepted_warning_codes,
                fidelity_source="adopt",
            )
            residue_finalized = SceneRunCheckpointService(
                session
            ).finalize_after_author_archive(scene_id)
            return {
                "scene_id": scene_id,
                "scene_status": "archived",
                "final_scene_row_id": state.current_final_scene_row_id,
                "already_archived": True,
                "safe_to_archive": confirmation["safe_to_archive"],
                "literary_warnings_unresolved": confirmation[
                    "literary_warnings_unresolved"
                ],
                "author_confirmed_final": confirmation["author_confirmed_final"],
                "finality": confirmation["finality"],
                "run_residue_finalized": residue_finalized,
                "author_state": compute_author_state(session, scene_id, state),
            }

        # Wave 2（治理 §5.3/§5.4）：只有真实 Q0/Q1 能阻断归档——投影为 hard_blocked
        # （当前 QC 报告存在 verified Q0/Q1 分级条目）时拒绝采纳，正文保留（§7.2）。
        projection = compute_author_state(session, scene_id, state)
        if projection["author_state"] == "hard_blocked":
            raise DomainError(
                "HARD_BLOCKED",
                "verified Q0/Q1 findings block adoption — resolve or revise before archiving",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "blocking_findings": projection["blocking_findings"],
                },
            )
        # Wave 3（§5.5 完成门）：关键场景未终选前不可归档——adopt 旁路同样封死
        if projection["author_state"] == "awaiting_author_choice":
            raise DomainError(
                "SELECTION_REQUIRED",
                "author terminal selection is required before archiving this critical scene",
                status_code=409,
                details={"scene_id": scene_id},
            )

        # 浏览器精确稿路径：先以 base_revision_no 保存请求中的确定正文，再把
        # 保存后的同一修订提升为 FinalScene。两个动作共享当前数据库事务；保存、
        # 安全门、聚合或归档任一步失败都会整体回滚。
        if exact_author_draft is not None:
            draft_id = exact_author_draft["draft_id"]
            draft = session.get(AuthorDraft, draft_id)
            if draft is None:
                raise DomainError(
                    "AUTHOR_DRAFT_NOT_FOUND",
                    "author draft not found",
                    status_code=404,
                    details={"draft_id": draft_id},
                )
            if draft.object_type != "scene" or draft.object_id != scene_id:
                raise DomainError(
                    "AUTHOR_DRAFT_SCENE_MISMATCH",
                    "author draft does not belong to the scene being adopted",
                    status_code=409,
                    details={
                        "draft_id": draft_id,
                        "draft_object_type": draft.object_type,
                        "draft_object_id": draft.object_id,
                        "scene_id": scene_id,
                    },
                )
            saved = AuthorDraftService(session).save(
                draft_id,
                {
                    "content": exact_author_draft["content"],
                    "base_revision_no": exact_author_draft["base_revision_no"],
                    "note": "atomic scene adoption",
                },
                actor_ref=actor_ref,
            )
            saved_draft = saved.get("draft") or {}
            saved_revision_no = saved_draft.get("revision_no")
            if not isinstance(saved_revision_no, int):
                raise DomainError(
                    "AUTHOR_DRAFT_SAVE_INCOMPLETE",
                    "saved author draft did not return a revision number",
                    status_code=500,
                    details={"draft_id": draft_id},
                )
            promoted = CanonicalSceneService(session).promote_author_draft(
                draft_id,
                {
                    "base_revision_no": saved_revision_no,
                    "expected_current_final_scene_row_id": exact_author_draft[
                        "expected_current_final_scene_row_id"
                    ],
                    # Saving exact author text proves which revision was chosen;
                    # it does not prove that story facts stayed unchanged.
                    "narrative_effect": "requires_reconcile",
                    "accepted_warning_codes": accepted_warning_codes,
                },
                actor_ref=actor_ref,
                fidelity_source="adopt",
            )
            session.flush()
            session.refresh(draft)
            promoted["author_draft"] = AuthorDraftService.serialize_draft(
                draft,
                current_final_scene_row_id=promoted["final_scene_row_id"],
            )
            promoted["exact_author_draft"] = True
            promoted["author_state"] = compute_author_state(session, scene_id, state)
            return promoted

        # 1) 内容源解析
        final: FinalScene | None = None
        if state.current_final_scene_row_id:
            row = session.get(FinalScene, state.current_final_scene_row_id)
            if row is not None and (row.content or "").strip():
                final = row
        source_draft_row_id: str | None = None
        content: str | None = None
        source_bundle_id: str | None = None
        source_bundle_hash: str | None = None
        if final is None:
            for row_id in (
                state.latest_valid_draft_row_id,
                state.current_style_draft_row_id,
                state.current_neutral_draft_row_id,
            ):
                if not row_id:
                    continue
                draft = session.get(SceneDraft, row_id)
                if draft is not None and (draft.content or "").strip():
                    source_draft_row_id = row_id
                    content = draft.content
                    source_bundle_id = draft.source_bundle_id
                    source_bundle_hash = draft.source_bundle_hash
                    break
            if content is None:
                author_draft = (
                    session.execute(
                        select(AuthorDraft).where(
                            AuthorDraft.object_type == "scene",
                            AuthorDraft.object_id == scene_id,
                            AuthorDraft.status == "current",
                        )
                    )
                    .scalars()
                    .first()
                )
                text = (
                    _author_draft_plain_text(author_draft.content)
                    if author_draft
                    else ""
                )
                if text.strip():
                    content = text
                    source_bundle_id = f"author_draft:{author_draft.draft_id}"
                    source_bundle_hash = f"author_draft_rev_{author_draft.revision_no}"
            if content is None:
                raise DomainError(
                    "NO_VALID_DRAFT",
                    "no valid draft content to adopt — generate or write the scene first",
                    status_code=409,
                    details={"scene_id": scene_id},
                )

        # 2) 唯一抄袭门（Q0 红线；风格参考 v3）：与绑定的参考书连续 ≥12 字相同或含受保护专名即拦。
        # 比对 bundle 冻结的绑定与这一场当前的活动绑定；归档时成稿门再过一遍同一道门（同一稿命中缓存）。
        target_content = final.content if final is not None else (content or "")
        bundle = (
            session.get(SceneBundle, state.current_bundle_id)
            if state.current_bundle_id
            else None
        )
        copy_check = check_reference_copy_for_scope(
            session,
            target_content,
            scope=scene,
            bundle_snapshot=bundle.frozen_snapshot_json if bundle else None,
        )
        scan = copy_check.audit()
        if copy_check.blocked:
            raise DomainError(
                "SOURCE_SAFETY_BLOCKED",
                "reference copy gate blocked adoption — draft is kept and can be revised",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "reference_copy": scan,
                    "author_action": copy_block_author_action(
                        copy_check, target_view="writer", target_ref=f"scene:{scene_id}"
                    ),
                },
            )

        # 3) FinalScene 建行或提升，经归档事务统一置权威态
        if final is None:
            final = FinalScene(
                row_id=f"final_scene_{scene_id}_adopt_{uuid4().hex[:10]}",
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                content=content or "",
                source_bundle_id=source_bundle_id or "author_adopt",
                source_bundle_hash=source_bundle_hash or "author_adopt",
            )
            session.add(final)
            session.flush()
        state.current_final_scene_row_id = final.row_id
        if source_draft_row_id:
            state.latest_valid_draft_row_id = source_draft_row_id

        carry_notes: list[dict[str, Any]] = [
            {"kind": "author_adoption", "actor_ref": actor_ref}
        ]
        quality_warnings = [
            item
            for item in projection.get("quality_warnings") or []
            if isinstance(item, dict)
        ]
        if quality_warnings:
            # Wave 2（Wave 2 项 7）：采纳带 Q2/Q3 警告的稿 = 作者显式接受，留审计
            carry_notes.append(
                {
                    "kind": "quality_warning_acceptance",
                    "actor_ref": actor_ref,
                    "accepted": [
                        {
                            "issue_key": item.get("issue_key") or item.get("kind"),
                            "quality_level": item.get("quality_level"),
                        }
                        for item in quality_warnings[:10]
                    ],
                }
            )
        archive_result = Archiver(session).archive_final_scene(
            scene_id,
            final.row_id,
            carry_notes_json=carry_notes,
            author_confirmed_final=True,
            accepted_warning_codes=accepted_warning_codes,
            fidelity_source="adopt",
        )
        # C2 状态一致性债务：归档后无主执行残留（failed@soft_qc_ready 等）
        # 在同一事务内收敛为 completed/archived，运维/展示不再被误导
        run_residue_finalized = SceneRunCheckpointService(
            session
        ).finalize_after_author_archive(scene_id)
        return {
            "scene_id": scene_id,
            "scene_status": archive_result["scene_status"],
            "final_scene_row_id": final.row_id,
            "scene_memory_row_id": archive_result["scene_memory_row_id"],
            "safe_to_archive": archive_result["safe_to_archive"],
            "literary_warnings_unresolved": archive_result[
                "literary_warnings_unresolved"
            ],
            "author_confirmed_final": archive_result["author_confirmed_final"],
            "finality": archive_result["finality"],
            "source_safety_scan": scan,
            "run_residue_finalized": run_residue_finalized,
            "author_state": compute_author_state(session, scene_id, state),
        }

    return mutate(
        request,
        session,
        payload={"scene_id": scene_id, **body},
        action=lambda: _adopt(session),
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


# WP4.1:本场参考窗口从哪几步的尝试回读,按优先级——风格稿(style_draft)是成稿前最后一次带
# 样例的通道;风格直起下中性步位的首稿同样带窗口;近终稿重写稿作兜底。
def _next_scene_seq(session: Session, chapter_id: str) -> int:
    return AuthorLifecycleService(session).next_scene_append_seq(chapter_id)
