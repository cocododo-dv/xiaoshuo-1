"""迁移 20261012_0100：故事记忆的四张表（章摘要 ``chapter_digests``、线索标记 ``story_thread_marks``、编辑通读
``editorial_reviews`` 与它的意见 ``editorial_guidance``）加上，卷汇总表 ``volume_summaries`` 删掉。

- 升级：新表带各自的 CHECK、唯一约束与查找索引，CHECK 真的拦得住；意见随通读删除；卷汇总表连同它的行一起删（派生缓存，
  不导出），别的表原样；
- 降级：四张新表删掉，卷汇总表按 0059 的形状回来（行不回来）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate, table_columns, table_names

PREVIOUS_HEAD = "20261004_0099"
REVISION = "20261012_0100"


def _index_names(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,))
        }


def test_0100_adds_chapter_digests_and_drops_volume_summaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "story-memory-0100.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        insert_minimal_row(connection, "story_projects", {"project_id": "work-a", "title": "雨城旧信"})
        insert_minimal_row(
            connection,
            "volume_summaries",
            {"row_id": "vs1", "project_id": "work-a", "volume_seq": 1, "atmosphere_summary": "雨一直没停。"},
        )
    assert "chapter_digests" not in table_names(path)

    migrate(path, REVISION, monkeypatch)

    tables = table_names(path)
    assert "chapter_digests" in tables and "volume_summaries" not in tables
    assert {
        "row_id", "project_id", "chapter_id", "status", "origin", "body_hash", "previous_digest_row_id",
        "previous_digest_hash", "source_hash", "digest_json", "content_hash", "llm_call_id", "prompt_version",
        "created_at", "updated_at",
    } == table_columns(path, "chapter_digests")
    assert {"ix_chapter_digests_chapter_status", "ix_chapter_digests_project"} <= _index_names(path, "chapter_digests")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        assert connection.execute("SELECT title FROM story_projects WHERE project_id = 'work-a'").fetchone() == ("雨城旧信",)
        insert_minimal_row(
            connection,
            "chapter_digests",
            {"row_id": "cd1", "project_id": "work-a", "chapter_id": "ch1", "status": "active", "origin": "llm"},
        )
        for column, value in (("status", "draft"), ("origin", "import")):
            with pytest.raises(sqlite3.IntegrityError):
                insert_minimal_row(
                    connection,
                    "chapter_digests",
                    {"row_id": f"bad-{column}", "project_id": "work-a", "chapter_id": "ch1",
                     "status": "active", "origin": "llm", column: value},
                )


def test_0100_adds_thread_marks_and_editorial_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "story-memory-0100-editorial.db"
    migrate(path, REVISION, monkeypatch)

    assert {"row_id", "project_id", "thread_id", "status", "label_snapshot", "note", "created_at", "updated_at"} == (
        table_columns(path, "story_thread_marks")
    )
    assert {
        "review_id", "project_id", "through_chapter_id", "window_chapter_ids_json", "status", "origin", "source_hash",
        "review_json", "llm_call_id", "prompt_version", "created_at", "updated_at",
    } == table_columns(path, "editorial_reviews")
    assert {
        "guidance_id", "review_id", "project_id", "chapter_id", "position", "text", "reason", "author_text", "status",
        "decided_at", "created_at", "updated_at",
    } == table_columns(path, "editorial_guidance")
    assert "ix_editorial_reviews_project_status" in _index_names(path, "editorial_reviews")
    assert {"ix_editorial_guidance_review", "ix_editorial_guidance_project_status"} <= _index_names(path, "editorial_guidance")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        mark = {"row_id": "m1", "project_id": "work-a", "thread_id": "ch1-T1", "status": "dropped"}
        insert_minimal_row(connection, "story_thread_marks", mark)
        with pytest.raises(sqlite3.IntegrityError):  # 一条线只有一个标记
            insert_minimal_row(connection, "story_thread_marks", {**mark, "row_id": "m2"})
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_row(connection, "story_thread_marks", {**mark, "row_id": "m3", "thread_id": "ch1-T2", "status": "open"})
        insert_minimal_row(
            connection,
            "editorial_reviews",
            {"review_id": "r1", "project_id": "work-a", "through_chapter_id": "ch3", "status": "active", "origin": "auto"},
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_row(
                connection,
                "editorial_reviews",
                {"review_id": "r2", "project_id": "work-a", "through_chapter_id": "ch3", "status": "active", "origin": "cron"},
            )
        insert_minimal_row(
            connection,
            "editorial_guidance",
            {"guidance_id": "g1", "review_id": "r1", "project_id": "work-a", "status": "proposed", "position": 0},
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_minimal_row(
                connection,
                "editorial_guidance",
                {"guidance_id": "g2", "review_id": "r1", "project_id": "work-a", "status": "maybe", "position": 1},
            )
        with pytest.raises(sqlite3.IntegrityError):  # 意见必须挂在一份通读上
            insert_minimal_row(
                connection,
                "editorial_guidance",
                {"guidance_id": "g3", "review_id": "nope", "project_id": "work-a", "status": "proposed", "position": 2},
            )
        connection.execute("DELETE FROM editorial_reviews WHERE review_id = 'r1'")
        assert connection.execute("SELECT COUNT(*) FROM editorial_guidance").fetchone() == (0,)


def test_0100_downgrade_restores_the_volume_summaries_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "story-memory-0100-down.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    before = table_columns(path, "volume_summaries")
    migrate(path, REVISION, monkeypatch)

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)

    tables = table_names(path)
    assert not {"chapter_digests", "story_thread_marks", "editorial_reviews", "editorial_guidance"} & tables
    assert table_columns(path, "volume_summaries") == before
