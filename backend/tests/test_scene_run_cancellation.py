from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Thread

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SqlAlchemySession

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    LlmCall,
    LlmCallAttempt,
    OperationLog,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.scene_run_jobs import SceneRunJobService
from tests.test_scene_run_jobs import _create_chapter_and_scene


def _seed_owned_scene(session) -> None:
    """Create the authoritative owner chain required by ``SceneRunState``."""
    session.add_all(
        [
            StoryProject(
                project_id="PRJ01",
                title="Scene cancellation integration",
                outline_text="Test-owned outline",
            ),
            ChapterGoal(
                chapter_id="CH01",
                project_id="PRJ01",
                planned_scene_count=1,
                chapter_goal="Exercise scene-run cancellation",
            ),
            SceneCard(
                scene_id="SC01",
                chapter_id="CH01",
                project_id="PRJ01",
                scene_seq=1,
                scene_goal="Exercise cancellation boundaries",
                onstage_chars_json=[],
                beats_json=[],
            ),
        ]
    )
    session.flush()


def _create_queued_job(client) -> dict:
    _create_chapter_and_scene(client)
    response = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false")
    assert response.status_code == 200
    return response.json()["data"]


def test_queued_cancel_is_terminal_idempotent_and_fully_audited(client, session) -> None:
    job = _create_queued_job(client)

    first = client.post(
        f"/api/v1/run-jobs/{job['job_id']}/cancel",
        json={"reason": "author stopped this draft"},
    )
    second = client.post(f"/api/v1/run-jobs/{job['job_id']}/cancel")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["status"] == "cancelled"
    assert second.json()["data"]["status"] == "cancelled"
    assert first.json()["data"]["error_code"] == "RUN_JOB_CANCELLED_BY_AUTHOR"
    session.expire_all()
    state = session.get(SceneRunState, "CHJOB_SC01")
    assert state is not None and state.active_run_job_id is None
    events = list(
        session.scalars(
            select(OperationLog)
            .where(OperationLog.object_ref == job["job_id"])
            .order_by(OperationLog.operation_id)
        )
    )
    assert [event.event_type for event in events] == [
        "scene_run_cancel_requested",
        "scene_run_cancelled",
    ]
    assert events[0].payload_json["actor_ref"] == "operator"
    assert events[0].payload_json["reason"] == "author stopped this draft"


def test_running_cancel_only_requests_and_does_not_clear_owner_or_active_lock(client, session) -> None:
    job = _create_queued_job(client)
    owner = SceneRunJobService(session).claim_running(
        job["job_id"],
        worker_id="worker-a",
        current_step="neutral_running",
        lease_seconds=120,
    )
    session.commit()

    response = client.post(f"/api/v1/run-jobs/{job['job_id']}/cancel")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancel_requested"
    session.expire_all()
    persisted = session.get(ChapterRunJob, job["job_id"])
    state = session.get(SceneRunState, "CHJOB_SC01")
    assert persisted is not None
    assert persisted.worker_id == owner.worker_id
    assert persisted.attempt_no == owner.attempt_no
    assert persisted.finished_at is None
    assert state is not None and state.active_run_job_id == job["job_id"]
    assert [
        event.event_type
        for event in session.scalars(
            select(OperationLog).where(OperationLog.object_ref == job["job_id"])
        )
    ] == ["scene_run_cancel_requested"]


@pytest.mark.parametrize("status", ["completed", "failed", "blocked"])
def test_cancel_rejects_non_cancel_terminal_status_with_stable_details(
    session,
    status: str,
) -> None:
    _seed_owned_scene(session)
    job = ChapterRunJob(
        job_id=f"terminal-cancel-{status}",
        scene_id="SC01",
        status=status,
        job_type="scene_run_full",
        payload_json={"current_step": status},
        result_summary_json={"current_step": status},
    )
    session.add(job)
    session.commit()

    with pytest.raises(DomainError) as exc_info:
        SceneRunJobService(session).request_cancel(job.job_id, actor_ref="author")

    assert exc_info.value.code == "RUN_JOB_CANCEL_CONFLICT"
    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"job_id": job.job_id, "status": status}


