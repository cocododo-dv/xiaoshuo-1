"""迁移 20260915_0086：snowflake_scene_plans.exception_reason（作者的破例理由），可空、不回填，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

PREVIOUS_HEAD = "20260914_0085"
CURRENT_HEAD = "20260916_0087"


def _config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return config


def _migrate(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, down: bool = False) -> None:
    from novel_system.db.session import reset_engine

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "style_reference_legacy_0086.json").write_text("[]", encoding="utf-8")
    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        migration_env.setenv("STYLE_REFERENCE_REPO_ROOT", str(tmp_path))
        reset_engine()
        try:
            if down:
                command.downgrade(_config(), revision)
            else:
                command.upgrade(_config(), revision)
        finally:
            reset_engine()


def _insert_minimal_row(connection: sqlite3.Connection, table: str, values: dict) -> None:
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
            row[name] = "2026-09-15T00:00:00Z" if name.endswith("_at") else "x"
    columns = ", ".join(f'"{name}"' for name in row)
    placeholders = ", ".join("?" for _ in row)
    connection.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(row.values()))


def _columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_0086_adds_exception_reason_nullable_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "exception-reason-0086.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert "exception_reason" not in _columns(path, "snowflake_scene_plans")

    with sqlite3.connect(path) as connection:
        _insert_minimal_row(
            connection,
            "snowflake_scene_plans",
            {
                "scene_plan_id": "plan-0086",
                "project_id": "prj-0086",
                "row_uid": "row_0086",
                "scene_id": "prj-0086_SC_row_0086",
                "chapter_id": "prj-0086_CH01",
                "scene_seq": 1,
                "scene_type": "proactive",
                "status": "draft",
            },
        )
        connection.commit()

    _migrate(path, "head", monkeypatch, tmp_path)
    assert "exception_reason" in _columns(path, "snowflake_scene_plans")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        # 历史行不回填
        assert connection.execute(
            "SELECT exception_reason FROM snowflake_scene_plans WHERE scene_plan_id = 'plan-0086'"
        ).fetchone() == (None,)
        index_names = {row[1] for row in connection.execute('PRAGMA index_list("snowflake_scene_plans")')}
        assert {"ix_snowflake_scene_plans_row_uid", "ix_snowflake_scene_plans_scene_id"} <= index_names

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert "exception_reason" not in _columns(path, "snowflake_scene_plans")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute("SELECT scene_plan_id FROM snowflake_scene_plans").fetchone() == ("plan-0086",)
