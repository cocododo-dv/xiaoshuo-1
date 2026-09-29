"""各步 user prompt 里的固定指令与修复简报：作者批注、风格通道的来源稿段、中性稿 / 安全修复 / 去模板简报。

修复简报把确定性失败（:class:`~.text_gates.ConstraintSnapshot` 读出来的缺项、禁用词、完整性标记与长度带）逐项翻成
一次有界修复，简报里不带正文。
"""

from __future__ import annotations

from typing import Any

from novel_system.db.models import SceneCard
from novel_system.services.author_instructions import render_author_note_instruction
from novel_system.services.scene_generation.length_policy import LengthPolicy, _style_repair_working_window
from novel_system.services.scene_generation.text_gates import ConstraintSnapshot


JSON_SCHEMA_INSTRUCTION = "Return JSON that matches the structured schema exactly."

# neutral_first 风格稿（重组中性稿）的来源稿指令；style_first 在入口就分流到风格步，走不到这条链。
_NEUTRAL_STYLE_INSTRUCTION = "Apply the style prompt template without changing the approved facts."


_STYLE_SAFETY_REPAIR_TASK_PROMPT = (
    "Edit the labeled rejected style draft directly. This is a local safety repair, not a new composition. "
    "Preserve its wording, paragraph architecture, reusable style, facts, chronology, and ending wherever they "
    "already pass; change only the exact hard-constraint failures listed below. Return one complete replacement "
    "scene_text and no commentary."
)
_STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT = (
    "Edit the labeled style draft directly. Apply only the listed de-template corrections while preserving its "
    "facts, chronology, functional paragraph architecture, broad style distribution, distinctive wording, and ending function. "
    "Do not restart the scene, recompose it from a blank page, or rewrite unaffected passages. Return one complete "
    "replacement scene_text and no commentary."
)


def author_note_instruction(author_note: str | None) -> str:
    """Backward-compatible renderer; bundle injection now carries it to every stage."""
    return render_author_note_instruction(author_note)


def _author_note_instruction_for_bundle(
    bundle: dict[str, Any],
    author_note: str | None,
) -> str:
    note = str(author_note or "").strip()
    frozen = str(
        ((bundle.get("snapshot") or {}).get("inline_digests") or {}).get(
            "author_instruction"
        )
        or ""
    )
    return "" if note == frozen else author_note_instruction(author_note)


def build_style_user_prompt(
    base_prompt: str,
    *,
    neutral_content: str,
    source_label: str,
    source_row_id: str,
    extra_instruction: str,
    patch_brief: list[str] | None = None,
    patch_heading: str = "Patch Brief",
) -> str:
    prompt_parts = [
        base_prompt,
        "",
        f"## {source_label}",
        neutral_content,
        "",
        f"Source Draft Row ID: {source_row_id}",
        extra_instruction,
    ]
    if patch_brief:
        prompt_parts.extend(
            [
                "",
                f"## {patch_heading}",
                "\n".join(f"- {item}" for item in patch_brief),
            ]
        )
    if JSON_SCHEMA_INSTRUCTION not in base_prompt:
        prompt_parts.extend(["", JSON_SCHEMA_INSTRUCTION])
    return "\n".join(prompt_parts).strip()


def _neutral_repair_brief(
    scene: SceneCard,
    *,
    source_content: str,
    assessment: dict[str, Any],
) -> str:
    """把中性稿的确定性失败逐项翻译成一次有界修复，不再误称为长度重试。"""

    snapshot = ConstraintSnapshot.read(scene, source_content, None)
    missing_terms = snapshot.missing_required
    forbidden_hits = snapshot.forbidden_hits
    integrity_markers = snapshot.integrity_markers
    lines = [
        "This is the only deterministic repair attempt. Edit the labeled draft directly and return one complete replacement scene only.",
        "Preserve every already-correct fact, causal step, character identity, chronology, and ending function.",
    ]
    if missing_terms:
        lines.append(
            "Restore each missing required constraint explicitly. A vertical bar means alternatives; include at least one literal alternative from every listed group: "
            + "；".join(missing_terms)
            + "。"
        )
    if forbidden_hits:
        lines.append(
            "Remove every currently present forbidden constraint without replacing it with a spelling variant: "
            + "；".join(forbidden_hits)
            + "。"
        )
    if integrity_markers:
        lines.append(
            "Remove response-format commentary, markdown/JSON wrappers, control tokens, malformed Unicode, and encoding artifacts; output Chinese scene prose only."
        )
    if "paragraphs_collapsed" in integrity_markers:
        lines.append(
            "The scene came back as one unbroken block. Restore paragraph breaks (a newline between paragraphs): "
            "start a new paragraph for each speaker's line and at each shift of action, place or time, the way the reference passages do."
        )
    if "target_length_not_met" not in set(assessment.get("reasons") or []):
        lines.append(
            "The current length is already acceptable; do not broadly expand or compress it while fixing the listed issue."
        )
    return "\n".join(f"- {line}" for line in lines)


