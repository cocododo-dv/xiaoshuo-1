"""编号分段补丁：长度补丁与风格救稿都不让模型整篇重写，而是由程序给原文分段编号、模型只提交
``segment_id + new_text``，原文定位与套用不依赖模型复制原文的精度（整篇「修长度」在真实模型上会稳定退化成摘要）。

这里是分段、可编辑段的挑选、schema 收紧、套用与验证（有任何歧义整批拒绝），以及两份补丁契约的指令。
"""

from __future__ import annotations

import math
import re
from typing import Any, Sequence

from novel_system.db.models import SceneCard
from novel_system.services.hash_engine import sha256_json_normalized
from novel_system.services.llm_client import LLMResponse
from novel_system.services.qc_constraints import constraint_terms
from novel_system.services.scene_generation.contracts import SceneGenerationPostprocessError
from novel_system.services.scene_generation.length_policy import LengthPolicy, _style_repair_working_window
from novel_system.services.scene_generation.text_gates import (
    _normalize_literal_unicode_escapes,
    _visible_char_count,
)


def _style_length_patch_segments(source_content: str) -> list[dict[str, Any]]:
    """将正文切成稳定的可定位段；最后一段由调用方固定保护。"""

    line_spans = [
        (match.start(), match.end())
        for match in re.finditer(r"[^\r\n]*\S[^\r\n]*", source_content)
    ]
    spans = line_spans
    if len(line_spans) == 1:
        line_start, line_end = line_spans[0]
        line_text = source_content[line_start:line_end]
        sentence_spans = [
            (line_start + match.start(), line_start + match.end())
            for match in re.finditer(
                r".+?(?:[。！？!?]”?|。?$)",
                line_text,
            )
            if match.group(0).strip()
        ]
        if len(sentence_spans) >= 2:
            spans = sentence_spans
    return [
        {
            "segment_id": f"S{index:03d}",
            "start": start,
            "end": end,
            "visible_chars": _visible_char_count(source_content[start:end]),
        }
        for index, (start, end) in enumerate(spans, start=1)
    ]


def _style_length_patch_editable_segment_ids(
    source_content: str,
    lengths: LengthPolicy,
) -> list[str]:
    segments = _style_length_patch_segments(source_content)
    if len(segments) < 2:
        return []
    candidates = segments[:-1]
    length_range = lengths.hard_range()
    source_length = _visible_char_count(source_content)
    if length_range is None or source_length < length_range[0]:
        return [str(segment["segment_id"]) for segment in candidates]

    minimum, maximum = length_range
    _local_minimum, local_maximum, _target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    desired_reduction = max(1, source_length - local_maximum)
    minimum_useful_chars = max(24, min(80, math.ceil(desired_reduction / 6)))
    useful = [
        segment
        for segment in candidates
        if int(segment["visible_chars"]) >= minimum_useful_chars
    ]
    if not useful:
        useful = [max(candidates, key=lambda segment: int(segment["visible_chars"]))]
    return [str(segment["segment_id"]) for segment in useful]


def _style_salvage_editable_segment_ids(source_content: str) -> list[str]:
    segments = _style_length_patch_segments(source_content)
    if len(segments) < 2:
        return []
    source_length = max(1, _visible_char_count(source_content))
    candidates = [
        segment
        for segment in segments[:-1]
        if int(segment["visible_chars"]) >= 60
        and 0.10
        <= int(segment["visible_chars"]) / source_length
        <= 0.35
    ]
    if not candidates:
        candidates = [
            segment
            for segment in segments[:-1]
            if int(segment["visible_chars"]) >= 40
            and int(segment["visible_chars"]) / source_length <= 0.45
        ]
    candidates = sorted(
        candidates,
        key=lambda segment: (
            abs(int(segment["visible_chars"]) / source_length - 0.22),
            int(str(segment["segment_id"])[1:]),
        ),
    )[:4]
    candidate_ids = {str(segment["segment_id"]) for segment in candidates}
    return [
        str(segment["segment_id"])
        for segment in segments
        if str(segment["segment_id"]) in candidate_ids
    ]


