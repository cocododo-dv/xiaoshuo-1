from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.system_config import (
    LlmNodeRouteSyncRequest,
    LlmProviderConfigRequest,
    LlmRoleRoutesRequest,
    ProviderProbeRequest,
)
from novel_system.api.response import respond
from novel_system.services.system_config import SystemConfigService, require_admin_token

router = APIRouter(tags=["system_config"])


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


@router.get("/api/v1/system-config")
def system_config_overview(request: Request, session: Session = Depends(get_session)):
    # 摘要：运行时状态 + 各类配置的来源与活动快照版本（不带 YAML 正文与历史快照；设置页读 /llm）
    return respond(request, SystemConfigService(session).overview(include_content=False))


@router.post("/api/v1/system-config/test-provider")
def test_system_config_provider(
    payload: ProviderProbeRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).test_provider(payload=body),
    )


@router.get("/api/v1/system-config/llm")
def system_config_llm_overview(request: Request, session: Session = Depends(get_session)):
    return respond(request, SystemConfigService(session).llm_overview())


@router.post("/api/v1/system-config/llm/providers")
def save_system_config_llm_provider(
    payload: LlmProviderConfigRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
        payload={"provider_id": provider_id},
        action=lambda: SystemConfigService(session, auto_commit=False).set_default_llm_provider(
            provider_id=provider_id,
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
        payload=request_payload,
        action=lambda: SystemConfigService(session, auto_commit=False).probe_llm_provider(
            provider_id=provider_id,
            payload=body,
        ),
    )


@router.get("/api/v1/system-config/llm/provider-presets")
def list_system_config_llm_provider_presets(request: Request, session: Session = Depends(get_session)):
    return respond(request, SystemConfigService(session).llm_provider_presets())


@router.get("/api/v1/system-config/llm/providers/{provider_id}/models")
def list_system_config_llm_provider_models(
    provider_id: str,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    return respond(request, SystemConfigService(session).list_llm_provider_models(provider_id=provider_id))


@router.post("/api/v1/system-config/llm/role-routes")
def save_system_config_llm_role_routes(
    payload: LlmRoleRoutesRequest,
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json", exclude_none=True)
    return mutate(
        request,
        session,
        payload=body,
        action=lambda: SystemConfigService(session, auto_commit=False).save_llm_role_routes(
            payload=body,
            actor_ref=actor_ref_of(request),
        ),
    )
