"""Scene-run checkpoint resume · provider owner lease, dispatch truth and the real pipeline."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    LlmCall,
    LlmCallAttempt,
    SceneBundle,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMClient, LLMRequest, LLMResponse
from novel_system.services.llm_task_runner import (
    UNBOUNDED_TIMEOUT_LEASE_SECONDS,
    _execution_owner_heartbeat,
    _execution_owner_lease_seconds,
    LLMNodeExecutionError,
    LLMNodeRunner,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.idempotency import owner_lease_grace_seconds, owner_lease_ttl_seconds
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

from tests.support.checkpoint_fakes import (
    _AccountedTestClient,
    _CountingGenerationClient,
    _FailBeforeNeutral,
    _FailBeforeHardQc,
    _response,
    _seed_resume_scene,
)

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def test_provider_owner_lease_tracks_each_request_timeout_and_restores_default(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    events: list[tuple[str, int]] = []

    class _TimeoutClient(_AccountedTestClient):
        def generate(self, request: LLMRequest) -> LLMResponse:
            events.append(("provider", int(request.timeout_seconds or 0)))
            return _response({"scene_text": "timeout lease"}, f"timeout-{request.timeout_seconds}")

    def _budget(**kwargs):  # noqa: ANN003, ANN202
        return {"budget": kwargs["base_budget"], "continuity_warning": {}}

    monkeypatch.setattr("novel_system.services.llm_task_runner.finalize_request_budget", _budget)

    def _task(timeout_seconds: int) -> SimpleNamespace:
        return SimpleNamespace(
            model="fake-model",
            temperature=0.2,
            max_output_tokens=128,
            response_format="json_object",
            provider="fake-provider",
            timeout_seconds=timeout_seconds,
            provider_id="provider-1",
            account_id="account-1",
            reasoning_level="medium",
            api_mode="responses",
            credential_mode=None,
            provider_options={},
        )

    routes = SimpleNamespace(
        node_routing={"short": _task(10), "long": _task(70)},
        task_routing={},
    )
    runner = LLMNodeRunner(session, llm_client=_TimeoutClient(), routing_config=routes)

    def _renew(*, lease_seconds: int) -> None:
        events.append(("lease", lease_seconds))

    token = begin_llm_execution("exec-timeout", lease_renewer=_renew)
    try:
        for node_id in ("short", "long"):
            runner.run(
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                bundle_id="bundle-timeout",
                bundle_hash="sha256:timeout",
                node_id=node_id,
                step=node_id,
                prompt={
                    "system_prompt": "system",
                    "token_budget": {},
                    "template_name": node_id,
                    "template_version": "v1",
                },
                user_prompt="user",
            )
    finally:
        end_llm_execution(token)

    grace = owner_lease_grace_seconds()
    default_ttl = owner_lease_ttl_seconds()
    assert events == [
        ("lease", max(default_ttl, 10 + grace)),
        ("provider", 10),
        ("lease", default_ttl),
        ("lease", max(default_ttl, 70 + grace)),
        ("provider", 70),
        ("lease", default_ttl),
    ]


def test_owner_lease_envelope_never_shrinks_default_and_covers_all_llm_retries(monkeypatch) -> None:
    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_ttl_seconds", lambda: 30)
    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_grace_seconds", lambda: 5)

    assert _execution_owner_lease_seconds(
        request_timeout_seconds=10,
        client=object(),
    ) == 30

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://provider.invalid/v1",
        api_key="test",
        timeout_seconds=70,
        max_retries=2,
        retry_backoff_seconds=1.0,
    )
    physical_attempts = (2 + 1) * (3 + 1)
    backoff_envelope = int((3 + 1) * 2 * 30 * 1.2)
    assert _execution_owner_lease_seconds(
        request_timeout_seconds=70,
        client=client,
    ) >= physical_attempts * 70 + backoff_envelope + 5


def test_owner_lease_envelope_survives_an_unbounded_request_timeout(monkeypatch) -> None:
    """不限时(0)不能退回默认 TTL:长任务会在调用中途丢租约并被二次执行。"""

    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_ttl_seconds", lambda: 30)
    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_grace_seconds", lambda: 5)

    unbounded = _execution_owner_lease_seconds(request_timeout_seconds=0, client=object())
    assert unbounded == UNBOUNDED_TIMEOUT_LEASE_SECONDS
    assert unbounded > 30


def test_provider_heartbeat_periodically_renews_with_a_detached_callback() -> None:
    class _Lease:
        def __init__(self) -> None:
            self.detached_renewals: list[int] = []

        def renew(self, *, lease_seconds: int) -> None:
            del lease_seconds

        def renew_detached(self, *, lease_seconds: int) -> None:
            self.detached_renewals.append(lease_seconds)

    lease = _Lease()
    token = begin_llm_execution("exec-heartbeat", lease_renewer=lease.renew)
    try:
        with _execution_owner_heartbeat(lease_seconds=3_600, interval_seconds=0.01):
            time.sleep(0.045)
    finally:
        end_llm_execution(token)

    assert len(lease.detached_renewals) >= 2
    assert set(lease.detached_renewals) == {3_600}


def test_dispatch_truth_allows_predispatch_retry_but_blocks_unknown_provider_outcome(session, monkeypatch) -> None:
    _seed_resume_scene(session)

    def _budget(**kwargs):  # noqa: ANN003, ANN202
        return {"budget": kwargs["base_budget"], "continuity_warning": {}}

    monkeypatch.setattr("novel_system.services.llm_task_runner.finalize_request_budget", _budget)
    task = SimpleNamespace(
        model="fake-model",
        temperature=0.2,
        max_output_tokens=128,
        response_format="json_object",
        provider="fake-provider",
        timeout_seconds=10,
        provider_id="provider-1",
        account_id="account-1",
        reasoning_level="medium",
        api_mode="responses",
        credential_mode=None,
        provider_options={},
    )
    routes = SimpleNamespace(node_routing={"dispatch-test": task}, task_routing={})
    prompt = {
        "system_prompt": "system",
        "token_budget": {},
        "template_name": "dispatch-test",
        "template_version": "v1",
    }

    class _MustNotDispatch(_AccountedTestClient):
        def generate(self, request):  # noqa: ANN001, ANN201
            raise AssertionError("provider must not be called")

    def _lost_before_dispatch(*, lease_seconds: int) -> None:
        raise DomainError("RUN_OWNER_LEASE_LOST", "lost before provider", status_code=409)

    token = begin_llm_execution("exec-predispatch", lease_renewer=_lost_before_dispatch)
    try:
        with pytest.raises(LLMNodeExecutionError):
            LLMNodeRunner(session, llm_client=_MustNotDispatch(), routing_config=routes).run(
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                bundle_id="bundle-dispatch",
                bundle_hash="sha256:dispatch",
                node_id="dispatch-test",
                step="dispatch-test",
                prompt=prompt,
                user_prompt="user",
            )
    finally:
        end_llm_execution(token)
    pre_calls = session.execute(
        select(LlmCall).where(LlmCall.execution_id == "exec-predispatch")
    ).scalars().all()
    assert pre_calls == []
    assert SceneRunCheckpointService(session).reconcile_step_output(
        scene_id="CH_RESUME_SC01",
        execution_id="exec-predispatch",
        execution_step_key="dispatch-test",
        output_exists=False,
    ) == "retry"

    class _UnknownProviderOutcome(_AccountedTestClient):
        def generate(self, request):  # noqa: ANN001, ANN201
            raise TimeoutError("provider outcome unknown")

    token = begin_llm_execution("exec-dispatched")
    try:
        with pytest.raises(LLMNodeExecutionError):
            LLMNodeRunner(session, llm_client=_UnknownProviderOutcome(), routing_config=routes).run(
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                bundle_id="bundle-dispatch",
                bundle_hash="sha256:dispatch",
                node_id="dispatch-test",
                step="dispatch-test",
                prompt=prompt,
                user_prompt="user",
            )
    finally:
        end_llm_execution(token)
    dispatched_call = session.execute(
        select(LlmCall).where(LlmCall.execution_id == "exec-dispatched")
    ).scalar_one()
    assert dispatched_call.request_dispatched_at is not None
    with pytest.raises(DomainError) as unknown:
        SceneRunCheckpointService(session).reconcile_step_output(
            scene_id="CH_RESUME_SC01",
            execution_id="exec-dispatched",
            execution_step_key="dispatch-test",
            output_exists=False,
        )
    assert unknown.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"


def test_real_pipeline_blocks_settled_ledger_before_repeating_neutral_provider(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:settled-narrow-window"
    with pytest.raises(RuntimeError, match="stop at bundle checkpoint"):
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "bundle_ready"
    session.add(
        LlmCall(
            llm_call_id="settled-neutral-window",
            scope_type="scene",
            scope_id="CH_RESUME_SC01",
            provider="fake",
            model="fake",
            node_id="neutral_draft",
            step="neutral_draft",
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            execution_id=execution_id,
            execution_step_key="neutral_draft",
            accounting_status="settled",
            request_dispatched_at="2026-07-13T00:00:00+00:00",
            settled_at="2026-07-13T00:00:01+00:00",
        )
    )
    session.commit()
    generation_client = _CountingGenerationClient()

    with pytest.raises(DomainError) as blocked:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert blocked.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert generation_client.requests == []


def test_checkpoint_rejects_tampered_frozen_bundle_snapshot(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:tampered-frozen-bundle"
    author_note = "冻结并保留这条作者指令。"
    with pytest.raises(RuntimeError, match="stop at bundle checkpoint"):
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
            author_note=author_note,
        )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "bundle_ready"
    bundle = session.get(SceneBundle, state.current_bundle_id)
    snapshot = dict(bundle.frozen_snapshot_json)
    inline = dict(snapshot["inline_digests"])
    inline["author_instruction"] = "持久层篡改后的作者指令。"
    snapshot["inline_digests"] = inline
    bundle.frozen_snapshot_json = snapshot
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
            author_note=author_note,
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert corrupt.value.details["bundle_integrity"]["error_code"] == "bundle_hash_mismatch"


def test_real_pipeline_releases_undispatched_reservation_then_calls_neutral_once(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:reserved-narrow-window"
    with pytest.raises(RuntimeError, match="stop at bundle checkpoint"):
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    state.scene_tokens_reserved = 20
    session.add(
        LlmCall(
            llm_call_id="reserved-neutral-window",
            scope_type="scene",
            scope_id="CH_RESUME_SC01",
            provider="fake",
            model="fake",
            node_id="neutral_draft",
            step="neutral_draft",
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            execution_id=execution_id,
            execution_step_key="neutral_draft",
            estimated_tokens=20,
            reserved_tokens=20,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id="attempt-reserved-neutral-window",
            llm_call_id="reserved-neutral-window",
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=10,
            estimated_tokens=20,
            reserved_tokens=20,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.commit()
    generation_client = _CountingGenerationClient()

    with pytest.raises(RuntimeError, match="stop after neutral retry"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=_FailBeforeHardQc(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    reserved_call = session.get(LlmCall, "reserved-neutral-window")
    assert reserved_call.accounting_status == "released"
    assert state.scene_tokens_reserved == 0
    assert state.run_checkpoint == "neutral_ready"
    assert len(generation_client.requests) == 1