def _annotate_style_length_patch_source(
    source_content: str,
    *,
    editable_segment_ids: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    segments = _style_length_patch_segments(source_content)
    if not segments:
        return source_content, []
    editable_ids = (
        [str(value) for value in editable_segment_ids]
        if editable_segment_ids is not None
        else [str(segment["segment_id"]) for segment in segments[:-1]]
    )
    editable_set = set(editable_ids)
    parts: list[str] = []
    cursor = 0
    for index, segment in enumerate(segments):
        start = int(segment["start"])
        end = int(segment["end"])
        segment_id = str(segment["segment_id"])
        parts.append(source_content[cursor:start])
        marker = (
            f"⟦{segment_id}:PROTECTED_ENDING⟧"
            if index == len(segments) - 1
            else (
                f"⟦{segment_id}⟧"
                if segment_id in editable_set
                else f"⟦{segment_id}:PROTECTED⟧"
            )
        )
        parts.append(marker)
        parts.append(source_content[start:end])
        cursor = end
    parts.append(source_content[cursor:])
    return "".join(parts), editable_ids


def _constrain_style_length_patch_schema(
    prompt: dict[str, Any],
    *,
    editable_segment_ids: Sequence[str],
    lengths: LengthPolicy,
    source_length: int,
) -> None:
    """把本次可编辑 ID 收紧为 JSON Schema enum，并刷新审计 hash。"""

    schema = prompt.get("structured_schema")
    try:
        edits_schema = schema["properties"]["edits"]
        item_schema = edits_schema["items"]
        properties = item_schema["properties"]
        segment_schema = properties["segment_id"]
        new_text_schema = properties["new_text"]
    except (KeyError, TypeError):
        return
    if editable_segment_ids:
        segment_schema["enum"] = list(editable_segment_ids)
        edits_schema["maxItems"] = min(6, len(editable_segment_ids))
    length_range = lengths.hard_range()
    if length_range is not None and source_length < length_range[0]:
        local_minimum, local_maximum, _target = _style_repair_working_window(
            *length_range,
            source_length=source_length,
        )
        minimum_delta = max(1, local_minimum - source_length)
        maximum_delta = max(minimum_delta, local_maximum - source_length)
        item_count = min(
            len(editable_segment_ids),
            6,
            max(1, math.ceil(minimum_delta / 240)),
        )
        if item_count > 0:
            edits_schema["minItems"] = item_count
            edits_schema["maxItems"] = item_count
            new_text_schema["minLength"] = math.ceil(
                minimum_delta / item_count
            )
            new_text_schema["maxLength"] = max(
                new_text_schema["minLength"],
                maximum_delta // item_count,
            )
            new_text_schema["description"] = (
                f"One of exactly {item_count} insertions; all insertions together "
                f"must add {minimum_delta}-{maximum_delta} visible characters."
            )
    prompt["prompt_hash"] = sha256_json_normalized(
        {
            "template_name": prompt.get("template_name"),
            "template_version": prompt.get("template_version"),
            "system_prompt": prompt.get("system_prompt"),
            "user_prompt": prompt.get("user_prompt"),
            "structured_schema": schema,
        }
    )


def _constrain_style_salvage_schema(
    prompt: dict[str, Any],
    *,
    editable_segment_ids: Sequence[str],
) -> None:
    schema = prompt.get("structured_schema")
    try:
        edits_schema = schema["properties"]["edits"]
        segment_schema = edits_schema["items"]["properties"]["segment_id"]
    except (KeyError, TypeError):
        return
    if editable_segment_ids:
        segment_schema["enum"] = list(editable_segment_ids)
    edits_schema["minItems"] = 1
    edits_schema["maxItems"] = 1
    prompt["prompt_hash"] = sha256_json_normalized(
        {
            "template_name": prompt.get("template_name"),
            "template_version": prompt.get("template_version"),
            "system_prompt": prompt.get("system_prompt"),
            "user_prompt": prompt.get("user_prompt"),
            "structured_schema": schema,
        }
    )


def _apply_style_length_patch(
    *,
    source_content: str,
    response: LLMResponse,
    lengths: LengthPolicy,
    llm_call_id: str,
) -> tuple[str, dict[str, Any]]:
    """验证并套用模型提交的分段编号 replacement；任何歧义都整批拒绝。"""

    def reject(reason: str) -> None:
        raise SceneGenerationPostprocessError(
            llm_call_id=llm_call_id,
            message=f"style length patch invalid: {reason}",
        )

    length_range = lengths.hard_range()
    if length_range is None:
        reject("target_length_range_unavailable")
    assert length_range is not None
    minimum, maximum = length_range
    source_length = _visible_char_count(source_content)
    if minimum <= source_length <= maximum:
        reject("source_length_already_valid")
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    direction = "expand" if source_length < minimum else "compress"

    payload = response.structured_output or {}
    raw_edits = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(raw_edits, list) or not 1 <= len(raw_edits) <= 6:
        reject("edit_count_invalid")

    segments = _style_length_patch_segments(source_content)
    editable_segment_ids = _style_length_patch_editable_segment_ids(
        source_content,
        lengths,
    )
    editable_segments = {
        segment["segment_id"]: segment
        for segment in segments
        if segment["segment_id"] in editable_segment_ids
    }
    if not editable_segments:
        reject("editable_segments_unavailable")
    edits: list[tuple[int, int, str, int, int, str]] = []
    submitted_segment_ids: list[str] = []
    for raw in raw_edits:
        if not isinstance(raw, dict):
            reject("edit_shape_invalid")
        segment_id = raw.get("segment_id")
        new_text = raw.get("new_text")
        if not isinstance(segment_id, str) or not segment_id.strip():
            reject("segment_id_invalid")
        if not isinstance(new_text, str):
            reject("new_text_invalid")
        segment_id = segment_id.strip().upper()
        segment = editable_segments.get(segment_id)
        if segment is None:
            reject("segment_id_not_editable")
        if segment_id in submitted_segment_ids:
            reject("segment_id_repeated")
        new_text = _normalize_literal_unicode_escapes(new_text)
        if "⟦S" in new_text or "SEGMENT" in new_text.upper():
            reject("segment_marker_leaked_into_new_text")
        if direction == "expand":
            start = int(segment["end"])
            end = start
            old_chars = 0
        else:
            start = int(segment["start"])
            end = int(segment["end"])
            old_chars = int(segment["visible_chars"])
        new_chars = _visible_char_count(new_text)
        delta = new_chars - old_chars
        if direction == "expand":
            if delta <= 0:
                reject("expansion_insertion_must_add_text")
        elif delta >= 0:
            reject("compression_segment_replacement_must_be_shorter")
        submitted_segment_ids.append(segment_id)
        edits.append((start, end, new_text, delta, old_chars, segment_id))

    edits.sort(key=lambda item: item[0])
    if any(
        left[1] > right[0]
        or (left[0] == left[1] == right[0] == right[1])
        for left, right in zip(edits, edits[1:])
    ):
        reject("edits_overlap")
    scope_limit = max(600, source_length // 2)
    best_choice: tuple[
        tuple[int, int, int, tuple[str, ...]],
        list[tuple[int, int, str, int, int, str]],
    ] | None = None
    for mask in range(1, 1 << len(edits)):
        selected = [
            edit for index, edit in enumerate(edits) if mask & (1 << index)
        ]
        selected_old_chars = sum(edit[4] for edit in selected)
        selected_delta = sum(edit[3] for edit in selected)
        if max(selected_old_chars, abs(selected_delta)) > scope_limit:
            continue
        candidate_length = source_length + selected_delta
        if not minimum <= candidate_length <= maximum:
            continue
        score = (
            0 if local_minimum <= candidate_length <= local_maximum else 1,
            abs(candidate_length - target),
            len(selected),
            tuple(edit[5] for edit in selected),
        )
        if best_choice is None or score < best_choice[0]:
            best_choice = (score, selected)
    if best_choice is None:
        reject("no_safe_edit_subset_reaches_target_range")
    selected_edits = best_choice[1]
    total_old_chars = sum(edit[4] for edit in selected_edits)
    total_delta = sum(edit[3] for edit in selected_edits)
    applied_segment_ids = [edit[5] for edit in selected_edits]

    patched = source_content
    for start, end, new_text, _delta, _old_chars, _segment_id in reversed(
        selected_edits
    ):
        patched = patched[:start] + new_text + patched[end:]
    output_length = _visible_char_count(patched)
    if not minimum <= output_length <= maximum:
        reject("patched_length_outside_absolute_range")
    if output_length - source_length != total_delta:
        reject("visible_delta_mismatch")

    return patched, {
        "version": "style_length_patch_v3",
        "valid": True,
        "mode": direction,
        "edit_count": len(selected_edits),
        "submitted_edit_count": len(edits),
        "omitted_edit_count": len(edits) - len(selected_edits),
        "segment_ids": applied_segment_ids,
        "source_visible_chars": source_length,
        "patched_visible_chars": output_length,
        "visible_delta": total_delta,
        "absolute_range": [minimum, maximum],
        "correction_window": [local_minimum, local_maximum],
        "correction_window_hit": local_minimum <= output_length <= local_maximum,
        "correction_target": target,
        "edited_source_visible_chars": total_old_chars,
        "deterministic_segment_address_validation": True,
        "non_overlapping": True,
    }


def _apply_style_salvage_patch(
    *,
    source_content: str,
    response: LLMResponse,
    lengths: LengthPolicy,
    llm_call_id: str,
) -> tuple[str, dict[str, Any]]:
    """只替换一个预编号中段，保留中性安全稿的其余文字与结尾。"""

    def reject(reason: str) -> None:
        raise SceneGenerationPostprocessError(
            llm_call_id=llm_call_id,
            message=f"style salvage patch invalid: {reason}",
        )

    payload = response.structured_output or {}
    raw_edits = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(raw_edits, list) or len(raw_edits) != 1:
        reject("exactly_one_edit_required")
    raw = raw_edits[0]
    if not isinstance(raw, dict):
        reject("edit_shape_invalid")
    segment_id = raw.get("segment_id")
    new_text = raw.get("new_text")
    if not isinstance(segment_id, str) or not segment_id.strip():
        reject("segment_id_invalid")
    if not isinstance(new_text, str):
        reject("new_text_invalid")
    segment_id = segment_id.strip().upper()
    new_text = _normalize_literal_unicode_escapes(new_text).strip()
    editable_ids = _style_salvage_editable_segment_ids(source_content)
    segment_by_id = {
        str(segment["segment_id"]): segment
        for segment in _style_length_patch_segments(source_content)
    }
    segment = segment_by_id.get(segment_id)
    if segment_id not in editable_ids or segment is None:
        reject("segment_id_not_editable")
    if "⟦S" in new_text or "SEGMENT" in new_text.upper():
        reject("segment_marker_leaked_into_new_text")
    old_text = source_content[int(segment["start"]) : int(segment["end"])]
    old_chars = int(segment["visible_chars"])
    new_chars = _visible_char_count(new_text)
    minimum_chars = max(20, math.floor(old_chars * 0.50))
    maximum_chars = max(minimum_chars, math.ceil(old_chars * 1.35))
    if not minimum_chars <= new_chars <= maximum_chars:
        reject("replacement_length_outside_local_window")
    from novel_system.services.style_reference.validation.plagiarism import (
        normalize_text_for_matching,
    )

    if normalize_text_for_matching(old_text) == normalize_text_for_matching(new_text):
        reject("replacement_not_substantively_changed")
    start = int(segment["start"])
    end = int(segment["end"])
    patched = source_content[:start] + new_text + source_content[end:]
    length_range = lengths.hard_range()
    output_chars = _visible_char_count(patched)
    if length_range is not None and not length_range[0] <= output_chars <= length_range[1]:
        reject("patched_length_outside_absolute_range")
    return patched, {
        "version": "style_salvage_patch_v1",
        "valid": True,
        "segment_id": segment_id,
        "source_visible_chars": _visible_char_count(source_content),
        "old_segment_visible_chars": old_chars,
        "new_segment_visible_chars": new_chars,
        "patched_visible_chars": output_chars,
        "replacement_window": [minimum_chars, maximum_chars],
        "substantive_change": True,
        "protected_ending": True,
    }


def _requires_style_salvage(base_safety: dict[str, Any]) -> bool:
    """只有事实安全但极端过短的风格稿才改为局部风格挽救。"""

    if set(base_safety.get("reasons") or []) != {"target_length_not_met"}:
        return False
    length_range = base_safety.get("target_length_range")
    rewritten_length = base_safety.get("rewritten_visible_chars")
    if (
        not isinstance(length_range, list)
        or len(length_range) != 2
        or not isinstance(length_range[0], int)
        or not isinstance(rewritten_length, int)
    ):
        return False
    return rewritten_length < math.ceil(length_range[0] * 0.6)


def _style_length_patch_instruction(
    scene: SceneCard,
    *,
    lengths: LengthPolicy,
    source_length: int,
    editable_segment_ids: Sequence[str],
) -> str:
    length_range = lengths.hard_range()
    if length_range is None:
        return ""
    minimum, maximum = length_range
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    if source_length < minimum:
        direction = (
            f"Expansion only: the combined replacements must add "
            f"{local_minimum - source_length}-{local_maximum - source_length} visible characters. "
            "For each selected segment_id, new_text is inserted immediately after that immutable source segment."
        )
    else:
        direction = (
            f"Compression only: the combined replacements must remove "
            f"{source_length - local_maximum}-{source_length - local_minimum} visible characters. "
            "For each selected segment_id, new_text replaces that one source segment and must be shorter. "
            "Delete only repetition or decorative description; do not replace omitted text with an ellipsis."
        )
    required_terms = constraint_terms(scene.must_include_text or "")
    required_rule = (
        " Do not alter or remove any required constraint group: "
        + "；".join(required_terms)
        + "。"
        if required_terms
        else ""
    )
    return (
        "\n\n[Deterministic Local Length Patch Contract]\n"
        f"The immutable source has about {source_length} visible characters. The final text after applying all edits "
        f"must be {local_minimum}-{local_maximum}, aiming near {target}; the absolute scene range is "
        f"{minimum}-{maximum}. {direction} Editable segment IDs: "
        f"{', '.join(editable_segment_ids) if editable_segment_ids else '(none)'}. "
        "The final source segment marked PROTECTED_ENDING is forbidden. Segment markers are addresses and must "
        "never appear in new_text."
        f"{required_rule}"
        " Return edits only, never scene_text or the complete scene."
    )


def _style_salvage_instruction(
    scene: SceneCard,
    *,
    source_content: str,
    editable_segment_ids: Sequence[str],
) -> str:
    segments = {
        str(segment["segment_id"]): int(segment["visible_chars"])
        for segment in _style_length_patch_segments(source_content)
    }
    windows = []
    for segment_id in editable_segment_ids:
        visible_chars = segments.get(segment_id, 0)
        windows.append(
            f"{segment_id}={max(20, math.floor(visible_chars * 0.50))}-"
            f"{max(20, math.ceil(visible_chars * 1.35))} visible characters"
        )
    required_terms = constraint_terms(scene.must_include_text or "")
    required_rule = (
        " Preserve every required constraint group wherever it appears: "
        + "；".join(required_terms)
        + "。"
        if required_terms
        else ""
    )
    return (
        "\n\n[Deterministic Bounded Style Salvage Contract]\n"
        "Replace exactly one editable segment; all other source characters and the protected ending remain "
        "immutable. Allowed segment windows: "
        + ("; ".join(windows) if windows else "(none)")
        + ". Make a substantive lexical/syntactic rewrite using the injected reusable style mechanisms, not a "
        "punctuation-only or whitespace-only change."
        + required_rule
        + " Return edits only, never scene_text or the complete scene."
    )
