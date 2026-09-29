"""Reflexion-style auto-critique pass — blueprint §8.

Independent 'editor' that reviews generated text against an AI-taste checklist
and produces targeted rewrite directives. Runs AFTER best-of-N selection,
BEFORE soft QC — giving the model a chance to self-correct before external QC.

Rule-based mode (``auto_critique``) is a pure-function service; no LLM calls.
LLM-augmented mode (``llm_auto_critique``) merges rule-based findings with a
semantic critic LLM pass for deeper, context-aware feedback.
"""
from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.orm import Session

from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMAccountingRejected,
    LLMCallContext,
    classify_advisory_failure,
    validate_product_call,
)
from novel_system.services.llm_audit import sanitize_audit_summary

logger = logging.getLogger(__name__)

from novel_system.services.literary_quality import (
    analyze_literary_quality,
)

CRITIQUE_THRESHOLD = 0.3

CRITIQUE_DIMS: tuple[str, ...] = (
    "perception_filter",
    "model_voice",
    "false_clarity",
    "over_explained_motive",
    "expository_dialogue",
    "syntax_monotony",
    "self_repetition",
    "decorative_imagery",
    "false_poetic_closure",
    "dialogue_as_report",
    "template_action_reuse",
    "repetitive_action",
    "image_homogeneity",
    "image_field_reuse",
    # §4/§8: cost & conflict dimensions (previously existed but not in critique loop)
    "painless_scene",
    "no_choice_scene",
    "choice_pressure",
    # §8: new conflict-too-clean detector
    "conflict_too_clean",
)

_DIRECTIVE_MAP: dict[str, str] = {
    "perception_filter": (
        "Remove perception filter words (她觉得/他看到/她意识到/她感到). "
        "Show directly, don't filter through character awareness."
    ),
    "model_voice": (
        "Reduce generic AI-prose patterns. Make word choices specific and unexpected."
    ),
    "false_clarity": (
        "Remove false emotional clarity. Characters should not neatly understand "
        "their own feelings mid-scene."
    ),
    "over_explained_motive": (
        "Cut explicit motive explanations. Let actions speak for themselves."
    ),
    "expository_dialogue": (
        "Characters are lecturing. Make dialogue serve character, not exposition."
    ),
    "syntax_monotony": (
        "Vary sentence structures. Current rhythm is too regular — "
        "mix short/long, fragment/complex."
    ),
    "self_repetition": (
        "Repeated expressions from previous scenes detected. Find fresh alternatives."
    ),
    "decorative_imagery": (
        "Imagery is decorative, not functional. Every image should carry narrative meaning."
    ),
    "false_poetic_closure": (
        "Ending wraps up too neatly. Leave productive ambiguity or unresolved tension."
    ),
    "dialogue_as_report": (
        "Dialogue reads like a report. Characters should withhold, deflect, or reveal "
        "through subtext, not state facts plainly."
    ),
    "template_action_reuse": (
        "Action descriptions use template phrases. Replace with specific, observed detail."
    ),
    "repetitive_action": (
        "Same physical actions repeat across passages. Vary the repertoire."
    ),
    "image_homogeneity": (
        "Sensory imagery draws from a narrow field. Introduce contrast across senses."
    ),
    "image_field_reuse": (
        "Same image domain reused. Reach into a different sensory or metaphoric field."
    ),
    "painless_scene": (
        "Scene lacks cost or consequence. Every meaningful choice must cost something — "
        "a relationship strained, a secret exposed, comfort sacrificed. "
        "Add what the character loses by acting."
    ),
    "no_choice_scene": (
        "Scene has no moment of choice. Characters drift through events without deciding. "
        "Force a fork: the character must choose, and the choice must be visible."
    ),
    "choice_pressure": (
        "Choice lacks pressure. The character decides too easily — no competing desires, "
        "no time constraint, no cost either way. Add what makes this choice hard."
    ),
    "conflict_too_clean": (
        "Conflict resolves too cleanly. Characters should NOT understand each other "
        "this quickly. Leave residual friction: an unspoken resentment, a half-lie "
        "accepted, or agreement that costs something the character didn't want to give. "
        "Real conflict leaves scars even when it 'resolves'."
    ),
}


