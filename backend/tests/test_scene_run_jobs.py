from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlAlchemySession

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    QcReport,
    SceneCard,
    SceneRunState,
)
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.scene_run_jobs import SceneRunJobService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService


def _seed_job_scene(
    session,
    *,
    scene_id: str = "SC01",
    chapter_id: str = "CH_SCENE_JOB",
) -> None:
    if session.get(ChapterGoal, chapter_id) is None:
        session.add(
            ChapterGoal(chapter_id=chapter_id, chapter_goal=f"goal {chapter_id}")
        )
        session.flush()
    if session.get(SceneCard, scene_id) is None:
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                scene_seq=1,
                scene_goal=f"goal {scene_id}",
            )
        )
        session.flush()


def _create_chapter_and_scene(client) -> None:
    chapter_response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": "CHJOB",
            "planned_scene_count": 1,
            "chapter_goal": "Run scene through background job",
            "main_plot_push": "Exercise job API",
            "emotional_target": "Keep operator unblocked",
            "ending_effect": "Pollable status",
        },
        headers={"X-Idempotency-Key": "chapter-job-create"},
    )
    assert chapter_response.status_code == 200
    scene_response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "CHJOB_SC01",
            "chapter_id": "CHJOB",
            "scene_seq": 1,
            "pov_character_id": "",
            "onstage_chars_json": [],
            "location": "Control room",
            "scene_goal": "Start a pollable run",
            "beats_json": ["start", "poll"],
            "target_length_band": "short",
            "scene_type": "test",
            "is_chapter_last": 1,
        },
        headers={"X-Idempotency-Key": "scene-job-create"},
    )
    assert scene_response.status_code == 200


def test_scene_run_job_api_creates_pollable_nonblocking_job(client, session) -> None:
    _create_chapter_and_scene(client)

    response = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false")

    assert response.status_code == 200
    job = response.json()["data"]
    assert job["scene_id"] == "CHJOB_SC01"
    assert job["chapter_id"] == "CHJOB"
    assert job["job_type"] == "scene_run_full"
    assert job["status"] == "queued"
    assert job["current_step"] == "queued"
    assert job["elapsed_ms"] >= 0
    assert job["stage_order"] == [
        "planning_running",
        "bundle_built",
        "neutral_running",
        "hard_qc_running",
        "style_running",
        "soft_qc_running",
        "rewrite_running",
        "acceptance_review_running",
        "near_final",
        "archived",
    ]

    poll = client.get(f"/api/v1/run-jobs/{job['job_id']}")

    assert poll.status_code == 200
    assert poll.json()["data"]["job_id"] == job["job_id"]
    assert poll.json()["data"]["scene_id"] == "CHJOB_SC01"
    session.expire_all()
    assert session.get(ChapterRunJob, job["job_id"]).scene_id == "CHJOB_SC01"


def test_scene_run_workers_run_at_most_two_pipelines_at_once(client, monkeypatch) -> None:
    """B03-13：场景任务的工人跑在有界的守护车道上（一次最多两条管线）。修之前每个任务一条不设上限的线程，
    启动恢复扫到 N 个排队任务就在 2 核的机器上同时起 N 条管线。"""
    import time
    from threading import Event

    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    for seq in (2, 3):
        created = client.post(
            "/api/v1/scenes",
            json={
                "scene_id": f"CHJOB_SC0{seq}",
                "chapter_id": "CHJOB",
                "scene_seq": seq,
                "pov_character_id": "",
                "onstage_chars_json": [],
                "location": "Control room",
                "scene_goal": f"Queue run {seq}",
                "beats_json": ["start", "poll"],
                "target_length_band": "short",
                "scene_type": "test",
            },
            headers={"X-Idempotency-Key": f"scene-job-lane-{seq}"},
        )
        assert created.status_code == 200, created.text
    lock = Lock()
    release = Event()
    started: list[str] = []
    running = 0
    peak = 0

    class _Pipeline:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, scene_id: str, **_kwargs) -> dict:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                started.append(scene_id)
            release.wait(30)
            with lock:
                running -= 1
            return {"scene_status": "archived"}

    def wait_until(predicate, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.05)
        return bool(predicate())

    def statuses() -> list[str]:
        with SessionLocal() as observer:
            return [observer.get(ChapterRunJob, job_id).status for job_id in job_ids]

    monkeypatch.setattr(job_module, "Orchestrator", _Pipeline)
    job_ids = [
        client.post(f"/api/v1/scenes/CHJOB_SC0{seq}/run/jobs").json()["data"]["job_id"] for seq in (1, 2, 3)
    ]
    try:
        assert wait_until(lambda: len(started) == 2)
        time.sleep(0.5)
        assert len(started) == 2
        assert sorted(statuses()) == ["queued", "running", "running"]
    finally:
        release.set()
    assert wait_until(lambda: statuses() == ["completed"] * 3)
    assert peak == 2 and len(started) == 3