def test_terminal_cleanup_is_fenced_to_its_own_active_job(session) -> None:
    _seed_owned_scene(session)
    state = SceneRunState(scene_id="SC01", active_run_job_id="new-job")
    old_job = ChapterRunJob(
        job_id="old-job",
        scene_id="SC01",
        status="running",
        job_type="scene_run_full",
        worker_id="old-worker",
        attempt_no=1,
    )
    session.add_all([state, old_job])
    session.commit()
    owner = SceneRunJobService(session).owner_for(old_job)

    SceneRunJobService(session).mark_finished(
        old_job,
        status="completed",
        current_step="archived",
        result={"scene_status": "archived"},
        owner=owner,
    )
    session.commit()

    session.expire_all()
    assert session.get(SceneRunState, "SC01").active_run_job_id == "new-job"


def test_concurrent_scene_job_creation_has_one_active_lock_winner(client) -> None:
    _create_chapter_and_scene(client)
    barrier = Barrier(2)

    def contender() -> tuple[str, str]:
        db = SessionLocal()
        try:
            service = SceneRunJobService(db)
            barrier.wait(timeout=10)
            try:
                job = service.create_job("CHJOB_SC01")
                db.commit()
                return "winner", job.job_id
            except DomainError as exc:
                db.rollback()
                return "loser", exc.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: contender(), range(2)))

    assert [kind for kind, _value in results].count("winner") == 1
    assert ("loser", "RUN_JOB_IN_PROGRESS") in results
    db = SessionLocal()
    try:
        active_id = db.get(SceneRunState, "CHJOB_SC01").active_run_job_id
        jobs = list(
            db.scalars(
                select(ChapterRunJob).where(
                    ChapterRunJob.scene_id == "CHJOB_SC01",
                    ChapterRunJob.status == "queued",
                )
            )
        )
        assert len(jobs) == 1
        assert active_id == jobs[0].job_id
    finally:
        db.close()


def test_latest_job_endpoint_is_database_authoritative(client) -> None:
    job = _create_queued_job(client)
    requested = client.post(f"/api/v1/run-jobs/{job['job_id']}/cancel")
    assert requested.status_code == 200

    latest = client.get("/api/v1/scenes/CHJOB_SC01/run/jobs/latest")

    assert latest.status_code == 200
    data = latest.json()["data"]
    assert data["job_id"] == job["job_id"]
    assert data["status"] == "cancelled"
    assert data["current_step"] == "cancelled"


def test_expired_cancel_requested_recovery_has_one_owner_cas_winner(session) -> None:
    from novel_system.services.scene_run_jobs import recover_expired_cancel_requested_jobs

    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    _seed_owned_scene(session)
    session.add_all(
        [
            SceneRunState(
                scene_id="SC01",
                active_run_job_id="recover-cancel",
                active_execution_id="recover-cancel",
                run_execution_status="active",
                run_checkpoint_json={"execution_id": "recover-cancel"},
            ),
            ChapterRunJob(
                job_id="recover-cancel",
                scene_id="SC01",
                status="cancel_requested",
                job_type="scene_run_full",
                worker_id="dead-worker",
                attempt_no=1,
                lease_expires_at=expired,
            ),
        ]
    )
    session.commit()
    barrier = Barrier(2)

    def sweep(worker_id: str) -> int:
        db = SessionLocal()
        try:
            barrier.wait(timeout=10)
            recovered = recover_expired_cancel_requested_jobs(db, worker_id=worker_id)
            db.commit()
            return len(recovered)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        wins = list(pool.map(sweep, ("recovery-a", "recovery-b")))

    assert sorted(wins) == [0, 1]
    session.expire_all()
    job = session.get(ChapterRunJob, "recover-cancel")
    state = session.get(SceneRunState, "SC01")
    assert job.status == "cancelled"
    assert job.error_code == "RUN_JOB_CANCELLED_BY_AUTHOR"
    assert state.active_run_job_id is None
    assert state.active_execution_id == "recover-cancel"
    assert state.run_execution_status == "cancelled"
    assert state.run_checkpoint == "cancelled"
    assert state.run_checkpoint_json["node_key"] == "cancelled"
    from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

    next_claim = SceneRunCheckpointService(session).acquire_execution("SC01", "new-execution")
    assert next_claim.execution_id == "new-execution"