@dataclass(slots=True)
class CritiqueResult:
    should_rewrite: bool
    directives: list[str] = field(default_factory=list)
    dimension_scores: dict[str, float] = field(default_factory=dict)
    flagged_dimensions: list[str] = field(default_factory=list)
    outcome: Literal[
        "not_invoked",
        "completed",
        "rejected_before_dispatch",
        "provider_failed",
        "parse_failed",
    ] = "not_invoked"
    rule_should_rewrite: bool = False
    rule_directives: list[str] = field(default_factory=list)
    rule_dimension_scores: dict[str, float] = field(default_factory=dict)
    rule_flagged_dimensions: list[str] = field(default_factory=list)
    llm_contribution: dict[str, Any] | None = None
    llm_call_id: str | None = None
    execution_id: str | None = None
    execution_step_key: str | None = None
    run_job_id: str | None = None
    reason: str | None = None
    error_code: str | None = None

    def product_snapshot(self) -> dict[str, Any]:
        """Stable JSON product persisted by the soft-QC sub-checkpoint."""

        return {
            "schema_version": 1,
            "outcome": self.outcome,
            "should_rewrite": bool(self.should_rewrite),
            "directives": list(self.directives),
            "dimension_scores": dict(self.dimension_scores),
            "flagged_dimensions": list(self.flagged_dimensions),
            "rule_should_rewrite": bool(self.rule_should_rewrite),
            "rule_directives": list(self.rule_directives),
            "rule_dimension_scores": dict(self.rule_dimension_scores),
            "rule_flagged_dimensions": list(self.rule_flagged_dimensions),
            "llm_contribution": deepcopy(self.llm_contribution),
            "llm_call_id": self.llm_call_id,
            "execution_id": self.execution_id,
            "execution_step_key": self.execution_step_key,
            "run_job_id": self.run_job_id,
            "reason": self.reason,
            "error_code": self.error_code,
        }