def test_scene_run_job_idempotency_replay_does_not_start_a_second_worker(client, monkeypatch) -> None:
    _create_chapter_and_scene(client)
    started: list[str] = []
    monkeypatch.setattr(
        "novel_system.api.routes.scenes.start_scene_run_job_worker",
        lambda job_id: started.append(job_id),
    )
    headers = {"X-Idempotency-Key": "scene-job-worker-once"}

    first = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs", headers=headers)
    replay = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs", headers=headers)

    assert first.status_code == replay.status_code == 200
    assert replay.headers.get("X-Idempotency-Status") == "replayed"
    assert replay.json()["data"]["job_id"] == first.json()["data"]["job_id"]
    assert started == [first.json()["data"]["job_id"]]


@pytest.mark.parametrize(
    ("node_key", "stage"),
    [
        ("budget_ready", "planning_running"),
        ("planning_ready", "bundle_built"),
        ("bundle_ready", "neutral_running"),
        ("neutral_ready", "hard_qc_running"),
        ("hard_qc_ready", "style_running"),
        ("style_ready", "soft_qc_running"),
        ("selection_wait", "awaiting_candidate_selection"),
        ("soft_qc_ready", "acceptance_review_running"),
        ("near_final_ready", "near_final"),
        ("archived", "archived"),
    ],
)
def test_running_scene_job_reports_the_stage_in_progress_not_the_checkpoint_node(
    session, node_key: str, stage: str
) -> None:
    """B03-03：检查点每存一次都把节点名（budget_ready / planning_ready …）写进任务的 current_step，
    前端的步位表只认管线阶段词，作者看到的是原样的英文词。任务视图只说一套词：检查点节点 → 进行中的阶段。"""
    _seed_job_scene(session, scene_id=f"SC_STEP_{node_key}")
    job = ChapterRunJob(
        job_id=f"scene_run_step_{node_key}",
        scene_id=f"SC_STEP_{node_key}",
        status="running",
        job_type="scene_run_full",
        worker_id="worker-a",
        attempt_no=1,
        payload_json={"current_step": node_key, "current_sub_index": 4},
        result_summary_json={"current_step": node_key, "current_sub_index": 4},
    )
    session.add(job)
    session.commit()

    serialized = SceneRunJobService(session).serialize_job(job)

    assert serialized["current_step"] == stage


def test_every_checkpoint_node_maps_into_the_scene_stage_vocabulary() -> None:
    from novel_system.services.run_job_leases import SCENE_RUN_STAGE_ORDER, scene_job_step
    from novel_system.services.scene_run_checkpoint import RUN_CHECKPOINT_ORDER

    known = {*SCENE_RUN_STAGE_ORDER, "awaiting_candidate_selection"}
    for node_key in RUN_CHECKPOINT_ORDER:
        assert scene_job_step(node_key) in known, node_key
    for token in (*SCENE_RUN_STAGE_ORDER, "queued", "preflight_blocked", "blocked", "failed", "cancelled"):
        assert scene_job_step(token) == token


def test_claimed_scene_job_starts_at_planning_not_at_the_draft(client, session, monkeypatch) -> None:
    """B03-03：认领时写的是 neutral_running（「中性稿」），而管线先做的是规划；认领写 planning_running。"""
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    job_id = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false").json()["data"]["job_id"]
    observed: list[str] = []

    class _Observer:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, *_args, **_kwargs) -> dict:
            with SessionLocal() as other:
                observed.append(SceneRunJobService(other).serialize_job(other.get(ChapterRunJob, job_id))["current_step"])
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _Observer)
    job_module._run_scene_job_worker(job_id)

    assert observed == ["planning_running"]


