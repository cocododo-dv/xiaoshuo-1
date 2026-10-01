from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from novel_system.db.models import Base

# 升到 head 后断言的版本号跟着 schema_contract 走（test_schema_contract_revision 把它钉在 Alembic 唯一 head 上），
# 以后再加迁移不必回来改这个文件。
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION as CURRENT_HEAD
from tests.support.migrations import migrate

PREVIOUS_HEAD = "20260805_0081"


def _migration_module():
    backend_dir = Path(__file__).resolve().parents[1]
    path = backend_dir / "alembic" / "versions" / "20260818_0082_canon_continuity.py"
    spec = importlib.util.spec_from_file_location("migration_0082_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_legacy_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO narrative_events(
            event_id, project_id, scene_id, chapter_id, event_type,
            entity_type, entity_id, fact_key, fact_value, payload_json, created_at
        ) VALUES (?, 'legacy_project', 'legacy_scene', 'legacy_chapter',
                  'character_state', 'character', 'legacy_character',
                  'mood', 'uneasy', ?, '2026-08-18T00:00:00Z')
        """,
        (event_id, json.dumps(payload, ensure_ascii=False)),
    )


def test_0082_materialized_schema_check_rejects_missing_nonindexed_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            assert migration._upgrade_already_materialized() is True

            connection.exec_driver_sql(
                "ALTER TABLE canon_commits DROP COLUMN decision_note"
            )
            assert migration._upgrade_already_materialized() is False
    finally:
        engine.dispose()


def test_0082_backfills_legacy_authority_fail_closed_and_downgrades_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "canon-continuity-0082.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        _insert_legacy_event(connection, event_id="legacy_plan", payload={})
        _insert_legacy_event(
            connection,
            event_id="legacy_prose",
            payload={"source": "prose", "extract_ordinal": 0},
        )

    migrate(path, "head", monkeypatch)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (CURRENT_HEAD,)
        rows = connection.execute(
            """
            SELECT event_id, authority_status, source_kind
            FROM narrative_events ORDER BY event_id
            """
        ).fetchall()
        assert rows == [
            ("legacy_plan", "planned", "legacy_plan"),
            ("legacy_prose", "pending", "prose_extraction"),
        ]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"canon_commits", "fact_candidates", "continuity_snapshots"} <= tables
        timeline_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(timeline_events)")
        }
        assert {
            "event_mode",
            "realization_status",
            "realized_canon_commit_id",
            "realized_scene_id",
        } <= timeline_columns


    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS_HEAD,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "canon_commits" not in tables
        assert "fact_candidates" not in tables
        assert "continuity_snapshots" not in tables
        narrative_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(narrative_events)")
        }
        assert "authority_status" not in narrative_columns
        assert "canon_commit_id" not in narrative_columns
