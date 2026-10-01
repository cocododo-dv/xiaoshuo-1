from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# 升到 head 后断言的版本号跟着 schema_contract 走（test_schema_contract_revision 把它钉在 Alembic 唯一 head 上），
# 以后再加迁移不必回来改这个文件。
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION as CURRENT_HEAD
from tests.support.migrations import migrate

PREVIOUS_HEAD = "20260802_0079"


def test_0080_adds_empty_deep_review_preferences_without_changing_scenes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scene-deep-review-0080.db"
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

    migrate(path, "head", monkeypatch)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        assert connection.execute(
            """
            SELECT deep_review_decision_log_json,
                   deep_review_ignored_keys_json,
                   deep_review_preferences_revision_no
            FROM scene_cards WHERE scene_id='s'
            """
        ).fetchone() == ("[]", "[]", 0)
