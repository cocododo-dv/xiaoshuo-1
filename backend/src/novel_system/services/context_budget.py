from __future__ import annotations

import copy
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from novel_system.services.hash_engine import normalize


TOKEN_ESTIMATOR_VERSION = "cjk_aware_conservative_v1"
STYLE_OBSERVATION_COMPRESSED_TOKENS = 48
CONTINUITY_DIGEST_COMPRESSED_TOKENS = 24
# v2（W5）：前文声音锚是软性延续信号，预算紧张时先于任何事实 section 被压缩——
# 保留尾部（离本场最近的节拍），再不够就整段省略；scene_card 永不因它被压。
VOICE_ANCHOR_COMPRESSED_TOKENS = 160
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?…]+[”’」』）)]*")


@dataclass(slots=True)
class PromptSection:
    name: str
    label: str
    text: str
    status: str = "included"
    compressed_text: str | None = None

    @property
    def effective_text(self) -> str:
        if self.status == "compressed" and self.compressed_text is not None:
            return self.compressed_text
        return self.text


SECTION_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("chapter_goal", "Chapter Goal", ("chapter_goal",)),
    ("scene_card", "Scene Card", ("scene_card",)),
    # 2026-09-13 阶段 A：雪花 / 章节编排的场景结构（形态、POV、坩埚、三拍、代价）。与
    # scene_card 同级的事实 section——不进任何压缩 / 省略名单（见 scene_structure_brief.py）。
    ("scene_structure_brief", "Scene Structure (Snowflake)", ("scene_structure_brief",)),
    # 2026-09-13 阶段 F：已确认的雪花设计背景（一句话 / 五句脊柱 / 道德前提 / 章位置 / POV 角色摘要 /
    # 视角故事 / 相邻两场）。背景而非事实：预算紧时先压成要点、最后整段省略；硬 QC 类任务不看。
    ("scene_design_context", "Scene Design Context (Snowflake)", ("scene_design_context",)),
    ("chapter_writer_brief", "Chapter Writer Brief", ("chapter_writer_brief",)),
    ("scene_writer_brief", "Scene Writer Brief", ("scene_writer_brief",)),
    ("author_instruction", "Author Instruction", ("author_instruction",)),
    ("scene_blueprint", "Scene Literary Blueprint", ("scene_blueprint",)),
    ("character_pressure", "Character Pressure Blueprint", ("character_pressure", "character_pressure_blueprint")),
    ("chapter_story_architecture", "Chapter Story Architecture", ("chapter_story_architecture",)),
    ("character_contract", "Character Continuity Contract", ("character_contract",)),
    ("narrative_state", "Authoritative Character State (Event Log)", ("narrative_state",)),
    ("information_asymmetry", "Information Asymmetry (who knows what)", ("information_asymmetry",)),
    ("pov_voice", "POV Voice", ("voice_card",)),
    ("author_preference_profile", "Author Preference Profile", ("author_preference_profile",)),
    ("literary_freshness_budget", "Literary Freshness Budget", ("literary_freshness_budget",)),
    # 2026-09 风格模仿 v2（W5，规格 §1.3）：三个新 section。叙事机制块是 neutral_draft
    # 唯一可见的风格参考块；前文声音锚 / 漂移校准只对 style_draft 可见。
    ("style_narrative_guidance", "Style Reference — Narrative Mechanisms", ("style_narrative_guidance",)),
    ("chapter_transition_buffer", "Chapter Transition Buffer", ("chapter_transition_buffer",)),
    (
        "previous_scene_voice_anchor",
        "Previous Scene Voice Anchor (own prose; keep the same voice)",
        ("previous_scene_voice_anchor",),
    ),
    ("style_drift_calibration", "Style Drift Calibration", ("style_drift_calibration",)),
    ("similar_scene_context", "Similar Scene Context", ("similar_scene", "similar_scene_context")),
    ("relation_digest", "Relation Digest", ("relation_card", "relation_digest")),
    ("scene_memory_digest", "Previous Scene Memory", ("scene_memory", "scene_memory_digest")),
    ("scene_summary", "Scene Summary", ("scene_summary",)),
    ("chapter_summary", "Chapter Summary", ("chapter_summary",)),
    ("volume_summary", "Volume Summary (atmosphere only)", ("volume_summary",)),
    ("avoid_recent_expressions", "Avoid Recent Expressions", ("avoid_recent_expressions",)),
)

