"""迁移 20260915_0086：snowflake_scene_plans.exception_reason（作者的破例理由），可空、不回填，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

PREVIOUS_HEAD = "20260914_0085"
# 升到 head 后断言的版本号跟着 schema_contract 走（test_schema_contract_revision 把它钉在 Alembic 唯一 head 上），
# 以后再加迁移不必回来改这个文件。
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION as CURRENT_HEAD  # noqa: E402

from tests.support.migrations import insert_minimal_row, migrate, table_columns


def test_0086_adds_exception_reason_nullable_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "exception-reason-0086.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    assert "exception_reason" not in table_columns(path, "snowflake_scene_plans")

    with sqlite3.connect(path) as connection:
        insert_minimal_row(
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

    migrate(path, "head", monkeypatch)
    assert "exception_reason" in table_columns(path, "snowflake_scene_plans")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        # 历史行不回填
        assert connection.execute(
            "SELECT exception_reason FROM snowflake_scene_plans WHERE scene_plan_id = 'plan-0086'"
        ).fetchone() == (None,)
        index_names = {row[1] for row in connection.execute('PRAGMA index_list("snowflake_scene_plans")')}
        assert {"ix_snowflake_scene_plans_row_uid", "ix_snowflake_scene_plans_scene_id"} <= index_names

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    assert "exception_reason" not in table_columns(path, "snowflake_scene_plans")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute("SELECT scene_plan_id FROM snowflake_scene_plans").fetchone() == ("plan-0086",)
