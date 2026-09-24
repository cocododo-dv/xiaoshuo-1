from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from novel_system.db.models import (
    ChapterGoal,
    SceneBundle,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.style_policy import policy_from_contract, style_policy_live
from novel_system.services.style_reference.inject.render import render_style, reset_render_cache
from novel_system.services.style_reference.inject.request import StyleRenderRequest
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION,
    blend_profile_metric_baselines,
    build_style_runtime_contract,
    contract_profile_objects,
    extract_style_generation_context,
    resolve_style_runtime_contract_state,
    style_runtime_contract_from_bundle,
    style_runtime_contract_status_from_bundle,
    validate_style_runtime_contract,
)


def _card_with_line(text: str) -> dict:
    return {
        "version": "dimension_card_v1",
        "temperament": [],
        "dimensions": [
            {
                "dimension": "language.sentence_structure",
                "distinctiveness": 0.9,
                "devices": [],
                "lines": [{"text": text, "mandatory": True, "distinctiveness": 0.9}],
            }
        ],
    }


def _seed_reference(
    session,
    *,
    seed: str,
    config_json: dict | None = None,
    task_type: str = "scene_generation",
    feature: str = "句式舒展，收束克制",
):
    repo = StyleReferenceRepository(session)
    book_id = f"contract_book_{seed}"
    run_id = f"contract_run_{seed}"
    profile_id = f"contract_profile_{seed}"
    binding_id = f"contract_binding_{seed}_{task_type}"
    quote_id = f"contract_quote_{seed}"
    quote_text = "雨在旧檐边停了一瞬，灯影便向里缩了缩。"
    repo.create_book(
        book_id=book_id,
        title="匿名参考",
        source_kind="upload",
        cloud_policy="segments_only",
        text_checksum=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        total_chars=10_000,
        status="ready",
        stats_json={
            "rights_declaration": {
                "declared": True,
                "send_rights": True,
            }
        },
    )
    repo.create_run(
        run_id=run_id,
        book_id=book_id,
        status="completed",
        phase="completed",
    )
    repo.create_quote(
        quote_id=quote_id,
        book_id=book_id,
        paragraph_id=None,
        span_start=0,
        span_end=len(quote_text),
        quote_text=quote_text,
        illustrates_dims=["language.rhythm"],
        extracted_features={},
    )
    repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=run_id,
        title="匿名风格",
        status="active",
        profile_json={
            "profile_version": "style_profile_v3",
            "narrative_summary": "克制观察，动作先于解释。",
            "qualitative_summary": "克制观察，动作先于解释。",
            # 2026-09-24 起没有旧画像的卡替身：要进提示的句子在文风卡上
            "dimension_card": _card_with_line(feature),
            "style_features": [feature],
            "banned_replication_rules": ["不要复刻专名与独特意象"],
            "scene_samples_index": {"narration": [quote_id]},
            "metrics_baseline": {
                "avg_sentence_length": {"mean": 18.0, "std": 2.0},
                "short_sentence_ratio": {"mean": 0.2, "std": 0.05},
            },
            # Simulate a future profile field that contains source prose.  The
            # runtime-contract allow-list must keep it out of SceneBundle.
            "future_raw_excerpt": "这段未来原文字段绝不能进入运行契约。",
        },
        coverage_json={},
        source_finding_ids_json=[],
        version_tag="v1",
    )
    repo.create_banned_term(
        term_id=f"contract_term_{seed}",
        profile_id=profile_id,
        term="不可复用的专名",
        replacement_hint=None,
        source="test",
        scope="generation",
    )
    binding = repo.create_binding(
        binding_id=binding_id,
        profile_id=profile_id,
        scope="project",
        scope_ref_id=f"contract_project_{seed}",
        task_type=task_type,
        strategy="mixed",
        config_json=dict(config_json or {}),
        status="active",
    )
    session.flush()
    return SimpleNamespace(
        repo=repo,
        book_id=book_id,
        profile_id=profile_id,
        binding_id=binding_id,
        quote_id=quote_id,
        quote_text=quote_text,
        project_id=f"contract_project_{seed}",
        binding=binding,
    )


