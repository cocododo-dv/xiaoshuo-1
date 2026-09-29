from __future__ import annotations

from typing import Any, ClassVar

from novel_system.services.llm_providers.base import LLMRequest, ProviderRuntimeConfig
from novel_system.services.llm_providers.openai_chat_family import OpenAIChatFamilyAdapter, thinking_enabled_or_disabled


class ZhipuGLMAdapter(OpenAIChatFamilyAdapter):
    provider_type: ClassVar[str] = "zhipu_glm"
    label_zh: ClassVar[str] = "智谱 GLM"
    default_base_url: ClassVar[str] = "https://open.bigmodel.cn/api/paas/v4"
    category: ClassVar[str] = "cn"
    docs_url: ClassVar[str | None] = "https://docs.bigmodel.cn"

    def chat_extra_payload(
        self,
        request: LLMRequest,
        provider_config: ProviderRuntimeConfig,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return thinking_enabled_or_disabled(request)
