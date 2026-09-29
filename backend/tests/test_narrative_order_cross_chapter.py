from __future__ import annotations

import pytest

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    NarrativeEvent,
    SceneCard,
    SceneMemory,
    StoryProject,
)
from novel_system.services.aggregator import Aggregator
from novel_system.services.catalog import CatalogService
from novel_system.services.errors import DomainError
from novel_system.services.narrative_event_log import NarrativeEventLog


PROJECT = "PROJ_NARRATIVE_ORDER"


def _seed_two_chapters(session) -> None:
    session.add(StoryProject(project_id=PROJECT, title="Order", outline_text="test"))
    for chapter_order in (1, 2):
        chapter_id = f"NO_CH{chapter_order}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=PROJECT,
                chapter_goal=f"chapter {chapter_order}",
                display_order=chapter_order,
                planned_scene_count=2,
            )
        )
        session.add(ChapterState(chapter_id=chapter_id, current_phase="drafting"))
        for scene_seq in (1, 2):
            session.add(
                SceneCard(
                    scene_id=f"{chapter_id}_SC{scene_seq}",
                    chapter_id=chapter_id,
                    project_id=PROJECT,
                    scene_seq=scene_seq,
                    scene_goal="test",
                )
            )
    session.commit()


def _event(log: NarrativeEventLog, scene_id: str, value: str, *, confidence: str = "high"):
    chapter_id = scene_id.rsplit("_SC", 1)[0]
    return log.log_event(
        project_id=PROJECT,
        scene_id=scene_id,
        chapter_id=chapter_id,
        event_type="character_state",
        entity_type="character",
        entity_id="CHAR_A",
        fact_key="location",
        fact_value=value,
        confidence=confidence,
        authority_status="accepted",
        source_kind="test_fixture",
    )


def test_second_chapter_first_scene_replays_previous_chapter(session) -> None:
    _seed_two_chapters(session)
    log = NarrativeEventLog(session)
    _event(log, "NO_CH1_SC1", "harbor")
    _event(log, "NO_CH1_SC2", "archive")
    session.commit()

    state = log.project_character_state(
        "CHAR_A", PROJECT, before_scene_id="NO_CH2_SC1",
    )

    assert state.get("location") == "archive"
    prompt = log.format_state_for_prompt(
        PROJECT,
        None,
        scene_id="NO_CH2_SC1",
        onstage_character_ids=["CHAR_A"],
    )
    assert "location: archive" in prompt


def test_story_position_wins_over_insert_time_and_local_seq_confidence(session) -> None:
    _seed_two_chapters(session)
    log = NarrativeEventLog(session)

    # Write later-story data first. Replay must follow catalog order, not insertion time.
    _event(log, "NO_CH2_SC1", "tower", confidence="extracted")
    _event(log, "NO_CH1_SC2", "archive", confidence="high")
    session.commit()

    state = log.project_character_state("CHAR_A", PROJECT)
    assert state.get("location") == "tower"

    # Both events have chapter-local scene_seq=1/2 combinations. A high-confidence
    # fact in another chapter must not be treated as a same-scene competitor.
    _event(log, "NO_CH1_SC1", "harbor", confidence="high")
    session.commit()
    state = log.project_character_state("CHAR_A", PROJECT)
    assert state.get("location") == "tower"


def test_chapter_reorder_changes_dynamic_replay_without_rewriting_events(session) -> None:
    _seed_two_chapters(session)
    log = NarrativeEventLog(session)
    first = _event(log, "NO_CH1_SC1", "harbor")
    second = _event(log, "NO_CH2_SC1", "tower")
    session.commit()

    assert log.project_character_state("CHAR_A", PROJECT).get("location") == "tower"
    before = {
        event.event_id: (
            event.scene_seq,
            event.scene_id,
            event.chapter_id,
            event.fact_value,
            event.created_at,
        )
        for event in (first, second)
    }

    # Reorder through the catalog boundary.  The service uses a collision-free
    # two-phase assignment because SQLite cannot defer the active-order unique
    # index while two rows swap positions.
    CatalogService(session).reorder_chapters(PROJECT, ["NO_CH2", "NO_CH1"])
    session.expire_all()

    # CH2 is now narrated first and CH1 last, so CH1's fact becomes current.
    assert log.project_character_state("CHAR_A", PROJECT).get("location") == "harbor"
    rows = [session.get(NarrativeEvent, event_id) for event_id in before]
    assert len(rows) == 2
    assert {
        event.event_id: (
            event.scene_seq,
            event.scene_id,
            event.chapter_id,
            event.fact_value,
            event.created_at,
        )
        for event in rows
    } == before

def test_legacy_scene_seq_cursor_is_rejected_for_multi_chapter_project(session) -> None:
    _seed_two_chapters(session)
    log = NarrativeEventLog(session)
    _event(log, "NO_CH1_SC1", "harbor")
    session.commit()

    with pytest.raises(DomainError) as rejected:
        log.project_character_state("CHAR_A", PROJECT, up_to_scene_seq=1)

    assert rejected.value.code == "NARRATIVE_CURSOR_AMBIGUOUS"


def test_final_aggregate_uses_scene_order_not_memory_row_id(session) -> None:
    _seed_two_chapters(session)
    session.add_all([
        SceneMemory(
            row_id="zzz_scene_one",
            scene_id="NO_CH1_SC1",
            chapter_id="NO_CH1",
            content="scene one",
            source_bundle_id="b1",
            final_scene_row_id="f1",
            active_flag=1,
        ),
        SceneMemory(
            row_id="aaa_scene_two",
            scene_id="NO_CH1_SC2",
            chapter_id="NO_CH1",
            content="scene two",
            source_bundle_id="b2",
            final_scene_row_id="f2",
            active_flag=1,
        ),
    ])
    session.commit()

    result = Aggregator(session).run_final_aggregate("NO_CH1")
    session.flush()

    assert result["status"] == "created"
    aggregate = session.get(ChapterMemory, result["chapter_memory_row_id"])
    assert aggregate is not None
    assert aggregate.content == "scene one\nscene two"


@pytest.mark.parametrize(
    ("memories", "expected_reason"),
    [
        (
            [
                ("m1", "NO_CH1_SC1"),
                ("m2", "NO_CH1_SC1"),
            ],
            "active_scene_memory_ambiguous",
        ),
        (
            [("m_orphan", "NO_CH1_MISSING")],
            "scene_memory_position_orphan",
        ),
    ],
)
def test_final_aggregate_blocks_invalid_active_memory_positions(
    session, memories, expected_reason,
) -> None:
    _seed_two_chapters(session)
    for row_id, scene_id in memories:
        session.add(
            SceneMemory(
                row_id=row_id,
                scene_id=scene_id,
                chapter_id="NO_CH1",
                content=row_id,
                source_bundle_id=f"b_{row_id}",
                final_scene_row_id=f"f_{row_id}",
                active_flag=1,
            )
        )
    session.commit()

    result = Aggregator(session).run_final_aggregate("NO_CH1")

    assert result["status"] == "blocked"
    assert result["reason"] == expected_reason
    assert result["chapter_memory_row_id"] is None
