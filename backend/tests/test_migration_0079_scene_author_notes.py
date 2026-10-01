from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import migrate

PREVIOUS_HEAD = "20260802_0078"
CURRENT_HEAD = "20260802_0079"


def test_0079_adds_durable_notes_without_changing_existing_scenes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scene-notes-0079.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            INSERT INTO story_projects(
                project_id, title, outline_text, planning_mode,
                snowflake_workflow_mode, status, approved_chapter_ids_json,
                trashed_flag, created_at, updated_at
            ) VALUES ('p', 'P', '', 'outline_driven', 'strict', 'outline_draft', '[]', 0, '1', '1');
            INSERT INTO chapter_goals(
                chapter_id, project_id, mid_aggregate_enabled, chapter_goal,
                state, display_order, trashed_flag, created_at, updated_at
            ) VALUES ('c', 'p', 0, 'goal', 'planned', 1, 0, '1', '1');
            INSERT INTO scene_cards(
                scene_id, chapter_id, project_id, scene_seq,
                onstage_chars_json, scene_goal, beats_json,
                is_chapter_last, state, words_current, trashed_flag,
                created_at, updated_at
            ) VALUES ('s', 'c', 'p', 1, '[]', 'goal', '[]', 0, 'todo', 0, 0, '1', '1');
            """
        )

    migrate(path, CURRENT_HEAD, monkeypatch)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        assert connection.execute(
            "SELECT scene_id, author_notes, author_notes_revision_no FROM scene_cards WHERE scene_id='s'"
        ).fetchone() == ("s", "", 0)
