from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading

import pytest
from fastapi.testclient import TestClient

from novel_system.api.app import create_app
from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    StyleReferenceBook,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.db.session import SessionLocal
from novel_system.services.background_recovery import (
    acquire_startup_recovery_lease,
    recover_run_job_dispatches,
    retire_legacy_style_reference_runs,
    run_startup_recovery,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import recover_stale_legacy_reservations
from novel_system.services.scene_run_jobs import SceneRunJobService


def _seed_run_job_parents(
    session,
    *,
    chapter_ids: tuple[str, ...] = (),
    scene_ids: tuple[str, ...] = (),
) -> None:
    existing_chapters = set(chapter_ids)
    scene_chapter_id = "C_SCENE_RECOVERY"
    if scene_ids:
        existing_chapters.add(scene_chapter_id)
    session.add_all(
        [
            ChapterGoal(chapter_id=chapter_id, chapter_goal=f"goal {chapter_id}")
            for chapter_id in sorted(existing_chapters)
        ]
    )
    session.add_all(
        [
            SceneCard(
                scene_id=scene_id,
                chapter_id=scene_chapter_id,
                scene_seq=index,
                scene_goal=f"goal {scene_id}",
            )
            for index, scene_id in enumerate(scene_ids, start=1)
        ]
    )
    session.flush()


def test_run_job_recovery_dispatches_missing_or_expired_leases_and_skips_active(session) -> None:
    _seed_run_job_parents(
        session,
        chapter_ids=("C1", "C2", "C3"),
        scene_ids=("S1", "S2", "S3"),
    )
    now = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    expired = (now - timedelta(seconds=1)).isoformat()
    active = (now + timedelta(minutes=5)).isoformat()
    session.add_all(
        [
            ChapterRunJob(job_id="scene-queued", scene_id="S1", status="queued", job_type="scene_run_full"),
            ChapterRunJob(
                job_id="scene-missing-lease",
                scene_id="S2",
                status="running",
                job_type="scene_run_full",
                worker_id="dead",
                attempt_no=1,
                lease_expires_at=None,
            ),
            ChapterRunJob(
                job_id="scene-active",
                scene_id="S3",
                status="running",
                job_type="scene_run_full",
                worker_id="alive",
                attempt_no=1,
                lease_expires_at=active,
            ),
            ChapterRunJob(job_id="chapter-pending", chapter_id="C1", status="pending", job_type="chapter_run_full"),
            ChapterRunJob(
                job_id="chapter-expired",
                chapter_id="C2",
                status="running",
                job_type="chapter_run_full",
                worker_id="dead",
                attempt_no=2,
                lease_expires_at=expired,
            ),
            ChapterRunJob(
                job_id="chapter-active",
                chapter_id="C3",
                status="running",
                job_type="chapter_run_full",
                worker_id="alive",
                attempt_no=2,
                lease_expires_at=active,
            ),
        ]
    )
    session.commit()
    scenes: list[str] = []
    chapters: list[tuple[str, str, str | None]] = []

    result = recover_run_job_dispatches(
        session,
        now=now,
        scene_dispatch=scenes.append,
        chapter_dispatch=lambda job_id, chapter_id, project_id: chapters.append(
            (job_id, chapter_id, project_id)
        ),
    )

    assert set(scenes) == {"scene-queued", "scene-missing-lease"}
    assert set(chapters) == {
        ("chapter-pending", "C1", None),
        ("chapter-expired", "C2", None),
    }
    assert set(result["active_lease_skipped"]) == {"scene-active", "chapter-active"}


def test_scene_running_without_lease_has_one_recovery_owner(session) -> None:
    _seed_run_job_parents(session, scene_ids=("S1",))
    session.add(
        ChapterRunJob(
            job_id="scene-no-lease-cas",
            scene_id="S1",
            status="running",
            job_type="scene_run_full",
            worker_id="crashed",
            attempt_no=4,
            lease_expires_at=None,
        )
    )
    session.commit()

    winner = SceneRunJobService(session).claim_running(
        "scene-no-lease-cas",
        worker_id="winner",
        current_step="neutral_running",
        lease_seconds=60,
    )
    session.commit()
    assert winner.attempt_no == 5

    contender = SessionLocal()
    try:
        with pytest.raises(DomainError) as rejected:
            SceneRunJobService(contender).claim_running(
                "scene-no-lease-cas",
                worker_id="loser",
                current_step="neutral_running",
                lease_seconds=60,
            )
        assert rejected.value.code == "RUN_JOB_IN_PROGRESS"
    finally:
        contender.close()


def test_startup_recovery_lease_elects_one_worker_and_allows_expired_takeover(session) -> None:
    now = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    assert acquire_startup_recovery_lease(
        session,
        owner_id="worker-a",
        now=now,
        lease_seconds=30,
    )

    contender = SessionLocal()
    try:
        assert not acquire_startup_recovery_lease(
            contender,
            owner_id="worker-b",
            now=now + timedelta(seconds=5),
            lease_seconds=30,
        )
        assert acquire_startup_recovery_lease(
            contender,
            owner_id="worker-b",
            now=now + timedelta(seconds=31),
            lease_seconds=30,
        )
    finally:
        contender.close()


def test_fastapi_lifespan_runs_background_recovery(monkeypatch) -> None:
    import novel_system.services.background_recovery as recovery_module

    calls: list[bool] = []
    monkeypatch.setattr(
        recovery_module,
        "run_startup_recovery",
        lambda: calls.append(True) or {},
    )

    with TestClient(create_app()) as client:
        assert client.get("/live").status_code == 200

    assert calls == [True]


def test_stale_legacy_llm_recovery_releases_or_fails_without_touching_owned_work(
    session,
) -> None:
    now = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    stale_at = (now - timedelta(hours=2)).isoformat()
    fresh_at = (now - timedelta(minutes=5)).isoformat()

    def add_call(
        call_id: str,
        *,
        created_at: str,
        dispatched: bool = False,
        scope_type: str = "project",
        scene_id: str | None = None,
        run_job_id: str | None = None,
    ) -> None:
        dispatched_at = created_at if dispatched else None
        session.add(
            LlmCall(
                llm_call_id=call_id,
                scope_type=scope_type,
                scope_id=scene_id or call_id,
                scene_id=scene_id,
                run_job_id=run_job_id,
                estimated_tokens=12,
                reserved_tokens=20,
                budget_charged_tokens=0,
                accounting_status="reserved",
                request_dispatched_at=dispatched_at,
                created_at=created_at,
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id=f"attempt-{call_id}",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=4,
                estimated_tokens=12,
                reserved_tokens=20,
                budget_charged_tokens=0,
                accounting_status="reserved",
                request_dispatched_at=dispatched_at,
                created_at=created_at,
            )
        )

    add_call("legacy-undispatched", created_at=stale_at)
    add_call("legacy-dispatched", created_at=stale_at, dispatched=True)
    add_call("legacy-fresh", created_at=fresh_at)
    add_call(
        "active-scene-owned",
        created_at=stale_at,
        dispatched=True,
        scope_type="scene",
        scene_id="scene-active-recovery",
    )
    add_call(
        "active-run-job-owned",
        created_at=stale_at,
        dispatched=True,
        scope_type="chapter",
        run_job_id="job-active-recovery",
    )
    session.commit()

    result = recover_stale_legacy_reservations(
        session,
        now=now,
        ttl_seconds=3_600,
    )

    assert result == {
        "released_call_ids": ["legacy-undispatched"],
        "failed_call_ids": ["legacy-dispatched"],
        "fresh_call_ids_skipped": [],
    }
    session.expire_all()
    assert session.get(LlmCall, "legacy-undispatched").accounting_status == "released"
    dispatched_parent = session.get(LlmCall, "legacy-dispatched")
    dispatched_attempt = session.get(LlmCallAttempt, "attempt-legacy-dispatched")
    assert dispatched_parent.accounting_status == "failed"
    assert dispatched_parent.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert dispatched_attempt.accounting_status == "failed"
    assert dispatched_attempt.total_tokens == 12
    assert dispatched_attempt.budget_charged_tokens == 12
    assert session.get(LlmCall, "legacy-fresh").accounting_status == "reserved"
    assert session.get(LlmCall, "active-scene-owned").accounting_status == "reserved"
    assert session.get(LlmCall, "active-run-job-owned").accounting_status == "reserved"

    assert recover_stale_legacy_reservations(
        session,
        now=now,
        ttl_seconds=3_600,
    ) == {
        "released_call_ids": [],
        "failed_call_ids": [],
        "fresh_call_ids_skipped": [],
    }


def test_concurrent_stale_legacy_llm_sweeps_have_exactly_one_recovery_winner(session) -> None:
    now = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    stale_at = (now - timedelta(hours=2)).isoformat()
    session.add(
        LlmCall(
            llm_call_id="legacy-concurrent-recovery",
            scope_type="system",
            scope_id="provider_probe",
            estimated_tokens=9,
            reserved_tokens=15,
            budget_charged_tokens=0,
            accounting_status="reserved",
            created_at=stale_at,
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id="attempt-legacy-concurrent-recovery",
            llm_call_id="legacy-concurrent-recovery",
            provider_attempt_no=0,
            dispatch_kind="system_probe",
            request_max_output_tokens=3,
            estimated_tokens=9,
            reserved_tokens=15,
            budget_charged_tokens=0,
            accounting_status="reserved",
            created_at=stale_at,
        )
    )
    session.commit()
    barrier = threading.Barrier(2)

    def sweep() -> dict[str, list[str]]:
        with SessionLocal() as worker_session:
            barrier.wait(timeout=5)
            return recover_stale_legacy_reservations(
                worker_session,
                now=now,
                ttl_seconds=3_600,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: sweep(), range(2)))

    winners = sum(
        "legacy-concurrent-recovery" in result["released_call_ids"]
        for result in results
    )
    assert winners == 1
    session.expire_all()
    assert session.get(LlmCall, "legacy-concurrent-recovery").accounting_status == "released"
    assert (
        session.get(LlmCallAttempt, "attempt-legacy-concurrent-recovery").accounting_status
        == "released"
    )


