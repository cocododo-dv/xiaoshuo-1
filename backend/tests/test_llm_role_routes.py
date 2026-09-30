"""角色分工槽位(role slots)的后端契约测试。

覆盖:槽位目录完整性、role-routes 展开正确性(只覆写槽内节点、保留节点
默认参数)、activate 校验失败路径、llm_overview 的槽位绑定推断。
"""

from __future__ import annotations

import pytest

from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_client import parse_model_routing_config
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.llm_node_registry import (
    ROLE_SLOTS,
    active_llm_node_ids,
    get_llm_node_spec,
    role_slot_catalog,
    role_slot_node_ids,
)


ADMIN_HEADERS = {"X-Admin-Token": "admin-token", "X-Operator-Ref": "ops.config"}


def _enable_admin(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("NOVEL_SYSTEM_CONFIG_SECRET", "config-secret")


def _create_provider(client, provider_id: str = "main_provider", models: list[str] | None = None):
    response = client.post(
        "/api/v1/system-config/llm/providers",
        headers=ADMIN_HEADERS,
        json={
            "provider_id": provider_id,
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "credential_mode": "none",
            "api_mode": "chat",
            "models": models or ["test-model-a", "test-model-b"],
        },
    )
    assert response.status_code == 200
    return response


def test_role_slots_cover_all_active_nodes_exactly_once() -> None:
    covered: list[str] = []
    for slot in ROLE_SLOTS:
        covered.extend(role_slot_node_ids(slot.slot_id))
    assert sorted(covered) == sorted(active_llm_node_ids())
    assert len(covered) == len(set(covered))


def test_role_slot_catalog_shape() -> None:
    catalog = role_slot_catalog()
    assert [entry["slot_id"] for entry in catalog] == ["drafting", "review", "extraction"]
    for entry in catalog:
        assert entry["label_zh"]
        assert entry["groups"]
        assert entry["node_ids"]


def test_llm_overview_exposes_role_slots(client) -> None:
    response = client.get("/api/v1/system-config/llm")
    assert response.status_code == 200
    payload = response.json()["data"]
    slots = {slot["slot_id"]: slot for slot in payload["role_slots"]}
    assert set(slots) == {"drafting", "review", "extraction"}
    # 默认 repo 配置无 provider_id 绑定 → 不可能是统一绑定到某 provider 的状态
    for slot in slots.values():
        current = slot["current"]
        assert current is None or current["provider_id"] is None


def test_save_role_routes_expands_only_slot_nodes(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    _create_provider(client)

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={
            "assignments": {"drafting": {"provider_id": "main_provider", "model": "test-model-b"}},
            "activate": False,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    applied = data["applied"]["drafting"]
    assert applied["provider_id"] == "main_provider"
    assert applied["model"] == "test-model-b"
    assert sorted(applied["node_ids"]) == sorted(role_slot_node_ids("drafting"))

    node_routing = data["snapshot"]["parsed"]["node_routing"]
    effective = parse_model_routing_config(data["snapshot"]["parsed"]).node_routing
    for node_id in role_slot_node_ids("drafting"):
        route = node_routing[node_id]
        assert route["provider_id"] == "main_provider"
        assert route["model"] == "test-model-b"
        spec = get_llm_node_spec(node_id)
        # 批准#5a:快照只存作者的选择;节点级默认参数(temperature/max_output_tokens)解析时取 spec
        assert "temperature" not in route and "max_output_tokens" not in route
        assert effective[node_id].temperature == spec.temperature
        assert effective[node_id].max_output_tokens == spec.max_output_tokens
    # 槽外节点不被覆写成该 provider
    for node_id in role_slot_node_ids("review"):
        route = node_routing.get(node_id)
        if route is not None:
            assert route.get("provider_id") != "main_provider"
    # 槽位绑定从 node_routes 反推(overview.role_slots),快照不再另存一份没人读的 role_assignments
    assert "role_assignments" not in data["snapshot"]["parsed"]


def test_save_role_routes_activate_updates_overview_inference(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    _create_provider(client)

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={
            "assignments": {
                "drafting": {"provider_id": "main_provider", "model": "test-model-a"},
                "review": {"provider_id": "main_provider", "model": "test-model-a"},
                "extraction": {"provider_id": "main_provider", "model": "test-model-b"},
            },
            "activate": True,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["snapshot"]["active"] is True
    slots = {slot["slot_id"]: slot for slot in data["overview"]["role_slots"]}
    assert slots["drafting"]["current"] == {"provider_id": "main_provider", "model": "test-model-a", "mixed": False}
    assert slots["extraction"]["current"] == {"provider_id": "main_provider", "model": "test-model-b", "mixed": False}
    # 全部 active 节点都被路由后,缺失列表应为空
    assert data["overview"]["missing_active_routes"] == []


def test_save_role_routes_partial_assignment_activates_incrementally(client, monkeypatch) -> None:
    """只分配一个槽位也能激活:校验范围限于本次触达的节点。"""
    _enable_admin(monkeypatch)
    _create_provider(client)

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={
            "assignments": {"drafting": {"provider_id": "main_provider", "model": "test-model-a"}},
            "activate": True,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["snapshot"]["active"] is True
    slots = {slot["slot_id"]: slot for slot in data["overview"]["role_slots"]}
    assert slots["drafting"]["current"] == {"provider_id": "main_provider", "model": "test-model-a", "mixed": False}
    # 其余槽位未触达 → 仍是未绑定/混合状态,而不是被连带改写
    extraction = slots["extraction"]["current"]
    assert extraction is None or extraction["provider_id"] != "main_provider" or extraction.get("mixed")


def test_save_role_routes_rejects_unknown_slot(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    _create_provider(client)

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"nonsense": {"provider_id": "main_provider", "model": "test-model-a"}}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_ROLE_SLOT_UNKNOWN"


def test_save_role_routes_rejects_unlisted_model(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    _create_provider(client)

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"drafting": {"provider_id": "main_provider", "model": "not-in-list"}}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_ROUTE_MODEL_MISSING"


def test_save_role_routes_rejects_disabled_provider(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    response = client.post(
        "/api/v1/system-config/llm/providers",
        headers=ADMIN_HEADERS,
        json={
            "provider_id": "disabled_provider",
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "credential_mode": "none",
            "api_mode": "chat",
            "models": ["test-model-a"],
            "enabled": False,
        },
    )
    assert response.status_code == 200

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"drafting": {"provider_id": "disabled_provider", "model": "test-model-a"}}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_ROUTE_PROVIDER_NOT_READY"


def test_role_routes_requires_admin_token(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        json={"assignments": {"drafting": {"provider_id": "x", "model": "y"}}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_TOKEN_REQUIRED"


def test_provider_presets_endpoint_lists_presets_and_catalog(client) -> None:
    response = client.get("/api/v1/system-config/llm/provider-presets")
    assert response.status_code == 200
    data = response.json()["data"]
    preset_ids = {preset["preset_id"] for preset in data["presets"]}
    assert {"openai", "anthropic", "deepseek", "qwen_dashscope", "moonshot", "doubao_ark", "openrouter", "custom"} <= preset_ids
    assert "qwen_dashscope" in data["provider_catalog"]
    relay = next(p for p in data["presets"] if p["preset_id"] == "openrouter")
    assert relay["is_relay"] is True
    assert relay["provider_type"] == "openai_compatible"


def test_provider_models_endpoint_live_and_preset_fallback(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    _create_provider(client, provider_id="live_provider", models=["seed-model"])

    def fake_models_ok(url: str, *, headers: dict[str, str], timeout: float, trust_env: bool):
        assert url == "http://127.0.0.1:8080/v1/models"
        import httpx

        request = httpx.Request("GET", url)
        return httpx.Response(200, json={"data": [{"id": "live-a"}, {"id": "live-b"}]}, request=request)

    monkeypatch.setattr("novel_system.services.llm_provider_probe.httpx.get", fake_models_ok)
    response = client.get(
        "/api/v1/system-config/llm/providers/live_provider/models",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "live"
    assert data["available_models"] == ["live-a", "live-b"]

    def fake_models_down(url: str, *, headers: dict[str, str], timeout: float, trust_env: bool):
        import httpx

        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr("novel_system.services.llm_provider_probe.httpx.get", fake_models_down)
    response = client.get(
        "/api/v1/system-config/llm/providers/live_provider/models",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "preset"
    # 回退 = 已配置模型(无同名 preset 的 openai_compatible 没有 common_models)
    assert data["available_models"] == ["seed-model"]


# ---- 运行时配置读取的瞬时容错 + runner 退避配置 ---------------------------


def test_load_active_config_payload_retries_transient_db_errors(client, session, monkeypatch) -> None:
    """sqlite 瞬时锁不应让 LLM 被误判为未配置(历史 KeyError 审计行的根因)。"""
    import pytest
    from sqlalchemy.exc import OperationalError

    from novel_system.services import system_config as sc

    real_session_local = sc.SessionLocal
    attempts = {"n": 0}

    def flaky_session_local():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))
        return real_session_local()

    monkeypatch.setattr(sc, "SessionLocal", flaky_session_local)
    monkeypatch.setattr(sc.time, "sleep", lambda _s: None)

    # 无激活快照 → 重试成功后正常返回 None(而不是吞错短路)
    assert sc.load_active_config_payload("api") is None
    assert attempts["n"] == 2

    # 持续失败 → 保留 OperationalError，由 API 边界明确映射为可重试 503；
    # 绝不能伪装成“没有激活配置”。
    attempts["n"] = -10_000
    monkeypatch.setattr(sc, "SessionLocal", lambda: (_ for _ in ()).throw(
        OperationalError("SELECT 1", {}, Exception("database is locked"))
    ))
    with pytest.raises(OperationalError):
        sc.load_active_config_payload("api")


def test_runner_retry_backoff_reads_job_runtime_override(session) -> None:
    from novel_system.services.llm_client import ModelRoutingConfig
    from novel_system.services.llm_task_runner import LLMNodeRunner

    default_runner = LLMNodeRunner(
        session,
        routing_config=ModelRoutingConfig(node_routing={}, task_routing={}),
    )
    assert default_runner._retry_backoff_seconds() == 1.5

    tuned_runner = LLMNodeRunner(
        session,
        routing_config=ModelRoutingConfig(
            node_routing={},
            task_routing={},
            job_runtime={"llm_retry_backoff_seconds": 0.25},
        ),
    )
    assert tuned_runner._retry_backoff_seconds() == 0.25


def _route_probe(session, node_id: str) -> str:
    """走真实运行器问一个节点要路由:返回拒绝码(路由缺失时运行器 fail-closed,在发请求之前)。"""
    with pytest.raises(LLMNodeExecutionError) as excinfo:
        LLMNodeRunner(session).run_task(
            task_name=node_id,
            prompt_text="x",
            system_prompt="y",
            context=LLMCallContext(scope_type="system", scope_id="route-probe", node_id=node_id, step="route_probe"),
        )
    return excinfo.value.error_code


def test_saved_provider_without_models_snapshot_routes_no_node_and_fails_closed(client, session, monkeypatch) -> None:
    """重评 R4 复核补充 1:作者已在设置里保存过服务、还没有 models 快照时,不拿注册表默认路由(占位模型 gpt-5)
    发到作者的中转——每个节点都是未指派(设置页显示 32 个缺失、给出一键补齐),运行时 LLM_ROUTE_NOT_CONFIGURED。"""
    _enable_admin(monkeypatch)
    _create_provider(client)

    overview = client.get("/api/v1/system-config/llm").json()["data"]
    assert overview["missing_active_routes"] == active_llm_node_ids()
    assert overview["readiness"]["configured_route_count"] == 0
    assert overview["readiness"]["active_route_count"] == len(active_llm_node_ids())
    assert overview["node_routes"]["neutral_draft"]["readiness_reason"] == "not_configured"
    assert _route_probe(session, "extraction") == "LLM_ROUTE_NOT_CONFIGURED"


def test_configured_snapshot_missing_a_node_still_fails_closed(client, session, monkeypatch) -> None:
    """节点级规则:有 models 快照时,快照里没有的节点就是没配——绝不在快照底下垫注册表默认值。"""
    _enable_admin(monkeypatch)
    _create_provider(client)
    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"drafting": {"provider_id": "main_provider", "model": "test-model-a"}}, "activate": True},
    )
    assert response.status_code == 200, response.json()
    assert "extraction" not in role_slot_node_ids("drafting")
    assert "extraction" in response.json()["data"]["overview"]["missing_active_routes"]
    assert _route_probe(session, "extraction") == "LLM_ROUTE_NOT_CONFIGURED"
