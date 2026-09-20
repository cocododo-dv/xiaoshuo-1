"""迁移 20260920_0089：snowflake_chapter_plans.catalog_chapter_id——已物化的章一次钉好，对不上的留空，可降级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260917_0088"
CURRENT_HEAD = "20260920_0089"
TABLE = "snowflake_chapter_plans"
COLUMN = "catalog_chapter_id"


def _chapter(connection: sqlite3.Connection, plan_id: str, seq: int, *, removed: bool = False) -> None:
    _insert_minimal_row(
        connection,
        TABLE,
        {"chapter_plan_id": plan_id, "project_id": "prj-0089", "row_uid": f"uid-{plan_id}", "chapter_seq": seq,
         **({"removed_at": "2026-09-19T00:00:00Z"} if removed else {})},
    )


def _scene(connection: sqlite3.Connection, scene_plan_id: str, plan_id: str, chapter_id: str) -> None:
    _insert_minimal_row(
        connection,
        "snowflake_scene_plans",
        {"scene_plan_id": scene_plan_id, "project_id": "prj-0089", "scene_id": f"scene-{scene_plan_id}",
         "chapter_id": chapter_id, "chapter_plan_id": plan_id, "scene_seq": 1, "row_uid": f"row-{scene_plan_id}"},
    )


def test_0089_pins_materialized_chapters_and_leaves_ambiguous_ones_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "chapter-plan-catalog-id-0089.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert COLUMN not in _columns(path, TABLE)

    # 原生 sqlite3 不开外键：不必先建作品行
    with sqlite3.connect(path) as connection:
        for chapter_id in ("prj-0089_CH01", "prj-0089_CH02"):
            _insert_minimal_row(connection, "chapter_goals", {"chapter_id": chapter_id, "project_id": "prj-0089", "chapter_goal": "x"})
        _chapter(connection, "cp-clean", 1)       # 全部场都指着目录里存在的 CH01 → 钉住
        _scene(connection, "sp1", "cp-clean", "prj-0089_CH01")
        _scene(connection, "sp2", "cp-clean", "prj-0089_CH01")
        _chapter(connection, "cp-mixed", 2)       # 旧数据里交错洗过的归属：场指着两个章 → 留空
        _scene(connection, "sp3", "cp-mixed", "prj-0089_CH02")
        _scene(connection, "sp4", "cp-mixed", "prj-0089_CH01")
        _chapter(connection, "cp-unmaterialized", 3)  # 指着目录里还没有的章 → 留空
        _scene(connection, "sp5", "cp-unmaterialized", "prj-0089_CH03")
        _chapter(connection, "cp-empty", 4)       # 一场都没有 → 留空
        _chapter(connection, "cp-removed", 5, removed=True)  # 已软删的章不钉
        _scene(connection, "sp6", "cp-removed", "prj-0089_CH02")
        connection.commit()

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)  # 显式升到本迁移：以后再加迁移不必回来改这个文件
    assert COLUMN in _columns(path, TABLE)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        pinned = dict(connection.execute(f"SELECT chapter_plan_id, {COLUMN} FROM {TABLE}").fetchall())
    assert pinned == {
        "cp-clean": "prj-0089_CH01", "cp-mixed": None, "cp-unmaterialized": None, "cp-empty": None, "cp-removed": None,
    }

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert COLUMN not in _columns(path, TABLE)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (5,)


def test_0089_does_not_pin_a_catalog_chapter_two_plans_lay_claim_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "chapter-plan-catalog-id-0089-claims.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        _insert_minimal_row(connection, "chapter_goals", {"chapter_id": "prj-0089_CH01", "project_id": "prj-0089", "chapter_goal": "x"})
        _chapter(connection, "cp-a", 1)
        _scene(connection, "sp1", "cp-a", "prj-0089_CH01")
        _chapter(connection, "cp-b", 2)
        _scene(connection, "sp2", "cp-b", "prj-0089_CH01")
        connection.commit()
    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        assert set(connection.execute(f"SELECT {COLUMN} FROM {TABLE}").fetchall()) == {(None,)}
