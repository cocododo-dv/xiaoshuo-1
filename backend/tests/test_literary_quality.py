"""规则维度引擎（``literary_quality``）的判定：模板复用、职业写作风险、感知过滤、模型腔、证据充分度、权重、自我重复。

文学质量视图与它的三个接口（巡检 / 即时扫描 / 章组复审）2026-10-20 退役，那些接口的用例随之删掉；规则发现现在只经
场景诊断（写作台深改）与成稿门出来，那两处各有自己的测试。"""

from __future__ import annotations

from novel_system.services.literary_quality import (
    AUTOMATED_EVIDENCE_SIGNAL,
    QUALITY_DIMENSIONS,
    adversarial_rank_score,
    analyze_literary_quality,
)


def test_literary_quality_detects_template_reuse() -> None:
    text = (
        "她低头看着钥匙，沉默了片刻。\n"
        "他低头看着录音，沉默了片刻。\n"
        "她低头看着门缝，沉默了片刻。\n"
        "月光、阴影、冷风和雾气反复压下来，月光又落在她手上。\n"
        "她忽然意识到这一切都变得不同了。她知道真相必须公开。"
    )

    signals, findings = analyze_literary_quality(text)

    assert signals["template_action_reuse"]["risk"] is True
    assert signals["image_field_reuse"]["risk"] is True
    assert signals["syntax_monotony"]["risk"] is True
    assert signals["false_clarity"]["risk"] is True
    # 批准#13c（重评 R7）：永远打满分、从不报问题的「有效留白」维度删了
    assert "valid_ambiguity" not in signals
    assert set(signals) - {AUTOMATED_EVIDENCE_SIGNAL} == set(QUALITY_DIMENSIONS)
    assert "valid_ambiguity" not in QUALITY_DIMENSIONS
    finding_dimensions = {finding["dimension"] for finding in findings}
    assert {
        "template_action_reuse",
        "image_field_reuse",
        "syntax_monotony",
        "false_clarity",
    } <= finding_dimensions


def test_literary_quality_detects_professional_writer_risks() -> None:
    text = (
        "月光像旧伤疤一样贴在档案柜上，冷意仿佛命运的回声。"
        "“真相是官方报告被改过，因为他们要保护码头，所以我现在解释给你听。”许望说。"
        "林岑忽然意识到自己必须公开证据，她知道一切都变得不同了。"
    )

    signals, findings = analyze_literary_quality(text)

    assert signals["painless_scene"]["risk"] is True
    assert signals["decorative_imagery"]["risk"] is True
    assert signals["dialogue_as_report"]["risk"] is True
    assert signals["over_explained_motive"]["risk"] is True
    assert signals["false_poetic_closure"]["risk"] is True
    dimensions = {finding["dimension"] for finding in findings}
    assert {
        "painless_scene",
        "decorative_imagery",
        "dialogue_as_report",
        "over_explained_motive",
        "false_poetic_closure",
    } <= dimensions


def test_literary_quality_detects_perception_filter() -> None:
    text = (
        "她注意到窗外的雨已经停了。"
        "走廊里有人在争吵，选择公开证据还是保护证人。"
        "代价是暴露自己的位置。她推开门走出去。"
    )
    signals, findings = analyze_literary_quality(text)
    assert signals["perception_filter"]["risk"] is True
    assert any(f["dimension"] == "perception_filter" for f in findings)


def test_literary_quality_expanded_model_voice_catches_chinese_cliches() -> None:
    text = (
        "她微微一笑，缓缓说道："
        "“你要选择哪一个？”"
        "他必须在公开和保护之间做出选择，"
        "代价是暴露位置。"
        "他推开门。"
    )
    signals, findings = analyze_literary_quality(text)
    assert signals["model_voice"]["risk"] is True


def test_action_keyword_bundle_is_insufficient_evidence_not_literary_perfection() -> None:
    text = "必须选择，付出代价，她推开门。"

    signals, findings = analyze_literary_quality(text)
    score = adversarial_rank_score(text)

    # The phrase can satisfy several structural word lists, but it is not
    # enough prose to support an upper-bound literary judgment.
    assert signals["no_choice_scene"]["risk"] is False
    assert signals["choice_pressure"]["risk"] is False
    assert signals["automated_evidence_sufficiency"]["risk"] is True
    assert signals["automated_evidence_sufficiency"]["human_judgment_required"] is True
    assert score < 0.6
    assert score != 1.0
    assert findings == []


def test_literary_quality_dimension_weights_sum_to_one() -> None:
    from novel_system.services.literary_quality import DIMENSION_WEIGHTS
    assert abs(sum(DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(DIMENSION_WEIGHTS) == set(QUALITY_DIMENSIONS)


def test_weighted_score_is_the_one_formula_for_every_caller() -> None:
    """B04-16：条目分、成稿门的总分与人物场景核心、对抗排名分都走 ``scoring.weighted_score``（维度组各选各的）。"""
    from novel_system.services.literary_quality import DIMENSION_WEIGHTS, weighted_score

    signals, _ = analyze_literary_quality("她低头看着钥匙，沉默了片刻。他低头看着录音，沉默了片刻。她知道真相必须公开。")
    risky = [dimension for dimension in QUALITY_DIMENSIONS if signals[dimension]["risk"]]
    assert risky
    assert weighted_score(signals, QUALITY_DIMENSIONS) == round(
        sum(signals[dimension]["score"] * DIMENSION_WEIGHTS[dimension] for dimension in QUALITY_DIMENSIONS), 4
    )
    # 成稿门：参考作者常态的维度按已满足计——全都放过就是满分
    assert weighted_score(signals, QUALITY_DIMENSIONS, forced_ok=risky) == 1.0
    # 归一：除以这组维度的权重和；没有信号的维度按满分算；权重和为 0 时给 empty
    core = ("no_choice_scene", "choice_pressure")
    assert weighted_score({}, core, normalize=True) == 1.0
    assert weighted_score(signals, core, {"no_choice_scene": 0.0}, normalize=True, empty=0.42) == 0.42


def test_self_repetition_dimension_defaults_to_no_risk() -> None:
    signals, _ = analyze_literary_quality("She opened the door. He must choose.")
    assert signals["self_repetition"]["risk"] is False
    assert signals["self_repetition"]["score"] == 1.0


def test_self_repetition_allows_deliberate_two_part_refrain() -> None:
    signals, findings = analyze_literary_quality(
        "那扇锈住的铁门还是没有打开。她走到楼下，听完雨里的脚步。"
        "那扇锈住的铁门还是没有打开。"
    )

    assert signals["self_repetition"]["risk"] is False
    assert not any(item["dimension"] == "self_repetition" for item in findings)


def test_self_repetition_detects_high_confidence_mechanical_loop() -> None:
    signals, findings = analyze_literary_quality(
        "他把目光重新移回那扇紧闭的门边。" * 4
    )

    assert signals["self_repetition"]["risk"] is True
    assert any(item["dimension"] == "self_repetition" for item in findings)
