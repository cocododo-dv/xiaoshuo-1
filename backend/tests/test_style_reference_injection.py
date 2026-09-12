"""PR-8 §5.1 — InjectionService.fragments_for 4 种 strategy + 边界用例。"""

from __future__ import annotations

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.injection import (
    InjectionService,
    _metric_direction,
    _reference_sample_style_distance,
    _truncate_lines,
    fit_fragments_to_input_budget,
    ordered_character_ids,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import (
    InjectionStrategy,
    SystemPromptFragments,
)


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    clear_config_cache()
    yield
    clear_config_cache()


_STEMS = "甲乙丙丁戊己庚辛壬癸"


def _marker_line(marker: str, index: int) -> str:
    """含标记字、无数字的 ~14 字机制句,便于按标记字出现次数比较各层份额。"""
    return f"{marker}层要点{_STEMS[index % 10]}{_STEMS[(index // 10) % 10]}动作先于解释"


def _seed(
    *,
    seed: str,
    profile_json: dict | None = None,
    forbidden_findings: list[str] | None = None,
    strategy: str = "A",
    config_json: dict | None = None,
    project_id: str = "project_x",
    task_type: str = "scene_generation",
    scope: str = "project",
    profile_status: str = "active",
    extra_bindings: list[dict] | None = None,
) -> str:
    """落 1 个 book + run + profile + binding(可选 forbidden findings)。"""
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    profile_id = f"sr_profile_{seed}"
    binding_id = f"sr_bind_{seed}"

    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")

        extraction_id = f"sr_ext_{seed}"
        if forbidden_findings:
            repo.create_extraction(
                extraction_id=extraction_id, run_id=run_id, book_id=book_id,
                layer="language", sub_dimension="language.vocabulary",
                status="done", raw_payload_json={}, purpose="extract",
            )
        finding_ids: list[str] = []
        for i, stmt in enumerate(forbidden_findings or []):
            fid = f"sr_find_{seed}_{i}"
            repo.create_finding(
                finding_id=fid, book_id=book_id, run_id=run_id,
                extraction_id=extraction_id, sub_dimension="language.vocabulary",
                finding_kind="forbidden_pattern", statement=stmt,
                confidence="high", status="approved",
            )
            finding_ids.append(fid)

        repo.create_profile(
            profile_id=profile_id, book_id=book_id, run_id=run_id, title="t",
            status=profile_status,
            profile_json=profile_json or {},
            coverage_json={},
            source_finding_ids_json=finding_ids,
        )
        repo.create_binding(
            binding_id=binding_id, profile_id=profile_id,
            scope=scope, scope_ref_id=project_id if scope == "project" else None,
            task_type=task_type, strategy=strategy,
            config_json=config_json or {}, status="active",
        )
        for i, extra in enumerate(extra_bindings or []):
            repo.create_binding(
                binding_id=f"sr_bind_{seed}_extra_{i}",
                profile_id=profile_id,
                scope=extra.get("scope", "global"),
                scope_ref_id=extra.get("scope_ref_id"),
                task_type=extra.get("task_type", task_type),
                strategy=extra.get("strategy", "A"),
                config_json=extra.get("config_json") or {},
                status=extra.get("status", "active"),
            )
        session.commit()
    return project_id


def test_strategy_a_renders_all_three_blocks():
    project_id = _seed(
        seed="sa",
        profile_json={
            "narrative_summary": "白话短句,克制留白。",
            "style_features": ["短句频繁", "动词驱动"],
            "narrative_patterns": ["回环结构"],
            "banned_replication_rules": ["禁堆砌华丽形容词"],
            "metrics_baseline": {
                "avg_sentence_length": {"mean": 18.0, "std": 4.0},
            },
        },
        forbidden_findings=["禁使用美轮美奂等套话"],
        strategy="A",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.strategy == InjectionStrategy.A
    assert "正向风格特征" in fragments.positive_block
    # v2:量化断言只软化不整行删除;基线句均 18(长短句混合)不与「短句频繁」相反 → 保留
    assert "短句频繁" in fragments.positive_block
    assert "动词驱动" in fragments.positive_block
    # 旧画像没有 voice_signature → 不渲染 [声音特征]
    assert fragments.voice_block == ""
    assert "[声音特征]" not in fragments.to_system_prompt_prefix()
    assert "回环结构" in fragments.positive_block
    assert "禁堆砌华丽形容词" in fragments.forbidden_block
    assert "禁使用美轮美奂等套话" in fragments.forbidden_block
    assert "句均字数" in fragments.metric_anchor_block
    prefix = fragments.to_system_prompt_prefix()
    assert prefix.startswith("[STYLE_REFERENCE]\n")
    assert prefix.endswith("[/STYLE_REFERENCE]\n\n")


def test_metric_baseline_suppresses_conflicting_legacy_surface_guidance() -> None:
    project_id = _seed(
        seed="legacy_metric_conflict",
        profile_json={
            "narrative_summary": "短句密集并高频设问，但动作随后释放信息。",
            "style_features": [
                "大量使用短句和反问",
                "在转折处用短句切断长句",
                "让动作承担解释",
            ],
            "narrative_patterns": ["转折前先释放可见线索"],
            "calibration_guidance": ["若句号不够密集就继续断句", "若解释过多就改回动作"],
            "metrics_baseline": {
                "avg_sentence_length": {"mean": 19.0, "std": 2.0},
                "short_sentence_ratio": {"mean": 0.32, "std": 0.04},
                "question_density_per_1k": {"mean": 0.3, "std": 0.1},
            },
        },
        strategy="A",
    )

    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            project_id,
            "scene_generation",
        )

    # 与冻结基线方向相反的频率断言(设问高频 vs 基线问号极少)才丢弃
    assert "短句密集" not in fragments.positive_block
    assert "大量使用短句和反问" not in fragments.positive_block
    # v2:同向 / 方向未知的频率机制句不再整行删除(基线短句占比 0.32 = 较高频,同向)
    assert "若句号不够密集就继续断句" in fragments.positive_block
    assert "在转折处用短句切断长句" in fragments.positive_block
    assert "让动作承担解释" in fragments.positive_block
    assert "转折前先释放可见线索" in fragments.positive_block
    assert "若解释过多就改回动作" in fragments.positive_block
    assert "句均字数" in fragments.metric_anchor_block
    # 单一标点的精确密度不再暴露给生成器，避免为了命中统计而凑问号。
    assert "问号/千字" not in fragments.metric_anchor_block


def test_exact_internal_metric_summary_cannot_bypass_soft_guidance() -> None:
    project_id = _seed(
        seed="exact_summary_bypass",
        profile_json={
            "narrative_summary": (
                "量化基线（与定性描述冲突时以此为准）：句均约18.0字、"
                "短句约32%；段均约90.0字、每千字约10.0段。"
            ),
            "qualitative_summary": "信息经由人物动作逐步释放，判断适当后置。",
            "style_features": ["让动作承担解释"],
            "metrics_baseline": {
                "avg_sentence_length": {"mean": 18.0, "std": 2.0},
                "short_sentence_ratio": {"mean": 0.32, "std": 0.04},
                "paragraph_mean_chars": {"mean": 90.0, "std": 12.0},
                "paragraphs_per_1k": {"mean": 10.0, "std": 1.5},
            },
        },
        strategy="A",
    )

    with SessionLocal() as session:
        prefix = (
            InjectionService(session)
            .fragments_for(project_id, "scene_generation")
            .to_system_prompt_prefix()
        )

    assert "信息经由人物动作逐步释放" in prefix
    assert "量化基线" not in prefix
    assert "句均约18.0字" not in prefix
    assert "每千字约10.0段" not in prefix


def test_mixed_positive_budget_keeps_expression_narrative_and_calibration() -> None:
    project_id = _seed(
        seed="balanced_positive",
        profile_json={
            "narrative_summary": "克制而具体的叙述。" * 5,
            "style_features": ["表达机制甲：长短句交替。" * 3, "表达机制乙。" * 8],
            "narrative_patterns": ["叙事机制甲：动作之后再释放信息。" * 2, "叙事机制乙。" * 8],
            "calibration_guidance": ["校准机制甲：偏离时减少解释。" * 2, "校准机制乙。" * 8],
        },
        strategy="mixed",
        config_json={"intensity": 100},
    )

    with SessionLocal() as session:
        block = InjectionService(session).fragments_for(
            project_id, "scene_generation"
        ).positive_block

    assert len(block) <= 1080  # intensity=100:total 2400 × positive ratio 0.45
    assert "表达机制甲" in block
    assert "叙事机制甲" in block
    assert "校准机制甲" in block
    assert not block.rstrip().endswith(("风格要点:", "叙事模式:", "叙事模式："))


def test_line_truncation_drops_orphan_subsection_heading() -> None:
    block = "[正向风格特征]\n概述:动作推进。\n叙事模式:\n- 转折后留白。"

    truncated = _truncate_lines(block, block.index("- 转折"))

    assert truncated.endswith("概述:动作推进。")
    assert "叙事模式" not in truncated


def test_line_truncation_has_no_character_level_fallback() -> None:
    """v2:预算装不下一整行时宁可少一行,不发半句;只剩标题时整块置空。"""
    block = "[正向风格特征]\n- " + "很长的单条规则" * 60
    assert _truncate_lines(block, 200) == ""
    two = "[禁忌模式]\n- 第一条禁忌\n- 第二条禁忌很长很长" + "很长" * 30
    truncated = _truncate_lines(two, 40)
    assert truncated == "[禁忌模式]\n- 第一条禁忌"
    assert _truncate_lines("[风格分布指导｜只控制整体倾向]", 5) == ""
    assert _truncate_lines("短块", 100) == "短块"


def test_reference_sample_distance_rejects_punctuation_outlier() -> None:
    baseline = {
        "paragraph_mean_chars": {"mean": 90.0, "std": 15.0},
        "paragraphs_per_1k": {"mean": 11.0, "std": 2.0},
        "avg_sentence_length": {"mean": 22.0, "std": 4.0},
        "short_sentence_ratio": {"mean": 0.15, "std": 0.05},
        "punctuation_density_per_1k": {"mean": 135.0, "std": 15.0},
        "question_density_per_1k": {"mean": 1.0, "std": 1.0},
        "semicolon_density_per_1k": {"mean": 8.0, "std": 2.0},
        "classical_word_ratio": {"mean": 0.08, "std": 0.03},
        "colloquial_marker_ratio": {"mean": 0.02, "std": 0.02},
    }
    representative = (
        "他沿着河岸慢慢走去，衣角沾了薄雾；远处的灯映在水上，"
        "一层一层散开。他停住片刻，又将未说的话收了回去。"
    )
    outlier = (
        "他为什么来？为什么停？为什么又走？谁知道呢！难道不是这样吗？"
        "可他究竟等什么？又怕什么？谁能说清呢！"
    )

    assert _reference_sample_style_distance(
        representative, baseline
    ) < _reference_sample_style_distance(outlier, baseline)


def test_final_input_budget_trims_metric_tail_before_style_or_safety_blocks():
    fragments = SystemPromptFragments(
        positive_block=(
            "[正向风格特征]\n概述:动作推进。\n风格要点:\n"
            "- 使用具体动词。\n- 让短句承担转折。"
        ),
        forbidden_block=(
            "[禁忌模式]\n- 不总结主题。\n- 不复用同一意象。"
        ),
        metric_anchor_block=(
            "[量化锚点]\n"
            + "\n".join(
                f"- 指标{i}:" + ("适度调整节奏" * 8) for i in range(6)
            )
        ),
        anti_plagiarism_block=(
            "## 严格禁止\n- 不得复用参考原文完整句子。"
        ),
        strategy=InjectionStrategy.A,
    )
    full_prefix = fragments.to_system_prompt_prefix()
    from novel_system.services.context_budget import estimate_tokens

    full_tokens = estimate_tokens(full_prefix + "BASE") + estimate_tokens("正文")
    fitted, audit = fit_fragments_to_input_budget(
        fragments,
        base_system_prompt="BASE",
        user_prompt="正文",
        target_input_tokens=full_tokens - 30,
    )

    assert audit["compacted"] is True
    assert audit["policy"] == "trim_metric_tail_preserve_style_and_safety_v1"
    assert audit["final_estimated_input_tokens"] <= audit["target_input_tokens"]
    assert fitted.positive_block == fragments.positive_block
    assert fitted.forbidden_block == fragments.forbidden_block
    assert fitted.anti_plagiarism_block == fragments.anti_plagiarism_block
    assert len(fitted.metric_anchor_block) < len(fragments.metric_anchor_block)
    assert audit["anti_plagiarism_preserved"] is True
    prefix = fitted.to_system_prompt_prefix()
    assert prefix.startswith("[STYLE_REFERENCE]\n")
    assert prefix.endswith("[/STYLE_REFERENCE]\n\n")


def test_metric_guidance_uses_soft_reference_distributions_without_quotas():
    project_id = _seed(
        seed="metric_delta",
        profile_json={
            "narrative_summary": "克制叙述。",
            "style_features": ["长短句交替"],
            "metrics_baseline": {
                # 故意把段型指标放在最前，防止回归到 dict 前八项截取。
                "dialogue_ratio": {"mean": 0.8, "std": 0.1},
                "action_ratio": {"mean": 0.7, "std": 0.1},
                "paragraph_mean_chars": {"mean": 90.0, "std": 12.0},
                "paragraph_length_std_chars": {"mean": 45.0, "std": 8.0},
                "paragraphs_per_1k": {"mean": 10.0, "std": 1.5},
                "single_sentence_paragraph_ratio": {"mean": 0.2, "std": 0.05},
                "quote_led_paragraph_ratio": {"mean": 0.1, "std": 0.03},
                "avg_sentence_length": {"mean": 8.0, "std": 1.0},
                "short_sentence_ratio": {"mean": 0.7, "std": 0.1},
                "punctuation_density_per_1k": {"mean": 150.0, "std": 10.0},
                "semicolon_density_per_1k": {"mean": 9.0, "std": 1.0},
                "ellipsis_density_per_1k": {"mean": 0.0, "std": 0.1},
                "classical_word_ratio": {"mean": 0.1, "std": 0.02},
                "colloquial_marker_ratio": {"mean": 0.03, "std": 0.01},
                "metaphor_density_per_1k": {"mean": 1.0, "std": 0.2},
                "sensory_auditory_per_1k": {"mean": 3.0, "std": 0.5},
            },
        },
        strategy="A",
    )
    with SessionLocal() as session:
        service = InjectionService(session)
        service.context_text = (
            "雨敲着窗。她听见门外有人停住，又走开。"
            "桌上的信没有拆，灯光把纸边照得发白。"
            "她把手从门把上收回来，等那阵脚步彻底消失。"
        )
        fragments = service.fragments_for(project_id, "scene_generation")

    block = fragments.metric_anchor_block
    assert "dialogue_ratio" not in block
    assert "action_ratio" not in block
    assert "风格分布指导" in block
    assert "段落组织" in block
    assert "句均字数" in block
    assert "标点/千字" in block
    assert "分号/千字" not in block
    assert "当前" in block
    assert "不追求固定段数" in block
    assert "不得为命中统计" in block
    assert "当前约" not in block
    assert "目标约" not in block
    assert "全文约" not in block
    assert "控制在" not in block


def test_metric_direction_returns_qualitative_actions_without_scene_quotas():
    paragraph = _metric_direction(
        "paragraph_mean_chars",
        current=40.0,
        target=197.6,
        std=12.0,
        context_chars=834,
    )
    semicolon = _metric_direction(
        "semicolon_density_per_1k",
        current=0.0,
        target=4.87,
        std=1.0,
        context_chars=834,
    )

    assert "合并同一叙事单元" in paragraph
    assert "禁逐句分段" in paragraph
    assert semicolon == "适量增加分号并列"
    assert not any(char.isdigit() for char in paragraph + semicolon)


def test_rejected_forbidden_finding_not_injected():
    """PR-23 — profile 引用 2 条 forbidden finding,其中 1 条 rejected → prefix 只含未驳回那条。"""
    project_id = _seed(
        seed="rej",
        profile_json={
            "narrative_summary": "s",
            "style_features": ["f"],
        },
        forbidden_findings=["未驳回的禁忌描述", "已驳回的禁忌描述"],
        strategy="A",
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.update_finding("sr_find_rej_1", status="rejected")
        session.commit()

    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    prefix = fragments.to_system_prompt_prefix()
    assert "未驳回的禁忌描述" in prefix
    assert "已驳回的禁忌描述" not in prefix


def test_generation_safe_forbidden_findings_replace_raw_source_findings():
    project_id = _seed(
        seed="safe_forbidden",
        profile_json={
            "narrative_summary": "s",
            "style_features": ["f"],
            "generation_safe_forbidden_findings": [
                {
                    "finding_id": "safe_1",
                    "sub_dimension": "language.vocabulary",
                    "statement": "只注入脱离原文的抽象禁忌",
                    "status": "approved",
                }
            ],
            "source_overlap_filter": {"applied": True, "threshold_chars": 8},
        },
        forbidden_findings=["这条原始发现包含不应进入提示的参考原句"],
        strategy="A",
    )

    with SessionLocal() as session:
        prefix = (
            InjectionService(session)
            .fragments_for(project_id, "scene_generation")
            .to_system_prompt_prefix()
        )

    assert "只注入脱离原文的抽象禁忌" in prefix
    assert "这条原始发现包含不应进入提示的参考原句" not in prefix


def test_strategy_b_truncates_by_budget():
    long_features = ["特点描述" * 200]  # 单条 800 字
    project_id = _seed(
        seed="sb",
        profile_json={
            "narrative_summary": "summary",
            "style_features": long_features,
            "banned_replication_rules": ["rule" * 200],
            "metrics_baseline": {"avg_sentence_length": {"mean": 18.0, "std": 4.0}},
        },
        strategy="B",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.strategy == InjectionStrategy.B
    # 2026-09-12 最大化模仿:未写 intensity 默认 100 → total = 2400;
    # 四块按 0.45 / 0.20 / 0.15 / 0.20 切,整行边界截断(单条 800 字的超长行整行丢弃)。
    assert len(fragments.positive_block) <= 1080
    assert len(fragments.forbidden_block) <= 480
    assert len(fragments.metric_anchor_block) <= 360
    assert not fragments.positive_block.endswith("…")


def test_strategy_c_drops_metric_and_summarizes_forbidden():
    project_id = _seed(
        seed="sc",
        profile_json={
            "narrative_summary": "summary",
            "style_features": ["要点 1"],
            "banned_replication_rules": ["规则" * 200],  # 800 字
            "metrics_baseline": {"avg_sentence_length": {"mean": 18.0, "std": 4.0}},
        },
        strategy="C",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.strategy == InjectionStrategy.C
    assert fragments.positive_block  # 全文保留
    assert len(fragments.forbidden_block) <= 200 + 2  # 摘要 200 字
    assert fragments.metric_anchor_block == ""


def test_strategy_mixed_respects_config_switches():
    project_id = _seed(
        seed="sm",
        profile_json={
            "narrative_summary": "summary",
            "style_features": ["要点"],
            "banned_replication_rules": ["规则"],
            "metrics_baseline": {"avg_sentence_length": {"mean": 18.0, "std": 4.0}},
        },
        strategy="mixed",
        config_json={
            "include_positive": True,
            "include_forbidden": False,
            "include_metric": True,
        },
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.strategy == InjectionStrategy.MIXED
    assert fragments.positive_block
    assert fragments.forbidden_block == ""
    assert fragments.metric_anchor_block


def test_strategy_mixed_includes_metric_by_default():
    project_id = _seed(
        seed="sm_metric_default",
        profile_json={
            "narrative_summary": "summary",
            "style_features": ["要点"],
            "metrics_baseline": {
                "paragraph_mean_chars": {"mean": 90.0, "std": 10.0}
            },
        },
        strategy="mixed",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            project_id, "scene_generation"
        )
    assert "段均字数" in fragments.metric_anchor_block


def test_empty_when_no_binding():
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for("no_such_project", "scene_generation")
    assert fragments.positive_block == ""
    assert fragments.forbidden_block == ""
    assert fragments.metric_anchor_block == ""
    assert fragments.to_system_prompt_prefix() == ""


def test_empty_when_profile_not_active():
    project_id = _seed(
        seed="inactive",
        profile_json={"narrative_summary": "x", "style_features": ["y"]},
        strategy="A",
        profile_status="draft",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.to_system_prompt_prefix() == ""


def test_profile_missing_fields_yields_empty_blocks():
    project_id = _seed(
        seed="empty",
        profile_json={},  # 全空
        strategy="A",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.positive_block == ""
    assert fragments.forbidden_block == ""
    assert fragments.metric_anchor_block == ""
    assert fragments.to_system_prompt_prefix() == ""


def test_project_binding_wins_over_global():
    """同一 task_type 同时有 project 与 global binding 时,project 优先。"""
    project_id = _seed(
        seed="prio",
        profile_json={"narrative_summary": "n", "style_features": ["project 专属要点"]},
        strategy="A",
        scope="project",
        extra_bindings=[
            {
                "scope": "global",
                "scope_ref_id": None,
                "task_type": "scene_generation",
                "strategy": "C",
            }
        ],
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.strategy == InjectionStrategy.A
    assert "project 专属要点" in fragments.positive_block


def test_mixed_intensity_scales_block_lengths():
    """v2 §1.5 — intensity 决定抽象总额:0 → 900 / 50 → 1650 / 100 → 2400,按 ratio 切块。"""
    stems = "甲乙丙丁戊己庚辛壬癸"
    # 60 条 ~18 字的表达机制(≈1140 字)与 30 条禁忌(≈450 字):三档都会被整行截断
    long_features = [f"要点{stems[i % 10]}{stems[i // 10]}动作先于解释" for i in range(60)]
    rules = [f"禁{stems[i % 10]}{stems[i // 10]}排比抒情堆叠" for i in range(30)]
    common = {
        "profile_json": {
            "narrative_summary": "summary",
            "style_features": long_features,
            "banned_replication_rules": rules,
            "metrics_baseline": {"m1": {"mean": 1.0, "std": 0.1}},
        },
        "strategy": "mixed",
    }
    proj_low = _seed(
        seed="low",
        config_json={"intensity": 0, "include_metric": True},
        project_id="proj_intensity_low",
        **common,
    )
    proj_mid = _seed(
        seed="mid",
        config_json={"intensity": 50, "include_metric": True},
        project_id="proj_intensity_mid",
        **common,
    )
    proj_hi = _seed(
        seed="hi",
        config_json={"intensity": 100, "include_metric": True},
        project_id="proj_intensity_hi",
        **common,
    )
    with SessionLocal() as session:
        svc = InjectionService(session)
        low = svc.fragments_for(proj_low, "scene_generation")
        mid = svc.fragments_for(proj_mid, "scene_generation")
        hi = svc.fragments_for(proj_hi, "scene_generation")
    # positive_block 长度应单调递增:low < mid < hi
    assert len(low.positive_block) < len(mid.positive_block) < len(hi.positive_block)
    # forbidden_block 同理
    assert len(low.forbidden_block) < len(mid.forbidden_block) < len(hi.forbidden_block)
    # 各档上限 = total(i) × ratio:positive 405 / 742 / 1080,forbidden 180 / 330 / 480
    assert len(low.positive_block) <= 405
    assert len(mid.positive_block) <= 742
    assert len(hi.positive_block) <= 1080
    assert len(hi.forbidden_block) <= 480
    # 整行截断:没有半截条目,也没有孤立标题
    for frag in (low, mid, hi):
        for block in (frag.positive_block, frag.forbidden_block):
            lines = block.splitlines()
            assert lines and not lines[-1].rstrip().endswith(("]", ":", "："))
            assert all(len(line) < 40 for line in lines if line.startswith("- "))


def test_mixed_sub_dimensions_filters_forbidden_findings():
    """PR-9 §"sub_dim 过滤" — MIXED 时 config["sub_dimensions"] 只取匹配的 forbidden_pattern。"""
    project_id = _seed(
        seed="subdim_miss",
        profile_json={"narrative_summary": "n", "style_features": ["短"]},
        forbidden_findings=["禁堆叠形容词"],  # sub_dimension=language.vocabulary
        strategy="mixed",
        config_json={
            # 只选 narrative 层 — language 层的 finding 应被过滤掉
            "sub_dimensions": ["narrative.pacing", "narrative.perspective"],
        },
        project_id="proj_subdim_miss",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    # banned_replication_rules 无,findings 因 sub_dim 不匹配被过滤 → forbidden_block 为空
    assert fragments.forbidden_block == ""

    # 对比:sub_dimensions 含 language.vocabulary 时应保留
    project_id_2 = _seed(
        seed="subdim_hit",
        profile_json={"narrative_summary": "n", "style_features": ["短"]},
        forbidden_findings=["禁堆叠形容词"],
        strategy="mixed",
        config_json={"sub_dimensions": ["language.vocabulary"]},
        project_id="proj_subdim_hit",
    )
    with SessionLocal() as session:
        fragments2 = InjectionService(session).fragments_for(project_id_2, "scene_generation")
    assert "禁堆叠形容词" in fragments2.forbidden_block


def test_to_system_prompt_prefix_ordering():
    """positive → forbidden → 软分布(2026-09-09 样例优先:量化分布退到抽象块之后)。"""
    project_id = _seed(
        seed="order",
        profile_json={
            "narrative_summary": "concept_a",
            "banned_replication_rules": ["concept_b"],
            "metrics_baseline": {"m1": {"mean": 1.0, "std": 0.1}},
        },
        strategy="A",
    )
    with SessionLocal() as session:
        prefix = InjectionService(session).fragments_for(project_id, "scene_generation").to_system_prompt_prefix()
    pos_idx = prefix.index("正向风格特征")
    forbid_idx = prefix.index("禁忌模式")
    metric_idx = prefix.index("风格分布指导")
    assert pos_idx < forbid_idx < metric_idx


# ---------------------------------------------------------------------------
# PR-14 — character scope binding(优先级单选 character > project > global)
# ---------------------------------------------------------------------------


def _seed_scoped_bindings(
    *,
    seed: str,
    project_id: str,
    character_id: str,
    char_feature: str = "角色专属特征",
    project_feature: str = "项目通用特征",
    char_status: str = "active",
    include_character: bool = True,
) -> None:
    """落 1 book/run + 2 profile(character / project)+ 对应 2 binding。

    两 profile 的 style_features 不同,便于断言注入命中哪个。
    """
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        # project profile + binding
        repo.create_profile(
            profile_id=f"sr_profile_{seed}_proj", book_id=book_id, run_id=run_id, title="proj",
            status="active",
            profile_json={"narrative_summary": "n", "style_features": [project_feature]},
            coverage_json={}, source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=f"sr_bind_{seed}_proj", profile_id=f"sr_profile_{seed}_proj",
            scope="project", scope_ref_id=project_id,
            task_type="scene_generation", strategy="A", config_json={}, status="active",
        )
        if include_character:
            repo.create_profile(
                profile_id=f"sr_profile_{seed}_char", book_id=book_id, run_id=run_id, title="char",
                status="active",
                profile_json={"narrative_summary": "n", "style_features": [char_feature]},
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_{seed}_char", profile_id=f"sr_profile_{seed}_char",
                scope="character", scope_ref_id=character_id,
                task_type="scene_generation", strategy="A", config_json={}, status=char_status,
            )
        session.commit()


def test_character_overlay_merges_with_project_base():
    """PR-16 — character(overlay)叠加 project(base):两层 positive 都注入。"""
    _seed_scoped_bindings(
        seed="charwin", project_id="proj_cw", character_id="char_cw",
        char_feature="角色专属特征CW", project_feature="项目通用特征CW",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_cw", "scene_generation", character_ids=["char_cw"],
        )
    # 两层叠加:overlay(character)+ base(project)均在
    assert "角色专属特征CW" in fragments.positive_block
    assert "项目通用特征CW" in fragments.positive_block


def test_character_id_mismatch_falls_back_to_project():
    _seed_scoped_bindings(
        seed="charmiss", project_id="proj_cm", character_id="char_cm",
        char_feature="角色专属特征CM", project_feature="项目通用特征CM",
    )
    with SessionLocal() as session:
        # 传一个不匹配任何 character binding 的 character_id
        fragments = InjectionService(session).fragments_for(
            "proj_cm", "scene_generation", character_ids=["char_other"],
        )
    assert "项目通用特征CM" in fragments.positive_block
    assert "角色专属特征CM" not in fragments.positive_block


def test_character_id_none_skips_character_binding():
    _seed_scoped_bindings(
        seed="charnone", project_id="proj_cn", character_id="char_cn",
        char_feature="角色专属特征CN", project_feature="项目通用特征CN",
    )
    with SessionLocal() as session:
        # character_id=None(默认)→ 跳过 character rank,回落 project
        fragments = InjectionService(session).fragments_for("proj_cn", "scene_generation")
    assert "项目通用特征CN" in fragments.positive_block
    assert "角色专属特征CN" not in fragments.positive_block


def test_disabled_character_binding_falls_back_to_project():
    _seed_scoped_bindings(
        seed="chardis", project_id="proj_cd", character_id="char_cd",
        char_feature="角色专属特征CD", project_feature="项目通用特征CD",
        char_status="disabled",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_cd", "scene_generation", character_ids=["char_cd"],
        )
    # character binding disabled → 不命中,回落 project
    assert "项目通用特征CD" in fragments.positive_block
    assert "角色专属特征CD" not in fragments.positive_block


# ---------------------------------------------------------------------------
# PR-15 — scene scope binding(优先级单选 scene > character > project > global)
# ---------------------------------------------------------------------------


def _seed_four_level_bindings(
    *,
    seed: str,
    project_id: str,
    character_id: str,
    scene_id: str,
    include_scene: bool = True,
    scene_feature: str = "场景专属特征",
    char_feature: str = "角色专属特征",
    project_feature: str = "项目通用特征",
) -> None:
    """落 1 book/run + 3 profile(scene/character/project)+ 对应 binding。

    profile style_features 各异,便于断言命中哪个。
    """
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")

        def _mk(suffix, feature, scope, ref):
            repo.create_profile(
                profile_id=f"sr_profile_{seed}_{suffix}", book_id=book_id, run_id=run_id,
                title=suffix, status="active",
                profile_json={"narrative_summary": "n", "style_features": [feature]},
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_{seed}_{suffix}", profile_id=f"sr_profile_{seed}_{suffix}",
                scope=scope, scope_ref_id=ref,
                task_type="scene_generation", strategy="A", config_json={}, status="active",
            )

        _mk("proj", project_feature, "project", project_id)
        _mk("char", char_feature, "character", character_id)
        if include_scene:
            _mk("scene", scene_feature, "scene", scene_id)
        session.commit()


def test_three_layer_full_merge():
    """PR-19 — 三层全叠:scene + character + project 都注入(PR-16 曾跳过 character)。"""
    _seed_four_level_bindings(
        seed="scenewin", project_id="proj_sw", character_id="char_sw", scene_id="scene_sw",
        scene_feature="场景专属SW", char_feature="角色专属SW", project_feature="项目通用SW",
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_sw", "scene_generation", character_ids=["char_sw"], scene_id="scene_sw",
        )
    pos = fragments.positive_block
    # 三层全叠:base(project)+ character + scene 都在
    assert "项目通用SW" in pos
    assert "角色专属SW" in pos
    assert "场景专属SW" in pos


def test_three_layer_order_general_to_specific():
    """PR-19 — 拼接顺序由泛到具体:project → character → scene。"""
    _seed_four_level_bindings(
        seed="order3", project_id="proj_o3", character_id="char_o3", scene_id="scene_o3",
        scene_feature="场景O3", char_feature="角色O3", project_feature="项目O3",
    )
    with SessionLocal() as session:
        pos = InjectionService(session).fragments_for(
            "proj_o3", "scene_generation", character_ids=["char_o3"], scene_id="scene_o3",
        ).positive_block
    assert pos.index("项目O3") < pos.index("角色O3") < pos.index("场景O3")


def test_three_layer_token_weighted_scene_largest():
    """PR-19 — token 加权:scene 段预算最大(权重 3/6),project 基底最小(1/6)。"""
    # 手工 seed 多条 style_features(各层 ~50 条 × ~14 字),确保都被 cap 整行截断,验证加权差异
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id="sr_book_w3b", title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum="chk_w3b", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id="sr_run_w3b", book_id="sr_book_w3b", status="done", phase="done")
        for suffix, scope, ref, feat in [
            ("proj", "project", "proj_w3b", "项"),
            ("char", "character", "char_w3b", "角"),
            ("scene", "scene", "scene_w3b", "景"),
        ]:
            repo.create_profile(
                profile_id=f"sr_profile_w3b_{suffix}", book_id="sr_book_w3b", run_id="sr_run_w3b",
                title=suffix, status="active",
                profile_json={
                    "narrative_summary": "n",
                    "style_features": [_marker_line(feat, i) for i in range(50)],
                },
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_w3b_{suffix}", profile_id=f"sr_profile_w3b_{suffix}",
                scope=scope, scope_ref_id=ref,
                task_type="scene_generation", strategy="A", config_json={}, status="active",
            )
        session.commit()
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_w3b", "scene_generation", character_ids=["char_w3b"], scene_id="scene_w3b",
        )
    pos = fragments.positive_block
    # scene 段(景)应比 project 段(项)长(加权 3:1)
    assert pos.count("景") > pos.count("项")


def test_three_layer_metric_prefers_most_specific():
    """PR-19 — metric_anchor 取最具体层(scene)优先。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id="sr_book_m3", title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum="chk_m3", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id="sr_run_m3", book_id="sr_book_m3", status="done", phase="done")
        for suffix, scope, ref, metric_name in [
            ("proj", "project", "proj_m3", "proj_metric"),
            ("scene", "scene", "scene_m3", "scene_metric"),
        ]:
            repo.create_profile(
                profile_id=f"sr_profile_m3_{suffix}", book_id="sr_book_m3", run_id="sr_run_m3",
                title=suffix, status="active",
                profile_json={
                    "narrative_summary": "n",
                    "metrics_baseline": {metric_name: {"mean": 1.0, "std": 0.1}},
                },
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_m3_{suffix}", profile_id=f"sr_profile_m3_{suffix}",
                scope=scope, scope_ref_id=ref,
                task_type="scene_generation", strategy="A", config_json={}, status="active",
            )
        session.commit()
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_m3", "scene_generation", scene_id="scene_m3",
        )
    assert "scene_metric" in fragments.metric_anchor_block
    assert "proj_metric" not in fragments.metric_anchor_block


def test_scene_id_mismatch_falls_back_to_character():
    _seed_four_level_bindings(
        seed="scenemiss", project_id="proj_sm", character_id="char_sm", scene_id="scene_sm",
        scene_feature="场景专属SM", char_feature="角色专属SM", project_feature="项目通用SM",
    )
    with SessionLocal() as session:
        # scene_id 不匹配任何 scene binding → 回落 character
        fragments = InjectionService(session).fragments_for(
            "proj_sm", "scene_generation", character_ids=["char_sm"], scene_id="scene_other",
        )
    assert "角色专属SM" in fragments.positive_block
    assert "场景专属SM" not in fragments.positive_block


def test_scene_id_none_skips_scene_binding():
    _seed_four_level_bindings(
        seed="scenenone", project_id="proj_sn", character_id="char_sn", scene_id="scene_sn",
        scene_feature="场景专属SN", char_feature="角色专属SN", project_feature="项目通用SN",
    )
    with SessionLocal() as session:
        # scene_id=None → 跳过 scene rank,overlay=character;叠加 project base
        fragments = InjectionService(session).fragments_for(
            "proj_sn", "scene_generation", character_ids=["char_sn"],
        )
    assert "角色专属SN" in fragments.positive_block
    assert "场景专属SN" not in fragments.positive_block


# ---------------------------------------------------------------------------
# PR-16 — 两层叠加合并(forbidden 去重 / token 各半 / 单层等价 / metric 优先)
# ---------------------------------------------------------------------------


def _seed_overlay_pair(
    *,
    seed: str,
    project_id: str,
    character_id: str,
    base_json: dict,
    overlay_json: dict,
    base_strategy: str = "A",
    overlay_strategy: str = "A",
    include_base: bool = True,
    include_overlay: bool = True,
) -> None:
    """落 project(base)+ character(overlay)两 profile/binding,profile_json 自定义。"""
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        if include_base:
            repo.create_profile(
                profile_id=f"sr_profile_{seed}_base", book_id=book_id, run_id=run_id,
                title="base", status="active", profile_json=base_json,
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_{seed}_base", profile_id=f"sr_profile_{seed}_base",
                scope="project", scope_ref_id=project_id,
                task_type="scene_generation", strategy=base_strategy, config_json={}, status="active",
            )
        if include_overlay:
            repo.create_profile(
                profile_id=f"sr_profile_{seed}_ov", book_id=book_id, run_id=run_id,
                title="ov", status="active", profile_json=overlay_json,
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_{seed}_ov", profile_id=f"sr_profile_{seed}_ov",
                scope="character", scope_ref_id=character_id,
                task_type="scene_generation", strategy=overlay_strategy, config_json={}, status="active",
            )
        session.commit()


def test_overlay_forbidden_dedup():
    """两层相同禁忌规则去重,只保留一条。"""
    _seed_overlay_pair(
        seed="dedup", project_id="proj_dd", character_id="char_dd",
        base_json={"narrative_summary": "b", "banned_replication_rules": ["禁堆砌华丽形容词", "禁项目独有"]},
        overlay_json={"narrative_summary": "o", "banned_replication_rules": ["禁堆砌华丽形容词", "禁角色独有"]},
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_dd", "scene_generation", character_ids=["char_dd"],
        )
    forbidden = fragments.forbidden_block
    # 共同的"禁堆砌华丽形容词"只出现一次
    assert forbidden.count("禁堆砌华丽形容词") == 1
    # 各自独有的都保留
    assert "禁项目独有" in forbidden
    assert "禁角色独有" in forbidden
    # 单个 [禁忌模式] 标题
    assert forbidden.count("[禁忌模式]") == 1


def test_overlay_token_each_half_capped():
    """base 与 overlay positive 各被截到自己的份额内(v2:总额 ×1.35,权重 1:2)。"""
    long_base = [_marker_line("基", i) for i in range(60)]      # ≈ 1000 字
    long_overlay = [_marker_line("增", i) for i in range(60)]   # ≈ 1000 字
    _seed_overlay_pair(
        seed="cap", project_id="proj_cap", character_id="char_cap",
        base_json={"narrative_summary": "b", "style_features": long_base},
        overlay_json={"narrative_summary": "o", "style_features": long_overlay},
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_cap", "scene_generation", character_ids=["char_cap"],
        )
    # intensity 默认 100:total 2400 × 1.35 = 3240;base 份额 1080 → positive 486,
    # overlay 份额 2160 → positive 972;合并后不超过两份之和(+换行余量)
    assert len(fragments.positive_block) <= 486 + 972 + 10
    assert fragments.positive_block.count("基") < fragments.positive_block.count("增")


def test_overlay_only_base_is_single_layer():
    """仅 project base(无 overlay)→ 单层路径:按自身 intensity 总额截断,不按多层份额 cap。"""
    long_base = [_marker_line("基", i) for i in range(120)]  # ≈ 2000 字
    _seed_overlay_pair(
        seed="baseonly", project_id="proj_bo", character_id="char_bo",
        base_json={"narrative_summary": "b", "style_features": long_base},
        overlay_json={},
        include_overlay=False,
    )
    with SessionLocal() as session:
        # 无 character binding → overlay=None → 单层 base
        fragments = InjectionService(session).fragments_for(
            "proj_bo", "scene_generation", character_ids=["char_bo"],
        )
    # 单层 strategy A:positive ≤ 1080(intensity 100 的 positive 份额),但远超多层 base 份额 486
    assert 486 < len(fragments.positive_block) <= 1080


def test_overlay_only_character_is_single_layer():
    """仅 character overlay(无 project base)→ 单层路径。"""
    _seed_overlay_pair(
        seed="ovonly", project_id="proj_oo", character_id="char_oo",
        base_json={},
        overlay_json={"narrative_summary": "o", "style_features": ["角色独有OO"]},
        include_base=False,
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_oo", "scene_generation", character_ids=["char_oo"],
        )
    assert "角色独有OO" in fragments.positive_block


def test_overlay_metric_prefers_overlay():
    """metric_anchor 取 overlay(更具体);overlay 有 metric 时不用 base 的。"""
    _seed_overlay_pair(
        seed="metric", project_id="proj_mt", character_id="char_mt",
        base_json={
            "narrative_summary": "b",
            "metrics_baseline": {"base_metric": {"mean": 10.0, "std": 1.0}},
        },
        overlay_json={
            "narrative_summary": "o",
            "metrics_baseline": {"overlay_metric": {"mean": 20.0, "std": 2.0}},
        },
    )
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_mt", "scene_generation", character_ids=["char_mt"],
        )
    assert "overlay_metric" in fragments.metric_anchor_block
    assert "base_metric" not in fragments.metric_anchor_block


# ---------------------------------------------------------------------------
# PR-18 — onstage 多角色 character 匹配(pov ∪ onstage,pov 优先决平)
# ---------------------------------------------------------------------------


def _seed_multi_character(
    *,
    seed: str,
    project_id: str,
    characters: list[dict],
    project_feature: str = "项目通用M",
) -> None:
    """落 1 book/run + 1 project binding + 多个 character binding。

    characters: [{character_id, feature, created_at?}]。
    """
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=f"sr_profile_{seed}_proj", book_id=book_id, run_id=run_id, title="proj",
            status="active",
            profile_json={"narrative_summary": "n", "style_features": [project_feature]},
            coverage_json={}, source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=f"sr_bind_{seed}_proj", profile_id=f"sr_profile_{seed}_proj",
            scope="project", scope_ref_id=project_id,
            task_type="scene_generation", strategy="A", config_json={}, status="active",
        )
        for i, ch in enumerate(characters):
            repo.create_profile(
                profile_id=f"sr_profile_{seed}_c{i}", book_id=book_id, run_id=run_id, title=f"c{i}",
                status="active",
                profile_json={"narrative_summary": "n", "style_features": [ch["feature"]]},
                coverage_json={}, source_finding_ids_json=[],
            )
            kwargs = dict(
                binding_id=f"sr_bind_{seed}_c{i}", profile_id=f"sr_profile_{seed}_c{i}",
                scope="character", scope_ref_id=ch["character_id"],
                task_type="scene_generation", strategy="A", config_json={}, status="active",
            )
            if ch.get("created_at"):
                kwargs["created_at"] = ch["created_at"]
            repo.create_binding(**kwargs)
        session.commit()


def test_ordered_character_ids_helper():
    # pov 排首 + onstage 去重(pov 可能不在 onstage)
    assert ordered_character_ids("A", ["B", "C"]) == ["A", "B", "C"]
    assert ordered_character_ids("A", ["A", "B"]) == ["A", "B"]      # 去重
    assert ordered_character_ids(None, ["B", "C"]) == ["B", "C"]     # pov 空
    assert ordered_character_ids("A", []) == ["A"]                   # onstage 空
    assert ordered_character_ids("A", ["B"]) == ["A", "B"]           # pov 不在 onstage


def test_onstage_nonpov_character_matches():
    """pov 无 binding,但 onstage 配角有 binding → 命中配角。"""
    _seed_multi_character(
        seed="onstage1", project_id="proj_os1",
        characters=[{"character_id": "charB", "feature": "配角专属OS1"}],
    )
    # pov=charNoBind(无 binding),onstage=[charB]
    cids = ordered_character_ids("charNoBind", ["charB"])
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_os1", "scene_generation", character_ids=cids,
        )
    assert "配角专属OS1" in fragments.positive_block


def test_pov_and_onstage_characters_both_merged():
    """PR-20 — pov 与 onstage 配角都有 binding → 两层全叠,pov 排在配角前(PR-19 曾单选只注 pov)。"""
    _seed_multi_character(
        seed="onstage2", project_id="proj_os2",
        characters=[
            {"character_id": "charA", "feature": "主视角OS2"},
            {"character_id": "charB", "feature": "配角OS2"},
        ],
    )
    cids = ordered_character_ids("charA", ["charB"])  # [charA, charB]
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_os2", "scene_generation", character_ids=cids,
        )
    pos = fragments.positive_block
    # 多配角全叠:pov(charA)与配角(charB)都注入
    assert "主视角OS2" in pos
    assert "配角OS2" in pos
    # pov 配角层排在非 pov 配角层之前(char_order)
    assert pos.index("主视角OS2") < pos.index("配角OS2")


def test_pov_not_in_onstage_union_matches():
    """pov 不在 onstage 列表里,但并集仍命中 pov binding。"""
    _seed_multi_character(
        seed="onstage3", project_id="proj_os3",
        characters=[{"character_id": "charA", "feature": "主视角OS3"}],
    )
    # pov=charA 不在 onstage=[charB];并集 [charA, charB]
    cids = ordered_character_ids("charA", ["charB"])
    assert cids == ["charA", "charB"]
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_os3", "scene_generation", character_ids=cids,
        )
    assert "主视角OS3" in fragments.positive_block


def test_pov_priority_orders_before_onstage_despite_created_at():
    """PR-20 — pov binding 创建较早、配角较新;两者全叠,但 char_order(pov 优先)盖过 created_at 排序。"""
    _seed_multi_character(
        seed="onstage4", project_id="proj_os4",
        characters=[
            {"character_id": "charA", "feature": "主视角OS4", "created_at": "2026-01-01T00:00:00Z"},
            {"character_id": "charB", "feature": "配角OS4", "created_at": "2026-05-01T00:00:00Z"},
        ],
    )
    cids = ordered_character_ids("charA", ["charB"])  # pov=charA 排首
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_os4", "scene_generation", character_ids=cids,
        )
    pos = fragments.positive_block
    # 两配角都注入;charA(pov,char_order 0)排在前,即便 charB created_at 更新
    assert "主视角OS4" in pos
    assert "配角OS4" in pos
    assert pos.index("主视角OS4") < pos.index("配角OS4")


def test_same_character_multiple_bindings_dedup_one_layer():
    """PR-20 — 同一 character_id 两个 active binding → 只一层(去重,取 created_at 较新)。"""
    _seed_multi_character(
        seed="dedup1", project_id="proj_dd",
        characters=[
            {"character_id": "charA", "feature": "旧腔调DD", "created_at": "2026-01-01T00:00:00Z"},
            {"character_id": "charA", "feature": "新腔调DD", "created_at": "2026-05-01T00:00:00Z"},
        ],
    )
    cids = ordered_character_ids("charA", [])  # 仅 charA
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_dd", "scene_generation", character_ids=cids,
        )
    pos = fragments.positive_block
    # 同角色去重:只保留较新的 binding(新腔调),旧的不注入
    assert "新腔调DD" in pos
    assert "旧腔调DD" not in pos


def test_multi_character_scene_segment_still_largest():
    """PR-20 — 多配角 + scene 时,token 加权 [1..N] 顺延,scene 段(最具体)仍预算最大。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id="sr_book_mw", title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum="chk_mw", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id="sr_run_mw", book_id="sr_book_mw", status="done", phase="done")
        # project base + 2 character + scene 各 ~50 条 feature,确保都被 cap 整行截断
        for suffix, scope, ref, feat in [
            ("proj", "project", "proj_mw", "项"),
            ("ca", "character", "charA", "甲"),
            ("cb", "character", "charB", "乙"),
            ("scene", "scene", "scene_mw", "景"),
        ]:
            repo.create_profile(
                profile_id=f"sr_profile_mw_{suffix}", book_id="sr_book_mw", run_id="sr_run_mw",
                title=suffix, status="active",
                profile_json={
                    "narrative_summary": "n",
                    "style_features": [_marker_line(feat, i) for i in range(50)],
                },
                coverage_json={}, source_finding_ids_json=[],
            )
            repo.create_binding(
                binding_id=f"sr_bind_mw_{suffix}", profile_id=f"sr_profile_mw_{suffix}",
                scope=scope, scope_ref_id=ref,
                task_type="scene_generation", strategy="A", config_json={}, status="active",
            )
        session.commit()
    cids = ordered_character_ids("charA", ["charB"])  # [charA, charB]
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_mw", "scene_generation", character_ids=cids, scene_id="scene_mw",
        )
    pos = fragments.positive_block
    # 四层全叠:project / charA / charB / scene 都在
    assert "项" in pos and "甲" in pos and "乙" in pos and "景" in pos
    # 加权 [1,2,3,4]:scene 段(景,权重 4)比 project 基底(项,权重 1)长
    assert pos.count("景") > pos.count("项")