def test_valid_cancel_requested_lease_is_not_recovered(session) -> None:
    from novel_system.services.scene_run_jobs import recover_expired_cancel_requested_jobs

    valid = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    _seed_owned_scene(session)
    session.add_all(
        [
            SceneRunState(scene_id="SC01", active_run_job_id="valid-cancel"),
            ChapterRunJob(
                job_id="valid-cancel",
                scene_id="SC01",
                status="cancel_requested",
                job_type="scene_run_full",
                worker_id="live-worker",
                attempt_no=1,
                lease_expires_at=valid,
            ),
        ]
    )
    session.commit()

    assert recover_expired_cancel_requested_jobs(session, worker_id="recovery") == []
    session.commit()
    session.expire_all()
    assert session.get(ChapterRunJob, "valid-cancel").status == "cancel_requested"
    assert session.get(SceneRunState, "SC01").active_run_job_id == "valid-cancel"


def test_cancelled_checkpoint_idempotency_is_fenced_to_active_execution(session) -> None:
    from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService

    _seed_owned_scene(session)
    session.add(
        SceneRunState(
            scene_id="SC01",
            active_execution_id="current-execution",
            run_execution_status="cancelled",
            run_checkpoint="cancelled",
            run_checkpoint_json={
                "execution_id": "current-execution",
                "node_key": "cancelled",
                "artifact_refs": {},
                "artifact_hashes": {},
            },
        )
    )
    session.commit()

    with pytest.raises(DomainError) as exc_info:
        SceneRunCheckpointService(session).mark_cancelled("SC01", "stale-execution")

    assert exc_info.value.code == "RUN_EXECUTION_SUPERSEDED"


@pytest.mark.parametrize(
    "hard_status",
    ["usage_exceeds_reservation", "accounting_integrity_blocked"],
)
def test_author_cancel_preserves_accounting_hard_checkpoint_fence(
    session,
    hard_status: str,
) -> None:
    job_id = f"cancel-hard-fence-{hard_status}"
    _seed_owned_scene(session)
    session.add_all(
        [
            SceneRunState(
                scene_id="SC01",
                active_run_job_id=job_id,
                active_execution_id=job_id,
                run_execution_status=hard_status,
                run_checkpoint="budget_ready",
                run_checkpoint_json={
                    "execution_id": job_id,
                    "node_key": "budget_ready",
                    "artifact_refs": {},
                    "artifact_hashes": {},
                },
            ),
            ChapterRunJob(
                job_id=job_id,
                scene_id="SC01",
                chapter_id="CH01",
                status="cancel_requested",
                job_type="scene_run_full",
                worker_id="hard-fence-worker",
                attempt_no=1,
                lease_expires_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
            ),
        ]
    )
    session.commit()

    service = SceneRunJobService(session)
    job = service.get_job(job_id)
    service.mark_cancelled(service.owner_for(job))
    session.commit()

    session.expire_all()
    persisted_job = session.get(ChapterRunJob, job_id)
    state = session.get(SceneRunState, "SC01")
    assert persisted_job.status == "cancelled"
    assert persisted_job.error_code == "RUN_JOB_CANCELLED_BY_AUTHOR"
    assert state.active_run_job_id is None
    assert state.active_execution_id == job_id
    assert state.run_execution_status == hard_status
    assert state.run_checkpoint == "budget_ready"
    assert state.run_checkpoint_json["node_key"] == "budget_ready"
    cancelled_events = list(
        session.scalars(
            select(OperationLog).where(
                OperationLog.object_ref == job_id,
                OperationLog.event_type == "scene_run_cancelled",
            )
        )
    )
    assert len(cancelled_events) == 1