def test_polling_a_running_scene_job_reads_its_frozen_bundle_once(session, monkeypatch) -> None:
    """B03-10：前端运行中每 2 秒轮询一次任务，任务视图的 draft_mode 每次都去读整份冻结 bundle（实库约 200 KB）。
    冻结 bundle 不再变：同一个 bundle 的起草方式只读一次。"""
    from novel_system.db.models import SceneBundle
    from novel_system.services import style_policy as style_policy_module

    _seed_job_scene(session, scene_id="SC_DRAFT_MODE")
    session.add(
        SceneBundle(
            bundle_id="bundle_SC_DRAFT_MODE_v1",
            scene_id="SC_DRAFT_MODE",
            chapter_id="CH_SCENE_JOB",
            execution_mode="P2",
            bundle_snapshot_hash="hash",
            frozen_snapshot_json={"inline_digests": {}, "source_version_refs": {}},
        )
    )
    session.add(SceneRunState(scene_id="SC_DRAFT_MODE", current_bundle_id="bundle_SC_DRAFT_MODE_v1"))
    job = ChapterRunJob(
        job_id="scene_run_draft_mode_poll",
        scene_id="SC_DRAFT_MODE",
        status="running",
        job_type="scene_run_full",
        worker_id="worker-a",
        attempt_no=1,
        payload_json={"current_step": "bundle_ready"},
        result_summary_json={"current_step": "bundle_ready"},
    )
    session.add(job)
    session.commit()
    reads: list[int] = []
    original = style_policy_module.style_policy_for_bundle
    monkeypatch.setattr(
        style_policy_module,
        "style_policy_for_bundle",
        lambda snapshot, *args, **kwargs: reads.append(1) or original(snapshot, *args, **kwargs),
    )

    polls = [SceneRunJobService(session).serialize_job(job) for _ in range(3)]

    assert {poll["draft_mode"] for poll in polls} == {"neutral_first"}
    assert reads == [1]


def test_scene_run_job_serialization_prefers_authoritative_scene_column(session) -> None:
    _seed_job_scene(session, scene_id="SCENE_COLUMN", chapter_id="CHJOB")
    job = ChapterRunJob(
        job_id="scene_run_authoritative_scope",
        chapter_id="CHJOB",
        scene_id="SCENE_COLUMN",
        status="queued",
        job_type="scene_run_full",
        payload_json={"scene_id": "STALE_PAYLOAD", "current_step": "queued"},
        result_summary_json={"scene_id": "STALE_SUMMARY"},
    )
    session.add(job)
    session.commit()

    serialized = SceneRunJobService(session).serialize_job(job)

    assert serialized["scene_id"] == "SCENE_COLUMN"


def _create_job_block_scene(client, *, key: str, **scene_fields) -> None:
    chapter_response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": "CHJOB_BLOCK",
            "planned_scene_count": 1,
            "chapter_goal": "Run scene preflight blocker",
            "main_plot_push": "Expose blocker before worker start",
            "emotional_target": "Keep operator informed",
            "ending_effect": "Pollable blocked state",
        },
        headers={"X-Idempotency-Key": f"chapter-job-block-create-{key}"},
    )
    assert chapter_response.status_code == 200
    scene_response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "CHJOB_BLOCK_SC01",
            "chapter_id": "CHJOB_BLOCK",
            "scene_seq": 1,
            "pov_character_id": "CHAR_A",
            "onstage_chars_json": ["CHAR_A"],
            "location": "Control room",
            "scene_goal": "Expose a preflight blocker before drafting",
            "beats_json": ["start", "block"],
            "target_length_band": "short",
            "scene_type": "test",
            "is_chapter_last": 1,
            **scene_fields,
        },
        headers={"X-Idempotency-Key": f"scene-job-preflight-block-{key}"},
    )
    assert scene_response.status_code == 200


def test_scene_run_job_returns_preflight_blocker_before_starting_worker(client) -> None:
    # 场景卡自相矛盾（必须写进去的词同时被禁用）是起草前真正要作者先处理的事
    _create_job_block_scene(client, key="conflict", must_include_text="铜钥匙", forbidden_text="铜钥匙")

    response = client.post("/api/v1/scenes/CHJOB_BLOCK_SC01/run/jobs")

    assert response.status_code == 200
    job = response.json()["data"]
    assert job["status"] == "blocked"
    assert job["current_step"] == "preflight_blocked"
    assert job["error_code"] == "SCENE_CONSTRAINT_CONFLICT"
    assert job["run_preflight"]["can_run"] is False
    assert job["run_preflight"]["constraint_conflicts"][0]["term"] == "铜钥匙"
    # 下一步说的是这个冲突本身，不再是「Create or release missing knowledge cards」
    assert "禁用规则" in job["result_summary"]["next_action"]


