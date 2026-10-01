"""迁移 20260914_0085：snowflake_scene_plans.expected_reader_emotion / story_time（原著场景表的两栏），可空、不回填，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

PREVIOUS_HEAD = "20260913_0084"
# 升到 head 后断言的版本号跟着 schema_contract 走（test_schema_contract_revision 把它钉在 Alembic 唯一 head 上），
# 以后再加迁移不必回来改这个文件。
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION as CURRENT_HEAD  # noqa: E402

from tests.support.migrations import insert_minimal_row, migrate, table_columns


def test_0085_adds_the_method_columns_nullable_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "method-fields-0085.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    before = table_columns(path, "snowflake_scene_plans")
    assert "expected_reader_emotion" not in before and "story_time" not in before

    with sqlite3.connect(path) as connection:
        insert_minimal_row(
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

    migrate(path, "head", monkeypatch)
    after = table_columns(path, "snowflake_scene_plans")
    assert {"expected_reader_emotion", "story_time"} <= after
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        # 历史行不回填：两列都空
        assert connection.execute(
            "SELECT expected_reader_emotion, story_time FROM snowflake_scene_plans WHERE scene_plan_id = 'plan-0085'"
        ).fetchone() == (None, None)
        index_names = {row[1] for row in connection.execute('PRAGMA index_list("snowflake_scene_plans")')}
        assert {"ix_snowflake_scene_plans_row_uid", "ix_snowflake_scene_plans_scene_id"} <= index_names

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    downgraded = table_columns(path, "snowflake_scene_plans")
    assert "expected_reader_emotion" not in downgraded and "story_time" not in downgraded
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute("SELECT scene_plan_id FROM snowflake_scene_plans").fetchone() == ("plan-0085",)
