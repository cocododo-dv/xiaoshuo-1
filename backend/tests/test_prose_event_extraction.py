"""Blueprint §2 — prose-grounded event extraction (close the "spec mirror" gap).

The spec-based recorder logs what the plan declared; this extractor reads the actual
prose and records what the TEXT realized, tagged as advisory (confidence="extracted",
source="prose") so a hallucinating extractor can never hard-block consistency (§15).
"""

from __future__ import annotations

import pytest


class _Resp:
    def __init__(self, text: str, *, llm_call_id: str | None = "llmcall_prose_test") -> None:
        self.text = text
        self.structured_output = None
        self.llm_call_id = llm_call_id


class _Runner:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []

    def run_task(self, *, task_name, prompt_text, system_prompt, context):
        self.calls.append({"task_name": task_name, "prompt_text": prompt_text, "context": context})
        return _Resp(self._text)


class _AccountedRunner(_Runner):
    def __init__(self, session, text: str) -> None:
        super().__init__(text)
        self.session = session

    def run_task(self, *, task_name, prompt_text, system_prompt, context):
        from novel_system.db.models import LlmCall, LlmCallAttempt

        self.calls.append({"task_name": task_name, "prompt_text": prompt_text, "context": context})
        call_id = "llmcall_prose_orchestrator"
        self.session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={"_accounting_provider_execution_mode": "online"},
                prompt_tokens=7,
                completion_tokens=0,
                total_tokens=7,
                estimated_tokens=7,
                reserved_tokens=7,
                budget_charged_tokens=7,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="settled",
                request_dispatched_at="2026-07-14T00:00:00Z",
                settled_at="2026-07-14T00:00:01Z",
            )
        )
        self.session.add(
            LlmCallAttempt(
                attempt_id="attempt_prose_orchestrator_0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=0,
                prompt_tokens=7,
                completion_tokens=0,
                total_tokens=7,
                estimated_tokens=7,
                reserved_tokens=7,
                budget_charged_tokens=7,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="settled",
                request_dispatched_at="2026-07-14T00:00:00Z",
                settled_at="2026-07-14T00:00:01Z",
            )
        )
        self.session.commit()
        return _Resp(self._text, llm_call_id=call_id)


def _llm_context():
    from novel_system.services.llm_accounting import LLMCallContext

    return LLMCallContext(
        scope_type="system",
        scope_id="prose-extraction-test",
        node_id="extraction",
        step="archive:prose_event_extract:0",
    )


def _owned_llm_context():
    from novel_system.services.llm_accounting import LLMCallContext

    return LLMCallContext(
        scope_type="scene",
        scope_id="SC_PROSE",
        project_id="P_PROSE",
        chapter_id="CH_PROSE",
        scene_id="SC_PROSE",
        node_id="extraction",
        step="archive:prose_event_extract:0",
        execution_id="exec-prose",
        execution_step_key="archive:prose_event_extract:0",
        run_job_id="job-prose",
    )


def _task_error(
    *, code: str, llm_call_id: str = "llmcall_prose_failed", rejected: bool = False
):
    from novel_system.services.llm_accounting import LLMAccountingRejected
    from novel_system.services.llm_task_runner import LLMNodeExecutionError

    return LLMNodeExecutionError(
        llm_call_id=llm_call_id,
        error_code=code,
        message=code,
        request_summary={},
        response_summary={},
        original_error=(LLMAccountingRejected(code, code) if rejected else RuntimeError(code)),
    )