def test_contract_is_hashed_tamper_evident_and_contains_no_raw_quote(session) -> None:
    seeded = _seed_reference(session, seed="hash")
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )

    assert contract is not None
    assert contract["contract_version"] == STYLE_RUNTIME_CONTRACT_VERSION
    assert validate_style_runtime_contract(contract) == contract
    serialized = json.dumps(contract, ensure_ascii=False)
    assert seeded.quote_text not in serialized
    assert "这段未来原文字段绝不能进入运行契约" not in serialized
    assert contract["layers"][0]["profile"]["profile_json"][
        "qualitative_summary"
    ] == "克制观察，动作先于解释。"
    # v2（J6 / E9）：不再冻结样例引文 / 段落引用（它们只服务已删的兜底路径）
    assert "sample_quote_refs" not in contract["layers"][0]
    assert "sample_paragraph_refs" not in contract["layers"][0]
    assert contract["schema_version"] == 2 and contract["layer_count"] == 1
    assert "scene_samples_index" not in contract["layers"][0]["profile"]["profile_json"]

    tampered = copy.deepcopy(contract)
    tampered["layers"][0]["profile"]["profile_json"]["style_features"] = [
        "篡改后的风格"
    ]
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_style_runtime_contract(tampered)


def test_frozen_contract_render_does_not_follow_later_profile_or_binding_edits(
    session,
) -> None:
    reset_render_cache()
    seeded = _seed_reference(session, seed="frozen", config_json={"reference_mode": "card_only"})
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    frozen_policy = policy_from_contract(contract, mode="frozen")
    request = StyleRenderRequest(scene_id="contract_scene_frozen")
    frozen_before = render_style(session, frozen_policy, request, use_cache=False).system_prefix

    profile = seeded.repo.get_profile(seeded.profile_id)
    profile.profile_json = {
        **dict(profile.profile_json or {}),
        "dimension_card": _card_with_line("后来被修改的实时风格"),
    }
    seeded.binding.config_json = {"reference_mode": "full", "sample_windows": 3}
    session.flush()

    frozen_after = render_style(session, frozen_policy, request, use_cache=False)
    live_policy = style_policy_live(
        session, SimpleNamespace(project_id=seeded.project_id, scene_id=None, pov_character_id=None, onstage_chars_json=[])
    )
    live_after = render_style(session, live_policy, request, use_cache=False).system_prefix

    assert frozen_after.system_prefix == frozen_before
    assert "句式舒展，收束克制" in frozen_after.system_prefix
    assert "后来被修改的实时风格" not in frozen_after.system_prefix
    assert "后来被修改的实时风格" in live_after
    assert live_policy.contract_hash != contract["contract_hash"]
    assert frozen_after.audit["contract_hash"] == contract["contract_hash"]
    # 冻结的是 v3 规范化配置：只用文风卡，不送窗口；后来改了绑定（3 窗）也不影响冻结的那份
    # （书是 segments_only：现解析的参考方式仍被云策略压成 card_only，但窗数已经是改过的 3）
    assert frozen_policy.reference_mode == "card_only" and frozen_after.stats["few_shot_windows"] == 0
    assert frozen_policy.sample_windows == 12 and live_policy.sample_windows == 3


def test_frozen_contract_samples_require_current_send_rights(session) -> None:
    """样例窗口在渲染时再查一次发送权：冻结后作者撤回了发送权 → 一窗都不送（卡与红线照送）。"""
    reset_render_cache()
    seeded = _seed_reference(session, seed="rights")
    book = seeded.repo.get_book(seeded.book_id)
    book.cloud_policy = "allow_full_cloud"
    for index in range(80):
        text = f"第{index}段：他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯。"
        seeded.repo.create_paragraph(
            paragraph_id=f"contract_rights_p{index:03d}",
            book_id=seeded.book_id,
            paragraph_index=index,
            paragraph_type="narration",
            start_offset=0,
            end_offset=len(text),
            text=text,
            char_count=len(text),
            classifier_confidence=0.9,
        )
    session.flush()
    contract = build_style_runtime_contract(seeded.repo, [seeded.binding], task_type="scene_generation")
    policy = policy_from_contract(contract, mode="frozen")
    request = StyleRenderRequest(scene_id="contract_scene_rights")
    original = render_style(session, policy, request, use_cache=False)
    assert original.stats["few_shot_windows"] >= 1 and "他把湿伞靠在墙角" in original.system_prefix

    book.stats_json = {"rights_declaration": {"declared": True, "send_rights": False}}
    session.flush()
    revoked = render_style(session, policy, request, use_cache=False)
    assert revoked.stats["few_shot_windows"] == 0 and "他把湿伞靠在墙角" not in revoked.system_prefix
    assert revoked.audit["samples_blocked"] == "cloud_policy_now"


