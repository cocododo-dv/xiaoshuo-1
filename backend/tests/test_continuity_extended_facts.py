from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.db.models import ChapterGoal, QcReport, SceneCard, SceneRunState, StoryProject
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.llm_task_runner import begin_llm_execution, end_llm_execution
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.qc_engine import HardQcEngine, _deterministic_quality_issues
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


# ---------------------------------------------------------------------------
# 确定性连续性检查（quality_checks.continuity）接进硬质检与成稿门：两边用的是同一份检查、调用方的会话
# ---------------------------------------------------------------------------

SEVERED_TEXT = "顾舟抬起右手，稳稳握住长刀。"


class _HardQcPayloadRunner:
    """假 LLMNodeRunner：原样交回一份硬质检回答（不经记账）。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def run(self, **kwargs):  # noqa: ANN003
        return SimpleNamespace(
            llm_call_id="llm_call_continuity",
            response=SimpleNamespace(structured_output=dict(self.payload)),
        )


def _hard_qc(session, content: str, *, rewrite_brief: list[str] | None = None):
    session.add(
        SceneRunState(
            scene_id=TARGET_ID,
            scene_status="ready",
            active_execution_id="exec-continuity",
            run_execution_status="active",
        )
    )
    session.flush()
    runner = _HardQcPayloadRunner(
        {
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [],
            "rewrite_brief": rewrite_brief or [],
        }
    )
    token = begin_llm_execution("exec-continuity")
    try:
        return HardQcEngine(session, llm_runner=runner).evaluate(
            scene_id=TARGET_ID,
            bundle={
                "bundle_id": "bundle_continuity",
                "bundle_snapshot_hash": "bundle_hash_continuity",
                "snapshot": {"scene_id": TARGET_ID, "chapter_id": CHAPTER_ID, "inline_digests": {"scene_card": "Goal"}},
            },
            neutral_draft_row_id="draft_neutral_continuity",
            neutral_content=content,
            execution_step_key="hard_qc:0",
        )
    finally:
        end_llm_execution(token)


def _log_uncommitted_severed_arm(session) -> None:
    """先把作品 / 章 / 场建好（另记一条无关的事实），再把「右臂已断」记进同一个会话、不提交：检查若另开会话读库，
    就看不见这一条。"""
    _log(session, entity="顾舟", key="appearance", value="hair_color:black")
    NarrativeEventLog(session).log_event(
        project_id=PROJECT_ID,
        chapter_id=CHAPTER_ID,
        scene_id=SETUP_ID,
        event_type="character_state",
        entity_type="character",
        entity_id="顾舟",
        fact_key="physical_state",
        fact_value="right_arm_severed",
        authority_status="accepted",
        source_kind="test_fixture",
    )
    session.flush()


def test_hard_qc_sends_back_a_draft_that_contradicts_the_event_log(session) -> None:
    """模型说 pass，正文却与事件账本记下的硬事实矛盾：检查读调用方会话里刚记下的事件，检测器即复核器定 Q1，
    硬质检改判局部重写。"""
    _log_uncommitted_severed_arm(session)

    decision = _hard_qc(session, SEVERED_TEXT)

    report = session.get(QcReport, decision.qc_report_id)
    assert decision.branch == "rewrite_partial"
    assert report.resolution_code == "hard_fail_partial"
    issue = next(item for item in report.issues_json if item["issue_key"] == "event_log_consistency_violation")
    assert issue["quality_level"] == "Q1" and issue["blocking"] is True
    assert issue["severity"] == "high"
    assert issue["verified_by"] == "narrative_event_log_keyword"
    assert issue["authority_ref"] == "event:顾舟.physical_state"
    assert session.get(SceneRunState, TARGET_ID).scene_status == "hard_qc_partial_rewrite_required"


def test_final_gate_blocks_a_text_that_contradicts_the_event_log(session) -> None:
    """成稿门走同一份检查：已证实的 Q1 挡住归档（不许作者豁免时），证据指向事件账本。"""
    _log_uncommitted_severed_arm(session)

    result = FinalTextGateService(session).evaluate(
        scene_id=TARGET_ID, content=SEVERED_TEXT, allow_author_waiver=False
    )

    assert "continuity:event_log_consistency_violation" in result["archive_blockers"]
    assert result["safe_to_archive"] is False
    blocking = result["continuity"]["blocking_issues"]
    assert [item["issue_key"] for item in blocking] == ["event_log_consistency_violation"]
    assert blocking[0]["verified_by"] == "narrative_event_log_keyword"

    clean = FinalTextGateService(session).evaluate(
        scene_id=TARGET_ID, content="顾舟用左手推开门，把长刀留在桌上。", allow_author_waiver=False
    )
    assert not any(code.startswith("continuity:") for code in clean["archive_blockers"])


def test_hard_qc_flags_required_beats_dumped_at_the_tail_as_a_warning(session) -> None:
    """必写节拍在段尾堆成清单：质检并入 ``mechanical_required_beat_listing``（Q2，不改判），修改简报里多一句怎么改。"""
    _log(session, entity="顾舟", key="appearance", value="hair_color:black")
    scene = session.get(SceneCard, TARGET_ID)
    scene.must_include_text = "旧信；案卷；雨城的钟"
    session.flush()

    decision = _hard_qc(
        session,
        "林昭推门进屋，把伞靠在墙边，雨水顺着伞骨往下淌。\n\n最后需要包含：旧信、案卷、雨城的钟。",
        rewrite_brief=["把门口那一段写慢一点。"],
    )

    report = session.get(QcReport, decision.qc_report_id)
    assert decision.branch == "continue"
    issue = next(item for item in report.issues_json if item["issue_key"] == "mechanical_required_beat_listing")
    assert issue["quality_level"] == "Q2" and issue["blocking"] is False
    assert issue["source"] == "deterministic"
    assert [entry["instruction"] for entry in report.rewrite_brief_json] == [
        "把门口那一段写慢一点。",
        "将必须出现的剧情节拍自然织入动作和因果，不要在段尾追加清单。",
    ]
