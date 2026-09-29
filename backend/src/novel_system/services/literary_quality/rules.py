"""规则维度（``QUALITY_DIMENSIONS``）：每条规则一个 ``_add_*_signal``，``analyze_literary_quality`` 按固定顺序跑一遍。
有参考书校准时参考作者的常用词从词表里去掉、常态维度降级；没有校准时就是房风词表。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from novel_system.services.literary_quality.calibration import (
    RuleCalibration,
    _apply_dimension_calibration,
    calibrated_lexicons,
)
from novel_system.services.literary_quality.dimensions import (
    AUTOMATED_EVIDENCE_SIGNAL,
    FINDING_ANCHORS,
    QUALITY_DIMENSIONS,
)
from novel_system.services.literary_quality.lexicons import (
    ATMOSPHERIC_IMAGE_TERMS,
    CHOICE_TERMS,
    CONFLICT_TERMS,
    COST_TERMS,
    DECORATIVE_IMAGE_TERMS,
    ENDING_ACTION_TERMS,
    EXPOSITORY_DIALOGUE_TERMS,
    FALSE_CLARITY_TERMS,
    FAULT_LEXICONS,
    IMAGE_TERMS,
    MOTIVE_EXPLANATION_TERMS,
    PERCEPTION_FILTER_TERMS,
    POETIC_CLOSURE_ACTION_TERMS,
    POETIC_CLOSURE_TERMS,
    PRESSURE_TERMS,
    RECONCILIATION_TERMS,
    REPETITIVE_ACTION_TERMS,
    REPORT_DIALOGUE_TERMS,
    SUMMARY_ENDING_TERMS,
)
from novel_system.services.literary_quality.scoring import (
    automated_evidence_sufficiency,
    _evidence_sufficiency_label,
)
from novel_system.services.literary_quality.text import (
    _action_template,
    _compact_ws,
    _dialogue_spans,
    _ending_slice,
    _excerpt,
    _first_present_term,
    _sentences,
    _syntax_pattern,
)


def analyze_literary_quality(
    text: str,
    *,
    calibration: RuleCalibration | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """规则维度（QUALITY_DIMENSIONS）。``calibration``（有风格绑定时由 scene_diagnosis 按参考书算出）：参考作者的常用词从「命中即
    毛病」的词表里去掉，作者常态的维度降为 ``info``——没有校准时就是房风词表，行为与从前逐字相同。"""

    normalized = _compact_ws(text)
    signals: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, str]] = []
    lex = calibrated_lexicons(calibration, normalized) if calibration is not None and calibration.active else dict(FAULT_LEXICONS)

    _add_term_signal(
        signals,
        findings,
        "model_voice",
        normalized,
        lex["model_voice"],
        issue="Possible model voice or generic emotional shortcut.",
        recommendation="Replace abstract realization with a concrete choice, gesture, or sensory consequence.",
    )
    _add_image_signal(signals, findings, normalized, terms=lex["image"])
    _add_repetitive_action_signal(signals, findings, normalized, terms=lex["repetitive_action"])
    _add_template_action_reuse_signal(signals, findings, normalized)
    _add_image_field_reuse_signal(signals, findings, normalized, terms=lex["atmospheric_image"])
    _add_syntax_monotony_signal(signals, findings, normalized)
    _add_self_repetition_signal(signals, findings, normalized)
    _add_false_clarity_signal(signals, findings, normalized, terms=lex["false_clarity"])
    _add_expository_dialogue_signal(signals, findings, normalized, terms=lex["expository_dialogue"])
    _add_dialogue_as_report_signal(signals, findings, normalized, terms=lex["report_dialogue"])
    _add_absence_signal(
        signals,
        findings,
        "no_choice_scene",
        normalized,
        CHOICE_TERMS,
        issue="The passage does not show a clear choice on the page.",
        recommendation="Give the character two incompatible options and make one option visibly cost something.",
    )
    _add_summary_ending_signal(signals, findings, normalized, terms=lex["summary_ending"])
    _add_absence_signal(
        signals,
        findings,
        "choice_pressure",
        normalized,
        PRESSURE_TERMS,
        issue="The choice lacks visible pressure or price.",
        recommendation="State the tradeoff through action: what is lost, risked, or refused because of the choice.",
    )
    _add_ending_drive_signal(signals, findings, normalized)
    _add_painless_scene_signal(signals, findings, normalized)
    _add_conflict_too_clean_signal(
        signals, findings, normalized, conflict_terms=lex["conflict"], reconciliation_terms=lex["reconciliation"]
    )
    _add_decorative_imagery_signal(signals, findings, normalized, terms=lex["decorative_image"])
    _add_over_explained_motive_signal(signals, findings, normalized, terms=lex["motive_explanation"])
    _add_false_poetic_closure_signal(signals, findings, normalized, terms=lex["poetic_closure"])
    _add_perception_filter_signal(signals, findings, normalized, terms=lex["perception_filter"])

    _apply_dimension_calibration(findings, calibration)
    for dimension in QUALITY_DIMENSIONS:
        signals.setdefault(dimension, {"risk": False, "score": 1.0, "evidence": ""})
    evidence_sufficiency = automated_evidence_sufficiency(normalized)
    signals[AUTOMATED_EVIDENCE_SIGNAL] = {
        "risk": evidence_sufficiency < 1.0,
        "score": evidence_sufficiency,
        "evidence": _evidence_sufficiency_label(normalized),
        "diagnostic_only": True,
        "human_judgment_required": True,
    }
    return signals, findings


def _add_self_repetition_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    """Detect exact sentence reuse inside one passage.

    This local guard catches the higher-confidence failure where a generated scene
    repeats the same substantive sentence verbatim (cross-scene repetition is prompt
    guidance in ``self_repetition``, not a signal of this engine).
    """

    normalized_sentences: dict[str, list[str]] = {}
    for sentence in _sentences(text):
        compact = re.sub(r"[^A-Za-z0-9\u3400-\u9fff]+", "", sentence).casefold()
        if len(compact) < 10:
            continue
        normalized_sentences.setdefault(compact, []).append(sentence.strip())
    # 两次完全重复可能是有意的回环、人物口头禅或首尾照应，不能因为系统
    # 支持任意参考风格就把这种作者选择一概当成 AI 痕迹。三次及以上才作为
    # 高置信机械复读；更细腻的重复判断留给盲评。
    repeated = [items for items in normalized_sentences.values() if len(items) >= 3]
    if not repeated:
        signals["self_repetition"] = {"risk": False, "score": 1.0, "evidence": ""}
        return

    duplicate_excess = sum(len(items) - 1 for items in repeated)
    evidence = repeated[0][0]
    score = max(0.0, 1.0 - 0.35 * duplicate_excess)
    signals["self_repetition"] = {
        "risk": True,
        "score": round(score, 4),
        "evidence": evidence,
    }
    findings.append(
        _finding(
            "self_repetition",
            "revision",
            "The passage repeats a substantive sentence verbatim.",
            evidence,
            "Keep the fact once; replace the repeated sentence with a new consequence, reaction, or information beat.",
            needle=evidence,
        )
    )


def _add_term_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    dimension: str,
    text: str,
    terms: tuple[str, ...],
    *,
    issue: str,
    recommendation: str,
) -> None:
    term = _first_present_term(text, terms)
    if term:
        evidence = _excerpt(text, term)
        signals[dimension] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(_finding(dimension, "revision", issue, evidence, recommendation, needle=term))
        return
    signals[dimension] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_absence_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    dimension: str,
    text: str,
    terms: tuple[str, ...],
    *,
    issue: str,
    recommendation: str,
) -> None:
    if _first_present_term(text, terms):
        signals[dimension] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, "")
    signals[dimension] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(_finding(dimension, "revision", issue, evidence, recommendation, anchor="scene"))


def _add_expository_dialogue_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = EXPOSITORY_DIALOGUE_TERMS,
) -> None:
    for dialogue in _dialogue_spans(text):
        term = _first_present_term(dialogue, terms)
        if term:
            evidence = _excerpt(dialogue, term)
            signals["expository_dialogue"] = {"risk": True, "score": 0.0, "evidence": evidence}
            findings.append(
                _finding(
                    "expository_dialogue",
                    "revision",
                    "Dialogue is carrying explanation instead of pressure or subtext.",
                    evidence,
                    "Move the fact into gesture, silence, contradiction, or a partial answer.",
                    needle=term,
                )
            )
            return
    signals["expository_dialogue"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_dialogue_as_report_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = REPORT_DIALOGUE_TERMS,
) -> None:
    for dialogue in _dialogue_spans(text):
        term = _first_present_term(dialogue, terms)
        if not term:
            continue
        evidence = _excerpt(dialogue, term)
        signals["dialogue_as_report"] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(
            _finding(
                "dialogue_as_report",
                "revision",
                "Dialogue is reporting plot information instead of changing pressure between characters.",
                evidence,
                "Turn the fact into a withheld answer, accusation, bargaining chip, or relationship wound.",
                needle=term,
            )
        )
        return
    signals["dialogue_as_report"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_conflict_too_clean_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    conflict_terms: tuple[str, ...] = CONFLICT_TERMS,
    reconciliation_terms: tuple[str, ...] = RECONCILIATION_TERMS,
) -> None:
    """Blueprint §8: detect conflict that resolves too cleanly.

    Checks if conflict-indicating terms and reconciliation terms co-occur
    within a short text window (~300 chars), and if reconciliation terms
    outnumber or match conflict terms — this signals 'too-clean' resolution.
    """
    lowered = text.lower()
    conflict_hits = [t for t in conflict_terms if t.lower() in lowered]
    reconcile_hits = [t for t in reconciliation_terms if t.lower() in lowered]

    if not conflict_hits or not reconcile_hits:
        signals["conflict_too_clean"] = {"risk": False, "score": 1.0, "evidence": ""}
        return

    # Check proximity: do conflict and reconciliation co-occur within 300 chars?
    proximity_count = 0
    for ct in conflict_hits:
        ct_lower = ct.lower()
        pos = 0
        while True:
            idx = lowered.find(ct_lower, pos)
            if idx < 0:
                break
            window = lowered[max(0, idx - 150):idx + len(ct_lower) + 150]
            if any(rt.lower() in window for rt in reconcile_hits):
                proximity_count += 1
            pos = idx + len(ct_lower)

    if proximity_count == 0:
        signals["conflict_too_clean"] = {"risk": False, "score": 1.0, "evidence": ""}
        return

    # If reconciliation ≥ conflict in a scene, the conflict resolves too cleanly
    if len(reconcile_hits) >= len(conflict_hits):
        evidence = _excerpt(text, conflict_hits[0])
        score = max(0.0, 1.0 - (len(reconcile_hits) / max(len(conflict_hits), 1)) * 0.5)
        signals["conflict_too_clean"] = {"risk": True, "score": round(score, 2), "evidence": evidence}
        findings.append(
            _finding(
                "conflict_too_clean",
                "revision",
                (
                    f"Conflict resolves too cleanly: {len(conflict_hits)} conflict signals "
                    f"vs {len(reconcile_hits)} reconciliation signals in close proximity. "
                    "Characters understand each other too quickly."
                ),
                evidence,
                "Leave residual friction: an unspoken resentment, a half-lie accepted, "
                "or agreement that costs something the character didn't want to give.",
                needle=conflict_hits[0],
            )
        )
    else:
        signals["conflict_too_clean"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_painless_scene_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    choice_term = _first_present_term(text, CHOICE_TERMS)
    cost_term = _first_present_term(text, COST_TERMS)
    if cost_term:
        signals["painless_scene"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, choice_term or "")
    signals["painless_scene"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "painless_scene",
            "revision",
            "The scene may be structurally clear but emotionally painless: no concrete loss, betrayal, risk, or sacrifice is visible.",
            evidence,
            "Make the character pay on the page: lose a resource, damage a bond, hide something, betray a value, or choose one safety over another.",
            needle=choice_term or "",
            anchor="text" if choice_term else "scene",
        )
    )


def _add_decorative_imagery_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = DECORATIVE_IMAGE_TERMS,
) -> None:
    hits = [term for term in terms if term.lower() in text.lower()]
    if len(hits) < 2:
        signals["decorative_imagery"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, hits[0])
    signals["decorative_imagery"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "decorative_imagery",
            "taste",
            "The image work is carrying atmosphere more than action, relationship, information, or theme pressure.",
            evidence,
            "Keep the strongest image only if it changes what a character does, reveals a withheld fact, or sharpens the cost of the choice.",
            needle=hits[0],
        )
    )


def _add_over_explained_motive_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = MOTIVE_EXPLANATION_TERMS,
) -> None:
    term = _first_present_term(text, terms)
    if not term:
        signals["over_explained_motive"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["over_explained_motive"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "over_explained_motive",
            "revision",
            "The motive is being explained directly instead of being pressured into action or omission.",
            evidence,
            "Let the reader infer motive from what the character refuses, delays, hides, or chooses under pressure.",
            needle=term,
        )
    )


def _add_false_poetic_closure_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = POETIC_CLOSURE_TERMS,
) -> None:
    ending = _ending_slice(text)
    term = _first_present_term(ending, terms)
    if not term or _first_present_term(ending, POETIC_CLOSURE_ACTION_TERMS):
        signals["false_poetic_closure"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(ending, term)
    signals["false_poetic_closure"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "false_poetic_closure",
            "revision",
            "The ending closes with poetic certainty rather than a hard next-scene action.",
            evidence,
            "End on a visible action, object transfer, refusal, departure, or irreversible reveal instead of abstract resonance.",
            needle=term,
            anchor="ending",
        )
    )


def _add_perception_filter_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = PERCEPTION_FILTER_TERMS,
) -> None:
    term = _first_present_term(text, terms)
    if not term:
        signals["perception_filter"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["perception_filter"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "perception_filter",
            "revision",
            "The narration routes sensation through a perception verb instead of rendering the stimulus directly.",
            evidence,
            "Delete the perception verb and let the stimulus land as action, object, or sensory detail.",
            needle=term,
        )
    )


def _add_image_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = IMAGE_TERMS,
) -> None:
    counts = Counter()
    lowered = text.lower()
    for term in terms:
        if re.fullmatch(r"[a-z]+", term):
            counts[term] = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        else:
            counts[term] = lowered.count(term.lower())
    term, count = max(counts.items(), key=lambda item: item[1]) if counts else ("", 0)
    if count >= 3:
        evidence = _excerpt(text, term)
        signals["image_homogeneity"] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(
            _finding(
                "image_homogeneity",
                "taste",
                f"The same image field repeats too often: {term}.",
                evidence,
                "Keep one anchor image, then vary texture through action, object, temperature, sound, or spatial detail.",
                needle=term,
            )
        )
        return
    signals["image_homogeneity"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_repetitive_action_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = REPETITIVE_ACTION_TERMS,
) -> None:
    counts = Counter()
    lowered = text.lower()
    for term in terms:
        if re.fullmatch(r"[a-z]+", term):
            counts[term] = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        else:
            counts[term] = lowered.count(term.lower())
    term, count = max(counts.items(), key=lambda item: item[1]) if counts else ("", 0)
    if count < 4:
        signals["repetitive_action"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["repetitive_action"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "repetitive_action",
            "revision",
            f"The same action beat repeats too often: {term}.",
            evidence,
            "Keep the strongest beat, then replace the others with a choice, object movement, silence, or changed blocking.",
            needle=term,
        )
    )


def _add_template_action_reuse_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    sentences = _sentences(text)
    templates = Counter(_action_template(sentence) for sentence in sentences)
    templates.pop("", None)
    template, count = max(templates.items(), key=lambda item: item[1]) if templates else ("", 0)
    if count < 3:
        signals["template_action_reuse"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    matching = [sentence for sentence in sentences if _action_template(sentence) == template]
    evidence = " / ".join(matching)[:180]
    signals["template_action_reuse"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "template_action_reuse",
            "revision",
            "Action beats are repeating the same sentence template.",
            evidence,
            "Keep one beat, then vary blocking, object movement, silence, or relational pressure.",
            needle=matching[0] if matching else "",
        )
    )


def _add_image_field_reuse_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = ATMOSPHERIC_IMAGE_TERMS,
) -> None:
    lowered = text.lower()
    hits: list[str] = []
    for term in terms:
        count = len(re.findall(rf"\b{re.escape(term)}\b", lowered)) if re.fullmatch(r"[a-z]+", term) else lowered.count(term.lower())
        hits.extend([term] * count)
    if len(hits) < 4 or len(set(hits)) < 3:
        signals["image_field_reuse"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, hits[0])
    signals["image_field_reuse"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "image_field_reuse",
            "taste",
            f"The atmospheric image field is doing too much repeated work: {', '.join(sorted(set(hits))[:5])}.",
            evidence,
            "Let one image carry mood, and make the next beat change through an object, decision, or body position.",
            needle=hits[0],
        )
    )


def _add_syntax_monotony_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    sentences = _sentences(text)
    patterns = Counter(_syntax_pattern(sentence) for sentence in sentences)
    patterns.pop("", None)
    pattern, count = max(patterns.items(), key=lambda item: item[1]) if patterns else ("", 0)
    if count < 3:
        signals["syntax_monotony"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    matching = [sentence for sentence in sentences if _syntax_pattern(sentence) == pattern]
    evidence = " / ".join(matching)[:180]
    signals["syntax_monotony"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "syntax_monotony",
            "taste",
            "Several consecutive sentences use the same syntactic shape.",
            evidence,
            "Break the rhythm with a short sentence, a withheld response, or a sentence that starts from consequence instead of gesture.",
            needle=matching[0] if matching else "",
        )
    )


def _add_false_clarity_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = FALSE_CLARITY_TERMS,
) -> None:
    term = _first_present_term(text, terms)
    if not term:
        signals["false_clarity"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["false_clarity"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "false_clarity",
            "revision",
            "The passage tells the reader what became clear instead of letting pressure reveal it.",
            evidence,
            "Cut the explanation and let a choice, refusal, object transfer, or silence create the reader's inference.",
            needle=term,
        )
    )


def _add_summary_ending_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = SUMMARY_ENDING_TERMS,
) -> None:
    ending = _ending_slice(text)
    term = _first_present_term(ending, terms)
    if term:
        evidence = _excerpt(ending, term)
        signals["summary_ending"] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(
            _finding(
                "summary_ending",
                "revision",
                "The ending explains the effect instead of landing on an action or image.",
                evidence,
                "Cut the summarizing sentence and end on the last irreversible action.",
                needle=term,
                anchor="ending",
            )
        )
        return
    signals["summary_ending"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_ending_drive_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    ending = _ending_slice(text)
    has_action = _first_present_term(ending, ENDING_ACTION_TERMS)
    if has_action and not signals.get("summary_ending", {}).get("risk"):
        signals["ending_drive"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(ending, "")
    signals["ending_drive"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "ending_drive",
            "revision",
            "The final beat does not push the reader into the next scene.",
            evidence,
            "End with a new action, object movement, arrival, departure, reveal, or refusal.",
            anchor="ending",
        )
    )


def _finding(
    dimension: str,
    severity: str,
    issue: str,
    evidence: str,
    recommendation: str,
    *,
    needle: str = "",
    anchor: str = "text",
) -> dict[str, str]:
    """One rule finding.

    ``needle`` is the exact text the rule matched (a term, the repeated sentence, …) —
    the workbench pins the finding to that string in the manuscript and the finding's
    stable id is derived from it. Findings without a needle carry an ``anchor``:
    ``scene`` (an absence — nothing in the text to point at) or ``ending`` (the last
    beat). ``text`` with an empty needle means "locate by the excerpt".
    """

    return {
        "dimension": dimension,
        "severity": severity,
        "issue": issue,
        "evidence_excerpt": evidence,
        "recommendation": recommendation,
        "needle": needle or "",
        "anchor": anchor if anchor in FINDING_ANCHORS else "text",
    }
