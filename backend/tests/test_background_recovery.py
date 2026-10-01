from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
import time

import pytest

from novel_system.api.app import create_app
from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
)
from novel_system.db.session import SessionLocal
from novel_system.services.background_recovery import (
    acquire_startup_recovery_lease,
    recover_run_job_dispatches,
    run_startup_recovery,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import recover_stale_legacy_reservations
from novel_system.services.scene_run_jobs import SceneRunJobService
from tests.support.api_client import AutoKeyTestClient


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

    with AutoKeyTestClient(create_app()) as client:
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


# ---------------------------------------------------------------------------
# B03-01：进程重启 / --reload 之后运行任务不再干等租约过期
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 20.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _job_status(job_id: str) -> str | None:
    with SessionLocal() as observer:
        job = observer.get(ChapterRunJob, job_id)
        return job.status if job is not None else None


def test_app_shutdown_releases_its_run_leases_so_a_restart_resumes_at_once(session, monkeypatch) -> None:
    """lifespan 结束（--reload / 停服）时，本进程工人持有的租约就地到期：重启后的启动恢复立刻把任务接着跑，
    不再因为「租约还有 600 秒」跳过它（修之前：任务一直 running、这一场一直 409，直到很久以后的某次重启）。
    进程退出之后工人的续约不再生效（心跳线程续不回来）。"""
    from novel_system.services import scene_run_jobs as job_module
    from novel_system.services.run_job_leases import lease_is_active
    from tests.support.catalog import create_job_chapter_and_scene as _create_chapter_and_scene

    started = threading.Event()
    release = threading.Event()
    lease_after_shutdown: list[str | None] = []

    class _BlockingPipeline:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, scene_id: str, *, run_job_id=None, lease_renewer=None, **_kwargs) -> dict:
            started.set()
            release.wait(30)
            lease_renewer(lease_seconds=600)  # 进程已在退出：不再续约
            with SessionLocal() as observer:
                lease_after_shutdown.append(observer.get(ChapterRunJob, run_job_id).lease_expires_at)
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _BlockingPipeline)
    with AutoKeyTestClient(create_app()) as client:
        _create_chapter_and_scene(client)
        job_id = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs").json()["data"]["job_id"]
        assert started.wait(10)
        with SessionLocal() as observer:
            assert lease_is_active(observer.get(ChapterRunJob, job_id).lease_expires_at)

    try:
        with SessionLocal() as observer:
            orphan = observer.get(ChapterRunJob, job_id)
            assert orphan.status == "running"
            assert not lease_is_active(orphan.lease_expires_at)
            dispatched: list[str] = []
            result = recover_run_job_dispatches(
                observer, scene_dispatch=dispatched.append, chapter_dispatch=lambda *_args: None
            )
        assert dispatched == [job_id]
        assert result["active_lease_skipped"] == []
    finally:
        release.set()
        assert _wait_until(lambda: bool(lease_after_shutdown))
    assert not lease_is_active(lease_after_shutdown[0])
    assert _wait_until(lambda: _job_status(job_id) == "completed")


