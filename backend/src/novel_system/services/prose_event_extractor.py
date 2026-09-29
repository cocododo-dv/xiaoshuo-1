"""Blueprint §2 — extract narrative events from FINISHED prose, not just the spec.

The spec-based recorder (orchestrator._record_narrative_events) logs what the scene
*plan* declared would happen (exit_change / must_reveal / relationship_turn). That makes
the "single source of truth" a mirror of the spec — it cannot catch the model drifting
away from its spec on the page (e.g. the prose has 林远 using a severed arm).

This extractor reads the *actually generated* prose and asks an LLM to surface the
concrete state changes / location moves / knowledge gains / relationship shifts that the
text itself realizes. Extracted events are tagged ``confidence="extracted"`` +
``payload={"source": "prose"}`` so the consistency checker treats them as ADVISORY — an
LLM extractor hallucinates, so per blueprint §15 it must never become a hard
authoritative blocker, only human-checkable signal.

Opt-in (``NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED`` + ``llm_enabled``); returns an
explicit product envelope for no-call, completed, rejected, provider-failed, and
parse-failed outcomes. Accounting/control-plane integrity failures are never degraded.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall
from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMAccountingRejected,
    LLMCallContext,
    classify_advisory_failure,
    validate_product_call,
)
from novel_system.services.llm_audit import sanitize_audit_summary
from novel_system.services.hash_engine import sha256_text
from novel_system.services.narrative.taxonomy import entity_type_for_event

logger = logging.getLogger(__name__)

EXTRACT_TASK_NAME = "narrative_event_extract"

EXTRACTOR_SYSTEM_PROMPT = """\
You are a precise continuity fact-extractor for a long-form novel.
Work in two passes: first scan the prose paragraph by paragraph and note every apparent
state change; then keep only the ones that are durable — facts that would still matter
several scenes later — and drop momentary action, mood, or interpretation.
Extract ONLY concrete, on-the-page facts that CHANGE STATE.

Event types (use exactly one of these strings):
- character_state: a durable change to a character (injury, item gained/lost, a
  condition that persists beyond this scene)
- location_change: a character is now at a specific place
- character_learns: a character gains specific knowledge/information
- relation_change: the relationship between two characters shifts

Examples of durable facts to report: a character loses an arm (character_state); a
character learns who the killer is (character_learns); two characters end a scene as
enemies after being allies (relation_change).
Examples to NOT report: a character feels afraid for a moment; a character walks across
a room; a character raises their voice.

Respond with JSON only, no prose:
{
  "events": [
    {
      "event_type": "character_state|location_change|character_learns|relation_change",
      "entity_id": "<character name/id exactly as written>",
      "fact_key": "<short snake_case key, e.g. injury / location / learned / stance_toward_X>",
      "fact_value": "<concise factual value>",
      "evidence": "<short quote from the prose supporting this fact>"
    }
  ]
}

