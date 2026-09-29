"""Tests for narrative event sourcing — blueprint §2 / §17 Action B."""
from __future__ import annotations


from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    NarrativeEvent,
    SceneCard,
)
from novel_system.services.narrative_event_log import (
    NarrativeEventLog as _BaseNarrativeEventLog,
)


class NarrativeEventLog(_BaseNarrativeEventLog):
    """Legacy projection fixtures explicitly represent author-accepted facts."""

    def log_event(self, **kwargs):
        kwargs.setdefault("authority_status", "accepted")
        kwargs.setdefault("source_kind", "test_fixture")
        return super().log_event(**kwargs)


def _seed_project(session) -> None:
    session.add(ChapterGoal(chapter_id="CH_EVT01", planned_scene_count=3, chapter_goal="test"))
    session.add(ChapterState(chapter_id="CH_EVT01", current_phase="drafting"))
    for seq in (1, 2, 3):
        session.add(SceneCard(
            scene_id=f"CH_EVT01_SC0{seq}",
            chapter_id="CH_EVT01",
            scene_seq=seq,
            scene_goal=f"Scene {seq}",
        ))
    session.commit()


def test_log_event_creates_row(session) -> None:
    _seed_project(session)
    log = _BaseNarrativeEventLog(session)
    evt = log.log_event(
        project_id="PROJ1",
        scene_id="CH_EVT01_SC01",
        chapter_id="CH_EVT01",
        event_type="character_state",
        entity_type="character",
        entity_id="CHAR_LIN",
        fact_key="location",
        fact_value="北境",
    )
    session.commit()
    assert evt.event_id.startswith("nevt_")
    assert evt.scene_seq == 1
    row = session.get(NarrativeEvent, evt.event_id)
    assert row is not None
    assert row.fact_value == "北境"
    assert row.authority_status == "planned"
    assert row.source_kind == "legacy_plan"


def test_project_character_state_replays_events(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="location", fact_value="沧澜城",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="physical_state", fact_value="healthy",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC02", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="location", fact_value="北境",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC02", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="physical_state", fact_value="right_arm_severed",
    )
    session.commit()

    state_at_1 = log.project_character_state("CHAR_LIN", "PROJ1", up_to_scene_seq=1)
    assert state_at_1.get("location") == "沧澜城"
    assert state_at_1.get("physical_state") == "healthy"

    state_at_2 = log.project_character_state("CHAR_LIN", "PROJ1", up_to_scene_seq=2)
    assert state_at_2.get("location") == "北境"
    assert state_at_2.get("physical_state") == "right_arm_severed"

    state_full = log.project_character_state("CHAR_LIN", "PROJ1")
    assert state_full.get("location") == "北境"


def test_known_facts_tracks_character_learns(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_learns", entity_type="character", entity_id="CHAR_SU",
        fact_key="knows_lin_injured", fact_value="false",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC03", chapter_id="CH_EVT01",
        event_type="character_learns", entity_type="character", entity_id="CHAR_SU",
        fact_key="knows_lin_injured", fact_value="true",
    )
    session.commit()

    facts_at_1 = log.known_facts_for_character("CHAR_SU", "PROJ1", up_to_scene_seq=1)
    assert len(facts_at_1) == 1
    assert facts_at_1[0].fact_value == "false"

    facts_at_3 = log.known_facts_for_character("CHAR_SU", "PROJ1", up_to_scene_seq=3)
    assert len(facts_at_3) == 2
    assert facts_at_3[-1].fact_value == "true"


def test_all_facts_at_scene_projects_all_characters(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="location", fact_value="沧澜城",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_SU",
        fact_key="location", fact_value="京城",
    )
    session.commit()

    states = log.all_facts_at_scene("PROJ1", scene_seq=1)
    assert "CHAR_LIN" in states
    assert "CHAR_SU" in states
    assert states["CHAR_LIN"].get("location") == "沧澜城"
    assert states["CHAR_SU"].get("location") == "京城"


def test_check_consistency_passes_for_valid_text(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="alive", fact_value="alive",
    )
    session.commit()

    report = log.check_consistency(
        "CHAR_LIN walked into the room and sat down.",
        "PROJ1", "CH_EVT01_SC02",
        character_ids=["CHAR_LIN"],
    )
    assert report.passed is True
    assert report.facts_checked >= 1


def test_check_consistency_detects_dead_character_acting(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="char_lin",
        fact_key="alive", fact_value="dead",
    )
    session.commit()

    report = log.check_consistency(
        "char_lin walked to the gate and spoke loudly.",
        "PROJ1", "CH_EVT01_SC02",
        character_ids=["char_lin"],
    )
    assert report.passed is False
    assert len(report.violations) >= 1
    assert report.violations[0].fact_key == "alive"


def test_format_state_for_prompt(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="location", fact_value="北境",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_LIN",
        fact_key="physical_state", fact_value="right_arm_severed",
    )
    session.commit()

    prompt_section = log.format_state_for_prompt(
        "PROJ1", scene_seq=2,
        onstage_character_ids=["CHAR_LIN"],
    )
    assert "CHAR_LIN" in prompt_section
    assert "location: 北境" in prompt_section
    assert "physical_state: right_arm_severed" in prompt_section
    assert "Authoritative Character State" in prompt_section


def test_format_state_for_prompt_pov_hides_other_secret(session) -> None:
    """Wave 4：传入 pov 时，format_state_for_prompt 委派投影，隐藏他人秘密正文。"""
    _seed_project(session)
    log = NarrativeEventLog(session)
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_X",
        fact_key="secret_held_by", fact_value="CHAR_X是内奸",
    )
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_X",
        fact_key="location", fact_value="议事厅",
    )
    session.commit()

    out = log.format_state_for_prompt(
        "PROJ1", scene_seq=2,
        pov_character_id="CHAR_POV", onstage_character_ids=["CHAR_X", "CHAR_POV"],
    )
    assert "CHAR_X是内奸" not in out          # 秘密正文被投影抑制
    assert "location: 议事厅" in out          # 公共事实保留


def test_information_asymmetry_digest_pov_hides_secrets(session) -> None:
    """Wave 4：传入 pov 时，信息不对称摘要不再打印 'Secrets held by X' 正文。"""
    _seed_project(session)
    log = NarrativeEventLog(session)
    log.log_event(
        project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
        event_type="character_state", entity_type="character", entity_id="CHAR_X",
        fact_key="secret_held_by", fact_value="毒药在酒里",
    )
    session.commit()

    out = log.information_asymmetry_digest(
        "PROJ1", 2, ["CHAR_X", "CHAR_POV"], pov_character_id="CHAR_POV",
    )
    assert "毒药在酒里" not in out
    assert "Secrets held by CHAR_X" not in out


def test_log_events_batch(session) -> None:
    _seed_project(session)
    log = NarrativeEventLog(session)

    events = log.log_events_batch([
        dict(
            project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
            event_type="character_state", entity_type="character", entity_id="A",
            fact_key="location", fact_value="east",
        ),
        dict(
            project_id="PROJ1", scene_id="CH_EVT01_SC01", chapter_id="CH_EVT01",
            event_type="character_state", entity_type="character", entity_id="B",
            fact_key="location", fact_value="west",
        ),
    ])
    session.commit()

    assert len(events) == 2
    assert all(e.event_id.startswith("nevt_") for e in events)