def test_periodic_run_job_sweep_recovers_orphans_without_a_restart(session, monkeypatch) -> None:
    """周期恢复：租约已过期、本进程里没有它的工人的 running 任务再派发一次（排队中的不归它管，启动恢复负责）；
    主人已死的取消请求收尾为 cancelled。修之前只有启动时扫一次。"""
    import novel_system.services.background_recovery as recovery_module
    from novel_system.services.run_job_leases import mark_dispatched, unmark_dispatched

    _seed_run_job_parents(session, chapter_ids=("C9",), scene_ids=("S1", "S2", "S3", "S4", "S5"))
    now = datetime.now(UTC)
    expired = (now - timedelta(seconds=5)).isoformat()
    active = (now + timedelta(minutes=5)).isoformat()

    def _running(job_id: str, scene_id: str, lease: str, *, status: str = "running") -> ChapterRunJob:
        return ChapterRunJob(
            job_id=job_id,
            scene_id=scene_id,
            status=status,
            job_type="scene_run_full",
            worker_id="dead-worker",
            attempt_no=1,
            lease_expires_at=lease,
        )

    session.add_all(
        [
            _running("scene-orphan", "S1", expired),
            _running("scene-alive-elsewhere", "S2", active),
            ChapterRunJob(job_id="scene-queued", scene_id="S3", status="queued", job_type="scene_run_full"),
            _running("scene-running-here", "S4", expired),
            _running("scene-cancel-orphan", "S5", expired, status="cancel_requested"),
            ChapterRunJob(
                job_id="chapter-orphan",
                chapter_id="C9",
                status="running",
                job_type="chapter_run_full",
                worker_id="dead-worker",
                attempt_no=1,
                lease_expires_at=expired,
            ),
        ]
    )
    session.commit()
    scenes: list[str] = []
    chapters: list[str] = []
    monkeypatch.setattr(recovery_module, "_dispatch_scene", scenes.append)
    monkeypatch.setattr(recovery_module, "_dispatch_chapter", lambda job_id, *_args: chapters.append(job_id))
    assert mark_dispatched("scene-running-here")
    try:
        summary = recovery_module.sweep_orphaned_run_jobs()
    finally:
        unmark_dispatched("scene-running-here")

    assert scenes == ["scene-orphan"]
    assert chapters == ["chapter-orphan"]
    assert [item["job_id"] for item in summary["expired_cancel_requests"]] == ["scene-cancel-orphan"]
    session.expire_all()
    assert session.get(ChapterRunJob, "scene-cancel-orphan").status == "cancelled"
    assert session.get(ChapterRunJob, "scene-queued").status == "queued"


def test_run_job_sweeper_tick_runs_due_system_maintenance(monkeypatch) -> None:
    """巡检线程的一拍顺带跑到期的全系统维护任务（``services/maintenance.py``）：启动时就跑的任务第一拍跑，
    ``run_at_start=False`` 的等满一个间隔；一个任务失败不影响别的任务，也不影响巡检本身。"""
    from novel_system.services import background_recovery as recovery_module
    from novel_system.services import maintenance

    ran: list[str] = []

    def failing() -> None:
        raise RuntimeError("maintenance task failed")

    monkeypatch.setattr(recovery_module, "sweep_orphaned_run_jobs", lambda: {"run_jobs": {}})
    maintenance.register_maintenance_task("t_eager", lambda: ran.append("eager"), interval_seconds=3600)
    maintenance.register_maintenance_task("t_failing", failing, interval_seconds=3600)
    maintenance.register_maintenance_task(
        "t_deferred", lambda: ran.append("deferred"), interval_seconds=3600, run_at_start=False
    )
    try:
        maintenance.reset_maintenance_schedule()
        summary = recovery_module.run_job_sweeper_tick()
        assert summary == {"run_jobs": {}, "maintenance": ["t_eager"]}
        assert ran == ["eager"]
        # 同一个间隔内的下一拍什么也不跑
        assert recovery_module.run_job_sweeper_tick()["maintenance"] == []
        assert maintenance.run_due_maintenance(now=time.monotonic() + 3601) == ["t_eager", "t_deferred"]
        assert ran == ["eager", "eager", "deferred"]
    finally:
        for name in ("t_eager", "t_failing", "t_deferred"):
            maintenance.unregister_maintenance_task(name)


def test_lifespan_runs_the_run_job_sweeper_only_while_the_app_is_up() -> None:
    from novel_system.services.background_recovery import RUN_JOB_SWEEPER_THREAD_NAME

    def sweepers() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == RUN_JOB_SWEEPER_THREAD_NAME and t.is_alive()]

    with AutoKeyTestClient(create_app()) as client:
        assert client.get("/live").status_code == 200
        assert len(sweepers()) == 1
    assert _wait_until(lambda: not sweepers(), timeout=10)