def test_scene_run_job_is_not_blocked_by_missing_voice_or_relation_cards(client) -> None:
    """2026-09-20 真实故障：POV 声线卡 / 同场关系卡没有任何地方能写，却是每一场起草的硬前提——

    真实作品的每一次「开始起草」都以 blocked / VOICE_PROFILE_MISSING 结束。缺卡不再拦起草。
    """
    _create_job_block_scene(client, key="no-cards", onstage_chars_json=["CHAR_B", "CHAR_C"])

    response = client.post("/api/v1/scenes/CHJOB_BLOCK_SC01/run/jobs?start=false")

    assert response.status_code == 200
    job = response.json()["data"]
    assert job["status"] == "queued"
    assert job["error_code"] is None
    assert job["run_preflight"]["can_run"] is True
    assert job["run_preflight"]["blocking_items"] == []
    assert "missing_dependencies" not in job["run_preflight"]
    assert "create_actions" not in job["run_preflight"]


def test_scene_run_job_latest_qc_exposes_issue_keys_for_operator_next_action(client, session) -> None:
    _create_chapter_and_scene(client)
    session.add(
        QcReport(
            qc_report_id="qc_CHJOB_SC01_hard_v1",
            scene_id="CHJOB_SC01",
            chapter_id="CHJOB",
            qc_type="hard",
            resolution_code="hard_fail_partial",
            pass_flag=0,
            next_action="partial_rewrite",
            issues_json=[
                {
                    "issue_key": "missing_relationship_turn",
                    "message": "关系转折没有出现在动作层。",
                }
            ],
        )
    )
    session.commit()

    response = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false")

    assert response.status_code == 200
    job = response.json()["data"]
    assert job["latest_qc"]["issue_keys"] == ["missing_relationship_turn"]
    assert job["latest_qc"]["primary_issue_key"] == "missing_relationship_turn"
    assert job["latest_qc"]["next_action"] == "partial_rewrite"


def test_scene_run_job_not_found_uses_structured_error(client) -> None:
    response = client.get("/api/v1/run-jobs/missing-job")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_JOB_NOT_FOUND"


def test_scene_run_job_worker_uses_job_id_as_execution_id(client, monkeypatch) -> None:
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    response = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false")
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(self, _session) -> None:
            pass

        def run_scene(
            self,
            scene_id: str,
            author_note=None,
            run_policy="reliable",
            *,
            execution_id=None,
            run_job_id=None,
            lease_renewer=None,
        ) -> dict:
            captured.update(
                scene_id=scene_id,
                execution_id=execution_id,
                run_job_id=run_job_id,
                has_lease_renewer=callable(lease_renewer),
            )
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _FakeOrchestrator)
    job_module._run_scene_job_worker(job_id)

    assert captured == {
        "scene_id": "CHJOB_SC01",
        "execution_id": job_id,
        "run_job_id": job_id,
        "has_lease_renewer": True,
    }


def test_author_budget_resume_job_reuses_the_server_owned_failed_execution(
    client, session, monkeypatch
) -> None:
    from novel_system.services import scene_run_jobs as job_module
    from novel_system.services.orchestrator import Orchestrator as RealOrchestrator

    _create_chapter_and_scene(client)
    first = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false").json()["data"]
    first_job = session.get(ChapterRunJob, first["job_id"])
    state = session.get(SceneRunState, "CHJOB_SC01")
    assert first_job is not None and state is not None
    first_job.status = "blocked"
    first_job.error_code = "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
    first_job.error_text = "scene token budget exhausted before dispatch"
    state.active_run_job_id = None
    state.active_execution_id = first_job.job_id
    state.run_execution_status = "failed"
    state.run_checkpoint = "hard_qc_ready"
    state.run_checkpoint_json = {
        "execution_id": first_job.job_id,
        "node_key": "hard_qc_ready",
        "artifact_refs": {},
        "artifact_hashes": {},
        "superseded_execution_ids": [],
    }
    session.commit()

    response = client.post(
        "/api/v1/scenes/CHJOB_SC01/run/jobs?start=false",
        json={"resume_budget": True},
    )

    assert response.status_code == 200
    resumed_job_id = response.json()["data"]["job_id"]
    resumed_job = session.get(ChapterRunJob, resumed_job_id)
    assert resumed_job is not None
    assert resumed_job.payload_json["budget_resume_parent_execution_id"] == first_job.job_id

    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, scene_id: str, *, execution_id=None, run_job_id=None, **_kwargs) -> dict:
            captured.update(
                scene_id=scene_id,
                execution_id=execution_id,
                run_job_id=run_job_id,
            )
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _FakeOrchestrator)
    job_module._run_scene_job_worker(resumed_job_id)

    assert captured == {
        "scene_id": "CHJOB_SC01",
        "execution_id": resumed_job_id,
        "run_job_id": resumed_job_id,
    }
    session.expire_all()
    resumed_state = session.get(SceneRunState, "CHJOB_SC01")
    assert resumed_state.active_execution_id == resumed_job_id
    assert resumed_state.run_checkpoint == "hard_qc_ready"
    assert first_job.job_id in resumed_state.run_checkpoint_json["artifact_execution_lineage_ids"]
    verifier = RealOrchestrator(session)
    verifier._execution_id = resumed_job_id
    verifier._run_job_id = resumed_job_id
    assert verifier._checkpoint_execution_owner_matches(first_job.job_id, first_job.job_id)
    assert not verifier._checkpoint_execution_owner_matches(first_job.job_id, "rogue-job")
    historical_context = verifier._auto_critique_patch_context(
        "CHJOB_SC01",
        execution_id=first_job.job_id,
        run_job_id=first_job.job_id,
    )
    assert historical_context.execution_id == first_job.job_id
    assert historical_context.run_job_id == first_job.job_id