def test_styled_gate_uses_the_live_banned_terms_not_the_frozen_contract(
    session,
) -> None:
    """风格参考 v3（H1）：风格稿门的禁用词只认画像**现行**的表——绑定之后改了表，冻结了旧词的契约也按新表判；
    冻结契约里的禁用词只用来渲染提示词的红线（契约照旧冻结它们，本用例也核对这一点）。

    （旧用例的方向相反：冻结的词是本 bundle 的裁决口径。那会让作者删掉一个误收的词之后，所有冻结过它的场景
    照样被拦；v3 的成稿门、抄袭门、软 QC 都改读现行的表。）
    """
    from novel_system.services.qc_engine import _styled_gate_report

    seeded = _seed_reference(session, seed="validation_inputs", config_json={"reference_mode": "card_only"})
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    frozen_terms = [term for layer in contract["layers"] for term in layer.get("banned_terms") or []]
    assert "不可复用的专名" in frozen_terms, "契约照旧冻结禁用词（给红线渲染用）"
    policy = policy_from_contract(contract, mode="frozen")

    term = seeded.repo.list_banned_terms(
        seeded.profile_id,
        scope="generation",
    )[0]
    term.term = "后来才加入的实时禁用词"
    session.flush()
    report = _styled_gate_report(session, policy, "她说出不可复用的专名，又说了后来才加入的实时禁用词。")

    assert [hit["pattern_statement"] for hit in report.forbidden_hits_json] == ["后来才加入的实时禁用词"]
    assert report.verdict == "fail" and report.quantitative_json == []
    # 现解析（没有冻结契约）同样按画像现行的词
    live = SimpleNamespace(contract=None, profile_id=seeded.profile_id, bound=True, book_id=seeded.book_id)
    live_report = _styled_gate_report(session, live, "她说出不可复用的专名，又说了后来才加入的实时禁用词。")
    assert [hit["pattern_statement"] for hit in live_report.forbidden_hits_json] == ["后来才加入的实时禁用词"]


def test_task_specific_bundle_contract_and_scene_injection_context_are_auditable(
    session,
) -> None:
    seeded = _seed_reference(session, seed="tasks", config_json={"reference_mode": "card_only"})
    long_binding = seeded.repo.create_binding(
        binding_id="contract_binding_tasks_long",
        profile_id=seeded.profile_id,
        scope="project",
        scope_ref_id=seeded.project_id,
        task_type="long_form_continuation",
        strategy="mixed",
        config_json={"reference_mode": "card_only"},
        status="active",
    )
    session.flush()
    scene_contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    long_contract = build_style_runtime_contract(
        seeded.repo,
        [long_binding],
        task_type="long_form_continuation",
    )
    assert scene_contract is not None and long_contract is not None
    bundle = {
        "snapshot": {
            "inline_digests": {
                "_style_reference_runtime_contract": json.dumps(scene_contract),
                "_style_reference_runtime_contract_long_form_continuation": json.dumps(
                    long_contract
                ),
            }
        }
    }
    assert (
        style_runtime_contract_from_bundle(bundle)["contract_hash"]
        == scene_contract["contract_hash"]
    )
    assert (
        style_runtime_contract_from_bundle(
            bundle,
            task_type="long_form_continuation",
        )["contract_hash"]
        == long_contract["contract_hash"]
    )

    session.add_all(
        [
            StoryProject(
                project_id=seeded.project_id,
                title="冻结契约项目",
                outline_text="",
            ),
            ChapterGoal(
                chapter_id="contract_chapter_tasks",
                project_id=seeded.project_id,
                chapter_goal="她必须确认门外是谁。",
            ),
            SceneCard(
                scene_id="contract_scene_tasks",
                project_id=seeded.project_id,
                chapter_id="contract_chapter_tasks",
                scene_seq=1,
                scene_goal="确认门外来客的身份。",
                onstage_chars_json=[],
            ),
            SceneRunState(scene_id="contract_scene_tasks"),
        ]
    )
    session.flush()
    neutral = "她先听见两下敲门声，随后把手按在没有点亮的灯罩上。"
    injected = SceneGenerationService(session)._inject_style_reference(
        {"system_prompt": "基础系统提示。"},
        session.get(SceneCard, "contract_scene_tasks"),
        task_type="scene_generation",
        bundle=bundle,
        context_text=neutral,
    )
    audit = injected["_style_reference_runtime_audit"]
    assert audit["contract_hash"] == scene_contract["contract_hash"]
    # v3：选窗与渲染都不看被润色的稿子（J2 / J4），审计里也没有它
    assert "context" not in audit
    assert neutral not in json.dumps(audit, ensure_ascii=False)


