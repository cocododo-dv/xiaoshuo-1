from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import migrate

PREVIOUS_HEAD = "20260802_0077"
CURRENT_HEAD = "20260802_0078"


def test_0078_repairs_legacy_order_and_nullable_links_then_enforces_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "core-integrity-0078.db"
    migrate(database_path, PREVIOUS_HEAD, monkeypatch)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            INSERT INTO story_projects(
                project_id, title, outline_text, planning_mode,
                snowflake_workflow_mode, status, approved_chapter_ids_json,
                trashed_flag, created_at, updated_at
            ) VALUES
                (
                    'P1', 'Project', 'outline', 'outline_driven',
                    'strict', 'outline_draft', '[]', 0, '1', '1'
                ),
                (
                    'P2', 'Wrong owner', 'outline', 'outline_driven',
                    'strict', 'outline_draft', '[]', 0, '1', '1'
                );

            INSERT INTO chapter_goals(
                chapter_id, project_id, mid_aggregate_enabled, chapter_goal,
                state, display_order, trashed_flag, created_at, updated_at
            ) VALUES
                ('C1', 'P1', 0, 'one', 'planned', 1, 0, '1', '1'),
                ('C2', 'P1', 0, 'two', 'planned', 1, 0, '2', '2');

            INSERT INTO scene_cards(
                scene_id, chapter_id, project_id, scene_seq,
                onstage_chars_json, scene_goal, beats_json,
                is_chapter_last, state, words_current, trashed_flag,
                created_at, updated_at
            ) VALUES
                ('S1', 'C1', 'P1', 1, '[]', 'one', '[]', 0, 'todo', 0, 0, '1', '1'),
                ('S2', 'C1', 'P1', 1, '[]', 'two', '[]', 0, 'todo', 0, 0, '2', '2');

            INSERT INTO scene_drafts(
                row_id, scene_id, chapter_id, stage, content,
                source_bundle_id, source_bundle_hash, created_at
            ) VALUES (
                'D1', 'S1', 'C2', 'neutral', 'draft', 'B1', 'H1', '1'
            );

            INSERT INTO chapter_run_jobs(
                job_id, chapter_id, scene_id, status, job_type, created_at, updated_at
            ) VALUES (
                'J1', 'MISSING_CHAPTER', 'MISSING_SCENE',
                'failed', 'scene_run_full', '1', '1'
            );

            INSERT INTO chapter_contracts(
                contract_id, project_id, chapter_id, status,
                constraints_json, created_at, updated_at
            ) VALUES (
                'CTR1', 'P2', 'C1', 'drafting', '[]', '1', '1'
            );

            INSERT INTO chapter_audit_findings(
                finding_id, project_id, chapter_id, kind, severity,
                text, status, created_at, updated_at
            ) VALUES (
                'AUD1', 'P2', 'C1', 'drift', 'warn',
                'legacy mismatch', 'open', '1', '1'
            );
            """
        )

    migrate(database_path, CURRENT_HEAD, monkeypatch)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (CURRENT_HEAD,)

        chapter_orders = connection.execute(
            "SELECT display_order FROM chapter_goals "
            "WHERE project_id = 'P1' AND trashed_flag = 0"
        ).fetchall()
        assert len({row[0] for row in chapter_orders}) == 2
        scene_orders = connection.execute(
            "SELECT scene_seq FROM scene_cards "
            "WHERE chapter_id = 'C1' AND trashed_flag = 0"
        ).fetchall()
        assert len({row[0] for row in scene_orders}) == 2
        assert connection.execute(
            "SELECT chapter_id FROM scene_drafts WHERE row_id = 'D1'"
        ).fetchone() == ("C1",)
        assert connection.execute(
            "SELECT chapter_id, scene_id FROM chapter_run_jobs WHERE job_id = 'J1'"
        ).fetchone() == (None, None)
        assert connection.execute(
            "SELECT project_id FROM chapter_contracts WHERE contract_id = 'CTR1'"
        ).fetchone() == ("P1",)
        assert connection.execute(
            "SELECT project_id FROM chapter_audit_findings WHERE finding_id = 'AUD1'"
        ).fetchone() == ("P1",)

        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(chapter_goals)")
        }
        assert "ux_chapter_goals_active_project_display_order" in indexes
        scene_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(scene_cards)")
        }
        assert "ux_scene_cards_active_chapter_scene_seq" in scene_indexes
        contract_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(chapter_contracts)")
        }
        assert "ux_chapter_contracts_project_chapter" in contract_indexes

        job_fks = {
            (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA foreign_key_list(chapter_run_jobs)")
        }
        assert ("chapter_goals", "chapter_id", "chapter_id") in job_fks
        assert ("scene_cards", "scene_id", "scene_id") in job_fks
        contract_fks = {
            (row[2], row[3], row[4])
            for row in connection.execute("PRAGMA foreign_key_list(chapter_contracts)")
        }
        finding_fks = {
            (row[2], row[3], row[4])
            for row in connection.execute(
                "PRAGMA foreign_key_list(chapter_audit_findings)"
            )
        }
        assert ("chapter_goals", "chapter_id", "chapter_id") in contract_fks
        assert ("chapter_goals", "chapter_id", "chapter_id") in finding_fks

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chapter_run_jobs("
                "job_id, chapter_id, status, job_type, created_at, updated_at"
                ") VALUES ('J2', 'STILL_MISSING', 'failed', 'chapter_run_full', '2', '2')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chapter_contracts("
                "contract_id, project_id, chapter_id, status, constraints_json, "
                "created_at, updated_at"
                ") VALUES ('CTR2', 'P1', 'MISSING', 'drafting', '[]', '2', '2')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chapter_contracts("
                "contract_id, project_id, chapter_id, status, constraints_json, "
                "created_at, updated_at"
                ") VALUES ('CTR3', 'P1', 'C1', 'drafting', '[]', '3', '3')"
            )


def test_0078_fails_closed_for_non_nullable_orphan_audit_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "core-integrity-orphan-0078.db"
    migrate(database_path, PREVIOUS_HEAD, monkeypatch)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO scene_drafts("
            "row_id, scene_id, chapter_id, stage, content, source_bundle_id, "
            "source_bundle_hash, created_at"
            ") VALUES ('D_ORPHAN', 'NO_SCENE', 'NO_CHAPTER', 'neutral', "
            "'draft', 'B', 'H', '1')"
        )

    with pytest.raises(RuntimeError, match="scene_drafts.scene_id has 1 orphan"):
        migrate(database_path, CURRENT_HEAD, monkeypatch)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute(
            "SELECT content FROM scene_drafts WHERE row_id = 'D_ORPHAN'"
        ).fetchone() == ("draft",)
