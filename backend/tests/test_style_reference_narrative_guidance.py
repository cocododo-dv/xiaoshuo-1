"""风格模仿 v2（W5，规格 §1.3 / §2.W5.1）— 叙事机制指引的收集与渲染。

``collect_narrative_guidance`` 合并冻结契约各层的 ``profile_json.narrative_guidance``
（泛 → 具体、去重、≤8 行），``render_narrative_section`` 渲染成 bundle section 正文。
旧画像 / 坏形状一律优雅退化为空，绝不抛异常。
"""

from __future__ import annotations

from novel_system.services.style_reference.narrative_guidance import (
    NARRATIVE_AVOID_MARKER,
    NARRATIVE_GUIDANCE_MAX_LINES,
    NARRATIVE_GUIDANCE_SECTION_KEY,
    NARRATIVE_GUIDANCE_SECTION_LABEL,
    collect_narrative_guidance,
    mark_forbidden_narrative_statement,
    render_narrative_section,
    render_section,
)


def _layer(order: int, guidance) -> dict:
    return {
        "order": order,
        "profile": {"profile_id": f"p{order}", "profile_json": {"narrative_guidance": guidance}},
    }


def test_collect_merges_layers_generic_to_specific_and_dedupes() -> None:
    contract = {
        "layers": [
            _layer(1, ["场景中段插入一次停顿", "关键信息放段首"]),
            _layer(0, ["关键信息放段首", "结尾不解释动机"]),
        ]
    }
    lines = collect_narrative_guidance(contract)
    # order=0（泛层）先出，具体层去重后接续
    assert lines == ["关键信息放段首", "结尾不解释动机", "场景中段插入一次停顿"]


def test_collect_caps_at_eight_lines_and_strips_list_prefixes() -> None:
    contract = {
        "layers": [
            _layer(0, [f"- 指引{i}" for i in range(12)]),
        ]
    }
    lines = collect_narrative_guidance(contract)
    assert len(lines) == NARRATIVE_GUIDANCE_MAX_LINES == 8
    assert lines[0] == "指引0"
    assert all(not line.startswith("-") for line in lines)


def test_collect_dedupes_case_insensitively_and_normalizes_whitespace() -> None:
    contract = {
        "layers": [
            _layer(0, ["Reveal  first,\nexplain never", "reveal first, explain never"]),
        ]
    }
    assert collect_narrative_guidance(contract) == ["Reveal first, explain never"]


def test_collect_degrades_gracefully_for_legacy_profiles_and_bad_shapes() -> None:
    assert collect_narrative_guidance(None) == []
    assert collect_narrative_guidance({}) == []
    assert collect_narrative_guidance({"layers": "not-a-list"}) == []
    # 旧画像：没有 narrative_guidance 键
    legacy = {"layers": [{"order": 0, "profile": {"profile_json": {"style_features": ["短句"]}}}]}
    assert collect_narrative_guidance(legacy) == []
    # 键存在但形状错误 / 空值
    assert collect_narrative_guidance({"layers": [_layer(0, 42)]}) == []
    assert collect_narrative_guidance({"layers": [_layer(0, [None, "", "   ", 7])]}) == []
    # 单个字符串也接受
    assert collect_narrative_guidance({"layers": [_layer(0, "只说一句")]}) == ["只说一句"]
    # 层不是 mapping 时跳过
    assert collect_narrative_guidance({"layers": [None, "x", _layer(0, ["a"])]}) == ["a"]


def test_render_section_prefixes_purpose_line_and_bullets() -> None:
    text = render_narrative_section(["关键信息放段首", "- 结尾不解释动机"])
    lines = text.split("\n")
    assert lines[0].startswith("以下是参考作品的叙事取舍机制")
    assert lines[1:] == ["- 关键信息放段首", "- 结尾不解释动机"]
    # 只决定叙事取舍，不含任何语言层块 / 原文样例的标记
    assert "[STYLE_REFERENCE]" not in text
    assert "[风格样例]" not in text
    assert render_section is render_narrative_section