def critique_llm_contribution_hash(contribution: dict[str, Any]) -> str:
    canonical = json.dumps(
        contribution,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def auto_critique(
    text: str,
    *,
    skip_critique: bool = False,
    threshold: float = CRITIQUE_THRESHOLD,
    previous_scenes_text: str | None = None,
) -> CritiqueResult:
    """Run adversarial-metric critique and return rewrite directives.

    Parameters
    ----------
    text:
        The style-draft content to critique.
    skip_critique:
        If True (transition scenes), return an empty pass-through result.
    threshold:
        Dimensions with score **at or below** this value are flagged.
        Score semantics: 1.0 = clean, 0.0 = fully triggered.
    previous_scenes_text:
        Optional prior-scene text for self-repetition context (not used
        by this function directly — the underlying ``analyze_literary_quality``
        operates on the single text; cross-scene repetition is handled by the
        separate ``self_repetition`` module wired through ``external_signals``).
    """
    if skip_critique or not text or not text.strip():
        return CritiqueResult(
            should_rewrite=False,
            reason="skip_critique" if skip_critique else "empty_text",
        )

    signals, _ = analyze_literary_quality(text)

    dimension_scores: dict[str, float] = {}
    flagged: list[str] = []
    directives: list[str] = []

    for dim in CRITIQUE_DIMS:
        sig = signals.get(dim)
        if sig is None:
            continue
        score = sig.get("score", 1.0)
        dimension_scores[dim] = score
        if score <= threshold:
            flagged.append(dim)
            directive = _DIRECTIVE_MAP.get(dim)
            if directive:
                evidence = sig.get("evidence", "")
                if evidence:
                    directives.append(f"{directive} (evidence: {evidence[:120]})")
                else:
                    directives.append(directive)

    return CritiqueResult(
        should_rewrite=len(directives) > 0,
        directives=directives,
        dimension_scores=dimension_scores,
        flagged_dimensions=flagged,
        rule_should_rewrite=len(directives) > 0,
        rule_directives=list(directives),
        rule_dimension_scores=dict(dimension_scores),
        rule_flagged_dimensions=list(flagged),
        reason="rule_only",
    )


def format_critique_brief(result: CritiqueResult) -> list[str]:
    """Format critique directives as a rewrite brief list (compatible with patch/rewrite APIs)."""
    if not result.should_rewrite:
        return []
    header = (
        "## Auto-Critique (reflexion pass — blueprint §8)\n"
        "The following AI-taste issues were detected. Revise accordingly:\n"
    )
    return [header] + result.directives


# ---------------------------------------------------------------------------
# LLM critic — blueprint §8 "independent editor role"
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SceneContext:
    """Context for the LLM critic to make richer judgments."""
    scene_goal: str = ""
    tension_target: int | None = None
    character_briefs: list[dict[str, str]] = field(default_factory=list)
    cost_requirement: str = ""


CRITIC_SYSTEM_PROMPT: str = """\
You are an independent fiction editor.  Your role is to perform a semantic
critique of a scene draft.  You are NOT the writer — you are a cold reader
who checks whether the scene actually achieves its dramatic goals on the page.

Your critique dimensions (check each one):

1. **Character consistency** — Does any character act in a way that
   contradicts their established personality, knowledge, or emotional state?
   Flag specific lines where the character seems to break voice.

2. **Earned emotion** — Is every emotional beat set up by prior action,
   pressure, or revealed information?  Flag any emotion that appears without
   a visible cause or that escalates faster than the scene has earned.

3. **Conflict credibility** — Does the central conflict of the scene resolve
   too easily, without the character paying a real cost?  A conflict that
   evaporates, gets talked away, or is solved by coincidence is a problem.

4. **Information dumping** — Is exposition delivered through dialogue ("as
   you know" patterns), internal monologue dumps, or narrator asides that
   stop the scene?  The reader should learn facts through pressure, not
   lecture.

5. **Show vs. tell** — Does the draft describe an emotional state
   ("she felt angry") instead of rendering it through action, body
   language, dialogue subtext, or environmental reaction?

6. **Pacing** — Given the scene's tension target, is the pacing
   appropriate?  A high-tension scene that lingers on atmospheric
   description is too slow; a quiet aftermath scene that rushes through
   reflection is too fast.

Respond in JSON with this exact schema:

{
  "should_rewrite": true/false,
  "issues": [
    {
      "dimension": "<one of: character_consistency | earned_emotion | conflict_credibility | information_dumping | show_vs_tell | pacing>",
      "directive": "<specific rewrite instruction for the writer>",
      "evidence": "<quote or paraphrase of the problematic passage>"
    }
  ]
}

Rules:
- Only report genuine problems.  Do not pad the list.
- If the scene is clean, return {"should_rewrite": false, "issues": []}.
- Keep each directive actionable and under 80 words.
- Quote or closely paraphrase the offending text in "evidence".
- Do NOT suggest style preferences — only flag craft failures.
- "dimension" must be exactly one of the six values listed above, lowercase with underscores — do not invent a new dimension name.
- Return ONLY the JSON object above: no markdown fences, no prose before or after it.
"""

CRITIC_TASK_PROMPT_TEMPLATE: str = """\
## Scene context

{scene_context_block}

## Scene text to critique

{text}
"""

_LLM_CRITIC_DIMENSIONS = frozenset({
    "character_consistency", "earned_emotion", "conflict_credibility",
    "information_dumping", "show_vs_tell", "pacing",
})
_LLM_CONTRIBUTION_DIMENSIONS = _LLM_CRITIC_DIMENSIONS | {"llm_general"}


def llm_auto_critique(
    text: str,
    scene_context: SceneContext | None = None,
    *,
    session: Session | None = None,
    llm_runner: Any | None = None,
    llm_context: LLMCallContext | None = None,
    skip_critique: bool = False,
    not_invoked_reason: str | None = None,
) -> CritiqueResult:
    """Run the hybrid rule-based + LLM critique pipeline.

    Always runs the rule-based pass.  When *llm_runner* is provided and
    *skip_critique* is ``False``, also calls an LLM critic and merges
    the results (deduplicated by dimension). The returned envelope distinguishes
    no-call, success, parse failure, pre-dispatch rejection, and provider failure.
    Accounting and control-plane integrity failures propagate to the caller.
    """
    rule_result = auto_critique(text, skip_critique=skip_critique)

    ownership = {
        "execution_id": llm_context.execution_id if llm_context is not None else None,
        "execution_step_key": (
            llm_context.execution_step_key if llm_context is not None else None
        ),
        "run_job_id": llm_context.run_job_id if llm_context is not None else None,
    }
    if skip_critique or llm_runner is None:
        return replace(
            rule_result,
            outcome="not_invoked",
            reason=(
                "skip_critique"
                if skip_critique
                else not_invoked_reason or "runner_disabled"
            ),
            llm_call_id=None,
            error_code=None,
            **ownership,
        )
    if getattr(llm_runner, "provider_execution_mode", "online") == "offline_deterministic":
        return replace(
            rule_result,
            outcome="not_invoked",
            reason="offline_unsupported",
            llm_call_id=None,
            error_code=None,
            **ownership,
        )
    if llm_context is None:
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_CONTEXT_REQUIRED",
            "LLM critic execution requires explicit accounting context",
        )
    if session is None:
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_SESSION_REQUIRED",
            "LLM critic execution requires a durable accounting session",
        )
    # A called advisory critic is always an online provider product.  Offline
    # runners return the explicit no-call envelope above, so accepting an
    # offline mode on a called context would let coordinated ledger rewrites
    # masquerade as a zero-attempt deterministic success.
    called_context = (
        llm_context
        if llm_context.provider_execution_mode == "online"
        else replace(llm_context, provider_execution_mode="online")
    )

    context_block = _format_scene_context(scene_context)
    task_prompt = CRITIC_TASK_PROMPT_TEMPLATE.format(
        scene_context_block=context_block,
        text=text,
    )

    try:
        response = llm_runner.run_task(
            task_name="auto_critique_llm",
            prompt_text=task_prompt,
            system_prompt=CRITIC_SYSTEM_PROMPT,
            context=called_context,
        )
    except Exception as exc:
        outcome, call_id, error_code = classify_advisory_failure(
            session,
            exc,
            called_context,
        )
        logger.warning("LLM critic call failed with durable degraded outcome", exc_info=True)
        return replace(
            rule_result,
            outcome=outcome,
            llm_call_id=call_id,
            reason=(
                "pre_dispatch_rejection"
                if outcome == "rejected_before_dispatch"
                else "provider_call_failed"
            ),
            error_code=error_code,
            **ownership,
        )

    llm_call_id = getattr(response, "llm_call_id", None)
    if not isinstance(llm_call_id, str) or not llm_call_id:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PARENT_ID_MISSING",
            "LLM critique response is missing its durable parent call id",
        )
    llm_parsed = _parse_llm_response(response)
    if llm_parsed is None:
        if session is not None:
            validate_product_call(
                session,
                llm_call_id,
                called_context,
                expected_outcome="parse_failed",
            )
        return replace(
            rule_result,
            outcome="parse_failed",
            llm_call_id=llm_call_id,
            reason="invalid_llm_response",
            error_code="LLM_CRITIQUE_RESPONSE_INVALID",
            **ownership,
        )
    parent = validate_product_call(
        session,
        llm_call_id,
        called_context,
        expected_outcome="completed",
    )
    llm_issues = []
    for raw_issue in llm_parsed.get("issues") or []:
        if not isinstance(raw_issue, dict):
            continue
        dimension = str(raw_issue.get("dimension") or "llm_general")
        if dimension not in _LLM_CRITIC_DIMENSIONS:
            dimension = "llm_general"
        llm_issues.append(
            {
                "dimension": dimension,
                "directive": str(raw_issue.get("directive") or ""),
                "evidence": str(raw_issue.get("evidence") or "")[:120],
            }
        )
    llm_should_rewrite = bool(llm_parsed.get("should_rewrite", False))
    llm_contribution = {
        "should_rewrite": llm_should_rewrite,
        "issues": llm_issues,
    }
    parent.response_payload_summary = sanitize_audit_summary(
        {
            **dict(parent.response_payload_summary or {}),
            "auto_critique_parsed_llm_hash": critique_llm_contribution_hash(
                llm_contribution
            ),
        }
    )
    session.commit()

    # Merge: rule-based first, then LLM (deduplicated by dimension)
    merged_directives = list(rule_result.directives)
    merged_flagged_dimensions = list(rule_result.flagged_dimensions)
    seen_dims = set(merged_flagged_dimensions)
    for issue in llm_issues:
        dim = issue.get("dimension", "llm_general")
        if dim not in _LLM_CRITIC_DIMENSIONS:
            dim = "llm_general"
        if dim not in seen_dims:
            directive_text = issue.get("directive", "")
            evidence = issue.get("evidence", "")
            if directive_text:
                entry = f"[LLM·{dim}] {directive_text}"
                if evidence:
                    entry += f" (evidence: {evidence[:120]})"
                merged_directives.append(entry)
                seen_dims.add(dim)
                merged_flagged_dimensions.append(dim)

    return CritiqueResult(
        should_rewrite=rule_result.should_rewrite or llm_should_rewrite,
        directives=merged_directives,
        dimension_scores=rule_result.dimension_scores,
        flagged_dimensions=merged_flagged_dimensions,
        outcome="completed",
        rule_should_rewrite=rule_result.rule_should_rewrite,
        rule_directives=list(rule_result.rule_directives),
        rule_dimension_scores=dict(rule_result.rule_dimension_scores),
        rule_flagged_dimensions=list(rule_result.rule_flagged_dimensions),
        llm_contribution=llm_contribution,
        llm_call_id=llm_call_id,
        execution_id=llm_context.execution_id,
        execution_step_key=llm_context.execution_step_key,
        run_job_id=llm_context.run_job_id,
    )


