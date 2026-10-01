"""迁移测试共用：把一个测试库升 / 降到指定 Alembic 版本，读表、列，按必填列补齐插一行。

库只由 Alembic 建（不经 ``create_all``）：``migrate`` 在 ``NOVEL_SYSTEM_DATABASE_URL`` 指向测试库的环境里跑
``alembic upgrade`` / ``downgrade``，退出时恢复用例原来的环境与引擎。

迁移 0036 只在旧 reference_learning 表里真有行时才要求 ``backups/style_reference_legacy_*.json``（B12-14），新库
没有可丢的东西，所以这里不再伪造备份、也不设 ``STYLE_REFERENCE_REPO_ROOT``；只有专测那道守卫的
``test_style_reference_schema.py`` 还用它。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def migrate(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch, *, down: bool = False) -> None:
    """把 ``path`` 上的库升到（``down=True`` 时降到）``revision``。"""
    from novel_system.db.session import reset_engine

    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        reset_engine()
        try:
            if down:
                command.downgrade(alembic_config(), revision)
            else:
                command.upgrade(alembic_config(), revision)
        finally:
            reset_engine()


def insert_minimal_row(connection: sqlite3.Connection, table: str, values: dict) -> None:
    """按 PRAGMA table_info 补齐所有 NOT NULL 且无默认值的列——迁移 DDL 的必填列不必在测试里逐个背。"""
    row = dict(values)
    for _cid, name, col_type, notnull, default, _pk in connection.execute(f'PRAGMA table_info("{table}")'):
        if name in row or not notnull or default is not None:
            continue
        upper = str(col_type or "").upper()
        if "INT" in upper:
            row[name] = 0
        elif name.endswith("_json") or "JSON" in upper:
            row[name] = "[]"
        else:
            row[name] = "2026-09-13T00:00:00Z" if name.endswith("_at") else "x"
    columns = ", ".join(f'"{name}"' for name in row)
    placeholders = ", ".join("?" for _ in row)
    connection.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(row.values()))


def table_columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


__all__ = ["BACKEND_DIR", "alembic_config", "insert_minimal_row", "migrate", "table_columns", "table_names"]
