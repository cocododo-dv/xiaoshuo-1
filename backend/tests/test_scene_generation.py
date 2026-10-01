"""场景生成整场：中性稿 → 风格稿 → 补丁的落库与成稿链接、起草步路由 / 提示词失败的记账、准终稿挡住缺必含的在线稿、
未开 Best-of-N 时只起一稿（X04-19：按 services/scene_generation/ 的子模块拆过；首稿、风格通道、确定性文本门、
长度带与分段补丁各有自己的测试文件）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    FinalScene,
    GenerationPlanningArtifact,
    LlmCall,
    SceneBlueprint,
    SceneBundle,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.errors import DomainError
from novel_system.services.context_budget import estimate_tokens
from novel_system.services.near_final import (
    CHAPTER_ARCHITECTURE_ARTIFACT,
    CHARACTER_PRESSURE_ARTIFACT,
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.prompt_builder import PromptConfigurationError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_generation import SceneGenerationService
from tests.real_llm_fakes import ScenePipelineOnlineFake
from tests.support.scene_generation import (
    STYLE_SCENE_TEXT,
    ThreeStepSceneClient as FakeSceneClient,
    seed_generation_scene as _seed_scene,
)


def _seed_scene_blueprint(session) -> None:
    session.add(
        SceneBlueprint(
            row_id="scene_blueprint_CH100_SC01_seed",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            source_bundle_id="seed_source_CH100_SC01",
            source_bundle_hash="seed_hash_CH100_SC01",
            blueprint_json={
                "character_current_desire": "CHAR_A wants the truth before CHAR_B can leave.",
                "concrete_obstacle": "CHAR_B controls the red envelope and refuses a straight answer.",
                "choice_under_pressure": "CHAR_A must choose whether to trust CHAR_B or expose the clue.",
                "information_release": "The envelope proves someone watched the reunion.",
                "power_shift": "CHAR_B begins with leverage; CHAR_A takes it back by naming the watcher.",
                "emotional_turn": "Suspicion hardens into reluctant alliance.",
                "irreversible_consequence": "Both characters know the secret is no longer private.",
                "ending_reader_question": "Who sent the red envelope?",
                "image_promise": "The red envelope returns with a changed meaning.",
            },
            status="accepted",
        )
    )
    session.commit()


def _seed_scene_planning(session) -> None:
    """预置章级架构 + 角色压力规划产物（status=active），让编排复用而非联网生成。

    这样 scene_blueprint（另由 _seed_scene_blueprint 预置）与规划两步都被跳过、
    不产生 LLM 调用，测试才能干净地落到 neutral_draft 的目标失败点。"""
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_chapter_arch_CH100_seed",
            artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
            object_type="chapter",
            object_id="CH100",
            chapter_id="CH100",
            payload_json={
                "chapter_promise": "the scene must change the available choices",
                "escalation_path": ["pressure appears", "a choice narrows", "a cost lands"],
                "reveal_plan": ["the governing constraint is exposed"],
                "payoff_target": "the chosen action creates the next problem",
                "character_shift": "certainty gives way to costly resolve",
                "ending_question": "what will the choice cost next",
            },
            status="active",
        )
    )
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_char_pressure_CH100_SC01_seed",
            artifact_type=CHARACTER_PRESSURE_ARTIFACT,
            object_type="scene",
            object_id="CH100_SC01",
            chapter_id="CH100",
            scene_id="CH100_SC01",
            payload_json={
                "surface_goal": "finish the immediate task",
                "hidden_fear": "the choice will expose a weakness",
                "wrong_belief": "control can prevent every loss",
                "shame_point": "asking for help feels like surrender",
                "avoidance_strategy": "delay the irreversible choice",
                "relationship_debt": "an old promise remains unpaid",
                "current_mask": "measured confidence",
            },
            status="active",
        )
    )
    session.commit()


def test_run_scene_persists_provider_neutral_draft_and_bundle_linkage(session) -> None:
    # This test isolates persistence/lineage. Continuity blocking for a missing
    # must-include fact is exercised explicitly by the offline test below.
    _seed_scene(session, must_include_text=None)
    fake_client = FakeSceneClient()
    support = ScenePipelineOnlineFake()

    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=fake_client),
        hard_qc_engine=HardQcEngine(session, llm_client=support),
        soft_qc_engine=SoftQcEngine(session, llm_client=support),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=support)

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    llm_calls = session.execute(select(LlmCall).order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())).scalars().all()
    llm_calls_by_step = {llm_call.step: llm_call for llm_call in llm_calls}
    neutral_llm_call = llm_calls_by_step["neutral_draft"]
    style_llm_call = llm_calls_by_step["style_draft"]
    bundle = session.execute(select(SceneBundle)).scalars().one()
    neutral_draft = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().one()
    style_draft = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "style_draft")
    ).scalars().one()
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")
    ).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")
    soft_qc = result["soft_qc"]

    assert len(fake_client.requests) == 2
    request = fake_client.requests[0]
    assert request.response_format == "json_object"
    assert request.response_schema == {
        "name": "neutral_draft",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scene_text"],
            "properties": {
                "scene_text": {"type": "string"},
                "continuity_notes": {"type": "array", "items": {"type": "string"}},
            },
        },
    }
    assert request.node_id == "neutral_draft"
    assert request.reasoning_level == "medium"
    assert any("Scene ID: CH100_SC01" in message["content"] for message in request.messages)
    assert any("Return JSON that matches the structured schema exactly." in message["content"] for message in request.messages)
    style_request = fake_client.requests[1]
    assert style_request.model == "gpt-5"
    assert style_request.node_id == "style_draft"
    assert style_request.response_schema == {
        "name": "style_draft",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scene_text"],
            "properties": {
                "scene_text": {"type": "string"},
                "style_notes": {"type": "array", "items": {"type": "string"}},
            },
        },
    }
    assert style_request.reasoning_level == "medium"
    assert any("Approved Neutral Draft" in message["content"] for message in style_request.messages)
    assert any("Provider-generated neutral scene text." in message["content"] for message in style_request.messages)
    assert any(
        "recompose it in the reference author's hand" in message["content"]
        for message in style_request.messages
    )
    assert any(
        "Keep every fact, causal step, ending function, must-include item, character identity, and POV" in message["content"]
        for message in style_request.messages
    )
    assert sum(message["content"].count("Return JSON that matches the structured schema exactly.") for message in style_request.messages) == 1

    assert neutral_draft.content == "Provider-generated neutral scene text."
    assert "Clocktower Roof" not in neutral_draft.content
    assert neutral_draft.generation_llm_call_id == neutral_llm_call.llm_call_id
    assert neutral_draft.source_bundle_id == bundle.bundle_id
    assert neutral_draft.source_bundle_hash == bundle.bundle_snapshot_hash
    assert style_draft.content == STYLE_SCENE_TEXT
    assert style_draft.generation_llm_call_id == style_llm_call.llm_call_id
    assert style_draft.source_bundle_id == bundle.bundle_id
    assert style_draft.source_bundle_hash == bundle.bundle_snapshot_hash

    assert {"neutral_draft", "hard_qc", "style_draft", "soft_qc"}.issubset(llm_calls_by_step)
    assert neutral_llm_call.provider == "fake-provider"
    assert neutral_llm_call.node_id == "neutral_draft"
    assert neutral_llm_call.reasoning_level == "medium"
    assert neutral_llm_call.model == "fake-neutral-model"
    assert neutral_llm_call.step == "neutral_draft"
    assert neutral_llm_call.scene_id == "CH100_SC01"
    assert neutral_llm_call.chapter_id == "CH100"
    assert neutral_llm_call.prompt_hash
    assert neutral_llm_call.prompt_tokens == 111
    assert neutral_llm_call.completion_tokens == 29
    assert neutral_llm_call.total_tokens == 140
    assert neutral_llm_call.finish_reason == "stop"
    assert neutral_llm_call.error_code is None
    assert neutral_llm_call.request_payload_summary["token_budget"]["estimated_input_tokens"] == sum(
        estimate_tokens(message["content"]) for message in request.messages
    )
    assert style_llm_call.provider == "fake-provider"
    assert style_llm_call.node_id == "style_draft"
    assert style_llm_call.reasoning_level == "medium"
    assert style_llm_call.model == "fake-style-model"
    assert style_llm_call.step == "style_draft"
    assert style_llm_call.scene_id == "CH100_SC01"
    assert style_llm_call.chapter_id == "CH100"
    assert style_llm_call.prompt_hash
    assert style_llm_call.prompt_tokens == 121
    assert style_llm_call.completion_tokens == 33
    assert style_llm_call.total_tokens == 154
    assert style_llm_call.finish_reason == "stop"
    assert style_llm_call.error_code is None
    assert style_llm_call.request_payload_summary["token_budget"]["estimated_input_tokens"] == sum(
        estimate_tokens(message["content"]) for message in style_request.messages
    )

    assert attempt.source_bundle_id == bundle.bundle_id
    assert attempt.details_json == {"row_id": neutral_draft.row_id, "llm_call_id": neutral_llm_call.llm_call_id}
    assert state.current_neutral_draft_row_id == neutral_draft.row_id
    assert state.current_bundle_id == bundle.bundle_id
    assert state.current_bundle_hash == bundle.bundle_snapshot_hash
    assert state.current_style_draft_row_id == style_draft.row_id
    assert state.total_attempt_count == 1
    assert final_scene.source_bundle_id == bundle.bundle_id
    assert final_scene.source_bundle_hash == bundle.bundle_snapshot_hash
    assert final_scene.content == style_draft.content
    assert final_scene.generation_llm_call_id == style_draft.generation_llm_call_id
    assert style_draft.content != neutral_draft.content

    assert result["current_bundle_id"] == bundle.bundle_id
    assert result["current_bundle_hash"] == bundle.bundle_snapshot_hash
    assert soft_qc["branch"] == "continue"


def test_run_scene_records_neutral_prompt_builder_failure_and_clears_stale_state(session, monkeypatch) -> None:
    _seed_scene(session)
    _seed_scene_blueprint(session)
    _seed_scene_planning(session)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_BASE_URL", raising=False)

    state = session.get(SceneRunState, "CH100_SC01")
    state.current_neutral_draft_row_id = "stale_neutral"
    state.current_qc_report_id = "stale_qc"
    state.soft_patch_count = 1
    session.commit()

    def failing_prompt_builder(self):
        raise PromptConfigurationError("prompts config missing")

    monkeypatch.setattr(SceneGenerationService, "_prompt_builder", failing_prompt_builder)

    orchestrator = Orchestrator(session)
    with pytest.raises(PromptConfigurationError):
        orchestrator.run_scene("CH100_SC01")
    session.commit()

    llm_call = session.execute(select(LlmCall)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")

    assert llm_call.step == "neutral_draft"
    assert llm_call.node_id == "neutral_draft"
    assert llm_call.error_code == "PromptConfigurationError"
    assert attempt.status == "failed"
    assert state.current_neutral_draft_row_id is None
    assert state.current_qc_report_id is None
    assert state.soft_patch_count == 0


def test_prompt_builder_failure_records_the_node_the_step_routes_to(session, monkeypatch) -> None:
    """B02-20：装配提示词就失败时，账本行记的是这一步本该派发到的节点——软补丁走 style_patch 路由，
    不是步名 soft_patch（成本看板按节点归类，步名在节点表里查不到）。"""
    _seed_scene(session)

    def failing_prompt_builder(self):
        raise PromptConfigurationError("prompts config missing")

    monkeypatch.setattr(SceneGenerationService, "_prompt_builder", failing_prompt_builder)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
    }
    service = SceneGenerationService(session, llm_client=FakeSceneClient())
    with pytest.raises(PromptConfigurationError):
        service.generate_style_patch(
            "CH100_SC01",
            bundle,
            source_style_draft_row_id="draft_style_CH100_SC01",
            source_style_content="旧稿。",
            rewrite_brief=["补一处动作"],
            source_qc_report_id="qc_report_CH100_SC01",
        )
    session.commit()

    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "soft_patch")).scalars().one()
    assert llm_call.node_id == "style_patch"
    assert llm_call.accounting_status == "rejected"


def test_run_scene_records_style_routing_failure(session, monkeypatch) -> None:
    _seed_scene(session, must_include_text=None)
    _seed_scene_blueprint(session)
    _seed_scene_planning(session)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_BASE_URL", raising=False)

    class FakeRoutingConfig:
        def __init__(self) -> None:
            self.task_routing = {
                "neutral_draft": type(
                    "TaskConfig",
                    (),
                    {
                        "provider": "offline_deterministic",
                        "model": "offline-neutral",
                        "temperature": 0.6,
                        "max_output_tokens": 6000,
                        "response_format": "json_object",
                    },
                )(),
                "hard_qc": type(
                    "TaskConfig",
                    (),
                    {
                        "provider": "offline_deterministic",
                        "model": "offline-hard-qc",
                        "temperature": 0.0,
                        "max_output_tokens": 4000,
                        "response_format": "json_object",
                    },
                )(),
            }

    monkeypatch.setattr(
        "novel_system.services.llm_service_base.load_model_routing_config",
        lambda: FakeRoutingConfig(),
    )

    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=support),
        hard_qc_engine=HardQcEngine(session, llm_client=support),
    )
    with pytest.raises(KeyError):
        orchestrator.run_scene("CH100_SC01")
    session.commit()

    llm_calls = session.execute(
        select(LlmCall).order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
    ).scalars().all()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "style_draft")).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")

    assert [llm_call.step for llm_call in llm_calls] == [
        "neutral_draft",
        "hard_qc",
        "style_draft",
    ]
    # 缺路由统一为引导性错误码(原为裸 "KeyError");原始 KeyError 仍向上抛(见 raises)
    assert llm_calls[-1].error_code == "LLM_ROUTE_NOT_CONFIGURED"
    assert attempt.status == "failed"
    assert attempt.details_json["llm_call_id"] == llm_calls[-1].llm_call_id
    assert attempt.details_json["error_code"] == "LLM_ROUTE_NOT_CONFIGURED"
    assert state.current_style_draft_row_id is None


def test_online_draft_cannot_advance_when_neutral_repair_still_misses_required_fact(session) -> None:
    # 中性稿是事实骨架。首次生成和唯一一次修复都缺失必含事实时，必须在进入
    # hard-QC/style 阶段前失败关闭，不能把已知不合格的原稿伪装成 active draft。
    _seed_scene(session)
    support = ScenePipelineOnlineFake()

    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=support),
        hard_qc_engine=HardQcEngine(session, llm_client=support),
        soft_qc_engine=SoftQcEngine(session, llm_client=support),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=support)
    with pytest.raises(DomainError) as blocked:
        orchestrator.run_scene("CH100_SC01")
    assert blocked.value.code == "NEUTRAL_DRAFT_REPAIR_INVALID"
    session.commit()

    llm_calls = session.execute(
        select(LlmCall).order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
    ).scalars().all()
    generation_steps = [
        llm_call.step
        for llm_call in llm_calls
        if llm_call.step in {"neutral_draft", "neutral_draft_repair", "hard_qc", "style_draft"}
    ]
    assert generation_steps == ["neutral_draft", "neutral_draft_repair"]
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().all() == []
    # orchestrator 对失败场景回滚业务草稿；LLM 记账仍独立保留。
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_rejected")
    ).scalars().all() == []
    assert session.execute(select(FinalScene)).scalars().all() == []
    assert all(llm_call.provider == "test-online-provider" for llm_call in llm_calls)
    assert all(llm_call.finish_reason == "stop" for llm_call in llm_calls)


def test_best_of_n_without_style_first_drafts_one_candidate(session) -> None:
    """2026-09-30 [批准#2]：先中性后润色的多稿整套删掉——没绑作者手笔直起的作品，开关打开、要 3 份也只起一稿，
    与编排器的单稿路径同一个结果：同一个步位与续跑基稿、不按温度展开、不补候选、不写分散度。"""
    _seed_scene(session, must_include_text=None)
    service = SceneGenerationService(session, llm_client=FakeSceneClient())
    bundle = BundleBuilder(session).build("CH100_SC01")
    neutral = service.generate_neutral_draft("CH100_SC01", bundle)
    reconciled: list[str] = []

    candidates = service.generate_style_draft_candidates(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id=neutral.row_id,
        neutral_content=neutral.content,
        n_candidates=3,
        step_reconciler=reconciled.append,
    )
    session.commit()

    assert len(candidates) == 1
    assert reconciled[:1] == ["style_draft:0"]
    style_rows = session.execute(
        select(SceneDraft.row_id).where(SceneDraft.scene_id == "CH100_SC01", SceneDraft.stage == "style_draft")
    ).scalars().all()
    assert [row_id for row_id in style_rows if "_cand_" in row_id] == []
    attempts = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == "CH100_SC01", AttemptTracker.step == "style_draft")
    ).scalars().all()
    assert len(attempts) == 1
    assert "candidate_index" not in (attempts[0].details_json or {})
    state = session.get(SceneRunState, "CH100_SC01")
    assert state.current_style_draft_row_id == candidates[0].row_id


def test_adversarial_rank_score_lower_for_ai_heavy_text() -> None:
    from novel_system.services.literary_quality import adversarial_rank_score

    clean_text = (
        "She opened the door. He must choose the archive or save the child. "
        "The cost was his position. He left."
    )
    ai_heavy_text = (
        "She suddenly realized the moon was somehow meaningful. "
        "Everything changed forever. As if fate."
    )
    assert adversarial_rank_score(clean_text) > adversarial_rank_score(ai_heavy_text)
