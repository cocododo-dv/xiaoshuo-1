"""2026-09-14 保真修补 · WP6「规划与写手侧看到参考」。

- ``InjectionService.few_shot_k_cap`` / ``inject_style_reference_prefix(few_shot_k_cap=)``：规划 /
  评审 / 局部补丁节点的样例窗口封顶（不传 → 起草通道逐字不变）；``runtime_contract=`` 让调用方
  用自己刚解析的契约渲染，审计记 ``resolved_live``。
- ``scene_blueprint``：有绑定（含 ``neutral_first``）时拿到 ``[STYLE_REFERENCE]``（k≤3），前缀与
  来源快照登记的契约同源，预算 24000；无绑定时提示词逐字不变。
- 近终稿规划（章架构 / 人物压力）的来源快照与蓝图共用 ``planning_context`` 的三块摘要，并经
  ``_planning_user_prompt`` 渲染结构画像 / 场景手法；两次 LLM 请求都带这些块。
- 写手侧：``author_proposal_generate``（产出正文的类型，完整 k）、``writer_passage_patch`` /
  ``writer_deep_review``（k≤3）按项目 / 场景绑定注入前缀；结构候选与无绑定不注入。
- 模板版本 / 条款 / 预算地板。

fake LLM；无真实模型。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceInjectionBinding,
)
from novel_system.db.session import SessionLocal
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.bundle_builder import resolve_scene_style_runtime_contract
from novel_system.services.context_budget import finalize_request_budget
from novel_system.services.llm_client import LLMRequest, LLMResponse, OnlineAccountedExecution
from novel_system.services.near_final import NearFinalPlanningService, _planning_user_prompt
from novel_system.services.prompt_builder import (
    PLANNING_STYLE_INPUT_TOKEN_BUDGET,
    RUNTIME_MIN_INPUT_BUDGETS,
    STYLE_PASS_INPUT_TOKEN_BUDGET,
    load_prompt_templates,
)
from novel_system.services.scene_blueprint import SCENE_BLUEPRINT_FIELDS, SceneBlueprintService
from novel_system.services.style_prompt_injection import (
    PLANNING_FEW_SHOT_K_CAP,
    RESOLVED_CONTRACT_MODE,
    RESOLVED_CONTRACT_STATUS,
    inject_style_reference_prefix,
    resolve_style_scope,
)
from novel_system.services.style_reference.injection import InjectionService, _few_shot_k
from novel_system.services.style_reference.planning_context import (
    STYLE_PLANNING_GUIDANCE_KEY,
    STYLE_REFERENCE_DIGEST_KEYS,
    STYLE_STRUCTURE_CARD_KEY,
    build_planning_style_reference,
    snapshot_has_style_reference,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import InjectionStrategy
from novel_system.services.writer_deep_review import WriterDeepReviewService
from tests.test_style_reference_injection_v2 import _bind, _seed_full
from tests.test_style_reference_structure import _profile_json_with_structure, _seed_style_binding

PROJECT_ID = "P_WP6"
CHAPTER_ID = "WP6_CH01"
SCENE_ID = "WP6_CH01_SC01"
EXCERPT = "主角追问那个名字。"
NARRATIVE_LABEL = "## Style Reference — Narrative Mechanisms"
CARD_LABEL = "## Style Reference — Structure Card"
CRAFT_LABEL = "## Style Reference — Scene Craft"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _seed_scene(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="WP6", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="一次重逢必须变成一个选择。",
            writer_brief_json={"chapter_promise": "重逢揭出危险的沉默"},
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A"],
            location="茶馆",
            scene_goal=EXCERPT,
            beats_json=["追问", "回避", "决定自己去查"],
            exit_change="老友成了嫌疑人。",
            hook="茶杯在名字出口时停住。",
            writer_brief_json={"choice_under_pressure": "信任老友还是独自调查"},
            target_length_band="short",
            scene_type="reveal",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()


def _bind_project(seed: str, *, project_id: str = PROJECT_ID, config_json: dict | None = None) -> tuple[str, str]:
    """真实规模画像（段落 + 引文 + voice_signature + narrative_guidance）+ project 作用域 mixed 绑定。"""
    book_id, profile_id = _seed_full(seed)
    with SessionLocal() as session:
        _bind(
            StyleReferenceRepository(session),
            binding_id=f"wp6_bind_{seed}",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            strategy="mixed",
            config_json=config_json if config_json is not None else {"intensity": 100},
        )
        session.commit()
    return book_id, profile_id


def _unbind_all(session) -> None:
    session.query(StyleReferenceInjectionBinding).delete()
    session.commit()


def _few_shot_block(text: str) -> str:
    """样例块:``[风格样例](…`` 标题行到 ``[/风格样例]``(2026-09-22 起不再有不可信数据边界)。"""
    start = text.find("[风格样例](")
    end = text.find("[/风格样例]", start)
    return text[start:end] if start >= 0 and end >= 0 else ""


def _few_shot_entries(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.startswith("- (")]


def _base_prompt() -> dict:
    return {
        "system_prompt": "BASE SYSTEM",
        "user_prompt": "BASE USER",
        "token_budget": {"target_input_tokens": PLANNING_STYLE_INPUT_TOKEN_BUDGET},
    }


class _ScriptedClient(OnlineAccountedExecution):
    """按顺序回放载荷的在线记账替身（记录每个请求）。"""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError(f"unexpected request for {request.node_id}")
        self.requests.append(request)
        payload = self.payloads.pop(0)
        return LLMResponse(
            request_id=f"req_{request.node_id}_{len(self.requests)}",
            provider="fake",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format=request.response_format,
            raw_response={"id": f"req_{len(self.requests)}", "model": request.model},
            usage={"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            finish_reason="stop",
        )

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def _architecture_payload() -> dict:
    return {
        "chapter_promise": "重逢揭出危险的沉默。",
        "escalation_path": ["追问", "回避", "决定自己去查"],
        "reveal_plan": ["老友认得那个名字"],
        "payoff_target": "主角不再请求许可。",
        "character_shift": "从信任转向怀疑。",
        "ending_question": "老友为什么隐瞒那个名字？",
    }


def _pressure_payload() -> dict:
    return {
        "surface_goal": "问出那个名字。",
        "hidden_fear": "老友也在骗她。",
        "wrong_belief": "只要问得够直接就能得到真话。",
        "shame_point": "她曾替老友遮掩过。",
        "avoidance_strategy": "用茶杯和客套拖延。",
        "relationship_debt": "老友当年替她顶过罪。",
        "current_mask": "叙旧的轻松。",
    }


def _patch_payload() -> dict:
    return {
        "patches": [
            {
                "target_text_ref": "author_draft:test",
                "source_excerpt": EXCERPT,
                "replacement_text": "主角把茶杯推过去，只问了一个名字。",
                "patch_type": "replace_excerpt",
                "changed_dimensions": ["dialogue_subtext"],
                "why_it_helps": "把追问落到动作上。",
            },
            {
                "target_text_ref": "author_draft:test",
                "source_excerpt": EXCERPT,
                "replacement_text": "名字出口时，茶杯停住了。",
                "patch_type": "replace_excerpt",
                "changed_dimensions": ["information_rhythm"],
                "why_it_helps": "用物件承载停顿。",
            },
        ],
        "rationale": "保留事实，换成作者的手笔。",
        "manual_only": True,
    }


def _deep_review_payload() -> dict:
    finding = {
        "lens": "prose",
        "dimension": "repetitive_expression",
        "severity": "revision",
        "classification": "revision",
        "issue": "同一动作重复出现。",
        "recommendation": "按参考作者的手笔处理复沓。",
        "evidence_excerpt": EXCERPT,
        "evidence_location": "scene body",
        "why_it_matters": "复读要对照参考作者判断。",
        "scene_form": "plot_scene",
    }
    return {
        "overall_score": 0.6,
        "scores": {"repetitive_expression": 0.55},
        "findings": [finding],
        "revision_brief": [
            {"classification": "revision", "dimension": "repetitive_expression", "recommendation": "对照样例。"}
        ],
        "lens_evaluations": [
            {"lens": "prose", "overall_score": 0.6, "scores": {"repetitive_expression": 0.55}, "findings": [finding], "revision_brief": []}
        ],
    }


class _BlueprintRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def provider_execution_mode(self) -> str:
        return "online"

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(
            llm_call_id=f"llm_call_wp6_bp_{len(self.calls)}",
            response=SimpleNamespace(structured_output={field: f"{field} 内容" for field in SCENE_BLUEPRINT_FIELDS}),
        )


# ---------------------------------------------------------------------------
# k 上限
# ---------------------------------------------------------------------------


def test_injection_service_honours_few_shot_k_cap() -> None:
    _book_id, profile_id = _seed_full("wp6_kcap")
    with SessionLocal() as session:
        profile = StyleReferenceRepository(session).get_profile(profile_id)
        svc = InjectionService(session)
        _fragments, uncapped = svc.render_preview(profile, InjectionStrategy.MIXED, {"intensity": 100})
        assert uncapped["few_shot_k"] == _few_shot_k(100) > PLANNING_FEW_SHOT_K_CAP
        assert uncapped["few_shot_windows"] > PLANNING_FEW_SHOT_K_CAP

        svc.few_shot_k_cap = PLANNING_FEW_SHOT_K_CAP
        fragments, capped = svc.render_preview(profile, InjectionStrategy.MIXED, {"intensity": 100})
        assert capped["few_shot_k"] == PLANNING_FEW_SHOT_K_CAP
        assert 1 <= capped["few_shot_windows"] <= PLANNING_FEW_SHOT_K_CAP
        assert len(_few_shot_entries(fragments.few_shot_block)) == capped["few_shot_windows"]
        # 抽象块不受上限影响；红线段仍随注
        assert capped["positive_lines"] == uncapped["positive_lines"]
        assert fragments.anti_plagiarism_block

        # 上限大于 k(intensity) 时不起作用；0 关掉样例块
        svc.few_shot_k_cap = 50
        _fragments, loose = svc.render_preview(profile, InjectionStrategy.MIXED, {"intensity": 100})
        assert loose["few_shot_k"] == uncapped["few_shot_k"]
        svc.few_shot_k_cap = 0
        none_fragments, none_stats = svc.render_preview(profile, InjectionStrategy.MIXED, {"intensity": 100})
        assert none_stats["few_shot_k"] == 0 and none_stats["few_shot_windows"] == 0
        assert none_fragments.few_shot_block == ""


def test_inject_style_reference_prefix_caps_windows_and_labels_resolved_contracts(session) -> None:
    _seed_scene(session)
    _bind_project("wp6_inj")
    scene = session.get(SceneCard, SCENE_ID)

    full = inject_style_reference_prefix(session, _base_prompt(), scene, None, final_user_prompt="BASE USER")
    capped = inject_style_reference_prefix(
        session, _base_prompt(), scene, None, final_user_prompt="BASE USER", few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP
    )
    for injected in (full, capped):
        assert injected["system_prompt"].startswith("[STYLE_REFERENCE]")
        assert injected["system_prompt"].endswith("BASE SYSTEM")
        assert "[风格样例]" in injected["system_prompt"]
    full_windows = full["_style_reference_runtime_audit"]["render_stats"]["few_shot_windows"]
    capped_windows = capped["_style_reference_runtime_audit"]["render_stats"]["few_shot_windows"]
    assert 1 <= capped_windows <= PLANNING_FEW_SHOT_K_CAP < full_windows
    assert capped["_style_reference_runtime_audit"]["render_stats"]["few_shot_k"] == PLANNING_FEW_SHOT_K_CAP
    assert len(_few_shot_entries(_few_shot_block(capped["system_prompt"]))) == capped_windows
    # 无 bundle → 实时绑定路径
    assert capped["_style_reference_runtime_audit"]["runtime_contract_mode"] == "legacy_live"

    # 调用方给出自己解析的契约：审计标 resolved_live，契约哈希与给出的契约一致
    contract = resolve_scene_style_runtime_contract(session, scene)
    assert contract is not None
    resolved = inject_style_reference_prefix(
        session,
        _base_prompt(),
        scene,
        None,
        final_user_prompt="BASE USER",
        few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP,
        runtime_contract=contract,
    )
    audit = resolved["_style_reference_runtime_audit"]
    assert audit["runtime_contract_status"] == RESOLVED_CONTRACT_STATUS == "resolved_live"
    assert audit["runtime_contract_mode"] == RESOLVED_CONTRACT_MODE == "resolved"
    assert audit["contract_hash"] == contract["contract_hash"]
    assert audit["outcome"] == "hit" and audit["render_stats"]["few_shot_k"] == PLANNING_FEW_SHOT_K_CAP
    assert resolved["system_prompt"].startswith("[STYLE_REFERENCE]")

    # 无绑定：逐字不变（连审计键都不加）
    _unbind_all(session)
    untouched = inject_style_reference_prefix(
        session, _base_prompt(), scene, None, final_user_prompt="BASE USER", few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP
    )
    assert untouched == _base_prompt()


# ---------------------------------------------------------------------------
# 6.2 · 场景蓝图带样例
# ---------------------------------------------------------------------------


def test_scene_blueprint_gets_the_style_prefix_only_when_bound(session) -> None:
    _seed_scene(session)
    runner = _BlueprintRunner()
    service = SceneBlueprintService(session, llm_runner=runner)
    template = load_prompt_templates()["scene_blueprint"]

    service.generate(SCENE_ID)
    plain = runner.calls[-1]["prompt"]
    assert plain["system_prompt"] == template.system_prompt
    assert "_style_reference_runtime_audit" not in plain
    assert plain["token_budget"]["target_input_tokens"] == PLANNING_STYLE_INPUT_TOKEN_BUDGET == 24000
    assert NARRATIVE_LABEL not in runner.calls[-1]["user_prompt"]
    session.commit()  # 另一个 SessionLocal 要写库：先放掉本会话的写锁

    # neutral_first 绑定同样注入：这是「看得见参考」，与起草方式无关
    _bind_project("wp6_bp", config_json={"intensity": 100, "draft_mode": "neutral_first"})
    service.generate(SCENE_ID)
    bound = runner.calls[-1]["prompt"]
    user_prompt = runner.calls[-1]["user_prompt"]
    assert bound["system_prompt"].startswith("[STYLE_REFERENCE]")
    assert bound["system_prompt"].endswith(template.system_prompt)
    assert "[风格样例]" in bound["system_prompt"]
    audit = bound["_style_reference_runtime_audit"]
    assert audit["runtime_contract_status"] == RESOLVED_CONTRACT_STATUS
    assert audit["render_stats"]["few_shot_k"] == PLANNING_FEW_SHOT_K_CAP
    assert 1 <= audit["render_stats"]["few_shot_windows"] <= PLANNING_FEW_SHOT_K_CAP
    # 前缀与来源快照登记的契约同源
    scene = session.get(SceneCard, SCENE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)
    source = service._source_snapshot(scene, chapter)
    assert source["style_runtime_contract"]["contract_hash"] == audit["contract_hash"]
    assert source["snapshot"]["source_version_refs"]["style_reference_runtime_contract_hash"] == audit["contract_hash"]
    assert snapshot_has_style_reference(source["snapshot"])
    # 叙事机制 section 仍在 user prompt 里（_seed_full 画像带 narrative_guidance）
    assert NARRATIVE_LABEL in user_prompt
    assert user_prompt.index(NARRATIVE_LABEL) < user_prompt.index("## Scene Blueprint Target")
    # 整个请求落在模板预算内（注入器按最终 user prompt 压缩）
    budget = finalize_request_budget(
        system_prompt=bound["system_prompt"], user_prompt=user_prompt, base_budget=bound["token_budget"]
    )["budget"]
    assert budget["estimated_input_tokens"] <= budget["target_input_tokens"]
    assert not budget.get("split_scene_recommended")

    # 解绑后再生成：又回到逐字相同的基础提示词
    _unbind_all(session)
    service.generate(SCENE_ID)
    assert runner.calls[-1]["prompt"]["system_prompt"] == template.system_prompt
    assert "style_runtime_contract" in service._source_snapshot(scene, chapter)
    assert service._source_snapshot(scene, chapter)["style_runtime_contract"] is None


def test_scene_blueprint_prefix_failure_degrades_to_the_base_prompt(session, monkeypatch) -> None:
    _seed_scene(session)
    _bind_project("wp6_bpfail")
    runner = _BlueprintRunner()
    service = SceneBlueprintService(session, llm_runner=runner)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr("novel_system.services.scene_blueprint.inject_style_reference_prefix", _boom)
    service.generate(SCENE_ID)
    prompt = runner.calls[-1]["prompt"]
    assert prompt["system_prompt"] == load_prompt_templates()["scene_blueprint"].system_prompt
    # 快照里的摘要块不受前缀失败影响
    assert STYLE_STRUCTURE_CARD_KEY not in prompt  # prompt 不是快照；只确认调用没有被阻断
    assert service.latest(SCENE_ID) is not None


# ---------------------------------------------------------------------------
# 6.1 · 近终稿规划快照
# ---------------------------------------------------------------------------


def test_near_final_planning_snapshot_carries_the_reference_like_the_blueprint(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, SCENE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)
    service = NearFinalPlanningService(session, llm_client=_ScriptedClient([]))

    plain = service._source_snapshot(scene=scene, chapter=chapter, include_chapter_architecture=False)
    assert not any(key.startswith("style_") for key in plain["snapshot"]["inline_digests"])
    assert "style_reference_runtime_contract_hash" not in plain["snapshot"]["source_version_refs"]
    assert _planning_user_prompt("BASE", scene=scene, chapter=chapter, source=plain) == _planning_user_prompt(
        "BASE", scene=scene, chapter=chapter
    )

    _seed_style_binding(
        session,
        project_id=PROJECT_ID,
        profile_json=_profile_json_with_structure(narrative_guidance=["关键信息放段首一次给出"]),
        seed="wp6nf",
    )
    source = service._source_snapshot(scene=scene, chapter=chapter, include_chapter_architecture=True)
    snapshot = source["snapshot"]
    digests = snapshot["inline_digests"]
    assert digests[STYLE_STRUCTURE_CARD_KEY].startswith("[结构画像]")
    assert digests[STYLE_PLANNING_GUIDANCE_KEY].startswith("[场景手法]")
    assert "- 关键信息放段首一次给出" in digests["style_narrative_guidance"]
    assert "短句克制" not in digests[STYLE_STRUCTURE_CARD_KEY]  # 语言层特征不进规划层
    slots = [item["slot"] for item in snapshot["ordered_injections"]]
    assert slots[-3:] == list(STYLE_REFERENCE_DIGEST_KEYS)
    refs = snapshot["source_version_refs"]
    assert len(refs["style_reference_runtime_contract_hash"]) == 64
    assert refs["style_narrative_guidance_line_count"] == 1
    assert refs["style_planning_guidance_line_count"] == 2
    assert refs["style_structure_card_chars"] == len(digests[STYLE_STRUCTURE_CARD_KEY])
    assert all(
        item["ref_id"] == refs["style_reference_runtime_contract_hash"] and item["digest_key"] == item["slot"]
        for item in snapshot["ordered_injections"]
        if item["slot"].startswith("style_")
    )
    assert snapshot_has_style_reference(snapshot)
    # 与蓝图快照共用同一套摘要（同一个助手、同一份契约）
    blueprint_snapshot = SceneBlueprintService(session, llm_client=object())._source_snapshot(scene, chapter)["snapshot"]
    assert {k: v for k, v in blueprint_snapshot["inline_digests"].items() if k.startswith("style_")} == {
        k: v for k, v in digests.items() if k.startswith("style_")
    }
    # 结构画像 / 场景手法不在 SECTION_SPECS 里：由规划 user prompt 直接渲染，排在 Planning Target 之前
    prompt = _planning_user_prompt("BASE", scene=scene, chapter=chapter, source=source)
    assert CARD_LABEL in prompt and CRAFT_LABEL in prompt
    assert prompt.index(CARD_LABEL) < prompt.index(CRAFT_LABEL) < prompt.index("## Planning Target")
    assert "共 5 章" in prompt

    # 两个规划节点的真实请求都带三块
    client = _ScriptedClient([_architecture_payload(), _pressure_payload()])
    result = NearFinalPlanningService(session, llm_client=client).ensure_scene_planning(SCENE_ID)
    assert [request.node_id for request in client.requests] == ["chapter_story_architecture", "character_pressure_blueprint"]
    for request in client.requests:
        user = request.messages[1]["content"]
        assert "[结构画像]" in user and "[场景手法]" in user and NARRATIVE_LABEL in user
        assert "- 关键信息放段首一次给出" in user
        assert user.index("[结构画像]") < user.index("## Planning Target")
    assert result["character_pressure"]["payload"]["wrong_belief"] == "只要问得够直接就能得到真话。"


def test_build_planning_style_reference_degrades_for_legacy_profiles() -> None:
    assert build_planning_style_reference(None) is None
    assert build_planning_style_reference({"contract_hash": "x" * 64, "layers": []}) is None
    legacy = {"contract_hash": "y" * 64, "layers": [{"profile": {"profile_id": "p", "profile_json": {"style_features": ["短句"]}}}]}
    assert build_planning_style_reference(legacy) is None
    craft_only = {
        "contract_hash": "z" * 64,
        "layers": [{"profile": {"profile_id": "p", "profile_json": {"planning_guidance": ["母题：灯与账本反复出现"]}}}],
    }
    reference = build_planning_style_reference(craft_only)
    assert reference is not None
    assert list(reference.digests) == [STYLE_PLANNING_GUIDANCE_KEY]
    assert reference.refs == {
        "style_reference_runtime_contract_hash": "z" * 64,
        "style_narrative_guidance_line_count": 0,
        "style_planning_guidance_line_count": 1,
    }


# ---------------------------------------------------------------------------
# 6.3 · 写手侧
# ---------------------------------------------------------------------------


def test_resolve_style_scope_prefers_the_scene_and_falls_back_to_the_project(session) -> None:
    _seed_scene(session)
    scene_scope = resolve_style_scope(session, scene_id=SCENE_ID)
    assert isinstance(scene_scope, SceneCard) and scene_scope.scene_id == SCENE_ID

    # 旧场景行没有 project_id：补上其章的项目，其余作用域属性照抄
    session.add(SceneCard(scene_id="WP6_LEGACY", chapter_id=CHAPTER_ID, scene_seq=2, scene_goal="x", beats_json=[], pov_character_id="CHAR_B"))
    session.commit()
    legacy = resolve_style_scope(session, scene_id="WP6_LEGACY")
    assert legacy.project_id == PROJECT_ID and legacy.scene_id == "WP6_LEGACY" and legacy.pov_character_id == "CHAR_B"

    chapter_scope = resolve_style_scope(session, chapter_id=CHAPTER_ID)
    assert chapter_scope.project_id == PROJECT_ID and chapter_scope.scene_id is None
    project_scope = resolve_style_scope(session, project_id=PROJECT_ID)
    assert project_scope.project_id == PROJECT_ID and project_scope.scene_id is None
    assert resolve_style_scope(session, scene_id="missing", chapter_id="missing") is None
    assert resolve_style_scope(session) is None


def test_author_proposal_prompt_carries_the_style_prefix_for_prose_types(session, monkeypatch) -> None:
    # 七次连续建议 × 完整 k 样例:场景 token 预算(套件默认武装 ×5)会在最后几次前耗尽——预算不是本用例的对象
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list[LLMRequest] = []

    def fake_generate(self, request, *, accounting_hook=None):  # noqa: ANN001
        captured.append(request)
        payload = {"content": "在线替身：按参考作者的手笔写。", "rationale": "whole_draft：放弃了房风的动作收尾。"}
        response = LLMResponse(
            request_id=f"resp_{len(captured)}",
            provider="fake",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_{len(captured)}", "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )
        if accounting_hook is not None:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response

    monkeypatch.setattr("novel_system.services.llm_client.LLMClient.generate", fake_generate)
    _seed_scene(session)
    service = AuthorDraftService(session)
    draft = service.ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]
    template = load_prompt_templates()["author_proposal_generate"]

    service.generate_proposal(draft["draft_id"], {"proposal_type": "whole_draft"})
    assert captured[-1].node_id == "author_proposal_generate"
    assert captured[-1].messages[0]["content"] == template.system_prompt
    session.commit()

    _bind_project("wp6_ap")
    for proposal_type in ("whole_draft", "continuation", "near_final_rewrite", "language_pass", "dialogue_pass", "passage_candidate"):
        service.generate_proposal(draft["draft_id"], {"proposal_type": proposal_type, "instruction": "保持作者手笔。"})
        system = captured[-1].messages[0]["content"]
        user = captured[-1].messages[1]["content"]
        assert system.startswith("[STYLE_REFERENCE]"), proposal_type
        assert system.endswith(template.system_prompt), proposal_type
        # 2026-09-22 风格参考优先:写手建议也把样例放到 user 消息末尾,system 只留一句指路
        assert "参考作者的原文样例在 user 消息的末尾" in system, proposal_type
        assert "[/风格样例]" in user and user.rstrip().endswith("输出仍只返回前文要求的 JSON。"), proposal_type
        assert "保持作者手笔。" in user
    # 建议是要写正文的节点：完整 k，不封顶
    assert len(_few_shot_entries(_few_shot_block(captured[-1].messages[1]["content"]))) > PLANNING_FEW_SHOT_K_CAP

    # 结构候选是修订笔记，不是正文 → 不注入
    service.generate_proposal(draft["draft_id"], {"proposal_type": "structure_candidate"})
    assert captured[-1].messages[0]["content"] == template.system_prompt

    # 整章稿按 project + global 作用域拿到同一参考
    chapter_draft = service.ensure_blank("chapter", CHAPTER_ID, actor_ref="writer")["draft"]
    service.generate_proposal(chapter_draft["draft_id"], {"proposal_type": "chapter_draft"})
    assert captured[-1].messages[0]["content"].startswith("[STYLE_REFERENCE]")

    _unbind_all(session)
    service.generate_proposal(draft["draft_id"], {"proposal_type": "whole_draft"})
    assert captured[-1].messages[0]["content"] == template.system_prompt


def test_passage_patch_prompt_carries_the_style_prefix_with_capped_windows(session) -> None:
    _seed_scene(session)
    draft = AuthorDraftService(session).ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]
    payload = {
        "object_type": "scene",
        "object_id": SCENE_ID,
        "chapter_id": CHAPTER_ID,
        "scene_id": SCENE_ID,
        "target_text_ref": f"author_draft:{draft['draft_id']}",
        "source_draft_id": draft["draft_id"],
        "source_excerpt": EXCERPT,
        "issue_dimension": "dialogue_subtext",
    }
    template = load_prompt_templates()["writer_passage_patch"]

    client = _ScriptedClient([_patch_payload()])
    WriterDeepReviewService(session, llm_client=client).create_patch_candidate(payload, actor_ref="writer")
    assert client.requests[-1].node_id == "writer_passage_patch"
    assert client.requests[-1].messages[0]["content"] == template.system_prompt
    session.commit()

    _bind_project("wp6_pp")
    client = _ScriptedClient([_patch_payload()])
    result = WriterDeepReviewService(session, llm_client=client).create_patch_candidate(payload, actor_ref="writer")
    system = client.requests[-1].messages[0]["content"]
    assert system.startswith("[STYLE_REFERENCE]") and system.endswith(template.system_prompt)
    assert "[风格样例]" in system
    assert 1 <= len(_few_shot_entries(_few_shot_block(system))) <= PLANNING_FEW_SHOT_K_CAP
    assert EXCERPT in client.requests[-1].messages[1]["content"]
    assert result["candidate"]["generation_llm_call_id"]

    # 章对象：project + global 作用域
    client = _ScriptedClient([_patch_payload()])
    WriterDeepReviewService(session, llm_client=client).create_patch_candidate(
        {**payload, "object_type": "chapter", "object_id": CHAPTER_ID, "scene_id": None}, actor_ref="writer"
    )
    assert client.requests[-1].messages[0]["content"].startswith("[STYLE_REFERENCE]")


def test_deep_review_prompt_carries_the_style_prefix_with_capped_windows(session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    _seed_scene(session)
    AuthorDraftService(session).ensure_blank("scene", SCENE_ID, actor_ref="writer")
    template = load_prompt_templates()["writer_deep_review"]

    client = _ScriptedClient([_deep_review_payload()])
    WriterDeepReviewService(session, llm_client=client).run_scene_review(SCENE_ID)
    assert client.requests[-1].node_id == "writer_deep_review"
    assert client.requests[-1].messages[0]["content"] == template.system_prompt
    session.commit()

    _bind_project("wp6_dr")
    client = _ScriptedClient([_deep_review_payload()])
    result = WriterDeepReviewService(session, llm_client=client).run_scene_review(SCENE_ID)
    system = client.requests[-1].messages[0]["content"]
    assert system.startswith("[STYLE_REFERENCE]") and system.endswith(template.system_prompt)
    assert "[风格样例]" in system
    assert 1 <= len(_few_shot_entries(_few_shot_block(system))) <= PLANNING_FEW_SHOT_K_CAP
    assert result["latest_evaluation"]["findings"][0]["dimension"] == "repetitive_expression"

    # 章级深评：project + global 作用域
    client = _ScriptedClient([_deep_review_payload()])
    WriterDeepReviewService(session, llm_client=client).run_chapter_review(CHAPTER_ID)
    assert client.requests[-1].messages[0]["content"].startswith("[STYLE_REFERENCE]")


def test_writer_prefix_failure_degrades_to_the_base_prompt(session, monkeypatch) -> None:
    _seed_scene(session)
    _bind_project("wp6_wrfail")
    draft = AuthorDraftService(session).ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]
    session.commit()

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr("novel_system.services.writer_deep_review.inject_style_reference_prefix", _boom)
    client = _ScriptedClient([_patch_payload()])
    result = WriterDeepReviewService(session, llm_client=client).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "source_draft_id": draft["draft_id"],
            "source_excerpt": EXCERPT,
            "issue_dimension": "dialogue_subtext",
        },
        actor_ref="writer",
    )
    assert result["candidate"]["generation_llm_call_id"]
    assert client.requests[-1].messages[0]["content"] == load_prompt_templates()["writer_passage_patch"].system_prompt


# ---------------------------------------------------------------------------
# 模板与预算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "version", "budget", "clause"),
    [
        (
            "scene_blueprint",
            "2026-09-15.v10",
            24000,
            "If a [STYLE_REFERENCE] block with [风格样例] is prepended, decide ending_action, information_release, image_anchor and anti_summary_rule",
        ),
        (
            "character_pressure_blueprint",
            "2026-09-14.v3",
            24000,
            '"without explanatory summary" applies only when no such section is present',
        ),
        ("chapter_story_architecture", "2026-09-12.v3", 24000, "[结构画像]"),
        ("author_proposal_generate", "2026-09-14.v3", 96000, "write the proposal in the reference author's hand"),
        ("writer_passage_patch", "2026-09-14.v3", 24000, "patch in the reference author's hand"),
        # 2026-09-22 场景诊断统一 v5：证据逐字引用 + 按雪花结构判断；风格条款不变
        ("writer_deep_review", "2026-09-22.v5", 30000, "judge in the reference author's hand"),
    ],
)
def test_wp6_templates_are_bumped_with_style_clauses(name: str, version: str, budget: int, clause: str) -> None:
    template = load_prompt_templates()[name]
    assert template.version == version
    assert template.input_token_budget == budget
    assert clause in template.task_prompt
    assert RUNTIME_MIN_INPUT_BUDGETS[name] == budget
    if name != "chapter_story_architecture":
        # 每条新条款都带红线：样例只学手法，不得复用
        assert "never reuse" in template.task_prompt or "never quote or reuse" in template.task_prompt


def test_wp6_template_details() -> None:
    templates = load_prompt_templates()
    blueprint = templates["scene_blueprint"]
    # 既有条款保留（结构跟随 / 阶段 A / 阶段 F 的测试仍在钉）
    assert "derive the proposal from it instead of re-inventing the scene" in blueprint.task_prompt
    assert "Structure Card ([结构画像]) or Scene Craft ([场景手法])" in blueprint.task_prompt
    assert "closing moves" in blueprint.task_prompt
    pressure = templates["character_pressure_blueprint"]
    assert "without explanatory summary" in pressure.system_prompt
    assert "pressure may become visible through narration, explanation or reflection where that author does so" in pressure.task_prompt
    review = templates["writer_deep_review"]
    assert "repetitive expression, image necessity and voice distinction are judged against what the reference author does" in review.task_prompt
    proposal = templates["author_proposal_generate"]
    assert "the [风格样例] settle questions of diction, imagery, sentence shapes, pauses and dialogue handling" in proposal.task_prompt
    patch = templates["writer_passage_patch"]
    assert "every replacement option must still sound like that author" in patch.task_prompt
    assert RUNTIME_MIN_INPUT_BUDGETS["author_proposal_generate"] == STYLE_PASS_INPUT_TOKEN_BUDGET == 96000
    assert PLANNING_STYLE_INPUT_TOKEN_BUDGET == 24000
