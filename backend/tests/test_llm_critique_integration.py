"""Blueprint §8 — wire the independent LLM editor critic into production.

``llm_auto_critique`` (a 6-dimension semantic editor pass) was fully implemented but had
ZERO callers because the runner interface it expected — ``run_task(task_name, prompt_text,
system_prompt)`` — existed only on a test fake, never on the production ``LLMNodeRunner``.
These tests cover the now-real wiring:

- ``LLMNodeRunner.run_task`` assembles an ad-hoc request and uses the resolved client.
- the ``auto_critique_llm`` route resolves (models.yaml task_routing).
- the orchestrator path degrades to rule-only when the runner is absent (opt-in default).
- the LLM editor's issues are merged into the rewrite brief when a runner is present.
"""

from __future__ import annotations

import pytest

from novel_system.db.models import ChapterGoal, SceneCard, SceneRunState, StoryProject


class _FakeResponse:
    def __init__(self, text: str, *, llm_call_id: str | None = "llmcall_auto_test") -> None:
        self.text = text
        self.structured_output = None
        self.llm_call_id = llm_call_id


def _seed_owned_scene(session) -> None:
    """Create the authoritative owner chain required by ``SceneRunState``."""
    session.add_all(
        [
            StoryProject(
                project_id="P_AUTO",
                title="Auto critique integration",
                outline_text="Test-owned outline",
            ),
            ChapterGoal(
                chapter_id="CH_AUTO",
                project_id="P_AUTO",
                planned_scene_count=1,
                chapter_goal="Exercise the independent editor critic",
            ),
            SceneCard(
                scene_id="SC_AUTO",
                chapter_id="CH_AUTO",
                project_id="P_AUTO",
                scene_seq=1,
                scene_goal="Produce an editor critique",
                onstage_chars_json=[],
                beats_json=[],
            ),
        ]
    )
    session.flush()


def _llm_context():
    from novel_system.services.llm_accounting import LLMCallContext

    return LLMCallContext(
        scope_type="system",
        scope_id="auto-critique-test",
        node_id="soft_qc",
        step="soft_qc:auto_critique:0",
    )


def _owned_llm_context():
    from novel_system.services.llm_accounting import LLMCallContext

    return LLMCallContext(
        scope_type="scene",
        scope_id="SC_AUTO",
        project_id="P_AUTO",
        chapter_id="CH_AUTO",
        scene_id="SC_AUTO",
        node_id="soft_qc",
        step="soft_qc:auto_critique:0",
        execution_id="exec-auto",
        execution_step_key="soft_qc:auto_critique:0",
        run_job_id="job-auto",
    )


