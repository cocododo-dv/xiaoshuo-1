"""迁移 20260924_0092：风格参考 v3 清理的数据回填——绑定配置改成 v3 四键、旧画像归档、旧版待办行删除。

升级前的库里有：三条旧形状的绑定（策略 A + 强度 / 策略 B 无配置 / 已经是 v3 四键的 mixed）、四份画像
（旧版 active / 旧版 draft / 旧版已归档 / v3 active）、四张待办卡（三种旧前缀 + 一张无关的）。升级只改数据、不改结构；
降级是空操作（数据原样）；再升一次不再改动（幂等）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260923_0091"
THIS_REVISION = "20260924_0092"
BINDINGS = "style_reference_injection_bindings"
PROFILES = "style_reference_profiles"
REVIEW_ITEMS = "review_items"


def _seed(path: Path) -> None:
    """原生 sqlite3 连接不开外键：各表只补必填列即可。"""
    with sqlite3.connect(path) as connection:
        for binding_id, strategy, config in (
            ("bind_a", "A", {"intensity": 50, "sub_dimensions": ["language.vocabulary"], "include_metric": True}),
            ("bind_b", "B", None),
            (
                "bind_v3",
                "mixed",
                {
                    "reference_mode": "samples_only",
                    "sample_windows": 5,
                    "dimension_states": {"scene.dialogue": "emphasize"},
                    "draft_mode": "neutral_first",
                },
            ),
            ("bind_c_bad_draft", "C", {"intensity": 100, "draft_mode": "hybrid", "sample_windows": 99}),
        ):
            _insert_minimal_row(
                connection,
                BINDINGS,
                {
                    "binding_id": binding_id,
                    "profile_id": "profile_legacy_active",
                    "scope": "project",
                    "scope_ref_id": f"proj_{binding_id}",
                    "task_type": "scene_generation",
                    "strategy": strategy,
                    "config_json": json.dumps(config) if config is not None else "{}",
                    "status": "active",
                },
            )
        for profile_id, status, profile_json in (
            ("profile_legacy_active", "active", {"style_features": ["短句"]}),
            ("profile_legacy_draft", "draft", {"qualitative_summary": "x"}),
            ("profile_legacy_archived", "archived", {}),
            ("profile_v3", "active", {"profile_version": "style_profile_v3", "dimension_card": {"version": "dimension_card_v1"}}),
        ):
            _insert_minimal_row(
                connection,
                PROFILES,
                {
                    "profile_id": profile_id,
                    "book_id": "book_0092",
                    "run_id": "run_0092",
                    "title": profile_id,
                    "status": status,
                    "profile_json": json.dumps(profile_json, ensure_ascii=False),
                    "coverage_json": "{}",
                    "source_finding_ids_json": "[]",
                },
            )
        for review_id in (
            "review_style_ref_finding_abcdef123456",
            "review_style_ref_apply_abcdef123456_p1",
            "review_style_ref_calib_abcdef123456_1",
            "review_scene_memory_keep_me",
        ):
            _insert_minimal_row(
                connection,
                REVIEW_ITEMS,
                {
                    "review_id": review_id,
                    "item_type": "style_observation",
                    "status": "pending",
                    "candidate_text": "x",
                    "candidate_payload_json": "{}",
                    "project_id": "proj_0092",
                },
            )


def _bindings(path: Path) -> dict[str, tuple[str, dict]]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]: (row[1], json.loads(row[2]))
            for row in connection.execute(f"SELECT binding_id, strategy, config_json FROM {BINDINGS}")
        }


def _profile_status(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute(f"SELECT profile_id, status FROM {PROFILES}").fetchall())


def _review_ids(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute(f"SELECT review_id FROM {REVIEW_ITEMS}")}


def test_0092_backfills_bindings_archives_legacy_profiles_and_drops_legacy_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "style-reference-v3-0092.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    columns_before = {table: _columns(path, table) for table in (BINDINGS, PROFILES, REVIEW_ITEMS)}
    _seed(path)

    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (THIS_REVISION,)
    # 只改数据：三张表的列一个没动
    assert {table: _columns(path, table) for table in (BINDINGS, PROFILES, REVIEW_ITEMS)} == columns_before

    bindings = _bindings(path)
    assert {strategy for strategy, _config in bindings.values()} == {"mixed"}
    # 策略 A + 强度 50 → 只用文风卡、round(3 + 9·0.5) = 8 窗；旧键不再写；draft_mode 不回填
    assert bindings["bind_a"][1] == {"reference_mode": "card_only", "sample_windows": 8, "dimension_states": {}}
    # 策略 B、没有配置 → 全面模仿、12 窗
    assert bindings["bind_b"][1] == {"reference_mode": "full", "sample_windows": 12, "dimension_states": {}}
    # 已经是 v3 四键的行原样保留（含 draft_mode 与维度状态）
    assert bindings["bind_v3"][1] == {
        "reference_mode": "samples_only",
        "sample_windows": 5,
        "dimension_states": {"scene.dialogue": "emphasize"},
        "draft_mode": "neutral_first",
    }
    # 策略 C → full；已有的 sample_windows 夹到 16、优先于 intensity；不合法的 draft_mode 丢掉
    assert bindings["bind_c_bad_draft"][1] == {"reference_mode": "full", "sample_windows": 16, "dimension_states": {}}

    status = _profile_status(path)
    assert status == {
        "profile_legacy_active": "archived",
        "profile_legacy_draft": "archived",
        "profile_legacy_archived": "archived",
        "profile_v3": "active",
    }
    assert _review_ids(path) == {"review_scene_memory_keep_me"}

    # 降级是空操作：数据原样；再升一次不再改动
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert _bindings(path) == bindings and _profile_status(path) == status
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    assert _bindings(path) == bindings and _profile_status(path) == status
    assert _review_ids(path) == {"review_scene_memory_keep_me"}


def test_0092_v3_binding_config_mapping_is_the_documented_one() -> None:
    """回填公式本身（不必起库）：A → card_only、其余 → full；强度 0 → 3 窗、100 → 12 窗；draft_mode 只保留合法值。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migration_0092",
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20260924_0092_style_reference_v3_backfill.py",
    )
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    v3 = migration.v3_binding_config
    assert v3("A", {})["reference_mode"] == "card_only"
    for strategy in ("B", "C", "mixed", None, ""):
        assert v3(strategy, {})["reference_mode"] == "full"
    assert v3("A", {"reference_mode": "full"})["reference_mode"] == "full"  # 已有合法值不动
    assert v3("mixed", {"intensity": 0})["sample_windows"] == 3
    assert v3("mixed", {"intensity": 100})["sample_windows"] == 12
    assert v3("mixed", {"intensity": "garbage"})["sample_windows"] == 12
    assert v3("mixed", {"sample_windows": -3})["sample_windows"] == 0
    assert "draft_mode" not in v3("mixed", {}) and "draft_mode" not in v3("mixed", {"draft_mode": "hybrid"})
    assert v3("mixed", {"draft_mode": "Style_First"})["draft_mode"] == "style_first"
    assert v3("mixed", {"dimension_states": ["bad"]})["dimension_states"] == {}
