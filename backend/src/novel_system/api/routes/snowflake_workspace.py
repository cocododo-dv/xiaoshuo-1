from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.project_requests import ProjectCreateRequest
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest
from novel_system.api.response import ok
from novel_system.api.snowflake_requests import (
    SnowflakeAcceptStaleScenesRequest,
    SnowflakeAcceptStaleStepRequest,
    SnowflakeAssistantRequest,
    SnowflakeDirectionBriefRequest,
    SnowflakeFeCandidatesRequest,
    SnowflakeOrphanResolveRequest,
    SnowflakeResyncRequest,
    SnowflakeSceneTriageSuggestRequest,
    SnowflakeStepApproveRequest,
    SnowflakeStepGenerateRequest,
    SnowflakeStepRestoreRequest,
)
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

router = APIRouter(tags=["snowflake-workspace"])


def _actor(request: Request) -> str:
    return getattr(request.state, "operator_ref", None) or "operator"


@router.get("/api/v2/projects")
def list_snowflake_workspace_projects(request: Request, session: Session = Depends(get_session)):
    return ok(
        SnowflakeWorkspaceService(session).list_projects(),
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v2/projects")
def create_snowflake_workspace_project(
    payload: ProjectCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects",
        payload=body,
        action=lambda: SnowflakeWorkspaceService(session).create_project(body),
    )


@router.get("/api/v2/projects/{project_id}/snowflake-workspace")
def get_snowflake_workspace(project_id: str, request: Request, session: Session = Depends(get_session)):
    return ok(
        SnowflakeWorkspaceService(session).workspace(project_id),
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate")
def generate_workspace_step(
    project_id: str,
    step_key: str,
    request: Request,
    payload: SnowflakeStepGenerateRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).generate_step(project_id, step_key, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/fe-candidates")
def generate_workspace_step_fe_candidates(
    project_id: str,
    step_key: str,
    request: Request,
    payload: SnowflakeFeCandidatesRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/fe-candidates",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).fe_step_candidates(project_id, step_key, body),
    )


@router.patch("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}")
def update_workspace_step(
    project_id: str,
    step_key: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).update_step(project_id, step_key, body),
    )


@router.get("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/history")
def get_workspace_step_history(
    project_id: str,
    step_key: str,
    request: Request,
    include_draft: bool = False,
    session: Session = Depends(get_session),
):
    return ok(
        SnowflakeWorkspaceService(session).step_history(project_id, step_key, include_draft=include_draft),
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/restore")
def restore_workspace_step(
    project_id: str,
    step_key: str,
    request: Request,
    payload: SnowflakeStepRestoreRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/restore",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).restore_step(project_id, step_key, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve")
def approve_workspace_step(
    project_id: str,
    step_key: str,
    request: Request,
    payload: SnowflakeStepApproveRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).approve_step(
            project_id, step_key, body, actor_ref=_actor(request)
        ),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/accept-stale")
def accept_workspace_stale_step(
    project_id: str,
    step_key: str,
    request: Request,
    payload: SnowflakeAcceptStaleStepRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/accept-stale",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).accept_stale_step(
            project_id,
            step_key,
            body,
            actor_ref=_actor(request),
        ),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/assistant")
def request_workspace_assistant(
    project_id: str,
    request: Request,
    payload: SnowflakeAssistantRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/assistant",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).request_assistant(project_id, body),
    )


@router.put("/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/direction-brief")
def update_workspace_direction_brief(
    project_id: str,
    step_key: str,
    request: Request,
    payload: SnowflakeDirectionBriefRequest | None = None,
    session: Session = Depends(get_session),
):
    """阶段 T：作者编辑本步意图要点（撤下 / 改写 / 加条 / 切换范围 / 恢复 / 是否继承上游）。"""
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="PUT",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/direction-brief",
        payload={"project_id": project_id, "step_key": step_key, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).update_direction_brief(project_id, step_key, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/scene-triage/suggest")
def suggest_workspace_scene_triage(
    project_id: str,
    request: Request,
    payload: SnowflakeSceneTriageSuggestRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/scene-triage/suggest",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).suggest_scene_triage(project_id, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/scene-triage")
def save_workspace_scene_triage(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/scene-triage",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).save_scene_triage(project_id, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/scenes/accept-stale")
def accept_workspace_stale_scenes(
    project_id: str,
    request: Request,
    payload: SnowflakeAcceptStaleScenesRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/scenes/accept-stale",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).accept_stale_scenes(
            project_id,
            body,
            actor_ref=_actor(request),
        ),
    )


@router.patch("/api/v2/projects/{project_id}/snowflake-workspace/scenes/{scene_plan_id}")
def update_workspace_scene_plan(
    project_id: str,
    scene_plan_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/scenes/{scene_plan_id}",
        payload={"project_id": project_id, "scene_plan_id": scene_plan_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).update_scene_plan(project_id, scene_plan_id, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/scene-triage/{triage_id}/apply")
def apply_workspace_scene_triage_repair(
    project_id: str,
    triage_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/scene-triage/{triage_id}/apply",
        payload={"project_id": project_id, "triage_id": triage_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).apply_scene_triage_repair(project_id, triage_id),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/materialize")
def materialize_workspace_outline(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload or {}
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).materialize(project_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview")
def preview_chapter_plan(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    """分章预览：**分章方案**只读推演，不落库。策略 spine_anchor / even / keep_current。

    「不落库」指的是这次推演算出来的归属——作者没在面板上确认之前，一场都不会按它改。

    但这个端点不是零副作用的：``ensure_chapter_plans`` 会为还没有章表行的项目补建
    ``SnowflakeChapterPlan``，并把**已经是既成事实**的归属绑上去（目录里的 ChapterGoal
    或场景行自带的 chapter_id —— 那是系统已经知道的事，不是待决策项），所以要 commit。
    历史项目第一次打开面板时，因此会看到归属从「未分章」变成实际章数，这是补录不是决策。
    """
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeChapteringService(session).preview(project_id, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/propose")
def propose_chapter_plan(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    """阶段 K：按场景列表提议并落一版章表（Ingermanson：章是列完场之后的包装决定）。

    载荷：``target_chapter_count``（缺省用作品设置）、``scenes_per_chapter``（缺省 3）、``replace``
    （已有章表时必须为 true）。回包是 keep_current 策略的分章预览，外加 created_chapter_count。
    """
    body = payload or {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/propose",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeChapteringService(session).propose_from_scenes(
            project_id, body, actor_ref=request.state.operator_ref
        ),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/suggest")
def suggest_chapter_plan(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    """让 LLM 给一份分章建议（只读，不落库）。

    fail-closed：LLM 没配好就 409 + author_action。作者点的是「让 AI 建议」，拿一份
    规则算出来的东西冒充建议是撒谎 —— 规则分章本来就以 spine_anchor 策略摆在面板上。
    """
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/suggest",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeChapteringService(session).suggest(project_id, body),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/titles")
def suggest_chapter_titles(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    """AI 起章名（阶段 W，只读，不落库）：给系统起的占位章名（空 / 「第 N 章」）各起一个名字、写一句章摘要。

    载荷 ``chapters``：面板此刻的章表 ``[{row_uid, title, act, spine, scene_plan_ids}]``（含未落库的 ``new:N``）；
    不带就按已保存的分章。``rename_all=true`` 连作者起过名字的章也重起。
    fail-closed：LLM 没配好 409 + author_action；模型没给出可用章名 502 ``SNOWFLAKE_CHAPTER_TITLES_EMPTY``。
    """
    body = payload or {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/titles",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeChapteringService(session).suggest_titles(project_id, body),
    )


@router.patch("/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan")
def save_chapter_plan(
    project_id: str,
    payload: BoundedJsonObject | None,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload or {}

    def save() -> dict:
        saved = SnowflakeChapteringService(session).save(
            project_id,
            body,
            actor_ref=_actor(request),
        )
        return {**saved, "workspace": SnowflakeWorkspaceService(session).workspace(project_id)}

    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan",
        payload={"project_id": project_id, "body": body},
        action=save,
    )


@router.post(
    "/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{scene_plan_id}/resolve"
)
def resolve_orphaned_scene(
    project_id: str,
    scene_plan_id: str,
    request: Request,
    payload: SnowflakeOrphanResolveRequest | None = None,
    session: Session = Depends(get_session),
):
    """处置一个孤儿场：``action`` = discard（正文也进回收站）/ keep（正文留在目录里）。

    没有这个端点时分章面板的 blocker 是死结——它让作者「先决定是一并删除还是保留」，
    但界面上不存在这两个决定，「确认分章」按钮从此再也点不动。
    """
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    action = str(body.get("action") or "").strip()

    def resolve() -> dict:
        resolved = SnowflakeChapteringService(session).resolve_orphan(
            project_id,
            scene_plan_id,
            action=action,
            actor_ref=_actor(request),
        )
        return {**resolved, "workspace": SnowflakeWorkspaceService(session).workspace(project_id)}

    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/orphaned-scenes/{scene_plan_id}/resolve",
        payload={"project_id": project_id, "scene_plan_id": scene_plan_id, "action": action},
        action=resolve,
    )


@router.get("/api/v2/projects/{project_id}/snowflake-workspace/resync-status")
def get_workspace_resync_status(project_id: str, request: Request, session: Session = Depends(get_session)):
    """阶段 X：写作台 / AI 起草台用的轻量读口——哪几场的场景卡落后于构思。"""
    return ok(
        SnowflakeWorkspaceService(session).resync_status(project_id),
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/resync")
def resync_workspace_scenes(
    project_id: str,
    request: Request,
    payload: SnowflakeResyncRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/resync",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).resync_materialized_scenes(
            project_id,
            body,
            actor_ref=_actor(request),
        ),
    )


@router.post("/api/v2/projects/{project_id}/snowflake-workspace/outline/approve")
def approve_workspace_outline(
    project_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
        payload={"project_id": project_id, "body": body},
        action=lambda: SnowflakeWorkspaceService(session).approve_outline(project_id),
    )