def _format_scene_context(ctx: SceneContext | None) -> str:
    if ctx is None:
        return "(No scene context provided.)"
    parts: list[str] = []
    if ctx.scene_goal:
        parts.append(f"- **Scene goal**: {ctx.scene_goal}")
    if ctx.tension_target is not None:
        parts.append(f"- **Tension target**: {ctx.tension_target}/10")
    if ctx.cost_requirement:
        parts.append(f"- **Cost requirement**: {ctx.cost_requirement}")
    if ctx.character_briefs:
        for brief in ctx.character_briefs:
            name = brief.get("name", "Unknown")
            traits = brief.get("traits", "")
            line = f"- **Character — {name}**: traits={traits}"
            emotional_state = brief.get("emotional_state", "")
            if emotional_state:
                line += f"; emotional_state={emotional_state}"
            parts.append(line)
    return "\n".join(parts) if parts else "(No scene context provided.)"


def _parse_llm_response(response: Any) -> dict[str, Any] | None:
    """Extract JSON from an LLM response object."""
    try:
        if hasattr(response, "structured_output") and response.structured_output:
            parsed = response.structured_output
        else:
            raw = getattr(response, "text", "") or ""
            # Strip markdown code fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw[:-3]
            parsed = json.loads(raw.strip())
        if (
            not isinstance(parsed, dict)
            or type(parsed.get("should_rewrite")) is not bool
            or not isinstance(parsed.get("issues"), list)
            or parsed["should_rewrite"] != bool(parsed["issues"])
            or any(
                not isinstance(issue, dict)
                or issue.get("dimension") not in _LLM_CONTRIBUTION_DIMENSIONS
                or not isinstance(issue.get("directive"), str)
                or not issue["directive"].strip()
                or not isinstance(issue.get("evidence", ""), str)
                for issue in parsed["issues"]
            )
        ):
            return None
        return parsed
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.warning("Failed to parse LLM critic response", exc_info=True)
        return None
