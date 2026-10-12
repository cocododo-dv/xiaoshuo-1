"""Scene-run checkpoint resume · 归档尾段（``near_final_ready`` 子游标 4、5，检查点格式 4）与章末那一场的汇总。

第 4 步 ``archive_effects`` 是一个事务：终稿归档（成稿门照这一份正文判）+ 读数 + 章末那一场的章汇总 / 卷汇总，与检查点
一起提交——断在提交之前什么也没留下、续跑整步重做；提交之后续跑直接去第 5 步（可选的正文抽取）。
"""

from __future__ import annotations

import json
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
    SceneCard,
    SceneMemory,
    SceneRunState,
    VolumeSummary,
    WriterEvaluation,
)
from novel_system.services.errors import DomainError
from novel_system.services.aggregator import VOLUME_CHAPTER_SPAN, Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run_checkpoint import CHECKPOINT_FORMAT

from tests.support.checkpoint_fakes import (
    _CountingGenerationClient,
    _FailArchiveOnce,
    _HardPassClient,
    _response,
    _seed_resume_scene,
)

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")

SCENE_ID = "CH_RESUME_SC01"


def _orchestrator(session, generation_client: _CountingGenerationClient | None = None) -> Orchestrator:
    return Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session, llm_client=generation_client or _CountingGenerationClient()
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    )


def _count(session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where))


def _archive_rows(session) -> dict[str, int]:
    """第 4 步写的行：终稿归档的三行与章末那一场的章汇总。"""
    return {
        "scene_memory": _count(session, SceneMemory, SceneMemory.scene_id == SCENE_ID),
        "rolling_note": _count(session, ChapterRollingNote, ChapterRollingNote.scene_id == SCENE_ID),
        "archive_attempt": _count(
            session, AttemptTracker, AttemptTracker.scene_id == SCENE_ID, AttemptTracker.step == "archive"
        ),
        "chapter_memory": _count(session, ChapterMemory, ChapterMemory.chapter_id == "CH_RESUME"),
    }


def _make_chapter_last(session) -> SceneCard:
    scene = session.get(SceneCard, SCENE_ID)
    scene.is_chapter_last = 1
    session.commit()
    return scene


