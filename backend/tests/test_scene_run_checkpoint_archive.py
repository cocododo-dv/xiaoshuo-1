"""Scene-run checkpoint resume · archive sub-checkpoints and chapter-last aggregation."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import partial

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRunJob,
    ChapterRollingNote,
    FinalScene,
    LlmCall,
    LlmCallAttempt,
    NarrativeEvent,
    SceneCard,
    SceneMemory,
    SceneRunState,
    VolumeSummary,
    WriterEvaluation,
)
from novel_system.services.errors import DomainError
from novel_system.services.aggregator import VOLUME_CHAPTER_SPAN, Aggregator, is_chapter_aggregate_of
from novel_system.services.archiver import Archiver
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run import archive as scene_run_archive

# Importing the autouse fixture runs every test here against the accounted online fake provider.
from tests.support.checkpoint_fakes import _accounted_online_default_orchestrator_runner  # noqa: F401
from tests.support.checkpoint_fakes import _CountingGenerationClient, _HardPassClient, _response, _seed_resume_scene


def test_post_archive_failure_retries_missing_side_effects_before_archived_checkpoint(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    post_archive_attempts = 0

    def _fail_post_archive(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal post_archive_attempts
        post_archive_attempts += 1
        if post_archive_attempts == 1:
            raise RuntimeError("post archive failure")

    first._record_narrative_events = _fail_post_archive
    with pytest.raises(RuntimeError, match="post archive failure"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:archived-replay")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 4
    assert state.run_execution_status == "failed"
    assert state.scene_status != "archived"
    assert session.scalar(
        select(func.count()).select_from(SceneMemory).where(
            SceneMemory.scene_id == "CH_RESUME_SC01"
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(ChapterRollingNote).where(
            ChapterRollingNote.scene_id == "CH_RESUME_SC01"
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "archive",
        )
    ) == 1
    provider_calls = len(generation_client.requests)

    resumed = orchestrator()
    resumed._record_narrative_events = _fail_post_archive
    replay = resumed.run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:archived-replay",
    )

    assert replay["scene_status"] == "archived"
    session.refresh(state)
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert post_archive_attempts == 2
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(SceneMemory).where(
            SceneMemory.scene_id == "CH_RESUME_SC01"
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "archive",
        )
    ) == 1


def test_archive_rule_events_step_records_an_empty_product(session) -> None:
    """归档第 5 步不再按场景计划写规则事件（它们从来没人读，B11-02）：产品是空的 rule_events，续跑照常。"""
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:archive-rule-events-empty"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after rule event checkpoint")
    )
    with pytest.raises(RuntimeError, match="stop after rule event checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 5
    assert refs["archive_rule_event_ids"] == []
    assert refs["archive_rule_events"] == []
    assert refs["archive_rule_product"]["kind"] == "rule_events"
    assert refs["archive_rule_product"]["outcome"] == "recorded"
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(NarrativeEvent).where(
            NarrativeEvent.scene_id == "CH_RESUME_SC01",
            NarrativeEvent.authority_status == "planned",
        )
    ) == 0


def test_checkpoint_written_with_rule_events_by_the_old_code_still_resumes(session) -> None:
    """改之前写下的检查点：第 5 步里记着规则事件。续跑照原样校验这几行、不重写、不重复。"""
    from novel_system.services.narrative_event_log import NarrativeEventLog

    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:archive-rule-events-legacy"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    def legacy_rule_events(scene, _contract, content, *, final_scene_row_id=None, **_kwargs):  # noqa: ANN001, ANN202
        event = NarrativeEventLog(session).log_event(
            project_id=scene.project_id or "P_RESUME",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            event_type="character_state",
            entity_type="character",
            entity_id="CHAR_A",
            fact_key="appeared_in_scene",
            fact_value=scene.scene_id,
            source_text_excerpt=content[:200],
            authority_status="planned",
            source_kind="scene_plan",
            final_scene_row_id=final_scene_row_id,
        )
        return [event.event_id]

    first = orchestrator()
    first._record_narrative_events = legacy_rule_events
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after legacy rule event checkpoint")
    )
    with pytest.raises(RuntimeError, match="stop after legacy rule event checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 5
    assert len(refs["archive_rule_event_ids"]) == 1
    assert {"causal_predecessor_id", "theme_tags", "obligation_ids", "created_at", "payload_json"}.issubset(
        refs["archive_rule_events"][0]
    )
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(NarrativeEvent).where(
            NarrativeEvent.scene_id == "CH_RESUME_SC01",
            NarrativeEvent.authority_status == "planned",
        )
    ) == 1


def test_archive_prose_checkpoint_is_durable_and_tamper_blocks_before_next_step(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-tamper"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_CountingGenerationClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_vector_index = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after prose checkpoint")
    )
    with pytest.raises(RuntimeError, match="stop after prose checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 6
    assert state.run_checkpoint_json["artifact_refs"]["archive_prose_product"]["outcome"] == "not_invoked"
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_prose_product"]["reason"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    resumed = orchestrator()
    resumed._run_archive_vector_index = lambda *_args, **_kwargs: pytest.fail(
        "tampered prose prefix must block before the vector slot"
    )
    with pytest.raises(DomainError) as corrupt:
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_prose_no_call_with_released_tombstone_never_advances_checkpoint(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-released-gate-closed"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_CountingGenerationClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop at rule prefix")
    )
    with pytest.raises(RuntimeError, match="stop at rule prefix"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 5

    session.add(
        LlmCall(
            llm_call_id="llm_archive_prose_released",
            provider="fake",
            model="fake",
            node_id="extraction",
            step="archive:prose_event_extract:0",
            project_id="P_RESUME",
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            scope_type="scene",
            scope_id="CH_RESUME_SC01",
            execution_id=execution_id,
            execution_step_key="archive:prose_event_extract:0",
            request_payload_summary={"_accounting_provider_execution_mode": "online"},
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0,
            estimated_tokens=0,
            reserved_tokens=0,
            budget_charged_tokens=0,
            accounting_status="released",
            settled_at="2026-07-14T00:00:00Z",
        )
    )
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 5
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:prose_event_extract:0",
        )
    ) == 1


def _add_settled_archive_parent(
    session,
    *,
    call_id: str,
    execution_id: str,
    step_key: str,
    node_id: str,
    scope_type: str,
    scope_id: str,
    scene_id: str | None,
) -> None:
    session.add(
        LlmCall(
            llm_call_id=call_id,
            provider="fake",
            model="fake",
            node_id=node_id,
            step=step_key if node_id == "extraction" else "chapter_near_final_review",
            project_id="P_RESUME",
            scene_id=scene_id,
            chapter_id="CH_RESUME",
            scope_type=scope_type,
            scope_id=scope_id,
            execution_id=execution_id,
            execution_step_key=step_key,
            request_payload_summary={"_accounting_provider_execution_mode": "online"},
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            latency_ms=10,
            estimated_tokens=12,
            reserved_tokens=12,
            budget_charged_tokens=12,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:01Z",
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id=f"attempt_{call_id}",
            llm_call_id=call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=4,
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            latency_ms=10,
            estimated_tokens=12,
            reserved_tokens=12,
            budget_charged_tokens=12,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:01Z",
        )
    )
    session.commit()


def test_archive_prose_settled_parent_without_product_blocks_resend_and_budget_growth(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-parent-only"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop before prose parent")
    )
    with pytest.raises(RuntimeError, match="stop before prose parent"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 5
    call_id = "llm_archive_prose_parent_only"
    _add_settled_archive_parent(
        session,
        call_id=call_id,
        execution_id=execution_id,
        step_key="archive:prose_event_extract:0",
        node_id="extraction",
        scope_type="scene",
        scope_id="CH_RESUME_SC01",
        scene_id="CH_RESUME_SC01",
    )
    tokens_before = state.scene_tokens_used
    counters = (12, 12, 12)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent = session.get(LlmCall, call_id)
    assert (parent.total_tokens, parent.reserved_tokens, parent.budget_charged_tokens) == counters
    assert session.scalar(
        select(func.count()).select_from(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == call_id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:prose_event_extract:0",
        )
    ) == 1
    session.refresh(state)
    assert state.scene_tokens_used == tokens_before


def test_chapter_evaluation_settled_parent_without_row_blocks_resend_and_budget_growth(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    execution_id = "idempotency:archive-chapter-parent-only"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop before chapter parent")
    )
    with pytest.raises(RuntimeError, match="stop before chapter parent"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 9
    call_id = "llm_archive_chapter_parent_only"
    _add_settled_archive_parent(
        session,
        call_id=call_id,
        execution_id=execution_id,
        step_key="archive:chapter_near_final:0",
        node_id="chapter_near_final_review",
        scope_type="chapter",
        scope_id=scene.chapter_id,
        scene_id=None,
    )
    tokens_before = state.scene_tokens_used

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene(scene.scene_id, execution_id=execution_id)
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent = session.get(LlmCall, call_id)
    assert (parent.total_tokens, parent.reserved_tokens, parent.budget_charged_tokens) == (12, 12, 12)
    assert session.scalar(
        select(func.count()).select_from(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == call_id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:chapter_near_final:0",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == scene.chapter_id,
        )
    ) == 0
    session.refresh(state)
    assert state.scene_tokens_used == tokens_before


def test_archive_stage_seven_is_a_retired_slot_that_resumes_and_is_tamper_checked(session) -> None:
    """[批准#1]（重评 R1）：归档第 7 步的向量索引已退役——槽位还在（sub_index 7、清单条目），记一份 retired 空产品；
    停在它之后的检查点照样续跑，产品被改动照样拦。"""
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-vector-retired"
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_chapter_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after the retired vector slot")
    )
    with pytest.raises(RuntimeError, match="stop after the retired vector slot"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 7
    product = state.run_checkpoint_json["artifact_refs"]["archive_vector_product"]
    assert product["kind"] == "vector_index"
    assert product["outcome"] == "retired"
    assert product["step_key"] == "archive:vector_index:0"
    assert not {"backend", "collection_name", "write_status"} & set(product)

    tampered = deepcopy(state.run_checkpoint_json)
    tampered["artifact_refs"]["archive_vector_product"]["reason"] = "tampered"
    original = state.run_checkpoint_json
    state.run_checkpoint_json = tampered
    session.commit()
    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"

    session.refresh(state)
    state.run_checkpoint_json = original
    state.run_execution_status = "failed"
    session.commit()
    provider_calls = len(generation_client.requests)
    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == provider_calls
    manifest = session.get(SceneRunState, "CH_RESUME_SC01").run_checkpoint_json["artifact_refs"]["archive_manifest"]
    assert [(entry["sub_index"], entry["kind"]) for entry in manifest][3] == (7, "vector_index")


def test_non_chapter_last_writes_fixed_archive_products_and_ordered_manifest(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-fixed-non-last"
    result = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["archive_chapter_product"]["outcome"] == "not_applicable"
    assert refs["archive_volume_product"]["outcome"] == "not_applicable"
    assert refs["archive_chapter_evaluation_product"]["outcome"] == "not_applicable"
    # 风格参考 v3：sub 11（archive:style_drift:0）不再做漂移读数，改记「像不像」读数（P5b 接上）：
    # 绑定了参考 → recorded + reading_id（见 test_style_fidelity_pipeline_v3）；这一场没绑定 → not_applicable。
    assert refs["archive_drift_product"]["outcome"] == "not_applicable"
    assert refs["archive_drift_product"]["reason"] == "unbound"
    assert [entry["sub_index"] for entry in refs["archive_manifest"]] == list(range(4, 12))


def test_archived_fast_path_revalidates_full_manifest_before_return(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-manifest-tamper"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_manifest"][0]["product_hash"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_core_checkpoint_keeps_independent_snapshot_hashes_not_the_bodies(session) -> None:
    """检查点格式 v2：第 4 步的四份行快照（终稿、场景记忆、章滚动笔记、归档尝试）各记一个独立的哈希，快照本身
    不进检查点——以前产品里存一份、另外每份再单独存一遍，整场正文在这一步就出现六次，之后每次存检查点都整份重写；
    准终稿评审的修订候选快照（带整份来源稿）也只记哈希。续跑时按库里的行重算快照核对哈希（改了任何一行都判损坏，
    见下一条）。"""
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-core-snapshots"
    Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    hashes = state.run_checkpoint_json["artifact_hashes"]
    snapshot_keys = {
        "archive_final_scene_snapshot",
        "archive_scene_memory_snapshot",
        "archive_rolling_note_snapshot",
        "archive_attempt_snapshot",
    }
    assert snapshot_keys.issubset(hashes)
    assert not snapshot_keys & set(refs)
    core = refs["archive_core"]
    assert core["schema_version"] == 2
    assert {
        core["final_scene_snapshot_hash"],
        core["scene_memory_snapshot_hash"],
        core["rolling_note_snapshot_hash"],
        core["archive_attempt_snapshot_hash"],
    } == {hashes[key] for key in snapshot_keys}
    final_text = session.get(FinalScene, refs["final_scene_row_id"]).content
    assert final_text and final_text not in json.dumps(core, ensure_ascii=False)
    # 准终稿评审的修订候选同理：只记哈希，不再存带整份来源稿的快照
    assert not {"near_eval0_candidate_snapshot", "near_eval1_candidate_snapshot"} & set(refs)
    assert {"near_eval0_candidate", "near_eval1_candidate"} & set(hashes)


@pytest.mark.parametrize(
    "mutation",
    [
        "final_source_bundle_hash",
        "memory_runtime_basis",
        "rolling_revision",
        "attempt_qc_tamper",
        "attempt_qc_delete",
    ],
)
def test_archive_core_snapshot_field_tamper_blocks_archived_fast_path(session, mutation) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-core-field-{mutation}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    if mutation == "final_source_bundle_hash":
        session.get(FinalScene, refs["final_scene_row_id"]).source_bundle_hash = "tampered"
    elif mutation == "memory_runtime_basis":
        session.get(SceneMemory, refs["scene_memory_row_id"]).runtime_eligibility_basis = "tampered"
    elif mutation == "rolling_revision":
        rolling = session.get(ChapterRollingNote, refs["archive_core"]["chapter_rolling_note_row_id"])
        rolling.revision_no += 1
    else:
        attempt = session.get(AttemptTracker, refs["archive_core"]["archive_attempt_id"])
        details = dict(attempt.details_json or {})
        if mutation == "attempt_qc_delete":
            details.pop("qc_report_id", None)
        else:
            details["qc_report_id"] = "tampered"
        attempt.details_json = details
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archived_worker_fast_path_restores_real_run_job_owner_and_rejects_wrong_job(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    state = session.get(SceneRunState, scene.scene_id)
    job_id = "scene-job-archive-fast-path"
    session.add(
        ChapterRunJob(
            job_id=job_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            status="running",
            job_type="scene_run_full",
            payload_json={"scene_id": scene.scene_id},
        )
    )
    state.active_run_job_id = job_id
    session.commit()
    execution_id = "scene-job-archive-execution"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    assert orchestrator().run_scene(
        scene.scene_id,
        execution_id=execution_id,
        run_job_id=job_id,
    )["scene_status"] == "archived"
    assert orchestrator().run_scene(
        scene.scene_id,
        execution_id=execution_id,
        run_job_id=job_id,
    )["scene_status"] == "archived"

    wrong_job_id = "scene-job-archive-wrong"
    session.add(
        ChapterRunJob(
            job_id=wrong_job_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            status="running",
            job_type="scene_run_full",
            payload_json={"scene_id": scene.scene_id},
        )
    )
    session.commit()
    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene(
            scene.scene_id,
            execution_id=execution_id,
            run_job_id=wrong_job_id,
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_chapter_last_archive_uses_real_chapter_scope_and_aggregate_inputs(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.project_id = None
    session.commit()
    execution_id = "idempotency:archive-chapter-last"

    result = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, scene.scene_id)
    refs = state.run_checkpoint_json["artifact_refs"]
    chapter_product = refs["archive_chapter_product"]
    assert chapter_product["outcome"] == "aggregated"
    assert chapter_product["inputs"] == sorted(
        chapter_product["inputs"], key=lambda item: item["row_id"]
    )
    evaluation_product = refs["archive_chapter_evaluation_product"]
    assert evaluation_product["outcome"] == "evaluated"
    parent = session.get(LlmCall, evaluation_product["evaluator_llm_call_id"])
    assert parent.scope_type == "chapter"
    assert parent.scope_id == scene.chapter_id
    assert parent.chapter_id == scene.chapter_id
    assert parent.scene_id is None
    assert parent.execution_step_key == "archive:chapter_near_final:0"

    Aggregator(session).run_final_aggregate(scene.chapter_id)
    session.commit()
    replay = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene(scene.scene_id, execution_id=execution_id)
    assert replay["scene_status"] == "archived"


@pytest.mark.parametrize("sub_index", [8, 9, 10, 11])
def test_archive_subcursor_8_to_11_resumes_without_replaying_prefix(session, sub_index) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-subcursor-{sub_index}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    if sub_index == 8:
        first._run_archive_volume_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after sub8")
        )
    elif sub_index == 9:
        first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after sub9")
        )
    elif sub_index == 10:
        original_product = first._archive_product

        def _stop_before_sub11(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if kwargs.get("kind") == "style_drift":
                raise RuntimeError("stop after sub10")
            return original_product(*args, **kwargs)

        first._archive_product = _stop_before_sub11
    else:
        first._archive_manifest = lambda: (_ for _ in ()).throw(RuntimeError("stop after sub11"))

    with pytest.raises(RuntimeError, match=f"stop after sub{sub_index}"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == sub_index
    assert state.scene_status != "archived"
    prefix_hashes = deepcopy(state.run_checkpoint_json["artifact_hashes"])

    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    session.refresh(state)
    for key, value in prefix_hashes.items():
        assert state.run_checkpoint_json["artifact_hashes"][key] == value


@pytest.mark.parametrize(
    ("sub_index", "product_key"),
    [
        (8, "archive_chapter_product"),
        (9, "archive_volume_product"),
        (10, "archive_chapter_evaluation_product"),
        (11, "archive_drift_product"),
    ],
)
def test_archive_subcursor_8_to_11_tamper_blocks_resume(session, sub_index, product_key) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-subcursor-tamper-{sub_index}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    if sub_index == 8:
        first._run_archive_volume_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
    elif sub_index == 9:
        first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
    elif sub_index == 10:
        original_product = first._archive_product
        first._archive_product = lambda *args, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("stop"))
            if kwargs.get("kind") == "style_drift"
            else original_product(*args, **kwargs)
        )
    else:
        first._archive_manifest = lambda: (_ for _ in ()).throw(RuntimeError("stop"))
    with pytest.raises(RuntimeError, match="stop"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"][product_key]["outcome"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_chapter_last_sub8_crash_does_not_create_second_chapter_memory(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    execution_id = "idempotency:chapter-last-sub8-crash"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_volume_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after real sub8")
    )
    with pytest.raises(RuntimeError, match="stop after real sub8"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 8
    row_id = state.run_checkpoint_json["artifact_refs"]["archive_chapter_product"]["chapter_memory"]["row_id"]
    assert session.scalar(
        select(func.count()).select_from(ChapterMemory).where(
            ChapterMemory.chapter_id == scene.chapter_id,
            ChapterMemory.aggregate_stage == "final",
        )
    ) == 1

    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"
    assert session.scalar(
        select(func.count()).select_from(ChapterMemory).where(
            ChapterMemory.chapter_id == scene.chapter_id,
            ChapterMemory.aggregate_stage == "final",
        )
    ) == 1
    assert session.get(ChapterMemory, row_id) is not None


def test_chapter_last_sub9_volume_boundary_crash_reuses_same_summary(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    chapter = session.get(ChapterGoal, scene.chapter_id)
    chapter.display_order = 5
    for ordinal in range(1, 5):
        chapter_id = f"CH_RESUME_{ordinal}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id="P_RESUME",
                display_order=ordinal,
                chapter_goal=f"prior {ordinal}",
            )
        )
        session.add(
            ChapterMemory(
                row_id=f"chapter_memory_final_{chapter_id}_v1",
                chapter_id=chapter_id,
                aggregate_stage="final",
                content=f"prior atmosphere {ordinal}",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            )
        )
    session.commit()
    execution_id = "idempotency:chapter-last-sub9-crash"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after real sub9")
    )
    with pytest.raises(RuntimeError, match="stop after real sub9"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 9
    volume_product = state.run_checkpoint_json["artifact_refs"]["archive_volume_product"]
    assert volume_product["outcome"] == "aggregated"
    row_id = volume_product["volume_summary"]["row_id"]
    assert session.scalar(select(func.count()).select_from(VolumeSummary)) == 1

    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"
    assert session.scalar(select(func.count()).select_from(VolumeSummary)) == 1
    assert session.get(VolumeSummary, row_id).active_flag == 1
    window = [f"CH_RESUME_{ordinal}" for ordinal in range(1, 5)] + [scene.chapter_id]
    Aggregator(session).aggregate_volume_summary("P_RESUME", 1, window)
    session.commit()
    assert session.get(VolumeSummary, row_id).active_flag == 0
    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"


def test_chapter_last_sub10_crash_reuses_evaluation_parent_and_budget(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    execution_id = "idempotency:chapter-last-sub10-crash"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    original_product = first._archive_product
    first._archive_product = lambda *args, **kwargs: (
        (_ for _ in ()).throw(RuntimeError("stop after real sub10"))
        if kwargs.get("kind") == "style_drift"
        else original_product(*args, **kwargs)
    )
    with pytest.raises(RuntimeError, match="stop after real sub10"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 10
    product = state.run_checkpoint_json["artifact_refs"]["archive_chapter_evaluation_product"]
    parent = session.get(LlmCall, product["evaluator_llm_call_id"])
    counters = (
        parent.estimated_tokens,
        parent.reserved_tokens,
        parent.budget_charged_tokens,
        parent.total_tokens,
    )
    call_count = session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:chapter_near_final:0",
        )
    )
    evaluation_count = session.scalar(
        select(func.count()).select_from(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == scene.chapter_id,
        )
    )

    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:chapter_near_final:0",
        )
    ) == call_count
    assert session.scalar(
        select(func.count()).select_from(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == scene.chapter_id,
        )
    ) == evaluation_count
    session.refresh(parent)
    assert (
        parent.estimated_tokens,
        parent.reserved_tokens,
        parent.budget_charged_tokens,
        parent.total_tokens,
    ) == counters


# ---------------------------------------------------------------------------------------------- 章汇总的输入（R13）


def _archived_sibling(session, scene_id: str, *, scene_seq: int, text: str) -> None:
    """同一章里先归档好的另一场（不走流水线：只落终稿、归档）。"""
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id="CH_RESUME",
            project_id="P_RESUME",
            scene_seq=scene_seq,
            scene_goal="sibling",
            is_chapter_last=0,
        )
    )
    final_id = f"final_scene_{scene_id}_v1"
    session.add(SceneRunState(scene_id=scene_id, scene_status="ready", current_final_scene_row_id=final_id))
    session.add(
        FinalScene(
            row_id=final_id,
            scene_id=scene_id,
            chapter_id="CH_RESUME",
            content=text,
            status="draft",
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash=f"hash_{scene_id}",
        )
    )
    session.flush()
    Archiver(session).archive_final_scene(scene_id, final_id, carry_notes_json=[], author_confirmed_final=True)
    session.commit()


class _FixedTextGenerationClient(_CountingGenerationClient):
    """起草的每个节点都交回同一段正文：章末那一场的终稿就是它，测试才好让别的场含着它。"""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def generate(self, request):  # noqa: ANN001, ANN201
        self.requests.append(request)
        return _response({"scene_text": self.text}, f"generation-{len(self.requests)}")


def _stop_after_sub9(
    session, execution_id: str, *, generation_client: _CountingGenerationClient | None = None
) -> dict:
    """章末那一场跑到归档第 10 步（章级准终稿评审）前停下；返回检查点里的章汇总产品。"""
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session, llm_client=generation_client or _CountingGenerationClient()
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    )
    first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after sub9")
    )
    with pytest.raises(RuntimeError, match="stop after sub9"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 9
    return state.run_checkpoint_json["artifact_refs"]["archive_chapter_product"]


def _resume(session, execution_id: str) -> dict:
    return Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)


def test_chapter_last_archive_leaves_a_trashed_sibling_out_of_the_aggregate_and_resumes(session) -> None:
    """R13：回收站里的场不在这一章里。以前它的记忆算成「位置孤儿」，章末那一场的章汇总从此拼不出来；现在汇总照常
    重建、只是没有它，输入清单与汇总一致，续跑时复验通过。"""
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 2
    session.commit()
    _archived_sibling(session, "CH_RESUME_SC00", scene_seq=1, text="回收站里那一场的正文。")
    AuthorLifecycleService(session).trash_scenes(["CH_RESUME_SC00"], "author")
    session.commit()
    execution_id = "idempotency:chapter-last-trashed-sibling"

    product = _stop_after_sub9(session, execution_id)

    assert product["outcome"] == "aggregated"
    assert [item["scene_id"] for item in product["inputs"]] == ["CH_RESUME_SC01"]
    memory = session.get(ChapterMemory, product["chapter_memory"]["row_id"])
    final = session.get(FinalScene, session.get(SceneRunState, "CH_RESUME_SC01").current_final_scene_row_id)
    assert memory.content == final.content
    assert _resume(session, execution_id)["scene_status"] == "archived"


def test_chapter_last_resume_accepts_an_aggregate_whose_scene_order_differs_from_memory_row_ids(session) -> None:
    """章汇总按场序拼，输入清单按 row_id 排；两者不一致时（手加的场 id 带随机后缀、雪花的场 id 来自构思行）续跑复验
    也得认——以前复验按 row_id 序重拼再逐字比，章末那一场在第 8 步之后停下就再也续不上（RUN_CHECKPOINT_CORRUPT）。"""
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 2
    session.commit()
    # 场序在前、row_id 在后的一场
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=1, text="林昭先读了旧信。")
    execution_id = "idempotency:chapter-last-scene-order"

    product = _stop_after_sub9(session, execution_id)

    assert product["outcome"] == "aggregated"
    assert [item["scene_id"] for item in product["inputs"]] == ["CH_RESUME_SC01", "CH_RESUME_SC09"]
    memory = session.get(ChapterMemory, product["chapter_memory"]["row_id"])
    assert memory.content.startswith("林昭先读了旧信。\n")
    assert _resume(session, execution_id)["scene_status"] == "archived"


_CHAPTER_LAST_TEXT = "林昭拆开旧信，雨一直下，她把最后一页压回案卷底下才起身。"


def test_chapter_last_archives_when_its_whole_text_also_occurs_in_an_earlier_scene(session) -> None:
    """复核 A1-R1：章末那一场的全文也出现在前面某一场里（短短的收尾场、重复的正文）。按「在汇总里第一次出现的位置」
    排，它就排到了中间那一场前面，对的汇总反被判损坏——新跑的第 8 步自检就失败，续跑、重放也一样，这一场永远归档
    不了。这一章 row_id 序正是场序，按 row_id 序重拼的旧自检是认的。"""
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 3
    session.commit()
    first_text = f"林昭说：走吧。{_CHAPTER_LAST_TEXT}"
    _archived_sibling(session, "CH_RESUME_SC00A", scene_seq=1, text=first_text)
    _archived_sibling(session, "CH_RESUME_SC00B", scene_seq=2, text="第二场，她把旧信收进案卷。")
    execution_id = "idempotency:chapter-last-text-inside-an-earlier-scene"

    product = _stop_after_sub9(
        session, execution_id, generation_client=_FixedTextGenerationClient(_CHAPTER_LAST_TEXT)
    )

    assert product["outcome"] == "aggregated"
    memory = session.get(ChapterMemory, product["chapter_memory"]["row_id"])
    assert memory.content == f"{first_text}\n第二场，她把旧信收进案卷。\n{_CHAPTER_LAST_TEXT}"
    assert _resume(session, execution_id)["scene_status"] == "archived"
    # 已归档的重放（同一执行键再跑）走快路径，第 8 步照样复验
    assert _resume(session, execution_id)["scene_status"] == "archived"


@pytest.mark.parametrize("tail", ["门外有人敲了三下。", "\n门外有人敲了三下。"], ids=["same-line", "next-line"])
def test_chapter_last_archives_when_its_whole_text_opens_an_earlier_scene(session, tail: str) -> None:
    """复核 A1-R1：前一场正以章末那一场的全文开头（同一行接着写，或另起一行）。两段在汇总里第一次出现的位置都是
    开头，只能靠 row_id 分先后，章末那一场的 row_id 在前，对的汇总又被判损坏。"""
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 2
    session.commit()
    # 场序在前、row_id 在后的一场，以章末那一场的全文开头
    earlier_text = f"{_CHAPTER_LAST_TEXT}{tail}"
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=1, text=earlier_text)
    execution_id = "idempotency:chapter-last-text-opens-an-earlier-scene"

    product = _stop_after_sub9(
        session, execution_id, generation_client=_FixedTextGenerationClient(_CHAPTER_LAST_TEXT)
    )

    assert product["outcome"] == "aggregated"
    memory = session.get(ChapterMemory, product["chapter_memory"]["row_id"])
    assert memory.content == f"{earlier_text}\n{_CHAPTER_LAST_TEXT}"
    assert _resume(session, execution_id)["scene_status"] == "archived"
    assert _resume(session, execution_id)["scene_status"] == "archived"  # 已归档的重放


def test_chapter_last_resume_still_rejects_an_aggregate_that_is_not_exactly_its_inputs(session) -> None:
    """次序放宽之后自检照样逐字：汇总里多出清单之外的文字（行与检查点里的快照一起改、产品哈希也重算）就报损坏。"""
    from novel_system.services.hash_engine import sha256_json_plain

    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 2
    session.commit()
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=1, text="林昭先读了旧信。")
    execution_id = "idempotency:chapter-last-forged-aggregate"
    product = _stop_after_sub9(session, execution_id)

    memory = session.get(ChapterMemory, product["chapter_memory"]["row_id"])
    forged = f"{memory.content}\n案卷里没有的一段。"
    memory.content = forged
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_chapter_product"]["chapter_memory"]["content"] = forged
    payload["artifact_hashes"]["archive_chapter_product"] = sha256_json_plain(
        payload["artifact_refs"]["archive_chapter_product"]
    )
    state.run_checkpoint_json = payload
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _resume(session, execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert "chapter aggregate output changed" in str(corrupt.value)


def test_chapter_last_self_check_compares_the_scene_order_join_before_searching(session, monkeypatch) -> None:
    """复核 I4-R1：第 8 步自检先按现在的场序把清单里的记忆拼一次比——新做的产品、场序没改过的续跑都一次比完，用不着
    逐段对（逐段对有步数上限，最坏的输入走满就报损坏）。这里把逐段对的步数压成 0：清单按 row_id 排、与场序相反，只有
    先按场序比的自检过得去——新跑的第 8 步、续跑、已归档的重放都是。"""
    monkeypatch.setattr(
        scene_run_archive, "is_chapter_aggregate_of", partial(is_chapter_aggregate_of, max_steps=0)
    )
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 2
    session.commit()
    # 场序在前、row_id 在后的一场
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=1, text="林昭先读了旧信。")
    execution_id = "idempotency:chapter-last-scene-order-first"

    product = _stop_after_sub9(session, execution_id)

    assert product["outcome"] == "aggregated"
    assert [item["scene_id"] for item in product["inputs"]] == ["CH_RESUME_SC01", "CH_RESUME_SC09"]
    assert _resume(session, execution_id)["scene_status"] == "archived"
    assert _resume(session, execution_id)["scene_status"] == "archived"  # 已归档的重放


def test_chapter_last_resume_after_the_chapter_was_reordered_still_recognises_its_aggregate(session) -> None:
    """复核 I4-R1：第 8 步之后、续跑之前，作者对调了这一章前两场的先后。汇总是按原来的场序拼的，按现在的场序先比比不上，
    就逐段对——第二场正以第一场的全文开头，照样认得出，续跑照常归档。（章末那一场的前一场没变，起草时的上下文也没变。）"""
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.scene_seq = 4
    session.commit()
    _archived_sibling(session, "CH_RESUME_SC07", scene_seq=1, text="林昭先读了旧信。")
    _archived_sibling(session, "CH_RESUME_SC08", scene_seq=2, text="林昭先读了旧信。\n雨城的钟敲过三下。")
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=3, text="第三场，她把旧信收进案卷。")
    execution_id = "idempotency:chapter-last-reordered-before-resume"
    product = _stop_after_sub9(session, execution_id)
    memory = session.get(ChapterMemory, product["chapter_memory"]["row_id"])
    assert memory.content.startswith("林昭先读了旧信。\n林昭先读了旧信。\n雨城的钟敲过三下。\n第三场，")

    # 前两场对调（同一章里场序不能重号，先挪到空着的号上）
    first, second = session.get(SceneCard, "CH_RESUME_SC07"), session.get(SceneCard, "CH_RESUME_SC08")
    first.scene_seq = 90
    session.flush()
    second.scene_seq = 1
    session.flush()
    first.scene_seq = 2
    session.commit()

    assert _resume(session, execution_id)["scene_status"] == "archived"


# ------------------------------------------------------------------ 卷汇总不看第 8 步的结果（复核 I4-R2）


def test_chapter_last_volume_rolls_up_the_stored_aggregate_when_stage_8_cannot_rebuild_it(session) -> None:
    """流水线第 9 步不看第 8 步的结果：章末那一场到了卷边界，第 8 步拼不出章汇总（同一章里一条没有场景卡的有效记忆——
    位置对不上的旧行），第 9 步照样卷，卷进去的是这一章存着的那份旧汇总。晋升不一样：这一次章汇总没重建成就不卷
    （``volume_aggregate`` 记 ``skipped``，见 test_chapter_aggregate_derive_on_read.py）。这里钉住的是现状，写在
    services/archive_effects_plan.py 的说明里；两条路径要是对齐，改这里也改那份说明。"""
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.get(ChapterGoal, scene.chapter_id).display_order = VOLUME_CHAPTER_SPAN
    for ordinal in range(1, VOLUME_CHAPTER_SPAN):
        chapter_id = f"CH_RESUME_{ordinal}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id="P_RESUME",
                display_order=ordinal,
                chapter_goal=f"prior {ordinal}",
            )
        )
        session.add(
            ChapterMemory(
                row_id=f"chapter_memory_final_{chapter_id}_v1",
                chapter_id=chapter_id,
                aggregate_stage="final",
                content=f"prior atmosphere {ordinal}",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            )
        )
    stored = ChapterMemory(
        row_id="chapter_memory_final_CH_RESUME_v1",
        chapter_id="CH_RESUME",
        aggregate_stage="final",
        content="这一章存着的旧汇总。",
        active_flag=1,
        runtime_eligible=1,
        runtime_eligibility_basis="direct_read",
    )
    session.add(stored)
    session.add(
        SceneMemory(
            row_id="scene_memory_CH_RESUME_GHOST_v1",
            scene_id="CH_RESUME_GHOST",
            chapter_id="CH_RESUME",
            content="没有场景卡的一场。",
            source_bundle_id="bundle_ghost",
            final_scene_row_id="final_scene_CH_RESUME_GHOST_v1",
            active_flag=1,
        )
    )
    session.commit()
    execution_id = "idempotency:chapter-last-volume-after-blocked-aggregate"

    chapter_product = _stop_after_sub9(session, execution_id)

    assert chapter_product["outcome"] == "no_op"
    assert chapter_product["result"]["status"] == "blocked"
    assert chapter_product["result"]["reason"] == "scene_memory_position_orphan"
    refs = session.get(SceneRunState, scene.scene_id).run_checkpoint_json["artifact_refs"]
    volume_product = refs["archive_volume_product"]
    assert volume_product["outcome"] == "aggregated"
    summary = session.get(VolumeSummary, volume_product["volume_summary"]["row_id"])
    assert f"【第{VOLUME_CHAPTER_SPAN}章 氛围】这一章存着的旧汇总。" in summary.atmosphere_summary
    # 第 8 步什么也没写：存着的那份还是这一章唯一一份、仍然有效
    assert session.get(ChapterMemory, stored.row_id).active_flag == 1
    assert session.scalar(
        select(func.count()).select_from(ChapterMemory).where(ChapterMemory.chapter_id == "CH_RESUME")
    ) == 1
