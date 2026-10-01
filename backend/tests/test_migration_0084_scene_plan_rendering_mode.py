"""迁移 20260913_0084：snowflake_scene_plans.rendering_mode，历史行回填 full，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate, table_columns

PREVIOUS_HEAD = "20260904_0083"
CURRENT_HEAD = "20260913_0084"


def test_0084_adds_rendering_mode_with_full_backfill_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rendering-mode-0084.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    assert "rendering_mode" not in table_columns(path, "snowflake_scene_plans")

    # 升级前就存在的规划行：回填 full（原生 sqlite3 连接不开外键，不必先建作品行）
    with sqlite3.connect(path) as connection:
        insert_minimal_row(
            connection,
            "snowflake_scene_plans",
            {
                "scene_plan_id": "plan-0084",
                "project_id": "prj-0084",
                "row_uid": "row_0084",
                "scene_id": "prj-0084_SC_row_0084",
                "chapter_id": "prj-0084_CH01",
                "scene_seq": 1,
                "scene_type": "reactive",
                "status": "draft",
            },
        )
        connection.commit()

    migrate(path, CURRENT_HEAD, monkeypatch)  # 0085 之后 head 往前走了：这里只测本迁移
    assert "rendering_mode" in table_columns(path, "snowflake_scene_plans")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        assert connection.execute("SELECT rendering_mode FROM snowflake_scene_plans WHERE scene_plan_id = 'plan-0084'").fetchone() == ("full",)
        # 旧的唯一索引在 batch 重建后仍然在
        index_names = {row[1] for row in connection.execute('PRAGMA index_list("snowflake_scene_plans")')}
        assert {"ix_snowflake_scene_plans_row_uid", "ix_snowflake_scene_plans_scene_id"} <= index_names

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    assert "rendering_mode" not in table_columns(path, "snowflake_scene_plans")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute("SELECT scene_plan_id FROM snowflake_scene_plans").fetchone() == ("plan-0084",)