def _seed_failure_ledger(session, *, context, outcome: str, error_code: str) -> None:
    from novel_system.db.models import LlmCall, LlmCallAttempt

    dispatched = outcome == "provider_failed"
    tokens = 13 if dispatched else 0
    session.add(
        LlmCall(
            llm_call_id="llmcall_prose_failed",
            provider="fake",
            model="fake",
            node_id=context.node_id,
            step=context.step,
            project_id=context.project_id,
            chapter_id=context.chapter_id,
            scene_id=context.scene_id,
            scope_type=context.scope_type,
            scope_id=context.scope_id,
            run_job_id=context.run_job_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            request_payload_summary={"_accounting_provider_execution_mode": "online"},
            prompt_tokens=tokens,
            completion_tokens=0,
            total_tokens=tokens,
            estimated_tokens=tokens,
            reserved_tokens=tokens,
            budget_charged_tokens=tokens,
            latency_ms=0,
            usage_is_estimate=True,
            accounting_status="failed" if dispatched else "rejected",
            request_dispatched_at="2026-07-14T00:00:00Z" if dispatched else None,
            settled_at="2026-07-14T00:00:01Z",
            error_code=error_code,
        )
    )
    if dispatched:
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_prose_failed_0",
                llm_call_id="llmcall_prose_failed",
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=0,
                prompt_tokens=tokens,
                completion_tokens=0,
                total_tokens=tokens,
                estimated_tokens=tokens,
                reserved_tokens=tokens,
                budget_charged_tokens=tokens,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="failed",
                request_dispatched_at="2026-07-14T00:00:00Z",
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
    session.commit()


def _seed_success_ledger(session, *, context, call_id: str = "llmcall_prose_test") -> None:
    from novel_system.db.models import LlmCall, LlmCallAttempt

    session.add(
        LlmCall(
            llm_call_id=call_id,
            provider="fake",
            model="fake",
            node_id=context.node_id,
            step=context.step,
            project_id=context.project_id,
            chapter_id=context.chapter_id,
            scene_id=context.scene_id,
            scope_type=context.scope_type,
            scope_id=context.scope_id,
            run_job_id=context.run_job_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            request_payload_summary={"_accounting_provider_execution_mode": "online"},
            prompt_tokens=8,
            completion_tokens=2,
            total_tokens=10,
            estimated_tokens=10,
            reserved_tokens=10,
            budget_charged_tokens=10,
            latency_ms=2,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:01Z",
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id=f"attempt_{call_id}_0",
            llm_call_id=call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=128,
            prompt_tokens=8,
            completion_tokens=2,
            total_tokens=10,
            estimated_tokens=10,
            reserved_tokens=10,
            budget_charged_tokens=10,
            latency_ms=2,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:01Z",
        )
    )
    session.commit()


def test_extract_without_runner_returns_empty() -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    assert extract_events_from_prose("some prose", llm_runner=None).outcome == "not_invoked"
    # also empty content with a runner
    assert extract_events_from_prose("", llm_runner=_Runner("{}")).reason == "empty_content"


def test_extract_with_runner_but_no_context_fails_before_runner_io() -> None:
    from novel_system.services.llm_accounting import LLMAccountingRejected
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    runner = _Runner("{}")
    with pytest.raises(LLMAccountingRejected) as rejected:
        extract_events_from_prose("prose", llm_runner=runner)

    assert rejected.value.code == "LLM_ACCOUNTING_CONTEXT_REQUIRED"
    assert runner.calls == []


def test_called_prose_extractor_normalizes_context_to_online(session) -> None:
    from dataclasses import replace

    from novel_system.services.prose_event_extractor import extract_events_from_prose

    runner = _Runner('{"events": []}')
    supplied = replace(
        _llm_context(),
        provider_execution_mode="offline_deterministic",
    )
    _seed_success_ledger(session, context=supplied)
    result = extract_events_from_prose(
        "some prose",
        session=session,
        llm_runner=runner,
        llm_context=supplied,
    )

    assert result.outcome == "completed_empty"
    assert runner.calls[0]["context"].provider_execution_mode == "online"


