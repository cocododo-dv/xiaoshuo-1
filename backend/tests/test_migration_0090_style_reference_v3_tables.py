"""迁移 20260923_0090：风格参考 v3 的四张新表（作业 / 窗口索引 / 读数 / 每场冻结选窗），可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _migrate

PREVIOUS_HEAD = "20260920_0089"
THIS_REVISION = "20260923_0090"
TABLES = {
    "style_reference_jobs": {
        "job_id", "kind", "book_id", "profile_id", "op_key", "state", "phase", "cancel_requested",
        "attempt", "owner_token", "heartbeat_at", "params_json", "cursor_json", "progress_json",
        "result_json", "error_json", "created_at", "updated_at", "started_at", "finished_at",
    },
    "style_reference_windows": {
        "window_id", "book_id", "index_version", "root_sha256", "window_no", "start_index", "end_index",
        "chapter_no", "position", "chars", "paragraph_count", "type_mix_json", "dialogue_share",
        "typicality", "features_json", "tags_json", "tags_version", "created_at", "updated_at",
    },
    "style_fidelity_readings": {
        "reading_id", "project_id", "scene_id", "profile_id", "binding_id", "source", "stage",
        "draft_ref", "text_sha256", "char_count", "percentile", "distance", "reading_json",
        "judge_json", "copy_check_json", "created_at",
    },
    "style_reference_scene_windows": {
        "selection_id", "selection_key", "scene_id", "bundle_id", "contract_hash", "window_refs_json",
        "params_json", "created_at",
    },
}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_0090_creates_v3_tables_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "style-reference-v3-0090.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert not (set(TABLES) & _tables(path))

    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    tables = _tables(path)
    for table, columns in TABLES.items():
        assert table in tables
        assert columns <= _columns(path, table), table

    with sqlite3.connect(path) as connection:
        window_indexes = {row[1]: int(row[2]) for row in connection.execute("PRAGMA index_list('style_reference_windows')")}
        assert window_indexes.get("ix_style_reference_windows_book_version_chapter") == 0
        # 一本书一个索引版本里窗口号唯一（内联 UNIQUE → sqlite 自动索引）
        assert any(name.startswith("sqlite_autoindex_style_reference_windows") and unique == 1 for name, unique in window_indexes.items())
        job_indexes = {row[1] for row in connection.execute("PRAGMA index_list('style_reference_jobs')")}
        assert {"ix_style_reference_jobs_book_kind_state", "ix_style_reference_jobs_state_heartbeat"} <= job_indexes
        selection_indexes = {row[1]: int(row[2]) for row in connection.execute("PRAGMA index_list('style_reference_scene_windows')")}
        assert any(name.startswith("sqlite_autoindex_style_reference_scene_windows") and unique == 1 for name, unique in selection_indexes.items())
        # 读数表刻意不存 chapter_id：场景改章时不必跟着搬（scene_rehome 守卫只盯同时带 scene_id 与 chapter_id 的表）
        assert "chapter_id" not in _columns(path, "style_fidelity_readings")

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert not (set(TABLES) & _tables(path))
