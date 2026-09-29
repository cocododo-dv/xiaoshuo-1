"""Scene-run checkpoint resume · archive sub-checkpoints and chapter-last aggregation."""

from __future__ import annotations

from copy import deepcopy

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
from novel_system.services.aggregator import Aggregator
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService

# Importing the autouse fixture runs every test here against the accounted online fake provider.
from tests.support.checkpoint_fakes import _accounted_online_default_orchestrator_runner  # noqa: F401
from tests.support.checkpoint_fakes import _CountingGenerationClient, _HardPassClient, _seed_resume_scene


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
    first._index_scene_to_vector_store = lambda *_args, **_kwargs: (_ for _ in ()).throw(
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
    resumed._index_scene_to_vector_store = lambda *_args, **_kwargs: pytest.fail(
        "tampered prose prefix must block before vector indexing"
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


def test_archive_vector_external_write_is_reused_after_cursor_crash(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-vector-crash"
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    original = first._index_scene_to_vector_store

    def _write_then_crash(scene, content, **kwargs):  # noqa: ANN001, ANN003, ANN202
        result = original(scene, content, **kwargs)
        assert result["outcome"] in {"indexed", "already_present", "non_persistent"}
        raise RuntimeError("crash after vector write")

    first._index_scene_to_vector_store = _write_then_crash
    with pytest.raises(RuntimeError, match="crash after vector write"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 6
    assert state.scene_status != "archived"

    resumed = orchestrator()
    resumed._run_archive_chapter_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after vector cursor")
    )
    with pytest.raises(RuntimeError, match="stop after vector cursor"):
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 7
    vector_product = state.run_checkpoint_json["artifact_refs"]["archive_vector_product"]
    assert vector_product["outcome"] == "non_persistent"
    assert vector_product["write_status"] == "already_present"


@pytest.mark.parametrize("external_change", ["cleared", "rebuilt_by_other_scene"])
def test_memory_vector_product_is_non_persistent_and_fast_path_ignores_external_reset(
    session, external_change
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:memory-vector-{external_change}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    assert orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)["scene_status"] == "archived"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    product = state.run_checkpoint_json["artifact_refs"]["archive_vector_product"]
    assert product["backend"] == "memory"
    assert product["outcome"] == "non_persistent"
    from novel_system.services.vector_store import get_vector_store

    store = get_vector_store(backend="memory")
    if external_change == "cleared":
        store.delete_collection(product["collection_name"])
    else:
        store.write_collection(
            product["collection_name"],
            [{"id": "another-scene", "text": "rebuilt elsewhere"}],
        )

    assert orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)["scene_status"] == "archived"


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


def test_archive_core_checkpoint_contains_full_independently_hashed_snapshots(session) -> None:
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
    assert {
        "archive_final_scene_snapshot",
        "archive_scene_memory_snapshot",
        "archive_rolling_note_snapshot",
        "archive_attempt_snapshot",
    }.issubset(refs)
    assert {
        "archive_final_scene_snapshot",
        "archive_scene_memory_snapshot",
        "archive_rolling_note_snapshot",
        "archive_attempt_snapshot",
    }.issubset(hashes)
    assert refs["archive_scene_memory_snapshot"]["runtime_eligibility_basis"] == "direct_read"
    assert refs["archive_rolling_note_snapshot"]["revision_no"] == 1
    assert "qc_report_id" in refs["archive_attempt_snapshot"]["details_json"]


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