def test_budget_resume_job_rejects_when_no_budget_blocked_execution_exists(client) -> None:
    _create_chapter_and_scene(client)

    response = client.post(
        "/api/v1/scenes/CHJOB_SC01/run/jobs?start=false",
        json={"resume_budget": True},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_BUDGET_RESUME_UNAVAILABLE"


def _create_budget_resume_job(client, session) -> tuple[str, str]:
    """一场被预算闸拦下的首跑 + 作者「追加预算后续跑」建的续跑任务（未启动）→ (父任务 id, 续跑任务 id)。"""
    _create_chapter_and_scene(client)
    first = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false").json()["data"]
    first_job = session.get(ChapterRunJob, first["job_id"])
    state = session.get(SceneRunState, "CHJOB_SC01")
    assert first_job is not None and state is not None
    first_job.status = "blocked"
    first_job.error_code = "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
    state.active_run_job_id = None
    state.active_execution_id = first_job.job_id
    state.run_execution_status = "failed"
    state.run_checkpoint = "hard_qc_ready"
    state.run_checkpoint_json = {
        "execution_id": first_job.job_id,
        "node_key": "hard_qc_ready",
        "artifact_refs": {},
        "artifact_hashes": {},
        "superseded_execution_ids": [],
    }
    session.commit()
    resumed = client.post(
        "/api/v1/scenes/CHJOB_SC01/run/jobs?start=false",
        json={"resume_budget": True},
    )
    assert resumed.status_code == 200, resumed.text
    return first_job.job_id, resumed.json()["data"]["job_id"]


def test_budget_resume_job_that_died_after_its_handoff_resumes_on_recovery(
    client, session, monkeypatch
) -> None:
    """B03-02：续跑任务交接完检查点后进程退出（--reload / 崩溃），恢复时不能再要求「父执行仍是失败态」。

    修之前：再次认领后交接报 RUN_BUDGET_RESUME_UNAVAILABLE，回滚把认领一起撤掉，记失败又因不是主人抛
    RUN_OWNER_LEASE_LOST 出线程——任务永远 running、这一场永远 409，每次重启都一样。
    """
    from novel_system.services import scene_run_jobs as job_module

    parent_id, resumed_id = _create_budget_resume_job(client, session)

    class _ProcessKilled:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, *_args, **_kwargs) -> dict:
            raise SystemExit("process killed mid-run")

    monkeypatch.setattr(job_module, "Orchestrator", _ProcessKilled)
    with pytest.raises(SystemExit):
        job_module._run_scene_job_worker(resumed_id)

    session.expire_all()
    orphan = session.get(ChapterRunJob, resumed_id)
    assert orphan is not None and orphan.status == "running"
    assert session.get(SceneRunState, "CHJOB_SC01").active_execution_id == resumed_id
    # 重启：死掉的工人的租约过期了，恢复把任务再派发一次
    orphan.lease_expires_at = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    session.commit()

    calls: list[tuple[str, str | None, str | None]] = []

    class _Resumed:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, scene_id: str, *, execution_id=None, run_job_id=None, **_kwargs) -> dict:
            calls.append((scene_id, execution_id, run_job_id))
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _Resumed)
    job_module._run_scene_job_worker(resumed_id)

    assert calls == [("CHJOB_SC01", resumed_id, resumed_id)]
    session.expire_all()
    job = session.get(ChapterRunJob, resumed_id)
    assert job is not None and job.status == "completed" and job.attempt_no == 2
    state = session.get(SceneRunState, "CHJOB_SC01")
    assert state.active_run_job_id is None
    assert state.active_execution_id == resumed_id
    assert parent_id in state.run_checkpoint_json["artifact_execution_lineage_ids"]