def test_qc_gate_validates_the_frozen_contract_profiles(session, monkeypatch) -> None:
    seeded = _seed_reference(session, seed="qc", config_json={"reference_mode": "card_only"})
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    project = StoryProject(
        project_id=seeded.project_id,
        title="质检契约项目",
        outline_text="",
    )
    chapter = ChapterGoal(
        chapter_id="contract_chapter_qc",
        project_id=seeded.project_id,
        chapter_goal="验证同源质检。",
    )
    scene = SceneCard(
        scene_id="contract_scene_qc",
        project_id=seeded.project_id,
        chapter_id=chapter.chapter_id,
        scene_seq=1,
        scene_goal="验证冻结画像。",
        onstage_chars_json=[],
    )
    session.add_all(
        [
            project,
            chapter,
            scene,
            SceneRunState(
                scene_id=scene.scene_id,
                current_bundle_id="contract_bundle_qc",
                current_bundle_hash="qc-hash",
            ),
            SceneBundle(
                bundle_id="contract_bundle_qc",
                scene_id=scene.scene_id,
                chapter_id=chapter.chapter_id,
                execution_mode="P2",
                bundle_snapshot_hash="qc-hash",
                frozen_snapshot_json={
                    "inline_digests": {
                        "_style_reference_runtime_contract": json.dumps(contract)
                    }
                },
            ),
        ]
    )
    session.flush()
    profile = seeded.repo.get_profile(seeded.profile_id)
    profile.profile_json = {
        **dict(profile.profile_json or {}),
        "dimension_card": _card_with_line("不应被本次质检读取的实时修改"),
    }
    session.flush()

    captured: dict[str, object] = {}

    def fake_copy_check(current_session, text, *, policy=None, book_ids=None, extra_policies=()):
        captured["text"] = text
        captured["policy"] = policy
        captured["session"] = current_session
        return SimpleNamespace(hits=(), protected_hits=(), blocked=False)

    # 风格参考 v3：中性步位的门走唯一抄袭门，策略来自场景当前 bundle 冻结的契约
    monkeypatch.setattr(
        "novel_system.services.reference_copy_gate.check_reference_copy",
        fake_copy_check,
    )

    verdict = HardQcEngine(session)._apply_style_validation_gate(
        scene,
        "待质检正文。",
    )

    assert verdict == "pass"
    assert captured["session"] is session
    policy = captured["policy"]
    assert policy.bound and policy.contract_hash == contract["contract_hash"]
    frozen_profile = policy.contract["layers"][-1]["profile"]
    frozen_lines = frozen_profile["profile_json"]["dimension_card"]["dimensions"][0]["lines"]
    assert [line["text"] for line in frozen_lines] == ["句式舒展，收束克制"]
    assert "style_features" not in frozen_profile["profile_json"]


def test_layered_baseline_blends_mean_and_total_variance(
    session,
) -> None:
    base = SimpleNamespace(
        profile_id="base",
        book_id="",
        profile_json={
            "metrics_baseline": {"avg_sentence_length": {"mean": 10.0, "std": 1.0}}
        },
    )
    specific = SimpleNamespace(
        profile_id="specific",
        book_id="",
        profile_json={
            "metrics_baseline": {"avg_sentence_length": {"mean": 20.0, "std": 2.0}}
        },
    )

    blended = blend_profile_metric_baselines([base, specific])
    assert blended["avg_sentence_length"]["mean"] == pytest.approx(50.0 / 3.0)
    assert blended["avg_sentence_length"]["std"] > 2.0
    # 2026-09-23 风格参考 v3（P5b）：旧的量化回测（以混合基线为对照目标）随校验层删除，只保留混合本身。


def test_context_extractor_is_bounded_normalized_and_audit_contains_only_hash() -> None:
    context = extract_style_generation_context(
        "前文\r\n\x00后文" + "甲" * 30,
        source_kind="continuation_tail",
        max_chars=12,
    )

    assert context.query_text == "甲" * 12
    assert context.char_count == 12
    assert set(context.audit_dict()) == {
        "version",
        "source_kind",
        "query_sha256",
        "char_count",
    }
    assert context.query_text not in json.dumps(
        context.audit_dict(), ensure_ascii=False
    )