def _task_error(
    *, code: str, llm_call_id: str = "llmcall_auto_failed", rejected: bool = False
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
    tokens = 11 if dispatched else 0
    session.add(
        LlmCall(
            llm_call_id="llmcall_auto_failed",
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
                attempt_id="attempt_auto_failed_0",
                llm_call_id="llmcall_auto_failed",
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


def _seed_success_ledger(session, *, context, call_id: str) -> None:
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
            prompt_tokens=7,
            completion_tokens=3,
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
            prompt_tokens=7,
            completion_tokens=3,
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


def test_auto_critique_passes_explicit_context_to_run_task(session) -> None:
    from novel_system.services.auto_critique import llm_auto_critique
    captured: dict = {}

    class _Runner:
        def run_task(self, *, task_name, prompt_text, system_prompt, context):
            captured.update(
                task_name=task_name,
                prompt_text=prompt_text,
                system_prompt=system_prompt,
                context=context,
            )
            return _FakeResponse('{"should_rewrite": false, "issues": []}')

    context = _llm_context()
    _seed_success_ledger(session, context=context, call_id="llmcall_auto_test")
    llm_auto_critique(
        "SCENE TEXT",
        session=session,
        llm_runner=_Runner(),
        llm_context=context,
    )
    assert captured["task_name"] == "auto_critique_llm"
    assert captured["context"] is context


def test_auto_critique_called_product_normalizes_context_to_online(session) -> None:
    from dataclasses import replace

    from novel_system.services.auto_critique import llm_auto_critique

    captured: dict = {}

    class _Runner:
        def run_task(self, *, task_name, prompt_text, system_prompt, context):
            captured["context"] = context
            return _FakeResponse('{"should_rewrite": false, "issues": []}')

    supplied = replace(
        _llm_context(),
        provider_execution_mode="offline_deterministic",
    )
    _seed_success_ledger(session, context=supplied, call_id="llmcall_auto_test")
    result = llm_auto_critique(
        "SCENE TEXT",
        session=session,
        llm_runner=_Runner(),
        llm_context=supplied,
    )

    assert result.outcome == "completed"
    assert captured["context"].provider_execution_mode == "online"


def test_auto_critique_llm_aliases_to_existing_route(session) -> None:
    """The §8 critic borrows the soft_qc route via run_task alias — no dedicated node, so
    it never pollutes active node_routing nor trips the sync-activation guard."""
    from novel_system.services.llm_task_runner import _AD_HOC_ROUTE_ALIASES, LLMNodeRunner

    assert _AD_HOC_ROUTE_ALIASES["auto_critique_llm"] == "soft_qc"
    cfg = LLMNodeRunner(session).task_config("soft_qc")
    assert getattr(cfg, "model", None), "soft_qc (critic alias target) did not resolve"


def test_llm_critique_degrades_to_rule_only_without_runner() -> None:
    from novel_system.services.auto_critique import auto_critique, llm_auto_critique

    text = "她觉得心里一紧，她意识到自己其实早就明白了一切。"
    rule = auto_critique(text)
    hybrid = llm_auto_critique(text, llm_runner=None)
    assert hybrid.directives == rule.directives
    assert hybrid.should_rewrite == rule.should_rewrite


def test_llm_critique_with_runner_but_no_context_fails_before_runner_io() -> None:
    from novel_system.services.auto_critique import llm_auto_critique
    from novel_system.services.llm_accounting import LLMAccountingRejected

    calls: list[str] = []

    class _Runner:
        def run_task(self, **_kwargs):
            calls.append("provider")

    with pytest.raises(LLMAccountingRejected) as rejected:
        llm_auto_critique("prose", llm_runner=_Runner())

    assert rejected.value.code == "LLM_ACCOUNTING_CONTEXT_REQUIRED"
    assert calls == []


def test_llm_critique_merges_editor_issues(session) -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    class _Runner:
        def run_task(self, *, task_name, prompt_text, system_prompt, context):
            return _FakeResponse(
                '{"should_rewrite": true, "issues": ['
                '{"dimension": "conflict_credibility", '
                '"directive": "raise the cost of the reconciliation", '
                '"evidence": "they simply hugged and moved on"}]}'
            )

    context = _llm_context()
    _seed_success_ledger(session, context=context, call_id="llmcall_auto_test")
    result = llm_auto_critique(
        "Some otherwise clean prose.",
        session=session,
        llm_runner=_Runner(),
        llm_context=context,
    )
    assert result.should_rewrite is True
    assert any("conflict_credibility" in directive for directive in result.directives)


def test_llm_critique_unaccounted_runner_error_is_not_degraded(session) -> None:
    """A provider-looking failure without a durable parent is an integrity ambiguity."""
    from novel_system.services.auto_critique import llm_auto_critique
    from novel_system.services.llm_accounting import LLMAccountingError

    class _BrokenRunner:
        def run_task(self, *, task_name, prompt_text, system_prompt, context):
            raise RuntimeError("LLM down")

    with pytest.raises(LLMAccountingError) as raised:
        llm_auto_critique(
            "她觉得很难过，她意识到自己错了。",
            llm_runner=_BrokenRunner(),
            llm_context=_llm_context(),
            session=session,
        )
    assert raised.value.code == "LLM_ACCOUNTING_ADVISORY_FAILURE_UNTRACKED"


def test_auto_critique_gate_resolves_runner_when_flag_enabled(session, monkeypatch) -> None:
    """§8 gate teeth (real code path): with llm_enabled + llm_auto_critique_enabled, the
    orchestrator's extracted gate resolves the real critic runner. This drives the same
    method run_scene calls — deleting the `and llm_auto_critique_enabled` clause flips the
    OFF test below to red."""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED", "true")
    from novel_system.services.orchestrator import Orchestrator

    orch = Orchestrator(session)
    sentinel = object()
    orch.llm_runner = sentinel
    assert orch._resolve_auto_critique_runner() is sentinel


def test_auto_critique_gate_suppressed_when_flag_disabled(session, monkeypatch) -> None:
    """§8 default: flag OFF -> critic runner is None -> llm_auto_critique == rule-only,
    and the injected runner is never called."""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED", "false")
    from novel_system.services.auto_critique import auto_critique, llm_auto_critique
    from novel_system.services.orchestrator import Orchestrator

    calls: list = []

    class _Runner:
        def run_task(self, *, task_name, prompt_text, system_prompt, context):
            calls.append(task_name)
            return _FakeResponse('{"should_rewrite": false, "issues": []}')

    orch = Orchestrator(session)
    orch.llm_runner = _Runner()
    runner = orch._resolve_auto_critique_runner()
    assert runner is None

    text = "她觉得心里一紧，她意识到自己其实早就明白了一切。"
    assert llm_auto_critique(text, llm_runner=runner).directives == auto_critique(text).directives
    assert calls == []


def test_llm_critique_no_call_reason_distinguishes_budget_gate() -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    result = llm_auto_critique(
        "prose",
        llm_runner=None,
        llm_context=_owned_llm_context(),
        not_invoked_reason="budget_or_candidate_cap",
    )

    assert result.outcome == "not_invoked"
    assert result.reason == "budget_or_candidate_cap"
    assert result.llm_call_id is None
    assert result.execution_id == "exec-auto"
    assert result.execution_step_key == "soft_qc:auto_critique:0"
    assert result.run_job_id == "job-auto"


def test_llm_critique_completed_empty_is_distinct_from_parse_failure(session) -> None:
    from novel_system.services.auto_critique import auto_critique, llm_auto_critique

    class _Runner:
        def __init__(self, response):
            self.response = response

        def run_task(self, **_kwargs):
            return self.response

    context = _llm_context()
    _seed_success_ledger(session, context=context, call_id="llmcall_auto_test")
    _seed_success_ledger(session, context=context, call_id="llmcall_parse")
    completed = llm_auto_critique(
        "clean prose",
        session=session,
        llm_runner=_Runner(_FakeResponse('{"should_rewrite": false, "issues": []}')),
        llm_context=context,
    )
    malformed = llm_auto_critique(
        "clean prose",
        session=session,
        llm_runner=_Runner(_FakeResponse("not-json", llm_call_id="llmcall_parse")),
        llm_context=context,
    )

    assert completed.outcome == "completed"
    assert completed.reason is None
    assert completed.rule_directives == auto_critique("clean prose").directives
    assert malformed.outcome == "parse_failed"
    assert malformed.reason == "invalid_llm_response"
    assert malformed.error_code == "LLM_CRITIQUE_RESPONSE_INVALID"
    assert malformed.llm_call_id == "llmcall_parse"


@pytest.mark.parametrize(
    "payload",
    [
        '{"should_rewrite": true, "issues": []}',
        (
            '{"should_rewrite": false, "issues": ['
            '{"dimension": "pacing", "directive": "tighten the turn", "evidence": "lag"}]}'
        ),
        (
            '{"should_rewrite": true, "issues": ['
            '{"dimension": "pacing", "directive": "   ", "evidence": "lag"}]}'
        ),
    ],
)
def test_llm_critique_inconsistent_or_empty_directive_schema_is_parse_failed(
    session,
    payload: str,
) -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    class _Runner:
        def run_task(self, **_kwargs):
            return _FakeResponse(payload)

    context = _llm_context()
    _seed_success_ledger(session, context=context, call_id="llmcall_auto_test")
    result = llm_auto_critique(
        "clean prose",
        session=session,
        llm_runner=_Runner(),
        llm_context=context,
    )

    assert result.outcome == "parse_failed"
    assert result.error_code == "LLM_CRITIQUE_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("error_code", "outcome"),
    [
        ("LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", "rejected_before_dispatch"),
        ("LLM_HTTP_REQUEST_FAILED", "provider_failed"),
    ],
)
def test_llm_critique_failure_envelope_preserves_parent_and_owner(
    session, error_code: str, outcome: str
) -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    context = _owned_llm_context()
    _seed_owned_scene(session)
    session.add(SceneRunState(scene_id=context.scene_id, scene_tokens_reserved=0))
    _seed_failure_ledger(session, context=context, outcome=outcome, error_code=error_code)
    error = _task_error(code=error_code, rejected=outcome == "rejected_before_dispatch")

    class _Runner:
        def run_task(self, **_kwargs):
            raise error

    result = llm_auto_critique(
        "prose",
        session=session,
        llm_runner=_Runner(),
        llm_context=context,
    )

    assert result.outcome == outcome
    assert result.llm_call_id == "llmcall_auto_failed"
    assert result.error_code == error_code
    assert result.execution_id == "exec-auto"
    assert result.execution_step_key == "soft_qc:auto_critique:0"
    assert result.run_job_id == "job-auto"


@pytest.mark.parametrize("tamper", [None, "dispatch", "status", "charge", "multiple"])
def test_llm_critique_rejected_physical_gate_ledger_is_strictly_validated(
    session,
    tamper: str | None,
) -> None:
    from novel_system.db.models import LlmCall, LlmCallAttempt
    from novel_system.services.auto_critique import llm_auto_critique
    from novel_system.services.llm_accounting import LLMAccountingError

    context = _owned_llm_context()
    _seed_owned_scene(session)
    error_code = "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED"
    session.add(SceneRunState(scene_id=context.scene_id, scene_tokens_reserved=0))
    parent = LlmCall(
        llm_call_id="llmcall_auto_failed",
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
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        estimated_tokens=23,
        reserved_tokens=29,
        budget_charged_tokens=0,
        latency_ms=0,
        usage_is_estimate=True,
        accounting_status="rejected",
        request_dispatched_at=None,
        settled_at="2026-07-14T00:00:01Z",
        error_code=error_code,
    )
    child = LlmCallAttempt(
        attempt_id="attempt_auto_rejected_gate_0",
        llm_call_id=parent.llm_call_id,
        provider_attempt_no=0,
        dispatch_kind="initial",
        request_max_output_tokens=128,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        estimated_tokens=23,
        reserved_tokens=29,
        budget_charged_tokens=0,
        latency_ms=0,
        usage_is_estimate=True,
        accounting_status="rejected",
        request_dispatched_at=None,
        settled_at="2026-07-14T00:00:01Z",
        error_code=error_code,
    )
    if tamper == "dispatch":
        child.request_dispatched_at = "2026-07-14T00:00:00Z"
    elif tamper == "status":
        child.accounting_status = "failed"
    elif tamper == "charge":
        child.prompt_tokens = 1
        child.total_tokens = 1
        child.budget_charged_tokens = 1
        parent.prompt_tokens = 1
        parent.total_tokens = 1
        parent.budget_charged_tokens = 1
    rows = [parent, child]
    if tamper == "multiple":
        rows.append(
            LlmCallAttempt(
                attempt_id="attempt_auto_rejected_gate_1",
                llm_call_id=parent.llm_call_id,
                provider_attempt_no=1,
                dispatch_kind="transport_retry",
                request_max_output_tokens=128,
                estimated_tokens=5,
                reserved_tokens=7,
                budget_charged_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                request_dispatched_at=None,
                settled_at="2026-07-14T00:00:02Z",
                error_code=error_code,
            )
        )
        parent.estimated_tokens += 5
        parent.reserved_tokens += 7
    session.add_all(rows)
    session.commit()
    error = _task_error(code=error_code, rejected=True)

    class _Runner:
        def run_task(self, **_kwargs):
            raise error

    if tamper is None:
        result = llm_auto_critique(
            "prose",
            session=session,
            llm_runner=_Runner(),
            llm_context=context,
        )
        assert result.outcome == "rejected_before_dispatch"
        assert result.llm_call_id == parent.llm_call_id
        assert result.error_code == error_code
        assert session.get(SceneRunState, context.scene_id).scene_tokens_reserved == 0
    else:
        with pytest.raises(LLMAccountingError) as raised:
            llm_auto_critique(
                "prose",
                session=session,
                llm_runner=_Runner(),
                llm_context=context,
            )
        assert raised.value.code == "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID"


def test_llm_critique_route_rejection_is_classified_from_rejected_parent_not_key_error(
    session,
) -> None:
    from novel_system.services.auto_critique import llm_auto_critique
    from novel_system.services.llm_task_runner import LLMNodeExecutionError

    context = _owned_llm_context()
    _seed_failure_ledger(
        session,
        context=context,
        outcome="rejected_before_dispatch",
        error_code="LLM_ROUTE_NOT_CONFIGURED",
    )
    error = LLMNodeExecutionError(
        llm_call_id="llmcall_auto_failed",
        error_code="LLM_ROUTE_NOT_CONFIGURED",
        message="route missing",
        request_summary={},
        response_summary={},
        original_error=KeyError("soft_qc"),
    )

    class _Runner:
        def run_task(self, **_kwargs):
            raise error

    result = llm_auto_critique(
        "prose",
        session=session,
        llm_runner=_Runner(),
        llm_context=context,
    )

    assert result.outcome == "rejected_before_dispatch"
    assert result.llm_call_id == "llmcall_auto_failed"
    assert result.error_code == "LLM_ROUTE_NOT_CONFIGURED"


@pytest.mark.parametrize("tamper", [None, "rejected_prefix", "duplicate_initial"])
def test_llm_critique_failed_parent_wins_over_terminal_undispatched_rejection(
    session,
    tamper: str | None,
) -> None:
    from novel_system.db.models import LlmCall, LlmCallAttempt
    from novel_system.services.auto_critique import llm_auto_critique

    context = _owned_llm_context()
    terminal_error = "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED"
    session.add(
        LlmCall(
            llm_call_id="llmcall_auto_failed",
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
            prompt_tokens=11,
            completion_tokens=0,
            total_tokens=11,
            estimated_tokens=34,
            reserved_tokens=40,
            budget_charged_tokens=11,
            latency_ms=2,
            usage_is_estimate=True,
            accounting_status="failed",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:02Z",
            error_code=terminal_error,
        )
    )
    session.add_all(
        [
            LlmCallAttempt(
                attempt_id="attempt_auto_failed_0",
                llm_call_id="llmcall_auto_failed",
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=0,
                prompt_tokens=11,
                completion_tokens=0,
                total_tokens=11,
                estimated_tokens=11,
                reserved_tokens=11,
                budget_charged_tokens=11,
                latency_ms=2,
                usage_is_estimate=True,
                accounting_status="failed",
                request_dispatched_at="2026-07-14T00:00:00Z",
                settled_at="2026-07-14T00:00:01Z",
                error_code="LLM_HTTP_REQUEST_FAILED",
            ),
            LlmCallAttempt(
                attempt_id="attempt_auto_failed_1",
                llm_call_id="llmcall_auto_failed",
                provider_attempt_no=1,
                dispatch_kind="transport_retry",
                request_max_output_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=23,
                reserved_tokens=29,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                request_dispatched_at=None,
                settled_at="2026-07-14T00:00:02Z",
                error_code=terminal_error,
            ),
        ]
    )
    session.commit()
    if tamper == "rejected_prefix":
        first = session.get(LlmCallAttempt, "attempt_auto_failed_0")
        terminal = session.get(LlmCallAttempt, "attempt_auto_failed_1")
        first.accounting_status = "rejected"
        first.request_dispatched_at = None
        first.prompt_tokens = 0
        first.total_tokens = 0
        first.budget_charged_tokens = 0
        first.latency_ms = 0
        terminal.accounting_status = "failed"
        terminal.request_dispatched_at = "2026-07-14T00:00:01Z"
        parent = session.get(LlmCall, "llmcall_auto_failed")
        parent.prompt_tokens = 0
        parent.total_tokens = 0
        parent.budget_charged_tokens = 0
        parent.latency_ms = 0
        session.commit()
    elif tamper == "duplicate_initial":
        session.get(LlmCallAttempt, "attempt_auto_failed_1").dispatch_kind = "initial"
        session.commit()
    error = _task_error(code=terminal_error, rejected=True)

    class _Runner:
        def run_task(self, **_kwargs):
            raise error

    if tamper is not None:
        from novel_system.services.llm_accounting import LLMAccountingError

        with pytest.raises(LLMAccountingError) as invalid:
            llm_auto_critique(
                "prose",
                session=session,
                llm_runner=_Runner(),
                llm_context=context,
            )
        assert invalid.value.code == "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID"
        return

    result = llm_auto_critique(
        "prose",
        session=session,
        llm_runner=_Runner(),
        llm_context=context,
    )

    assert result.outcome == "provider_failed"
    assert result.llm_call_id == "llmcall_auto_failed"
    assert result.error_code == terminal_error


@pytest.mark.parametrize(
    "error_code",
    [
        "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
        "LLM_ACCOUNTING_HOOK_NOT_INVOKED",
        "RUN_CHECKPOINT_OUTPUT_MISSING",
        "RUN_OWNER_LEASE_LOST",
        "LLM_ACCOUNTING_HOOK_UNSUPPORTED",
        "LLM_OFFLINE_RESPONSE_INVALID",
        "LLM_OFFLINE_CAPABILITY_UNSUPPORTED",
    ],
)
def test_llm_critique_control_plane_failures_are_not_degraded(
    session, error_code: str
) -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    error = _task_error(code=error_code)
    if error_code == "LLM_ACCOUNTING_HOOK_UNSUPPORTED":
        _seed_failure_ledger(
            session,
            context=_owned_llm_context(),
            outcome="rejected_before_dispatch",
            error_code=error_code,
        )

    class _Runner:
        def run_task(self, **_kwargs):
            raise error

    with pytest.raises(type(error)) as raised:
        llm_auto_critique(
            "prose",
            session=session,
            llm_runner=_Runner(),
            llm_context=_owned_llm_context(),
        )
    assert raised.value is error


def test_llm_critique_success_without_parent_id_is_integrity_error(session) -> None:
    from novel_system.services.auto_critique import llm_auto_critique
    from novel_system.services.llm_accounting import LLMAccountingError

    class _Runner:
        def run_task(self, **_kwargs):
            return _FakeResponse('{"should_rewrite": false, "issues": []}', llm_call_id=None)

    with pytest.raises(LLMAccountingError) as raised:
        llm_auto_critique(
            "prose",
            session=session,
            llm_runner=_Runner(),
            llm_context=_owned_llm_context(),
        )
    assert raised.value.code == "LLM_ACCOUNTING_PARENT_ID_MISSING"


def test_llm_critique_requires_session_before_provider_io() -> None:
    from novel_system.services.auto_critique import llm_auto_critique
    from novel_system.services.llm_accounting import LLMAccountingRejected

    calls: list[str] = []

    class _Runner:
        def run_task(self, **_kwargs):
            calls.append("provider")

    with pytest.raises(LLMAccountingRejected) as raised:
        llm_auto_critique(
            "prose",
            llm_runner=_Runner(),
            llm_context=_llm_context(),
        )
    assert raised.value.code == "LLM_ACCOUNTING_SESSION_REQUIRED"
    assert calls == []


def test_llm_critique_offline_runner_is_explicit_no_call() -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    class _OfflineRunner:
        provider_execution_mode = "offline_deterministic"

        def run_task(self, **_kwargs):
            raise AssertionError("offline advisory pass must not call run_task")

    result = llm_auto_critique(
        "prose",
        llm_runner=_OfflineRunner(),
        llm_context=_llm_context(),
    )
    assert result.outcome == "not_invoked"
    assert result.reason == "offline_unsupported"
    assert result.llm_call_id is None
