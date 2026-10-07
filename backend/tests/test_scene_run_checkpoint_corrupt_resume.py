"""Scene-run checkpoint resume · corrupt or missing checkpoint products block resume without provider replay."""

from __future__ import annotations

import pytest

from novel_system.db.models import FinalScene, QcReport, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService

from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _HardPassClient,
    _FailAfterStyle,
    _PassSoftQc,
    _FailNearFinal,
    _PassNearFinal,
    _FailArchiveOnce,
    _seed_resume_scene,
)

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def test_missing_soft_qc_checkpoint_row_blocks_without_repeating_provider(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-missing")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc_report_id"]
    session.delete(session.get(QcReport, report_id))
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-missing")
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_soft_qc_report_content_hash_blocks_without_repeating_provider(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-report-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc_report_id"]
    report = session.get(QcReport, report_id)
    report.rewrite_brief_json = [{"instruction": "tampered carry note"}]
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-report-corrupt")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_near_final_checkpoint_source_blocks_without_repeating_provider(session) -> None:
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

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    final_row_id = state.run_checkpoint_json["artifact_refs"]["final_scene_row_id"]
    final_scene = session.get(FinalScene, final_row_id)
    final_scene.source_bundle_hash = "sha256:tampered"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:near-corrupt")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_near_final_carry_notes_hash_blocks_without_repeating_provider(session) -> None:
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

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-carry-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = dict(state.run_checkpoint_json)
    refs = dict(payload["artifact_refs"])
    refs["carry_notes"] = [*(refs.get("carry_notes") or []), {"kind": "tampered"}]
    payload["artifact_refs"] = refs
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:near-carry-corrupt")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_hard_qc_source_binding_blocks_checkpoint_resume(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:hard-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    hard_report_id = state.run_checkpoint_json["artifact_refs"]["qc_report_id"]
    report = session.get(QcReport, hard_report_id)
    report.source_draft_row_id = "draft_from_another_execution"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:hard-corrupt")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls


def test_corrupt_soft_qc_decision_hash_blocks_checkpoint_resume(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    checkpoint = dict(state.run_checkpoint_json)
    refs = dict(checkpoint["artifact_refs"])
    refs["soft_qc_branch"] = "waive"
    checkpoint["artifact_refs"] = refs
    state.run_checkpoint_json = checkpoint
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-corrupt")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_checkpoint_left_by_an_older_format_refuses_resume_and_a_fresh_run_takes_over(session) -> None:
    """进程在旧版本的一次运行半路上退出（执行还是 active，检查点停在软 QC 之后、没有当前格式标记）：续跑不复验旧产品，
    回 409 RUN_CHECKPOINT_FORMAT_OUTDATED 与作者指引，这次执行落成 failed；作者重新起草，新的执行从头跑到归档。"""
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)

    def orchestrator(near_final) -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator(_FailNearFinal()).run_scene("CH_RESUME_SC01", execution_id="idempotency:format-old")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    payload = dict(state.run_checkpoint_json)
    payload.pop("checkpoint_format", None)
    state.run_checkpoint_json = payload
    state.run_execution_status = "active"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as outdated:
        orchestrator(_PassNearFinal(session)).run_scene("CH_RESUME_SC01", execution_id="idempotency:format-old")
    assert outdated.value.code == "RUN_CHECKPOINT_FORMAT_OUTDATED"
    assert outdated.value.details["author_action"]["message"] == "这一场上次的运行是旧版本留下的，请重新起草（会重新调用模型）"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls
    session.rollback()
    session.expire_all()
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.active_execution_id == "idempotency:format-old"
    assert state.run_execution_status == "failed"

    result = orchestrator(_PassNearFinal(session)).run_scene("CH_RESUME_SC01", execution_id="idempotency:format-fresh")
    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) > provider_calls