def _de_template_rewrite_brief(quality_gate: dict[str, Any]) -> list[str]:
    brief = [
        "Run no more than this one de-template pass; do not add another rewrite loop.",
        "Keep the same plot facts, speaker identities, core choice, cost, and final hook.",
        "Preserve the reference-derived broad rhythm and paragraph tendencies, but never keep or add an awkward sentence merely to match punctuation or length statistics.",
    ]
    for finding in quality_gate.get("findings", [])[:5]:
        signal_id = finding.get("quality_signal_id", "quality:unknown")
        issue = finding.get("issue") or "anti-template risk"
        evidence = finding.get("evidence_excerpt") or ""
        recommendation = finding.get("recommendation") or ""
        brief.append(f"{signal_id}: {issue}")
        if evidence:
            brief.append(f"Evidence: {evidence}")
        if recommendation:
            brief.append(f"Fix: {recommendation}")
    return brief


def _style_safety_repair_brief(
    *,
    scene: SceneCard,
    source_content: str,
    authoritative_content: str,
    lengths: LengthPolicy,
) -> list[str]:
    """把确定性失败翻译成一次可执行、无正文泄漏的修复清单。"""
    del authoritative_content  # 仅表明调用方已提供可信事实基线；正文不进入提示。
    snapshot = ConstraintSnapshot.read(scene, source_content, lengths)
    required_terms = snapshot.required_terms
    missing_terms = snapshot.missing_required
    length_range = snapshot.length_range
    current_length = snapshot.visible_chars
    brief = [
        "This is the only safety repair attempt. Edit the labeled rejected draft directly, keep its distinctive reusable style, and change only what the hard constraints require.",
        "Return only the complete replacement scene_text prose: no reasoning, markdown fence, JSON wrapper, schema label, or commentary.",
    ]
    if required_terms:
        brief.append(
            "Every final required constraint must be explicit. A vertical bar means alternatives; include at least one literal alternative from each group: "
            + "；".join(required_terms)
            + "。"
        )
    if missing_terms:
        brief.append(
            "Restore the currently missing required constraints: "
            + "；".join(missing_terms)
            + "。"
        )
    if length_range is not None:
        minimum, maximum = length_range
        local_minimum, local_maximum, target = _style_repair_working_window(
            minimum,
            maximum,
            source_length=current_length,
        )
        brief.append(
            f"Final visible Chinese prose length must be {minimum}-{maximum} characters; "
            f"the rejected draft is about {current_length}. Aim near {target} and keep the working "
            f"window at {local_minimum}-{local_maximum}; never use the absolute maximum as the target."
        )
        if current_length < minimum:
            brief.append(
                f"Add {local_minimum - current_length}-{local_maximum - current_length} visible characters; do not return fewer or more than that correction range. "
                "Keep every existing factual beat in order; "
                "add concrete action-reaction, blocking, perception, or consequence inside the same event "
                f"until {local_minimum}-{local_maximum} visible characters are present."
            )
        elif current_length > maximum:
            brief.append(
                f"Remove {current_length - local_maximum}-{current_length - local_minimum} visible characters by compressing repetition only, "
                f"then stop inside {local_minimum}-{local_maximum}; do not remove any required fact, causal step, or ending hook."
            )
    if snapshot.integrity_markers:
        brief.append(
            "Remove malformed Unicode escapes, placeholder controls, and encoding artifacts while preserving the intended Chinese prose."
        )
    brief.append(
        "Use the Scene Card as factual authority. Do not invent a new event, change chronology, or replace the ending hook."
    )
    return brief
