"""Blueprint §2 — extract narrative events from FINISHED prose, not just the plan.

This extractor reads the *actually written* prose and asks an LLM to surface the concrete
state changes / location moves / knowledge gains / relationship shifts that the text itself
realizes. Extracted events are staged as ``pending`` candidates (``confidence="extracted"``,
``payload.source="prose"``): an LLM extractor hallucinates, so nothing it says enters canon
until the author accepts it in the 正史 review (成稿中心).

Two callers: the author's 「提取」 (``CanonContinuityService.extract_scene_candidates``, reads
the whole scene paragraph-chunk by chunk — ``extract_scene_events``) and the opt-in archive
step (``NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED`` + ``llm_enabled``, one call over the first
6,000 characters — its checkpoint product holds exactly one parent call). The prompt is the
``narrative_event_extract`` template in ``config/prompts.yaml``. Every call returns an explicit
product envelope for no-call, completed, rejected, provider-failed, and parse-failed outcomes;
accounting/control-plane integrity failures are never degraded.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
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

EXTRACT_TEMPLATE_NAME = "narrative_event_extract"
# 一次调用最多读的正文字数：归档那一步只读开头这么多；作者点「提取」时按段落切成这么大的几段，读完整场。
EXTRACT_CHUNK_CHARS = 6000
_REPO_PROMPTS_PATH = Path(__file__).resolve().parents[4] / "config" / "prompts.yaml"


def _extractor_template() -> Any:
    """抽取的提示词模板：库里有活动提示词快照读快照，否则读仓库的 ``config/prompts.yaml``。

    这个模板以前写死在代码里；保存过提示词快照、还没跑 ``sync_prompt_templates`` 的安装，快照里还没有它——
    那时用仓库里的这一份（作者不可能改过它，仓库版就是它现在该有的样子）。"""
    from novel_system.services.prompt_builder import load_prompt_templates

    template = load_prompt_templates().get(EXTRACT_TEMPLATE_NAME)
    if template is None:
        template = load_prompt_templates(_REPO_PROMPTS_PATH).get(EXTRACT_TEMPLATE_NAME)
    if template is None:
        raise LookupError(f"prompt template {EXTRACT_TEMPLATE_NAME!r} is missing from config/prompts.yaml")
    return template


def _task_prompt(template: Any, prose: str, *, part: tuple[int, int] | None) -> str:
    head = str(template.task_prompt or "").rstrip()
    if part is not None and part[1] > 1:
        head += f"\n\n(Part {part[0]} of {part[1]} of this scene. Report only facts written in this part.)"
    return f"{head}\n\n{prose}"


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
    max_chars: int = EXTRACT_CHUNK_CHARS,
    part: tuple[int, int] | None = None,
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
    # A called extractor is an online advisory product.
    called_context = (
        llm_context
        if llm_context.provider_execution_mode == "online"
        else replace(llm_context, provider_execution_mode="online")
    )

    template = _extractor_template()
    try:
        response = llm_runner.run_task(
            task_name=EXTRACT_TASK_NAME,
            prompt_text=_task_prompt(template, content[:max_chars], part=part),
            system_prompt=template.system_prompt,
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


_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?；;…])")


def split_prose_for_extraction(content: str, max_chars: int = EXTRACT_CHUNK_CHARS) -> list[str]:
    """把一场正文按段落切成 ≤``max_chars`` 字的几段（每段都是原文的连续子串，证据摘句照样能在终稿里找到原句）。

    整段放得下就整段放；一段本身超长时按句末切，一句还超长就硬切。"""
    text = str(content or "")
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    pieces: list[str] = []
    for paragraph in text.splitlines(keepends=True):
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        for sentence in _SENTENCE_END_RE.split(paragraph):
            while len(sentence) > max_chars:
                pieces.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if sentence:
                pieces.append(sentence)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > max_chars:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


@dataclass(slots=True)
class SceneExtraction:
    """一场正文分段抽取的汇总：``result`` 是整场的产品（事件按段落先后接起来），``event_call_ids`` 是每条事件
    来自哪一次调用，``chunk_count`` 是分了几段。"""

    result: ProseExtractionResult
    event_call_ids: list[str | None] = field(default_factory=list)
    event_chunks: list[int] = field(default_factory=list)
    chunk_count: int = 0


def extract_scene_events(
    content: str,
    *,
    llm_runner: Any,
    llm_context: LLMCallContext,
    session: Session,
    max_chars: int = EXTRACT_CHUNK_CHARS,
) -> SceneExtraction:
    """读完整场：按段落切成几段、每段一次（照常记账的）调用，把事件按段落先后接起来（批准 #14，B11-15）。

    以前只读前 6,000 字、只给 1,200 个输出 token，却照样报「抽取完成」——后半场（挫折与决定常在那里）从来没读过，
    正史面板还请作者确认本场事实已齐。任何一段没抽成（派发前被拒 / 调用失败 / 回答解析不了），整场按那一段的
    结果报降级、一条事件也不暂存：作者再点一次就整场重读，不会留下半场的重复候选。
    """
    chunks = split_prose_for_extraction(content, max_chars)
    if not chunks:
        return SceneExtraction(
            result=extract_events_from_prose(
                content,
                llm_runner=llm_runner,
                llm_context=llm_context,
                session=session,
                max_chars=max_chars,
            ),
        )
    events: list[ExtractedEvent] = []
    event_call_ids: list[str | None] = []
    event_chunks: list[int] = []
    first: ProseExtractionResult | None = None
    for index, chunk in enumerate(chunks):
        kwargs: dict[str, Any] = {
            "llm_runner": llm_runner,
            "llm_context": llm_context,
            "session": session,
            "max_chars": max_chars,
        }
        if len(chunks) > 1:
            kwargs["part"] = (index + 1, len(chunks))
        product = extract_events_from_prose(chunk, **kwargs)
        first = first or product
        if product.outcome not in {"completed_events", "completed_empty"}:
            return SceneExtraction(result=product, chunk_count=len(chunks))
        events.extend(product.events)
        event_call_ids.extend(product.llm_call_id for _ in product.events)
        event_chunks.extend(index for _ in product.events)
    assert first is not None
    return SceneExtraction(
        result=replace(
            first,
            events=events,
            outcome="completed_events" if events else "completed_empty",
        ),
        event_call_ids=event_call_ids,
        event_chunks=event_chunks,
        chunk_count=len(chunks),
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
