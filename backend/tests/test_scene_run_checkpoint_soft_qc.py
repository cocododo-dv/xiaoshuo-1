"""Scene-run checkpoint resume · soft QC / soft patch sub-checkpoints."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService

from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _FailAfterStyle,
    _HardPassClient,
    _PassSoftQc,
    _SequencedSoftQc,
    _FailNearFinal,
    _seed_resume_scene,
)

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def test_soft_qc_checkpoint_resume_does_not_repeat_qc_or_generation(session) -> None:
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

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
            orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "soft_qc_ready"
    assert soft_qc.calls == 1
    assert near_final.calls == 2
    assert len(generation_client.requests) == 2


def test_soft_qc0_checkpoint_resumes_at_patch_without_replaying_qc0(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_patch(execution_step_key: str) -> None:
        if execution_step_key == "soft_patch:soft_qc:0":
            raise RuntimeError("stop after soft QC0 checkpoint")
        original_reconcile(execution_step_key)

    first._reconcile_execution_step = stop_before_patch
    with pytest.raises(RuntimeError, match="stop after soft QC0 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc0-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    assert state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"] == "qc_CH_RESUME_SC01_soft_qc_0"
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc0-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["soft_final_qc_round"] == 1
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert state.scene_tokens_used > tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "soft_qc:0",
            )
        ).scalars().all()
    ) == 1
    assert len(
        [
            attempt
            for attempt in session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == "CH_RESUME_SC01",
                    AttemptTracker.step == "soft_qc",
                )
            ).scalars().all()
            if (attempt.details_json or {}).get("execution_step_key") == "soft_qc:0"
        ]
    ) == 1


def test_soft_patch_checkpoint_resumes_at_qc1_without_replaying_patch(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_qc1(execution_step_key: str) -> None:
        if execution_step_key == "soft_qc:1":
            raise RuntimeError("stop after soft patch checkpoint")
        original_reconcile(execution_step_key)

    first._reconcile_execution_step = stop_before_qc1
    with pytest.raises(RuntimeError, match="stop after soft patch checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-patch-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 2
    patch_row_id = state.run_checkpoint_json["artifact_refs"]["soft_patch_draft_row_id"]
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-patch-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["soft_final_draft_row_id"] == patch_row_id
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls
    assert state.scene_tokens_used == tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "soft_patch:soft_qc:0",
            )
        ).scalars().all()
    ) == 1
    assert len(
        [
            attempt
            for attempt in session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == "CH_RESUME_SC01",
                    AttemptTracker.step == "soft_patch",
                )
            ).scalars().all()
            if (attempt.details_json or {}).get("row_id") == patch_row_id
        ]
    ) == 1


def test_soft_qc_without_patch_advances_directly_to_complete_subcursor(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "continue"})
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
            orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-no-patch")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_completion_skip_reason"] == "no_patch_requested"
    assert refs["soft_final_draft_row_id"] == refs["soft_input_draft_row_id"]
    assert soft_qc.calls == ["soft_qc:0"]
    assert len(generation_client.requests) == 2


def test_soft_qc_budget_skip_persists_branch_without_patch_call(session, monkeypatch) -> None:
    from novel_system.services import scene_budget

    _seed_resume_scene(session)
    monkeypatch.setattr(scene_budget, "can_spend", lambda *args, **kwargs: False)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "patch"})
    near_final = _FailNearFinal()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=near_final,
    )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-budget-skip")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_qc0_control"] == {
        "patch_allowed": False,
        "skip_reason": "budget_or_candidate_cap",
    }
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_qc_branch"] == "patch"
    assert refs.get("soft_patch_draft_row_id") is None
    assert len(generation_client.requests) == 2


def test_soft_qc_human_review_branch_is_complete_and_resume_safe(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "human_review_required"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
        )

    first = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:soft-human-review",
    )
    provider_calls = len(generation_client.requests)
    second = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:soft-human-review",
    )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert first["scene_status"] == second["scene_status"] == "human_review_required"
    assert first["soft_qc"]["branch"] == second["soft_qc"]["branch"] == "human_review_required"
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_completion_skip_reason"] == "human_review_required"
    assert soft_qc.calls == ["soft_qc:0"]
    assert len(generation_client.requests) == provider_calls


def test_complete_soft_prefix_missing_qc0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
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
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-missing")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    session.delete(session.get(QcReport, report_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(soft_qc.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-missing")

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert soft_qc.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_soft_prefix_tampered_qc0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
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
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-tamper")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    report = session.get(QcReport, report_id)
    report.rewrite_brief_json = [{"instruction": "tampered QC0"}]
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(soft_qc.calls)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-tamper")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_soft_qc_input_is_the_selected_style_draft_with_no_critique_patch_before_it(session) -> None:
    """自动批评删了（G4 B3-1b.1）：没有绑定参考书的作品，风格稿进软 QC 之前不再先过一遍规则 / LLM 批评、发一次
    风格补丁——软 QC 是唯一提出改稿的地方。软 QC 第 0 步只记 soft_input（就是选定的风格稿本身）。"""
    _seed_resume_scene(session)
    # 章末场是「标准」关键度：以前这一级不跳批评，规则批评一命中就先发一次风格补丁
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-input-no-critique")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert refs["soft_input_draft_row_id"] == refs["soft_input_source_draft_row_id"]
    assert session.get(SceneDraft, refs["soft_input_draft_row_id"]).stage != "style_patch"
    assert not [key for key in refs if "critique" in key]
    assert not [key for key in state.run_checkpoint_json["artifact_hashes"] if "critique" in key]
    assert len(generation_client.requests) == 2
    assert session.scalar(
        select(LlmCall.llm_call_id).where(LlmCall.execution_step_key.like("%critique%"))
    ) is None


def test_soft_input_pointing_at_another_draft_blocks_resume_without_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()

    def orchestrator(soft_qc_engine) -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc_engine,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator(_FailAfterStyle()).run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-input-tamper")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = dict(state.run_checkpoint_json)
    refs = dict(payload["artifact_refs"])
    refs["soft_input_draft_row_id"] = state.current_neutral_draft_row_id
    payload["artifact_refs"] = refs
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)
    soft_qc = _PassSoftQc(session)

    with pytest.raises(DomainError) as corrupt:
        orchestrator(soft_qc).run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-input-tamper")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 0
    assert len(generation_client.requests) == provider_calls