def test_cancel_before_next_reservation_makes_zero_provider_calls(session) -> None:
    from novel_system.services.llm_accounting import (
        LLMAccountingRejected,
        LLMCallContext,
        OnlineAccountedExecution,
        execute_accounted_call,
    )
    from novel_system.services.llm_client import LLMRequest

    _seed_owned_scene(session)
    session.add_all(
        [
            SceneRunState(
                scene_id="SC01",
                scene_token_budget=10000,
                scene_tokens_used=0,
                scene_tokens_reserved=0,
                provider_attempt_budget=10,
                provider_attempts_used=0,
                attempt_budget=10,
                total_attempt_count=0,
                active_run_job_id="job-cancel-before-claim",
            ),
            ChapterRunJob(
                job_id="job-cancel-before-claim",
                scene_id="SC01",
                status="cancel_requested",
                job_type="scene_run_full",
                worker_id="worker-a",
                attempt_no=1,
                lease_expires_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
            ),
        ]
    )
    session.commit()

    class NeverCalled(OnlineAccountedExecution):
        calls = 0

        def generate_accounted(self, request, *, accounting_hook):  # noqa: ANN001
            self.calls += 1
            raise AssertionError("cancelled next node must not reach provider")

    context = LLMCallContext(
        scope_type="scene",
        scope_id="SC01",
        scene_id="SC01",
        chapter_id="CH01",
        project_id="PRJ01",
        node_id="neutral_draft",
        step="draft",
        run_job_id="job-cancel-before-claim",
        execution_id="exec-cancel-before-claim",
        execution_step_key="neutral_draft:0",
    )
    request = LLMRequest(
        model="test",
        messages=[{"role": "user", "content": "draft"}],
        temperature=0,
        max_output_tokens=32,
        response_format="text",
        provider="openai_compatible",
        node_id="neutral_draft",
    )
    provider = NeverCalled()

    with pytest.raises(LLMAccountingRejected) as exc_info:
        execute_accounted_call(session, provider, request, context)

    assert exc_info.value.code == "RUN_JOB_CANCELLED_BY_AUTHOR"
    assert provider.calls == 0
    assert session.query(LlmCallAttempt).count() == 0
    assert session.get(SceneRunState, "SC01").scene_tokens_reserved == 0


