"""迁移 20260914_0085：snowflake_scene_plans.expected_reader_emotion / story_time（原著场景表的两栏），可空、不回填，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

PREVIOUS_HEAD = "20260913_0084"
CURRENT_HEAD = "20260917_0088"


def _config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return config


def _migrate(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, down: bool = False) -> None:
    from novel_system.db.session import reset_engine

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "style_reference_legacy_0085.json").write_text("[]", encoding="utf-8")
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


def _columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def test_0085_adds_the_method_columns_nullable_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "method-fields-0085.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    before = _columns(path, "snowflake_scene_plans")
    assert "expected_reader_emotion" not in before and "story_time" not in before

    with sqlite3.connect(path) as connection:
        _insert_minimal_row(
            connection,
            "snowflake_scene_plans",
            {
                "scene_plan_id": "plan-0085",
                "project_id": "prj-0085",
                "row_uid": "row_0085",
                "scene_id": "prj-0085_SC_row_0085",
                "chapter_id": "prj-0085_CH01",
                "scene_seq": 1,
                "scene_type": "proactive",
                "status": "draft",
            },
        )
        connection.commit()

    _migrate(path, "head", monkeypatch, tmp_path)
    after = _columns(path, "snowflake_scene_plans")
    assert {"expected_reader_emotion", "story_time"} <= after
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        # 历史行不回填：两列都空
        assert connection.execute(
            "SELECT expected_reader_emotion, story_time FROM snowflake_scene_plans WHERE scene_plan_id = 'plan-0085'"
        ).fetchone() == (None, None)
        index_names = {row[1] for row in connection.execute('PRAGMA index_list("snowflake_scene_plans")')}
        assert {"ix_snowflake_scene_plans_row_uid", "ix_snowflake_scene_plans_scene_id"} <= index_names

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    downgraded = _columns(path, "snowflake_scene_plans")
    assert "expected_reader_emotion" not in downgraded and "story_time" not in downgraded
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute("SELECT scene_plan_id FROM snowflake_scene_plans").fetchone() == ("plan-0085",)
