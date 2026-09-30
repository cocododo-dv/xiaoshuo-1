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

批准#5a 之后快照里的路由只存作者的选择:节点以后退役时它那条瘦路由没有 spec 可补参数,照样只是惰性的退役
路由;一键补齐逐个节点看自己存着的路由,只重绑没配、配坏了或不就绪的节点,作者解析得了的路由一条不动。
"""

from __future__ import annotations

import uuid

import yaml

from sqlalchemy import func, select, update

from novel_system.db.models import SystemConfigSnapshot, utcnow
from novel_system.services.llm_node_registry import (
    active_llm_node_ids,
    default_task_config_payload,
    llm_node_catalog,
    role_slot_node_ids,
)
from novel_system.services.llm_routing import (
    ROUTE_CHOICE_FIELDS,
    load_model_routing_config,
    parse_model_routing_config,
    registry_default_node_routing,
    resolve_node_route,
)
from novel_system.services.system_config import default_config_payload, validate_config


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


def _replace_active_snapshot(session, *, category: str, parsed: dict) -> str:
    """把当前活动快照换成 ``parsed``(另存下一个版本、旧的标 superseded),模拟以后的版本读到的库。"""
    session.execute(
        update(SystemConfigSnapshot)
        .where(SystemConfigSnapshot.category == category, SystemConfigSnapshot.active_flag == 1)
        .values(active_flag=0, status="superseded")
    )
    version = session.execute(
        select(func.max(SystemConfigSnapshot.version)).where(SystemConfigSnapshot.category == category)
    ).scalar_one_or_none()
    snapshot_id = f"config_{category}_{uuid.uuid4().hex[:12]}"
    session.add(
        SystemConfigSnapshot(
            snapshot_id=snapshot_id,
            category=category,
            version=int(version or 0) + 1,
            yaml_raw=yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False),
            parsed_json=parsed,
            validation_json={"ok": True, "message": f"{category} config is valid"},
            status="active",
            active_flag=1,
            activated_at=utcnow(),
            created_by="later-release",
        )
    )
    session.commit()
    return snapshot_id


def _ui_configured_install(client) -> dict:
    """经界面写路径配好的安装:一键补齐把每个节点绑到服务的第一个模型(model-a),再把「写作主力」分给 model-b。
    返回活动 models 快照的内容(批准#5a 之后的瘦路由:只存作者的选择)。"""
    assert _create_provider(client, "local_qwen", models=["model-a", "model-b"]).status_code == 200
    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"drafting": {"provider_id": "local_qwen", "model": "model-b"}}, "activate": True},
    )
    assert response.status_code == 200, response.json()
    parsed = response.json()["data"]["snapshot"]["parsed"]
    assert set(parsed) == {"node_routing"}
    return parsed


def _assert_author_models_kept(node_routing: dict, *, except_ids: tuple[str, ...] = ()) -> None:
    """「写作主力」槽里的节点仍是 model-b,其余节点仍是 model-a。"""
    drafting = set(role_slot_node_ids("drafting"))
    for node_id in active_llm_node_ids():
        if node_id in except_ids:
            continue
        expected = "model-b" if node_id in drafting else "model-a"
        assert node_routing[node_id]["model"] == expected, node_id
        assert node_routing[node_id]["provider_id"] == "local_qwen", node_id


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


# 批准#5a 之后快照里的路由只存作者的选择,缺的参数解析时取节点 spec。以后的版本把某个节点从注册表删掉时
# (第三批要重写场景流水线),它那条瘦路由没有 spec 可补——不能因此拖垮整张表:运行时每次调用都
# LLM_MODEL_CONFIG_INVALID、设置页显示全部未指派、「一键补齐」把作者每个槽选的模型都改回默认。它应当和以前
# 整份抄进快照的退役路由一样:惰性地留在表里,设置页列为 stale_routes,下一次一键补齐 / 分工剪掉,别的不动。
RETIRED_LEAN_ID = "retired_lean_probe"
RETIRED_BROKEN_ID = "retired_broken_probe"


def test_lean_route_of_a_node_retired_later_is_an_inert_stale_route(client, session, monkeypatch) -> None:
    _enable_admin(monkeypatch)
    parsed = _ui_configured_install(client)
    drafting = role_slot_node_ids("drafting")
    lean = dict(parsed["node_routing"][drafting[0]])
    assert set(lean) <= set(ROUTE_CHOICE_FIELDS)
    retired = {
        RETIRED_LEAN_ID: lean,
        # 连中性占位都救不了的(字段值非法)也只是放在一边,照样列为 stale、照样剪掉
        RETIRED_BROKEN_ID: {**lean, "response_format": "xml"},
    }
    stored = {"node_routing": {**parsed["node_routing"], **retired}}
    for node_id in retired:
        assert node_id not in llm_node_catalog()
    _replace_active_snapshot(session, category="models", parsed=stored)

    # 运行时照常加载,作者的选择原样生效
    routing = load_model_routing_config()
    assert resolve_node_route(routing, drafting[0]).model == "model-b"
    assert resolve_node_route(routing, "hard_qc").model == "model-a"
    # 草稿校验(raise_llm_output_budget 经 create_draft 走这里)也放行
    assert validate_config("models", yaml.safe_dump(stored))[1]["ok"] is True

    overview = client.get("/api/v1/system-config/llm").json()["data"]
    assert overview["stale_routes"] == sorted(retired)
    assert overview["missing_active_routes"] == []
    assert overview["readiness"]["configured_route_count"] == len(active_llm_node_ids())
    assert overview["readiness"]["ready"] is True
    assert not set(retired) & set(overview["node_routes"])

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["synced_node_ids"] == []
    assert payload["pruned_stale_routes"] == sorted(retired)
    assert payload["snapshot"]["parsed"] == parsed
    _assert_author_models_kept(payload["snapshot"]["parsed"]["node_routing"])
    assert payload["overview"]["stale_routes"] == []