def test_extract_rejects_mixed_valid_and_invalid_event_items(session) -> None:
    from novel_system.services.prose_event_extractor import (
        EXTRACT_TASK_NAME,
        extract_events_from_prose,
    )

    runner = _Runner(
        '{"events": ['
        '{"event_type":"character_state","entity_id":"林远","fact_key":"injury","fact_value":"右臂截断","evidence":"他的右臂被斩断"},'
        '{"event_type":"NOT_A_TYPE","entity_id":"B","fact_key":"k","fact_value":"v"},'
        '{"event_type":"location_change","entity_id":"","fact_key":"loc","fact_value":"城外"}'
        ']}'
    )
    context = _llm_context()
    _seed_success_ledger(session, context=context)
    events = extract_events_from_prose(
        "林远的右臂被斩断了。", session=session, llm_runner=runner, llm_context=context
    )

    assert events.outcome == "parse_failed"
    assert events.events == []
    # routed via the dedicated §2 extraction task
    assert runner.calls[0]["task_name"] == EXTRACT_TASK_NAME


def test_extract_unaccounted_runner_error_is_not_degraded(session) -> None:
    from novel_system.services.llm_accounting import LLMAccountingError
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    class _Broken:
        def run_task(self, **_kw):
            raise RuntimeError("LLM down")

    with pytest.raises(LLMAccountingError) as raised:
        extract_events_from_prose(
            "prose", session=session, llm_runner=_Broken(), llm_context=_llm_context()
        )
    assert raised.value.code == "LLM_ACCOUNTING_ADVISORY_FAILURE_UNTRACKED"


def test_extract_malformed_json_returns_empty(session) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    context = _llm_context()
    _seed_success_ledger(session, context=context)
    result = extract_events_from_prose(
        "prose", session=session, llm_runner=_Runner("not json at all"), llm_context=context
    )
    assert result.outcome == "parse_failed"
    assert result.events == []


def test_narrative_event_extract_aliases_to_existing_route(session) -> None:
    from novel_system.services.llm_task_runner import _AD_HOC_ROUTE_ALIASES, LLMNodeRunner

    assert _AD_HOC_ROUTE_ALIASES["narrative_event_extract"] == "extraction"
    cfg = LLMNodeRunner(session).task_config("extraction")
    assert getattr(cfg, "model", None), "extraction (extractor alias target) did not resolve"


def test_extracted_events_logged_as_advisory(session) -> None:
    """End-to-end: prose -> extract -> log as advisory (confidence=extracted, source=prose),
    so projection/consistency can distinguish it from authoritative spec facts."""
    from novel_system.services.narrative_event_log import NarrativeEventLog
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    runner = _AccountedRunner(
        session,
        '{"events": [{"event_type":"character_state","entity_id":"林远",'
        '"fact_key":"injury","fact_value":"右臂截断","evidence":"右臂已断"}]}'
    )
    events = extract_events_from_prose(
        "林远的右臂被斩断。", session=session, llm_runner=runner, llm_context=_llm_context()
    )
    assert len(events.events) == 1

    _seed_event_scene(session, "p1", "c1", "s1")
    log = NarrativeEventLog(session)
    ev = events.events[0]
    logged = log.log_event(
        project_id="p1", scene_id="s1", chapter_id="c1",
        event_type=ev.event_type, entity_type="character", entity_id=ev.entity_id,
        fact_key=ev.fact_key, fact_value=ev.fact_value,
        confidence="extracted", source_text_excerpt=ev.evidence,
        payload={"source": "prose"},
    )
    assert logged.confidence == "extracted"
    assert logged.payload_json.get("source") == "prose"