def test_running_cancel_settles_current_call_preserves_product_and_blocks_next(session) -> None:
    from novel_system.services.llm_accounting import (
        LLMAccountingRejected,
        LLMCallContext,
        OnlineAccountedExecution,
        execute_accounted_call,
    )
    from novel_system.services.llm_client import LLMRequest, LLMResponse

    _seed_owned_scene(session)
    job = ChapterRunJob(
        job_id="job-cancel-during-provider",
        scene_id="SC01",
        chapter_id="CH01",
        status="running",
        job_type="scene_run_full",
        worker_id="worker-a",
        attempt_no=1,
        lease_expires_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
    )
    session.add_all(
        [
            SceneRunState(
                scene_id="SC01",
                scene_token_budget=10000,
                scene_tokens_used=0,
                scene_tokens_reserved=0,
                provider_attempt_budget=10,
                provider_attempts_used=0,
                attempt_budget=10,
                total_attempt_count=0,
                active_run_job_id=job.job_id,
            ),
            job,
        ]
    )
    session.commit()
    dispatched = Event()
    release_provider = Event()

    class BlockingProvider(OnlineAccountedExecution):
        calls = 0

        def generate_accounted(self, request, *, accounting_hook):  # noqa: ANN001
            self.calls += 1
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            dispatched.set()
            assert release_provider.wait(timeout=10)
            response = LLMResponse(
                text="current product",
                model="test",
                provider="openai_compatible",
                request_id="provider-current",
                finish_reason="stop",
                structured_output=None,
                response_format="text",
                usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                raw_usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                raw_response={"id": "provider-current"},
                usage_present=True,
                usage_complete=True,
            )
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
            return response

    request = LLMRequest(
        model="test",
        messages=[{"role": "user", "content": "draft"}],
        temperature=0,
        max_output_tokens=32,
        response_format="text",
        provider="openai_compatible",
        node_id="neutral_draft",
    )
    base_context = LLMCallContext(
        scope_type="scene",
        scope_id="SC01",
        scene_id="SC01",
        chapter_id="CH01",
        project_id="PRJ01",
        node_id="neutral_draft",
        step="draft",
        run_job_id=job.job_id,
        execution_id="exec-cancel-during-provider",
        execution_step_key="neutral_draft:0",
    )
    provider = BlockingProvider()
    worker_error: list[BaseException] = []

    def current_node() -> None:
        db = SessionLocal()
        try:
            response = execute_accounted_call(db, provider, request, base_context)
            db.add(
                SceneDraft(
                    row_id="draft-cancel-current",
                    scene_id="SC01",
                    chapter_id="CH01",
                    stage="neutral",
                    content=response.text,
                    source_bundle_id="bundle",
                    source_bundle_hash="hash",
                )
            )
            db.commit()
        except BaseException as exc:  # pragma: no cover - assertion reports worker boundary
            worker_error.append(exc)
        finally:
            db.close()

    worker = Thread(target=current_node)
    worker.start()
    assert dispatched.wait(timeout=10)
    canceller = SessionLocal()
    try:
        SceneRunJobService(canceller).request_cancel(job.job_id, actor_ref="author")
        canceller.commit()
    finally:
        canceller.close()
    release_provider.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert worker_error == [], repr(
        [(error, error.__cause__, error.__context__) for error in worker_error]
    )
    session.expire_all()
    assert provider.calls == 1
    assert session.get(SceneDraft, "draft-cancel-current").content == "current product"
    current_call = session.scalar(
        select(LlmCall).where(LlmCall.execution_step_key == "neutral_draft:0")
    )
    assert current_call.accounting_status == "settled"
    assert current_call.total_tokens == 5
    assert current_call.run_job_id == job.job_id
    assert current_call.execution_id == "exec-cancel-during-provider"
    assert current_call.step == "draft"
    assert current_call.execution_step_key == "neutral_draft:0"
    assert session.get(ChapterRunJob, job.job_id).status == "cancel_requested"

    next_context = LLMCallContext(
        scope_type="scene",
        scope_id="SC01",
        scene_id="SC01",
        chapter_id="CH01",
        project_id="PRJ01",
        node_id="hard_qc",
        step="hard_qc",
        run_job_id=job.job_id,
        execution_id="exec-cancel-during-provider",
        execution_step_key="hard_qc:0",
    )
    with pytest.raises(LLMAccountingRejected) as exc_info:
        execute_accounted_call(session, provider, request, next_context)
    assert exc_info.value.code == "RUN_JOB_CANCELLED_BY_AUTHOR"
    assert provider.calls == 1


