"""2026-09-12 风格直起（Step 2）：draft_mode 契约、首稿直起、定稿式复读、长度带放宽。

规格：docs/history/style/style-first-draft-plan-2026-09-12.md。fake LLM；无真实模型。
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
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference.inject.bindings import resolve_binding_layers
from novel_system.services.style_reference.runtime_contract import (
    DRAFT_MODE_NEUTRAL_FIRST,
    DRAFT_MODE_STYLE_FIRST,
    build_style_runtime_contract,
    resolve_draft_mode,
    validate_style_runtime_contract,
)
from tests.real_llm_fakes import install_online_pipeline
from tests.style_reference_inject_helpers import bind_profile as _bind, seed_full as _seed_full


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
        layers = resolve_binding_layers(session, project_id, "scene_generation", character_ids=[], scene_id=scene_id)
        contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
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


def _force_readings(monkeypatch, *, first: str, first_distance: float = 1.5, other_distance: float = 1.0) -> None:
    """风格参考 v3（P5b）：风格步按读数决定——测试里把读数定死：首稿可靠且越界（节奏维），其余文字各给一个 distance。"""
    from novel_system.services.style_reference import readings
    from novel_system.services.style_reference.fidelity import FidelityReading

    def _reading(distance: float, percentile: float, out_of_band: list[dict]) -> FidelityReading:
        return FidelityReading(
            distance=distance,
            percentile=percentile,
            out_of_band=out_of_band,
            dimension_scores={"narrative.pacing": 5.0},
            feature_z={},
            char_count=1200,
            window_count=28,
            reliable=True,
            kernel_version="measure_v1",
            reference_version="ref_test",
        )

    pacing = [
        {
            "feature": "para_len_mean",
            "dimension": "narrative.pacing",
            "z": 3.0,
            "direction": "high",
            "phrase": "段落比作者长，换段太少",
            "value": 150.0,
            "author_typical": 60.0,
        }
    ]

    def fake(_session, policy, text):  # noqa: ANN001
        if not getattr(policy, "bound", False) or not str(text or "").strip():
            return None
        if text == first:
            return _reading(first_distance, 97.0, pacing)
        return _reading(other_distance, 60.0, [])

    monkeypatch.setattr(readings, "reading_for_text", fake)


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
    policy = style_policy_for_bundle(bundle)
    assert policy.draft_mode == DRAFT_MODE_STYLE_FIRST and policy.bound and policy.style_first

    _seed_binding("sfd_c2", project_id="proj_sfd_c2", config_json={"draft_mode": "neutral_first"})
    bundle2 = _frozen_bundle("proj_sfd_c2", "SFD_C2_SC01", "SFD_C2")
    contract2 = json.loads(bundle2["snapshot"]["inline_digests"]["_style_reference_runtime_contract"])
    assert contract2["draft_mode"] == DRAFT_MODE_NEUTRAL_FIRST
    policy2 = style_policy_for_bundle(bundle2)
    assert policy2.draft_mode == DRAFT_MODE_NEUTRAL_FIRST and policy2.bound and not policy2.style_first


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
    old_policy = style_policy_for_bundle(old_bundle)
    assert old_policy.bound and old_policy.draft_mode == DRAFT_MODE_NEUTRAL_FIRST and not old_policy.style_first
    # 无绑定 / 明确 absent → 不让位（neutral_first）
    assert style_policy_for_bundle(None).draft_mode == DRAFT_MODE_NEUTRAL_FIRST
    absent = style_policy_for_bundle({"snapshot": {"source_version_refs": {"style_reference_runtime_contract_status": "absent"}}})
    assert absent.draft_mode == DRAFT_MODE_NEUTRAL_FIRST and not absent.bound and not absent.style_first


def test_apply_request_carries_draft_mode_into_config_json() -> None:
    # v3 直接绑定(P6a):起草方式是绑定配置的一键,不给就不动这条绑定已有的值(新建时取默认 style_first)
    req = ApplyProfileRequest.model_validate(
        {"scope": "project", "scope_ref_id": "p", "config": {"draft_mode": "neutral_first", "sample_windows": 8}}
    )
    assert req.config.as_patch() == {"draft_mode": "neutral_first", "sample_windows": 8}
    assert ApplyProfileRequest.model_validate({"scope": "project", "scope_ref_id": "p"}).config is None
    with pytest.raises(ValueError):
        ApplyProfileRequest.model_validate({"scope": "project", "scope_ref_id": "p", "config": {"draft_mode": "hybrid"}})
    # 旧参数(强度 / 策略)不再收
    with pytest.raises(ValueError):
        ApplyProfileRequest.model_validate({"scope": "project", "scope_ref_id": "p", "intensity": 80})


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
    assert "in the hand of one specific reference author" in call["prompt"]["system_prompt"]
    # 2026-09-22 风格参考优先:样例块落在 user 消息末尾(紧挨输出),system 只留抽象块与一句指路
    assert "- (" not in call["prompt"]["system_prompt"].split("[/STYLE_REFERENCE]")[0]
    assert "参考作者的原文样例在 user 消息的末尾" in call["prompt"]["system_prompt"]
    assert call["user_prompt"].rstrip().endswith("输出仍只返回前文要求的 JSON。")
    tail = call["user_prompt"][call["user_prompt"].find("[风格样例](") :]
    assert "[/风格样例]" in tail and "以上 [风格样例] 是本场唯一的文风权威" in tail
    assert "[UNTRUSTED_REFERENCE_DATA" not in call["user_prompt"]
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
    assert "[/风格样例]" in repair_call["user_prompt"]  # 修复稿同样把样例放在 user 消息末尾
    assert "Rejected First Draft Requiring One Deterministic Repair" in repair_call["user_prompt"]
    assert "keep the reference author's manner" in repair_call["user_prompt"]


def test_style_draft_step_labels_the_source_as_first_draft_under_style_first(session, monkeypatch) -> None:
    """风格参考 v3（P5b，L1）：作者手笔直起时风格步不再「复读」——首稿越界才做定向修改，来源稿仍标成首稿。"""
    _seed_binding("sfd_d4", project_id="proj_sfd_d4")
    scene = _seed_scene(session, project_id="proj_sfd_d4", scene_id="SFD_D4_SC01", chapter_id="SFD_D4")
    bundle = _frozen_bundle("proj_sfd_d4", scene.scene_id, scene.chapter_id)
    runner = _Runner(outputs={}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    # 修改稿与首稿同文：读数没变近 → 保留首稿
    _force_readings(monkeypatch, first=_VOICED_TEXT, other_distance=1.5)
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    call = runner.calls[-1]
    assert call["step"] == "style_draft" and call["prompt"]["template_name"] == "style_targeted_revision"
    assert f"## {sg.FIRST_DRAFT_SOURCE_LABEL}" in call["user_prompt"]
    assert "## Approved Neutral Draft" not in call["user_prompt"]
    assert "## Dimensions To Move Toward The Author" in call["user_prompt"]
    assert refined.content == _VOICED_TEXT
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "style_draft")
    ).scalars().one()
    assert attempt.details_json["content_source"] == "revision_not_closer"
    assert attempt.details_json["style_reference_runtime"]["draft_mode"] == DRAFT_MODE_STYLE_FIRST
    assert attempt.details_json["style_reference_runtime"]["role"] == "revise"


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


def test_refine_fallback_returns_to_the_first_draft_and_the_first_draft_anchors_voice(session, monkeypatch) -> None:
    """定向修改丢了必写项（没过确定性安全门）→ 保留首稿；首稿仍可作前文声音锚。"""
    _seed_binding("sfd_d6", project_id="proj_sfd_d6")
    scene = _seed_scene(session, project_id="proj_sfd_d6", scene_id="SFD_D6_SC01", chapter_id="SFD_D6")
    bundle = _frozen_bundle("proj_sfd_d6", scene.scene_id, scene.chapter_id)
    lost_required = "脚步在门外停了；他把那个东西搁到桌上——也不说话。她没有去接。"
    runner = _Runner(outputs={"style_draft": lost_required}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _force_readings(monkeypatch, first=_VOICED_TEXT, other_distance=0.5)
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    codes = [item["code"] for item in refined.notices]
    assert sg.STYLE_NOTICE_REVISION_REJECTED in codes and sg.STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL not in codes
    rejected = next(item for item in refined.notices if item["code"] == sg.STYLE_NOTICE_REVISION_REJECTED)
    assert rejected["reason"] == "base_safety_failed" and "首稿" in rejected["message"]
    assert refined.content == _VOICED_TEXT
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "style_draft")
    ).scalars().one()
    assert attempt.details_json["content_source"] == "revision_not_closer"
    assert attempt.details_json["style_reference_runtime"]["generation_outcome"] == "revision_not_closer"
    # 声音锚:风格稿行的正文就是首稿(目标文风),不再按「与中性稿相同即回退」排除
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


def test_house_taste_gate_is_recorded_but_never_rewrites_under_style_first(session, monkeypatch) -> None:
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
    # 风格参考 v3：定向修改稿更像作者（读数变近）→ 采用；房风门照样让位、不触发去模板
    _force_readings(monkeypatch, first=_VOICED_TEXT, other_distance=1.0)
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
    # 门裁决随产品回调进检查点:风格步让位于参考,不跑房风门(v3:只记风格步的决定)
    gate_decision = products[-1]["gate_decision"]
    assert gate_decision["house_taste_gate"] == "deferred_to_reference"
    assert gate_decision["triggered"] is False
    assert gate_decision["style_step"]["decision"] == "revision_kept"
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


def test_final_text_gate_literary_thresholds_defer_under_style_bound(session, monkeypatch) -> None:
    from novel_system.db.models import SceneBundle
    from novel_system.services.final_text_gate import FinalTextGateService

    # 风格参考 v3（V11）：有参考书校准时按校准判（见 test_style_reference_v3_pipeline）；这里守的是校准不可用时
    # 作者手笔直起仍整体让位的那条退路
    monkeypatch.setattr(FinalTextGateService, "_rule_calibration", lambda self, policy: None)

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
    # 风格参考 v3：没有 bundle 时按当前活动绑定现解析（仍是作者手笔直起 → 让位）；撤掉绑定才施加房风阈值
    state.current_bundle_id = None
    session.commit()
    assert gate._literary(scene, text)["house_taste_thresholds"] == "deferred_to_reference"
    from novel_system.db.models import StyleReferenceInjectionBinding

    for binding in session.query(StyleReferenceInjectionBinding).all():
        binding.status = "archived"
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
    assert templates["style_first_draft"]["input_token_budget"] == 96000
    assert "The bundle decides what happens" in templates["style_first_draft"]["system_prompt"]
    # 风格参考 v3（L5「全学」）：设计只是情节框架，气质照参考走；蓝图只给事实，只为中和写法字段的条款删了
    assert "is the plot framework only" in templates["style_first_draft"]["system_prompt"]
    assert "Every item the style card marks 必须 (must) shows up in this scene" in templates["style_first_draft"]["task_prompt"]
    assert "It never supplies a line to write" in templates["style_first_draft"]["task_prompt"]
    for gone in ("the author's manner wins and the fact of the field stays", "image_anchor", "anti-summary rule"):
        assert gone not in templates["style_first_draft"]["task_prompt"], gone
    # 2026-09-22 风格参考优先:样例在 user 消息末尾;幽灵标签 [禁止复刻] 不再出现在任何模板里
    for name in ("style_first_draft", "style_draft"):
        assert "[风格样例] block at the end of the user message" in templates[name]["system_prompt"]
        assert "[禁止复刻]" not in templates[name]["system_prompt"] + templates[name]["task_prompt"]
        assert "even when the reference author uses another" not in templates[name]["task_prompt"]
    assert "[禁止复刻]" not in templates["soft_qc"]["task_prompt"]
    # 风格参考 v3（V7）：分数一律 0–10，代码统一换算
    assert "numbers from 0 to 10" in templates["soft_qc"]["task_prompt"]
    assert "First Draft already in the reference author's hand" in templates["style_draft"]["task_prompt"]
    # 2026-09-14 保真修补:系统提示不再与长度指导矛盾(作者尺度优先),偏好画像只在不冲突时服从
    assert "never imitate their length" not in templates["style_draft"]["system_prompt"]
    assert "the reference author's own scale wins" in templates["style_draft"]["system_prompt"]
    assert "obey it only where it does not conflict with the reference author's manner" in templates["style_draft"]["task_prompt"]
    assert "Never wash the voice back toward a neutral register" in templates["style_draft"]["task_prompt"]
    assert "never a hard violation" in templates["hard_qc"]["task_prompt"]
    assert "restate paragraph 3" not in templates["hard_qc"]["task_prompt"]
    # 风格参考 v3（L2）：有风格块时软 QC 是参考评审，不带房风规则；按 16 维打分
    assert "Do not bring a house rubric of your own" in templates["soft_qc"]["system_prompt"]
    assert "dimension_scores" in templates["soft_qc"]["task_prompt"]
    assert "Emotional clarity is a goal only when no [STYLE_REFERENCE] block is present" in templates["soft_qc"]["system_prompt"]
    assert "If no [STYLE_REFERENCE] block is present, do not pass scenes" in templates["near_final_acceptance_review"]["task_prompt"]


# ---------------------------------------------------------------------------
# 决策卡 effect 与运行任务视图
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 风格参考 v3 S2（2026-09-24）· v2 指标包络退役：neutral_first 也按读数「永不越改越远」；候选 style_score 由读数给、
# 没检查成的候选不进盲选；改写不退步只看读数（patch_max_distance_increase）
# ---------------------------------------------------------------------------

_NEUTRAL_MARK = "对面的人先开口"  # 只在 _NEUTRAL_TEXT 里
_VOICED_MARK = "她终于没有去接"  # 在 _VOICED_TEXT / _HOUSE_TASTE_TEXT 里
_CLEANED_TEXT = "脚步在门外停了；他将信封搁到桌上——也不说话，只等着。她没有去接，只把手收回袖子里。"
_CLEANED_MARK = "收回袖子里"


def _fixed_reading(distance: float, percentile: float, *, reliable: bool = True):
    from novel_system.services.style_reference.fidelity import FidelityReading

    return FidelityReading(
        distance=distance,
        percentile=percentile,
        out_of_band=[],
        dimension_scores={},
        feature_z={},
        char_count=900,
        window_count=28,
        reliable=reliable,
        kernel_version="measure_v1",
        reference_version="ref_test",
    )


def _install_readings(monkeypatch, table: dict[str, object]) -> None:
    """``readings.reading_for_text`` 的替身：文字含哪个记号就给哪个读数（未绑定照旧 None；值是异常类则抛出）。"""
    from novel_system.services.style_reference import readings

    def fake(_session, policy, text):  # noqa: ANN001
        if policy is None or not getattr(policy, "bound", False) or not str(text or "").strip():
            return None
        for marker, reading in table.items():
            if marker in str(text):
                if isinstance(reading, type) and issubclass(reading, Exception):
                    raise reading("reading blew up")
                return reading
        return None

    monkeypatch.setattr(readings, "reading_for_text", fake)


def _neutral_first_scene(session, seed: str):
    _seed_binding(seed, project_id=f"proj_{seed}", config_json={"draft_mode": "neutral_first"})
    scene = _seed_scene(session, project_id=f"proj_{seed}", scene_id=f"{seed.upper()}_SC01", chapter_id=seed.upper())
    return scene, _frozen_bundle(f"proj_{seed}", scene.scene_id, scene.chapter_id)


def _style_attempt(session, scene_id: str) -> AttemptTracker:
    return session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene_id, AttemptTracker.step == "style_draft")
    ).scalars().one()


def _readings_by_stage(session, scene_id: str) -> dict[tuple[str, str], float]:
    from novel_system.db.models import StyleFidelityReading

    rows = session.scalars(select(StyleFidelityReading).where(StyleFidelityReading.scene_id == scene_id)).all()
    return {(row.stage, row.draft_ref): row.distance for row in rows}


def test_neutral_first_keeps_the_style_draft_when_it_reads_closer(session, monkeypatch) -> None:
    scene, bundle = _neutral_first_scene(session, "sfd_s2a")
    runner = _Runner(outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": _VOICED_TEXT}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(monkeypatch, {_NEUTRAL_MARK: _fixed_reading(1.5, 97.0), _VOICED_MARK: _fixed_reading(1.0, 60.0)})
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    assert refined.content == _VOICED_TEXT
    assert refined.style_step["decision"] == "revision_kept" and refined.style_step["reason"] == "closer_to_author"
    assert refined.style_step["draft_mode"] == DRAFT_MODE_NEUTRAL_FIRST
    assert sg.STYLE_NOTICE_REVISION_REJECTED not in [item["code"] for item in refined.notices]
    attempt = _style_attempt(session, scene.scene_id)
    assert attempt.details_json["content_source"] == "provider_style_output"
    assert attempt.details_json["style_step"]["first_reading"]["distance"] == 1.5
    assert attempt.details_json["style_step"]["revision_reading"]["distance"] == 1.0
    assert attempt.details_json["not_closer_rejected_row_id"] is None
    assert attempt.details_json["style_reference_runtime"]["generation_outcome"] == "provider_style_output"
    # 两条读数：中性稿 first_draft（按中性稿行）、风格稿 revision（按风格稿行）
    assert _readings_by_stage(session, scene.scene_id) == {
        ("first_draft", first.row_id): 1.5,
        ("revision", refined.row_id): 1.0,
    }


def test_neutral_first_delivers_the_neutral_draft_when_the_style_draft_is_not_closer(session, monkeypatch) -> None:
    scene, bundle = _neutral_first_scene(session, "sfd_s2b")
    runner = _Runner(outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": _VOICED_TEXT}, default=_VOICED_TEXT)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    # 风格稿比中性稿远（1.2 > 1.0）：不更像 → 交付中性稿（与作者手笔直起同一条「永不越改越远」规则）
    _install_readings(monkeypatch, {_NEUTRAL_MARK: _fixed_reading(1.0, 60.0), _VOICED_MARK: _fixed_reading(1.2, 80.0)})
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    assert refined.content == _NEUTRAL_TEXT
    assert session.get(SceneDraft, refined.row_id).content == _NEUTRAL_TEXT
    rejected = next(item for item in refined.notices if item["code"] == sg.STYLE_NOTICE_REVISION_REJECTED)
    assert rejected["reason"] == "not_closer" and "中性稿" in rejected["message"]
    assert rejected["draft_mode"] == DRAFT_MODE_NEUTRAL_FIRST
    assert rejected["first_distance"] == 1.0 and rejected["revision_distance"] == 1.2
    rejected_row = session.get(SceneDraft, rejected["rejected_candidate_row_id"])
    assert rejected_row.stage == "style_rejected" and rejected_row.status == "rejected" and rejected_row.content == _VOICED_TEXT
    attempt = _style_attempt(session, scene.scene_id)
    assert attempt.details_json["content_source"] == "revision_not_closer"
    assert attempt.details_json["not_closer_rejected_row_id"] == rejected_row.row_id
    assert attempt.details_json["rejected_candidate_row_id"] is None  # 不是安全门回退：不进安全修复通道
    assert attempt.details_json["style_step"]["decision"] == "revision_rejected"
    assert attempt.details_json["style_reference_runtime"]["generation_outcome"] == "revision_not_closer"
    assert refined.style_step["reason"] == "not_closer"
    # 风格稿的 revision 读数按被退回的那一行记
    assert _readings_by_stage(session, scene.scene_id) == {
        ("first_draft", first.row_id): 1.0,
        ("revision", rejected_row.row_id): 1.2,
    }
    # 差得不到 revision_min_improvement 也算「不更像」（阈值与作者手笔直起同一组）
    thresholds = sg.style_step.fidelity_thresholds()
    scene2, bundle2 = _neutral_first_scene(session, "sfd_s2b2")
    runner2 = _Runner(outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": _VOICED_TEXT}, default=_VOICED_TEXT)
    service2 = SceneGenerationService(session, llm_runner=runner2)
    first2 = service2.generate_neutral_draft(scene2.scene_id, bundle2)
    session.commit()
    _install_readings(
        monkeypatch,
        {
            _NEUTRAL_MARK: _fixed_reading(1.0, 60.0),
            _VOICED_MARK: _fixed_reading(1.0 - thresholds.revision_min_improvement / 2, 58.0),
        },
    )
    refined2 = service2.generate_style_draft(
        scene2.scene_id, bundle2, neutral_draft_row_id=first2.row_id, neutral_content=first2.content
    )
    assert refined2.content == _NEUTRAL_TEXT and refined2.style_step["reason"] == "not_closer"


def test_neutral_first_accepts_the_style_draft_when_readings_are_unreliable_or_fail(session, monkeypatch) -> None:
    # 不可信（太短 / 窗口太少）→ 照旧接受风格稿，段落原样交付（旧的段落形态整理已删）
    scene, bundle = _neutral_first_scene(session, "sfd_s2c")
    two_paragraphs = _VOICED_TEXT + "\n\n她把手收回袖子里。"
    runner = _Runner(outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": two_paragraphs}, default=two_paragraphs)
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    _install_readings(
        monkeypatch,
        {_NEUTRAL_MARK: _fixed_reading(1.0, 60.0), _VOICED_MARK: _fixed_reading(1.9, 99.0, reliable=False)},
    )
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    assert refined.content == two_paragraphs
    assert refined.style_step["decision"] == "revision_kept" and refined.style_step["reason"] == "reading_unreliable"
    assert sg.STYLE_NOTICE_REVISION_REJECTED not in [item["code"] for item in refined.notices]
    attempt = _style_attempt(session, scene.scene_id)
    assert attempt.details_json["content_source"] == "provider_style_output"
    assert "paragraph_shape_normalization" not in attempt.details_json
    # 读数出错（不是「书没有尺子」）→ 同样接受风格稿，原因单独说
    scene2, bundle2 = _neutral_first_scene(session, "sfd_s2c2")
    runner2 = _Runner(outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": _VOICED_TEXT}, default=_VOICED_TEXT)
    service2 = SceneGenerationService(session, llm_runner=runner2)
    first2 = service2.generate_neutral_draft(scene2.scene_id, bundle2)
    session.commit()
    _install_readings(monkeypatch, {_NEUTRAL_MARK: RuntimeError, _VOICED_MARK: _fixed_reading(1.0, 60.0)})
    refined2 = service2.generate_style_draft(
        scene2.scene_id, bundle2, neutral_draft_row_id=first2.row_id, neutral_content=first2.content
    )
    session.commit()
    assert refined2.content == _VOICED_TEXT
    assert refined2.style_step["decision"] == "revision_kept" and refined2.style_step["reason"] == "reading_failed"
    assert refined2.style_step["first_reading"] is None and refined2.style_step["revision_reading"]["distance"] == 1.0
    assert _readings_by_stage(session, scene2.scene_id) == {("revision", refined2.row_id): 1.0}
    # 读不出（书没有可用的尺子）→ 接受，原因 reading_unavailable
    scene3, bundle3 = _neutral_first_scene(session, "sfd_s2c3")
    runner3 = _Runner(outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": _VOICED_TEXT}, default=_VOICED_TEXT)
    service3 = SceneGenerationService(session, llm_runner=runner3)
    first3 = service3.generate_neutral_draft(scene3.scene_id, bundle3)
    session.commit()
    _install_readings(monkeypatch, {})
    refined3 = service3.generate_style_draft(
        scene3.scene_id, bundle3, neutral_draft_row_id=first3.row_id, neutral_content=first3.content
    )
    assert refined3.content == _VOICED_TEXT and refined3.style_step["reason"] == "reading_unavailable"


def test_neutral_first_candidate_style_score_comes_from_readings_and_unchecked_candidates_are_not_offered(
    session, monkeypatch
) -> None:
    from novel_system.services.orchestrator import Orchestrator

    scene, bundle = _neutral_first_scene(session, "sfd_s2d")
    service = SceneGenerationService(session, llm_runner=_Runner(outputs={}, default=_VOICED_TEXT))
    result = sg.StyleGenerationResult(
        row_id="cand_a",
        content=_VOICED_TEXT,
        llm_call_id="llm_a",
        bundle_id=bundle["bundle_id"],
        bundle_hash=bundle["bundle_snapshot_hash"],
    )
    # 读数可信：style_score = 1 − percentile/100（四位小数）；抄袭门查过、没重合
    _install_readings(monkeypatch, {_VOICED_MARK: _fixed_reading(1.0, 30.0)})
    audit = service._candidate_style_assessment(bundle, result, 0.5, rank=0)
    assert audit["style_score"] == 0.7 and audit["fidelity_distance"] == 1.0 and audit["fidelity_percentile"] == 30.0
    assert audit["plagiarism_checked"] is True and audit["plagiarism_passed"] is True and audit["plagiarism_hit_count"] == 0
    assert audit["rank"] == 0 and audit["selected"] is True and audit["selection_reason"] == "quality_order"
    assert audit["quality_score"] == 0.5
    assert audit["rerank"] == {"applied_mode": "off", "reason": None, "runtime_contract_mode": "frozen"}
    # 读数不可信 → style_score None（抄袭门照查）
    _install_readings(monkeypatch, {_VOICED_MARK: _fixed_reading(1.0, 30.0, reliable=False)})
    unreliable = service._candidate_style_assessment(bundle, result, 0.5, rank=1)
    assert unreliable["style_score"] is None and unreliable["plagiarism_checked"] is True
    assert unreliable["rerank"]["reason"] == "reading_unreliable" and unreliable["selected"] is False
    # 读数抛异常 → 没检查成：plagiarism_checked=False / plagiarism_passed=None，候选照常交付
    _install_readings(monkeypatch, {_VOICED_MARK: RuntimeError})
    unchecked = service._candidate_style_assessment(bundle, result, 0.5, rank=1)
    assert unchecked["plagiarism_checked"] is False and unchecked["plagiarism_passed"] is None
    assert unchecked["style_score"] is None and unchecked["rerank"]["reason"] == "assessment_internal_error"
    assert unchecked["rerank"]["error_code"] == "RuntimeError"
    # 未绑定的 bundle：不读、不查（照旧）
    unbound = service._candidate_style_assessment({"snapshot": {}}, result, 0.5, rank=0)
    assert unbound["style_score"] is None and unbound["plagiarism_checked"] is False and unbound["plagiarism_passed"] is None
    # 终选门：有绑定时没检查成的候选不交给盲选；全部没检查成 → None（管线继续）；未绑定照旧交付
    state = session.get(SceneRunState, scene.scene_id)
    checked = SimpleNamespace(row_id="cand_ok", content=_NEUTRAL_TEXT, ranking_audit={**audit, "row_id": "cand_ok"})
    unchecked_cand = SimpleNamespace(row_id="cand_unchecked", content=_VOICED_TEXT, ranking_audit=unchecked)
    orchestrator = Orchestrator(session)
    assert orchestrator._offer_candidates_for_selection(scene, state, bundle, [unchecked_cand, checked]) == ["cand_ok"]
    assert orchestrator._offer_candidates_for_selection(scene, state, bundle, [unchecked_cand]) is None
    assert orchestrator._offer_candidates_for_selection(scene, state, {"snapshot": {}}, [unchecked_cand]) == [
        "cand_unchecked"
    ]


def test_style_rewrite_drift_reads_both_texts_and_flags_a_measurable_regression(session, monkeypatch) -> None:
    _seed_binding("sfd_s2e", project_id="proj_sfd_s2e", config_json={"draft_mode": "neutral_first"})
    bundle = _frozen_bundle("proj_sfd_s2e", "SFD_S2E_SC01", "SFD_S2E")
    thresholds = sg.style_step.fidelity_thresholds()
    step = float(thresholds.patch_max_distance_increase)

    def drift(policy_or_bundle, source_d, rewritten_d, *, rewritten_reliable: bool = True):
        _install_readings(
            monkeypatch,
            {
                _NEUTRAL_MARK: _fixed_reading(source_d, 60.0),
                _VOICED_MARK: _fixed_reading(rewritten_d, 70.0, reliable=rewritten_reliable),
            },
        )
        return sg._assess_style_rewrite_drift(
            session, policy_or_bundle=policy_or_bundle, source_content=_NEUTRAL_TEXT, rewritten_content=_VOICED_TEXT
        )

    regressed = drift(bundle, 1.0, 1.0 + step + 0.01)
    assert regressed["version"] == "style_rewrite_drift_v1"
    assert regressed["available"] is True and regressed["comparable"] is True and regressed["regressed"] is True
    assert regressed["source"]["distance"] == 1.0 and regressed["rewritten"]["distance"] == pytest.approx(1.0 + step + 0.01)
    assert regressed["max_distance_increase"] == step and regressed["distance_delta"] == pytest.approx(step + 0.01)
    assert "unavailable_reason" not in regressed
    # 远得不到 patch_max_distance_increase → 不算退步；也收 StylePolicy
    fine = drift(style_policy_for_bundle(bundle), 1.0, 1.0 + step - 0.01)
    assert fine["comparable"] is True and fine["regressed"] is False
    # 改写稿更像 → 不算退步
    assert drift(bundle, 1.0, 0.5)["regressed"] is False
    # 一边不可信 → 不可比、不算退步
    unreliable = drift(bundle, 1.0, 3.0, rewritten_reliable=False)
    assert unreliable["available"] is True and unreliable["comparable"] is False and unreliable["regressed"] is False
    assert unreliable["unavailable_reason"] == "reading_unreliable"
    # 读数出错 → 不可比
    _install_readings(monkeypatch, {_NEUTRAL_MARK: RuntimeError, _VOICED_MARK: _fixed_reading(1.0, 60.0)})
    failed = sg._assess_style_rewrite_drift(
        session, policy_or_bundle=bundle, source_content=_NEUTRAL_TEXT, rewritten_content=_VOICED_TEXT
    )
    assert failed["available"] is False and failed["comparable"] is False and failed["regressed"] is False
    assert failed["unavailable_reason"] == "reading_failed" and failed["source"] is None
    # 未绑定 → 不可比（去模板不拒、挽救补丁不采用——消费方语义不变）
    unbound = sg._assess_style_rewrite_drift(
        session, policy_or_bundle={"snapshot": {}}, source_content=_NEUTRAL_TEXT, rewritten_content=_VOICED_TEXT
    )
    assert unbound["available"] is False and unbound["comparable"] is False and unbound["regressed"] is False
    assert unbound["unavailable_reason"] in {"bundle_has_no_style_profile", "style_policy_unbound"}
    # 同一段文字只读一次
    calls: list[str] = []
    from novel_system.services.style_reference import readings

    original = readings.reading_for_text

    def counting(session_, policy, text):  # noqa: ANN001
        calls.append(text)
        return original(session_, policy, text)

    _install_readings(monkeypatch, {_NEUTRAL_MARK: _fixed_reading(1.0, 60.0)})
    original = readings.reading_for_text
    monkeypatch.setattr(readings, "reading_for_text", counting)
    same = sg._assess_style_rewrite_drift(
        session, policy_or_bundle=bundle, source_content=_NEUTRAL_TEXT, rewritten_content=_NEUTRAL_TEXT
    )
    assert len(calls) == 1 and same["regressed"] is False and same["comparable"] is True


def test_de_template_rewrite_is_rejected_when_it_reads_farther_from_the_author(session, monkeypatch) -> None:
    """去模板改写按读数拒：改写稿比来源稿远出 patch_max_distance_increase → style_conformance_regressed，基稿保留。"""
    scene, bundle = _neutral_first_scene(session, "sfd_s2f")
    runner = _Runner(
        outputs={"neutral_draft": _NEUTRAL_TEXT, "style_draft": _HOUSE_TASTE_TEXT, "de_template": _CLEANED_TEXT},
        default=_CLEANED_TEXT,
    )
    service = SceneGenerationService(session, llm_runner=runner)
    first = service.generate_neutral_draft(scene.scene_id, bundle)
    session.commit()
    # 风格稿比中性稿像（交付）；房风门触发去模板；去模板稿比风格稿远 0.3 → 拒
    _install_readings(
        monkeypatch,
        {
            _NEUTRAL_MARK: _fixed_reading(1.5, 97.0),
            _CLEANED_MARK: _fixed_reading(1.3, 85.0),
            _VOICED_MARK: _fixed_reading(1.0, 60.0),
        },
    )
    refined = service.generate_style_draft(
        scene.scene_id, bundle, neutral_draft_row_id=first.row_id, neutral_content=first.content
    )
    session.commit()
    steps = [str(call["step"]) for call in runner.calls]
    assert "de_template" in steps, steps
    assert refined.content == _HOUSE_TASTE_TEXT
    de_template = session.execute(
        select(AttemptTracker).where(AttemptTracker.scene_id == scene.scene_id, AttemptTracker.step == "de_template")
    ).scalars().one()
    acceptance = de_template.details_json["acceptance"]
    assert acceptance["accepted"] is False
    assert "style_conformance_regressed" in acceptance["reasons"]
    assert acceptance["style_non_regression_enforced"] is True
    assert acceptance["style_conformance"]["regressed"] is True
    assert acceptance["style_conformance"]["source"]["distance"] == 1.0
    assert acceptance["style_conformance"]["rewritten"]["distance"] == 1.3
    assert "paragraph_shape_normalization" not in de_template.details_json
