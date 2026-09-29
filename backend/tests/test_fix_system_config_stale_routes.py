"""系统配置 / AI 提供方:退役节点残留路由 + 非法 api_mode 的回归测试。

场景来自老安装:节点注册表退役了 7 个 LLM 节点(long_form_continuation、
reference_profile_synthesize …),但活动 models 快照仍带着它们的路由,而且路由指向
的服务可能早已删除。修复前:
- 「一键补齐路由」(sync-missing)永远 422(CONFIG_ROUTE_PROVIDER_MISSING),因为激活校验
  会连退役条目一起校验;
- role-routes / sync-missing 在遇到非法 api_mode / response_format 时把
  LLMConfigurationError 漏成 500 INTERNAL_ERROR;
- 服务保存不校验 api_mode。
(原样整表写入的 node-routes 接口没有界面调用,2026-09-30 重评 R15a 删了,它的三例随之删掉。)
"""

from __future__ import annotations

import uuid

import yaml

from novel_system.db.models import SystemConfigSnapshot, utcnow
from novel_system.services.llm_node_registry import (
    active_llm_node_ids,
    default_task_config_payload,
    llm_node_catalog,
)
from novel_system.services.llm_routing import parse_model_routing_config, registry_default_node_routing
from novel_system.services.system_config import default_config_payload


ADMIN_HEADERS = {"X-Admin-Token": "admin-token", "X-Operator-Ref": "ops.config"}
# 真实退役过的节点 id(已不在 llm_node_catalog 中)
RETIRED_NODE_ID = "reference_profile_synthesize"
RETIRED_TASK_ID = "long_form_continuation"


def _enable_admin(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("NOVEL_SYSTEM_CONFIG_SECRET", "config-secret")


def _create_provider(client, provider_id: str, *, api_mode: str = "chat", models: list[str] | None = None):
    return client.post(
        "/api/v1/system-config/llm/providers",
        headers=ADMIN_HEADERS,
        json={
            "provider_id": provider_id,
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "enabled": True,
            "credential_mode": "none",
            "api_mode": api_mode,
            "models": models or ["Qwen3-14B-Q8_0.gguf"],
        },
    )


def _route(provider_id: str, model: str = "Qwen3-14B-Q8_0.gguf", **overrides) -> dict:
    payload = {
        "provider": "openai_compatible",
        "provider_id": provider_id,
        "model": model,
        "temperature": 0.25,
        "max_output_tokens": 3200,
        "response_format": "json_object",
        "reasoning_level": "medium",
        "api_mode": "chat",
        "credential_mode": "none",
    }
    payload.update(overrides)
    return payload


def _seed_active_snapshot(session, *, category: str, parsed: dict) -> str:
    """直接写活动快照,模拟绕过当前写路径校验的老安装数据。"""
    snapshot_id = f"config_{category}_{uuid.uuid4().hex[:12]}"
    session.add(
        SystemConfigSnapshot(
            snapshot_id=snapshot_id,
            category=category,
            version=1,
            yaml_raw=yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False),
            parsed_json=parsed,
            validation_json={"ok": True, "message": f"{category} config is valid"},
            status="active",
            active_flag=1,
            activated_at=utcnow(),
            created_by="legacy-install",
        )
    )
    session.commit()
    return snapshot_id


def _legacy_models_payload(**stale_entries: dict) -> dict:
    """老安装的 models 快照形状:当年的写路径把仓库 models.yaml(task_routing 是注册表的整份抄本 + stylize
    别名)整个抄进快照,再加上老安装遗留的退役节点 task_routing 条目。"""
    _, parsed, _, _ = default_config_payload("models")
    task_routing = {
        **{node_id: _route_without_provider(route) for node_id, route in registry_default_node_routing().items()},
        "stylize": _route_without_provider(registry_default_node_routing()["style_draft"]),
    }
    payload = {
        "model_profiles": {"quality_strong": {"label": "精修"}},
        "task_routing": {**task_routing, **stale_entries},
        "node_routing": {},
        "retry_budget": dict(parsed.get("retry_budget") or {}),
        "job_runtime": dict(parsed.get("job_runtime") or {}),
    }
    return payload


def _route_without_provider(route: dict) -> dict:
    return {key: value for key, value in route.items() if key != "api_mode"}


def test_retired_fixture_ids_are_really_outside_the_catalog() -> None:
    catalog = llm_node_catalog()
    assert RETIRED_NODE_ID not in catalog
    assert RETIRED_TASK_ID not in catalog


