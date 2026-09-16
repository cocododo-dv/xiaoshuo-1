"""迁移 20260916_0087：snowflake_direction_briefs（作者意图要点，一步一行），可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260915_0086"
CURRENT_HEAD = "20260916_0087"
TABLE = "snowflake_direction_briefs"


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_0087_creates_direction_briefs_with_unique_step_index_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "direction-briefs-0087.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert TABLE not in _tables(path)

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    assert TABLE in _tables(path)
    assert {
        "brief_id", "project_id", "step_key", "lines_json", "inherit_upstream", "revision",
        "source_turn_ids_json", "author_edited_at", "created_at", "updated_at",
    } <= _columns(path, TABLE)

    with sqlite3.connect(path) as connection:
        indexes = connection.execute(f"PRAGMA index_list('{TABLE}')").fetchall()
        assert any(row[1] == "ix_snowflake_direction_briefs_step" and int(row[2]) == 1 for row in indexes), indexes
        # 一步一行：同一作品同一步的第二行被唯一索引拦下（原生 sqlite3 不开外键，不必先建作品行）
        _insert_minimal_row(connection, TABLE, {"brief_id": "b1", "project_id": "prj", "step_key": "book_brief", "lines_json": "[]"})
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_minimal_row(connection, TABLE, {"brief_id": "b2", "project_id": "prj", "step_key": "book_brief", "lines_json": "[]"})
        connection.rollback()
        # 服务端默认：继承上游打开、版本从 1 起（ORM 新行写 0 再 +1，DDL 默认只为手工行兜底）
        row = connection.execute(f"SELECT inherit_upstream, revision FROM {TABLE} WHERE brief_id = 'b1'").fetchone()
        assert row == (1, 1)

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert TABLE not in _tables(path)
