"""运行时 LLM 接入底座（llm_service_base）：三个服务共用一套懒加载，prompt 指纹不变。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.services import llm_service_base
from novel_system.services.chapter_plan_llm import ChapterPlanService
from novel_system.services.llm_client import LLMClient, ModelRoutingConfig
from novel_system.services.llm_service_base import RuntimeLLMAccess, structured_prompt_hash
from novel_system.services.llm_task_runner import LLMNodeRunner
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService


def test_structured_prompt_hash_is_pinned() -> None:
    # 原先 chapter_plan_llm / snowflake_workspace_llm 各一份 _prompt_hash 的输出；审计与幂等引用它，不能漂。
    assert (
        structured_prompt_hash(
            "snowflake_generate_book_brief",
            "2026-09-29.v1",
            "系统：雨城",
            '{"a":1}',
            {"type": "object", "properties": {"林昭": {"type": "string"}}},
        )
        == "1b60f331be08516a9fdb3712059b47b6"
    )


@pytest.mark.parametrize("service_cls", [ChapterPlanService, SnowflakeWorkspaceLLMService, LLMNodeRunner])
def test_services_share_the_lazy_runtime_access(session, monkeypatch, service_cls) -> None:
    assert issubclass(service_cls, RuntimeLLMAccess)
    calls: list[str] = []
    settings = SimpleNamespace(
        llm_enabled=True,
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_api_key="sk-fixture",
        llm_timeout_seconds=30,
    )
    routing = ModelRoutingConfig(node_routing={}, task_routing={}, retry_budget={}, job_runtime={})

    def fake_settings():
        calls.append("settings")
        return settings

    def fake_routing():
        calls.append("routing")
        return routing

    def fake_provider_configs():
        calls.append("providers")
        return {}

    monkeypatch.setattr(llm_service_base, "get_settings", fake_settings)
    monkeypatch.setattr(llm_service_base, "load_model_routing_config", fake_routing)
    monkeypatch.setattr(llm_service_base, "load_llm_provider_runtime_configs", fake_provider_configs)

    service = service_cls(session)
    assert calls == []  # 构造时什么都不读

    assert service._settings_payload() is settings
    assert service._settings_payload() is settings
    assert service._routing() is routing
    assert service._routing() is routing
    assert service._llm_enabled() is True
    client = service._client()
    assert isinstance(client, LLMClient)
    service._client()
    assert calls == ["settings", "routing", "providers"]  # 每样只加载一次
    # 重试退避只有场景运行器有（B09-09 现状）
    expected_backoff = 1.5 if service_cls is LLMNodeRunner else 0.0
    assert client._retry_backoff_seconds == expected_backoff


def test_injected_client_wins(session) -> None:
    injected = object()
    assert ChapterPlanService(session, llm_client=injected)._client() is injected
    assert SnowflakeWorkspaceLLMService(session, llm_client=injected)._client() is injected


def test_runtime_client_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(llm_service_base, "get_settings", lambda: SimpleNamespace(llm_enabled=False))
    assert llm_service_base.runtime_llm_client_and_enabled() == (None, False)