# --------------------------------------------------------------------------- (1)
def test_sync_missing_prunes_stale_route_bound_to_deleted_provider(client, session, monkeypatch) -> None:
    """退役节点的路由指向已删除的服务时,「一键补齐路由」必须成功并剪掉该条目。"""
    _enable_admin(monkeypatch)
    assert _create_provider(client, "legacy_provider").status_code == 200
    assert _create_provider(client, "local_qwen").status_code == 200

    # 老安装的活动快照:一个目录内节点和一个退役节点都指着 legacy_provider
    _seed_active_snapshot(
        session,
        category="models",
        parsed={
            "node_routing": {
                "snowflake_step_candidates": _route("legacy_provider"),
                RETIRED_NODE_ID: _route("legacy_provider"),
            }
        },
    )

    delete_response = client.delete(
        "/api/v1/system-config/llm/providers/legacy_provider",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 200
    # 退役节点不需要重绑,不该出现在「孤儿路由」提示里
    assert delete_response.json()["data"]["orphaned_route_node_ids"] == ["snowflake_step_candidates"]

    before = client.get("/api/v1/system-config/llm").json()["data"]
    assert before["stale_routes"] == [RETIRED_NODE_ID]
    assert before["node_routes"]["snowflake_step_candidates"]["ready"] is False

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["pruned_stale_routes"] == [RETIRED_NODE_ID]
    assert payload["snapshot"]["active"] is True
    assert RETIRED_NODE_ID not in payload["snapshot"]["parsed"]["node_routing"]
    # 重评 R4:写路径不再抄 task_routing(注册表的旧抄本)
    assert "task_routing" not in payload["snapshot"]["parsed"]
    assert "snowflake_step_candidates" in payload["synced_node_ids"]

    after = client.get("/api/v1/system-config/llm").json()["data"]
    assert after["stale_routes"] == []
    assert after["missing_active_routes"] == []
    assert after["readiness"]["blocked_routes"] == []
    for node_id in active_llm_node_ids():
        assert after["node_routes"][node_id]["provider_id"] == "local_qwen"
        assert after["node_routes"][node_id]["ready"] is True


def test_sync_missing_prunes_stale_task_routing_entry_from_legacy_snapshot(client, session, monkeypatch) -> None:
    """老安装的 task_routing 也可能带退役节点(解析时会并入 node_routing),同样要剪。"""
    _enable_admin(monkeypatch)
    assert _create_provider(client, "local_qwen").status_code == 200
    _seed_active_snapshot(
        session,
        category="models",
        parsed=_legacy_models_payload(**{RETIRED_TASK_ID: _route("deleted_provider", model="gpt-old")}),
    )

    before = client.get("/api/v1/system-config/llm").json()["data"]
    assert before["stale_routes"] == [RETIRED_TASK_ID]

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["pruned_stale_routes"] == [RETIRED_TASK_ID]
    parsed = payload["snapshot"]["parsed"]
    assert RETIRED_TASK_ID not in parsed["node_routing"]
    # 老快照 task_routing 里还在起作用的条目并进 node_routing,整张表不再存(重评 R4);
    # stylize 别名不必存,解析时照旧从 style_draft 镜像出来
    assert set(parsed) == {"node_routing"}
    assert parse_model_routing_config(parsed).task_routing["stylize"].model == "Qwen3-14B-Q8_0.gguf"
    assert payload["overview"]["stale_routes"] == []
    assert payload["overview"]["missing_active_routes"] == []


# 2026-09-30 重评 R15b:删掉的四个保留节点里,archive / chapter_aggregate 在真实安装的活动快照里还带着
# task_routing 条目(老版 models.yaml 抄进去的)。它们现在是退役路由:overview 列在 stale_routes 里、
# 不计入就绪统计,下一次「一键补齐」/ 分工保存把它们剪掉,激活不因它们 422。
_RESERVED_LEGACY_IDS = ("archive", "chapter_aggregate")


def _live_shaped_models_payload(provider_id: str) -> dict:
    """真实安装形状的活动 models 快照:每个节点都绑在一个服务上,task_routing 里还留着两个保留节点。"""
    node_routing = {
        node_id: default_task_config_payload(
            node_id,
            provider_id=provider_id,
            provider="openai_compatible",
            model="Qwen3-14B-Q8_0.gguf",
            api_mode="chat",
            credential_mode="none",
        )
        for node_id in active_llm_node_ids()
    }
    task_routing = {
        node_id: _route(provider_id, max_output_tokens=4000 if node_id == "chapter_aggregate" else 1200)
        for node_id in _RESERVED_LEGACY_IDS
    }
    return {"task_routing": task_routing, "node_routing": node_routing, "retry_budget": {}, "job_runtime": {}}


def test_reserved_node_routes_in_live_snapshot_are_stale_and_pruned_by_sync_missing(client, session, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    assert _create_provider(client, "local_qwen").status_code == 200
    _seed_active_snapshot(session, category="models", parsed=_live_shaped_models_payload("local_qwen"))

    before = client.get("/api/v1/system-config/llm").json()["data"]
    assert before["stale_routes"] == list(_RESERVED_LEGACY_IDS)
    assert before["readiness"]["active_route_count"] == 32
    assert before["missing_active_routes"] == []
    for node_id in _RESERVED_LEGACY_IDS:
        assert node_id not in before["node_routes"]
        assert node_id not in before["node_catalog"]

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["pruned_stale_routes"] == list(_RESERVED_LEGACY_IDS)
    assert not set(_RESERVED_LEGACY_IDS) & set(payload["snapshot"]["parsed"]["node_routing"])
    assert "task_routing" not in payload["snapshot"]["parsed"]
    assert payload["overview"]["stale_routes"] == []
    assert payload["overview"]["readiness"]["active_route_count"] == 32


def test_role_routes_save_activates_with_reserved_nodes_left_in_legacy_task_routing(client, session, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    assert _create_provider(client, "local_qwen").status_code == 200
    _seed_active_snapshot(session, category="models", parsed=_live_shaped_models_payload("local_qwen"))

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={
            "assignments": {"review": {"provider_id": "local_qwen", "model": "Qwen3-14B-Q8_0.gguf"}},
            "activate": True,
        },
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["snapshot"]["active"] is True
    assert payload["pruned_stale_routes"] == list(_RESERVED_LEGACY_IDS)
    assert payload["overview"]["stale_routes"] == []
    assert payload["overview"]["readiness"]["active_route_count"] == 32


def test_role_routes_save_prunes_stale_routes_and_reports_them(client, session, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    assert _create_provider(client, "local_qwen").status_code == 200
    _seed_active_snapshot(
        session,
        category="models",
        parsed=_legacy_models_payload(**{RETIRED_TASK_ID: _route("deleted_provider", model="gpt-old")}),
    )

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={
            "assignments": {"drafting": {"provider_id": "local_qwen", "model": "Qwen3-14B-Q8_0.gguf"}},
            "activate": True,
        },
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["pruned_stale_routes"] == [RETIRED_TASK_ID]
    assert "task_routing" not in payload["snapshot"]["parsed"]
    assert RETIRED_TASK_ID not in payload["snapshot"]["parsed"]["node_routing"]
    assert payload["overview"]["stale_routes"] == []


# --------------------------------------------------------------------------- (2)
def test_provider_save_rejects_invalid_api_mode(client, monkeypatch) -> None:
    _enable_admin(monkeypatch)

    response = _create_provider(client, "bad_mode", api_mode="completions")
    assert response.status_code == 422, response.json()
    error = response.json()["error"]
    assert error["code"] == "CONFIG_PROVIDER_INVALID"
    assert "api_mode" in error["message"]
    assert "completions" in error["message"]
    assert "bad_mode" in error["message"]

    overview = client.get("/api/v1/system-config/llm").json()["data"]
    assert "bad_mode" not in overview["providers"]


def test_role_routes_names_provider_whose_stored_api_mode_is_invalid(client, session, monkeypatch) -> None:
    """老快照里的服务 api_mode 非法:角色分工保存要 422 点名该服务,而不是 500。"""
    _enable_admin(monkeypatch)
    _seed_active_snapshot(
        session,
        category="api",
        parsed={
            "llm": {
                "enabled": True,
                "timeout_seconds": 0,
                "default_provider_id": "bad_mode",
                "providers": {
                    "bad_mode": {
                        "provider_id": "bad_mode",
                        "provider_type": "openai_compatible",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "enabled": True,
                        "credential_mode": "none",
                        "api_mode": "completions",
                        "models": ["Qwen3-14B-Q8_0.gguf"],
                    }
                },
            }
        },
    )

    for path, body in (
        (
            "/api/v1/system-config/llm/role-routes",
            {"assignments": {"drafting": {"provider_id": "bad_mode", "model": "Qwen3-14B-Q8_0.gguf"}}, "activate": True},
        ),
        ("/api/v1/system-config/llm/node-routes/sync-missing", {"activate": True}),
    ):
        response = client.post(path, headers=ADMIN_HEADERS, json=body)
        assert response.status_code == 422, (path, response.json())
        error = response.json()["error"]
        assert error["code"] == "CONFIG_PROVIDER_INVALID"
        assert "bad_mode" in error["message"]
        assert "api_mode" in error["message"]


def test_invalid_stored_route_is_a_domain_error_for_role_routes_and_is_replaced_by_sync_missing(
    client, session, monkeypatch
) -> None:
    """活动 models 快照里某个目录内节点带非法 api_mode:

    - 分工保存不碰这个节点(它不在这次的槽里)→ 422 点名节点,不是 500;
    - 一键补齐:overview 读不懂这份快照,每个节点都算未配好,于是整张表按当前服务重写——非法路由被换掉。
      (重评 R4 之前写路径还会把老快照的 task_routing 原样抄进新快照,非法条目跟着留下,补齐也只能 422。)
    """
    _enable_admin(monkeypatch)
    assert _create_provider(client, "local_qwen").status_code == 200
    _seed_active_snapshot(
        session,
        category="models",
        parsed=_legacy_models_payload(neutral_draft=_route("local_qwen", api_mode="completions")),
    )

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"review": {"provider_id": "local_qwen", "model": "Qwen3-14B-Q8_0.gguf"}}, "activate": True},
    )
    assert response.status_code == 422, response.json()
    error = response.json()["error"]
    assert error["code"] == "CONFIG_ROUTE_INVALID"
    assert "neutral_draft" in error["message"]
    assert "api_mode" in error["message"]

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["snapshot"]["parsed"]["node_routing"]["neutral_draft"]["api_mode"] == "chat"
    assert payload["overview"]["missing_active_routes"] == []
    assert payload["overview"]["node_routes"]["neutral_draft"]["ready"] is True
