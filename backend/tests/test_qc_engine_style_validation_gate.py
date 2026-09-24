"""PR-8 §6.6 / 风格模仿 v2（W5，规格 §2.W5.5）— style validation gates。

- ``HardQcEngine._apply_style_validation_gate``（中性稿）：只保留确定性 n-gram 抄袭（Q0）
  裁决——返 ``None`` / ``"pass"`` / ``"plagiarism"``；冻结禁用词 / 量化容差不再对中性稿产生
  fail / partial。
- ``run_styled_draft_style_gate``（风格稿）：plagiarism + 冻结 banned_terms，quant 只记诊断；
  每次裁决写 ``styled_draft_gate_decided`` MetricEvent。
- ``SoftQcEngine.evaluate``：注入与 style_draft 相同的 ``[STYLE_REFERENCE]`` 前缀；styled-draft
  gate 抄袭命中 → Q0 阻断并升级人工复核（不允许软风险接受）；禁用词命中 → 要求人工复核。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    HumanReviewEvent,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    StyleReferenceMetricEvent,
)
from novel_system.db.session import SessionLocal
from novel_system.services.llm_task_runner import begin_llm_execution, end_llm_execution
from novel_system.services.qc_engine import (
    STYLE_BANNED_TERM_ISSUE_KEY,
    STYLE_GATE_UNAVAILABLE_ISSUE_KEY,
    STYLE_PLAGIARISM_ISSUE_KEY,
    STYLE_VALIDATION_PLAGIARISM_TRIGGER,
    STYLED_DRAFT_GATE_EVENT_KIND,
    STYLED_DRAFT_GATE_STAGES,
    STYLED_GATE_UNAVAILABLE_VERDICT,
    HardQcEngine,
    SoftQcEngine,
    run_styled_draft_style_gate,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

# 参考书原文段（抄袭语料）：连续 ≥12 字重叠即命中。
REFERENCE_PARAGRAPH = "月光落在青石板上，像一层薄薄的盐，他踩过去时鞋底发出细碎的声响。"
COPIED_SENTENCE = "月光落在青石板上，像一层薄薄的盐"
CLEAN_TEXT = "门外的脚步停住了，他把信封放到桌上，等对面的人先开口。"


def _seed_style_binding(
    *,
    project_id: str | None,
    seed: str,
    scope: str = "project",
    scope_ref_id: str | None = None,
    profile_status: str = "active",
    profile_json: dict | None = None,
    forbidden_terms: list[str] | None = None,
    paragraphs: list[str] | None = None,
) -> str:
    """落 book + run + profile + binding，可选 banned_term / 原文段。返回 profile_id。"""
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    profile_id = f"sr_profile_{seed}"
    binding_id = f"sr_bind_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="segments_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready",
            stats_json={"rights_declaration": {
                "declared": True, "analysis_rights": True, "send_rights": True,
            }},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=profile_id, book_id=book_id, run_id=run_id, title="t",
            status=profile_status,
            profile_json=profile_json or {"narrative_summary": "n", "style_features": ["短句克制"]},
            coverage_json={}, source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=binding_id, profile_id=profile_id,
            scope=scope, scope_ref_id=scope_ref_id if scope_ref_id is not None else project_id,
            # 只用文风卡（这些用例原来写的旧策略 A 的语义；2026-09-24 起 strategy 列恒 mixed、参考方式看 config）
            task_type="scene_generation", strategy="mixed",
            config_json={"reference_mode": "card_only"}, status="active",
        )
        for i, term in enumerate(forbidden_terms or []):
            repo.create_banned_term(
                term_id=f"sr_term_{seed}_{i}",
                profile_id=profile_id, scope="generation",
                term=term, source="manual",
            )
        offset = 0
        for i, text in enumerate(paragraphs or []):
            repo.create_paragraph(
                paragraph_id=f"sr_par_{seed}_{i}",
                book_id=book_id,
                paragraph_index=i,
                paragraph_type="narration",
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
                char_count=len(text),
            )
            offset += len(text)
        session.commit()
    return profile_id


def _make_scene(project_id: str | None, *, scene_id: str | None = None) -> SceneCard:
    return SceneCard(
        scene_id=scene_id or f"CH800_SC{project_id or 'X'}",
        chapter_id="CH800",
        project_id=project_id,
        scene_seq=1,
        pov_character_id="A",
        onstage_chars_json=["A"],
        location="x",
        scene_goal="g",
        beats_json=["b"],
        must_include_text="m",
        target_length_band="short",
        scene_type="t",
        is_chapter_last=0,
    )


# ---------------------------------------------------------------------------
# 中性稿 gate：只认抄袭
# ---------------------------------------------------------------------------


def test_gate_returns_none_when_no_project_id(session) -> None:
    engine = HardQcEngine(session, llm_client=object())
    verdict = engine._apply_style_validation_gate(_make_scene(None), "一段文本")
    assert verdict is None


def test_gate_returns_none_when_no_active_binding(session) -> None:
    engine = HardQcEngine(session, llm_client=object())
    scene = _make_scene("project_no_binding")
    verdict = engine._apply_style_validation_gate(scene, "一段文本")
    assert verdict is None


def test_gate_returns_pass_when_validation_clean(session) -> None:
    _seed_style_binding(project_id="proj_pass", seed="pass", paragraphs=[REFERENCE_PARAGRAPH])
    engine = HardQcEngine(session, llm_client=object())
    scene = _make_scene("proj_pass")
    verdict = engine._apply_style_validation_gate(scene, "一段普通文本,完全合规。")
    assert verdict == "pass"


def test_neutral_gate_ignores_frozen_banned_terms(session) -> None:
    """v2：中性稿没有注入任何参考风格，冻结禁用词对它无意义——不再返 fail。"""
    _seed_style_binding(
        project_id="proj_fail",
        seed="fail",
        forbidden_terms=["美轮美奂"],
    )
    engine = HardQcEngine(session, llm_client=object())
    scene = _make_scene("proj_fail")
    verdict = engine._apply_style_validation_gate(scene, "这景色真是美轮美奂极了。")
    assert verdict == "pass"


def test_neutral_gate_keeps_deterministic_plagiarism_verdict(session) -> None:
    _seed_style_binding(project_id="proj_plag", seed="plag", paragraphs=[REFERENCE_PARAGRAPH])
    engine = HardQcEngine(session, llm_client=object())
    scene = _make_scene("proj_plag")
    verdict = engine._apply_style_validation_gate(scene, f"他想起那夜：{COPIED_SENTENCE}。")
    assert verdict == "plagiarism"


def test_gate_swallows_exception_and_returns_none(session) -> None:
    _seed_style_binding(project_id="proj_explode", seed="explode")
    engine = HardQcEngine(session, llm_client=object())
    scene = _make_scene("proj_explode")
    with patch(
        "novel_system.services.reference_copy_gate.check_reference_copy",
        side_effect=RuntimeError("boom"),
    ):
        verdict = engine._apply_style_validation_gate(scene, "一段文本")
    # 异常吞掉,gate 返回 None(qc 直通,不阻塞)
    assert verdict is None


def test_gate_character_scope_binding_triggers_verdict(session) -> None:
    """PR-14 — scene.pov_character_id 命中 character binding，其原文语料触发 plagiarism。"""
    _seed_style_binding(
        project_id=None, seed="charg", scope="character", scope_ref_id="A",
        paragraphs=[REFERENCE_PARAGRAPH],
    )
    engine = HardQcEngine(session, llm_client=object())
    scene = _make_scene("proj_no_project_binding")
    verdict = engine._apply_style_validation_gate(scene, f"{COPIED_SENTENCE}。")
    assert verdict == "plagiarism"


def test_gate_scene_scope_binding_triggers_verdict(session) -> None:
    """PR-15 — scene.scene_id 命中 scene binding。"""
    scene = _make_scene("proj_no")
    _seed_style_binding(
        project_id=None, seed="sceneg", scope="scene", scope_ref_id=scene.scene_id,
        paragraphs=[REFERENCE_PARAGRAPH],
    )
    engine = HardQcEngine(session, llm_client=object())
    verdict = engine._apply_style_validation_gate(scene, f"{COPIED_SENTENCE}。")
    assert verdict == "plagiarism"


def test_gate_onstage_nonpov_character_triggers_verdict(session) -> None:
    """PR-18 — pov 无 binding，onstage 配角 character binding 生效。"""
    scene = _make_scene("proj_no")
    scene.pov_character_id = "POV_NO_BIND"
    scene.onstage_chars_json = ["POV_NO_BIND", "B"]
    _seed_style_binding(
        project_id=None, seed="onstageg", scope="character", scope_ref_id="B",
        paragraphs=[REFERENCE_PARAGRAPH],
    )
    engine = HardQcEngine(session, llm_client=object())
    verdict = engine._apply_style_validation_gate(scene, f"{COPIED_SENTENCE}。")
    assert verdict == "plagiarism"


# ---------------------------------------------------------------------------
# styled-draft gate（模块级函数）
# ---------------------------------------------------------------------------


def _metric_events(session, kind: str) -> list[StyleReferenceMetricEvent]:
    return list(
        session.execute(
            select(StyleReferenceMetricEvent).where(
                StyleReferenceMetricEvent.event_kind == kind
            )
        ).scalars().all()
    )


def test_styled_gate_returns_none_without_binding_or_text(session) -> None:
    scene = _make_scene("proj_styled_none")
    assert run_styled_draft_style_gate(session, scene, "风格稿正文。") is None
    assert run_styled_draft_style_gate(session, _make_scene(None), "风格稿正文。") is None
    _seed_style_binding(project_id="proj_styled_empty", seed="styled_empty")
    assert run_styled_draft_style_gate(session, _make_scene("proj_styled_empty"), "   ") is None
    assert _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND) == []


def test_styled_gate_rejects_unknown_stage(session) -> None:
    with pytest.raises(ValueError):
        # 2026-09-12 风格直起:neutral_draft 已是合法阶段(style_first 首稿过门);hard_qc 仍非法
        run_styled_draft_style_gate(session, _make_scene("p"), "x", stage="hard_qc")


def test_styled_gate_clean_text_passes_and_records_event(session) -> None:
    _seed_style_binding(project_id="proj_styled_pass", seed="styled_pass", paragraphs=[REFERENCE_PARAGRAPH])
    scene = _make_scene("proj_styled_pass")
    gate = run_styled_draft_style_gate(session, scene, CLEAN_TEXT, stage="style_draft")
    assert gate is not None
    assert gate["stage"] == "style_draft"
    assert gate["verdict"] == "pass"
    assert gate["plagiarism_passed"] is True
    assert gate["plagiarism_hits"] == [] and gate["forbidden_hits"] == []
    assert gate["profile_id"] == "sr_profile_styled_pass"
    # 风格参考 v3：没有 bundle 时按当前活动绑定轻量现解析（StylePolicy.mode == "live"）
    assert gate["runtime_contract_mode"] == "live"
    assert set(gate["quantitative"]) == {"checked", "passed"}
    events = _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)
    assert len(events) == 1 and events[0].outcome == "pass"
    assert events[0].context_json["stage"] == "style_draft"
    assert gate["metric_event_id"] == events[0].event_id


def test_styled_gate_plagiarism_hit_is_reported_without_leaking_source_text(session) -> None:
    _seed_style_binding(project_id="proj_styled_plag", seed="styled_plag", paragraphs=[REFERENCE_PARAGRAPH])
    scene = _make_scene("proj_styled_plag")
    styled = f"他回到巷口。{COPIED_SENTENCE}。他没有停下。"
    gate = run_styled_draft_style_gate(session, scene, styled, stage="style_draft")
    assert gate is not None
    assert gate["verdict"] == "plagiarism"
    assert gate["plagiarism_passed"] is False
    assert gate["plagiarism_hit_count"] >= 1 and len(gate["plagiarism_hits"]) >= 1
    hit = gate["plagiarism_hits"][0]
    assert set(hit) == {"position", "matched_length", "matched_sha256"}
    assert hit["matched_length"] >= 12
    # 命中片段本身就是参考原文：诊断里只留位置 / 长度 / 指纹，不落文本
    assert COPIED_SENTENCE not in str(gate)
    events = _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)
    assert [event.outcome for event in events] == ["plagiarism"]
    assert events[0].context_json["plagiarism_hit_count"] == gate["plagiarism_hit_count"]


def test_styled_gate_reports_frozen_banned_term_hits(session) -> None:
    _seed_style_binding(
        project_id="proj_styled_term", seed="styled_term", forbidden_terms=["美轮美奂"],
    )
    scene = _make_scene("proj_styled_term")
    gate = run_styled_draft_style_gate(session, scene, "这景色真是美轮美奂极了。", stage="soft_qc")
    assert gate is not None
    assert gate["verdict"] == "fail"
    assert gate["plagiarism_passed"] is True
    assert [hit["matched_excerpt"] for hit in gate["forbidden_hits"]] == ["美轮美奂"]
    assert gate["forbidden_hit_count"] == 1


def _frozen_bundle(session, scene: SceneCard, *, bundle_id: str) -> dict:
    """用当前 active 绑定冻结一份运行时契约，嵌进 bundle 快照（与 BundleBuilder 同形）。"""
    import json

    from novel_system.services.bundle_builder import resolve_scene_style_runtime_contract

    contract = resolve_scene_style_runtime_contract(session, scene)
    assert contract is not None
    return {
        "bundle_id": bundle_id,
        "bundle_snapshot_hash": f"{bundle_id}_hash",
        "snapshot": {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "source_version_refs": {
                "style_reference_runtime_contract_status": "frozen",
                "style_reference_runtime_contract_hash": contract["contract_hash"],
            },
            "inline_digests": {
                "scene_card": "Goal",
                "_style_reference_runtime_contract": json.dumps(contract, ensure_ascii=False),
            },
        },
    }


def test_styled_gate_matches_the_live_banned_term_table_not_the_frozen_contract(session) -> None:
    """风格参考 v3（H1）：禁用词只认现行的表（与成稿门、抄袭门同一张）——冻结之后新加的词照样认，作者删掉的词
    （哪怕冻结契约里还记着）立刻不再认；冻结契约里的词只用来渲染提示词的红线。"""
    _seed_style_binding(
        project_id="proj_styled_frozen", seed="styled_frozen", forbidden_terms=["冻结词"],
    )
    scene = _make_scene("proj_styled_frozen")
    bundle = _frozen_bundle(session, scene, bundle_id="bundle_styled_frozen")
    # 冻结之后再加一个禁用词——实时表有、冻结契约没有
    with SessionLocal() as other:
        StyleReferenceRepository(other).create_banned_term(
            term_id="sr_term_styled_frozen_late", profile_id="sr_profile_styled_frozen",
            scope="generation", term="事后词", source="manual",
        )
        other.commit()
    text = "文中出现了事后词，也出现了冻结词。"
    gate = run_styled_draft_style_gate(session, scene, text, stage="style_draft", bundle=bundle)
    assert gate is not None
    assert gate["runtime_contract_mode"] == "frozen"
    assert gate["runtime_contract_hash"]
    assert [hit["matched_excerpt"] for hit in gate["forbidden_hits"]] == ["事后词", "冻结词"]

    # 作者把「冻结词」从表里删了：冻结契约里还记着，也不再认
    StyleReferenceRepository(session).delete_banned_term("sr_term_styled_frozen_0")
    session.commit()
    gate = run_styled_draft_style_gate(session, scene, text, stage="style_draft", bundle=bundle)
    assert [hit["matched_excerpt"] for hit in gate["forbidden_hits"]] == ["事后词"]


def test_styled_gate_is_unavailable_when_the_bound_book_was_deleted(session) -> None:
    """风格参考 v3（L4）：bundle 冻结的书后来被删了——抄袭门对它什么也没比对，风格稿门报 unavailable（软 QC 挂 Q2
    复核、起草链路发 STYLE_GATE_UNAVAILABLE），不能报「通过」。"""
    from novel_system.services.style_reference.cleanup import delete_reference_book

    _seed_style_binding(project_id="proj_styled_gone", seed="styled_gone", paragraphs=[REFERENCE_PARAGRAPH])
    scene = _make_scene("proj_styled_gone")
    bundle = _frozen_bundle(session, scene, bundle_id="bundle_styled_gone")
    delete_reference_book(session, "sr_book_styled_gone")
    session.commit()

    gate = run_styled_draft_style_gate(
        session, scene, f"他回到巷口。{COPIED_SENTENCE}。", stage="style_draft", bundle=bundle
    )
    assert gate is not None
    assert gate["verdict"] == STYLED_GATE_UNAVAILABLE_VERDICT
    assert gate["error_code"] == "STYLE_REFERENCE_BOOK_MISSING"
    assert gate["runtime_contract_mode"] == "frozen"
    events = _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)
    assert [event.outcome for event in events] == ["error"]


def test_styled_gate_absent_contract_means_no_binding(session) -> None:
    """bundle 显式冻结「无绑定」时，事后新增的绑定不得影响回放。"""
    scene = _make_scene("proj_styled_absent")
    bundle = {
        "bundle_id": "b_absent",
        "bundle_snapshot_hash": "h",
        "snapshot": {
            "source_version_refs": {"style_reference_runtime_contract_status": "absent"},
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    _seed_style_binding(project_id="proj_styled_absent", seed="styled_absent", paragraphs=[REFERENCE_PARAGRAPH])
    assert run_styled_draft_style_gate(session, scene, COPIED_SENTENCE, bundle=bundle) is None


def test_styled_gate_failure_is_unavailable_not_no_binding(session) -> None:
    """校验异常 → verdict=unavailable（只记异常类型名），仍记 error 事件；绝不返 None 冒充无绑定。"""
    _seed_style_binding(project_id="proj_styled_boom", seed="styled_boom")
    scene = _make_scene("proj_styled_boom")
    with patch(
        "novel_system.services.reference_copy_gate.check_reference_copy",
        side_effect=RuntimeError("boom secret excerpt"),
    ):
        gate = run_styled_draft_style_gate(session, scene, CLEAN_TEXT)
    assert gate is not None
    assert gate["verdict"] == STYLED_GATE_UNAVAILABLE_VERDICT == "unavailable"
    assert gate["stage"] == "style_draft"
    assert gate["error"] == "RuntimeError" and gate["error_code"] is None
    assert gate["profile_id"] == "sr_profile_styled_boom"
    assert gate["plagiarism_hits"] == [] and gate["forbidden_hits"] == []
    assert gate["plagiarism_hit_count"] is None and gate["forbidden_hit_count"] is None
    # 异常文本可能夹带参考原文：不落诊断
    assert "secret excerpt" not in str(gate)
    events = _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)
    assert [(event.outcome, event.profile_id) for event in events] == [("error", "sr_profile_styled_boom")]
    assert events[0].context_json["error"] == "RuntimeError"
    assert gate["metric_event_id"] == events[0].event_id


def test_styled_gate_contract_error_is_unavailable_and_recorded_without_profile(session) -> None:
    """契约解析失败发生在解析出画像之前（frozen 状态却没有契约）：也要有诊断与事件。"""
    _seed_style_binding(project_id="proj_styled_ctr", seed="styled_ctr")
    scene = _make_scene("proj_styled_ctr")
    bundle = {
        "bundle_id": "b_ctr",
        "bundle_snapshot_hash": "h",
        "snapshot": {
            "source_version_refs": {"style_reference_runtime_contract_status": "frozen"},
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    gate = run_styled_draft_style_gate(session, scene, CLEAN_TEXT, bundle=bundle)
    assert gate is not None
    assert gate["verdict"] == STYLED_GATE_UNAVAILABLE_VERDICT
    assert gate["error"] == "ValueError"
    assert gate["error_code"] == "runtime_contract_missing"
    assert gate["runtime_contract_mode"] == "degraded"
    assert gate["profile_id"] is None
    events = _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)
    assert [(event.outcome, event.profile_id) for event in events] == [("error", None)]
    assert events[0].context_json["error_code"] == "runtime_contract_missing"
    assert events[0].context_json["stage"] == "style_draft"


def test_styled_gate_accepts_near_final_rewrite_stage_and_records_it(session) -> None:
    """准终稿重写稿也是会直接成为终稿的风格化输出：gate 必须认这个阶段并记真实阶段。"""
    assert "near_final_rewrite" in STYLED_DRAFT_GATE_STAGES
    _seed_style_binding(
        project_id="proj_styled_nfr", seed="styled_nfr", paragraphs=[REFERENCE_PARAGRAPH],
    )
    scene = _make_scene("proj_styled_nfr")
    gate = run_styled_draft_style_gate(
        session, scene, f"他回到巷口。{COPIED_SENTENCE}。", stage="near_final_rewrite"
    )
    assert gate is not None
    assert gate["stage"] == "near_final_rewrite" and gate["verdict"] == "plagiarism"
    events = _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)
    assert [(event.context_json["stage"], event.outcome) for event in events] == [
        ("near_final_rewrite", "plagiarism")
    ]


# ---------------------------------------------------------------------------
# SoftQcEngine：前缀注入 + styled-draft gate 升级
# ---------------------------------------------------------------------------

SOFT_SCENE_ID = "CH810_SC01"
SOFT_DRAFT_ROW_ID = "draft_style_CH810_SC01"


def _seed_soft_scene(session, *, project_id: str, draft_content: str) -> SceneCard:
    session.add(StoryProject(project_id=project_id, title="Soft QC", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="CH810",
            project_id=project_id,
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    scene = SceneCard(
        scene_id=SOFT_SCENE_ID,
        chapter_id="CH810",
        project_id=project_id,
        scene_seq=1,
        pov_character_id="A",
        onstage_chars_json=["A"],
        scene_goal="Force both characters to reveal what they know.",
        must_include_text="",
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=SOFT_SCENE_ID, scene_status="style_draft_ready"))
    session.add(
        SceneDraft(
            row_id=SOFT_DRAFT_ROW_ID,
            scene_id=SOFT_SCENE_ID,
            chapter_id="CH810",
            stage="style_draft",
            content=draft_content,
            source_bundle_id="bundle_CH810_SC01",
            source_bundle_hash="bundle_hash_CH810_SC01",
        )
    )
    session.commit()
    return scene


class _SoftPassRunner:
    """假 LLMNodeRunner：记录收到的 prompt，返回合法的 soft_pass payload。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(
            llm_call_id=f"llm_call_soft_{len(self.calls)}",
            response=SimpleNamespace(
                structured_output={
                    "resolution_code": "soft_pass",
                    "pass_flag": True,
                    "next_action": "pass",
                    "issues": [],
                    "rewrite_brief": [],
                }
            ),
        )