def test_worker_failure_after_losing_its_lease_does_not_raise_out_of_the_thread(
    client, session, monkeypatch
) -> None:
    """B03-02 (c)：租约过期后别的工人接手了任务，这个工人随后失败——记失败被 fence 拒绝时只记日志，
    不能把 RUN_OWNER_LEASE_LOST 抛出工人线程，也不能碰新主人的任务。"""
    from sqlalchemy import update

    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    job_id = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false").json()["data"]["job_id"]

    class _LeaseTakenOver:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, *_args, **_kwargs) -> dict:
            with SessionLocal() as other:
                other.execute(
                    update(ChapterRunJob)
                    .where(ChapterRunJob.job_id == job_id)
                    .values(lease_expires_at=(datetime.now(UTC) - timedelta(seconds=5)).isoformat())
                )
                other.commit()
                SceneRunJobService(other).claim_running(
                    job_id,
                    worker_id="worker-after-expiry",
                    current_step="planning_running",
                    lease_seconds=600,
                )
                other.commit()
            raise DomainError("LLM_PROVIDER_FAILED", "provider failed after the lease was taken over")

    monkeypatch.setattr(job_module, "Orchestrator", _LeaseTakenOver)
    job_module._run_scene_job_worker(job_id)

    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job is not None
    assert (job.status, job.worker_id, job.attempt_no, job.error_code) == (
        "running",
        "worker-after-expiry",
        2,
        None,
    )


def test_worker_whose_claim_was_rolled_back_fails_the_unowned_job_instead_of_leaving_it_queued(
    client, session, monkeypatch
) -> None:
    """B03-02 (c)：认领在工人手里被回滚（这里：场景被别的任务占着），任务没有活着的主人——按任务 id 记失败
    （error_details.retryable，认领路径可以重领），不再永远停在 queued、也不把异常抛出线程。"""
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    job_id = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false").json()["data"]["job_id"]
    state = session.get(SceneRunState, "CHJOB_SC01")
    state.active_run_job_id = "scene_run_other_owner"
    session.commit()

    class _NeverCalled:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, *_args, **_kwargs) -> dict:
            raise AssertionError("the pipeline must not run without owning the scene")

    monkeypatch.setattr(job_module, "Orchestrator", _NeverCalled)
    job_module._run_scene_job_worker(job_id)

    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job is not None
    assert job.status == "failed" and job.error_code == "RUN_JOB_IN_PROGRESS"
    assert job.result_summary_json["error_details"]["retryable"] is True
    assert session.get(SceneRunState, "CHJOB_SC01").active_run_job_id == "scene_run_other_owner"


def test_scene_job_retry_reuses_execution_checkpoint_without_recharging(client, session, monkeypatch) -> None:
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    response = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false")
    job_id = response.json()["data"]["job_id"]
    observed_execution_ids: list[str] = []
    provider_dispatches = 0

    class _CheckpointingOrchestrator:
        def __init__(self, worker_session) -> None:
            self.session = worker_session

        def run_scene(self, scene_id: str, *args, execution_id=None, **kwargs) -> dict:  # noqa: ANN002, ANN003
            nonlocal provider_dispatches
            observed_execution_ids.append(execution_id)
            checkpoints = SceneRunCheckpointService(self.session)
            claim = checkpoints.acquire_execution(scene_id, execution_id)
            state = self.session.get(SceneRunState, scene_id)
            assert state is not None
            if claim.last_node is None:
                provider_dispatches += 1
                state.scene_tokens_used = 15
                state.scene_tokens_reserved = 4
                state.provider_attempts_used = 1
                self.session.flush()
                checkpoints.save_checkpoint(
                    scene_id=scene_id,
                    execution_id=execution_id,
                    node_key="budget_ready",
                    artifact_refs={"provider_output": "durable"},
                )
                checkpoints.mark_failed(scene_id, execution_id)
                self.session.commit()
                raise DomainError(
                    "RUN_JOB_RETRYABLE_FAILURE",
                    "fail after durable job checkpoint",
                    details={"retryable": True},
                )
            assert claim.last_node == "budget_ready"
            state.scene_status = "archived"
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _CheckpointingOrchestrator)
    job_module._run_scene_job_worker(job_id)
    job_module._run_scene_job_worker(job_id)

    session.expire_all()
    state = session.get(SceneRunState, "CHJOB_SC01")
    job = session.get(ChapterRunJob, job_id)
    assert job is not None and job.status == "completed"
    assert observed_execution_ids == [job_id, job_id]
    assert provider_dispatches == 1
    assert state.run_checkpoint == "budget_ready"
    assert state.scene_tokens_used == 15
    assert state.scene_tokens_reserved == 4
    assert state.provider_attempts_used == 1


