"""场景生成 · 长度带与分段补丁（services/scene_generation/length_policy.py、segment_patch.py）：修复的长度窗口、
最近安全边界、逐段插入 / 替换补丁与受保护的结尾段（X04-19，自 test_scene_generation 拆出）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.services.llm_client import LLMResponse
from novel_system.services.scene_generation import (
    LengthPolicy,
    _apply_style_length_patch,
    _apply_style_salvage_patch,
    _neutral_length_instruction,
    _style_repair_length_instruction,
)


def test_neutral_repair_keeps_an_already_valid_source_in_a_local_length_window() -> None:
    scene = SimpleNamespace(target_length_band="700-1350 Chinese characters")

    instruction = _neutral_length_instruction(
        LengthPolicy.plain(scene),
        previous_length=900,
        retry=True,
    )

    assert "previous length already passed" in instruction
    assert "within 810-990 visible characters" in instruction
    assert "smallest localized edits" in instruction


def test_style_salvage_patch_rejects_protected_ending_segment() -> None:
    source = "\n".join(("甲" * 70, "乙" * 70, "丙" * 50))
    payload = {"edits": [{"segment_id": "S003", "new_text": "丁" * 50}]}
    response = LLMResponse(
        request_id="resp_salvage_protected_ending",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    with pytest.raises(Exception, match="segment_id_not_editable"):
        _apply_style_salvage_patch(
            source_content=source,
            response=response,
            lengths=LengthPolicy(band="120-260 Chinese characters"),
            llm_call_id="llm_salvage_protected_ending",
        )


@pytest.mark.parametrize(
    ("target_length_band", "source_length", "expected"),
    [
        (
            "700-1350 Chinese characters",
            642,
            ("add 108-188 visible characters", "narrow 750-830"),
        ),
        (
            "650-1250 Chinese characters",
            1500,
            ("remove 300-380 visible characters", "narrow 1120-1200"),
        ),
    ],
)
def test_style_repair_length_guard_targets_nearest_safe_boundary(
    target_length_band: str,
    source_length: int,
    expected: tuple[str, str],
) -> None:
    scene = SimpleNamespace(target_length_band=target_length_band)

    instruction = _style_repair_length_instruction(
        LengthPolicy.plain(scene),
        source_length=source_length,
    )

    assert expected[0] in instruction
    assert expected[1] in instruction


def test_exact_style_length_patch_applies_non_overlapping_expansion() -> None:
    source = "甲" * 40 + "。\n" + "乙" * 40 + "。\n" + "己" * 8
    payload = {
        "edits": [
            {
                "segment_id": "S002",
                "new_text": "庚" * 30,
            }
        ]
    }
    response = LLMResponse(
        request_id="resp_patch_expand",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    patched, audit = _apply_style_length_patch(
        source_content=source,
        response=response,
        lengths=LengthPolicy(band="100-200 Chinese characters"),
        llm_call_id="llm_patch_expand",
    )

    assert sum(not char.isspace() for char in patched) == 120
    assert patched == "甲" * 40 + "。\n" + "乙" * 40 + "。" + "庚" * 30 + "\n" + "己" * 8
    assert audit["valid"] is True
    assert audit["mode"] == "expand"
    assert audit["visible_delta"] == 30


def test_exact_style_length_patch_uses_segment_id_to_disambiguate_repeated_span() -> None:
    repeated = "可删片段" * 10
    source = "\n".join(("甲" * 30, repeated, "乙" * 20, repeated, "丙" * 30))
    payload = {
        "edits": [
            {
                "segment_id": "S004",
                "new_text": "",
            }
        ]
    }
    response = LLMResponse(
        request_id="resp_patch_disambiguated_compress",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    patched, audit = _apply_style_length_patch(
        source_content=source,
        response=response,
        lengths=LengthPolicy(band="100-130 Chinese characters"),
        llm_call_id="llm_patch_disambiguated_compress",
    )

    assert patched == "\n".join(("甲" * 30, repeated, "乙" * 20, "", "丙" * 30))
    assert audit["valid"] is True
    assert audit["mode"] == "compress"
    assert audit["deterministic_segment_address_validation"] is True


def test_exact_style_length_patch_selects_safe_subset_of_oversized_insertions() -> None:
    source = "\n".join(("甲" * 200, "乙" * 220, "丙" * 222))
    payload = {
        "edits": [
            {"segment_id": "S001", "new_text": "丁" * 340},
            {"segment_id": "S002", "new_text": "戊" * 391},
        ]
    }
    response = LLMResponse(
        request_id="resp_patch_subset_expand",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    patched, audit = _apply_style_length_patch(
        source_content=source,
        response=response,
        lengths=LengthPolicy(band="700-1350 Chinese characters"),
        llm_call_id="llm_patch_subset_expand",
    )

    assert sum(not char.isspace() for char in patched) == 982
    assert audit["submitted_edit_count"] == 2
    assert audit["edit_count"] == 1
    assert audit["omitted_edit_count"] == 1
    assert audit["segment_ids"] == ["S001"]