def test_reservation_claim_and_cancel_start_on_barrier_have_one_linearized_outcome(session) -> None:
    from novel_system.services.llm_accounting import (
        LLMAccountingRejected,
        LLMCallContext,
        OnlineAccountedExecution,
        execute_accounted_call,
    )
    from novel_system.services.llm_client import LLMRequest, LLMResponse

    job_id = "job-reservation-cancel-race"
    _seed_owned_scene(session)
    session.add_all(
        [
            SceneRunState(
                scene_id="SC01",
                scene_token_budget=10000,
                scene_tokens_used=0,
                scene_tokens_reserved=0,
                provider_attempt_budget=10,
                provider_attempts_used=0,
                attempt_budget=10,
                total_attempt_count=0,
                active_run_job_id=job_id,
            ),
            ChapterRunJob(
                job_id=job_id,
                scene_id="SC01",
                chapter_id="CH01",
                status="running",
                job_type="scene_run_full",
                worker_id="race-worker",
                attempt_no=1,
                lease_expires_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
            ),
        ]
    )
    session.commit()
    start = Barrier(2)
    physical_posts: list[str] = []
    worker_errors: list[BaseException] = []

    class RacingProvider(OnlineAccountedExecution):
        def generate_accounted(self, request, *, accounting_hook):  # noqa: ANN001
            start.wait(timeout=10)
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            physical_posts.append("POST")
            response = LLMResponse(
                text="claimed node",
                model="test",
                provider="openai_compatible",
                request_id="provider-race",
                finish_reason="stop",
                structured_output=None,
                response_format="text",
                usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                raw_usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                raw_response={"id": "provider-race"},
                usage_present=True,
                usage_complete=True,
            )
            accounting_hook.after_response(
                handle,
                request=request,
                response=response,
                latency_ms=1,
            )
            return response

    request = LLMRequest(
        model="test",
        messages=[{"role": "user", "content": "draft"}],
        temperature=0,
        max_output_tokens=32,
        response_format="text",
        provider="openai_compatible",
        node_id="neutral_draft",
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="SC01",
        scene_id="SC01",
        chapter_id="CH01",
        project_id="PRJ01",
        node_id="neutral_draft",
        step="draft",
        run_job_id=job_id,
        execution_id="exec-reservation-cancel-race",
        execution_step_key="neutral_draft:0",
    )

    def run_node() -> None:
        db = SessionLocal()
        try:
            execute_accounted_call(db, RacingProvider(), request, context)
        except BaseException as exc:  # asserted below
            worker_errors.append(exc)
        finally:
            db.close()

    worker = Thread(target=run_node)
    worker.start()
    start.wait(timeout=10)
    canceller = SessionLocal()
    try:
        SceneRunJobService(canceller).request_cancel(job_id, actor_ref="author")
        canceller.commit()
    finally:
        canceller.close()
    worker.join(timeout=10)

    assert not worker.is_alive()
    session.expire_all()
    assert session.get(ChapterRunJob, job_id).status == "cancel_requested"
    assert len(physical_posts) in {0, 1}
    attempts = list(session.scalars(select(LlmCallAttempt)))
    if physical_posts:
        assert worker_errors == []
        assert len(attempts) == 1
        assert attempts[0].accounting_status == "settled"
    else:
        assert len(worker_errors) == 1
        assert isinstance(worker_errors[0], LLMAccountingRejected)
        assert worker_errors[0].code == "RUN_JOB_CANCELLED_BY_AUTHOR"
        assert attempts == []


def test_worker_confirms_cancel_only_after_current_product_commit(client, session, monkeypatch) -> None:
    from novel_system.services import scene_run_jobs as job_module

    job = _create_queued_job(client)

    class ProductThenCancelOrchestrator:
        def __init__(self, worker_session) -> None:
            self.session = worker_session

        def run_scene(self, scene_id: str, *args, **kwargs) -> dict:  # noqa: ANN002, ANN003
            canceller = SessionLocal()
            try:
                SceneRunJobService(canceller).request_cancel(job["job_id"], actor_ref="author")
                canceller.commit()
            finally:
                canceller.close()
            self.session.add(
                SceneDraft(
                    row_id="draft-worker-cancel-boundary",
                    scene_id=scene_id,
                    chapter_id="CHJOB",
                    stage="neutral",
                    content="provider result committed before cancellation boundary",
                    source_bundle_id="bundle",
                    source_bundle_hash="hash",
                )
            )
            self.session.commit()
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", ProductThenCancelOrchestrator)

    job_module._run_scene_job_worker(job["job_id"])

    session.expire_all()
    persisted = session.get(ChapterRunJob, job["job_id"])
    assert persisted.status == "cancelled"
    assert persisted.error_code == "RUN_JOB_CANCELLED_BY_AUTHOR"
    assert session.get(SceneDraft, "draft-worker-cancel-boundary") is not None
    assert session.get(SceneRunState, "CHJOB_SC01").active_run_job_id is None


@pytest.mark.parametrize(
    ("error_code", "expected_status", "expected_event"),
    [
        ("LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", "blocked", "scene_run_budget_blocked"),
        ("LLM_PROVIDER_TRANSPORT_ERROR", "failed", "scene_run_failed"),
    ],
)
def test_budget_and_provider_failures_have_distinct_terminal_audits(
    client,
    session,
    monkeypatch,
    error_code: str,
    expected_status: str,
    expected_event: str,
) -> None:
    from novel_system.services import scene_run_jobs as job_module

    job = _create_queued_job(client)

    class FailingOrchestrator:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, *_args, **_kwargs) -> dict:
            raise DomainError(error_code, "controlled failure")

    monkeypatch.setattr(job_module, "Orchestrator", FailingOrchestrator)
    job_module._run_scene_job_worker(job["job_id"])

    session.expire_all()
    persisted = session.get(ChapterRunJob, job["job_id"])
    assert persisted.status == expected_status
    assert persisted.error_code == error_code
    events = list(
        session.scalars(
            select(OperationLog).where(OperationLog.object_ref == job["job_id"])
        )
    )
    assert [event.event_type for event in events] == [expected_event]