def test_startup_recovery_uses_configured_ttl_for_legacy_llm_reservations(
    session,
    monkeypatch,
) -> None:
    created_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    session.add(
        LlmCall(
            llm_call_id="legacy-startup-recovery",
            scope_type="system",
            scope_id="startup_probe",
            estimated_tokens=8,
            reserved_tokens=12,
            budget_charged_tokens=0,
            accounting_status="reserved",
            created_at=created_at,
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id="attempt-legacy-startup-recovery",
            llm_call_id="legacy-startup-recovery",
            provider_attempt_no=0,
            dispatch_kind="system_probe",
            request_max_output_tokens=2,
            estimated_tokens=8,
            reserved_tokens=12,
            budget_charged_tokens=0,
            accounting_status="reserved",
            created_at=created_at,
        )
    )
    session.commit()
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS", "60")

    summary = run_startup_recovery()

    assert summary["llm_legacy_reservations"]["released_call_ids"] == [
        "legacy-startup-recovery"
    ]
    session.expire_all()
    assert session.get(LlmCall, "legacy-startup-recovery").accounting_status == "released"


def test_legacy_style_reference_runs_are_retired_and_learn_runs_are_left_alone(session) -> None:
    """旧抽取流程(RunOrchestrator,2026-09-23 删除)留下的「运行中」run 标 failed;学习作业的血缘 run 不动。"""
    session.add(
        StyleReferenceBook(
            book_id="book-recovery",
            title="Recovery",
            source_kind="upload",
            cloud_policy="segments_only",
            text_checksum="book-recovery-checksum",
        )
    )
    session.add_all(
        [
            StyleReferenceRun(
                run_id="run-queued",
                book_id="book-recovery",
                status="running",
                phase="extract",
                dispatch_state="queued",
                requested_layers_json=["language", "scene"],
            ),
            StyleReferenceRun(
                run_id="run-running",
                book_id="book-recovery",
                status="running",
                phase="extract",
                dispatch_state="running",
                requested_layers_json=["language"],
            ),
            StyleReferenceRun(
                run_id="run-learn",
                book_id="book-recovery",
                status="running",
                phase="extract",
                dispatch_state="learn_job",
            ),
            StyleReferenceRun(
                run_id="run-done",
                book_id="book-recovery",
                status="done",
                phase="done",
                dispatch_state="completed",
            ),
        ]
    )
    session.commit()

    assert sorted(retire_legacy_style_reference_runs(session)) == ["run-queued", "run-running"]
    session.expire_all()
    for run_id in ("run-queued", "run-running"):
        run = session.get(StyleReferenceRun, run_id)
        assert run.status == "failed" and run.error_code == "STYLE_REFERENCE_RUN_RETIRED"
    assert session.get(StyleReferenceRun, "run-learn").status == "running"
    assert session.get(StyleReferenceRun, "run-done").status == "done"
    assert retire_legacy_style_reference_runs(session) == []
