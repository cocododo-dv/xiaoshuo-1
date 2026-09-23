"""绑定:把画像直接用于作品(或某一场 / 某个角色)、改配置、解除;一部作品现在用的是哪一份;叠层只读视图。

- ``POST /profiles/{id}/apply``:``{scope, scope_ref_id, config}``——直接写绑定(台账 U1:不再经待办;U9:不再有
  合成后绑到「当时打开的作品」上的全局卡)。同一画像 + 同一目标 → 更新配置;**一个目标只有一条生效的绑定**:
  把另一份画像用于同一目标时旧的那条停用,响应的 ``replaced`` 里列出来;
- ``PATCH /bindings/{id}``:``{config}``,``dimension_states`` 按维合并(文风画像页一次改一维,N2);
- ``DELETE /bindings/{id}``:解除;
- ``GET /projects/{project_id}/style-binding``:这部作品现在实际生效的绑定 + 画像摘要 + v3 配置 + 现解析的
  风格策略审计(用于作品页与起草台读);
- ``GET /injection/layers``:只读叠层视图(命中了哪几层、哪一层生效)。

配置一律是 v3 四键(``binding_config``):参考方式 ``reference_mode``、样例窗数 ``sample_windows``(0–16)、
维度状态 ``dimension_states``、起草方式 ``draft_mode``;绑定行的旧 ``strategy`` 列恒写 ``mixed``。
删掉的:``GET /bindings/{id}/injection-preview``(预览一律走 ``POST /profiles/{id}/injection-preview``)、
``GET /injection/task-defaults``(旧任务默认策略表,U16)。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.api.routes.style_reference._common import PATH_PREFIX, ROUTE_TAGS, req_id
from novel_system.db.models import StyleReferenceBook, StyleReferenceProfile
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.binding_apply import (
    BindingChange,
    apply_style_profile,
    binding_payload,
    project_style_binding,
    remove_binding,
    update_binding_config,
)
from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    MAX_SAMPLE_WINDOWS,
    MIN_SAMPLE_WINDOWS,
)
from novel_system.services.style_reference.inject.bindings import describe_binding_layers
from novel_system.services.style_reference.repository import StyleReferenceRepository

router = APIRouter(tags=ROUTE_TAGS)

DimensionKey = Literal[ALL_DIMENSIONS]  # type: ignore[valid-type]


class BindingConfigBody(BaseModel):
    """v3 绑定配置(四键都可省:省掉的键保留这条绑定已有的值,新建时取默认)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    reference_mode: Literal["full", "samples_only", "card_only"] | None = None
    sample_windows: int | None = Field(default=None, ge=MIN_SAMPLE_WINDOWS, le=MAX_SAMPLE_WINDOWS)
    dimension_states: dict[DimensionKey, Literal["emphasize", "normal", "exclude"]] | None = Field(
        default=None, max_length=len(ALL_DIMENSIONS)
    )
    draft_mode: Literal["style_first", "neutral_first"] | None = None

    def as_patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ApplyProfileRequest(BaseModel):
    """把画像用于一个目标:作品(``project``)/ 某一场(``scene``)/ 某个角色(``character``)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    scope: Literal["project", "scene", "character"]
    scope_ref_id: str = Field(min_length=1, max_length=255)
    config: BindingConfigBody | None = None


class BindingPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    config: BindingConfigBody


def _cloud_policy_of(session: Session, profile_id: str) -> str | None:
    return session.scalar(
        select(StyleReferenceBook.cloud_policy)
        .join(StyleReferenceProfile, StyleReferenceProfile.book_id == StyleReferenceBook.book_id)
        .where(StyleReferenceProfile.profile_id == str(profile_id))
    )


def _change_payload(session: Session, change: BindingChange) -> dict[str, Any]:
    binding = change.binding
    return {
        "binding": binding_payload(binding, cloud_policy=_cloud_policy_of(session, binding.profile_id)),
        "created": change.created,
        "changed": change.changed,
        "replaced": change.replaced,
        "superseded_planning": change.superseded_planning,
    }


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/apply")
def apply_profile(
    profile_id: str,
    payload: ApplyProfileRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """直接把画像用于目标(见模块文档)。目标不存在 404 ``STYLE_REFERENCE_APPLY_TARGET_NOT_FOUND``;画像失效 /
    归档 409。响应:``binding``(v3 配置 + 生效的参考方式)、``created`` / ``changed``、``replaced``(被这次换下来的
    别的画像的绑定)、``superseded_planning``。"""
    body = payload.model_dump(mode="json")
    patch = payload.config.as_patch() if payload.config is not None else {}

    def _do() -> dict[str, Any]:
        change = apply_style_profile(
            session,
            profile_id,
            scope=payload.scope,
            scope_ref_id=payload.scope_ref_id,
            config=patch,
        )
        return {"profile_id": profile_id, **_change_payload(session, change)}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/apply",
        payload={"profile_id": profile_id, **body},
        action=_do,
    )


@router.patch(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def patch_binding(
    binding_id: str,
    payload: BindingPatchRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """改一条绑定的配置(``dimension_states`` 按维合并)。配置真的变了,它作用范围内的规划产物作废。"""
    patch = payload.config.as_patch()

    def _do() -> dict[str, Any]:
        return _change_payload(session, update_binding_config(session, binding_id, patch))

    return idempotent_response(
        request,
        session,
        method="PATCH",
        path_template=f"{PATH_PREFIX}/bindings/{{binding_id}}",
        payload={"binding_id": binding_id, "config": patch},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/bindings")
def list_bindings(
    profile_id: str,
    request: Request,
    task_type: str | None = None,
    session: Session = Depends(get_session),
):
    """这份画像的全部绑定(含已停用的,``status`` 如实给出;按创建时间)。"""
    bindings = StyleReferenceRepository(session).list_bindings(profile_id=profile_id, task_type=task_type)
    cloud_policy = _cloud_policy_of(session, profile_id)
    return ok(
        {"bindings": [binding_payload(b, cloud_policy=cloud_policy) for b in bindings]},
        req_id=req_id(request),
    )


@router.delete(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def delete_binding(
    binding_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """解除绑定;它作用范围内按这本参考做的规划产物作废(下一次运行重做)。"""

    def _do() -> dict[str, Any]:
        return remove_binding(session, binding_id)

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/bindings/{{binding_id}}",
        payload={"binding_id": binding_id},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/projects/{{project_id}}/style-binding")
def get_project_style_binding(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """只读:这部作品现在实际生效的绑定(没有时 ``binding`` 为 null)、它的画像 / 参考书摘要、v3 配置、按当前绑定
    现解析的风格策略审计(不冻结契约、不写库)。作品不存在 404 ``STYLE_REFERENCE_PROJECT_NOT_FOUND``。"""
    if not project_id or len(project_id) > 128:
        raise DomainError("STYLE_REFERENCE_PROJECT_NOT_FOUND", "project not found", status_code=404)
    return ok(project_style_binding(session, project_id), req_id=req_id(request))


@router.get(f"{PATH_PREFIX}/injection/layers")
def get_injection_layers(
    request: Request,
    project_id: str | None = None,
    task_type: str = "scene_generation",
    scene_id: str | None = None,
    character_ids: str | None = None,
    session: Session = Depends(get_session),
):
    """只读叠层视图:命中了哪几层(scene > 角色 > project > global)、哪一层生效(v3 只有最具体的一层生效)。

    character_ids 逗号分隔(onstage 多角色)。无命中层时 layers=[]、merged=null。只查列、不渲染(U10)。
    """
    chars = [c.strip() for c in (character_ids or "").split(",") if c.strip()] or None
    data = describe_binding_layers(
        session,
        project_id,
        task_type,
        character_ids=chars,
        scene_id=scene_id,
    )
    return ok(data, req_id=req_id(request))