CONTINUITY_DROP_ORDER: tuple[str, ...] = (
    "relation_digest",
    "world_rules",
    "scene_memory_digest",
)

# neutral_draft 只负责事件、因果与连续性骨架。下面这些 section 都会把
# 目标风格提前施加到结构草稿上，既与“Keep the prose neutral”冲突，也让后续
# style_draft 的风格增益无法被单独衡量。人物 POV/voice contract 不在这里：它们
# 属于角色身份与连续性约束，而不是被模仿作品的目标文风。
NEUTRAL_DRAFT_STYLE_SECTIONS: tuple[str, ...] = (
    "style_profile",
    "author_preference_profile",
    "style_rules",
    "banned_rules",
    "style_observations",
    "narrative_patterns",
    "calibration_lines",
    # v2（规格 §1.3）：前文声音锚与漂移校准都是目标文风的延续信号，中性稿不看；
    # style_narrative_guidance 刻意不在此列——叙事取舍机制正是中性稿要吸收的。
    "previous_scene_voice_anchor",
    "style_drift_calibration",
)

CONTINUITY_POLICY: list[str] = [
    "drop_similar_scene_context",
    "drop_raw_style_rules_when_style_profile_exists",
    "compress_style_observations",
    "drop_calibration_lines",
    "drop_relation_world_memory_digests",
    "split_scene_recommendation",
]

TASK_KIND_POLICIES: dict[str, list[str]] = {
    "default": list(CONTINUITY_POLICY),
    "drafting": [
        "preserve_author_instruction",
        "preserve_style_profile_author_preference_and_calibration",
        *CONTINUITY_POLICY,
    ],
    "neutral_draft": [
        "omit_target_style_context_until_style_pass",
        "preserve_character_identity_and_continuity_context",
        *CONTINUITY_POLICY,
    ],
    "hard_qc": [
        "preserve_facts_constraints_and_character_contract",
        "drop_style_context_before_fact_context",
        *CONTINUITY_POLICY,
    ],
    "chapter_review": [
        "preserve_chapter_promise_payoff_and_memory",
        *CONTINUITY_POLICY,
    ],
}


def collect_prompt_sections(bundle_snapshot: Mapping[str, Any]) -> list[PromptSection]:
    inline_digests = bundle_snapshot.get("inline_digests")
    if not isinstance(inline_digests, Mapping):
        return []

    sections: list[PromptSection] = []
    for name, label, digest_keys in SECTION_SPECS:
        text = _first_text(inline_digests, digest_keys)
        if text is None:
            continue
        sections.append(PromptSection(name=name, label=label, text=text))
    return sections