@pytest.mark.parametrize("dispatched", [False, True])
def test_expired_cancellation_reconciles_reservation_from_dispatch_truth(
    session,
    dispatched: bool,
) -> None:
    from novel_system.services.scene_run_jobs import recover_expired_cancel_requested_jobs

    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    dispatched_at = "2026-07-14T01:02:03+00:00" if dispatched else None
    _seed_owned_scene(session)
    session.add_all(
        [
            SceneRunState(
                scene_id="SC01",
                active_run_job_id="job-reconcile-cancel",
                scene_token_budget=1000,
                scene_tokens_used=0,
                scene_tokens_reserved=50,
                provider_attempt_budget=10,
                provider_attempts_used=1 if dispatched else 0,
                attempt_budget=10,
                total_attempt_count=0,
            ),
            ChapterRunJob(
                job_id="job-reconcile-cancel",
                scene_id="SC01",
                status="cancel_requested",
                job_type="scene_run_full",
                worker_id="dead-worker",
                attempt_no=1,
                lease_expires_at=expired,
            ),
            LlmCall(
                llm_call_id="call-reconcile-cancel",
                scope_type="scene",
                scope_id="SC01",
                scene_id="SC01",
                node_id="neutral_draft",
                step="draft",
                run_job_id="job-reconcile-cancel",
                execution_id="exec-reconcile-cancel",
                execution_step_key="neutral_draft:0",
                estimated_tokens=20,
                reserved_tokens=50,
                budget_charged_tokens=0,
                usage_is_estimate=True,
                accounting_status="reserved",
                request_dispatched_at=dispatched_at,
            ),
            LlmCallAttempt(
                attempt_id="attempt-reconcile-cancel",
                llm_call_id="call-reconcile-cancel",
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=8,
                estimated_tokens=20,
                reserved_tokens=50,
                budget_charged_tokens=0,
                usage_is_estimate=True,
                accounting_status="reserved",
                request_dispatched_at=dispatched_at,
            ),
        ]
    )
    session.commit()

    recovered = recover_expired_cancel_requested_jobs(session, worker_id="recovery")

    assert [item["job_id"] for item in recovered] == ["job-reconcile-cancel"]
    session.expire_all()
    call = session.get(LlmCall, "call-reconcile-cancel")
    attempt = session.get(LlmCallAttempt, "attempt-reconcile-cancel")
    state = session.get(SceneRunState, "SC01")
    if dispatched:
        assert call.accounting_status == "failed"
        assert call.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"
        assert attempt.accounting_status == "failed"
        assert state.scene_tokens_used == 20
    else:
        assert call.accounting_status == "released"
        assert attempt.accounting_status == "released"
        assert state.scene_tokens_used == 0
    assert state.scene_tokens_reserved == 0
    assert state.active_run_job_id is None


def test_request_cancel_is_not_durable_before_commit(client, session) -> None:
    job = _create_queued_job(client)
    service = SceneRunJobService(session)

    service.request_cancel(job["job_id"], actor_ref="author")

    with SessionLocal() as observer:
        assert observer.get(ChapterRunJob, job["job_id"]).status == "queued"
    session.rollback()
    session.expire_all()
    assert session.get(ChapterRunJob, job["job_id"]).status == "queued"


def test_current_owner_can_renew_after_cancel_request_to_finish_settlement(client, session) -> None:
    job = _create_queued_job(client)
    owner = SceneRunJobService(session).claim_running(
        job["job_id"],
        worker_id="worker-settling",
        current_step="neutral_running",
        lease_seconds=5,
    )
    session.commit()
    before = owner.lease_expires_at
    canceller = SessionLocal()
    try:
        SceneRunJobService(canceller).request_cancel(job["job_id"], actor_ref="author")
        canceller.commit()
    finally:
        canceller.close()

    after = owner.renew(lease_seconds=120)
    session.commit()

    assert after > before
    session.expire_all()
    persisted = session.get(ChapterRunJob, job["job_id"])
    assert persisted.status == "cancel_requested"
    assert persisted.worker_id == owner.worker_id
    assert persisted.attempt_no == owner.attempt_no


