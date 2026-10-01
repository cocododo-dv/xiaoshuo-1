"""迁移 20260929_0097：目录章序一次压实（与过去目录读取时的惰性补号同一规则），之后读取不再写；可重复执行。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate

PREVIOUS_HEAD = "20260929_0096"
CURRENT_HEAD = "20260929_0097"


def _project(connection: sqlite3.Connection, project_id: str, approved: str = "[]") -> None:
    insert_minimal_row(
        connection,
        "story_projects",
        {"project_id": project_id, "title": project_id, "outline_text": "x", "approved_chapter_ids_json": approved},
    )


def _chapter(
    connection: sqlite3.Connection,
    chapter_id: str,
    project_id: str,
    order: int | None,
    *,
    state: str = "planned",
    trashed: bool = False,
) -> None:
    insert_minimal_row(
        connection,
        "chapter_goals",
        {
            "chapter_id": chapter_id,
            "project_id": project_id,
            "chapter_goal": chapter_id,
            "display_order": order,
            "state": state,
            "trashed_flag": 1 if trashed else 0,
        },
    )


def _orders(path: Path, project_id: str) -> dict[str, int | None]:
    with sqlite3.connect(path) as connection:
        return dict(
            connection.execute(
                "SELECT chapter_id, display_order FROM chapter_goals WHERE project_id = ?", (project_id,)
            ).fetchall()
        )


def test_0097_compacts_drifted_chapter_orders_except_where_an_approved_chapter_would_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "compact-display-order-0097.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    # 原生 sqlite3 不开外键：作品行只为读 approved_chapter_ids_json
    with sqlite3.connect(path) as connection:
        _project(connection, "prj-gap")
        _chapter(connection, "gap-a", "prj-gap", 2)
        _chapter(connection, "gap-c", "prj-gap", None)
        _chapter(connection, "gap-b", "prj-gap", 5)
        _chapter(connection, "gap-trashed", "prj-gap", 1, trashed=True)
        _project(connection, "prj-dense")
        _chapter(connection, "dense-1", "prj-dense", 1)
        _chapter(connection, "dense-2", "prj-dense", 2)
        # 要挪的行里有终审章（state）：整部作品不动
        _project(connection, "prj-locked-state")
        _chapter(connection, "state-a", "prj-locked-state", 3, state="approved")
        _chapter(connection, "state-b", "prj-locked-state", 7)
        # 要挪的行里有终审章（作品的 approved_chapter_ids_json）：同样不动
        _project(connection, "prj-locked-list", approved='["list-a"]')
        _chapter(connection, "list-a", "prj-locked-list", 4)
        _chapter(connection, "list-b", "prj-locked-list", None)
        # 终审章本来就在自己的位置上：只压实后面的章
        _project(connection, "prj-approved-in-place", approved='["place-a"]')
        _chapter(connection, "place-a", "prj-approved-in-place", 1, state="approved")
        _chapter(connection, "place-b", "prj-approved-in-place", 9)
        connection.commit()

    migrate(path, CURRENT_HEAD, monkeypatch)  # 显式升到本迁移：以后再加迁移不必回来改这个文件
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
    assert _orders(path, "prj-gap") == {"gap-a": 1, "gap-b": 2, "gap-c": 3, "gap-trashed": 1}
    assert _orders(path, "prj-dense") == {"dense-1": 1, "dense-2": 2}
    assert _orders(path, "prj-locked-state") == {"state-a": 3, "state-b": 7}
    assert _orders(path, "prj-locked-list") == {"list-a": 4, "list-b": None}
    assert _orders(path, "prj-approved-in-place") == {"place-a": 1, "place-b": 2}

    # 降级是空操作；再升一次没有要改的行（可重复执行）
    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    assert _orders(path, "prj-gap") == {"gap-a": 1, "gap-b": 2, "gap-c": 3, "gap-trashed": 1}
    migrate(path, CURRENT_HEAD, monkeypatch)
    assert _orders(path, "prj-gap") == {"gap-a": 1, "gap-b": 2, "gap-c": 3, "gap-trashed": 1}
    assert _orders(path, "prj-locked-list") == {"list-a": 4, "list-b": None}
