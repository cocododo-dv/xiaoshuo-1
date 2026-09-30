"""P0-4 · authoritative character entity — regression tests.

Covers:

1.  Confirming the character steps in the v2 workspace writes the canonical
    ``StoryCharacter`` entity: 04 (character sheets) creates the rows with the
    summary layer, 08 (character bibles) adds the bible layer on the SAME rows.
    (Ported from the retired v1 planner in 2026-09-30, R9; the v1-only copy of the
    06 synopses into ``StoryCharacter.synopsis_json`` was deliberately not ported.)
2.  Runtime impact analysis keys identity on ``character_id`` ONLY — never on a
    mutable ``display_name`` / ``name``. A character with no stable id yields a
    broad-invalidation signal instead of a mis-keyed identity.
3.  Renaming a character keeps the same id, so it reads as a *scoped change*
    (not an add+remove), keeping downstream invalidation precise.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from novel_system.db.models import StoryCharacter
from novel_system.services.project_runtime_invalidation import SnowflakeImpactAnalyzer
from tests.real_llm_fakes import install_skeleton_snowflake


@pytest.fixture(autouse=True)
def _skeleton(monkeypatch):
    install_skeleton_snowflake(monkeypatch, llm_enabled=True)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _create_project(client) -> str:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "雨城残响",
            "genre": "都市悬疑",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": (
                "女主收到一封来自十年前的信。\n"
                "她回到雨城，发现旧案和家族秘密有关。\n"
                "结尾她决定公开真相。"
            ),
        },
        headers={"X-Idempotency-Key": f"create-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _approve_step(client, project_id: str, step_key: str) -> dict:
    generated = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json={},
        headers={"X-Idempotency-Key": f"gen-{project_id}-{step_key}"},
    )
    assert generated.status_code == 200, generated.text
    approved = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-{project_id}-{step_key}"},
    )
    assert approved.status_code == 200, approved.text
    return approved.json()["data"]["step"]


def _characters(session, project_id: str) -> list[StoryCharacter]:
    session.expire_all()
    return list(session.execute(select(StoryCharacter).where(StoryCharacter.project_id == project_id)).scalars())


# --------------------------------------------------------------------------- #
# 1. the character steps write the canonical entity
# --------------------------------------------------------------------------- #
def test_confirming_character_sheets_and_bibles_writes_the_story_characters(client, session) -> None:
    project_id = _create_project(client)
    for step_key in ["book_brief", "one_sentence_summary", "one_paragraph_summary"]:
        _approve_step(client, project_id, step_key)
    assert _characters(session, project_id) == []

    sheets = _approve_step(client, project_id, "character_sheets")
    rows = _characters(session, project_id)
    names = {item["display_name"] for item in sheets["draft"]["characters"]}
    assert rows and {row.display_name for row in rows} == names
    assert all(row.status == "approved" and row.summary_json.get("goal") for row in rows)
    assert all(not row.bible_json for row in rows)

    for step_key in ["short_synopsis", "character_synopses", "long_synopsis"]:
        _approve_step(client, project_id, step_key)
    _approve_step(client, project_id, "character_bibles")
    rows_after = _characters(session, project_id)
    assert {row.character_id for row in rows_after} == {row.character_id for row in rows}, "08 更新同一批实体，不另建"
    assert all((row.bible_json.get("psychological_profile") or {}).get("deepest_fear") for row in rows_after)


# --------------------------------------------------------------------------- #
# 2. identity keys on character_id only
# --------------------------------------------------------------------------- #
def test_impact_keys_on_character_id_only(session) -> None:
    analyzer = SnowflakeImpactAnalyzer(session)

    with_id = {"characters": [{"character_id": "P_CHAR_a1", "display_name": "林岚"}]}
    keyed = analyzer._characters_by_id(with_id)
    assert keyed is not None
    assert set(keyed) == {"P_CHAR_a1"}

    # No stable id → cannot scope precisely → signal broad invalidation (None),
    # instead of mis-keying identity on a mutable display name (old behaviour).
    without_id = {"characters": [{"display_name": "林岚"}]}
    assert analyzer._characters_by_id(without_id) is None


# --------------------------------------------------------------------------- #
# 3. rename is a scoped change, not an identity fork
# --------------------------------------------------------------------------- #
def test_rename_is_a_change_not_an_identity_fork(session) -> None:
    analyzer = SnowflakeImpactAnalyzer(session)
    previous = {"characters": [{"character_id": "P_CHAR_a1", "display_name": "林岚"}]}
    current = {"characters": [{"character_id": "P_CHAR_a1", "display_name": "林岚（化名）"}]}

    changed = analyzer._changed_character_ids(previous, current)
    # Same id on both sides → set equality holds → reported as a precise change,
    # so downstream invalidation stays scoped instead of degrading to broadcast.
    assert changed == {"P_CHAR_a1"}
