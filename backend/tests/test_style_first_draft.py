"""2026-09-12 风格直起（Step 2）：draft_mode 契约、首稿直起、定稿式复读、长度带放宽。

规格：docs/style-first-draft-plan-2026-09-12.md。fake LLM；无真实模型。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.api.routes.style_reference import ApplyProfileRequest
from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services import scene_generation as sg
from novel_system.services.bundle_builder import latest_styled_draft_for_scene
from novel_system.services.qc_engine import STYLED_DRAFT_GATE_STAGES
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    DRAFT_MODE_NEUTRAL_FIRST,
    DRAFT_MODE_STYLE_FIRST,
    build_style_runtime_contract,
    effective_draft_mode,
    is_style_bound,
    resolve_draft_mode,
    validate_style_runtime_contract,
)
from tests.real_llm_fakes import install_online_pipeline
from tests.test_style_reference_injection_v2 import _bind, _seed_full


@pytest.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    install_online_pipeline(monkeypatch)


_NEUTRAL_TEXT = "门外的脚步停住了。他把信封放到桌上，等对面的人先开口。她没有伸手去接。"
_VOICED_TEXT = "脚步在门外停了；他将信封搁到桌上——也不说话，只等着。她终于没有去接。"


def _seed_binding(seed: str, *, project_id: str, config_json: dict | None = None) -> tuple[str, str]:
    """真实规模画像 + project 作用域绑定；返回 (book_id, profile_id)。"""
    book_id, profile_id = _seed_full(seed)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        _bind(
            repo,
            binding_id=f"sfd_bind_{seed}",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            strategy="mixed",
            config_json=config_json,
        )
        session.commit()
    return book_id, profile_id


def _frozen_bundle(project_id: str, scene_id: str, chapter_id: str) -> dict:
    """按 bundle_builder 的冻结方式（status=frozen + inline contract）造一份 bundle。"""
    with SessionLocal() as session:
        from novel_system.services.style_reference.injection import InjectionService

        svc = InjectionService(session)
        layers = svc.resolve_binding_layers(project_id, "scene_generation", character_ids=[], scene_id=scene_id)
        contract = build_style_runtime_contract(svc.repo, layers, task_type="scene_generation")
    return {
        "bundle_id": f"bundle_{scene_id}_sfd",
        "bundle_snapshot_hash": "bundle_hash_sfd",
        "snapshot": {
            "contract_version": "BSHASH_v1",
            "stage_allowlist_name": "bundle_build_allowlist_v1",
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "source_version_refs": {
                "style_reference_runtime_contract_status": "frozen",
                "style_reference_runtime_contract_version": contract["contract_version"],
                "style_reference_runtime_contract_hash": contract["contract_hash"],
            },
            "inline_digests": {
                "scene_card": "Reveal the letter without explaining it.",
                "_style_reference_runtime_contract": json.dumps(contract, ensure_ascii=False, sort_keys=True),
            },
        },
    }


def _seed_scene(session, *, project_id: str, scene_id: str, chapter_id: str, band: str = "short") -> SceneCard:
    session.add(StoryProject(project_id=project_id, title="sfd", outline_text=""))
    session.add(ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="g"))
    scene = SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="Café",
        scene_goal="reveal",
        beats_json=["arrival"],
        must_include_text="信封",
        target_length_band=band,
        scene_type="reveal",
        is_chapter_last=0,
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))
    session.commit()
    return scene


class _Runner:
    def __init__(self, outputs: dict[str, str], default: str) -> None:
        self.outputs = outputs
        self.default = default
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        text = self.outputs.get(str(kwargs.get("step")), self.default)
        return SimpleNamespace(
            llm_call_id=f"llm_call_sfd_{len(self.calls)}",
            response=SimpleNamespace(structured_output={"scene_text": text}),
        )


# ---------------------------------------------------------------------------
# W1 · 契约里的 draft_mode
# ---------------------------------------------------------------------------


def test_contract_freezes_default_style_first_and_explicit_neutral_first() -> None:
    _seed_binding("sfd_c1", project_id="proj_sfd_c1")
    bundle = _frozen_bundle("proj_sfd_c1", "SFD_C1_SC01", "SFD_C1")
    contract = json.loads(bundle["snapshot"]["inline_digests"]["_style_reference_runtime_contract"])
    assert contract["draft_mode"] == DRAFT_MODE_STYLE_FIRST
    assert effective_draft_mode(bundle) == DRAFT_MODE_STYLE_FIRST and is_style_bound(bundle)

    _seed_binding("sfd_c2", project_id="proj_sfd_c2", config_json={"draft_mode": "neutral_first"})
    bundle2 = _frozen_bundle("proj_sfd_c2", "SFD_C2_SC01", "SFD_C2")
    contract2 = json.loads(bundle2["snapshot"]["inline_digests"]["_style_reference_runtime_contract"])
    assert contract2["draft_mode"] == DRAFT_MODE_NEUTRAL_FIRST
    assert effective_draft_mode(bundle2) == DRAFT_MODE_NEUTRAL_FIRST and not is_style_bound(bundle2)


def test_resolve_draft_mode_ignores_garbage_and_falls_back_to_yaml_default() -> None:
    assert resolve_draft_mode({"draft_mode": "neutral_first"}) == DRAFT_MODE_NEUTRAL_FIRST
    assert resolve_draft_mode({"draft_mode": " Style_First "}) == DRAFT_MODE_STYLE_FIRST
    assert resolve_draft_mode({"draft_mode": "whatever"}) == DRAFT_MODE_STYLE_FIRST
    assert resolve_draft_mode(None) == DRAFT_MODE_STYLE_FIRST


def test_validation_rejects_bad_draft_mode_and_old_contracts_default_to_neutral_first() -> None:
    _seed_binding("sfd_c3", project_id="proj_sfd_c3")
    bundle = _frozen_bundle("proj_sfd_c3", "SFD_C3_SC01", "SFD_C3")
    contract = json.loads(bundle["snapshot"]["inline_digests"]["_style_reference_runtime_contract"])
    broken = dict(contract)
    broken["draft_mode"] = "hybrid"
    with pytest.raises(ValueError):
        validate_style_runtime_contract(broken)
    # 旧契约没有 draft_mode 键(重新计算哈希以模拟当年冻结的契约)→ neutral_first,重放不变
    from novel_system.services.style_reference.runtime_contract import _json_hash

    old = {k: v for k, v in contract.items() if k not in {"draft_mode", "contract_hash"}}
    old["contract_hash"] = _json_hash(old)
    validate_style_runtime_contract(old)
    old_bundle = json.loads(json.dumps(bundle))
    old_bundle["snapshot"]["inline_digests"]["_style_reference_runtime_contract"] = json.dumps(old, ensure_ascii=False, sort_keys=True)
    old_bundle["snapshot"]["source_version_refs"]["style_reference_runtime_contract_hash"] = old["contract_hash"]
    assert effective_draft_mode(old_bundle) == DRAFT_MODE_NEUTRAL_FIRST
    # 无绑定 / 明确 absent → neutral_first
    assert effective_draft_mode(None) == DRAFT_MODE_NEUTRAL_FIRST
    assert effective_draft_mode({"snapshot": {"source_version_refs": {"style_reference_runtime_contract_status": "absent"}}}) == DRAFT_MODE_NEUTRAL_FIRST


def test_apply_request_carries_draft_mode_into_config_json() -> None:
    req = ApplyProfileRequest(scope="project", scope_ref_id="p", draft_mode="neutral_first", intensity=80)
    assert req.injection_config() == {"intensity": 80, "draft_mode": "neutral_first"}
    assert "draft_mode" not in ApplyProfileRequest(scope="project", scope_ref_id="p").injection_config()
    with pytest.raises(ValueError):
        ApplyProfileRequest(scope="project", scope_ref_id="p", draft_mode="hybrid")


# ---------------------------------------------------------------------------
# W2 · 首稿直起
# ---------------------------------------------------------------------------


def test_style_first_writes_the_first_draft_in_the_reference_hand(session) -> None:
    _seed_binding("sfd_d1", project_id="proj_sfd_d1")
    scene = _seed_scene(session, project_id="proj_sfd_d1", scene_id="SFD_D1_SC01", chapter_id="SFD_D1")
    bundle = _frozen_bundle("proj_sfd_d1", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)

    result = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()

    call = runner.calls[0]
    assert call["step"] == "neutral_draft" and call["node_id"] == "style_draft"
    assert call["prompt"]["template_name"] == "style_first_draft"
    assert call["prompt"]["system_prompt"].startswith("[STYLE_REFERENCE]\n")
    assert "[风格样例]" in call["prompt"]["system_prompt"]
    assert "in the hand of a specific reference author" in call["prompt"]["system_prompt"]
    assert "Scene Length Guide" not in call["user_prompt"]  # band "short" 不是数字带
    assert result.draft_mode == DRAFT_MODE_STYLE_FIRST
    codes = [item["code"] for item in result.notices]
    assert sg.STYLE_NOTICE_FIRST_DRAFT in codes
    assert sg.STYLE_NOTICE_INJECTION_MISS not in codes and sg.STYLE_NOTICE_INJECTION_DEGRADED not in codes
    # 步位 / 行 / 指针不变
    row = session.execute(
        select(SceneDraft).where(SceneDraft.scene_id == scene.scene_id, SceneDraft.stage == "neutral_draft")
    ).scalars().one()
    assert row.content == _VOICED_TEXT and result.row_id == row.row_id
    state = session.get(SceneRunState, scene.scene_id)
    assert state.current_neutral_draft_row_id == row.row_id == state.latest_valid_draft_row_id
    attempt = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.scene_id == scene.scene_id,
            AttemptTracker.step == "neutral_draft",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()
    details = attempt.details_json
    assert details["content_source"] == sg.STYLE_FIRST_DRAFT_CONTENT_SOURCE
    assert details["draft_mode"] == DRAFT_MODE_STYLE_FIRST and details["template_name"] == "style_first_draft"
    assert sg.STYLE_NOTICE_FIRST_DRAFT in [item["code"] for item in details["notices"]]
    assert details["style_reference_runtime"]["generation_outcome"] == sg.STYLE_FIRST_DRAFT_CONTENT_SOURCE
    # 首稿也过门(无抄袭 → 没有命中 notice,但门裁决已记录)
    assert "styled_draft_gate" in details and result.styled_draft_gate is not None
    assert sg.STYLE_NOTICE_PLAGIARISM_HIT not in codes
    # API 回读:首稿 notices 也在当前运行内
    assert sg.STYLE_NOTICE_FIRST_DRAFT in [
        item["code"] for item in sg.latest_style_notices(session, scene.scene_id, bundle_id=bundle["bundle_id"])
    ]


def test_neutral_first_binding_keeps_the_neutral_draft_unchanged(session) -> None:
    _seed_binding("sfd_d2", project_id="proj_sfd_d2", config_json={"draft_mode": "neutral_first"})
    scene = _seed_scene(session, project_id="proj_sfd_d2", scene_id="SFD_D2_SC01", chapter_id="SFD_D2")
    bundle = _frozen_bundle("proj_sfd_d2", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={}, default=_NEUTRAL_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)

    result = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()

    call = runner.calls[0]
    assert call["step"] == "neutral_draft" and call["node_id"] == "neutral_draft"
    assert call["prompt"]["template_name"] == "neutral_draft"
    assert "[STYLE_REFERENCE]" not in call["prompt"]["system_prompt"]
    assert result.draft_mode == DRAFT_MODE_NEUTRAL_FIRST and result.notices == []
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "neutral_draft")
    ).scalars().one()
    # 对照组的 attempt 明细逐字不变:没有 draft_mode / template_name / content_source
    assert attempt.details_json == {"row_id": result.row_id, "llm_call_id": "llm_call_sfd_1"}


def test_style_first_repair_keeps_the_prefix_and_label(session) -> None:
    _seed_binding("sfd_d3", project_id="proj_sfd_d3")
    scene = _seed_scene(session, project_id="proj_sfd_d3", scene_id="SFD_D3_SC01", chapter_id="SFD_D3")
    bundle = _frozen_bundle("proj_sfd_d3", scene.scene_id, scene.chapter_id)
    # 首稿漏了必含项「信封」→ 一次修复;修复稿合格
    runner = _Runner(outputs={"neutral_draft": "脚步在门外停了；他什么也没放下。", "neutral_draft_repair": _VOICED_TEXT}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    result = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    assert result.content == _VOICED_TEXT
    repair_call = runner.calls[1]
    assert repair_call["step"] == "neutral_draft_repair" and repair_call["node_id"] == "style_draft"
    assert repair_call["prompt"]["system_prompt"].startswith("[STYLE_REFERENCE]\n")
    assert "Rejected First Draft Requiring One Deterministic Repair" in repair_call["user_prompt"]
    assert "keep the reference author's manner" in repair_call["user_prompt"]


def test_style_draft_step_labels_the_source_as_first_draft_under_style_first(session) -> None:
    _seed_binding("sfd_d4", project_id="proj_sfd_d4")
    scene = _seed_scene(session, project_id="proj_sfd_d4", scene_id="SFD_D4_SC01", chapter_id="SFD_D4")
    bundle = _frozen_bundle("proj_sfd_d4", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    call = runner.calls[-1]
    assert call["step"] == "style_draft" and call["prompt"]["template_name"] == "style_draft"
    assert f"## {sg.FIRST_DRAFT_SOURCE_LABEL}" in call["user_prompt"]
    assert "## Approved Neutral Draft" not in call["user_prompt"]
    assert "one step closer to the reference samples" in call["user_prompt"]
    assert refined.content == _VOICED_TEXT
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "style_draft")
    ).scalars().one()
    assert attempt.details_json["content_source"] == "provider_style_output"
    assert attempt.details_json["style_reference_runtime"]["draft_mode"] == DRAFT_MODE_STYLE_FIRST


def test_style_draft_step_keeps_neutral_label_under_neutral_first(session) -> None:
    _seed_binding("sfd_d5", project_id="proj_sfd_d5", config_json={"draft_mode": "neutral_first"})
    scene = _seed_scene(session, project_id="proj_sfd_d5", scene_id="SFD_D5_SC01", chapter_id="SFD_D5")
    bundle = _frozen_bundle("proj_sfd_d5", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    service.generate_style_draft(scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content)
    call = runner.calls[-1]
    assert "## Approved Neutral Draft" in call["user_prompt"]
    assert f"## {sg.FIRST_DRAFT_SOURCE_LABEL}" not in call["user_prompt"]


def test_refine_fallback_returns_to_the_first_draft_and_the_first_draft_anchors_voice(session) -> None:
    _seed_binding("sfd_d6", project_id="proj_sfd_d6")
    scene = _seed_scene(session, project_id="proj_sfd_d6", scene_id="SFD_D6_SC01", chapter_id="SFD_D6")
    bundle = _frozen_bundle("proj_sfd_d6", scene.scene_id, scene.chapter_id)
    lost_required = "脚步在门外停了；他把那个东西搁到桌上——也不说话。她没有去接。"
    runner = _Runner(outputs={"style_draft": lost_required}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    codes = [item["code"] for item in refined.notices]
    assert sg.STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL in codes
    fallback = next(item for item in refined.notices if item["code"] == sg.STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL)
    assert "首稿" in fallback["message"] and fallback["draft_mode"] == DRAFT_MODE_STYLE_FIRST
    assert refined.content == _VOICED_TEXT
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "style_draft")
    ).scalars().one()
    assert attempt.details_json["content_source"] == sg.FIRST_DRAFT_FALLBACK_CONTENT_SOURCE
    assert attempt.details_json["style_reference_runtime"]["generation_outcome"] == sg.FIRST_DRAFT_FALLBACK_CONTENT_SOURCE
    # 声音锚:回退稿的正文就是首稿(目标文风),不再按「与中性稿相同即回退」排除
    anchor = latest_styled_draft_for_scene(session, scene.scene_id)
    assert anchor is not None and anchor.content == _VOICED_TEXT


def test_first_draft_alone_can_anchor_the_next_scene_voice(session) -> None:
    _seed_binding("sfd_d7", project_id="proj_sfd_d7")
    scene = _seed_scene(session, project_id="proj_sfd_d7", scene_id="SFD_D7_SC01", chapter_id="SFD_D7")
    bundle = _frozen_bundle("proj_sfd_d7", scene.scene_id, scene.chapter_id)
    service = SceneGenerationService(session, llm_runner=_Runner(outputs={}, default=_VOICED_TEXT))
    service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    anchor = latest_styled_draft_for_scene(session, scene.scene_id)
    assert anchor is not None and anchor.stage == "neutral_draft" and anchor.content == _VOICED_TEXT


def test_neutral_first_neutral_draft_never_anchors_voice(session) -> None:
    _seed_binding("sfd_d8", project_id="proj_sfd_d8", config_json={"draft_mode": "neutral_first"})
    scene = _seed_scene(session, project_id="proj_sfd_d8", scene_id="SFD_D8_SC01", chapter_id="SFD_D8")
    bundle = _frozen_bundle("proj_sfd_d8", scene.scene_id, scene.chapter_id)
    service = SceneGenerationService(session, llm_runner=_Runner(outputs={}, default=_NEUTRAL_TEXT))
    service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    assert latest_styled_draft_for_scene(session, scene.scene_id) is None


# ---------------------------------------------------------------------------
# W2 · 长度带放宽
# ---------------------------------------------------------------------------


def test_length_band_widens_only_inside_a_style_bound_context() -> None:
    _seed_binding("sfd_l1", project_id="proj_sfd_l1")
    bound = _frozen_bundle("proj_sfd_l1", "SFD_L1_SC01", "SFD_L1")
    assert sg._parse_numeric_length_band("1200-1800") == (1200, 1800)
    with sg._length_band_slack_for(bound):
        assert sg._parse_numeric_length_band("1200-1800") == (600, 2700)
        assert sg._parse_numeric_length_band("1200-1800", slack=0.0) == (1200, 1800)
    assert sg._parse_numeric_length_band("1200-1800") == (1200, 1800)
    with sg._length_band_slack_for({"snapshot": {}}):
        assert sg._parse_numeric_length_band("1200-1800") == (1200, 1800)


def test_style_first_accepts_the_authors_scale_where_neutral_first_rejects_it(session) -> None:
    _seed_binding("sfd_l2", project_id="proj_sfd_l2")
    scene = _seed_scene(session, project_id="proj_sfd_l2", scene_id="SFD_L2_SC01", chapter_id="SFD_L2", band="1200-1800")
    bundle = _frozen_bundle("proj_sfd_l2", scene.scene_id, scene.chapter_id)
    short_but_complete = ("脚步在门外停了；他将信封搁到桌上——也不说话，只等着。" * 40)[:900]
    assert 600 <= sg._visible_char_count(short_but_complete) < 1200
    with sg._length_band_slack_for(bundle):
        assessment = sg._assess_neutral_draft(scene, short_but_complete)
    assert assessment["accepted"], assessment
    assert assessment["target_length_range"] == [600, 2700]
    assert not sg._assess_neutral_draft(scene, short_but_complete)["accepted"]
    # 首稿的长度指引告诉模型:计划带 vs 硬范围,作者尺度优先
    with sg._length_band_slack_for(bundle):
        guide = sg._style_first_length_instruction(scene)
    assert "planned 1200-1800" in guide and "hard range 600-2700" in guide
    assert "this author's own means" in guide


def test_style_first_run_uses_the_widened_band_end_to_end(session) -> None:
    _seed_binding("sfd_l3", project_id="proj_sfd_l3")
    scene = _seed_scene(session, project_id="proj_sfd_l3", scene_id="SFD_L3_SC01", chapter_id="SFD_L3", band="1200-1800")
    bundle = _frozen_bundle("proj_sfd_l3", scene.scene_id, scene.chapter_id)
    short_but_complete = ("脚步在门外停了；他将信封搁到桌上——也不说话，只等着。" * 40)[:900]
    assert 600 <= sg._visible_char_count(short_but_complete) < 1200
    runner = _Runner(outputs={}, default=short_but_complete)
    service = SceneGenerationService(session, llm_runner=runner)
    result = service.generate_neutral_draft(scene.scene_id, bundle)
    assert result.content == short_but_complete and len(runner.calls) == 1  # 不触发修复
    assert "hard range 600-2700" in runner.calls[0]["user_prompt"]
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=result.row_id, neutral_content=result.content
    )
    assert sg.STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL not in [item["code"] for item in refined.notices]
    style_calls = [call for call in runner.calls if call["step"] == "style_draft"]
    assert style_calls and "Style Revision Length Guide" in style_calls[-1]["user_prompt"]
    assert "hard range 600-2700" in style_calls[-1]["user_prompt"]


def test_styled_gate_accepts_the_first_draft_stage() -> None:
    assert "neutral_draft" in STYLED_DRAFT_GATE_STAGES


# ---------------------------------------------------------------------------
# W3 · 房风门让位
# ---------------------------------------------------------------------------

_HOUSE_TASTE_TEXT = "脚步在门外停了；他将信封搁到桌上——也不说话，只等着。她终于没有去接。她知道这意味着一切都变了。"


def test_house_taste_gate_is_recorded_but_never_rewrites_under_style_first(session) -> None:
    gate = sg._anti_template_quality_gate(_HOUSE_TASTE_TEXT, scene_id="s", chapter_id="c")
    assert gate["triggered"] and "summary_ending" in gate["risk_dimensions"]
    deferred = sg._defer_house_taste_gate(gate)
    assert deferred["triggered"] is False and deferred["rewrite_pass"] == 0
    assert deferred["findings"] == [] and deferred["risk_dimensions"] == []
    assert deferred["house_taste_gate"] == "deferred_to_reference"
    assert "summary_ending" in deferred["advisory_risk_dimensions"]
    assert len(deferred["advisory_findings"]) == len(gate["findings"]) >= 1

    _seed_binding("sfd_g1", project_id="proj_sfd_g1")
    scene = _seed_scene(session, project_id="proj_sfd_g1", scene_id="SFD_G1_SC01", chapter_id="SFD_G1")
    bundle = _frozen_bundle("proj_sfd_g1", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={"style_draft": _HOUSE_TASTE_TEXT}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    products: list[dict] = []
    refined = service.generate_style_draft(
        scene.scene_id,
        bundle,
        neutral_draft_row_id=first.row_id,
        neutral_content=first.content,
        product_callback=lambda _slot, _kind, _result, payload: products.append(payload),
    )
    session.commit()
    assert refined.content == _HOUSE_TASTE_TEXT
    steps = [str(call["step"]) for call in runner.calls]
    assert "de_template" not in steps, steps
    # 门裁决随产品回调进检查点:房风维度只记录,不触发
    gate_decision = products[-1]["gate_decision"]
    assert gate_decision["house_taste_gate"] == "deferred_to_reference"
    assert gate_decision["triggered"] is False
    assert "summary_ending" in gate_decision["advisory_risk_dimensions"]
    assert products[-1]["de_template_outcome"] == {"status": "not_required"}


def test_house_taste_gate_still_rewrites_under_neutral_first(session) -> None:
    _seed_binding("sfd_g2", project_id="proj_sfd_g2", config_json={"draft_mode": "neutral_first"})
    scene = _seed_scene(session, project_id="proj_sfd_g2", scene_id="SFD_G2_SC01", chapter_id="SFD_G2")
    bundle = _frozen_bundle("proj_sfd_g2", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={"style_draft": _HOUSE_TASTE_TEXT}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    service.generate_style_draft(scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content)
    session.commit()
    steps = [str(call["step"]) for call in runner.calls]
    assert "de_template" in steps, steps


def test_de_template_regression_check_ignores_house_dims_when_deferred(session) -> None:
    scene = _seed_scene(session, project_id="proj_sfd_g3", scene_id="SFD_G3_SC01", chapter_id="SFD_G3")
    source_gate = sg._anti_template_quality_gate(_VOICED_TEXT, scene_id=scene.scene_id, chapter_id=scene.chapter_id)
    strict = sg._assess_de_template_rewrite(
        scene=scene, source_content=_VOICED_TEXT, rewritten_content=_HOUSE_TASTE_TEXT, source_quality_gate=source_gate
    )
    assert "anti_template_risks_increased" in strict["reasons"]
    deferred = sg._assess_de_template_rewrite(
        scene=scene,
        source_content=_VOICED_TEXT,
        rewritten_content=_HOUSE_TASTE_TEXT,
        source_quality_gate=source_gate,
        house_taste_deferred=True,
    )
    assert not any(reason.startswith("anti_template") or reason.startswith("target_quality") for reason in deferred["reasons"])


def test_near_final_deterministic_gates_defer_under_style_bound() -> None:
    from novel_system.services.near_final import _apply_scene_near_final_gates

    payload = {"pass_flag": True, "near_final_status": "ready", "overall_score": 0.9, "scores": {}, "findings": [], "revision_brief": []}
    text = "她知道这意味着一切都变了。他忽然意识到前因后果。"
    strict = _apply_scene_near_final_gates(dict(payload), text)
    assert strict["pass_flag"] is False
    deferred = _apply_scene_near_final_gates(dict(payload), text, style_bound=True)
    assert deferred == payload


def test_final_text_gate_literary_thresholds_defer_under_style_bound(session) -> None:
    from novel_system.db.models import SceneBundle
    from novel_system.services.final_text_gate import FinalTextGateService

    _seed_binding("sfd_g4", project_id="proj_sfd_g4")
    scene = _seed_scene(session, project_id="proj_sfd_g4", scene_id="SFD_G4_SC01", chapter_id="SFD_G4")
    bundle = _frozen_bundle("proj_sfd_g4", scene.scene_id, scene.chapter_id)
    session.add(
        SceneBundle(
            bundle_id=bundle["bundle_id"],
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            bundle_snapshot_hash=bundle["bundle_snapshot_hash"],
            frozen_snapshot_json=bundle["snapshot"],
        )
    )
    state = session.get(SceneRunState, scene.scene_id)
    state.current_bundle_id = bundle["bundle_id"]
    session.commit()
    gate = FinalTextGateService(session)
    text = "他走了。她坐着。天黑了。"  # 无选择 / 无代价 / 无结尾动作:三个阈值在现状下都会拦
    bound = gate._literary(scene, text)
    assert bound["house_taste_thresholds"] == "deferred_to_reference"
    assert not [item for item in bound["promotion_blockers"] if item.startswith("literary:")]
    state.current_bundle_id = None
    session.commit()
    plain = gate._literary(scene, text)
    assert plain["house_taste_thresholds"] == "applied"
    assert [item for item in plain["promotion_blockers"] if item.startswith("literary:")]


def test_prompts_gate_house_taste_behind_the_style_block() -> None:
    import pathlib

    import yaml

    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    assert templates["style_first_draft"]["version"] == "2026-09-12.v1"
    assert templates["style_first_draft"]["input_token_budget"] == 64000
    assert "The bundle decides what happens" in templates["style_first_draft"]["system_prompt"]
    assert "the author's manner wins and the fact of the field stays" in templates["style_first_draft"]["task_prompt"]
    assert templates["style_draft"]["version"] == "2026-09-12.v10"
    assert "First Draft already in the reference author's hand" in templates["style_draft"]["task_prompt"]
    assert "Never wash the voice back toward a neutral register" in templates["style_draft"]["task_prompt"]
    assert templates["hard_qc"]["version"] == "2026-09-12.v3"
    assert "never a hard violation" in templates["hard_qc"]["task_prompt"]
    assert "restate paragraph 3" not in templates["hard_qc"]["task_prompt"]
    assert templates["soft_qc"]["version"] == "2026-09-12.v5"
    assert "only where the reference author demonstrably does not do these things" in templates["soft_qc"]["task_prompt"]
    assert templates["near_final_acceptance_review"]["version"] == "2026-09-12.v4"
    assert "If no [STYLE_REFERENCE] block is present, do not pass scenes" in templates["near_final_acceptance_review"]["task_prompt"]


# ---------------------------------------------------------------------------
# 决策卡 effect 与运行任务视图
# ---------------------------------------------------------------------------


def test_review_effect_apply_carries_draft_mode_like_the_route() -> None:
    from novel_system.services.review_effects import _style_injection_config

    assert _style_injection_config({"intensity": 90, "draft_mode": "neutral_first"}) == {
        "intensity": 90,
        "draft_mode": "neutral_first",
    }
    assert _style_injection_config({"draft_mode": " Style_First "}) == {"draft_mode": "style_first"}
    assert "draft_mode" not in _style_injection_config({"draft_mode": "hybrid"})
    assert "draft_mode" not in _style_injection_config({})


def test_run_job_view_reports_the_frozen_draft_mode(session) -> None:
    from novel_system.db.models import ChapterRunJob, SceneBundle
    from novel_system.services.scene_run_jobs import SceneRunJobService

    _seed_binding("sfd_j1", project_id="proj_sfd_j1")
    scene = _seed_scene(session, project_id="proj_sfd_j1", scene_id="SFD_J1_SC01", chapter_id="SFD_J1")
    bundle = _frozen_bundle("proj_sfd_j1", scene.scene_id, scene.chapter_id)
    session.add(
        SceneBundle(
            bundle_id=bundle["bundle_id"],
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            bundle_snapshot_hash=bundle["bundle_snapshot_hash"],
            frozen_snapshot_json=bundle["snapshot"],
        )
    )
    job = ChapterRunJob(
        job_id="job_sfd_j1",
        chapter_id=scene.chapter_id,
        scene_id=scene.scene_id,
        job_type="scene_run",
        status="running",
        payload_json={"scene_id": scene.scene_id, "current_step": "neutral_running"},
        result_summary_json={},
    )
    session.add(job)
    session.commit()
    service = SceneRunJobService(session)
    assert service.serialize_job(job)["draft_mode"] is None  # bundle 尚未冻结到 run state
    state = session.get(SceneRunState, scene.scene_id)
    state.current_bundle_id = bundle["bundle_id"]
    session.commit()
    assert service.serialize_job(job)["draft_mode"] == DRAFT_MODE_STYLE_FIRST