def _soft_bundle() -> dict:
    return {
        "bundle_id": "bundle_CH810_SC01",
        "bundle_snapshot_hash": "bundle_hash_CH810_SC01",
        "snapshot": {
            "scene_id": SOFT_SCENE_ID,
            "chapter_id": "CH810",
            "inline_digests": {"scene_card": "Goal"},
        },
    }


def _run_soft_qc(session, runner, draft_content: str):
    engine = SoftQcEngine(session, llm_runner=runner)
    state = session.get(SceneRunState, SOFT_SCENE_ID)
    state.active_execution_id = "exec-soft-style"
    state.run_execution_status = "active"
    session.commit()
    token = begin_llm_execution("exec-soft-style")
    try:
        return engine.evaluate(
            scene_id=SOFT_SCENE_ID,
            bundle=_soft_bundle(),
            source_draft_row_id=SOFT_DRAFT_ROW_ID,
            source_draft_content=draft_content,
        )
    finally:
        end_llm_execution(token)


def test_soft_qc_prompt_receives_same_style_reference_prefix(session) -> None:
    # 2026-09-24 起没有旧画像的卡替身：前缀里的抽象块是 v3 文风卡 + 声音特征
    _seed_style_binding(
        project_id="proj_soft_prefix", seed="soft_prefix",
        profile_json={
            "profile_version": "style_profile_v3",
            "dimension_card": {
                "version": "dimension_card_v1",
                "temperament": [],
                "dimensions": [
                    {
                        "dimension": "language.sentence_structure",
                        "distinctiveness": 0.9,
                        "devices": [],
                        "lines": [{"text": "短句克制", "mandatory": True, "distinctiveness": 0.9}],
                    }
                ],
            },
            "voice_signature": {"version": "voice_signature_v2", "habits": ["动作先于解释"], "deliberate_repetition": False},
        },
        paragraphs=[REFERENCE_PARAGRAPH],
    )
    _seed_soft_scene(session, project_id="proj_soft_prefix", draft_content=CLEAN_TEXT)
    runner = _SoftPassRunner()
    decision = _run_soft_qc(session, runner, CLEAN_TEXT)
    session.commit()

    assert decision.branch == "continue"
    assert len(runner.calls) == 1
    system_prompt = runner.calls[0]["prompt"]["system_prompt"]
    assert system_prompt.startswith("[STYLE_REFERENCE]\n")
    assert "短句克制" in system_prompt and "动作先于解释" in system_prompt
    # 模板文案：不再是七维打分，而是对照文风卡 / 声音特征
    assert "[声音特征]" in system_prompt and "[文风卡]" in system_prompt
    assert "Score style adherence from 0 to 1" not in runner.calls[0]["user_prompt"]
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "soft_qc")
    ).scalars().one()
    assert attempt.details_json["style_reference_runtime"]["outcome"] == "hit"
    assert attempt.details_json["styled_draft_gate"]["verdict"] == "pass"
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []


def test_soft_qc_styled_gate_plagiarism_escalates_to_human_review(session) -> None:
    _seed_style_binding(project_id="proj_soft_plag", seed="soft_plag", paragraphs=[REFERENCE_PARAGRAPH])
    styled = f"他回到巷口。{COPIED_SENTENCE}。他没有停下。"
    _seed_soft_scene(session, project_id="proj_soft_plag", draft_content=styled)
    runner = _SoftPassRunner()
    decision = _run_soft_qc(session, runner, styled)
    session.commit()

    assert decision.branch == "human_review_required"
    assert decision.should_continue is False
    assert decision.stop_reason == STYLE_VALIDATION_PLAGIARISM_TRIGGER
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    assert report.resolution_code == "soft_block_human"
    assert report.next_action == "human_review_required"
    issue = next(item for item in report.issues_json if item["issue_key"] == STYLE_PLAGIARISM_ISSUE_KEY)
    assert issue["quality_level"] == "Q0"
    assert issue["blocking"] is True
    assert issue["verified_by"] == "style_plagiarism_ngram"
    event = session.execute(select(HumanReviewEvent)).scalars().one()
    assert event.details_json["trigger_reason"] == STYLE_VALIDATION_PLAGIARISM_TRIGGER
    # 抄袭红线不开放软风险接受
    assert "accept_soft_risk" not in event.allowed_actions_json
    state = session.get(SceneRunState, SOFT_SCENE_ID)
    assert state.scene_status == "human_review_required"
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "soft_qc")
    ).scalars().one()
    assert attempt.status == "human_review_required"
    assert attempt.details_json["styled_draft_gate"]["verdict"] == "plagiarism"


