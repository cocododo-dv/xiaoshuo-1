"""迁移 20260917_0088：snowflake_assistant_turns 的 turn_kind（历史行回填 chat）与三列可空 JSON，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate, table_columns

PREVIOUS_HEAD = "20260916_0087"
CURRENT_HEAD = "20260917_0088"
TABLE = "snowflake_assistant_turns"
NEW_COLUMNS = {"turn_kind", "candidates_json", "brief_delta_json", "adoption_json"}


def test_0088_adds_turn_kind_with_chat_backfill_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "assistant-turn-kinds-0088.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    assert not (NEW_COLUMNS & table_columns(path, TABLE))

    # 升级前就存在的教练回合：turn_kind 回填 chat，其余三列留空（原生 sqlite3 不开外键，不必先建作品行）
    with sqlite3.connect(path) as connection:
        insert_minimal_row(
            connection,
            TABLE,
            {"turn_id": "turn-0088", "project_id": "prj-0088", "step_key": "book_brief", "user_message": "缺什么？", "reply": "先写读者。"},
        )
        connection.commit()

    migrate(path, CURRENT_HEAD, monkeypatch)  # 升到本迁移自己的版本：后续迁移不必回头改这里
    assert NEW_COLUMNS <= table_columns(path, TABLE)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        row = connection.execute(
            f"SELECT turn_kind, candidates_json, brief_delta_json, adoption_json FROM {TABLE} WHERE turn_id = 'turn-0088'"
        ).fetchone()
        assert row == ("chat", None, None, None)
        # 新回合可以是「方向」回合
        insert_minimal_row(
            connection,
            TABLE,
            {"turn_id": "turn-0088-c", "project_id": "prj-0088", "step_key": "book_brief", "user_message": "给我 3 个方向",
             "reply": "", "turn_kind": "candidates", "candidates_json": '{"items": []}'},
        )
        connection.commit()
        assert connection.execute(f"SELECT turn_kind FROM {TABLE} WHERE turn_id = 'turn-0088-c'").fetchone() == ("candidates",)

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    assert not (NEW_COLUMNS & table_columns(path, TABLE))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert {row[0] for row in connection.execute(f"SELECT turn_id FROM {TABLE}")} == {"turn-0088", "turn-0088-c"}
