from __future__ import annotations

import pytest

from novel_system.services.qc_validator import QCValidationError, validate_qc_report


def test_soft_qc_waive_requires_carry_note() -> None:
    with pytest.raises(QCValidationError):
        validate_qc_report(
            "soft_qc",
            {
                "resolution_code": "soft_waive",
                "pass_flag": True,
                "next_action": "pass_with_notes",
                "issues": [],
            },
        )


def test_hard_qc_accepts_valid_combo() -> None:
    report = validate_qc_report(
        "hard_qc",
        {
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [],
            "rewrite_brief": [],
        },
    )

    assert report.resolution_code == "hard_pass"


def test_qc_validator_normalizes_string_rewrite_brief_from_local_models() -> None:
    hard_report = validate_qc_report(
        "hard_qc",
        {
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [],
            "rewrite_brief": "",
        },
    )
    soft_report = validate_qc_report(
        "soft_qc",
        {
            "resolution_code": "soft_patch",
            "pass_flag": False,
            "next_action": "patch",
            "issues": [{"issue_key": "cadence_flat", "message": "Needs sharper rhythm."}],
            "rewrite_brief": "Tighten the first paragraph.",
        },
    )

    assert hard_report.rewrite_brief == []
    assert soft_report.rewrite_brief == ["Tighten the first paragraph."]


def test_qc_validator_normalizes_soft_issue_aliases_from_local_models() -> None:
    report = validate_qc_report(
        "soft_qc",
        {
            "resolution_code": "soft_patch",
            "pass_flag": False,
            "next_action": "patch",
            "issues": [
                {"type": "style_drift", "description": "The draft drifts from the target profile."},
                {"issue_key": "imagery_balance", "message": "Imagery needs a clearer emotional edge."},
            ],
            "rewrite_brief": ["Sharpen the profile-specific imagery."],
        },
    )

    assert [issue.issue_key for issue in report.issues] == ["style_drift", "imagery_balance"]
    assert report.issues[0].message == "The draft drifts from the target profile."


def test_qc_validator_drops_extra_issue_location_from_local_models() -> None:
    report = validate_qc_report(
        "soft_qc",
        {
            "resolution_code": "soft_patch",
            "pass_flag": False,
            "next_action": "patch",
            "issues": [
                {
                    "issue_key": "required_phrase",
                    "message": "The required phrase is not exact.",
                    "location": "Scene Card: required text",
                }
            ],
            "rewrite_brief": ["Restore the required phrase exactly."],
        },
    )

    assert report.issues[0].issue_key == "required_phrase"
    assert report.issues[0].message == "The required phrase is not exact."


def test_qc_validator_derives_soft_pass_flag_from_resolution_and_action() -> None:
    report = validate_qc_report(
        "soft_qc",
        {
            "resolution_code": "soft_pass",
            "pass_flag": False,
            "next_action": "pass",
            "issues": [],
            "rewrite_brief": [],
        },
    )

    assert report.pass_flag is True


def test_qc_validator_normalizes_single_style_deviation_from_local_models() -> None:
    report = validate_qc_report(
        "soft_qc",
        {
            "resolution_code": "soft_patch",
            "pass_flag": False,
            "next_action": "patch",
            "issues": [{"issue_key": "dialogue_ratio", "message": "Dialogue is too direct."}],
            "rewrite_brief": ["Make dialogue sparse and move pressure into gesture."],
            "style_deviations": {
                "patch_brief": "Adjust dialogue to be sparse and grounded in gesture.",
                "severity": "medium",
            },
        },
    )

    assert len(report.style_deviations) == 1
    assert report.style_deviations[0].dimension == "style"
    assert report.style_deviations[0].patch_brief == "Adjust dialogue to be sparse and grounded in gesture."


def test_qc_validator_normalizes_flattened_style_deviation_keys_from_local_models() -> None:
    report = validate_qc_report(
        "soft_qc",
        {
            "resolution_code": "soft_patch",
            "pass_flag": False,
            "next_action": "patch",
            "issues": [{"issue_key": "dialogue_ratio", "message": "Dialogue is too direct."}],
            "rewrite_brief": ["Make dialogue sparse and move pressure into gesture."],
            "style_deviations.patch_brief": "Adjust dialogue to be sparse and grounded in gesture.",
            "style_deviations.severity": "medium",
        },
    )

    assert len(report.style_deviations) == 1
    assert report.style_deviations[0].severity == "medium"
    assert report.style_deviations[0].patch_brief == "Adjust dialogue to be sparse and grounded in gesture."


def test_legacy_style_scores_alias_is_scaled_before_validation() -> None:
    """B04-18：本地模型给的旧别名 ``style_scores`` 是 0–10 分——节点出口先把它摊开、按模板声明的刻度换算，
    校验才收得下（以前别名在校验里才摊开，错过换算，7.5 超出 0–1 → 整遍软 QC 被判 invalid 豁免）。"""
    from novel_system.services.qc_engine import _normalize_soft_qc_scores

    schema = {"properties": {"style_score": {"type": "number", "minimum": 0, "maximum": 10}}}
    payload = {
        "resolution_code": "soft_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
        "style_scores": {"rhythm": 8, "voice": 7},
    }

    report = validate_qc_report("soft_qc", _normalize_soft_qc_scores(payload, schema=schema))

    assert report.style_score == 0.75
    assert [(item.name, item.score) for item in report.style_dimensions] == [("rhythm", 0.8), ("voice", 0.7)]
