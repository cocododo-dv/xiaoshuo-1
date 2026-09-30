"""系统配置的请求体：服务商连通测试、服务商配置、节点补齐、按角色分工。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from novel_system.api.requests.common import BoundedJsonObject, StrictRequestModel


class ProviderProbeRequest(StrictRequestModel):
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


class LlmProviderConfigRequest(StrictRequestModel):
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


class LlmNodeRouteSyncRequest(StrictRequestModel):
    provider_id: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    activate: bool = True


class LlmRoleRoutesRequest(StrictRequestModel):
    assignments: BoundedJsonObject
    activate: bool = True