def apply_context_budget(
    *,
    system_prompt: str,
    task_prompt: str,
    bundle_snapshot: Mapping[str, Any],
    sections: list[PromptSection],
    max_input_tokens: int,
    task_kind: str = "default",
) -> dict[str, Any]:
    normalized_task_kind = task_kind if task_kind in TASK_KIND_POLICIES else "default"
    budget = {
        "target_input_tokens": max_input_tokens,
        "token_estimator": TOKEN_ESTIMATOR_VERSION,
        "task_kind": normalized_task_kind,
        "estimated_input_tokens": 0,
        "remaining_input_tokens": 0,
        "included_sections": [],
        "compressed_sections": [],
        "omitted_sections": [],
        "section_status": {},
        "continuity_policy": list(TASK_KIND_POLICIES[normalized_task_kind]),
        "split_scene_recommended": False,
        "stop_reason": None,
        "continuity_warning": None,
    }
    section_lookup = {section.name: section for section in sections}

    if normalized_task_kind == "neutral_draft":
        for section_name in NEUTRAL_DRAFT_STYLE_SECTIONS:
            _omit_section(section_lookup, section_name)
    if normalized_task_kind == "hard_qc":
        # 硬 QC 只审事实与硬约束；设计背景（前提、价值观、相邻场）会诱使它核对本场之外的东西。
        _omit_section(section_lookup, "scene_design_context")

    if _rendered_prompt_tokens(
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        bundle_snapshot=bundle_snapshot,
        sections=sections,
        split_scene_recommended=False,
    ) > max_input_tokens:
        similar_scene = section_lookup.get("similar_scene_context")
        if similar_scene is not None:
            similar_scene.status = "omitted"

        # v2：前文声音锚先被压成尾部片段（比任何事实 section 都先让路）。
        if _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            voice_anchor = section_lookup.get("previous_scene_voice_anchor")
            if voice_anchor is not None and voice_anchor.status == "included":
                _apply_compressed_text(
                    voice_anchor, _compress_voice_anchor(voice_anchor.text)
                )

        # 阶段 F：设计背景紧随其后压成要点（视角故事 / 五句脊柱 / 在场人物先让路），
        # 仍先于任何事实 section；整段省略排在最后一轮（见下）。
        if _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            design_context = section_lookup.get("scene_design_context")
            if design_context is not None and design_context.status == "included":
                _apply_compressed_text(design_context, _compress_design_context(design_context.text))

        if normalized_task_kind == "hard_qc" and _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            _omit_section(section_lookup, "style_rules")

        if _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            style_rules = section_lookup.get("style_rules")
            style_profile = section_lookup.get("style_profile")
            if style_rules is not None and style_profile is not None:
                style_rules.status = "omitted"

        if _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            style_observations = section_lookup.get("style_observations")
            if style_observations is not None:
                style_observations.compressed_text = _compress_style_observations(style_observations.text)
                style_observations.status = "compressed"

        if _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            calibration_lines = section_lookup.get("calibration_lines")
            if calibration_lines is not None:
                _apply_compressed_text(calibration_lines, _compress_calibration_lines(calibration_lines.text))

        for section_name in CONTINUITY_DROP_ORDER:
            if _rendered_prompt_tokens(
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                bundle_snapshot=bundle_snapshot,
                sections=sections,
                split_scene_recommended=False,
            ) <= max_input_tokens:
                break
            section = section_lookup.get(section_name)
            if section is not None:
                _apply_compressed_text(section, _compress_continuity_digest(section.text))

        # v2：连续性摘要都压过仍超预算 → 整段省略声音锚 / 漂移校准（软性延续信号），
        # 再走拆场建议；scene_card 等事实 section 从不被动。
        for section_name in ("previous_scene_voice_anchor", "style_drift_calibration", "scene_design_context"):
            if _rendered_prompt_tokens(
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                bundle_snapshot=bundle_snapshot,
                sections=sections,
                split_scene_recommended=False,
            ) <= max_input_tokens:
                break
            _omit_section(section_lookup, section_name)

        if _rendered_prompt_tokens(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=bundle_snapshot,
            sections=sections,
            split_scene_recommended=False,
        ) > max_input_tokens:
            budget["split_scene_recommended"] = True
            budget["stop_reason"] = "split_scene_recommended"

    _finalize_budget(
        budget=budget,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        bundle_snapshot=bundle_snapshot,
        sections=sections,
    )
    budget["continuity_warning"] = _build_continuity_warning(budget)
    user_prompt = render_user_prompt(
        task_prompt=task_prompt,
        bundle_snapshot=bundle_snapshot,
        sections=sections,
        budget=budget,
    )
    return {
        "budget": budget,
        "user_prompt": user_prompt,
        "continuity_warning": budget["continuity_warning"],
    }


def finalize_request_budget(
    *,
    system_prompt: str,
    user_prompt: str,
    base_budget: Mapping[str, Any],
) -> dict[str, Any]:
    budget = copy.deepcopy(dict(base_budget))
    budget["estimated_input_tokens"] = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
    budget["remaining_input_tokens"] = budget["target_input_tokens"] - budget["estimated_input_tokens"]
    if budget["estimated_input_tokens"] > budget["target_input_tokens"]:
        budget["split_scene_recommended"] = True
        budget["stop_reason"] = "split_scene_recommended"
    budget["continuity_warning"] = _build_continuity_warning(budget)
    return {
        "budget": budget,
        "continuity_warning": budget["continuity_warning"],
    }


