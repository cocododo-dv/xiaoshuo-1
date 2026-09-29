"""豆包 — 火山方舟 Ark v3 OpenAI 兼容 Chat Completions。

文档锚点(2026-06 核验,volcengine.com 82379/1494384 + 官方 Python SDK):
- base: https://ark.cn-beijing.volces.com/api/v3,Bearer(ARK API Key)
- model 字段接受 Model ID(如 doubao-seed-2-0-pro-260215)或接入点 ep-xxx
- response_format 支持 text / json_object / json_schema
- 思考为顶层 ``thinking: {"type": "enabled"|"disabled"|"auto"}``
- 数据面**没有** GET /models(模型列表在控制面 ListFoundationModels,AK/SK
  签名),因此 list_models_request 返回 None → 走预设/手填回退。
"""

from __future__ import annotations

from typing import Any, ClassVar

from novel_system.services.llm_providers.base import LLMRequest, ModelListRequest, ProviderRuntimeConfig
from novel_system.services.llm_providers.openai_chat_family import OpenAIChatFamilyAdapter, thinking_enabled_or_disabled


class DoubaoArkAdapter(OpenAIChatFamilyAdapter):
    provider_type: ClassVar[str] = "doubao_ark"
    label_zh: ClassVar[str] = "豆包（火山方舟）"
    default_base_url: ClassVar[str] = "https://ark.cn-beijing.volces.com/api/v3"
    category: ClassVar[str] = "cn"
    docs_url: ClassVar[str | None] = "https://www.volcengine.com/docs/82379/1494384"

    def chat_extra_payload(
        self,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return thinking_enabled_or_disabled(request)

    def list_models_request(
        self,
        *,
        base_url: str,
        api_key: str | None,
        provider_options: dict[str, Any] | None = None,
    ) -> ModelListRequest | None:
        return None
