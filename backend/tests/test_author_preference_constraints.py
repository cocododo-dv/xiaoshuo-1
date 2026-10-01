from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from novel_system.db.models import AuthorPreferenceProfile
from tests.support.migrations import migrate

REVISION_0071 = "20260716_0071"
REVISION_0072 = "20260716_0072"


@pytest.mark.parametrize(
    ("scope_type", "runtime_eligible"),
    [
        ("workspace", 0),
        ("global", -1),
        ("project", 2),
    ],
)
def test_author_preference_model_rejects_invalid_scope_and_runtime_flag(
    session,
    scope_type: str,
    runtime_eligible: int,
) -> None:
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                AuthorPreferenceProfile(
                    profile_id=f"invalid_{scope_type}_{runtime_eligible}",
                    scope_type=scope_type,
                    scope_ref_id="test",
                    status="draft",
                    runtime_eligible=runtime_eligible,
                    summary_json={},
                    source_patch_ids_json=[],
                )
            )
            session.flush()


def test_author_preference_model_accepts_supported_scopes_and_boolean_flags(session) -> None:
    profiles = [
        AuthorPreferenceProfile(
            profile_id=f"valid_{scope_type}",
            scope_type=scope_type,
            scope_ref_id="global" if scope_type == "global" else f"{scope_type}_1",
            status="approved" if runtime_eligible else "draft",
            runtime_eligible=runtime_eligible,
            summary_json={},
            source_patch_ids_json=[],
        )
        for scope_type, runtime_eligible in (
            ("global", 1),
            ("genre", 0),
            ("project", 1),
            ("chapter", 0),
        )
    ]
    session.add_all(profiles)
    session.flush()

    assert {profile.scope_type for profile in profiles} == {
        "global",
        "genre",
        "project",
        "chapter",
    }


def test_0072_migration_fails_closed_instead_of_relabeling_invalid_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "invalid-preference-values.db"
    migrate(database_path, REVISION_0072, monkeypatch)
    migrate(database_path, REVISION_0071, monkeypatch, down=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO author_preference_profiles (
                profile_id, scope_type, scope_ref_id, status,
                runtime_eligible, summary_json, source_patch_ids_json,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid_legacy_profile",
                "workspace",
                "legacy",
                "draft",
                2,
                "{}",
                "[]",
                "migration_test",
                "2026-07-16T00:00:00Z",
                "2026-07-16T00:00:00Z",
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="invalid_scope_rows=1, invalid_runtime_eligible_rows=1",
    ):
        migrate(database_path, REVISION_0072, monkeypatch)