def _raise(message: str):
    def raiser(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError(message)

    return raiser


def _refs(session) -> dict:
    return session.get(SceneRunState, SCENE_ID).run_checkpoint_json["artifact_refs"]


# ------------------------------------------------------------------------- 第 4 步一个事务：断在提交之前 / 之后


@pytest.mark.parametrize("chapter_last", [False, True], ids=["mid-chapter", "chapter-last"])
def test_crash_before_the_archive_effects_commit_leaves_nothing_and_resume_redoes_it(session, chapter_last) -> None:
    """归档的三行、章末那一场的章汇总都已写进事务、检查点还没提交时断掉：什么也没留下，检查点停在准终稿第 3 步、终稿
    还没归档；续跑把第 4 步整个重做一遍（每样只有一份），不再调模型。"""
    _seed_resume_scene(session)
    if chapter_last:
        _make_chapter_last(session)
    execution_id = f"idempotency:archive-effects-crash-before-commit-{int(chapter_last)}"
    generation_client = _CountingGenerationClient()
    first = _orchestrator(session, generation_client)
    original_save = first._save_run_checkpoint
    in_transaction: dict[str, int] = {}

    def crash_before_sub4_commit(node_key, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if node_key == "near_final_ready" and kwargs.get("sub_index") == 4:
            session.flush()
            in_transaction.update(_archive_rows(session))
            raise RuntimeError("crash before the archive effects commit")
        return original_save(node_key, **kwargs)

    first._save_run_checkpoint = crash_before_sub4_commit
    with pytest.raises(RuntimeError, match="crash before the archive effects commit"):
        first.run_scene(SCENE_ID, execution_id=execution_id)
    expected = {
        "scene_memory": 1,
        "rolling_note": 1,
        "archive_attempt": 1,
        "chapter_memory": 1 if chapter_last else 0,
    }
    assert in_transaction == expected, "断之前，第 4 步的写都已经在同一个事务里"

    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert (state.run_checkpoint, state.run_checkpoint_json["sub_index"]) == ("near_final_ready", 3)
    assert state.run_execution_status == "failed"
    assert state.scene_status != "archived"
    assert _archive_rows(session) == dict.fromkeys(expected, 0)
    assert session.get(FinalScene, state.current_final_scene_row_id).status != "archived"
    provider_calls = len(generation_client.requests)

    result = _orchestrator(session, generation_client).run_scene(SCENE_ID, execution_id=execution_id)

    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == provider_calls
    assert _archive_rows(session) == expected
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert (state.run_checkpoint, state.run_execution_status) == ("archived", "completed")


@pytest.mark.parametrize("chapter_last", [False, True], ids=["mid-chapter", "chapter-last"])
def test_crash_after_the_archive_effects_commit_resumes_at_prose_extraction(session, chapter_last) -> None:
    """第 4 步提交之后、正文抽取那一步断掉：归档的三行与章汇总各一份留着，检查点停在第 4 步；续跑不再归档（归档器一调就
    失败）、不再汇总，直接做第 5 步，然后归档完成，第 4 步的产品原样不动。"""
    _seed_resume_scene(session)
    if chapter_last:
        _make_chapter_last(session)
    execution_id = f"idempotency:archive-effects-crash-after-commit-{int(chapter_last)}"
    generation_client = _CountingGenerationClient()
    first = _orchestrator(session, generation_client)
    first._record_prose_events = _raise("crash in prose extraction")
    with pytest.raises(RuntimeError, match="crash in prose extraction"):
        first.run_scene(SCENE_ID, execution_id=execution_id)

    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert (state.run_checkpoint, state.run_checkpoint_json["sub_index"]) == ("near_final_ready", 4)
    assert state.scene_status != "archived"
    expected = {
        "scene_memory": 1,
        "rolling_note": 1,
        "archive_attempt": 1,
        "chapter_memory": 1 if chapter_last else 0,
    }
    assert _archive_rows(session) == expected
    assert session.get(FinalScene, state.current_final_scene_row_id).status == "archived"
    effects_hash = state.run_checkpoint_json["artifact_hashes"]["archive_effects"]
    provider_calls = len(generation_client.requests)

    resumed = _orchestrator(session, generation_client)
    resumed.archiver = _FailArchiveOnce()
    result = resumed.run_scene(SCENE_ID, execution_id=execution_id)

    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == provider_calls
    assert _archive_rows(session) == expected
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint_json["artifact_hashes"]["archive_effects"] == effects_hash
    assert state.run_checkpoint_json["artifact_refs"]["archive_prose_product"]["outcome"] == "not_invoked"


def test_the_archive_tail_writes_one_effects_product_and_a_two_entry_manifest(session) -> None:
    """检查点格式 4：归档尾段只有两份产品——第 4 步 archive_effects（行 id、四份行快照的哈希、三项收尾的结果；快照本身与
    整场正文不进检查点）与第 5 步的正文抽取；归档清单两条。成稿门判的就是归档的这一份正文。以前八个子步的产品、章级准终稿
    评审的结果一个也不写了。"""
    _seed_resume_scene(session)
    result = _orchestrator(session).run_scene(SCENE_ID, execution_id="idempotency:archive-effects-shape")

    assert result["scene_status"] == "archived"
    assert "chapter_near_final" not in result
    payload = session.get(SceneRunState, SCENE_ID).run_checkpoint_json
    refs, hashes = payload["artifact_refs"], payload["artifact_hashes"]
    assert payload["checkpoint_format"] == CHECKPOINT_FORMAT == 4
    effects = refs["archive_effects"]
    assert (effects["schema_version"], effects["kind"], effects["outcome"], effects["step_key"]) == (
        3,
        "archive_effects",
        "completed",
        "archive:effects:0",
    )
    assert effects["chapter_aggregate"] == {"outcome": "not_applicable", "reason": "not_chapter_last"}
    assert effects["volume_aggregate"] == {"outcome": "not_applicable", "reason": "not_chapter_last"}
    assert effects["fidelity_reading"] == {"outcome": "not_applicable", "reason": "unbound"}
    assert set(effects["snapshot_hashes"]) == {"final_scene", "scene_memory", "rolling_note", "archive_attempt"}
    final = session.get(FinalScene, effects["final_scene_row_id"])
    assert final.content and final.content not in json.dumps(effects, ensure_ascii=False)
    attempt = session.get(AttemptTracker, effects["archive_attempt_id"])
    assert attempt.details_json["final_text_gate"]["content_hash"] == final.content_hash
    assert attempt.details_json["execution_id"] == "idempotency:archive-effects-shape"
    assert refs["archive_prose_product"]["outcome"] == "not_invoked"
    assert [(entry["sub_index"], entry["kind"], entry["hash_key"]) for entry in refs["archive_manifest"]] == [
        (4, "archive_effects", "archive_effects"),
        (5, "prose_extraction", "archive_prose_product"),
    ]
    retired = {
        "archive_core",
        "archive_rule_product",
        "archive_rule_events",
        "archive_rule_event_ids",
        "archive_vector_product",
        "archive_chapter_product",
        "archive_volume_product",
        "archive_chapter_evaluation_product",
        "archive_drift_product",
        "archive_final_scene_snapshot",
        "archive_scene_memory_snapshot",
        "archive_rolling_note_snapshot",
        "archive_attempt_snapshot",
        "scene_memory_row_id",
    }
    assert not retired & (set(refs) | set(hashes))
    # 准终稿评审的修订候选同理：只记哈希，不存带整份来源稿的快照
    assert not {"near_eval0_candidate_snapshot", "near_eval1_candidate_snapshot"} & set(refs)
    assert {"near_eval0_candidate", "near_eval1_candidate"} & set(hashes)


# ------------------------------------------------------------------------- 续跑 / 重放时的复验


@pytest.mark.parametrize(
    "tamper",
    ["outcome", "chapter_aggregate", "snapshot_hash", "row_id"],
)
def test_tampered_archive_effects_product_blocks_resume(session, tamper) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-effects-tamper-{tamper}"
    first = _orchestrator(session)
    first._record_prose_events = _raise("stop after the archive effects")
    with pytest.raises(RuntimeError, match="stop after the archive effects"):
        first.run_scene(SCENE_ID, execution_id=execution_id)
    state = session.get(SceneRunState, SCENE_ID)
    payload = deepcopy(state.run_checkpoint_json)
    effects = payload["artifact_refs"]["archive_effects"]
    if tamper == "outcome":
        effects["outcome"] = "tampered"
    elif tamper == "chapter_aggregate":
        effects["chapter_aggregate"] = {"outcome": "tampered"}
    elif tamper == "snapshot_hash":
        effects["snapshot_hashes"]["scene_memory"] = "tampered"
    else:
        effects["scene_memory_row_id"] = "scene_memory_missing"
    state.run_checkpoint_json = payload
    session.commit()

    resumed = _orchestrator(session)
    resumed._record_prose_events = lambda *_args, **_kwargs: pytest.fail("a tampered archive prefix must block first")
    with pytest.raises(DomainError) as corrupt:
        resumed.run_scene(SCENE_ID, execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_prose_checkpoint_is_durable_and_tamper_blocks_before_the_manifest(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-tamper"
    first = _orchestrator(session)
    first._archive_manifest = _raise("stop after prose checkpoint")
    with pytest.raises(RuntimeError, match="stop after prose checkpoint"):
        first.run_scene(SCENE_ID, execution_id=execution_id)

    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint_json["sub_index"] == 5
    assert state.run_checkpoint_json["artifact_refs"]["archive_prose_product"]["outcome"] == "not_invoked"
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_prose_product"]["reason"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    resumed = _orchestrator(session)
    resumed._archive_manifest = lambda: pytest.fail("a tampered prose prefix must block before the manifest")
    with pytest.raises(DomainError) as corrupt:
        resumed.run_scene(SCENE_ID, execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_prose_no_call_with_released_tombstone_never_advances_checkpoint(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-released-gate-closed"
    first = _orchestrator(session)
    first._record_prose_events = _raise("stop at the archive effects")
    with pytest.raises(RuntimeError, match="stop at the archive effects"):
        first.run_scene(SCENE_ID, execution_id=execution_id)
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint_json["sub_index"] == 4

    session.add(
        LlmCall(
            llm_call_id="llm_archive_prose_released",
            provider="fake",
            model="fake",
            node_id="extraction",
            step="archive:prose_event_extract:0",
            project_id="P_RESUME",
            scene_id=SCENE_ID,
            chapter_id="CH_RESUME",
            scope_type="scene",
            scope_id=SCENE_ID,
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
        _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 4
    assert _count(
        session,
        LlmCall,
        LlmCall.execution_id == execution_id,
        LlmCall.execution_step_key == "archive:prose_event_extract:0",
    ) == 1


def _add_settled_prose_parent(session, *, call_id: str, execution_id: str) -> None:
    step_key = "archive:prose_event_extract:0"
    session.add(
        LlmCall(
            llm_call_id=call_id,
            provider="fake",
            model="fake",
            node_id="extraction",
            step=step_key,
            project_id="P_RESUME",
            scene_id=SCENE_ID,
            chapter_id="CH_RESUME",
            scope_type="scene",
            scope_id=SCENE_ID,
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
    first = _orchestrator(session)
    first._record_prose_events = _raise("stop before prose parent")
    with pytest.raises(RuntimeError, match="stop before prose parent"):
        first.run_scene(SCENE_ID, execution_id=execution_id)
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint_json["sub_index"] == 4
    call_id = "llm_archive_prose_parent_only"
    _add_settled_prose_parent(session, call_id=call_id, execution_id=execution_id)
    tokens_before = state.scene_tokens_used

    with pytest.raises(DomainError) as missing:
        _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent = session.get(LlmCall, call_id)
    assert (parent.total_tokens, parent.reserved_tokens, parent.budget_charged_tokens) == (12, 12, 12)
    assert _count(session, LlmCallAttempt, LlmCallAttempt.llm_call_id == call_id) == 1
    assert _count(
        session,
        LlmCall,
        LlmCall.execution_id == execution_id,
        LlmCall.execution_step_key == "archive:prose_event_extract:0",
    ) == 1
    session.refresh(state)
    assert state.scene_tokens_used == tokens_before


def test_archived_fast_path_revalidates_full_manifest_before_return(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-manifest-tamper"
    _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)
    state = session.get(SceneRunState, SCENE_ID)
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_manifest"][0]["product_hash"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


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
def test_archive_snapshot_field_tamper_blocks_archived_fast_path(session, mutation) -> None:
    """终稿归档的四行按库里的行重算快照、与第 4 步产品里的哈希比：改了任何一行都判损坏。"""
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-core-field-{mutation}"
    _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)
    effects = _refs(session)["archive_effects"]
    if mutation == "final_source_bundle_hash":
        session.get(FinalScene, effects["final_scene_row_id"]).source_bundle_hash = "tampered"
    elif mutation == "memory_runtime_basis":
        session.get(SceneMemory, effects["scene_memory_row_id"]).runtime_eligibility_basis = "tampered"
    elif mutation == "rolling_revision":
        session.get(ChapterRollingNote, effects["chapter_rolling_note_row_id"]).revision_no += 1
    else:
        attempt = session.get(AttemptTracker, effects["archive_attempt_id"])
        details = dict(attempt.details_json or {})
        if mutation == "attempt_qc_delete":
            details.pop("qc_report_id", None)
        else:
            details["qc_report_id"] = "tampered"
        attempt.details_json = details
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archived_worker_fast_path_restores_real_run_job_owner_and_rejects_wrong_job(session) -> None:
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
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

    assert _orchestrator(session).run_scene(
        scene.scene_id, execution_id=execution_id, run_job_id=job_id
    )["scene_status"] == "archived"
    assert _orchestrator(session).run_scene(
        scene.scene_id, execution_id=execution_id, run_job_id=job_id
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
        _orchestrator(session).run_scene(scene.scene_id, execution_id=execution_id, run_job_id=wrong_job_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


# ------------------------------------------------------------------------- 章末那一场：章汇总 / 卷汇总（行为）


def test_chapter_last_archive_rebuilds_the_chapter_aggregate_without_any_chapter_scope_call(session) -> None:
    """章末那一场：第 4 步重建章汇总（aggregated + 行 id）；章级准终稿评审删了，整次运行没有章级的模型调用、没有章级评审行。
    之后别的路径又重建了章汇总，已归档的重放照样通过（章汇总是派生缓存，产品只记当时的结果，不回读它）。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    execution_id = "idempotency:archive-chapter-last"

    result = _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)

    assert result["scene_status"] == "archived"
    effects = _refs(session)["archive_effects"]
    chapter = effects["chapter_aggregate"]
    assert (chapter["outcome"], chapter["status"]) == ("aggregated", "created")
    memory = session.get(ChapterMemory, chapter["chapter_memory_row_id"])
    assert memory.content == session.get(FinalScene, effects["final_scene_row_id"]).content
    assert memory.active_flag == 1
    # 这一章没排进书（display_order 为空）：到不了卷边界
    assert effects["volume_aggregate"] == {"outcome": "no_op", "status": "no_op", "reason": "chapter_unmapped"}
    assert _count(session, LlmCall, LlmCall.scope_type == "chapter") == 0
    assert _count(session, LlmCall, LlmCall.node_id == "chapter_near_final_review") == 0
    assert _count(session, WriterEvaluation, WriterEvaluation.object_type == "chapter") == 0

    Aggregator(session).run_final_aggregate(scene.chapter_id)
    session.commit()
    assert session.get(ChapterMemory, chapter["chapter_memory_row_id"]).active_flag == 0
    assert _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)["scene_status"] == "archived"


def _seed_prior_chapters(session, count: int) -> None:
    """这一章前面排着的 ``count`` 章，各有一份存着的章汇总；这一章排在它们后面。"""
    session.get(ChapterGoal, "CH_RESUME").display_order = count + 1
    for ordinal in range(1, count + 1):
        chapter_id = f"CH_RESUME_{ordinal}"
        session.add(
            ChapterGoal(chapter_id=chapter_id, project_id="P_RESUME", display_order=ordinal, chapter_goal=f"prior {ordinal}")
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


def test_chapter_last_volume_boundary_resume_reuses_the_same_summary(session) -> None:
    """卷边界上的章末那一场：第 4 步卷出卷汇总；之后断在正文抽取，续跑不再卷第二份。别的路径后来重卷、取代了它，
    已归档的重放照样通过（卷汇总同样是派生缓存）。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    _seed_prior_chapters(session, VOLUME_CHAPTER_SPAN - 1)
    execution_id = "idempotency:chapter-last-volume-boundary"
    first = _orchestrator(session)
    first._record_prose_events = _raise("stop after the archive effects")
    with pytest.raises(RuntimeError, match="stop after the archive effects"):
        first.run_scene(SCENE_ID, execution_id=execution_id)

    volume = _refs(session)["archive_effects"]["volume_aggregate"]
    assert (volume["outcome"], volume["status"]) == ("aggregated", "created")
    row_id = volume["volume_summary_row_id"]
    assert _count(session, VolumeSummary) == 1

    assert _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)["scene_status"] == "archived"
    assert _count(session, VolumeSummary) == 1
    assert session.get(VolumeSummary, row_id).active_flag == 1
    window = [f"CH_RESUME_{ordinal}" for ordinal in range(1, VOLUME_CHAPTER_SPAN)] + [scene.chapter_id]
    Aggregator(session).aggregate_volume_summary("P_RESUME", 1, window)
    session.commit()
    assert session.get(VolumeSummary, row_id).active_flag == 0
    assert _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)["scene_status"] == "archived"


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


def _stop_after_archive_effects(
    session, execution_id: str, *, generation_client: _CountingGenerationClient | None = None
) -> dict:
    """章末那一场跑完第 4 步、断在正文抽取；返回检查点里第 4 步记下的章汇总结果。"""
    first = _orchestrator(session, generation_client)
    first._record_prose_events = _raise("stop after the archive effects")
    with pytest.raises(RuntimeError, match="stop after the archive effects"):
        first.run_scene(SCENE_ID, execution_id=execution_id)
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint_json["sub_index"] == 4
    return state.run_checkpoint_json["artifact_refs"]["archive_effects"]["chapter_aggregate"]


def _resume(session, execution_id: str) -> dict:
    return _orchestrator(session).run_scene(SCENE_ID, execution_id=execution_id)


def _chapter_memory(session, chapter_aggregate: dict) -> ChapterMemory:
    assert chapter_aggregate["outcome"] == "aggregated"
    return session.get(ChapterMemory, chapter_aggregate["chapter_memory_row_id"])


def test_chapter_last_archive_leaves_a_trashed_sibling_out_of_the_aggregate_and_resumes(session) -> None:
    """R13：回收站里的场不在这一章里。章汇总照常重建、只是没有它；续跑、重放照常归档。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    scene.scene_seq = 2
    session.commit()
    _archived_sibling(session, "CH_RESUME_SC00", scene_seq=1, text="回收站里那一场的正文。")
    AuthorLifecycleService(session).trash_scenes(["CH_RESUME_SC00"], "author")
    session.commit()
    execution_id = "idempotency:chapter-last-trashed-sibling"

    memory = _chapter_memory(session, _stop_after_archive_effects(session, execution_id))

    final = session.get(FinalScene, session.get(SceneRunState, SCENE_ID).current_final_scene_row_id)
    assert memory.content == final.content
    assert _resume(session, execution_id)["scene_status"] == "archived"
    assert _resume(session, execution_id)["scene_status"] == "archived"  # 已归档的重放


def test_chapter_last_aggregate_follows_the_scene_order_not_the_memory_row_ids(session) -> None:
    """章汇总按场序拼：场序在前、row_id 在后的一场排在前面（手加的场 id 带随机后缀、雪花的场 id 来自构思行）。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    scene.scene_seq = 2
    session.commit()
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=1, text="林昭先读了旧信。")
    execution_id = "idempotency:chapter-last-scene-order"

    memory = _chapter_memory(session, _stop_after_archive_effects(session, execution_id))

    final = session.get(FinalScene, session.get(SceneRunState, SCENE_ID).current_final_scene_row_id)
    assert memory.content == f"林昭先读了旧信。\n{final.content}"
    assert _resume(session, execution_id)["scene_status"] == "archived"


_CHAPTER_LAST_TEXT = "林昭拆开旧信，雨一直下，她把最后一页压回案卷底下才起身。"


def test_chapter_last_archives_when_its_whole_text_also_occurs_in_an_earlier_scene(session) -> None:
    """章末那一场的全文也出现在前面某一场里（短短的收尾场、重复的正文）：章汇总照场序拼，续跑、重放照常归档。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    scene.scene_seq = 3
    session.commit()
    first_text = f"林昭说：走吧。{_CHAPTER_LAST_TEXT}"
    _archived_sibling(session, "CH_RESUME_SC00A", scene_seq=1, text=first_text)
    _archived_sibling(session, "CH_RESUME_SC00B", scene_seq=2, text="第二场，她把旧信收进案卷。")
    execution_id = "idempotency:chapter-last-text-inside-an-earlier-scene"

    memory = _chapter_memory(
        session,
        _stop_after_archive_effects(
            session, execution_id, generation_client=_FixedTextGenerationClient(_CHAPTER_LAST_TEXT)
        ),
    )

    assert memory.content == f"{first_text}\n第二场，她把旧信收进案卷。\n{_CHAPTER_LAST_TEXT}"
    assert _resume(session, execution_id)["scene_status"] == "archived"
    assert _resume(session, execution_id)["scene_status"] == "archived"  # 已归档的重放


@pytest.mark.parametrize("tail", ["门外有人敲了三下。", "\n门外有人敲了三下。"], ids=["same-line", "next-line"])
def test_chapter_last_archives_when_its_whole_text_opens_an_earlier_scene(session, tail: str) -> None:
    """前一场正以章末那一场的全文开头（同一行接着写，或另起一行）：章汇总照场序拼，续跑、重放照常归档。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    scene.scene_seq = 2
    session.commit()
    earlier_text = f"{_CHAPTER_LAST_TEXT}{tail}"
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=1, text=earlier_text)
    execution_id = "idempotency:chapter-last-text-opens-an-earlier-scene"

    memory = _chapter_memory(
        session,
        _stop_after_archive_effects(
            session, execution_id, generation_client=_FixedTextGenerationClient(_CHAPTER_LAST_TEXT)
        ),
    )

    assert memory.content == f"{earlier_text}\n{_CHAPTER_LAST_TEXT}"
    assert _resume(session, execution_id)["scene_status"] == "archived"
    assert _resume(session, execution_id)["scene_status"] == "archived"  # 已归档的重放


def test_chapter_last_resume_after_the_chapter_was_reordered_still_archives(session) -> None:
    """第 4 步之后、续跑之前，作者对调了这一章前两场的先后：第 4 步早已提交，续跑直接做正文抽取、照常归档。"""
    _seed_resume_scene(session)
    scene = _make_chapter_last(session)
    scene.scene_seq = 4
    session.commit()
    _archived_sibling(session, "CH_RESUME_SC07", scene_seq=1, text="林昭先读了旧信。")
    _archived_sibling(session, "CH_RESUME_SC08", scene_seq=2, text="林昭先读了旧信。\n雨城的钟敲过三下。")
    _archived_sibling(session, "CH_RESUME_SC09", scene_seq=3, text="第三场，她把旧信收进案卷。")
    execution_id = "idempotency:chapter-last-reordered-before-resume"
    memory = _chapter_memory(session, _stop_after_archive_effects(session, execution_id))
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


# ------------------------------------------------------------------ 卷汇总不看章汇总这一次的结果（复核 I4-R2）


def test_chapter_last_volume_rolls_up_the_stored_aggregate_when_the_chapter_cannot_rebuild_it(session) -> None:
    """流水线的卷汇总不看章汇总这一次重建成没成：章末那一场到了卷边界，章汇总拼不出来（同一章里一条没有场景卡的有效
    记忆——位置对不上的旧行），卷汇总照样卷，卷进去的是这一章存着的那份旧汇总。晋升不一样：这一次章汇总没重建成就
    不卷（``volume_aggregate`` 记 ``skipped``，见 test_chapter_aggregate_derive_on_read.py）。这里钉住的是现状，写在
    services/archive_effects_plan.py 的说明里；两条路径要是对齐，改这里也改那份说明。"""
    _seed_resume_scene(session)
    _make_chapter_last(session)
    _seed_prior_chapters(session, VOLUME_CHAPTER_SPAN - 1)
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

    chapter = _stop_after_archive_effects(session, execution_id)

    assert (chapter["outcome"], chapter["status"], chapter["reason"]) == (
        "no_op",
        "blocked",
        "scene_memory_position_orphan",
    )
    volume = _refs(session)["archive_effects"]["volume_aggregate"]
    assert volume["outcome"] == "aggregated"
    summary = session.get(VolumeSummary, volume["volume_summary_row_id"])
    assert f"【第{VOLUME_CHAPTER_SPAN}章 氛围】这一章存着的旧汇总。" in summary.atmosphere_summary
    # 章汇总什么也没写：存着的那份还是这一章唯一一份、仍然有效
    assert session.get(ChapterMemory, stored.row_id).active_flag == 1
    assert _count(session, ChapterMemory, ChapterMemory.chapter_id == "CH_RESUME") == 1
    assert _resume(session, execution_id)["scene_status"] == "archived"