@pytest.mark.parametrize("terminal_status", ["completed", "blocked"])
def test_terminal_scene_job_claim_is_rejected_without_mutation(session, terminal_status: str) -> None:
    _seed_job_scene(session)
    job = ChapterRunJob(
        job_id=f"job-terminal-{terminal_status}",
        scene_id="SC01",
        status=terminal_status,
        job_type="scene_run_full",
        worker_id="worker-finished",
        attempt_no=2,
        heartbeat_at="2026-07-10T01:02:03+00:00",
        lease_expires_at="2026-07-10T01:03:03+00:00",
        started_at="2026-07-10T01:00:00+00:00",
        finished_at="2026-07-10T01:02:03+00:00",
        payload_json={"current_step": terminal_status},
        result_summary_json={"current_step": terminal_status},
    )
    session.add(job)
    session.commit()
    before = {
        "status": job.status,
        "worker_id": job.worker_id,
        "attempt_no": job.attempt_no,
        "heartbeat_at": job.heartbeat_at,
        "lease_expires_at": job.lease_expires_at,
        "finished_at": job.finished_at,
        "payload_json": dict(job.payload_json or {}),
        "result_summary_json": dict(job.result_summary_json or {}),
    }

    with pytest.raises(DomainError) as exc_info:
        SceneRunJobService(session).claim_running(
            job.job_id,
            worker_id="duplicate-worker",
            current_step="neutral_running",
            lease_seconds=30,
        )

    assert exc_info.value.code == "RUN_JOB_NOT_CLAIMABLE"
    session.expire_all()
    unchanged = session.get(ChapterRunJob, job.job_id)
    assert unchanged is not None
    assert {
        "status": unchanged.status,
        "worker_id": unchanged.worker_id,
        "attempt_no": unchanged.attempt_no,
        "heartbeat_at": unchanged.heartbeat_at,
        "lease_expires_at": unchanged.lease_expires_at,
        "finished_at": unchanged.finished_at,
        "payload_json": dict(unchanged.payload_json or {}),
        "result_summary_json": dict(unchanged.result_summary_json or {}),
    } == before


@pytest.mark.parametrize(
    ("scene_status", "terminal_status"),
    [("archived", "completed"), ("human_review_required", "blocked")],
)
def test_duplicate_worker_does_not_reopen_terminal_job(
    client,
    session,
    monkeypatch,
    scene_status: str,
    terminal_status: str,
) -> None:
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene(client)
    response = client.post("/api/v1/scenes/CHJOB_SC01/run/jobs?start=false")
    job_id = response.json()["data"]["job_id"]
    provider_dispatches = 0

    class _TerminalOrchestrator:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, *_args, **_kwargs) -> dict:
            nonlocal provider_dispatches
            provider_dispatches += 1
            return {"scene_status": scene_status}

    monkeypatch.setattr(job_module, "Orchestrator", _TerminalOrchestrator)
    job_module._run_scene_job_worker(job_id)
    job_module._run_scene_job_worker(job_id)

    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job is not None
    assert job.status == terminal_status
    assert job.attempt_no == 1
    assert provider_dispatches == 1


def test_active_scene_job_lease_rejects_another_worker(session) -> None:
    _seed_job_scene(session)
    now = datetime.now(UTC)
    session.add(
        ChapterRunJob(
            job_id="job-active-lease",
            scene_id="SC01",
            status="running",
            job_type="scene_run_full",
            worker_id="worker-a",
            attempt_no=1,
            heartbeat_at=now.isoformat(),
            lease_expires_at=(now + timedelta(seconds=60)).isoformat(),
        )
    )
    session.commit()

    with pytest.raises(DomainError) as exc_info:
        SceneRunJobService(session).claim_running(
            "job-active-lease",
            worker_id="worker-b",
            current_step="neutral_running",
            lease_seconds=30,
        )
    assert exc_info.value.code == "RUN_JOB_IN_PROGRESS"