Rules:
- Only facts literally supported by the prose. When unsure, omit it.
- Prefer durable facts (matter in later scenes) over momentary action.
- fact_value under 200 chars. Return {"events": []} if nothing qualifies.
"""

_VALID_EVENT_TYPES = frozenset(
    {"character_state", "location_change", "character_learns", "relation_change"}
)


def prose_extraction_parsed_hash(events: list[dict[str, Any]]) -> str:
    """Hash the independently normalized extractor output bound to its LLM parent."""

    payload = json.dumps(
        events,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256_text(payload)


@dataclass(slots=True)
class ExtractedEvent:
    event_type: str
    entity_id: str
    fact_key: str
    fact_value: str
    evidence: str = ""


@dataclass(slots=True)
class ProseExtractionResult:
    events: list[ExtractedEvent] = field(default_factory=list)
    outcome: Literal[
        "not_invoked",
        "rejected_before_dispatch",
        "provider_failed",
        "parse_failed",
        "completed_empty",
        "completed_events",
    ] = "not_invoked"
    llm_call_id: str | None = None
    execution_id: str | None = None
    execution_step_key: str | None = None
    run_job_id: str | None = None
    reason: str | None = None
    error_code: str | None = None

    def product_snapshot(self) -> dict[str, Any]:
        """Return the stable JSON form used by durable checkpoints."""

        return {
            "schema_version": 1,
            "outcome": self.outcome,
            "events": [
                {
                    "event_type": event.event_type,
                    "entity_id": event.entity_id,
                    "fact_key": event.fact_key,
                    "fact_value": event.fact_value,
                    "evidence": event.evidence,
                }
                for event in self.events
            ],
            "llm_call_id": self.llm_call_id,
            "execution_id": self.execution_id,
            "execution_step_key": self.execution_step_key,
            "run_job_id": self.run_job_id,
            "reason": self.reason,
            "error_code": self.error_code,
        }


def extract_events_from_prose(
    content: str,
    *,
    llm_runner: Any | None = None,
    llm_context: LLMCallContext | None = None,
    session: Session | None = None,
    not_invoked_reason: str | None = None,
    max_chars: int = 6000,
) -> ProseExtractionResult:
    """Return prose-grounded events with an explicit invocation/accounting outcome.

    Only failures backed by a valid rejected/failed ledger become degraded products;
    accounting and control-plane integrity failures propagate to the caller.
    """
    if llm_runner is None or not content or not content.strip():
        return ProseExtractionResult(
            outcome="not_invoked",
            execution_id=llm_context.execution_id if llm_context is not None else None,
            execution_step_key=(
                llm_context.execution_step_key if llm_context is not None else None
            ),
            run_job_id=llm_context.run_job_id if llm_context is not None else None,
            reason=(
                not_invoked_reason or "runner_disabled"
                if llm_runner is None
                else "empty_content"
            ),
        )
    if getattr(llm_runner, "provider_execution_mode", "online") == "offline_deterministic":
        return ProseExtractionResult(
            outcome="not_invoked",
            execution_id=llm_context.execution_id if llm_context is not None else None,
            execution_step_key=(
                llm_context.execution_step_key if llm_context is not None else None
            ),
            run_job_id=llm_context.run_job_id if llm_context is not None else None,
            reason="offline_unsupported",
        )
    if llm_context is None:
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_CONTEXT_REQUIRED",
            "prose event extraction requires explicit accounting context",
        )
    if session is None:
        raise LLMAccountingRejected(
            "LLM_ACCOUNTING_SESSION_REQUIRED",
            "prose event extraction requires a durable accounting session",
        )
    # A called extractor is an online advisory product.  Offline execution is
    # represented only by the no-call envelope above.
    called_context = (
        llm_context
        if llm_context.provider_execution_mode == "online"
        else replace(llm_context, provider_execution_mode="online")
    )

    task_prompt = f"## Scene prose\n\n{content[:max_chars]}"
    try:
        response = llm_runner.run_task(
            task_name=EXTRACT_TASK_NAME,
            prompt_text=task_prompt,
            system_prompt=EXTRACTOR_SYSTEM_PROMPT,
            context=called_context,
        )
    except Exception as exc:
        outcome, call_id, error_code = classify_advisory_failure(
            session,
            exc,
            called_context,
        )
        logger.warning("prose event extraction produced a durable degraded outcome", exc_info=True)
        return ProseExtractionResult(
            outcome=outcome,
            llm_call_id=call_id,
            execution_id=llm_context.execution_id,
            execution_step_key=llm_context.execution_step_key,
            run_job_id=llm_context.run_job_id,
            reason=(
                "pre_dispatch_rejection"
                if outcome == "rejected_before_dispatch"
                else "provider_call_failed"
            ),
            error_code=error_code,
        )

    llm_call_id = getattr(response, "llm_call_id", None)
    if not isinstance(llm_call_id, str) or not llm_call_id:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PARENT_ID_MISSING",
            "prose extraction response is missing its durable parent call id",
        )
    parsed = _parse_response(response)
    if parsed is None:
        if session is not None:
            validate_product_call(
                session,
                llm_call_id,
                called_context,
                expected_outcome="parse_failed",
            )
        return ProseExtractionResult(
            outcome="parse_failed",
            llm_call_id=llm_call_id,
            execution_id=llm_context.execution_id,
            execution_step_key=llm_context.execution_step_key,
            run_job_id=llm_context.run_job_id,
            reason="invalid_llm_response",
            error_code="PROSE_EXTRACTION_RESPONSE_INVALID",
        )
    if session is not None:
        validate_product_call(
            session,
            llm_call_id,
            called_context,
            expected_outcome="completed",
        )
    events: list[ExtractedEvent] = []
    for raw in parsed.get("events", []) or []:
        if not isinstance(raw, dict):
            continue
        etype = raw.get("event_type", "")
        if etype not in _VALID_EVENT_TYPES:
            continue
        entity_id = str(raw.get("entity_id", "")).strip()
        fact_key = str(raw.get("fact_key", "")).strip()
        fact_value = str(raw.get("fact_value", "")).strip()
        if not (entity_id and fact_key and fact_value):
            continue
        events.append(
            ExtractedEvent(
                event_type=etype,
                entity_id=entity_id,
                fact_key=fact_key[:80],
                fact_value=fact_value[:200],
                evidence=str(raw.get("evidence", ""))[:200],
            )
        )
    normalized_events = [
        {
            "event_type": event.event_type,
            "entity_id": event.entity_id,
            "fact_key": event.fact_key,
            "fact_value": event.fact_value,
            "evidence": event.evidence,
        }
        for event in events
    ]
    parent = session.get(LlmCall, llm_call_id)
    if parent is None:
        raise LLMAccountingError(
            "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
            "prose extraction parent disappeared before parsed output anchoring",
        )
    parent.response_payload_summary = sanitize_audit_summary(
        {
            **dict(parent.response_payload_summary or {}),
            "prose_extraction_parsed_hash": prose_extraction_parsed_hash(
                normalized_events
            ),
        }
    )
    session.flush()
    return ProseExtractionResult(
        events=events,
        outcome="completed_events" if events else "completed_empty",
        llm_call_id=llm_call_id,
        execution_id=llm_context.execution_id,
        execution_step_key=llm_context.execution_step_key,
        run_job_id=llm_context.run_job_id,
    )


def stage_prose_events(
    log: Any,
    base: dict[str, str],
    events: list[ExtractedEvent],
    *,
    final_scene_row_id: str | None,
    payload: Callable[[int], dict[str, Any]],
) -> list[str]:
    """把抽取出的事件写进事件账本、等作者核对：一律 ``pending`` / ``prose_extraction`` / ``extracted``。

    作者点「提取」与归档时的自动抽取共用这一份（以前各写一遍，B11-13）；``payload(序号)`` 给各自的出处键。
    """
    event_ids: list[str] = []
    for ordinal, extracted in enumerate(events):
        event = log.log_event(
            **base,
            event_type=extracted.event_type,
            entity_type=entity_type_for_event(extracted.event_type),
            entity_id=extracted.entity_id,
            fact_key=extracted.fact_key,
            fact_value=extracted.fact_value,
            confidence="extracted",
            # Never manufacture evidence from an arbitrary prose prefix. A missing
            # quote must remain missing so acceptance fails closed.
            source_text_excerpt=extracted.evidence or None,
            payload=payload(ordinal),
            authority_status="pending",
            source_kind="prose_extraction",
            final_scene_row_id=final_scene_row_id,
        )
        event_ids.append(event.event_id)
    return event_ids


def _parse_response(response: Any) -> dict[str, Any] | None:
    try:
        if hasattr(response, "structured_output") and response.structured_output:
            parsed = response.structured_output
        else:
            raw = (getattr(response, "text", "") or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw[:-3]
            parsed = json.loads(raw.strip())
        if not isinstance(parsed, dict) or not isinstance(parsed.get("events"), list):
            return None
        if any(
            not isinstance(event, dict)
            or event.get("event_type") not in _VALID_EVENT_TYPES
            or not isinstance(event.get("entity_id"), str)
            or not event["entity_id"].strip()
            or not isinstance(event.get("fact_key"), str)
            or not event["fact_key"].strip()
            or not isinstance(event.get("fact_value"), str)
            or not event["fact_value"].strip()
            or not isinstance(event.get("evidence", ""), str)
            for event in parsed["events"]
        ):
            return None
        return parsed
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.warning("failed to parse prose extraction response", exc_info=True)
        return None
