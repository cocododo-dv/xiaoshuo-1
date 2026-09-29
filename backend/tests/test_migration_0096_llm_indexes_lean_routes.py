"""迁移 20260929_0096：成本看板的三个索引 + 活动 models 快照只存作者的选择（批准#5a / 重评 R4），可降级。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from novel_system.services.llm_node_registry import get_llm_node_spec
from novel_system.services.llm_routing import parse_model_routing_config
from tests.test_migration_0084_scene_plan_rendering_mode import _migrate

PREVIOUS_HEAD = "20260924_0092"
CURRENT_HEAD = "20260929_0096"
INDEXES = {
    "llm_calls": {"ix_llm_calls_project_created", "ix_llm_calls_chapter"},
    "llm_call_attempts": {"ix_llm_call_attempts_created"},
}
MIGRATION_FILE = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20260929_0096_llm_ledger_indexes_lean_model_routes.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0096", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _full_route(node_id: str, **choice) -> dict:
    """老写路径抄进快照的整份路由：作者的选择 + 节点 spec 的全部默认参数（当年的值，可能已过时）。"""
    return {
        "provider": "openai_compatible",
        "temperature": 0.25,
        "max_output_tokens": 3200,  # 过时的默认值：迁移后应取 spec 的现值
        "response_format": "json_object",
        "reasoning_level": "medium",
        "model_profile": "quality_strong",
        **choice,
    }


def _live_shaped_models_payload() -> dict:
    relay = {"provider_id": "relay", "model": "relay-sonnet", "api_mode": "chat", "credential_mode": "api_key"}
    return {
        "model_profiles": {"quality_strong": {"label": "精修"}},
        "task_routing": {
            # yaml 的整张抄本：node_routing 里已有的节点被遮住，不起作用
            "snowflake_step_generate": _full_route("snowflake_step_generate", model="gpt-5"),
            # node_routing 里没有的节点：它的路由此前就是这一条（解析时并进 node_routing）
            "hard_qc": _full_route("hard_qc", model="gpt-5", max_output_tokens=2200),
            "stylize": _full_route("stylize", model="gpt-5", temperature=0.8),
            "archive": _full_route("archive", model="gpt-5-mini", max_output_tokens=1200),
        },
        "node_routing": {
            "snowflake_step_generate": _full_route("snowflake_step_generate", **relay),
            "style_draft": _full_route(
                "style_draft", **relay, temperature=0.8, frequency_penalty=0.0, presence_penalty=0.0,
                provider_options={"extra_payload": {"seed": 7}},
            ),
            "writer_passage_patch": _full_route("writer_passage_patch", **relay, max_output_tokens=2600),
            "long_form_continuation": _full_route("long_form_continuation", **relay),  # 退役节点：原样留着
        },
        "retry_budget": {"provider_attempt_budget": 32, "hard_partial_max": 2, "total_attempt_budget": 4},
        "job_runtime": {"idempotency_claim_ttl_seconds": 600, "reindex_lease_ttl_seconds": 30},
        "role_assignments": {"drafting": {"provider_id": "relay", "model": "relay-sonnet"}},
    }


def _insert_snapshot(connection: sqlite3.Connection, snapshot_id: str, version: int, parsed: dict, *, active: bool) -> None:
    connection.execute(
        "INSERT INTO system_config_snapshots (snapshot_id, category, version, yaml_raw, parsed_json, validation_json, "
        "status, active_flag, created_by, created_at, activated_at) VALUES (?, 'models', ?, ?, ?, ?, ?, ?, 'legacy', ?, ?)",
        (
            snapshot_id,
            version,
            yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False),
            json.dumps(parsed),
            json.dumps({"ok": True, "message": "models config is valid"}),
            "active" if active else "superseded",
            1 if active else 0,
            f"2026-09-2{version}T00:00:00+00:00",
            f"2026-09-2{version}T00:00:00+00:00",
        ),
    )


def _index_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA index_list("{table}")')}


def _snapshots(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT snapshot_id, version, status, active_flag, created_by FROM system_config_snapshots "
        "WHERE category = 'models' ORDER BY version"
    ).fetchall()


def test_0096_adds_ledger_indexes_and_slims_the_active_models_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lean-routes-0096.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    live = _live_shaped_models_payload()
    with sqlite3.connect(path) as connection:
        for table, names in INDEXES.items():
            assert not names & _index_names(connection, table)
        _insert_snapshot(connection, "config_models_old", 1, {"task_routing": {}}, active=False)
        _insert_snapshot(connection, "config_models_live", 2, live, active=True)
        connection.commit()

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)  # 显式升到本迁移：以后再加迁移不必回来改这个文件
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        for table, names in INDEXES.items():
            assert names <= _index_names(connection, table)
        rows = _snapshots(connection)
        assert [row[:4] for row in rows[:2]] == [
            ("config_models_old", 1, "superseded", 0),
            ("config_models_live", 2, "superseded", 0),
        ]
        migrated_id, version, status, active, created_by = rows[2]
        assert (version, status, active, created_by) == (3, "active", 1, "migration:20260929_0096")
        yaml_raw, parsed_json, validation_json = connection.execute(
            "SELECT yaml_raw, parsed_json, validation_json FROM system_config_snapshots WHERE snapshot_id = ?", (migrated_id,)
        ).fetchone()
    lean = json.loads(parsed_json)
    assert yaml.safe_load(yaml_raw) == lean
    assert json.loads(validation_json)["ok"] is True
    assert json.loads(validation_json)["migrated_from"] == "config_models_live"

    # 只剩 node_routing：抄本、别名、没人读的段、仓库管的两段都不再存
    assert set(lean) == {"node_routing"}
    relay_choice = {"provider": "openai_compatible", "provider_id": "relay", "model": "relay-sonnet", "api_mode": "chat", "credential_mode": "api_key"}
    assert lean["node_routing"]["snowflake_step_generate"] == relay_choice
    assert lean["node_routing"]["writer_passage_patch"] == relay_choice
    # 显式覆盖（provider_options）留着，抄进去的默认参数去掉
    assert lean["node_routing"]["style_draft"] == {**relay_choice, "provider_options": {"extra_payload": {"seed": 7}}}
    # node_routing 里没有、此前靠 task_routing 起作用的节点：并进来，同样只留选择
    assert lean["node_routing"]["hard_qc"] == {"provider": "openai_compatible", "model": "gpt-5"}
    # 退役节点原样留着（设置页列为 stale_routes，下一次一键补齐剪掉）
    assert lean["node_routing"]["long_form_continuation"] == live["node_routing"]["long_form_continuation"]
    assert lean["node_routing"]["archive"] == live["task_routing"]["archive"]
    assert "stylize" not in lean["node_routing"]

    # 解析结果：作者的选择原样，参数取节点 spec 的现值（代码里修好的默认值到达实例）
    routing = parse_model_routing_config(lean)
    for node_id in ("snowflake_step_generate", "writer_passage_patch", "style_draft", "hard_qc"):
        spec = get_llm_node_spec(node_id)
        route = routing.node_routing[node_id]
        assert route.max_output_tokens == spec.max_output_tokens
        assert route.temperature == spec.temperature
        assert route.reasoning_level == spec.reasoning_level
    assert routing.node_routing["snowflake_step_generate"].max_output_tokens == 8192
    assert routing.node_routing["writer_passage_patch"].max_output_tokens == 8192
    assert routing.node_routing["style_draft"].model == "relay-sonnet"
    assert routing.node_routing["style_draft"].frequency_penalty == 0.0
    assert routing.node_routing["style_draft"].provider_options == {"extra_payload": {"seed": 7}}

    # 降级：索引没了，迁移另存的快照删掉，来源快照重新激活
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    with sqlite3.connect(path) as connection:
        for table, names in INDEXES.items():
            assert not names & _index_names(connection, table)
        assert [row[:4] for row in _snapshots(connection)] == [
            ("config_models_old", 1, "superseded", 0),
            ("config_models_live", 2, "active", 1),
        ]

    # 再升一次结果相同；升完之后的快照已是瘦身形状，再跑一遍瘦身什么都不做
    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        rows = _snapshots(connection)
        assert len(rows) == 3 and rows[2][2:4] == ("active", 1)
        reslimmed = json.loads(
            connection.execute("SELECT parsed_json FROM system_config_snapshots WHERE snapshot_id = ?", (rows[2][0],)).fetchone()[0]
        )
    assert reslimmed == lean
    assert _migration_module().lean_models_payload(lean) == lean


def test_0096_leaves_an_already_lean_snapshot_and_later_author_saves_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "lean-routes-0096-noop.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    lean = {"node_routing": {"neutral_draft": {"provider": "openai_compatible", "provider_id": "relay", "model": "m", "api_mode": "chat"}}}
    with sqlite3.connect(path) as connection:
        _insert_snapshot(connection, "config_models_lean", 1, lean, active=True)
        connection.commit()

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        assert [row[:4] for row in _snapshots(connection)] == [("config_models_lean", 1, "active", 1)]

    # 作者在迁移之后又保存了路由：降级不回滚作者的保存
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE system_config_snapshots SET active_flag = 0, status = 'superseded'")
        _insert_snapshot(connection, "config_models_author", 2, lean, active=True)
        connection.commit()
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    with sqlite3.connect(path) as connection:
        assert [row[:4] for row in _snapshots(connection)] == [
            ("config_models_lean", 1, "superseded", 0),
            ("config_models_author", 2, "active", 1),
        ]
