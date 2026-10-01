from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# 升到 head 后断言的版本号跟着 schema_contract 走（test_schema_contract_revision 把它钉在 Alembic 唯一 head 上），
# 以后再加迁移不必回来改这个文件。
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION as CURRENT_HEAD
from tests.support.migrations import migrate

REAL_ONLY_HEAD = "20260717_0075"
CHAPTERING_HEAD = "20260725_0076"
MERGED_HEAD = "20260802_0077"


def test_published_real_only_head_remains_upgradeable_to_single_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "real-only-head.db"
    migrate(database_path, REAL_ONLY_HEAD, monkeypatch)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall() == [(REAL_ONLY_HEAD,)]

    migrate(database_path, "head", monkeypatch)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall() == [(CURRENT_HEAD,)]


def test_merge_repairs_historical_0076_membership_and_installs_fk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "historical-0076.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE story_projects (
                project_id VARCHAR NOT NULL PRIMARY KEY
            );
            CREATE TABLE snowflake_chapter_plans (
                chapter_plan_id VARCHAR NOT NULL PRIMARY KEY,
                project_id VARCHAR NOT NULL,
                FOREIGN KEY(project_id) REFERENCES story_projects(project_id)
            );
            CREATE TABLE snowflake_scene_plans (
                scene_plan_id VARCHAR NOT NULL PRIMARY KEY,
                project_id VARCHAR NOT NULL,
                chapter_plan_id VARCHAR,
                FOREIGN KEY(project_id) REFERENCES story_projects(project_id)
            );
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version(version_num)
                VALUES ('20260717_0075'), ('20260725_0076');
            INSERT INTO story_projects(project_id) VALUES ('project-1');
            INSERT INTO snowflake_scene_plans(
                scene_plan_id, project_id, chapter_plan_id
            ) VALUES ('scene-plan-1', 'project-1', 'missing-chapter');
            """
        )

    migrate(database_path, MERGED_HEAD, monkeypatch)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT chapter_plan_id FROM snowflake_scene_plans "
            "WHERE scene_plan_id = 'scene-plan-1'"
        ).fetchone() == (None,)
        membership_fks = [
            row
            for row in connection.execute(
                "PRAGMA foreign_key_list(snowflake_scene_plans)"
            )
            if row[2] == "snowflake_chapter_plans"
            and row[3] == "chapter_plan_id"
            and row[4] == "chapter_plan_id"
        ]
        assert len(membership_fks) == 1
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(snowflake_scene_plans)"
            )
        }
        assert "ix_snowflake_scene_plans_chapter_plan_id" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snowflake_scene_plans SET chapter_plan_id = 'still-missing' "
                "WHERE scene_plan_id = 'scene-plan-1'"
            )
