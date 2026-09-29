"""运行时 LLM 接入的共同底座：设置、客户端、供应商配置、路由、节点配置、模板与 prompt 哈希。

LLMNodeRunner、ChapterPlanService、SnowflakeWorkspaceLLMService 以前各抄一份
``_settings_payload`` / ``_client`` / ``_runtime_provider_configs`` / ``_routing`` /
``_task_config`` / ``_template`` 和 ``_prompt_hash``；现在都继承 ``RuntimeLLMAccess``。
客户端只有一个构造点 ``system_config.build_llm_client``；风格参考的四个客户端解析点
（分类 / 学习 / 对照检查作业与路由包）经 ``runtime_llm_client_and_enabled`` 走同一个工厂。

打桩点：测试替换设置 / 路由 / 模板加载要打在本模块（``get_settings`` /
``load_model_routing_config`` / ``load_prompt_templates`` / ``load_llm_provider_runtime_configs``）；
子类上的方法（如 ``SnowflakeWorkspaceLLMService._client``）照旧可以在子类上打桩。

退避：只有场景运行器（LLMNodeRunner）传 ``retry_backoff_seconds``，其余调用方用
LLMClient 的默认值（不退避）——这是现状（B09-09），这里只收拢构造，不改谁有退避。
"""

from __future__ import annotations

import uuid
from typing import Any

from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_client import LLMClient, load_model_routing_config, resolve_node_route
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.system_config import (
    build_llm_client,
    build_runtime_llm_client,
    load_llm_provider_runtime_configs,
)
from novel_system.settings import get_settings


def runtime_llm_client_and_enabled() -> tuple[LLMClient | None, bool]:
    """按**当前**运行时配置取客户端（fail-closed：未启用返回 (None, False)）。

    风格参考作业与路由的解析点都委托到这里；它们各自保留同名模块属性供测试打桩。
    """
    return build_runtime_llm_client(settings=get_settings())


def structured_prompt_hash(
    template_name: str,
    template_version: str,
    system_prompt: str,
    user_prompt: str,
    structured_schema: dict[str, Any],
) -> str:
    """结构化调用的 prompt 指纹（uuid5，审计 / 幂等用；输出与原先两份实现逐字节一致）。"""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        canonical_json(
            {
                "template_name": template_name,
                "template_version": template_version,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "structured_schema": structured_schema,
            }
        ),
    ).hex


class RuntimeLLMAccess:
    """运行时 LLM 接入的共同状态与懒加载器（混入类）。

    构造时不读库、不解密：设置 / 路由 / 供应商配置 / 模板都在第一次用到时才加载并缓存在实例上
    （只读服务构造就建好运行器，大多数请求一次 LLM 调用都不发）。
    """

    _settings: Any
    _llm_client: Any | None
    _routing_config: Any | None
    _prompt_templates: dict[str, Any] | None
    _provider_configs: dict[str, Any] | None

    def _init_runtime_llm_access(
        self,
        *,
        llm_client: Any | None = None,
        routing_config: Any | None = None,
        prompt_templates: dict[str, Any] | None = None,
        settings: Any | None = None,
    ) -> None:
        self._settings = settings
        self._llm_client = llm_client
        self._routing_config = routing_config
        self._prompt_templates = prompt_templates
        self._provider_configs = None

    def _settings_payload(self) -> Any:
        if self._settings is None:
            self._settings = get_settings()
        return self._settings

    def _llm_enabled(self) -> bool:
        return bool(self._settings_payload().llm_enabled)

    def _runtime_provider_configs(self) -> dict[str, Any]:
        if self._provider_configs is None:
            self._provider_configs = load_llm_provider_runtime_configs()
        return self._provider_configs

    def _build_runtime_client(self, *, retry_backoff_seconds: float | None = None) -> LLMClient:
        return build_llm_client(
            self._settings_payload(),
            provider_configs=self._runtime_provider_configs(),
            retry_backoff_seconds=retry_backoff_seconds,
        )

    def _client(self) -> Any:
        if self._llm_client is not None:
            return self._llm_client
        return self._build_runtime_client()

    def _routing(self) -> Any:
        if self._routing_config is None:
            self._routing_config = load_model_routing_config()
        return self._routing_config

    def _task_config(self, task_key: str) -> Any:
        return resolve_node_route(self._routing(), task_key)

    def _template(self, template_name: str) -> Any:
        if self._prompt_templates is None:
            self._prompt_templates = load_prompt_templates()
        return self._prompt_templates[template_name]