def test_claim_and_cancel_race_is_linearized_by_database_cas(client) -> None:
    job = _create_queued_job(client)
    barrier = Barrier(2)

    def claim() -> tuple[str, str]:
        db = SessionLocal()
        try:
            barrier.wait(timeout=10)
            try:
                SceneRunJobService(db).claim_running(
                    job["job_id"],
                    worker_id="race-worker",
                    current_step="neutral_running",
                    lease_seconds=60,
                )
                db.commit()
                return "claimed", "running"
            except DomainError as exc:
                db.rollback()
                return "claim_rejected", exc.code
        finally:
            db.close()

    def cancel() -> tuple[str, str]:
        db = SessionLocal()
        try:
            barrier.wait(timeout=10)
            try:
                current = SceneRunJobService(db).request_cancel(job["job_id"], actor_ref="author")
                db.commit()
                return "cancelled", current.status
            except DomainError as exc:
                db.rollback()
                return "cancel_rejected", exc.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_result = pool.submit(claim)
        cancel_result = pool.submit(cancel)
        results = [claim_result.result(), cancel_result.result()]

    db = SessionLocal()
    try:
        persisted = db.get(ChapterRunJob, job["job_id"])
        assert persisted.status in {"cancelled", "cancel_requested"}
        if persisted.status == "cancelled":
            assert ("claim_rejected", "RUN_JOB_NOT_CLAIMABLE") in results
            assert persisted.worker_id is None
        else:
            assert ("claimed", "running") in results
            assert persisted.worker_id == "race-worker"
    finally:
        db.close()


def test_endpoint_commit_failure_leaves_no_persisted_cancel(
    client,
    monkeypatch,
) -> None:
    job = _create_queued_job(client)
    original_commit = SqlAlchemySession.commit
    injected: list[str] = []

    def fail_the_commit_carrying_the_cancel(db) -> None:  # noqa: ANN001
        # 按内容认提交（不按第几次）：幂等占位、取消前的空提交都放行，只让写着「已取消」的那一次失败
        status = db.execute(
            select(ChapterRunJob.status).where(ChapterRunJob.job_id == job["job_id"])
        ).scalar_one_or_none()
        if status == "cancelled" and not injected:
            injected.append(status)
            raise RuntimeError("injected cancellation commit failure")
        original_commit(db)

    monkeypatch.setattr(SqlAlchemySession, "commit", fail_the_commit_carrying_the_cancel)

    response = client.post(f"/api/v1/run-jobs/{job['job_id']}/cancel")

    assert injected == ["cancelled"]
    # 幂等执行把任意异常收成稳定的 500（原文只进服务端日志）
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    db = SessionLocal()
    try:
        persisted = db.get(ChapterRunJob, job["job_id"])
        assert persisted.status == "queued"
        assert db.get(SceneRunState, "CHJOB_SC01").active_run_job_id == job["job_id"]
    finally:
        db.close()


def test_endpoint_database_busy_leaves_no_persisted_cancel(
    client,
    monkeypatch,
) -> None:
    from novel_system.services import scene_run_jobs as job_module

    job = _create_queued_job(client)

    def fail_begin_immediate(_session) -> None:  # noqa: ANN001
        raise OperationalError(
            "BEGIN IMMEDIATE",
            {},
            RuntimeError("database is locked"),
        )

    monkeypatch.setattr(job_module, "_begin_immediate", fail_begin_immediate)

    response = client.post(f"/api/v1/run-jobs/{job['job_id']}/cancel")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "DATABASE_BUSY",
        "message": "database is busy; retry after the current long-running operation finishes",
        "details": {"retryable": True},
    }

    db = SessionLocal()
    try:
        persisted = db.get(ChapterRunJob, job["job_id"])
        assert persisted.status == "queued"
        assert db.get(SceneRunState, "CHJOB_SC01").active_run_job_id == job["job_id"]
    finally:
        db.close()