def render_user_prompt(
    *,
    task_prompt: str,
    bundle_snapshot: Mapping[str, Any],
    sections: list[PromptSection],
    budget: Mapping[str, Any],
) -> str:
    prompt_parts = [
        task_prompt,
        "",
        f"Scene ID: {bundle_snapshot.get('scene_id', '')}",
        f"Chapter ID: {bundle_snapshot.get('chapter_id', '')}",
        f"Bundle Contract: {bundle_snapshot.get('contract_version', '')}",
        f"Stage Allowlist: {bundle_snapshot.get('stage_allowlist_name', '')}",
    ]
    if budget.get("split_scene_recommended"):
        prompt_parts.extend(
            [
                "",
                "Split-scene recommendation: continuity still exceeds the configured prompt budget after deterministic compaction.",
            ]
        )

    for section in sections:
        if section.status == "omitted":
            continue
        heading = section.label
        if section.status == "compressed":
            heading = f"{heading} (compressed)"
        prompt_parts.extend(["", f"## {heading}", section.effective_text])

    prompt_parts.extend(["", "Return JSON that matches the structured schema exactly."])
    return "\n".join(prompt_parts).strip()


def estimate_tokens(text: str) -> int:
    """Return a conservative, deterministic prompt-token estimate.

    The previous ``len(text) / 4`` rule is a reasonable rough estimate for
    English, but it under-counts Chinese/Japanese/Korean text by roughly four
    times.  East-Asian wide characters (including CJK punctuation and most
    emoji) are therefore charged as one token each, while the remaining text
    keeps the established four-characters-per-token approximation.

    This is deliberately a budgeting upper bound rather than a claim about a
    provider's exact tokenizer.  Provider-reported usage remains authoritative
    for accounting after the request completes.
    """
    normalized_text = _normalize_text(text)
    if not normalized_text:
        return 0
    wide_count = sum(1 for char in normalized_text if _is_wide_token_char(char))
    compact_count = len(normalized_text) - wide_count
    return max(1, wide_count + math.ceil(compact_count / 4))


def _finalize_budget(
    *,
    budget: dict[str, Any],
    system_prompt: str,
    task_prompt: str,
    bundle_snapshot: Mapping[str, Any],
    sections: list[PromptSection],
) -> None:
    budget["included_sections"] = []
    budget["compressed_sections"] = []
    budget["omitted_sections"] = []
    budget["section_status"] = {}
    budget["estimated_input_tokens"] = _rendered_prompt_tokens(
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        bundle_snapshot=bundle_snapshot,
        sections=sections,
        split_scene_recommended=bool(budget["split_scene_recommended"]),
    )
    budget["remaining_input_tokens"] = budget["target_input_tokens"] - budget["estimated_input_tokens"]
    for section in sections:
        budget["section_status"][section.name] = {
            "label": section.label,
            "status": section.status,
            "estimated_tokens": estimate_tokens(section.effective_text) if section.status != "omitted" else 0,
        }
        if section.status == "included":
            budget["included_sections"].append(section.name)
        elif section.status == "compressed":
            budget["compressed_sections"].append(section.name)
        elif section.status == "omitted":
            budget["omitted_sections"].append(section.name)


def _build_continuity_warning(budget: Mapping[str, Any]) -> dict[str, Any] | None:
    if not budget.get("split_scene_recommended"):
        return None
    return {
        "code": "continuity_budget_exceeded",
        "message": "Prompt still exceeds the safe input budget after deterministic continuity compaction.",
        "recommended_action": "split_scene",
        "requires_scene_split": True,
        "compressed_sections": list(budget.get("compressed_sections", [])),
        "omitted_sections": list(budget.get("omitted_sections", [])),
        "estimated_input_tokens": int(budget.get("estimated_input_tokens", 0)),
        "target_input_tokens": int(budget.get("target_input_tokens", 0)),
    }


def _rendered_prompt_tokens(
    *,
    system_prompt: str,
    task_prompt: str,
    bundle_snapshot: Mapping[str, Any],
    sections: list[PromptSection],
    split_scene_recommended: bool,
) -> int:
    rendered_prompt = render_user_prompt(
        task_prompt=task_prompt,
        bundle_snapshot=bundle_snapshot,
        sections=sections,
        budget={"split_scene_recommended": split_scene_recommended},
    )
    return estimate_tokens(system_prompt) + estimate_tokens(rendered_prompt)


def _compress_style_observations(text: str) -> str:
    blocks = _split_blocks(text)
    candidate = "\n\n".join(blocks[:3]) if blocks else _normalize_text(text)
    return _truncate_to_estimated_tokens(
        candidate,
        max_tokens=STYLE_OBSERVATION_COMPRESSED_TOKENS,
    )