def test_route_without_spec_parses_with_neutral_placeholders_and_a_broken_one_is_set_aside() -> None:
    lean = {"provider": "openai_compatible", "provider_id": "local_qwen", "model": "model-a", "api_mode": "chat"}
    routing = parse_model_routing_config(
        {
            "node_routing": {
                RETIRED_LEAN_ID: lean,
                RETIRED_BROKEN_ID: {**lean, "api_mode": "completions"},
                "hard_qc": lean,
            }
        }
    )
    retired_route = routing.node_routing[RETIRED_LEAN_ID]
    assert (retired_route.temperature, retired_route.max_output_tokens, retired_route.response_format) == (
        0.2,
        2200,
        "json_object",
    )
    assert retired_route.reasoning_level == "medium"
    assert RETIRED_BROKEN_ID not in routing.node_routing
    assert routing.node_routing["hard_qc"].model == "model-a"


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


def test_sync_missing_replaces_only_the_node_whose_own_stored_route_is_broken(client, session, monkeypatch) -> None:
    """一个目录内节点自己的路由解析不了(老快照 task_routing 里的非法 api_mode,node_routing 里没有它),
    其余路由是作者经界面配的:

    - 分工保存不碰这个节点(它不在这次的槽里)→ 422 点名节点,不是 500,活动快照不动;
    - 一键补齐只换掉这一个节点,作者给每个槽选的模型原样留着——overview 读不懂这份快照时说「全部未配」,
      不能拿它决定重写谁。
    """
    _enable_admin(monkeypatch)
    parsed = _ui_configured_install(client)
    node_routing = {node_id: route for node_id, route in parsed["node_routing"].items() if node_id != "neutral_draft"}
    seeded_id = _replace_active_snapshot(
        session,
        category="models",
        parsed={
            "node_routing": node_routing,
            "task_routing": {"neutral_draft": _route("local_qwen", model="model-b", api_mode="completions")},
        },
    )

    response = client.post(
        "/api/v1/system-config/llm/role-routes",
        headers=ADMIN_HEADERS,
        json={"assignments": {"review": {"provider_id": "local_qwen", "model": "model-b"}}, "activate": True},
    )
    assert response.status_code == 422, response.json()
    error = response.json()["error"]
    assert error["code"] == "CONFIG_ROUTE_INVALID"
    assert "neutral_draft" in error["message"]
    assert "api_mode" in error["message"]
    assert client.get("/api/v1/system-config/llm").json()["data"]["models_snapshot"]["snapshot_id"] == seeded_id

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["synced_node_ids"] == ["neutral_draft"]
    stored = payload["snapshot"]["parsed"]
    assert set(stored) == {"node_routing"}
    assert stored["node_routing"]["neutral_draft"]["model"] == "model-a"
    assert stored["node_routing"]["neutral_draft"]["api_mode"] == "chat"
    _assert_author_models_kept(stored["node_routing"], except_ids=("neutral_draft",))
    assert payload["overview"]["missing_active_routes"] == []
    assert payload["overview"]["node_routes"]["neutral_draft"]["ready"] is True
    assert resolve_node_route(load_model_routing_config(), "style_draft").model == "model-b"


def test_sync_missing_keeps_every_author_route_when_only_a_shadowed_legacy_copy_is_broken(
    client, session, monkeypatch
) -> None:
    """老快照 task_routing 里一条读不懂的旧抄本(非法 response_format),它的节点在 node_routing 里有作者的路由:
    运行时、overview 都读不懂整份快照,但它不是任何节点正在用的路由。一键补齐一个节点都不重写,新快照不再带
    旧抄本,运行时于是又读得懂了。"""
    _enable_admin(monkeypatch)
    parsed = _ui_configured_install(client)
    _replace_active_snapshot(
        session,
        category="models",
        parsed={
            "node_routing": parsed["node_routing"],
            "task_routing": {"style_draft": _route("local_qwen", model="model-a", response_format="xml")},
        },
    )

    response = client.post(
        "/api/v1/system-config/llm/node-routes/sync-missing",
        headers=ADMIN_HEADERS,
        json={"activate": True},
    )
    assert response.status_code == 200, response.json()
    payload = response.json()["data"]
    assert payload["synced_node_ids"] == []
    assert payload["snapshot"]["parsed"] == parsed
    _assert_author_models_kept(payload["snapshot"]["parsed"]["node_routing"])
    assert resolve_node_route(load_model_routing_config(), "style_draft").model == "model-b"
