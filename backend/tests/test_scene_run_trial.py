"""快速试写（``stop_after="hard_qc"``）与「继续跑完」（``continue_trial``）：任务载荷、检查点交接、真管线只跑到硬 QC、
继续之后从风格稿往下跑且不重做规划 / 首稿 / 硬 QC，以及各种拒绝。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from novel_system.db.models import ChapterRunJob, LlmCall, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run_checkpoint import (
    CHECKPOINT_FORMAT,
    TRIAL_SCENE_STATUS,
    SceneRunCheckpointService,
    scene_run_outcome,
)
from tests.support.catalog import job_chapter_and_scene as _create_chapter_and_scene
from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _HardPassClient,
    _PassNearFinal,
    _PassSoftQc,
    _seed_resume_scene,
)

SCENE = "CHJOB_SC01"


def _post(client, body: dict):
    return client.post(f"/api/v1/scenes/{SCENE}/run/jobs?start=false", json=body)


def _fake_trial_finished(session, job_id: str, *, author_note: str | None = None) -> ChapterRunJob:
    """一次试写跑完之后的服务端状态：任务完成、执行完成、检查点停在 hard_qc_ready、场景停在试写稿上。"""
    first = session.get(ChapterRunJob, job_id)
    payload = dict(first.payload_json or {})
    payload.update({"stop_after": "hard_qc", "run_policy": "strict", **({"author_note": author_note} if author_note else {})})
    first.payload_json = payload
    first.status = "completed"
    state = session.get(SceneRunState, SCENE)
    state.active_run_job_id = None
    state.active_execution_id = first.job_id
    state.run_execution_status = "completed"
    state.run_checkpoint = "hard_qc_ready"
    state.scene_status = TRIAL_SCENE_STATUS
    state.run_checkpoint_json = {
        "execution_id": first.job_id,
        "checkpoint_format": CHECKPOINT_FORMAT,
        "node_key": "hard_qc_ready",
        "artifact_refs": {},
        "artifact_hashes": {},
        "superseded_execution_ids": [],
    }
    session.commit()
    return first


# ---------------------------------------------------------------------------
# 任务层
# ---------------------------------------------------------------------------


def test_a_trial_job_records_stop_after_and_the_outcome_counts_as_completed(client, session, monkeypatch) -> None:
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene()
    response = _post(client, {"stop_after": "hard_qc", "run_policy": "strict"})
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    assert session.get(ChapterRunJob, job_id).payload_json["stop_after"] == "hard_qc"
    assert scene_run_outcome(TRIAL_SCENE_STATUS) == "completed"

    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, scene_id: str, **kwargs) -> dict:
            captured.update(kwargs)
            return {"scene_status": TRIAL_SCENE_STATUS}

    monkeypatch.setattr(job_module, "Orchestrator", _FakeOrchestrator)
    job_module._run_scene_job_worker(job_id)
    assert captured["stop_after"] == "hard_qc"
    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job.status == "completed" and job.result_summary_json["current_step"] == TRIAL_SCENE_STATUS


def test_bad_trial_requests_are_refused(client, session) -> None:
    _create_chapter_and_scene()
    assert _post(client, {"stop_after": "soft_qc"}).status_code == 422
    unavailable = _post(client, {"continue_trial": True})
    assert unavailable.status_code == 409 and unavailable.json()["error"]["code"] == "RUN_TRIAL_RESUME_UNAVAILABLE"
    conflict = _post(client, {"continue_trial": True, "stop_after": "hard_qc"})
    assert conflict.status_code == 422 and conflict.json()["error"]["code"] == "RUN_TRIAL_RESUME_CONFLICT"


def test_continue_trial_hands_the_checkpoint_to_a_new_job_with_the_trials_note_and_policy(client, session, monkeypatch) -> None:
    from novel_system.services import scene_run_jobs as job_module

    _create_chapter_and_scene()
    trial_job_id = _post(client, {"stop_after": "hard_qc"}).json()["data"]["job_id"]
    first = _fake_trial_finished(session, trial_job_id, author_note="保留雨声的节奏")

    response = _post(client, {"continue_trial": True, "author_note": "这一句会被忽略"})
    assert response.status_code == 200
    resumed_id = response.json()["data"]["job_id"]
    resumed = session.get(ChapterRunJob, resumed_id)
    assert resumed.payload_json["trial_resume_parent_execution_id"] == first.job_id
    assert resumed.payload_json["author_note"] == "保留雨声的节奏" and resumed.payload_json["run_policy"] == "strict"
    assert "stop_after" not in resumed.payload_json

    captured: dict[str, object] = {}

    class _FakeOrchestrator:
        def __init__(self, _session) -> None:
            pass

        def run_scene(self, scene_id: str, **kwargs) -> dict:
            captured.update(kwargs)
            return {"scene_status": "archived"}

    monkeypatch.setattr(job_module, "Orchestrator", _FakeOrchestrator)
    job_module._run_scene_job_worker(resumed_id)
    assert captured["execution_id"] == resumed_id and "stop_after" not in captured
    assert captured["author_note"] == "保留雨声的节奏"
    session.expire_all()
    state = session.get(SceneRunState, SCENE)
    assert state.active_execution_id == resumed_id and state.run_checkpoint == "hard_qc_ready"
    assert first.job_id in state.run_checkpoint_json["artifact_execution_lineage_ids"]
    assert state.run_checkpoint_json["trial_resume_parent_execution_id"] == first.job_id
    # 交接过一次就不能再从同一份试写接第二次
    again = _post(client, {"continue_trial": True})
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# 真管线
# ---------------------------------------------------------------------------


def _calls(session, node_id: str) -> int:
    return int(session.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.node_id == node_id)) or 0)


@pytest.mark.usefixtures("online_orchestrator_runner")
def test_a_trial_stops_after_hard_qc_and_continuing_archives_without_redoing_the_first_half(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    trial = orchestrator().run_scene("CH_RESUME_SC01", execution_id="exec-trial", stop_after="hard_qc")
    assert trial["scene_status"] == TRIAL_SCENE_STATUS and trial["trial"] is True
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert (state.run_execution_status, state.run_checkpoint) == ("completed", "hard_qc_ready")
    assert state.current_neutral_draft_row_id, "试写稿就是那份首稿"
    assert soft_qc.calls == 0 and near_final.calls == 0
    first_half = {node: _calls(session, node) for node in ("neutral_draft", "hard_qc", "scene_blueprint")}
    generation_after_trial = len(generation_client.requests)

    # 不是这一次执行 / 不是试写稿：不交接
    with pytest.raises(DomainError) as wrong_parent:
        SceneRunCheckpointService(session).acquire_trial_resume(
            "CH_RESUME_SC01", "exec-continue", expected_parent_execution_id="someone-else"
        )
    assert wrong_parent.value.code == "RUN_TRIAL_RESUME_UNAVAILABLE"
    session.rollback()

    SceneRunCheckpointService(session).acquire_trial_resume(
        "CH_RESUME_SC01", "exec-continue", expected_parent_execution_id="exec-trial"
    )
    session.commit()
    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id="exec-continue")

    assert result["scene_status"] == "archived"
    assert soft_qc.calls == 1 and near_final.calls == 1
    assert {node: _calls(session, node) for node in first_half} == first_half, "规划 / 首稿 / 硬 QC 没有重跑"
    assert len(generation_client.requests) > generation_after_trial, "风格稿是接着写的"
    session.refresh(state)
    assert state.run_checkpoint == "archived" and state.run_execution_status == "completed"
