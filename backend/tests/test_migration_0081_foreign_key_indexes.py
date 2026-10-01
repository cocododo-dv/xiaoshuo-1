from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# 升到 head 后断言的版本号跟着 schema_contract 走（test_schema_contract_revision 把它钉在 Alembic 唯一 head 上），
# 以后再加迁移不必回来改这个文件。
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION as CURRENT_HEAD
from tests.support.migrations import migrate

PREVIOUS_HEAD = "20260802_0080"


def _uncovered_foreign_keys(path: Path) -> list[str]:
    uncovered: list[str] = []
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table_name in tables:
            indexes = list(connection.execute(f'PRAGMA index_list("{table_name}")'))
            prefixes = {
                columns[0][2]
                for index in indexes
                if (columns := list(connection.execute(f'PRAGMA index_info("{index[1]}")')))
            }
            for foreign_key in connection.execute(f'PRAGMA foreign_key_list("{table_name}")'):
                if foreign_key[3] not in prefixes:
                    uncovered.append(f"{table_name}.{foreign_key[3]}")
    return sorted(uncovered)


def test_0081_covers_all_foreign_key_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "foreign-key-indexes-0081.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    assert len(_uncovered_foreign_keys(path)) == 38

    migrate(path, "head", monkeypatch)
    assert _uncovered_foreign_keys(path) == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)

