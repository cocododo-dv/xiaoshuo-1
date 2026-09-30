from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SqlAlchemySession

from novel_system.db.models import IdempotencyKey, OperationLog
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.idempotency import (
    IdempotencyLeaseService,
    canonical_request_hash,
    execute_with_idempotency,
)


def chapter_payload(goal: str) -> dict:
    return {
        "chapter_id": "CH001",
        "planned_scene_count": 3,
        "chapter_goal": goal,
        "main_plot_push": "推进重逢线索",
        "emotional_target": "紧张试探",
        "ending_effect": "以余波收束",
    }


def test_post_requires_idempotency_header(raw_client) -> None:
    response = raw_client.post("/api/v1/chapters", json=chapter_payload("目标一"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_request_identity_comes_from_the_matched_route_template(client, session) -> None:
    """mutate() 用这次请求匹配到的路由模板（不是具体 URL、也不再手抄）算请求哈希：与以前各路由手抄的字面量一致，
    既有的幂等记录照旧重放。执行失败的请求也先记下了 idempotency_started。"""
    response = client.patch(
        "/api/v2/projects/missing-project-7/profile",
        json={"title": "改名"},
        headers={"X-Idempotency-Key": "route-template-probe"},
    )
    assert response.status_code == 404

    started = session.execute(
        select(OperationLog).where(
            OperationLog.event_type == "idempotency_started",
            OperationLog.object_ref == "route-template-probe",
        )
    ).scalar_one()
    assert started.payload_json["request_method"] == "PATCH"
    assert started.payload_json["request_path_template"] == "/api/v2/projects/{project_id}/profile"
    record = session.get(IdempotencyKey, "route-template-probe")
    assert record.status == "failed"
    assert record.request_hash == canonical_request_hash(
        "PATCH",
        "/api/v2/projects/{project_id}/profile",
        {"project_id": "missing-project-7", "body": {"title": "改名"}},
    )


def test_same_key_same_payload_is_replayed(client) -> None:
    payload = chapter_payload("目标一")
    first = client.post(
        "/api/v1/chapters",
        json=payload,
        headers={"X-Idempotency-Key": "chapter-create-1"},
    )
    second = client.post(
        "/api/v1/chapters",
        json=payload,
        headers={"X-Idempotency-Key": "chapter-create-1"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["X-Idempotency-Status"] == "replayed"


def test_same_key_different_payload_is_rejected(client) -> None:
    first = client.post(
        "/api/v1/chapters",
        json=chapter_payload("目标一"),
        headers={"X-Idempotency-Key": "chapter-create-2"},
    )
    second = client.post(
        "/api/v1/chapters",
        json=chapter_payload("目标二"),
        headers={"X-Idempotency-Key": "chapter-create-2"},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"


def test_after_commit_observes_durable_success_and_replays_without_rerunning_action(session) -> None:
    action_calls = 0
    observations: list[tuple[str | None, bool]] = []

    def action() -> dict:
        nonlocal action_calls
        action_calls += 1
        session.add(
            OperationLog(
                event_type="post_commit_domain_write",
                object_type="test",
                object_ref="durable-before-dispatch",
                payload_json={},
            )
        )
        return {"queued": True}

    def after_commit(_result: dict) -> None:
        with SessionLocal() as observer:
            record = observer.get(IdempotencyKey, "post-commit-dispatch")
            domain_write = observer.execute(
                select(OperationLog).where(
                    OperationLog.event_type == "post_commit_domain_write"
                )
            ).scalar_one_or_none()
            observations.append(
                (record.status if record is not None else None, domain_write is not None)
            )

    first, first_status = execute_with_idempotency(
        session,
        idempotency_key="post-commit-dispatch",
        method="POST",
        path_template="/queued-action",
        payload={"work": 1},
        action=action,
        after_commit=after_commit,
    )
    replay, replay_status = execute_with_idempotency(
        session,
        idempotency_key="post-commit-dispatch",
        method="POST",
        path_template="/queued-action",
        payload={"work": 1},
        action=lambda: pytest.fail("replay must not rerun the business action"),
        after_commit=after_commit,
    )

    assert first["queued"] is True
    assert first_status is None
    assert replay["queued"] is True
    assert replay_status == "replayed"
    assert action_calls == 1
    assert observations == [("succeeded", True), ("succeeded", True)]


def test_after_commit_failure_keeps_succeeded_record_for_dispatch_retry(session) -> None:
    def dispatch_failure(_result: dict) -> None:
        raise RuntimeError("queue temporarily unavailable")

    with pytest.raises(RuntimeError, match="queue temporarily unavailable"):
        execute_with_idempotency(
            session,
            idempotency_key="post-commit-retry",
            method="POST",
            path_template="/queued-action",
            payload={},
            action=lambda: {"queued": True},
            after_commit=dispatch_failure,
        )

    session.expire_all()
    record = session.get(IdempotencyKey, "post-commit-retry")
    assert record is not None
    assert record.status == "succeeded"

    dispatched: list[bool] = []
    replay, replay_status = execute_with_idempotency(
        session,
        idempotency_key="post-commit-retry",
        method="POST",
        path_template="/queued-action",
        payload={},
        action=lambda: pytest.fail("dispatch retry must not rerun the action"),
        after_commit=lambda result: dispatched.append(bool(result["queued"])),
    )
    assert replay["queued"] is True
    assert replay_status == "replayed"
    assert dispatched == [True]


def test_concurrent_same_key_insert_is_reported_as_in_progress(session, monkeypatch) -> None:
    def raise_duplicate_key() -> None:
        raise IntegrityError("INSERT INTO idempotency_keys", {}, Exception("duplicate key"))

    monkeypatch.setattr(session, "flush", raise_duplicate_key)

    with pytest.raises(DomainError) as exc_info:
        execute_with_idempotency(
            session,
            idempotency_key="chapter-create-race",
            method="POST",
            path_template="/api/v1/chapters",
            payload=chapter_payload("race"),
            action=lambda: {"created": True},
        )

    assert exc_info.value.code == "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    assert exc_info.value.status_code == 409
    assert exc_info.value.details["retryable"] is True


def test_failed_idempotent_action_reenters_with_same_execution_and_new_attempt(session) -> None:
    seen: list[tuple[str, int]] = []

    def fail(lease) -> dict:  # noqa: ANN001
        seen.append((lease.execution_id, lease.attempt_no))
        raise DomainError("SIMULATED_FAILURE", "fail after a durable checkpoint")

    with pytest.raises(DomainError, match="fail after a durable checkpoint"):
        execute_with_idempotency(
            session,
            idempotency_key="retry-same-execution",
            method="POST",
            path_template="/api/v1/scenes/{scene_id}/run/full",
            payload={"scene_id": "SC01"},
            action=fail,
            worker_id="worker-a",
        )

    result, replay = execute_with_idempotency(
        session,
        idempotency_key="retry-same-execution",
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/run/full",
        payload={"scene_id": "SC01"},
        action=lambda lease: seen.append((lease.execution_id, lease.attempt_no)) or {"ok": True},
        worker_id="worker-b",
    )

    assert result["ok"] is True
    assert replay is None
    assert seen == [
        ("idempotency:retry-same-execution", 1),
        ("idempotency:retry-same-execution", 2),
    ]


def test_owned_failure_callback_commits_only_controlled_failure_state(session) -> None:
    def fail_with_unrelated_pending_write() -> dict:
        session.add(
            OperationLog(
                event_type="unrelated_action_write",
                object_type="test",
                object_ref="must-rollback",
                payload_json={},
            )
        )
        raise DomainError("EXPECTED_FAILURE", "publish controlled failure")

    def publish_owned_failure(error: DomainError) -> None:
        assert error.code == "EXPECTED_FAILURE"
        session.add(
            OperationLog(
                event_type="controlled_failure_write",
                object_type="test",
                object_ref="must-commit",
                payload_json={"error_code": error.code},
            )
        )

    with pytest.raises(DomainError, match="publish controlled failure"):
        execute_with_idempotency(
            session,
            idempotency_key="controlled-failure-publish",
            method="POST",
            path_template="/controlled-failure",
            payload={},
            action=fail_with_unrelated_pending_write,
            owned_failure_callback=publish_owned_failure,
            worker_id="failure-owner",
        )

    event_types = session.execute(
        select(OperationLog.event_type).where(OperationLog.object_type == "test")
    ).scalars().all()
    record = session.get(IdempotencyKey, "controlled-failure-publish")
    assert event_types == ["controlled_failure_write"]
    assert record is not None
    assert record.status == "failed"


def test_reclaimed_worker_cannot_run_owned_failure_callback_or_commit_pending_writes(session) -> None:
    callback_errors: list[str] = []
    request_hash = canonical_request_hash("POST", "/stale-failure", {})

    def fail_after_lease_reclaim() -> dict:
        observed_owner = session.get(IdempotencyKey, "stale-failure-owner")
        assert observed_owner is not None
        assert observed_owner.worker_id == "old-worker"
        session.add(
            OperationLog(
                event_type="stale_action_write",
                object_type="test",
                object_ref="must-rollback",
                payload_json={},
            )
        )
        contender = SessionLocal()
        try:
            expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            contender.execute(
                update(IdempotencyKey)
                .where(
                    IdempotencyKey.idempotency_key == "stale-failure-owner",
                    IdempotencyKey.worker_id == "old-worker",
                    IdempotencyKey.attempt_no == 1,
                )
                .values(lease_expires_at=expired, heartbeat_at=expired)
            )
            contender.commit()
            replacement = IdempotencyLeaseService(contender).claim(
                idempotency_key="stale-failure-owner",
                request_hash=request_hash,
                worker_id="new-worker",
                lease_seconds=30,
            )
            contender.commit()
            assert replacement.attempt_no == 2
        finally:
            contender.close()
        raise DomainError("STALE_WORKER_FAILURE", "old worker finished late")

    with pytest.raises(DomainError, match="old worker finished late"):
        execute_with_idempotency(
            session,
            idempotency_key="stale-failure-owner",
            method="POST",
            path_template="/stale-failure",
            payload={},
            action=fail_after_lease_reclaim,
            owned_failure_callback=lambda error: callback_errors.append(error.code),
            worker_id="old-worker",
        )

    session.expire_all()
    record = session.get(IdempotencyKey, "stale-failure-owner")
    stale_write = session.execute(
        select(OperationLog).where(OperationLog.event_type == "stale_action_write")
    ).scalar_one_or_none()
    assert callback_errors == []
    assert stale_write is None
    assert record is not None
    assert record.status == "started"
    assert record.worker_id == "new-worker"
    assert record.attempt_no == 2


def test_owned_failure_callback_error_rolls_back_owner_and_domain_updates(session) -> None:
    def fail() -> dict:
        raise DomainError("EXPECTED_FAILURE", "domain operation failed")

    def broken_publisher(_error: DomainError) -> None:
        session.add(
            OperationLog(
                event_type="partial_failure_publish",
                object_type="test",
                object_ref="must-rollback",
                payload_json={},
            )
        )
        raise RuntimeError("failure publisher unavailable")

    with pytest.raises(RuntimeError, match="failure publisher unavailable"):
        execute_with_idempotency(
            session,
            idempotency_key="broken-failure-publisher",
            method="POST",
            path_template="/broken-failure-publisher",
            payload={},
            action=fail,
            owned_failure_callback=broken_publisher,
            worker_id="failure-owner",
        )

    session.expire_all()
    record = session.get(IdempotencyKey, "broken-failure-publisher")
    partial_write = session.execute(
        select(OperationLog).where(OperationLog.event_type == "partial_failure_publish")
    ).scalar_one_or_none()
    assert record is not None
    assert record.status == "started"
    assert partial_write is None


def test_unexpected_idempotent_action_error_is_sanitized(session) -> None:
    def leak_secret() -> dict:
        raise RuntimeError("provider secret=sk-do-not-return manuscript excerpt")

    with pytest.raises(DomainError) as exc_info:
        execute_with_idempotency(
            session,
            idempotency_key="sanitize-unexpected-error",
            method="POST",
            path_template="/unsafe",
            payload={"safe": True},
            action=leak_secret,
        )

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert exc_info.value.message == "internal operation failed"
    assert "sk-do-not-return" not in exc_info.value.message


def test_active_idempotency_lease_is_not_reclaimed_by_another_worker(session) -> None:
    request_hash = canonical_request_hash("POST", "/run", {"scene_id": "SC01"})
    first = IdempotencyLeaseService(session).claim(
        idempotency_key="active-lease",
        request_hash=request_hash,
        worker_id="worker-a",
        lease_seconds=30,
    )
    session.commit()

    other = SessionLocal()
    try:
        with pytest.raises(DomainError) as exc_info:
            IdempotencyLeaseService(other).claim(
                idempotency_key="active-lease",
                request_hash=request_hash,
                worker_id="worker-b",
                lease_seconds=30,
            )
        assert exc_info.value.code == "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    finally:
        other.close()

    assert first.worker_id == "worker-a"
    assert first.attempt_no == 1


def test_expired_idempotency_lease_has_one_cas_reclaim_winner(session) -> None:
    request_hash = canonical_request_hash("POST", "/run", {"scene_id": "SC01"})
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    session.add(
        IdempotencyKey(
            idempotency_key="expired-lease",
            request_hash=request_hash,
            status="started",
            worker_id="dead-worker",
            attempt_no=1,
            heartbeat_at=expired,
            lease_expires_at=expired,
        )
    )
    session.commit()

    contender_a = SessionLocal()
    contender_b = SessionLocal()
    try:
        winner = IdempotencyLeaseService(contender_a).claim(
            idempotency_key="expired-lease",
            request_hash=request_hash,
            worker_id="worker-a",
            lease_seconds=30,
        )
        contender_a.commit()
        with pytest.raises(DomainError) as loser:
            IdempotencyLeaseService(contender_b).claim(
                idempotency_key="expired-lease",
                request_hash=request_hash,
                worker_id="worker-b",
                lease_seconds=30,
            )
        assert loser.value.code == "IDEMPOTENCY_REQUEST_IN_PROGRESS"
        assert winner.attempt_no == 2
    finally:
        contender_a.close()
        contender_b.close()


def test_expired_idempotency_lease_barrier_has_one_provider_budget_winner(session, monkeypatch) -> None:
    request_hash = canonical_request_hash("POST", "/run", {"scene_id": "SC01"})
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    session.add(
        IdempotencyKey(
            idempotency_key="expired-lease-barrier",
            request_hash=request_hash,
            status="started",
            worker_id="dead-worker",
            attempt_no=1,
            heartbeat_at=expired,
            lease_expires_at=expired,
        )
    )
    session.commit()

    barrier = Barrier(2)
    refresh_lock = Lock()
    synchronized_sessions: set[int] = set()
    original_refresh = SqlAlchemySession.refresh

    def synchronized_refresh(db, instance, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        original_refresh(db, instance, *args, **kwargs)
        if not isinstance(instance, IdempotencyKey) or instance.idempotency_key != "expired-lease-barrier":
            return
        with refresh_lock:
            first_refresh = id(db) not in synchronized_sessions
            synchronized_sessions.add(id(db))
        if first_refresh:
            barrier.wait(timeout=10)

    monkeypatch.setattr(SqlAlchemySession, "refresh", synchronized_refresh)
    provider_budget_effects: list[tuple[str, int]] = []
    effects_lock = Lock()

    def contender(worker_id: str) -> tuple[str, str]:
        db = SessionLocal()
        try:
            try:
                lease = IdempotencyLeaseService(db).claim(
                    idempotency_key="expired-lease-barrier",
                    request_hash=request_hash,
                    worker_id=worker_id,
                    lease_seconds=30,
                )
                db.commit()
            except DomainError as exc:
                return "loser", exc.code
            with effects_lock:
                provider_budget_effects.append((worker_id, lease.attempt_no))
            return "winner", worker_id
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ("worker-a", "worker-b")))

    assert [kind for kind, _value in results].count("winner") == 1
    assert ("loser", "IDEMPOTENCY_REQUEST_IN_PROGRESS") in results
    assert len(provider_budget_effects) == 1
    assert provider_budget_effects[0][1] == 2


def test_lease_renewal_is_fenced_by_worker_and_attempt(session) -> None:
    request_hash = canonical_request_hash("POST", "/run", {"scene_id": "SC01"})
    owner = IdempotencyLeaseService(session).claim(
        idempotency_key="renew-lease",
        request_hash=request_hash,
        worker_id="worker-a",
        lease_seconds=1,
    )
    before = owner.lease_expires_at
    renewed = owner.renew(lease_seconds=120)
    assert renewed > before

    owner.attempt_no += 1
    with pytest.raises(DomainError) as lost:
        owner.renew(lease_seconds=120)
    assert lost.value.code == "RUN_OWNER_LEASE_LOST"


def test_detached_lease_renewal_commits_from_an_independent_session(session) -> None:
    request_hash = canonical_request_hash("POST", "/run", {"scene_id": "SC01"})
    owner = IdempotencyLeaseService(session).claim(
        idempotency_key="renew-lease-detached",
        request_hash=request_hash,
        worker_id="worker-a",
        lease_seconds=1,
    )
    session.commit()
    before = owner.lease_expires_at

    renewed = owner.renew_detached(lease_seconds=120)

    with SessionLocal() as observer:
        persisted = observer.get(IdempotencyKey, "renew-lease-detached")
        assert persisted is not None
        assert persisted.lease_expires_at == renewed
        assert persisted.lease_expires_at > before
