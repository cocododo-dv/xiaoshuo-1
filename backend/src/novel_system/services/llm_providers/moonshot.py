"""Moonshot AI Kimi — OpenAI 兼容 Chat Completions。

文档锚点(2026-06 核验,platform.kimi.com):
- base: https://api.moonshot.cn/v1(国内)/ https://api.moonshot.ai/v1(海外),Bearer
- GET /v1/models 可用;response_format 支持 json_object / json_schema
- 思考为顶层 ``thinking: {"type": "enabled"|"disabled"}``;kimi-k2.7-code 传
  disabled 会报错,故只在需要时发送 enabled,从不发送 disabled。
"""

from __future__ import annotations

from typing import Any, ClassVar

from novel_system.services.llm_providers.base import LLMRequest, ProviderRuntimeConfig
from novel_system.services.llm_providers.openai_chat_family import OpenAIChatFamilyAdapter, thinking_enabled_on_demand


class MoonshotAdapter(OpenAIChatFamilyAdapter):
    provider_type: ClassVar[str] = "moonshot"
    label_zh: ClassVar[str] = "Kimi / Moonshot"
    default_base_url: ClassVar[str] = "https://api.moonshot.cn/v1"
    appends_v1_to_bare_host: ClassVar[bool] = True
    category: ClassVar[str] = "cn"
    docs_url: ClassVar[str | None] = "https://platform.kimi.com/docs"

    def chat_extra_payload(
        self,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return thinking_enabled_on_demand(request)
