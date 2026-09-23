"""风格参考 v3 · P5a 管线守卫（契约 docs/style-reference-v3-2026-09-23.md §2.4 / §4）。

- V8：一份 StylePolicy（轻量现解析的选层规则；场景诊断的「房风」标记只在让位时打）；
- L3 / N4：有绑定且作者手笔直起时场景蓝图只写事实 + 场面标签，版式不符的旧蓝图不复用；
- L2 / V7：软 QC 是参考评审——按 16 维打 0–10 的分，分数先换算再校验、落库；准定稿评分带范围、不再饱和；
- L4：评审节点只拿 4 窗样例；
- L8：让位时新鲜度预算只留逐字层；一个 bundle 只建一次契约；
- V11：成稿门在绑定下按参考书校准的规则判。

全部是合成文本。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    FinalScene,
    QcReport,
    SceneBlueprint,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceInjectionBinding,
)
from novel_system.services.review_scores import REVIEW_FEW_SHOT_K_CAP, score_scale, to_unit
from novel_system.services.style_policy import StylePolicy, style_policy_live
from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from tests.reference_copy_fixtures import seed_bound_reference

PROJECT_ID = "P_V3_PIPE"
CHAPTER_ID = "P_V3_PIPE_CH01"
SCENE_ID = "P_V3_PIPE_CH01_SC02"
PREVIOUS_SCENE_ID = "P_V3_PIPE_CH01_SC01"


def _seed_scene(session, *, pov: str = "A", onstage: list[str] | None = None) -> SceneCard:
    session.add(StoryProject(project_id=PROJECT_ID, title="v3 管线", outline_text="", planning_mode="snowflake"))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=2,
            chapter_goal="码头的交易必须变成一个选择。",
            display_order=1,
        )
    )
    for seq, scene_id in ((1, PREVIOUS_SCENE_ID), (2, SCENE_ID)):
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id=CHAPTER_ID,
                project_id=PROJECT_ID,
                scene_seq=seq,
                pov_character_id=pov,
                onstage_chars_json=onstage if onstage is not None else [pov],
                scene_goal="拿到仓库的钥匙，并决定要不要相信船老大。",
                beats_json=["追问钥匙", "船老大岔开话题", "主角决定自己去找"],
                exit_change="船老大成了嫌疑人。",
                target_length_band="1200-1800",
            )
        )
        session.add(SceneRunState(scene_id=scene_id))
    session.commit()
    return session.get(SceneCard, SCENE_ID)


def _bind(session, *, draft_mode: str = "style_first", scope: str = "project", scope_ref_id: str = PROJECT_ID, seed: str = "pipe"):
    return seed_bound_reference(
        session,
        seed=seed,
        scope=scope,
        scope_ref_id=scope_ref_id,
        config_json={"draft_mode": draft_mode},
    )


# ---------------------------------------------------------------------------
# V8 · StylePolicy：轻量现解析的选层规则
# ---------------------------------------------------------------------------


def test_light_live_policy_picks_the_most_specific_active_binding(session) -> None:
    scene = _seed_scene(session, pov="A", onstage=["A", "B"])
    assert style_policy_live(session, scene, freeze_contract=False).bound is False

    project = _bind(session, seed="light_project", draft_mode="neutral_first")
    light = style_policy_live(session, scene, freeze_contract=False)
    assert light.bound and light.mode == "live" and light.contract is None
    assert light.binding_id == project["binding_id"] and light.book_id == project["book_id"]
    assert light.style_first is False and light.defers_house_taste() is False

    # 角色层比项目层具体；POV 优先于其他在场角色
    onstage = _bind(session, seed="light_onstage", scope="character", scope_ref_id="B")
    pov = _bind(session, seed="light_pov", scope="character", scope_ref_id="A")
    assert style_policy_live(session, scene, freeze_contract=False).binding_id == pov["binding_id"]
    session.execute(
        StyleReferenceInjectionBinding.__table__.update()
        .where(StyleReferenceInjectionBinding.binding_id == pov["binding_id"])
        .values(status="archived")
    )
    session.commit()
    assert style_policy_live(session, scene, freeze_contract=False).binding_id == onstage["binding_id"]

    # 场景层最具体；绑定作者手笔直起 → 让位
    scene_level = _bind(session, seed="light_scene", scope="scene", scope_ref_id=SCENE_ID)
    policy = style_policy_live(session, scene, freeze_contract=False)
    assert policy.binding_id == scene_level["binding_id"] and policy.defers_house_taste() is True

    # 冻结契约的完整现解析与轻量版对「绑没绑、让不让位」给出同一结论
    frozen = style_policy_live(session, scene)
    assert frozen.bound and frozen.contract is not None and frozen.defers_house_taste() is True


def test_scene_diagnosis_marks_house_taste_only_when_the_policy_defers(session) -> None:
    from novel_system.services.scene_diagnosis import SceneDiagnosisService

    scene = _seed_scene(session)
    _bind(session, draft_mode="neutral_first", seed="diag_neutral")
    service = SceneDiagnosisService(session)
    # 先中性后润色的绑定：参考书照样校准（bound），但房风不让位
    assert service.style_bound(scene) is True
    assert service.style_policy(scene).defers_house_taste() is False
    payload = service.diagnose_scene(scene)
    assert payload["style_bound"] is True and payload["house_taste_deferred"] is False
    assert not any(item.get("house_taste") for item in payload["findings"])


# ---------------------------------------------------------------------------
# L3 / N4 · 事实版场景蓝图
# ---------------------------------------------------------------------------


class _FactsRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def provider_execution_mode(self) -> str:
        return "online"

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        output = {
            "visible_desire": "拿到仓库钥匙",
            "forced_choice": "信船老大，或者自己闯仓库",
            "price_paid": "和船老大的交情断了",
            "information_release": "钥匙三天前就被人拿走了",
            "relationship_turn": "信任变成戒备",
            "ending_function": "挫折落地：钥匙没了，原计划作废",
            "next_scene_pull": "谁先拿走了钥匙",
            "situation_tags": ["对峙审问", "不存在的标签", "对峙审问", "悬疑揭示", "计划商议", "独处内省"],
            # 模型多给的写法字段：事实版一律不收
            "image_anchor": "湿漉漉的石阶上映着灯",
            "ending_action": "「你在替他圆谎？」",
            "anti_summary_rule": "不许总结式收尾",
        }
        if kwargs["prompt"].get("template_name") != "scene_blueprint_facts":
            output = {
                key: value
                for key, value in output.items()
                if key not in {"ending_function", "situation_tags"}
            }
        return SimpleNamespace(
            llm_call_id=f"llm_call_v3_facts_{len(self.calls)}",
            response=SimpleNamespace(structured_output=output),
        )


def test_style_first_binding_gets_a_facts_only_blueprint_with_situation_tags(session) -> None:
    from novel_system.services.scene_blueprint import (
        SCENE_BLUEPRINT_FACT_FIELDS,
        SceneBlueprintService,
        blueprint_situation_tags,
        is_facts_blueprint,
    )

    _seed_scene(session)
    _bind(session, draft_mode="style_first", seed="facts")
    runner = _FactsRunner()
    service = SceneBlueprintService(session, llm_runner=runner)

    row = service.generate(SCENE_ID)

    call = runner.calls[-1]
    assert call["prompt"]["template_name"] == "scene_blueprint_facts"
    assert call["node_id"] == "scene_blueprint"
    assert "State facts only" in call["prompt"]["system_prompt"]
    assert "Produce the scene's fact sheet" in call["user_prompt"]
    assert "anti-summary rule" not in call["user_prompt"]
    blueprint = row.blueprint_json
    assert set(blueprint) == {*SCENE_BLUEPRINT_FACT_FIELDS, "situation_tags"}
    assert is_facts_blueprint(blueprint)
    assert blueprint_situation_tags(blueprint) == ["对峙审问", "悬疑揭示", "计划商议"]
    assert "湿漉漉" not in str(blueprint) and "圆谎" not in str(blueprint)

    # 场面标签随 bundle 冻结（同一场所有工序读同一份；以 _ 开头，不进 section）
    import json

    from novel_system.services.bundle_builder import SCENE_SITUATION_TAGS_KEY, BundleBuilder

    snapshot = BundleBuilder(session).build(SCENE_ID)["snapshot"]
    assert json.loads(snapshot["inline_digests"][SCENE_SITUATION_TAGS_KEY]) == ["对峙审问", "悬疑揭示", "计划商议"]
    assert snapshot["source_version_refs"]["scene_situation_tags"] == ["对峙审问", "悬疑揭示", "计划商议"]


def test_blueprint_mode_follows_the_policy_and_stale_versions_are_regenerated(session) -> None:
    from novel_system.services.scene_blueprint import SceneBlueprintService, is_facts_blueprint

    _seed_scene(session)
    # v3 之前在绑定下生成的写法版蓝图（预写结尾台词、指定意象）
    session.add(
        SceneBlueprint(
            row_id="scene_blueprint_v3_legacy",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            blueprint_json={"visible_desire": "旧", "ending_action": "「你在替他圆谎？」", "image_anchor": "灯"},
            status="accepted",
        )
    )
    session.commit()
    binding = _bind(session, draft_mode="style_first", seed="regen")
    runner = _FactsRunner()
    service = SceneBlueprintService(session, llm_runner=runner)

    assert service.reusable(SCENE_ID) is None
    fresh = service.ensure_for_scene(SCENE_ID)
    assert len(runner.calls) == 1 and is_facts_blueprint(fresh.blueprint_json)
    assert session.get(SceneBlueprint, "scene_blueprint_v3_legacy").status == "superseded"
    assert service.ensure_for_scene(SCENE_ID).row_id == fresh.row_id
    assert len(runner.calls) == 1, "版式相符的蓝图直接复用"

    # 改成先中性后润色：事实版不再相符，重生成写法版
    session.execute(
        StyleReferenceInjectionBinding.__table__.update()
        .where(StyleReferenceInjectionBinding.binding_id == binding["binding_id"])
        .values(config_json={"draft_mode": "neutral_first"})
    )
    session.commit()
    regenerated = service.ensure_for_scene(SCENE_ID)
    assert runner.calls[-1]["prompt"]["template_name"] == "scene_blueprint"
    assert not is_facts_blueprint(regenerated.blueprint_json)
    assert regenerated.blueprint_json["ending_action"] == "「你在替他圆谎？」"


def test_facts_blueprint_validation() -> None:
    from novel_system.services.errors import DomainError
    from novel_system.services.scene_blueprint import (
        SCENE_BLUEPRINT_FACT_FIELDS,
        _validate_facts_blueprint_payload,
    )

    payload = {field: f"{field} 事实" for field in SCENE_BLUEPRINT_FACT_FIELDS}
    payload["forced_choice"] = "无"
    payload["situation_tags"] = "不是列表"
    normalized = _validate_facts_blueprint_payload(payload)
    assert normalized["forced_choice"] == "无" and normalized["situation_tags"] == []
    with pytest.raises(DomainError):
        _validate_facts_blueprint_payload({**payload, "visible_desire": "无"})
    with pytest.raises(DomainError):
        _validate_facts_blueprint_payload({key: value for key, value in payload.items() if key != "ending_function"})


# ---------------------------------------------------------------------------
# L2 / V7 / L4 · 软 QC 参考评审
# ---------------------------------------------------------------------------


class _JudgeRunner:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls: list[dict] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(
            llm_call_id=f"llm_call_v3_judge_{len(self.calls)}",
            response=SimpleNamespace(structured_output=dict(self.output)),
        )


def _judge_output(**overrides) -> dict:
    output = {
        "resolution_code": "soft_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
        "style_score": 8.2,
        "dimension_scores": {"language.rhetoric": 6.5, "theme.emotional_tone": 9, "bogus.dimension": 3},
    }
    output.update(overrides)
    return output


def test_soft_qc_reference_judge_scores_are_validated_rescaled_and_persisted(session, monkeypatch) -> None:
    from tests.test_qc_engine_style_validation_gate import (
        CLEAN_TEXT,
        REFERENCE_PARAGRAPH,
        _run_soft_qc,
        _seed_soft_scene,
        _seed_style_binding,
    )

    captured: list[dict] = []

    def fake_inject(session_arg, prompt, scene, bundle=None, **kwargs):  # noqa: ANN001, ANN003
        captured.append(kwargs)
        return prompt

    monkeypatch.setattr("novel_system.services.style_prompt_injection.inject_style_reference_prefix", fake_inject)
    _seed_style_binding(project_id="proj_v3_judge", seed="v3_judge", paragraphs=[REFERENCE_PARAGRAPH])
    _seed_soft_scene(session, project_id="proj_v3_judge", draft_content=CLEAN_TEXT)
    decision = _run_soft_qc(session, _JudgeRunner(_judge_output()), CLEAN_TEXT)
    session.commit()

    # 0–10 的分以前在校验里就被 le=1 拒掉、整遍软 QC 作废；现在先换算再校验
    assert decision.branch == "continue"
    assert captured and captured[0]["few_shot_k_cap"] == REVIEW_FEW_SHOT_K_CAP == 4
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    judge = next(entry for entry in report.rewrite_brief_json if entry.get("kind") == "reference_judge")
    assert judge == {
        "kind": "reference_judge",
        "scale": "0-10",
        "style_score": 8.2,
        "dimension_scores": {"language.rhetoric": 6.5, "theme.emotional_tone": 9.0},
    }
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "soft_qc")).scalars().one()
    assert attempt.details_json["reference_judge"] == judge


def test_soft_qc_score_scale_is_decided_per_answer() -> None:
    from novel_system.services.qc_engine import _normalize_soft_qc_scores

    percent = _normalize_soft_qc_scores({"style_score": 93, "dimension_scores": {"scene.dialogue": 70}})
    assert percent["style_score"] == 0.93 and percent["dimension_scores"] == {"scene.dialogue": 0.7}
    # 没给总分：取按维分的均值；换算一次后再换算不变
    derived = _normalize_soft_qc_scores({"dimension_scores": {"scene.dialogue": 6, "theme.values": 8}})
    assert derived["style_score"] == 0.7
    assert _normalize_soft_qc_scores(derived) == derived


def test_prompt_schemas_declare_judge_and_review_score_ranges() -> None:
    from novel_system.services.near_final import SCENE_ACCEPTANCE_SCORE_KEYS
    from novel_system.services.prompt_builder import load_prompt_templates

    templates = load_prompt_templates()
    soft = templates["soft_qc"].structured_schema["properties"]
    assert set(soft["dimension_scores"]["properties"]) == set(ALL_DIMENSIONS)
    assert all(
        spec == {"type": "number", "minimum": 0, "maximum": 10}
        for spec in soft["dimension_scores"]["properties"].values()
    )
    assert soft["style_score"]["maximum"] == 10
    review = templates["near_final_acceptance_review"].structured_schema["properties"]
    assert set(review["scores"]["properties"]) == set(SCENE_ACCEPTANCE_SCORE_KEYS)
    assert set(review["scores"]["required"]) == set(SCENE_ACCEPTANCE_SCORE_KEYS)
    assert review["overall_score"]["maximum"] == 10


# ---------------------------------------------------------------------------
# V7 / L4 · 准定稿评审
# ---------------------------------------------------------------------------


def test_near_final_scores_are_rescaled_not_saturated() -> None:
    from novel_system.services.near_final import _normalize_acceptance_payload

    ten = _normalize_acceptance_payload(
        {
            "near_final_status": "near_final_ready",
            "pass_flag": True,
            "overall_score": 9.3,
            "scores": {"author_voice_match": 9.3, "ending_drive": 7, "continuity": True, "bogus": "x"},
        }
    )
    assert ten["overall_score"] == 0.93, "0–10 的 9.3 是 0.93，不是夹成的 1.0"
    assert ten["scores"] == {"author_voice_match": 0.93, "ending_drive": 0.7}
    unit = _normalize_acceptance_payload({"overall_score": 0.85, "scores": {"story_necessity": 0.9}})
    assert unit["overall_score"] == 0.85 and unit["scores"] == {"story_necessity": 0.9}
    percent = _normalize_acceptance_payload({"overall_score": 93, "scores": {"story_necessity": 40}})
    assert percent["overall_score"] == 0.93 and percent["scores"] == {"story_necessity": 0.4}
    assert score_scale([9.3, 0.5]) == 10.0 and to_unit(9.3, 10.0) == 0.93


def test_near_final_review_gets_four_sample_windows(session, monkeypatch) -> None:
    from novel_system.services.near_final import NearFinalAcceptanceService

    scene = _seed_scene(session)
    captured: list[dict] = []

    def fake_inject(session_arg, prompt, scene_arg, bundle=None, **kwargs):  # noqa: ANN001, ANN003
        captured.append(kwargs)
        return prompt

    monkeypatch.setattr("novel_system.services.style_prompt_injection.inject_style_reference_prefix", fake_inject)
    NearFinalAcceptanceService(session, llm_runner=object())._inject_style_reference_prefix(
        {"system_prompt": "S", "user_prompt": "U"},
        scene,
        {"snapshot": {}},
        context_text="正文",
        final_user_prompt="U",
    )
    assert captured[0]["few_shot_k_cap"] == REVIEW_FEW_SHOT_K_CAP


# ---------------------------------------------------------------------------
# L8 · 新鲜度预算；J1 · 一个 bundle 一份契约
# ---------------------------------------------------------------------------

_PRIOR_FINAL = (
    "他像一只受惊的猫一样缩回门后，像一只受惊的猫一样盯着走廊。"
    "码头的风很冷，码头的风很冷，他把领子立起来又放下去。"
) * 3


def _seed_prior_final(session) -> None:
    session.add(
        FinalScene(
            row_id="final_v3_pipe_sc01",
            scene_id=PREVIOUS_SCENE_ID,
            chapter_id=CHAPTER_ID,
            content=_PRIOR_FINAL,
            status="archived",
            source_bundle_id="bundle_prev",
            source_bundle_hash="hash_prev",
        )
    )
    session.get(SceneRunState, PREVIOUS_SCENE_ID).current_final_scene_row_id = "final_v3_pipe_sc01"
    session.commit()


def test_freshness_budget_keeps_only_verbatim_lists_when_the_policy_defers(session, monkeypatch) -> None:
    from novel_system.services import self_repetition
    from novel_system.services.bundle_builder import BundleBuilder

    scene = _seed_scene(session)
    _seed_prior_final(session)
    monkeypatch.setattr(
        self_repetition.LifetimeExpressionRegistry,
        "get_lifetime_avoidance_guidance",
        lambda self, project_id: "【全书已用表达禁用清单】劣质的迷彩布",
    )
    monkeypatch.setattr(
        self_repetition,
        "check_semantic_repetition",
        lambda *args, **kwargs: [
            self_repetition.SemanticRepetitionHit(
                pattern_type="metaphor", current_text="劣质的帆布", previous_text="劣质的迷彩布", source_scene_id="x"
            )
        ],
    )
    monkeypatch.setattr(
        self_repetition.SelfRepetitionDetector,
        "top_repeated_ngrams",
        lambda self, chapter_id, **kwargs: ["码头的风很冷他把"],
    )
    builder = BundleBuilder(session)

    house = builder._literary_freshness_budget(scene)["budget"]
    assert "lifetime_banned_expressions" in house and "semantic_repetition_alert" in house
    assert "avoid_image_fields" in house

    deferring = StylePolicy(bound=True, style_first=True, mode="frozen")
    deferred = builder._literary_freshness_budget(scene, deferring)["budget"]
    assert deferred["construction_lists"] == "deferred_to_reference"
    assert "lifetime_banned_expressions" not in deferred
    assert "semantic_repetition_alert" not in deferred
    assert "avoid_image_fields" not in deferred and "avoid_action_templates" not in deferred
    assert deferred["avoid_recent_ngrams"] == ["码头的风很冷他把"], "逐字层的防复读保留"
    assert "劣质" not in str(deferred)


def test_bundle_builds_the_style_contract_once_and_the_budget_reads_its_policy(session, monkeypatch) -> None:
    from novel_system.services import bundle_builder as bundle_module

    _seed_scene(session)
    _seed_prior_final(session)
    _bind(session, draft_mode="style_first", seed="once")
    calls: list[int] = []
    original = bundle_module.build_style_runtime_contract

    def counting(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(bundle_module, "build_style_runtime_contract", counting)
    monkeypatch.setattr(
        bundle_module,
        "resolve_scene_style_runtime_contract",
        lambda *args, **kwargs: pytest.fail("the freshness budget must not build a second contract"),
    )
    snapshot = bundle_module.BundleBuilder(session).build(SCENE_ID)["snapshot"]
    assert calls == [1]
    import json

    budget = json.loads(snapshot["inline_digests"]["literary_freshness_budget"])
    assert budget["construction_lists"] == "deferred_to_reference"


# ---------------------------------------------------------------------------
# V11 · 成稿门按参考书校准的规则判
# ---------------------------------------------------------------------------


def test_final_gate_uses_the_reference_calibration_under_a_binding(session, monkeypatch) -> None:
    from novel_system.services.final_text_gate import FinalTextGateService
    from novel_system.services.literary_quality import RuleCalibration

    scene = _seed_scene(session)
    text = "他走了。她坐着。天黑了。"  # 无选择 / 无代价 / 无结尾动作
    gate = FinalTextGateService(session)

    house = gate._literary(scene, text, StylePolicy())
    assert house["house_taste_thresholds"] == "applied" and house["warnings"]
    assert "literary:painless_scene" in {item["issue_key"] for item in house["warnings"]}

    calibration = RuleCalibration(
        source="reference",
        dimension_stats={
            "painless_scene": {"fired": 44, "n": 48, "share": 0.92, "lower_bound": 0.85, "level": "habit"},
        },
    )
    monkeypatch.setattr(FinalTextGateService, "_rule_calibration", lambda self, policy: calibration if policy.bound else None)
    bound = gate._literary(scene, text, StylePolicy(bound=True, style_first=True, mode="frozen"))
    assert bound["house_taste_thresholds"] == "calibrated_to_reference"
    keys = {item["issue_key"] for item in bound["warnings"]}
    assert "literary:painless_scene" not in keys, "参考作者的常态不是毛病"
    assert keys, "其余风险照常警告——不再整体丢掉"
    assert bound["rule_calibration"]["habitual_dimensions"] == ["painless_scene"]

    # 校准不可用：作者手笔直起时整体让位，先中性后润色时按房风
    monkeypatch.setattr(FinalTextGateService, "_rule_calibration", lambda self, policy: None)
    deferred = gate._literary(scene, text, StylePolicy(bound=True, style_first=True, mode="frozen"))
    assert deferred["house_taste_thresholds"] == "deferred_to_reference" and deferred["warnings"] == []
    neutral = gate._literary(scene, text, StylePolicy(bound=True, style_first=False, mode="frozen"))
    assert neutral["house_taste_thresholds"] == "applied" and neutral["warnings"]
