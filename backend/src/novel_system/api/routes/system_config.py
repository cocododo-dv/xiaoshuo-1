from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session, request_id_of
from novel_system.api.mutations import optional_idempotent_response
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest
from novel_system.api.response import ok
from novel_system.services.system_config import SystemConfigService, require_admin_token

router = APIRouter(tags=["system_config"])


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


class SystemConfigDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    category: Literal["api", "models", "prompts", "allowlists", "hash_contract"]
    yaml_raw: str = Field(min_length=1, max_length=2_000_000)
    secrets: dict[
        Annotated[str, Field(min_length=1, max_length=255)],
        Annotated[str, Field(max_length=16_384)],
    ] | None = Field(default=None, max_length=64)


class ProviderProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: str | None = Field(default=None, max_length=64)
    provider_type: str | None = Field(default=None, max_length=64)
    provider_id: str | None = Field(default=None, max_length=255)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=16_384)
    credential_mode: str | None = Field(default=None, max_length=64)
    api_mode: str | None = Field(default=None, max_length=64)
    provider_options: BoundedJsonObject | None = None
    timeout_seconds: float | None = Field(default=None, ge=0, le=3_600)
    model: str | None = Field(default=None, max_length=255)
    models: list[
        Annotated[str, Field(min_length=1, max_length=255)]
    ] | None = Field(default=None, max_length=256)
    check_completion: bool | None = None


class LlmProviderConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider_id: str = Field(min_length=1, max_length=255)
    provider_type: str = Field(min_length=1, max_length=64)
    account_id: str | None = Field(default=None, max_length=255)
    base_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True
    credential_mode: str = Field(default="api_key", max_length=64)
    api_mode: str | None = Field(default=None, max_length=64)
    models: list[
        Annotated[str, Field(min_length=1, max_length=255)]
    ] | None = Field(default=None, max_length=256)
    provider_options: BoundedJsonObject | None = None
    api_key: str | None = Field(default=None, max_length=16_384)


class LlmNodeRoutesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_routing: BoundedJsonObject
    model_profiles: BoundedJsonObject | None = None
    task_routing: BoundedJsonObject | None = None
    retry_budget: BoundedJsonObject | None = None
    job_runtime: BoundedJsonObject | None = None
    activate: bool = False


class LlmNodeRouteSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider_id: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    activate: bool = True


class LlmRoleRoutesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    assignments: BoundedJsonObject
    activate: bool = True


@router.get("/api/v1/system-config")
def system_config_overview(request: Request, session: Session = Depends(get_session)):
    return ok(SystemConfigService(session).overview(), req_id=request_id_of(request))


@router.post("/api/v1/system-config/drafts")
def create_system_config_draft(
    payload: SystemConfigDraftRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json")
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/drafts",
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).create_draft(
            category=payload.category,
            yaml_raw=payload.yaml_raw,
            secrets=payload.secrets,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v1/system-config/{snapshot_id}/activate")
def activate_system_config_snapshot(
    snapshot_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/{snapshot_id}/activate",
        payload={"snapshot_id": snapshot_id},
        action=lambda: SystemConfigService(session, auto_commit=False).activate(
            snapshot_id,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v1/system-config/test-provider")
def test_system_config_provider(
    payload: ProviderProbeRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/test-provider",
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).test_provider(payload=body),
    )


@router.get("/api/v1/system-config/export/{category}")
def export_system_config_category(category: str, request: Request, session: Session = Depends(get_session)):
    return ok(
        SystemConfigService(session).export_category(category),
        req_id=request_id_of(request),
    )


@router.get("/api/v1/system-config/llm")
def system_config_llm_overview(request: Request, session: Session = Depends(get_session)):
    return ok(SystemConfigService(session).llm_overview(), req_id=request_id_of(request))


@router.get("/api/v1/system-config/llm/calls/audit")
def system_config_llm_call_audit(request: Request, session: Session = Depends(get_session)):
    return ok(SystemConfigService(session).llm_call_audit(), req_id=request_id_of(request))


@router.post("/api/v1/system-config/llm/providers")
def save_system_config_llm_provider(
    payload: LlmProviderConfigRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/llm/providers",
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).save_llm_provider(
            payload=body,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.delete("/api/v1/system-config/llm/providers/{provider_id}")
def delete_system_config_llm_provider(
    provider_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    return optional_idempotent_response(
        request,
        session,
        method="DELETE",
        path_template="/api/v1/system-config/llm/providers/{provider_id}",
        payload={"provider_id": provider_id},
        action=lambda: SystemConfigService(session, auto_commit=False).delete_llm_provider(
            provider_id=provider_id,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v1/system-config/llm/providers/{provider_id}/default")
def set_default_system_config_llm_provider(
    provider_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/llm/providers/{provider_id}/default",
        payload={"provider_id": provider_id},
        action=lambda: SystemConfigService(session, auto_commit=False).set_default_llm_provider(
            provider_id=provider_id,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v1/system-config/llm/node-routes")
def save_system_config_llm_node_routes(
    payload: LlmNodeRoutesRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/llm/node-routes",
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).save_llm_node_routes(
            payload=body,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v1/system-config/llm/node-routes/sync-missing")
def sync_missing_system_config_llm_node_routes(
    payload: LlmNodeRouteSyncRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/llm/node-routes/sync-missing",
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).sync_missing_llm_node_routes(
            payload=body,
            actor_ref=actor_ref_of(request),
        ),
    )


@router.post("/api/v1/system-config/llm/providers/{provider_id}/probe")
def probe_system_config_llm_provider(
    provider_id: str,
    request: Request,
    payload: ProviderProbeRequest | None = None,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True) if payload else {}
    request_payload = {"provider_id": provider_id, **body}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/llm/providers/{provider_id}/probe",
        payload=request_payload,
        action=lambda: SystemConfigService(session, auto_commit=False).probe_llm_provider(
            provider_id=provider_id,
            payload=body,
        ),
    )


@router.get("/api/v1/system-config/llm/provider-presets")
def list_system_config_llm_provider_presets(request: Request, session: Session = Depends(get_session)):
    return ok(
        SystemConfigService(session).llm_provider_presets(),
        req_id=request_id_of(request),
    )


@router.get("/api/v1/system-config/llm/providers/{provider_id}/models")
def list_system_config_llm_provider_models(
    provider_id: str,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    return ok(
        SystemConfigService(session).list_llm_provider_models(provider_id=provider_id),
        req_id=None,
    )


@router.post("/api/v1/system-config/llm/role-routes")
def save_system_config_llm_role_routes(
    payload: LlmRoleRoutesRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/system-config/llm/role-routes",
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).save_llm_role_routes(
            payload=body,
            actor_ref=actor_ref_of(request),
        ),
    )