def test_orchestrator_prose_extraction_gate_open_when_flag_enabled(session, monkeypatch) -> None:
    """§2 gate teeth (real code path): flag ON + runner present -> the orchestrator's
    _record_prose_events extracts from prose and logs an advisory NarrativeEvent
    (confidence=extracted, source=prose). Drives the REAL gate (orchestrator.py:648),
    not a mirror — removing the `llm_event_extraction_enabled` guard flips the OFF test."""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED", "true")
    from sqlalchemy import select

    from novel_system.db.models import LlmCall, NarrativeEvent
    from novel_system.services.narrative_event_log import NarrativeEventLog
    from novel_system.services.orchestrator import Orchestrator

    runner = _AccountedRunner(
        session,
        '{"events": [{"event_type":"character_state","entity_id":"林远",'
        '"fact_key":"injury","fact_value":"右臂截断","evidence":"右臂被斩断"}]}'
    )
    orch = Orchestrator(session)
    orch.llm_runner = runner
    log = NarrativeEventLog(session)
    base = {"project_id": "p_pe", "scene_id": "s_pe", "chapter_id": "c_pe"}
    _seed_event_scene(session, "p_pe", "c_pe", "s_pe")
    result = orch._record_prose_events(log, None, base, "林远的右臂被斩断了。")
    session.flush()

    assert runner.calls, "extractor never called -> gate stayed closed despite flag ON"
    assert result.outcome == "completed_events"
    assert result.llm_call_id == "llmcall_prose_orchestrator"
    from novel_system.services.prose_event_extractor import prose_extraction_parsed_hash
    parent = session.get(LlmCall, result.llm_call_id)
    assert parent.response_payload_summary["prose_extraction_parsed_hash"] == (
        prose_extraction_parsed_hash(result.product_snapshot()["events"])
    )
    rows = (
        session.execute(
            select(NarrativeEvent).where(
                NarrativeEvent.scene_id == "s_pe",
                NarrativeEvent.confidence == "extracted",
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].entity_id == "林远"
    assert rows[0].fact_key == "injury"
    assert rows[0].payload_json.get("source") == "prose"


def test_orchestrator_prose_extraction_gate_closed_when_flag_disabled(session, monkeypatch) -> None:
    """§2 default: flag OFF -> the gate short-circuits before touching the runner."""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED", "false")
    from sqlalchemy import select

    from novel_system.db.models import NarrativeEvent
    from novel_system.services.narrative_event_log import NarrativeEventLog
    from novel_system.services.orchestrator import Orchestrator

    runner = _Runner(
        '{"events": [{"event_type":"character_state","entity_id":"林远",'
        '"fact_key":"injury","fact_value":"x","evidence":"y"}]}'
    )
    orch = Orchestrator(session)
    orch.llm_runner = runner
    log = NarrativeEventLog(session)
    base = {"project_id": "p_pe2", "scene_id": "s_pe2", "chapter_id": "c_pe2"}
    orch._record_prose_events(log, None, base, "林远的右臂被斩断了。")
    session.flush()

    assert runner.calls == [], "runner called despite flag OFF -> gate broken"
    rows = (
        session.execute(
            select(NarrativeEvent).where(NarrativeEvent.confidence == "extracted")
        )
        .scalars()
        .all()
    )
    assert rows == []


def test_orchestrator_prose_duplicate_events_return_direct_ordered_row_ids(
    session, monkeypatch
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED", "true")
    from novel_system.db.models import NarrativeEvent
    from novel_system.services.narrative_event_log import NarrativeEventLog
    from novel_system.services.orchestrator import Orchestrator

    duplicate = {
        "event_type": "character_state",
        "entity_id": "Lin",
        "fact_key": "injury",
        "fact_value": "arm broken",
        "evidence": "his arm broke",
    }
    runner = _AccountedRunner(
        session,
        __import__("json").dumps({"events": [duplicate, duplicate]}),
    )
    orch = Orchestrator(session)
    orch.llm_runner = runner
    orch._execution_id = "exec-prose-duplicates"
    _seed_event_scene(session, "p_dup", "c_dup", "s_dup")
    result, event_ids = orch._record_prose_events(
        NarrativeEventLog(session),
        None,
        {"project_id": "p_dup", "scene_id": "s_dup", "chapter_id": "c_dup"},
        "his arm broke",
        return_event_ids=True,
    )
    assert result.outcome == "completed_events"
    assert len(event_ids) == 2
    assert len(set(event_ids)) == 2
    rows = [session.get(NarrativeEvent, event_id) for event_id in event_ids]
    assert [row.payload_json["archive_ordinal"] for row in rows] == [0, 1]


def test_extract_returns_explicit_no_call_and_completed_empty_envelopes(session) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    disabled = extract_events_from_prose("some prose", llm_runner=None)
    assert disabled.outcome == "not_invoked"
    assert disabled.reason == "runner_disabled"
    assert disabled.llm_call_id is None
    assert disabled.events == []

    context = _owned_llm_context()
    _seed_success_ledger(session, context=context)
    completed = extract_events_from_prose(
        "some prose",
        session=session,
        llm_runner=_Runner('{"events": []}'),
        llm_context=context,
    )
    assert completed.outcome == "completed_empty"
    assert completed.reason is None
    assert completed.llm_call_id == "llmcall_prose_test"
    assert completed.execution_id == "exec-prose"
    assert completed.execution_step_key == "archive:prose_event_extract:0"
    assert completed.run_job_id == "job-prose"
    assert completed.events == []


def test_extract_parse_failure_is_distinct_from_completed_empty(session) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    context = _owned_llm_context()
    _seed_success_ledger(session, context=context)
    result = extract_events_from_prose(
        "prose", session=session, llm_runner=_Runner("not json"), llm_context=context
    )

    assert result.outcome == "parse_failed"
    assert result.reason == "invalid_llm_response"
    assert result.error_code == "PROSE_EXTRACTION_RESPONSE_INVALID"
    assert result.llm_call_id == "llmcall_prose_test"
    assert result.events == []


@pytest.mark.parametrize(
    ("error_code", "outcome"),
    [
        ("LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", "rejected_before_dispatch"),
        ("LLM_HTTP_REQUEST_FAILED", "provider_failed"),
    ],
)
def test_extract_failure_envelope_preserves_parent_and_owner(
    session, error_code: str, outcome: str
) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    context = _owned_llm_context()
    _seed_failure_ledger(session, context=context, outcome=outcome, error_code=error_code)
    error = _task_error(code=error_code, rejected=outcome == "rejected_before_dispatch")

    class _Broken:
        def run_task(self, **_kwargs):
            raise error

    result = extract_events_from_prose(
        "prose", session=session, llm_runner=_Broken(), llm_context=context
    )

    assert result.outcome == outcome
    assert result.llm_call_id == "llmcall_prose_failed"
    assert result.error_code == error_code
    assert result.execution_id == "exec-prose"
    assert result.execution_step_key == "archive:prose_event_extract:0"
    assert result.run_job_id == "job-prose"


def test_extract_failed_parent_is_authoritative_over_rejected_exception_type(session) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    context = _owned_llm_context()
    _seed_failure_ledger(
        session,
        context=context,
        outcome="provider_failed",
        error_code="LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
    )
    error = _task_error(code="LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", rejected=True)

    class _Broken:
        def run_task(self, **_kwargs):
            raise error

    result = extract_events_from_prose(
        "prose",
        session=session,
        llm_runner=_Broken(),
        llm_context=context,
    )

    assert result.outcome == "provider_failed"
    assert result.error_code == "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
    assert result.llm_call_id == "llmcall_prose_failed"


def test_extract_true_child_aggregate_mismatch_is_not_degraded(session) -> None:
    from novel_system.db.models import LlmCallAttempt
    from novel_system.services.llm_accounting import LLMAccountingError
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    context = _owned_llm_context()
    _seed_failure_ledger(
        session,
        context=context,
        outcome="provider_failed",
        error_code="LLM_HTTP_REQUEST_FAILED",
    )
    child = session.get(LlmCallAttempt, "attempt_prose_failed_0")
    child.prompt_tokens += 1
    child.total_tokens += 1
    session.commit()
    error = _task_error(code="LLM_HTTP_REQUEST_FAILED")

    class _Broken:
        def run_task(self, **_kwargs):
            raise error

    with pytest.raises(LLMAccountingError) as raised:
        extract_events_from_prose(
            "prose",
            session=session,
            llm_runner=_Broken(),
            llm_context=context,
        )
    assert raised.value.code == "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID"


@pytest.mark.parametrize(
    "error_code",
    [
        "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
        "RUN_CHECKPOINT_OUTPUT_MISSING",
        "RUN_OWNER_LEASE_LOST",
        "LLM_ACCOUNTING_HOOK_UNSUPPORTED",
        "LLM_OFFLINE_RESPONSE_INVALID",
        "LLM_OFFLINE_CAPABILITY_UNSUPPORTED",
    ],
)
def test_extract_control_plane_failures_are_not_degraded(
    session, error_code: str
) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    error = _task_error(code=error_code)
    if error_code == "LLM_ACCOUNTING_HOOK_UNSUPPORTED":
        _seed_failure_ledger(
            session,
            context=_owned_llm_context(),
            outcome="rejected_before_dispatch",
            error_code=error_code,
        )

    class _Broken:
        def run_task(self, **_kwargs):
            raise error

    with pytest.raises(type(error)) as raised:
        extract_events_from_prose(
            "prose", session=session, llm_runner=_Broken(), llm_context=_owned_llm_context()
        )
    assert raised.value is error


def test_extract_success_without_parent_id_is_integrity_error(session) -> None:
    from novel_system.services.llm_accounting import LLMAccountingError
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    class _RunnerWithoutParent:
        def run_task(self, **_kwargs):
            return _Resp('{"events": []}', llm_call_id=None)

    with pytest.raises(LLMAccountingError) as raised:
        extract_events_from_prose(
            "prose", session=session, llm_runner=_RunnerWithoutParent(), llm_context=_owned_llm_context()
        )
    assert raised.value.code == "LLM_ACCOUNTING_PARENT_ID_MISSING"


def test_extract_requires_session_before_provider_io() -> None:
    from novel_system.services.llm_accounting import LLMAccountingRejected
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    runner = _Runner('{"events": []}')
    with pytest.raises(LLMAccountingRejected) as raised:
        extract_events_from_prose(
            "prose",
            llm_runner=runner,
            llm_context=_llm_context(),
        )
    assert raised.value.code == "LLM_ACCOUNTING_SESSION_REQUIRED"
    assert runner.calls == []


def test_prose_control_plane_failure_crosses_recording_catches(session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED", "true")
    from types import SimpleNamespace

    from novel_system.services.narrative_event_log import NarrativeEventLog
    from novel_system.services.orchestrator import Orchestrator

    error = _task_error(code="LLM_ACCOUNTING_EXECUTION_STEP_EXISTS")

    def raise_control(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(
        "novel_system.services.prose_event_extractor.extract_events_from_prose",
        raise_control,
    )
    orch = Orchestrator(session)
    base = {"project_id": "p_control", "scene_id": "s_control", "chapter_id": "c_control"}
    with pytest.raises(type(error)) as direct:
        orch._record_prose_events(NarrativeEventLog(session), None, base, "prose")
    assert direct.value is error

    scene = SimpleNamespace(
        scene_id="s_control",
        chapter_id="c_control",
        project_id="p_control",
        pov_character_id=None,
        onstage_chars_json=[],
        exit_change="",
        location=None,
        writer_brief_json={},
    )
    contract = SimpleNamespace(payload_json={})
    orch._record_relation_events = lambda *_args, **_kwargs: None
    orch._record_foreshadow_events = lambda *_args, **_kwargs: None
    orch._record_prose_events = raise_control
    with pytest.raises(type(error)) as outer:
        orch._record_narrative_events(scene, contract, "prose")
    assert outer.value is error
# Event rows are position-addressed; unit tests that exercise the recorder need
# the same minimal catalog identity as the production archive path.
def _seed_event_scene(session, project_id: str, chapter_id: str, scene_id: str) -> None:
    from novel_system.db.models import ChapterGoal, SceneCard, StoryProject

    session.add(
        StoryProject(
            project_id=project_id,
            title="Prose event extraction",
            outline_text="",
        )
    )
    session.flush()
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            chapter_goal="event extraction",
            display_order=1,
        )
    )
    session.flush()
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            scene_goal="event extraction",
        )
    )
    session.flush()