def _compress_voice_anchor(text: str) -> str:
    """保留声音锚的**尾部**（最靠近本场的节拍），并从句边界起头。"""
    normalized = _normalize_text(text)
    if not normalized or estimate_tokens(normalized) <= VOICE_ANCHOR_COMPRESSED_TOKENS:
        return normalized
    # 从尾部向前收，直到估算 token 落进上限。
    start = 0
    while start < len(normalized) and estimate_tokens(normalized[start:]) > VOICE_ANCHOR_COMPRESSED_TOKENS:
        start += max(1, (len(normalized) - start) // 8)
    tail = normalized[start:]
    match = _SENTENCE_BOUNDARY_RE.search(tail)
    if match is not None and match.end() < len(tail):
        tail = tail[match.end():]
    tail = tail.strip()
    return f"... {tail}" if tail else normalized[-1:]


def _compress_calibration_lines(text: str) -> str:
    blocks = _split_blocks(text)
    if len(blocks) <= 1:
        return _normalize_text(text)
    return blocks[0]


def _compress_continuity_digest(text: str) -> str:
    blocks = _split_blocks(text)
    candidate = blocks[0] if blocks else _normalize_text(text)
    return _truncate_to_estimated_tokens(
        candidate,
        max_tokens=CONTINUITY_DIGEST_COMPRESSED_TOKENS,
    )


# 设计背景压缩时保留的行首标签（背景里最省不掉的部分）；其余行（五句脊柱 / 视角故事 / 在场人物）先让路。
# 放在这里而不是 scene_design_context.py：那边要读 settings，settings → system_config → prompt_builder →
# 本模块，反向引用会成环。
DESIGN_CONTEXT_COMPRESS_KEEP_LABELS: tuple[str, ...] = (
    "Book logline:",
    "Narrative stance",
    "Moral premise:",
    "Chapter:",
    "Scene position:",
    "POV character sheet",
    "Previous scene",
    "Next scene",
)


def compress_design_context(text: str) -> str:
    """设计背景的压缩形态：只留一句话、道德前提、章位置、POV 摘要表与相邻两场。"""
    kept = [
        line
        for line in str(text or "").split("\n")
        if any(line.startswith(label) for label in DESIGN_CONTEXT_COMPRESS_KEEP_LABELS)
    ]
    return "\n".join(kept) if kept else str(text or "").split("\n")[0]


def _compress_design_context(text: str) -> str:
    return compress_design_context(text)


def _omit_section(section_lookup: Mapping[str, PromptSection], section_name: str) -> None:
    section = section_lookup.get(section_name)
    if section is not None:
        section.status = "omitted"


def _first_text(inline_digests: Mapping[str, Any], digest_keys: tuple[str, ...]) -> str | None:
    for digest_key in digest_keys:
        value = inline_digests.get(digest_key)
        if isinstance(value, str) and value.strip():
            return _normalize_text(value)
    return None


def _normalize_text(text: str) -> str:
    normalized = normalize(text)
    if not isinstance(normalized, str):
        raise ValueError("text must normalize to a string")
    return normalized


def _split_blocks(text: str) -> list[str]:
    return [block.strip() for block in _normalize_text(text).split("\n\n") if block.strip()]


def _apply_compressed_text(section: PromptSection, compressed_text: str) -> None:
    if _normalize_text(compressed_text) == _normalize_text(section.text):
        return
    section.compressed_text = compressed_text
    section.status = "compressed"


def _is_wide_token_char(char: str) -> bool:
    """Whether a character should be budgeted as an approximately whole token."""
    if not char or char.isspace():
        return False
    return unicodedata.east_asian_width(char) in {"W", "F"}


def _truncate_to_estimated_tokens(text: str, *, max_tokens: int) -> str:
    """Truncate mixed-language text without relying on whitespace tokenization."""
    normalized = _normalize_text(text)
    if not normalized or estimate_tokens(normalized) <= max_tokens:
        return normalized

    wide_units = 0
    compact_units = 0
    end = 0
    for index, char in enumerate(normalized):
        next_wide = wide_units + (1 if _is_wide_token_char(char) else 0)
        next_compact = compact_units + (0 if _is_wide_token_char(char) else 1)
        estimated = next_wide + math.ceil(next_compact / 4)
        if estimated > max_tokens:
            break
        wide_units = next_wide
        compact_units = next_compact
        end = index + 1

    clipped = normalized[:end].rstrip()
    return f"{clipped} ..." if clipped else "..."