def test_expired_scene_job_lease_has_one_cas_reclaim_winner(session) -> None:
    _seed_job_scene(session)
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    session.add(
        ChapterRunJob(
            job_id="job-expired-lease",
            scene_id="SC01",
            status="running",
            job_type="scene_run_full",
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
        winner = SceneRunJobService(contender_a).claim_running(
            "job-expired-lease",
            worker_id="worker-a",
            current_step="neutral_running",
            lease_seconds=30,
        )
        contender_a.commit()
        with pytest.raises(DomainError) as loser:
            SceneRunJobService(contender_b).claim_running(
                "job-expired-lease",
                worker_id="worker-b",
                current_step="neutral_running",
                lease_seconds=30,
            )
        assert loser.value.code == "RUN_JOB_IN_PROGRESS"
        assert winner.attempt_no == 2
    finally:
        contender_a.close()
        contender_b.close()


def test_expired_scene_job_barrier_has_one_provider_budget_winner(session, monkeypatch) -> None:
    _seed_job_scene(session)
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    session.add(
        ChapterRunJob(
            job_id="job-expired-barrier",
            scene_id="SC01",
            status="running",
            job_type="scene_run_full",
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
        if not isinstance(instance, ChapterRunJob) or instance.job_id != "job-expired-barrier":
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
                owner = SceneRunJobService(db).claim_running(
                    "job-expired-barrier",
                    worker_id=worker_id,
                    current_step="neutral_running",
                    lease_seconds=30,
                )
                db.commit()
            except DomainError as exc:
                return "loser", exc.code
            with effects_lock:
                provider_budget_effects.append((worker_id, owner.attempt_no))
            return "winner", worker_id
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ("worker-a", "worker-b")))

    assert [kind for kind, _value in results].count("winner") == 1
    assert ("loser", "RUN_JOB_IN_PROGRESS") in results
    assert len(provider_budget_effects) == 1
    assert provider_budget_effects[0][1] == 2


def test_scene_job_lease_renewal_is_fenced_by_worker_and_attempt(session) -> None:
    _seed_job_scene(session)
    session.add(
        ChapterRunJob(
            job_id="job-renew-lease",
            scene_id="SC01",
            status="queued",
            job_type="scene_run_full",
            attempt_no=0,
        )
    )
    session.commit()
    owner = SceneRunJobService(session).claim_running(
        "job-renew-lease",
        worker_id="worker-a",
        current_step="neutral_running",
        lease_seconds=1,
    )
    before = owner.lease_expires_at

    renewed = owner.renew(lease_seconds=120)
    assert renewed > before
    owner.attempt_no += 1
    with pytest.raises(DomainError) as lost:
        owner.renew(lease_seconds=120)
    assert lost.value.code == "RUN_OWNER_LEASE_LOST"


def test_scene_job_detached_renewal_is_visible_to_other_sessions(session) -> None:
    _seed_job_scene(session)
    session.add(
        ChapterRunJob(
            job_id="job-renew-detached",
            scene_id="SC01",
            status="queued",
            job_type="scene_run_full",
            attempt_no=0,
        )
    )
    session.commit()
    owner = SceneRunJobService(session).claim_running(
        "job-renew-detached",
        worker_id="worker-a",
        current_step="neutral_running",
        lease_seconds=1,
    )
    session.commit()
    before = owner.lease_expires_at

    renewed = owner.renew_detached(lease_seconds=120)

    with SessionLocal() as observer:
        persisted = observer.get(ChapterRunJob, "job-renew-detached")
        assert persisted is not None
        assert persisted.lease_expires_at == renewed
        assert persisted.lease_expires_at > before


def test_scene_run_states_listing_backs_fe_queue_recovery(client, session) -> None:
    """贯通轮遗留 ①：GET /scene-run-states 是起草台队列成员的后端派生源——
    只返回离开过 ready 的场（进过管线），换浏览器后 FE 据此恢复队列。"""
    from tests.fixture_works import seed_fixture_works

    seed_fixture_works(session)
    session.commit()
    scenes = session.execute(
        select(SceneCard).where(SceneCard.project_id == "work-a", SceneCard.trashed_flag == 0)
    ).scalars().all()
    assert len(scenes) >= 2
    touched, untouched = scenes[0], scenes[1]
    for scene, status in ((touched, "human_review_required"), (untouched, "ready")):
        state = session.get(SceneRunState, scene.scene_id)
        if state is None:
            state = SceneRunState(scene_id=scene.scene_id)
            session.add(state)
        state.scene_status = status
    session.commit()

    response = client.get("/api/v1/scene-run-states?project_id=work-a")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    by_id = {item["scene_id"]: item for item in data["items"]}
    assert touched.scene_id in by_id
    assert by_id[touched.scene_id]["scene_status"] == "human_review_required"
    assert by_id[touched.scene_id]["chapter_id"] == touched.chapter_id
    # ready = 从未进管线，不参与队列恢复
    assert untouched.scene_id not in by_id

    missing = client.get("/api/v1/scene-run-states?project_id=no-such-project")
    assert missing.status_code == 404
