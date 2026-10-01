"""Scene-run checkpoint resume · execution ids, CAS ownership and ledger truth before the first checkpoint."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from novel_system.db.models import (
    ChapterGoal,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.errors import DomainError
from novel_system.db.session import SessionLocal
from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.scene_run_checkpoint import (
    RUN_CHECKPOINT_ORDER,
    SceneRunCheckpointService,
    chapter_scene_execution_id,
    idempotency_execution_id,
    scene_job_execution_id,
)

from tests.support.checkpoint_fakes import _seed_resume_scene

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def test_checkpoint_draft_invalid_ledger_returns_domain_error_instead_of_name_error(monkeypatch) -> None:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.session = SimpleNamespace(get=lambda _model, _row_id: SimpleNamespace())
    refs = {
        "neutral_row": "draft-row",
        "neutral_llm_call_id": "llm-call",
        "neutral_execution_step_key": "neutral_draft",
        "neutral_artifact_execution_id": "execution-id",
    }
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_artifact",
        lambda key, **_kwargs: refs[key],
    )
    monkeypatch.setattr(orchestrator, "_load_checkpoint_bundle", lambda _scene_id: {"bundle_id": "bundle"})
    monkeypatch.setattr(orchestrator, "_validate_artifact_execution_owner", lambda value: value)
    monkeypatch.setattr(
        orchestrator,
        "_validate_checkpoint_llm_output",
        lambda **_kwargs: SimpleNamespace(),
    )

    def invalid_ledger(_parent) -> None:
        raise LLMAccountingError("LEDGER_INVALID", "ledger invalid")

    monkeypatch.setattr(orchestrator, "_validate_settled_parent_ledger", invalid_ledger)

    with pytest.raises(DomainError) as exc_info:
        orchestrator._load_checkpoint_draft(
            "scene-id",
            ref_key="neutral_row",
            expected_stage="neutral",
            expected_node_at_least="neutral_draft",
            result_type="neutral",
        )

    assert exc_info.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert exc_info.value.message == "neutral checkpoint generation attempt ledger is invalid"
    assert exc_info.value.details == {"llm_call_id": "llm-call", "error_code": "LEDGER_INVALID"}


def _state(session, *, scene_id: str = "SC_CHECKPOINT") -> SceneRunState:
    project_id = f"P_{scene_id}"
    chapter_id = f"CH_{scene_id}"
    session.add(StoryProject(project_id=project_id, title="Checkpoint", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            planned_scene_count=1,
            chapter_goal="checkpoint",
        )
    )
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            scene_goal="checkpoint",
        )
    )
    state = SceneRunState(scene_id=scene_id)
    session.add(state)
    session.commit()
    return state


def test_execution_ids_are_stable_at_each_entrypoint() -> None:
    assert idempotency_execution_id("request-123") == "idempotency:request-123"
    assert scene_job_execution_id("scene_job_123") == "scene_job_123"
    assert chapter_scene_execution_id("chapter_job_123", "SC01") == "chapter_job_123:SC01"


def test_same_execution_resumes_after_last_durable_checkpoint(session) -> None:
    _state(session)
    checkpoints = SceneRunCheckpointService(session)

    first = checkpoints.acquire_execution("SC_CHECKPOINT", "idempotency:req-1")
    assert first.resumed is False
    assert first.next_node == "budget_ready"

    checkpoints.save_checkpoint(
        scene_id="SC_CHECKPOINT",
        execution_id="idempotency:req-1",
        node_key="budget_ready",
        artifact_refs={"budget_basis_hash": "sha256:budget"},
    )
    session.commit()

    resumed = checkpoints.acquire_execution("SC_CHECKPOINT", "idempotency:req-1")
    assert resumed.resumed is True
    assert resumed.last_node == "budget_ready"
    assert resumed.next_node == "planning_ready"
    assert resumed.checkpoint_json["artifact_refs"] == {"budget_basis_hash": "sha256:budget"}


def test_active_execution_blocks_competitor_and_terminal_execution_can_be_superseded(session) -> None:
    state = _state(session)
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(state.scene_id, "exec-a")
    session.commit()

    with pytest.raises(DomainError) as active_error:
        checkpoints.acquire_execution(state.scene_id, "exec-b")
    assert active_error.value.code == "RUN_EXECUTION_IN_PROGRESS"

    checkpoints.mark_failed(state.scene_id, "exec-a")
    session.commit()
    replacement = checkpoints.acquire_execution(state.scene_id, "exec-b")
    assert replacement.resumed is False
    session.commit()

    with pytest.raises(DomainError) as old_retry:
        checkpoints.acquire_execution(state.scene_id, "exec-a")
    assert old_retry.value.code == "RUN_EXECUTION_SUPERSEDED"


def test_concurrent_execution_cas_has_one_winner_and_old_retry_is_read_only(session) -> None:
    state = _state(session, scene_id="SC_EXECUTION_RACE")
    barrier = Barrier(2)

    def _contend(execution_id: str) -> tuple[str, str]:
        contender = SessionLocal()
        try:
            barrier.wait(timeout=5)
            try:
                SceneRunCheckpointService(contender).acquire_execution(state.scene_id, execution_id)
                contender.commit()
                return ("won", execution_id)
            except OperationalError:
                # SQLite may surface the losing simultaneous write as BUSY before
                # the winning commit becomes visible. Re-read after the lock clears;
                # the durable result must still be the execution-owner fence.
                contender.rollback()
                time.sleep(0.05)
                try:
                    SceneRunCheckpointService(contender).acquire_execution(state.scene_id, execution_id)
                    contender.commit()
                    return ("won", execution_id)
                except DomainError as exc:
                    contender.rollback()
                    return (exc.code, execution_id)
            except DomainError as exc:
                contender.rollback()
                return (exc.code, execution_id)
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_contend, ("exec-race-a", "exec-race-b")))

    winners = [execution_id for outcome, execution_id in outcomes if outcome == "won"]
    losers = [outcome for outcome, _execution_id in outcomes if outcome != "won"]
    assert len(winners) == 1
    assert losers == ["RUN_EXECUTION_IN_PROGRESS"]
    winner = winners[0]

    session.expire_all()
    state = session.get(SceneRunState, state.scene_id)
    assert state is not None
    assert state.active_execution_id == winner
    state.scene_tokens_used = 31
    state.scene_tokens_reserved = 7
    state.provider_attempts_used = 2
    session.flush()
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.save_checkpoint(
        scene_id=state.scene_id,
        execution_id=winner,
        node_key="budget_ready",
        artifact_refs={"budget_basis_hash": "sha256:race"},
    )
    checkpoints.mark_failed(state.scene_id, winner)
    session.commit()

    replacement = "exec-race-next"
    checkpoints.acquire_execution(state.scene_id, replacement)
    session.commit()
    session.refresh(state)
    snapshot = {
        "active_execution_id": state.active_execution_id,
        "run_checkpoint": state.run_checkpoint,
        "run_checkpoint_json": dict(state.run_checkpoint_json or {}),
        "scene_tokens_used": state.scene_tokens_used,
        "scene_tokens_reserved": state.scene_tokens_reserved,
        "provider_attempts_used": state.provider_attempts_used,
    }

    with pytest.raises(DomainError) as old_retry:
        checkpoints.acquire_execution(state.scene_id, winner)
    assert old_retry.value.code == "RUN_EXECUTION_SUPERSEDED"
    session.rollback()
    session.refresh(state)
    assert {
        "active_execution_id": state.active_execution_id,
        "run_checkpoint": state.run_checkpoint,
        "run_checkpoint_json": dict(state.run_checkpoint_json or {}),
        "scene_tokens_used": state.scene_tokens_used,
        "scene_tokens_reserved": state.scene_tokens_reserved,
        "provider_attempts_used": state.provider_attempts_used,
    } == snapshot


def test_selection_resume_checkpoint_handoff_has_one_cas_owner(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    checkpoints = SceneRunCheckpointService(session)
    old_execution = "idempotency:selection-origin"
    checkpoints.acquire_execution(scene_id, old_execution)
    for node in (
        "budget_ready",
        "planning_ready",
        "bundle_ready",
        "neutral_ready",
        "hard_qc_ready",
        "style_ready",
        "selection_wait",
    ):
        checkpoints.save_checkpoint(
            scene_id=scene_id,
            execution_id=old_execution,
            node_key=node,
            artifact_refs={"selection_context": "durable"} if node == "selection_wait" else None,
        )
    checkpoints.mark_waiting_selection(scene_id, old_execution)
    session.commit()
    barrier = Barrier(2)

    def _resume_contender(execution_id: str) -> tuple[str, str]:
        contender = SessionLocal()
        try:
            barrier.wait(timeout=5)
            try:
                SceneRunCheckpointService(contender).acquire_selection_resume(scene_id, execution_id)
                contender.commit()
                return ("won", execution_id)
            except OperationalError:
                contender.rollback()
                time.sleep(0.05)
                try:
                    SceneRunCheckpointService(contender).acquire_selection_resume(scene_id, execution_id)
                    contender.commit()
                    return ("won", execution_id)
                except DomainError as exc:
                    contender.rollback()
                    return (exc.code, execution_id)
            except DomainError as exc:
                contender.rollback()
                return (exc.code, execution_id)
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                _resume_contender,
                ("idempotency:selection-resume-a", "idempotency:selection-resume-b"),
            )
        )
    winners = [execution_id for outcome, execution_id in outcomes if outcome == "won"]
    assert len(winners) == 1
    assert [outcome for outcome, _ in outcomes if outcome != "won"] == ["RUN_EXECUTION_IN_PROGRESS"]
    winner = winners[0]

    session.expire_all()
    state = session.get(SceneRunState, scene_id)
    assert state.active_execution_id == winner
    assert state.run_execution_status == "active"
    assert state.run_checkpoint == "selection_wait"
    assert state.run_checkpoint_json["execution_id"] == winner
    assert state.run_checkpoint_json["artifact_refs"]["selection_context"] == "durable"
    assert old_execution in state.run_checkpoint_json["superseded_execution_ids"]

    before = dict(state.run_checkpoint_json)
    with pytest.raises(DomainError) as stale_terminal:
        checkpoints.mark_failed(scene_id, old_execution)
    assert stale_terminal.value.code == "RUN_EXECUTION_SUPERSEDED"
    session.rollback()
    session.refresh(state)
    assert state.active_execution_id == winner
    assert state.run_execution_status == "active"
    assert state.run_checkpoint_json == before


def test_failed_post_selection_checkpoint_hands_off_to_a_fresh_resume_owner(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    checkpoints = SceneRunCheckpointService(session)
    origin = "idempotency:selection-origin-budget"
    first_resume = "idempotency:selection-resume-budget-a"
    second_resume = "idempotency:selection-resume-budget-b"
    checkpoints.acquire_execution(scene_id, origin)
    for node in RUN_CHECKPOINT_ORDER[: RUN_CHECKPOINT_ORDER.index("selection_wait") + 1]:
        checkpoints.save_checkpoint(
            scene_id=scene_id,
            execution_id=origin,
            node_key=node,
            artifact_refs={"selection_context": "durable"} if node == "selection_wait" else None,
        )
    checkpoints.mark_waiting_selection(scene_id, origin)
    checkpoints.acquire_selection_resume(scene_id, first_resume)
    checkpoints.save_checkpoint(
        scene_id=scene_id,
        execution_id=first_resume,
        node_key="soft_qc_ready",
        artifact_refs={"post_selection_product": "kept"},
    )
    checkpoints.mark_failed(scene_id, first_resume)

    claimed = checkpoints.acquire_selection_resume(scene_id, second_resume)

    state = session.get(SceneRunState, scene_id)
    assert claimed.resumed is True
    assert state.active_execution_id == second_resume
    assert state.run_execution_status == "active"
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["artifact_refs"]["post_selection_product"] == "kept"
    assert first_resume in state.run_checkpoint_json["artifact_execution_lineage_ids"]
    assert first_resume in state.run_checkpoint_json["superseded_execution_ids"]


def test_checkpoint_rejects_wrong_execution_and_out_of_order_node(session) -> None:
    state = _state(session)
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(state.scene_id, "exec-a")

    with pytest.raises(DomainError) as wrong_owner:
        checkpoints.save_checkpoint(
            scene_id=state.scene_id,
            execution_id="exec-b",
            node_key="budget_ready",
        )
    assert wrong_owner.value.code == "RUN_EXECUTION_SUPERSEDED"

    with pytest.raises(DomainError) as out_of_order:
        checkpoints.save_checkpoint(
            scene_id=state.scene_id,
            execution_id="exec-a",
            node_key="bundle_ready",
        )
    assert out_of_order.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_cancelled_execution_has_durable_terminal_checkpoint_and_can_be_superseded(session) -> None:
    state = _state(session, scene_id="SC_CANCELLED_CHECKPOINT")
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(state.scene_id, "exec-cancelled")
    checkpoints.save_checkpoint(
        scene_id=state.scene_id,
        execution_id="exec-cancelled",
        node_key="budget_ready",
        artifact_refs={"scene_token_budget": 100},
    )
    checkpoints.mark_cancelled(state.scene_id, "exec-cancelled")
    session.commit()

    session.refresh(state)
    assert state.run_execution_status == "cancelled"
    assert state.run_checkpoint == "cancelled"
    assert state.run_checkpoint_json["node_key"] == "cancelled"
    assert state.run_checkpoint_json["cancelled_from_node"] == "budget_ready"

    with pytest.raises(DomainError) as same_execution:
        checkpoints.acquire_execution(state.scene_id, "exec-cancelled")
    assert same_execution.value.code == "RUN_EXECUTION_CANCELLED"

    claim = checkpoints.acquire_execution(state.scene_id, "exec-replacement")
    assert claim.resumed is False
    assert claim.last_node is None
    assert "exec-cancelled" in claim.checkpoint_json["superseded_execution_ids"]

    with pytest.raises(DomainError) as stale_owner:
        checkpoints.mark_failed(state.scene_id, "exec-cancelled")
    assert stale_owner.value.code == "RUN_EXECUTION_SUPERSEDED"


def test_settled_or_dispatched_ledger_without_output_is_blocked(session) -> None:
    state = _state(session, scene_id="SC_LEDGER_MISSING")
    state.scene_token_budget = 100
    state.scene_tokens_reserved = 0
    state.scene_tokens_used = 10
    session.add(
        LlmCall(
            llm_call_id="call-settled-missing",
            provider="fake",
            model="fake",
            step="neutral_draft",
            scene_id=state.scene_id,
            scope_type="scene",
            scope_id=state.scene_id,
            execution_id="exec-ledger",
            execution_step_key="neutral_draft",
            estimated_tokens=20,
            reserved_tokens=20,
            budget_charged_tokens=10,
            accounting_status="settled",
            request_dispatched_at="2026-07-13T00:00:00+00:00",
            settled_at="2026-07-13T00:00:01+00:00",
        )
    )
    session.commit()

    with pytest.raises(DomainError) as exc_info:
        SceneRunCheckpointService(session).reconcile_step_output(
            scene_id=state.scene_id,
            execution_id="exec-ledger",
            execution_step_key="neutral_draft",
            output_exists=False,
        )
    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"


def test_undispatched_reservation_is_released_for_checkpoint_retry(session) -> None:
    state = _state(session, scene_id="SC_LEDGER_RELEASE")
    state.scene_token_budget = 100
    state.scene_tokens_reserved = 20
    session.add(
        LlmCall(
            llm_call_id="call-reserved-undispatched",
            provider="fake",
            model="fake",
            step="style_draft",
            scene_id=state.scene_id,
            scope_type="scene",
            scope_id=state.scene_id,
            execution_id="exec-ledger",
            execution_step_key="style_draft:1",
            estimated_tokens=20,
            reserved_tokens=20,
            budget_charged_tokens=0,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id="attempt-reserved-undispatched",
            llm_call_id="call-reserved-undispatched",
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=10,
            estimated_tokens=20,
            reserved_tokens=20,
            budget_charged_tokens=0,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.commit()

    outcome = SceneRunCheckpointService(session).reconcile_step_output(
        scene_id=state.scene_id,
        execution_id="exec-ledger",
        execution_step_key="style_draft:1",
        output_exists=False,
    )
    session.commit()

    session.refresh(state)
    call = session.get(LlmCall, "call-reserved-undispatched")
    attempt = session.get(LlmCallAttempt, "attempt-reserved-undispatched")
    assert outcome == "retry"
    assert call.accounting_status == "released"
    assert call.reserved_tokens == attempt.reserved_tokens == 20
    assert call.budget_charged_tokens == attempt.budget_charged_tokens == 0
    assert attempt.accounting_status == "released"
    assert attempt.settled_at is not None
    assert state.scene_tokens_reserved == 0
