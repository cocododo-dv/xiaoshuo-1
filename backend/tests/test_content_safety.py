from __future__ import annotations

import pytest

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.content_safety import ContentSafetyService
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService


def test_compound_minor_and_explicit_sexual_indicators_require_review() -> None:
    result = ContentSafetyService.assess("角色只有16岁，段落明确描写两人的性行为。", mode="review")

    assert result["requires_human_review"] is True
    assert result["blocking_codes"] == [
        "content_safety_review:sexual_content_with_minor_indicators"
    ]
    assert result["findings"][0]["confidence"] == "heuristic"
    assert result["limitations"]


def test_exact_author_acknowledgement_releases_only_that_content_risk() -> None:
    result = ContentSafetyService.assess(
        "角色只有16岁，段落明确描写两人的性行为。",
        acknowledged_codes=["sexual_content_with_minor_indicators"],
        mode="review",
    )

    assert result["blocking_codes"] == []
    assert result["findings"][0]["acknowledged"] is True
    assert result["warnings"][0]["issue_key"] == (
        "content_safety:sexual_content_with_minor_indicators"
    )


def test_unknown_prefilled_codes_never_enter_acknowledgement_audit() -> None:
    result = ContentSafetyService.assess(
        "角色只有16岁，段落明确描写两人的性行为。",
        acknowledged_codes=["not_a_real_finding"],
        mode="review",
    )

    assert result["blocking_codes"]
    assert result["acknowledged_codes"] == []


def test_actionable_self_harm_combination_requires_review_but_theme_alone_is_advisory() -> None:
    actionable = ContentSafetyService.assess(
        "他计划自杀，并逐项计算安眠药剂量和具体步骤。",
        mode="review",
    )
    thematic = ContentSafetyService.assess("她承认自己曾有过轻生念头，随后去寻求帮助。", mode="review")

    assert actionable["blocking_codes"] == [
        "content_safety_review:actionable_self_harm_detail"
    ]
    assert thematic["blocking_codes"] == []
    assert thematic["findings"][0]["code"] == "self_harm_theme"


def test_dark_genre_content_without_compound_high_risk_signal_is_not_blocked() -> None:
    result = ContentSafetyService.assess(
        "十六岁的侦探在雨夜调查旧案。凶手留下血迹，但现场没有尸体。",
        mode="review",
    )

    assert result["blocking_codes"] == []
    assert result["requires_human_review"] is False


def test_final_text_gate_blocks_unattended_archive_and_accepts_exact_risk_code(
    session,
    monkeypatch,
) -> None:
    session.add(StoryProject(project_id="SAFE_P1", title="Safety", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="SAFE_CH1",
            project_id="SAFE_P1",
            chapter_goal="核对风险",
            planned_scene_count=1,
        )
    )
    session.add(
        SceneCard(
            scene_id="SAFE_SC1",
            chapter_id="SAFE_CH1",
            project_id="SAFE_P1",
            scene_seq=1,
            scene_goal="核对风险",
            beats_json=[],
            onstage_chars_json=[],
        )
    )
    session.commit()
    # 风格参考 v3：没有绑定、没有全局受保护词时抄袭门本来就放行，不必再打桩
    text = "角色只有16岁，段落明确描写两人的性行为。"
    gate = FinalTextGateService(session)

    blocked = gate.evaluate(scene_id="SAFE_SC1", content=text)
    with pytest.raises(DomainError) as exc_info:
        gate.raise_if_not_archivable(blocked, scene_id="SAFE_SC1")
    assert exc_info.value.code == "CONTENT_SAFETY_REVIEW_REQUIRED"

    acknowledged = gate.evaluate(
        scene_id="SAFE_SC1",
        content=text,
        accepted_warning_codes=["sexual_content_with_minor_indicators"],
        author_confirmed_final=True,
    )
    assert not any(
        code.startswith("content_safety_review:")
        for code in acknowledged["archive_blockers"]
    )
    assert acknowledged["content_safety"]["findings"][0]["acknowledged"] is True