def test_mark_forbidden_statement_adds_avoid_marker_unless_already_negated() -> None:
    """C17:forbidden 陈述按抽取约定命名模式本身,平铺进 narrative_guidance 必须带极性。"""
    assert NARRATIVE_AVOID_MARKER == "避免："
    assert mark_forbidden_narrative_statement("以全知旁白直接解释人物动机") == "避免：以全知旁白直接解释人物动机"
    # 只规整空白,不改正文
    assert mark_forbidden_narrative_statement("  以全知旁白\n直接解释人物动机 ") == "避免：以全知旁白 直接解释人物动机"
    # 已是否定 / 回避措辞的原样保留,不叠成双重否定
    for negated in (
        "不用连续回忆段拖慢当下动作",
        "不会在对白里交代背景",
        "无需在段首交代动机",
        "避免：以全知旁白解释动机",
        "避免以全知旁白解释动机",
        "勿在段首解释动机",
        "禁止倒叙开场",
        "从不解释人物动机",
        "少用回忆段",
    ):
        assert mark_forbidden_narrative_statement(negated) == negated, negated
    assert mark_forbidden_narrative_statement("") == ""
    assert mark_forbidden_narrative_statement("   ") == ""
    assert mark_forbidden_narrative_statement(None) == ""
    # 复审后续:只认完整否定词——以「不断 / 不同 / 不时 / 无数 / 无论 / 别出心裁 /
    # 莫名其妙」等起头的正面陈述仍是模式本身,必须带极性标记
    for positive in (
        "不断插入旁白评论",
        "不同人物视角在同一段内交替切换",
        "不时跳出故事直接对读者说话",
        "无数细节堆叠开场",
        "无论何处都以回忆段开场",
        "别出心裁地倒叙开场",
        "莫名其妙地切换视角",
        "忌惮式地反复铺垫同一伏笔",
    ):
        assert mark_forbidden_narrative_statement(positive) == f"避免：{positive}", positive


def test_avoid_marker_survives_collect_and_render() -> None:
    marked = mark_forbidden_narrative_statement("以全知旁白直接解释人物动机")
    contract = {"layers": [_layer(0, ["关键信息放段首", marked])]}
    lines = collect_narrative_guidance(contract)
    assert lines == ["关键信息放段首", "避免：以全知旁白直接解释人物动机"]
    rendered = render_narrative_section(lines).split("\n")
    assert rendered[1:] == ["- 关键信息放段首", "- 避免：以全知旁白直接解释人物动机"]


def test_render_section_empty_returns_empty_string() -> None:
    assert render_narrative_section([]) == ""
    assert render_narrative_section(["", "   "]) == ""


def test_section_constants_match_context_budget_registration() -> None:
    from novel_system.services.context_budget import (
        NEUTRAL_DRAFT_STYLE_SECTIONS,
        SECTION_SPECS,
    )

    specs = {name: (label, keys) for name, label, keys in SECTION_SPECS}
    assert NARRATIVE_GUIDANCE_SECTION_KEY == "style_narrative_guidance"
    label, keys = specs[NARRATIVE_GUIDANCE_SECTION_KEY]
    assert label == NARRATIVE_GUIDANCE_SECTION_LABEL == "Style Reference — Narrative Mechanisms"
    assert keys == (NARRATIVE_GUIDANCE_SECTION_KEY,)
    # 叙事机制块是 neutral_draft 唯一可见的风格参考块（规格 §1.3）
    assert NARRATIVE_GUIDANCE_SECTION_KEY not in NEUTRAL_DRAFT_STYLE_SECTIONS
    assert "previous_scene_voice_anchor" in NEUTRAL_DRAFT_STYLE_SECTIONS
    assert "style_drift_calibration" in NEUTRAL_DRAFT_STYLE_SECTIONS
    assert specs["previous_scene_voice_anchor"][0] == (
        "Previous Scene Voice Anchor (own prose; keep the same voice)"
    )
    assert specs["style_drift_calibration"][0] == "Style Drift Calibration"
