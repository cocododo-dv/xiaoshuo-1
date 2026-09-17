"""迁移 20260917_0088：snowflake_assistant_turns 的 turn_kind（历史行回填 chat）与三列可空 JSON，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260916_0087"
CURRENT_HEAD = "20260917_0088"
TABLE = "snowflake_assistant_turns"
NEW_COLUMNS = {"turn_kind", "candidates_json", "brief_delta_json", "adoption_json"}


def test_0088_adds_turn_kind_with_chat_backfill_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "assistant-turn-kinds-0088.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert not (NEW_COLUMNS & _columns(path, TABLE))

    # 升级前就存在的教练回合：turn_kind 回填 chat，其余三列留空（原生 sqlite3 不开外键，不必先建作品行）
    with sqlite3.connect(path) as connection:
        _insert_minimal_row(
            connection,
            TABLE,
            {"turn_id": "turn-0088", "project_id": "prj-0088", "step_key": "book_brief", "user_message": "缺什么？", "reply": "先写读者。"},
        )
        connection.commit()

    _migrate(path, "head", monkeypatch, tmp_path)
    assert NEW_COLUMNS <= _columns(path, TABLE)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        row = connection.execute(
            f"SELECT turn_kind, candidates_json, brief_delta_json, adoption_json FROM {TABLE} WHERE turn_id = 'turn-0088'"
        ).fetchone()
        assert row == ("chat", None, None, None)
        # 新回合可以是「方向」回合
        _insert_minimal_row(
            connection,
            TABLE,
            {"turn_id": "turn-0088-c", "project_id": "prj-0088", "step_key": "book_brief", "user_message": "给我 3 个方向",
             "reply": "", "turn_kind": "candidates", "candidates_json": '{"items": []}'},
        )
        connection.commit()
        assert connection.execute(f"SELECT turn_kind FROM {TABLE} WHERE turn_id = 'turn-0088-c'").fetchone() == ("candidates",)

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert not (NEW_COLUMNS & _columns(path, TABLE))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert {row[0] for row in connection.execute(f"SELECT turn_id FROM {TABLE}")} == {"turn-0088", "turn-0088-c"}
