"""迁移 20261004_0099：删三张没有读写者的旧表（G3 ART 与它的附注 2）。

- ``snowflake_artifacts``（旧版 v1 雪花规划器，R9 退役）、``snowflake_revision_links``（B06-14 起不再写、从来没有读者）、
  ``author_preference_profiles``（写作偏好学习退役，批准 #6 / R5）；
- 哪张表还有行，先原样写进库文件旁边的 ``<库文件>.0099-<表名>.json`` 再删；空表不留导出文件；库不是文件又有行时
  在删任何一张表之前就拒绝升级；
- 降级按 0098 时的形状把三张表的**结构**（列、默认值、主键、外键、CHECK、查找索引）加回来，删掉的行不回来。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.support.migrations import BACKEND_DIR, insert_minimal_row, migrate, table_names

PREVIOUS_HEAD = "20260929_0098"
REVISION = "20261004_0099"
DEAD_TABLES = ("snowflake_artifacts", "snowflake_revision_links", "author_preference_profiles")
LOOKUP_INDEXES = {"ix_snowflake_artifacts_project_id", "ix_snowflake_revision_links_project_id"}


def _export_path(path: Path, table: str) -> Path:
    return path.with_name(f"{path.name}.0099-{table}.json")


def _index_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _shape(path: Path, table: str) -> dict:
    """一张表的结构：列（次序、类型、可空、默认值）、主键、外键、索引、CHECK——按库里反射出来的样子。"""
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}")
    try:
        inspector = sa.inspect(engine)
        return {
            "columns": [
                (column["name"], str(column["type"]), column["nullable"], column["default"])
                for column in inspector.get_columns(table)
            ],
            "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
            "foreign_keys": sorted(
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in inspector.get_foreign_keys(table)
            ),
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


def _seed_project(connection: sqlite3.Connection) -> None:
    insert_minimal_row(connection, "story_projects", {"project_id": "work-a", "title": "雨城旧信"})


def test_0099_exports_the_leftover_rows_table_by_table_and_drops_the_three_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实库的情形：规划器表是空的，修订链与偏好档案各有残留行——只给有行的表写导出文件。"""
    path = tmp_path / "dead-tables-0099.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        _seed_project(connection)
        for index in (1, 2):
            insert_minimal_row(
                connection,
                "snowflake_revision_links",
                {
                    "revision_link_id": f"link-{index}",
                    "project_id": "work-a",
                    "source_step_key": "book_brief",
                    "affected_kind": "scene_plan",
                    "affected_id": f"scene-plan-{index}",
                    "reason": "林昭改了旧信的去向",
                },
            )
        insert_minimal_row(
            connection,
            "author_preference_profiles",
            {
                "profile_id": "pref-1",
                "scope_type": "project",
                "scope_ref_id": "work-a",
                "status": "approved",
                "runtime_eligible": 1,
                "summary_json": json.dumps({"preferred_revision_moves": ["更短的句子"]}, ensure_ascii=False),
                "source_patch_ids_json": "[]",
                "created_by": "writer_deep_review",
            },
        )
    assert set(DEAD_TABLES) <= table_names(path)
    assert LOOKUP_INDEXES <= _index_names(path)

    migrate(path, REVISION, monkeypatch)

    assert set(DEAD_TABLES).isdisjoint(table_names(path))
    assert LOOKUP_INDEXES.isdisjoint(_index_names(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        assert connection.execute("SELECT title FROM story_projects WHERE project_id = 'work-a'").fetchone() == (
            "雨城旧信",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert not _export_path(path, "snowflake_artifacts").exists()
    links = json.loads(_export_path(path, "snowflake_revision_links").read_text(encoding="utf-8"))
    assert links["revision"] == REVISION and links["table"] == "snowflake_revision_links"
    assert [row["revision_link_id"] for row in links["rows"]] == ["link-1", "link-2"]
    assert links["rows"][0]["reason"] == "林昭改了旧信的去向"
    assert links["rows"][0]["status"] == "open"
    profiles = json.loads(_export_path(path, "author_preference_profiles").read_text(encoding="utf-8"))
    assert profiles["table"] == "author_preference_profiles"
    assert [row["profile_id"] for row in profiles["rows"]] == ["pref-1"]
    # 行原样导出：JSON 列是库里存的文本，不再转一道
    assert json.loads(profiles["rows"][0]["summary_json"]) == {"preferred_revision_moves": ["更短的句子"]}


def test_0099_writes_no_export_for_empty_tables_and_downgrade_restores_the_0098_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty-dead-tables-0099.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    before = {table: _shape(path, table) for table in DEAD_TABLES}
    assert ("resolved_at", "VARCHAR", True, None) not in before["snowflake_revision_links"]["columns"]

    migrate(path, REVISION, monkeypatch)

    assert set(DEAD_TABLES).isdisjoint(table_names(path))
    assert sorted(item.name for item in tmp_path.iterdir() if ".0099-" in item.name) == []

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)

    assert {table: _shape(path, table) for table in DEAD_TABLES} == before
    assert LOOKUP_INDEXES <= _index_names(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        for table in DEAD_TABLES:
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,), table
        # 结构回来了：0098 时能写的行照样写得进去（CHECK 与外键都在）
        _seed_project(connection)
        insert_minimal_row(
            connection,
            "snowflake_artifacts",
            {"artifact_id": "artifact-1", "project_id": "work-a", "step_key": "book_brief"},
        )
        assert connection.execute(
            "SELECT version, status FROM snowflake_artifacts WHERE artifact_id = 'artifact-1'"
        ).fetchone() == (1, "pending_review")
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_row(
                connection,
                "author_preference_profiles",
                {"profile_id": "pref-bad", "scope_type": "workspace", "status": "draft", "runtime_eligible": 0},
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _load_migration_module():
    path = BACKEND_DIR / "alembic" / "versions" / "20261004_0099_drop_three_dead_tables.py"
    spec = importlib.util.spec_from_file_location("migration_0099_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_0099_refuses_an_in_memory_database_with_rows_before_dropping_anything() -> None:
    """库不是文件、没处导出：有行时拒绝升级，三张表一张都没删（先查完全部再删）。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration_module()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            for table in DEAD_TABLES:
                connection.exec_driver_sql(f'CREATE TABLE "{table}" (row_id VARCHAR PRIMARY KEY, note TEXT)')
            # 只有最后一张表有行：前两张空表也不能先被删掉
            connection.exec_driver_sql(
                "INSERT INTO author_preference_profiles (row_id, note) VALUES ('pref-1', '旧偏好')"
            )
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="author_preference_profiles 1 行"):
                    migration.upgrade()
            remaining = set(sa.inspect(connection).get_table_names())
        assert set(DEAD_TABLES) <= remaining
    finally:
        engine.dispose()


def test_0099_exports_never_land_in_the_public_repository() -> None:
    """导出的行可能带作者的构思与偏好文字：库默认就在仓库的 backend/ 下，导出文件与库文件一样必须被 .gitignore 挡住。"""
    from fnmatch import fnmatch

    repo_root = Path(__file__).resolve().parents[2]
    patterns = [
        line.strip()
        for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "!")) and "/" not in line.strip()
    ]
    for table in DEAD_TABLES:
        exported = _export_path(repo_root / "backend" / "novel_system.db", table).name
        assert any(fnmatch(exported, pattern) for pattern in patterns), exported
    # 备份清单（*.db.meta.json）按惯例入库，不能被同一条规则顺手挡掉
    assert not any(fnmatch("novel_system_pre_0099.db.meta.json", pattern) for pattern in patterns)