def test_contract_aware_bundle_never_falls_back_to_a_later_live_binding(
    session,
) -> None:
    seeded = _seed_reference(session, seed="no_fallback", config_json={"reference_mode": "card_only"})
    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )
    assert contract is not None
    scene = SceneCard(
        scene_id="contract_scene_no_fallback",
        project_id=seeded.project_id,
        chapter_id="contract_chapter_no_fallback",
        scene_seq=1,
        scene_goal="验证冻结空状态。",
        onstage_chars_json=[],
    )
    base = {"system_prompt": "BASE", "user_prompt": "USER"}
    absent_bundle = {
        "snapshot": {
            "source_version_refs": {
                "style_reference_runtime_contract_version": STYLE_RUNTIME_CONTRACT_VERSION,
                "style_reference_runtime_contract_status": "absent",
            },
            "inline_digests": {},
        }
    }
    degraded_bundle = copy.deepcopy(absent_bundle)
    degraded_bundle["snapshot"]["source_version_refs"][
        "style_reference_runtime_contract_status"
    ] = "degraded"
    conflicting_bundle = copy.deepcopy(absent_bundle)
    conflicting_bundle["snapshot"]["inline_digests"][
        "_style_reference_runtime_contract"
    ] = contract

    absent = SceneGenerationService(session)._inject_style_reference(
        base,
        scene,
        bundle=absent_bundle,
        context_text="中性草稿。",
    )
    degraded = SceneGenerationService(session)._inject_style_reference(
        base,
        scene,
        bundle=degraded_bundle,
        context_text="中性草稿。",
    )
    conflicting = SceneGenerationService(session)._inject_style_reference(
        base,
        scene,
        bundle=conflicting_bundle,
        context_text="中性草稿。",
    )

    assert style_runtime_contract_status_from_bundle(absent_bundle) == "absent"
    assert resolve_style_runtime_contract_state(absent_bundle).mode == "absent"
    assert absent is base
    assert "句式舒展，收束克制" not in absent["system_prompt"]
    assert degraded["system_prompt"] == "BASE"
    assert degraded["_style_reference_runtime_audit"]["outcome"] == "degraded"
    assert "句式舒展，收束克制" not in degraded["system_prompt"]
    assert conflicting["system_prompt"] == "BASE"
    assert conflicting["_style_reference_runtime_audit"]["error_code"] == (
        "runtime_contract_status_conflict"
    )


def test_contract_freezes_voice_signature_and_narrative_guidance_but_not_raw_fields(
    session,
) -> None:
    """2026-09 v2 · W1:新键进冻结契约;非 allow-list 键(含原文)仍被拒之门外。"""
    seeded = _seed_reference(session, seed="v2_keys", config_json={"reference_mode": "card_only"})
    profile = seeded.repo.get_profile(seeded.profile_id)
    voice_signature = {
        "version": "voice_signature_v1",
        "features": {"comma_per_sentence": 1.5},
        "habits": ["常用连接词:却、便、又;少用:然而、于是"],
        "deliberate_repetition": False,
    }
    profile.profile_json = {
        **dict(profile.profile_json or {}),
        "voice_signature": voice_signature,
        "narrative_guidance": ["关键信息放段首一次给出,之后不回头解释"],
        # 审计字段不属于运行时契约,不该被冻结进每个 SceneBundle。
        "anchor_quotes_used": 12,
        "synthesis_input_budget": {"degradation_stage": "full"},
    }
    session.flush()

    contract = build_style_runtime_contract(
        seeded.repo,
        [seeded.binding],
        task_type="scene_generation",
    )

    assert contract is not None
    frozen = contract["layers"][0]["profile"]["profile_json"]
    assert frozen["voice_signature"] == voice_signature
    assert frozen["narrative_guidance"] == ["关键信息放段首一次给出,之后不回头解释"]
    assert "anchor_quotes_used" not in frozen
    assert "synthesis_input_budget" not in frozen
    assert "future_raw_excerpt" not in frozen
    assert validate_style_runtime_contract(contract) == contract

    # 旧画像(没有新键)照旧可冻结、可校验——优雅退化。
    legacy = _seed_reference(session, seed="v2_legacy", config_json={"reference_mode": "card_only"})
    legacy_contract = build_style_runtime_contract(
        legacy.repo,
        [legacy.binding],
        task_type="scene_generation",
    )
    assert legacy_contract is not None
    legacy_profile = legacy_contract["layers"][0]["profile"]["profile_json"]
    assert "voice_signature" not in legacy_profile
    assert "narrative_guidance" not in legacy_profile
    assert validate_style_runtime_contract(legacy_contract) == legacy_contract
