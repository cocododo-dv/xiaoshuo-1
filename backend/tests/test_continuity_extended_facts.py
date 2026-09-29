from __future__ import annotations

import pytest

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.qc_engine import _deterministic_quality_issues
from novel_system.services.quality_classifier import classify_issues


PROJECT_ID = "continuity_extended"
CHAPTER_ID = "continuity_extended_ch"
SETUP_ID = "continuity_extended_setup"
TARGET_ID = "continuity_extended_target"


def _log(session, *, entity: str, key: str, value: str) -> NarrativeEventLog:
    if session.get(StoryProject, PROJECT_ID) is None:
        session.add(StoryProject(project_id=PROJECT_ID, title="Extended continuity", outline_text=""))
        session.flush()
        session.add(
            ChapterGoal(
                chapter_id=CHAPTER_ID,
                project_id=PROJECT_ID,
                chapter_goal="Verify structured continuity facts.",
            )
        )
        session.flush()
        session.add_all(
            [
                SceneCard(
                    scene_id=SETUP_ID,
                    chapter_id=CHAPTER_ID,
                    project_id=PROJECT_ID,
                    scene_seq=1,
                    scene_goal="establish facts",
                ),
                SceneCard(
                    scene_id=TARGET_ID,
                    chapter_id=CHAPTER_ID,
                    project_id=PROJECT_ID,
                    scene_seq=2,
                    scene_goal="check prose",
                ),
            ]
        )
        session.flush()
    log = NarrativeEventLog(session)
    log.log_event(
        project_id=PROJECT_ID,
        chapter_id=CHAPTER_ID,
        scene_id=SETUP_ID,
        event_type="character_state",
        entity_type="character",
        entity_id=entity,
        fact_key=key,
        fact_value=value,
        authority_status="accepted",
        source_kind="test_fixture",
    )
    session.commit()
    return log


@pytest.mark.parametrize(
    ("key", "value", "text"),
    [
        ("physical_state", "unconscious", "顾舟忽然起身说道：不要关灯。"),
        ("physical_state", "blind", "顾舟看见窗外升起一盏红灯。"),
        ("physical_state", "right_arm_severed", "顾舟抬起右手，稳稳握住长刀。"),
        ("appearance", "hair_color:black", "顾舟的一头金色头发在灯下发亮。"),
        ("appearance", "eye_color:green", "顾舟的蓝色眼睛映着火光。"),
        ("ability", "cannot:magic", "顾舟施法点燃了整面石墙。"),
        ("ability", "cannot:swim", "顾舟游泳穿过了冰冷的河道。"),
    ],
)
def test_structured_extended_fact_contradictions_are_detected(session, key, value, text) -> None:
    log = _log(session, entity="顾舟", key=key, value=value)

    report = log.check_consistency(text, PROJECT_ID, TARGET_ID, character_ids=["顾舟"])

    assert not report.passed
    assert any(item.fact_key == key for item in report.violations)


@pytest.mark.parametrize(
    ("key", "value", "text"),
    [
        ("physical_state", "unconscious", "顾舟仍在昏迷中，护士调低了灯光。"),
        ("physical_state", "blind", "顾舟借助屏幕阅读器听完了整封信。"),
        ("physical_state", "paralyzed", "顾舟试图站起，却没能离开轮椅。"),
        ("appearance", "hair_color:black", "顾舟围着金色围巾，黑色头发被雨打湿。"),
        ("appearance", "hair_color:black", "旧照片里，顾舟曾经染成金色的头发已经褪色。"),
        ("ability", "cannot:magic", "顾舟试图施法，却没能唤起任何火星。"),
        ("ability", "cannot:swim", "顾舟无法游泳，只能沿岸寻找小船。"),
    ],
)
def test_structured_extended_fact_near_misses_do_not_false_alarm(session, key, value, text) -> None:
    log = _log(session, entity="顾舟", key=key, value=value)

    report = log.check_consistency(text, PROJECT_ID, TARGET_ID, character_ids=["顾舟"])

    assert report.passed, [(item.fact_key, item.evidence) for item in report.violations]


def test_limb_and_item_actions_must_belong_to_the_affected_character(session) -> None:
    log = _log(session, entity="顾舟", key="missing_limb", value="right_arm")
    log.log_event(
        project_id=PROJECT_ID,
        chapter_id=CHAPTER_ID,
        scene_id=SETUP_ID,
        event_type="item_change",
        entity_type="character",
        entity_id="顾舟",
        fact_key="has_item",
        fact_value="lost:盐钟",
        authority_status="accepted",
        source_kind="test_fixture",
    )
    session.commit()

    report = log.check_consistency(
        "苏晚抬起右手握住长刀，又拿出盐钟；顾舟站在一旁。",
        PROJECT_ID,
        TARGET_ID,
        character_ids=["顾舟"],
    )

    assert report.passed, [(item.fact_key, item.evidence) for item in report.violations]


def test_continuity_engine_failure_surfaces_nonblocking_warning(monkeypatch) -> None:
    def fail_check(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("sensitive database detail")

    monkeypatch.setattr(NarrativeEventLog, "check_consistency", fail_check)
    scene = SceneCard(
        scene_id=TARGET_ID,
        chapter_id=CHAPTER_ID,
        project_id=PROJECT_ID,
        scene_seq=2,
        scene_goal="check",
    )

    raw = _deterministic_quality_issues(scene, "正文")
    classified = classify_issues(raw, scene=scene, content="正文")

    assert raw[0]["issue_key"] == "continuity_validation_unavailable"
    assert "sensitive database detail" not in str(raw)
    assert classified[0]["quality_level"] == "Q2"
    assert classified[0]["blocking"] is False
