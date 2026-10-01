"""软质检回包的校验与规范化（``qc_validator.validate_qc_report``，type ``soft_qc``）：补丁 / 放行两种形状、模型把问题写成
字符串或字典、风格分的范围与别名、未知诊断字段丢掉、放行时缺的结转说明补上。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_system.services.qc_validator import QCValidationError, validate_qc_report
from tests.support.qc import soft_qc_payload as _base_soft_qc_payload


def test_soft_qc_validator_accepts_patch_and_waive_payloads() -> None:
    patch = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_patch",
            next_action="patch",
            issues=[{"issue_key": "cadence_flat", "message": "The opening needs a stronger pulse."}],
            rewrite_brief=["Tighten the first paragraph.", "Shift the line breaks earlier."],
        ),
    )
    waive = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_waive",
            next_action="pass_with_notes",
            carry_forward_note=True,
            note_scope="chapter_memory",
            carry_note_text="Keep the envelope as a recurring tension motif.",
        ),
    )

    assert patch.resolution_code == "soft_patch"
    assert patch.next_action == "patch"
    assert patch.pass_flag is False
    assert patch.rewrite_brief == ["Tighten the first paragraph.", "Shift the line breaks earlier."]
    assert waive.resolution_code == "soft_waive"
    assert waive.next_action == "pass_with_notes"
    assert waive.pass_flag is True
    assert waive.carry_forward_note is True
    assert waive.note_scope == "chapter_memory"
    assert waive.carry_note_text == "Keep the envelope as a recurring tension motif."


def test_soft_qc_validator_normalizes_string_issues_from_model_payload() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_pass",
            next_action="pass",
            issues=[
                "草稿精准执行了场景目标、强制节拍和结尾钩子。",
            ],
        ),
    )

    assert report.issues[0].issue_key == "local_model_issue"
    assert report.issues[0].message == "草稿精准执行了场景目标、强制节拍和结尾钩子。"


def test_soft_qc_validator_normalizes_dict_issues_from_model_payload() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_pass",
            next_action="pass",
            issues={
                "style_adherence": 0.95,
                "summary": {"message": "Draft is ready to archive."},
            },
        ),
    )

    assert report.issues[0].issue_key == "style_adherence"
    assert report.issues[0].message == "0.95"
    assert report.issues[1].issue_key == "summary"
    assert report.issues[1].message == "Draft is ready to archive."


def test_soft_qc_validator_accepts_style_score_contract_and_rejects_out_of_range() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_patch",
            next_action="patch",
            issues=[{"issue_key": "style_profile_drift", "message": "Dialogue ratio is too high."}],
            rewrite_brief=["Reduce dialogue and restore interior pressure."],
            style_score=0.62,
            style_dimensions=[
                {
                    "name": "rhythm",
                    "score": 0.7,
                    "evidence": "Several paragraph endings carry pressure.",
                },
                {
                    "name": "dialogue_ratio",
                    "score": 0.45,
                    "evidence": "Dialogue crowds out the requested interior distance.",
                },
            ],
            style_deviations=[
                {
                    "dimension": "dialogue_ratio",
                    "severity": "medium",
                    "patch_brief": "Cut two spoken lines and move one beat into narration.",
                }
            ],
        ),
    )

    assert report.style_score == 0.62
    assert report.style_dimensions[0].name == "rhythm"
    assert report.style_dimensions[1].score == 0.45
    assert report.style_deviations[0].patch_brief == "Cut two spoken lines and move one beat into narration."

    with pytest.raises((QCValidationError, ValidationError)):
        validate_qc_report(
            "soft_qc",
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
                style_score=1.2,
                style_dimensions=[{"name": "rhythm", "score": 1.3, "evidence": "too high"}],
            ),
        )


def test_soft_qc_validator_maps_style_scores_alias_from_model_payload() -> None:
    payload = _base_soft_qc_payload(
        resolution_code="soft_pass",
        next_action="pass",
    )
    payload["style_scores"] = {
        "rhythm": 0.9,
        "syntax": 0.8,
        "imagery": 1.0,
    }

    report = validate_qc_report("soft_qc", payload)

    assert round(report.style_score or 0, 4) == 0.9
    assert [item.name for item in report.style_dimensions] == ["rhythm", "syntax", "imagery"]
    assert report.style_dimensions[0].score == 0.9


def test_soft_qc_validator_drops_unknown_diagnostic_fields_from_model_payload() -> None:
    payload = _base_soft_qc_payload(
        resolution_code="soft_pass",
        next_action="pass",
    )
    payload["overall_comment"] = "Model-side diagnostic note."

    report = validate_qc_report("soft_qc", payload)

    assert report.resolution_code == "soft_pass"
    assert not hasattr(report, "overall_comment")


def test_soft_qc_validator_derives_waive_note_when_model_omits_it() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_waive",
            next_action="pass_with_notes",
            issues=["整体通过，但保留场景记忆提示。"],
            carry_forward_note=False,
        ),
    )

    assert report.carry_forward_note is True
    assert report.note_scope == "scene_memory"
    assert report.carry_note_text == "整体通过，但保留场景记忆提示。"