def test_soft_qc_styled_gate_banned_term_requests_human_review_with_soft_risk_option(session) -> None:
    _seed_style_binding(
        project_id="proj_soft_term", seed="soft_term", forbidden_terms=["美轮美奂"],
    )
    styled = "这景色真是美轮美奂极了。他把信封放到桌上。"
    _seed_soft_scene(session, project_id="proj_soft_term", draft_content=styled)
    runner = _SoftPassRunner()
    decision = _run_soft_qc(session, runner, styled)
    session.commit()

    assert decision.branch == "human_review_required"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    issue = next(item for item in report.issues_json if item["issue_key"] == STYLE_BANNED_TERM_ISSUE_KEY)
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert "美轮美奂" in issue["message"]
    event = session.execute(select(HumanReviewEvent)).scalars().one()
    assert event.details_json["trigger_reason"] == "soft_qc_requested_human_review"
    assert "accept_soft_risk" in event.allowed_actions_json


def test_soft_qc_styled_gate_unavailable_requests_human_review_and_is_recorded(session) -> None:
    """gate 没跑成 ≠ 无绑定：带样例前缀生成的风格稿没有过抄袭检查 → Q2 + 人工复核（可软风险接受）。"""
    _seed_style_binding(project_id="proj_soft_unavail", seed="soft_unavail", paragraphs=[REFERENCE_PARAGRAPH])
    # 这份风格稿若 gate 跑了会判抄袭；gate 失败时它绝不能悄悄以 continue 交付。
    styled = f"他回到巷口。{COPIED_SENTENCE}。他没有停下。"
    _seed_soft_scene(session, project_id="proj_soft_unavail", draft_content=styled)
    runner = _SoftPassRunner()
    with patch(
        "novel_system.services.reference_copy_gate.check_reference_copy",
        side_effect=ValueError("reference paragraphs unreadable"),
    ):
        decision = _run_soft_qc(session, runner, styled)
    session.commit()

    assert decision.branch == "human_review_required"
    assert decision.should_continue is False
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    assert report.resolution_code == "soft_block_human"
    assert report.next_action == "human_review_required"
    issue = next(item for item in report.issues_json if item["issue_key"] == STYLE_GATE_UNAVAILABLE_ISSUE_KEY)
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["details"]["error"] == "ValueError"
    assert STYLE_PLAGIARISM_ISSUE_KEY not in [item["issue_key"] for item in report.issues_json]
    event = session.execute(select(HumanReviewEvent)).scalars().one()
    assert event.details_json["trigger_reason"] == "soft_qc_requested_human_review"
    assert "accept_soft_risk" in event.allowed_actions_json
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "soft_qc")
    ).scalars().one()
    gate = attempt.details_json["styled_draft_gate"]
    assert gate["verdict"] == STYLED_GATE_UNAVAILABLE_VERDICT and gate["error"] == "ValueError"
    assert [event.outcome for event in _metric_events(session, STYLED_DRAFT_GATE_EVENT_KIND)] == ["error"]


def test_soft_qc_without_binding_keeps_plain_prompt_and_no_gate(session) -> None:
    _seed_soft_scene(session, project_id="proj_soft_plain", draft_content=CLEAN_TEXT)
    runner = _SoftPassRunner()
    decision = _run_soft_qc(session, runner, CLEAN_TEXT)
    session.commit()

    assert decision.branch == "continue"
    assert not runner.calls[0]["prompt"]["system_prompt"].startswith("[STYLE_REFERENCE]")
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "soft_qc")
    ).scalars().one()
    assert "style_reference_runtime" not in attempt.details_json
    assert "styled_draft_gate" not in attempt.details_json
