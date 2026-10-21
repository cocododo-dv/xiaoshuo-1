"""迁移 20261021_0102：删待办收件箱退役后留下的两张表（``review_items``、``review_derived_snoozes``）。

- 哪张表还有行，先原样写进库文件旁边的 ``<库文件>.0102-<表名>.json`` 再删（生成列 ``target_collection`` 不导出）；
  空表不留导出文件；库不是文件又有行时在删任何一张表之前就拒绝升级；
- 降级按 0101 时的形状把两张表的**结构**（列、生成列、主键、CHECK、三个索引）加回来，删掉的行不回来。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.support.migrations import BACKEND_DIR, insert_minimal_row, migrate, table_names

PREVIOUS_HEAD = "20261021_0101"
REVISION = "20261021_0102"
DEAD_TABLES = ("review_items", "review_derived_snoozes")
INDEXES = {"ix_review_items_project_state", "ix_review_items_scene", "ux_review_items_project_dedupe"}


def _export_path(path: Path, table: str) -> Path:
    return path.with_name(f"{path.name}.0102-{table}.json")


def _index_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _shape(path: Path, table: str) -> dict:
    """一张表的结构：列（次序、类型、可空、默认值、生成式）、主键、索引、CHECK——按库里反射出来的样子。"""
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    try:
        inspector = sa.inspect(engine)
        return {
            "columns": [
                (
                    column["name"], str(column["type"]), column["nullable"], column["default"],
                    " ".join(str((column.get("computed") or {}).get("sqltext") or "").split()),
                )
                for column in inspector.get_columns(table)
            ],
            "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
            "indexes": sorted(
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            ),
            "checks": sorted(
                (check["name"], " ".join(check["sqltext"].split())) for check in inspector.get_check_constraints(table)
            ),
        }
    finally:
        engine.dispose()


def _card(review_id: str, **extra) -> dict:
    return {
        "review_id": review_id, "item_type": "fe_card", "status": "pending", "candidate_text": "林昭要不要拆旧信",
        "candidate_payload_json": "{}", "project_id": "work-a", "kind": "decision", "state": "resolved",
        "card_json": json.dumps({"detail": "雨城的旧信"}, ensure_ascii=False), **extra,
    }


def test_0102_exports_the_leftover_cards_and_drops_both_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """实库的情形：收件箱的持久卡与旧审阅行还有几行，「稍后」记录是空的——只给有行的表写导出文件。"""
    path = tmp_path / "review-tables-0102.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        insert_minimal_row(connection, "story_projects", {"project_id": "work-a", "title": "雨城旧信"})
        insert_minimal_row(connection, "review_items", _card("card-1"))
        insert_minimal_row(
            connection, "review_items",
            _card("review_pref_legacy", item_type="author_preference_profile", kind=None, state=None, project_id=None),
        )
        assert connection.execute(
            "SELECT target_collection FROM review_items WHERE review_id = 'review_pref_legacy'"
        ).fetchone() == ("author_preference_profiles",)
    assert set(DEAD_TABLES) <= table_names(path)
    assert INDEXES <= _index_names(path)

    migrate(path, REVISION, monkeypatch)

    assert set(DEAD_TABLES).isdisjoint(table_names(path))
    assert INDEXES.isdisjoint(_index_names(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        assert connection.execute("SELECT title FROM story_projects WHERE project_id = 'work-a'").fetchone() == ("雨城旧信",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert not _export_path(path, "review_derived_snoozes").exists()
    exported = json.loads(_export_path(path, "review_items").read_text(encoding="utf-8"))
    assert exported["revision"] == REVISION and exported["table"] == "review_items"
    assert sorted(row["review_id"] for row in exported["rows"]) == ["card-1", "review_pref_legacy"]
    card = next(row for row in exported["rows"] if row["review_id"] == "card-1")
    assert card["candidate_text"] == "林昭要不要拆旧信"
    assert json.loads(card["card_json"]) == {"detail": "雨城的旧信"}  # 行原样导出：JSON 列是库里存的文本
    assert all("target_collection" not in row for row in exported["rows"])  # 生成列只是 item_type 的派生


def test_0102_writes_no_export_for_empty_tables_and_downgrade_restores_the_0101_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty-review-tables-0102.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    before = {table: _shape(path, table) for table in DEAD_TABLES}
    assert any(column[0] == "target_collection" and column[4] for column in before["review_items"]["columns"])

    migrate(path, REVISION, monkeypatch)

    assert set(DEAD_TABLES).isdisjoint(table_names(path))
    assert sorted(item.name for item in tmp_path.iterdir() if ".0102-" in item.name) == []

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)

    assert {table: _shape(path, table) for table in DEAD_TABLES} == before
    assert INDEXES <= _index_names(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        for table in DEAD_TABLES:
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,), table
        # 结构回来了：0101 时能写的行照样写得进去，生成列照样算、CHECK 与去重键照样拦
        insert_minimal_row(connection, "review_items", _card("card-1", item_type="world_rule", dedupe_key="once"))
        assert connection.execute(
            "SELECT target_collection FROM review_items WHERE review_id = 'card-1'"
        ).fetchone() == ("world_rules",)
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_row(connection, "review_items", _card("card-2", status="open"))
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_row(connection, "review_items", _card("card-3", dedupe_key="once"))
        insert_minimal_row(connection, "review_derived_snoozes", {"project_id": "work-a", "fingerprint": "derived:x"})


def _load_migration_module():
    path = BACKEND_DIR / "alembic" / "versions" / "20261021_0102_drop_review_inbox_tables.py"
    spec = importlib.util.spec_from_file_location("migration_0102_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_0102_refuses_an_in_memory_database_with_rows_before_dropping_anything() -> None:
    """库不是文件、没处导出：有行时拒绝升级，两张表一张都没删（先查完全部再删）。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            for table in DEAD_TABLES:
                connection.exec_driver_sql(f'CREATE TABLE "{table}" (row_id VARCHAR PRIMARY KEY, note TEXT)')
            # 只有后一张表有行：前一张空表也不能先被删掉
            connection.exec_driver_sql("INSERT INTO review_derived_snoozes (row_id, note) VALUES ('snooze-1', '稍后')")
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="review_derived_snoozes 1 行"):
                    migration.upgrade()
            remaining = set(sa.inspect(connection).get_table_names())
        assert set(DEAD_TABLES) <= remaining
    finally:
        engine.dispose()
