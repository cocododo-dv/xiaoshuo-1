"""MetricsEngine 单测:21 项 MetricName(测量核之上,汇总口径)+ 方差 + 边界。

2026-09-23:文本指标改由测量核按汇总口径计算(不再逐段平均),「每千字」与句长按可见字(不含标点),
分段按唯一规则(换行即段界);五个感官词表指标删除。
"""

from __future__ import annotations

from novel_system.services.style_reference.metrics import (
    METRIC_NAMES,
    PROSE_SHAPE_METRIC_NAMES,
    MetricsEngine,
    ParagraphRecord,
    compute_prose_shape_from_text,
    compute_prose_shape_metrics,
)


def _engine() -> MetricsEngine:
    return MetricsEngine()


def test_metric_names_count() -> None:
    assert len(METRIC_NAMES) == 21
    assert not [name for name in METRIC_NAMES if name.startswith("sensory_")]


def test_compute_all_returns_all_metrics() -> None:
    paragraphs = [ParagraphRecord(text="天气晴朗。", paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert set(result.keys()) == set(METRIC_NAMES)


def test_compute_all_empty_returns_zeros() -> None:
    result = _engine().compute_all([])
    assert all(v == 0.0 for v in result.values())


def test_compute_with_variance_returns_tuples() -> None:
    paragraphs = [
        ParagraphRecord(text="天气很好。", paragraph_type="narration"),
        ParagraphRecord(text="今天下雨了!", paragraph_type="narration"),
    ]
    result = _engine().compute_with_variance(paragraphs)
    assert all(isinstance(v, tuple) and len(v) == 2 for v in result.values())


def test_prose_shape_metrics_are_separate_from_the_metric_contract() -> None:
    paragraphs = [
        ParagraphRecord(text="“走吧。”", paragraph_type="dialogue"),
        ParagraphRecord(text="天亮了。他没动。", paragraph_type="narration"),
    ]

    result = compute_prose_shape_metrics(paragraphs)

    assert set(result) == set(PROSE_SHAPE_METRIC_NAMES)
    assert set(result).isdisjoint(METRIC_NAMES)
    # 可见字:「走吧」2 字、「天亮了他没动」6 字(标点不计)
    assert result["paragraph_mean_chars"] == pytest.approx(4.0)
    assert result["paragraph_length_std_chars"] == pytest.approx(2.0)
    assert result["paragraphs_per_1k"] == pytest.approx(2 * 1000 / 8)
    assert result["single_sentence_paragraph_ratio"] == pytest.approx(0.5)
    assert result["quote_led_paragraph_ratio"] == pytest.approx(0.5)


def test_prose_shape_from_text_uses_the_kernel_paragraph_rule() -> None:
    """换行即段界:单换行与空行分段、作者稿 HTML 测得相同(V4:作者稿单换行分段曾被当成整场一段)。"""
    lines = ["“先别开门。”", "这是第二段。", "他把手收了回来。"]
    single = compute_prose_shape_from_text("\n".join(lines))
    blank = compute_prose_shape_from_text("\n\n".join(lines))
    mixed = compute_prose_shape_from_text("“先别开门。”\n这是第二段。\n\n他把手收了回来。")
    html = compute_prose_shape_from_text("<p>“先别开门。”</p><p>这是第二段。</p><p>他把手收了回来。</p>")
    assert single == blank == mixed == html
    assert single["quote_led_paragraph_ratio"] == pytest.approx(1 / 3)
    assert single["single_sentence_paragraph_ratio"] == pytest.approx(1.0)


def test_text_metrics_are_pooled_not_per_paragraph_means() -> None:
    """汇总口径:一句 30 字的段 + 十句 3 字的段,平均句长是 60 / 11,不是两段各自平均后的 16.5。"""
    long_paragraph = "甲" * 30 + "。"
    short_paragraph = "乙乙乙。" * 10
    paragraphs = [
        ParagraphRecord(text=long_paragraph, paragraph_type="narration"),
        ParagraphRecord(text=short_paragraph, paragraph_type="narration"),
    ]
    result = _engine().compute_all(paragraphs)
    assert result["avg_sentence_length"] == pytest.approx(60 / 11)
    assert result["short_sentence_ratio"] == pytest.approx(10 / 11)
    # 问号密度同理:全文问号数 ÷ 全文可见字
    questions = [
        ParagraphRecord(text="你去吗？", paragraph_type="dialogue"),
        ParagraphRecord(text="他" * 97 + "。", paragraph_type="narration"),
    ]
    pooled = _engine().compute_all(questions)["question_density_per_1k"]
    assert pooled == pytest.approx(1000 / 100)
    per_paragraph_mean = (1000 / 3 + 0) / 2
    assert pooled < per_paragraph_mean / 10


def test_avg_sentence_length() -> None:
    paragraphs = [ParagraphRecord(text="一句话。两句话。", paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    # 两个句子各 3 字
    assert result["avg_sentence_length"] == 3.0


def test_short_long_sentence_ratio() -> None:
    short_text = "短。短。短。"
    # 长句 long_sentence_ratio 阈值是 >=30 字
    long_text = "这是一个明显超过三十字门槛的中文长句用来测试长句指标的计算逻辑确认覆盖无误。"
    paragraphs = [
        ParagraphRecord(text=short_text, paragraph_type="narration"),
        ParagraphRecord(text=long_text, paragraph_type="narration"),
    ]
    result = _engine().compute_all(paragraphs)
    assert result["short_sentence_ratio"] > 0
    assert result["long_sentence_ratio"] > 0


def test_punctuation_density() -> None:
    text = "天气好,真的很好,实在是好。"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert result["punctuation_density_per_1k"] > 0


def test_ellipsis_density() -> None:
    text = "他说……然后……走了。"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert result["ellipsis_density_per_1k"] > 0


def test_dash_em_density() -> None:
    text = "他说——好的——走了。"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert result["dash_em_density_per_1k"] > 0


def test_classical_word_ratio() -> None:
    classical = "学而时习之,不亦说乎?有朋自远方来,不亦乐乎?"
    modern = "天气很好。我出门去。"
    paragraphs_c = [ParagraphRecord(text=classical, paragraph_type="narration")]
    paragraphs_m = [ParagraphRecord(text=modern, paragraph_type="narration")]
    r_c = _engine().compute_all(paragraphs_c)["classical_word_ratio"]
    r_m = _engine().compute_all(paragraphs_m)["classical_word_ratio"]
    assert r_c > r_m


def test_colloquial_marker_ratio() -> None:
    colloq = "走吧。来呢。好啊。嗯。"
    formal = "他向门口走去,看见院子里的雪。"
    r_c = _engine().compute_all(
        [ParagraphRecord(text=colloq, paragraph_type="narration")]
    )["colloquial_marker_ratio"]
    r_f = _engine().compute_all(
        [ParagraphRecord(text=formal, paragraph_type="narration")]
    )["colloquial_marker_ratio"]
    assert r_c > r_f


def test_metaphor_density() -> None:
    text = "她像一朵花,仿佛一颗星,犹如清晨的露珠。"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert result["metaphor_density_per_1k"] > 0


def test_personification_density() -> None:
    """拟人密度:含拟人动词的文本应 >0,平实记叙文本应为 0。

    词表填充后(原 PR-2 占位为空恒返 0),该子维不再静默失效。
    """
    vivid = "风在呜咽,落叶低语,远山沉睡,溪水翩跹起舞。"
    plain = "我买了三个苹果,把它们放进篮子里,然后回家。"
    vivid_d = _engine().compute_all(
        [ParagraphRecord(text=vivid, paragraph_type="description_env")]
    )["personification_density_per_1k"]
    plain_d = _engine().compute_all(
        [ParagraphRecord(text=plain, paragraph_type="narration")]
    )["personification_density_per_1k"]
    assert vivid_d > 0
    assert plain_d == 0.0
    assert vivid_d > plain_d


def test_dialogue_ratio_by_paragraph_type() -> None:
    paragraphs = [
        ParagraphRecord(text="他说:你好。", paragraph_type="dialogue"),
        ParagraphRecord(text="他点点头。", paragraph_type="dialogue"),
        ParagraphRecord(text="天气很好。", paragraph_type="narration"),
        ParagraphRecord(text="他想着昨天。", paragraph_type="psychology"),
    ]
    result = _engine().compute_all(paragraphs)
    assert result["dialogue_ratio"] == 0.5
    assert result["psychology_ratio"] == 0.25
    assert result["narration_ratio"] == 0.25


def test_all_paragraph_type_ratios_covered() -> None:
    paragraphs = [
        ParagraphRecord(text="对话。", paragraph_type="dialogue"),
        ParagraphRecord(text="心理。", paragraph_type="psychology"),
        ParagraphRecord(text="环境。", paragraph_type="description_env"),
        ParagraphRecord(text="人物。", paragraph_type="description_char"),
        ParagraphRecord(text="动作。", paragraph_type="action"),
        ParagraphRecord(text="叙述。", paragraph_type="narration"),
        ParagraphRecord(text="过渡。", paragraph_type="transition"),
        ParagraphRecord(text="闪回。", paragraph_type="flashback"),
    ]
    result = _engine().compute_all(paragraphs)
    # 每个 type 各 1 段,占比 1/8 = 0.125
    for metric in (
        "dialogue_ratio",
        "psychology_ratio",
        "description_env_ratio",
        "description_char_ratio",
        "action_ratio",
        "narration_ratio",
        "transition_ratio",
        "flashback_ratio",
    ):
        assert result[metric] == 0.125, f"{metric} 期望 0.125,实际 {result[metric]}"


def test_question_density() -> None:
    text = "你是谁?为什么在这里?要去哪儿?"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="dialogue")]
    result = _engine().compute_all(paragraphs)
    assert result["question_density_per_1k"] > 0


def test_semicolon_density() -> None:
    text = "天气好;心情好;一切都好。"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert result["semicolon_density_per_1k"] > 0


def test_sentence_length_std_zero_for_single_sentence() -> None:
    text = "只有一句话"
    paragraphs = [ParagraphRecord(text=text, paragraph_type="narration")]
    result = _engine().compute_all(paragraphs)
    assert result["sentence_length_std"] == 0.0


def test_compute_with_variance_std_is_chunk_level() -> None:
    """std 现为块间标准差(块 ≈1500 字),非逐段 std。

    校准修正(2026-06):逐段 0/1 的段级 std 噪声过大(~0.5)使
    tolerance=max(std×1.25, floor) 宽到几乎不拦截;改用块间 std。
    两块 dialogue 占比 [1.0, 0.0] → std=0.5;mean 仍为全文逐段均值。
    paragraph_type 比例与文本内容无关,只用 char_count 控分块、type 定占比。
    """
    long = "话" * 800  # 单段 800 字;两段累计 ≥1500 切一块
    paragraphs = [
        ParagraphRecord(text=long, paragraph_type="dialogue"),
        ParagraphRecord(text=long, paragraph_type="dialogue"),   # 块1:全对话 → 1.0
        ParagraphRecord(text=long, paragraph_type="narration"),
        ParagraphRecord(text=long, paragraph_type="narration"),  # 块2:全叙述 → 0.0
    ]
    mean, std = _engine().compute_with_variance(paragraphs)["dialogue_ratio"]
    assert mean == pytest.approx(0.5, rel=1e-3)
    assert std == pytest.approx(0.5, rel=1e-3)


def test_compute_with_variance_single_chunk_std_zero() -> None:
    """短语料(不足一块)无块间样本 → std=0,由 tolerance floor 兜底。"""
    paragraphs = [
        ParagraphRecord(text="对话。", paragraph_type="dialogue"),
        ParagraphRecord(text="叙述文字。", paragraph_type="narration"),
        ParagraphRecord(text="对话。", paragraph_type="dialogue"),
    ]
    mean, std = _engine().compute_with_variance(paragraphs)["dialogue_ratio"]
    assert mean == pytest.approx(2 / 3, rel=1e-3)  # mean 不受分块影响
    assert std == 0.0


def test_sensory_lexicon_metrics_are_gone() -> None:
    """2026-09-23:感官词表指标(子串匹配会把人名里的「明」算成视觉词)随测量核删除。"""
    result = _engine().compute_all([ParagraphRecord(text="他看见光，听见声音。", paragraph_type="narration")])
    assert not [name for name in result if name.startswith("sensory_")]


# 2026-09-23 风格参考 v3（P5b）：旧的量化回测（validation/quantitative.py）删除，生成稿指标 compute_generated_metrics
# 搬到 metrics.py（候选排序与预览还用它）；下面两条从旧的量化回测单测里保留。


def test_generated_metrics_single_newline_blank_line_and_html_paragraphs_measure_the_same() -> None:
    """作者稿常用单换行分段；过去只按空行切，整场被当成一段。"""
    from novel_system.services.style_reference.metrics import compute_generated_metrics

    paragraphs = ["“先别开门。”", "他把手收了回来，站在门外听了很久。", "屋里没有声音。"]
    single = compute_generated_metrics("\n".join(paragraphs))
    blank = compute_generated_metrics("\n\n".join(paragraphs))
    html = compute_generated_metrics("".join(f"<p>{p}</p>" for p in paragraphs))
    assert single == blank == html
    assert single["single_sentence_paragraph_ratio"] == 1.0
    assert single["quote_led_paragraph_ratio"] == 1 / 3


def test_generated_metrics_expose_paragraph_shape_and_every_metric() -> None:
    from novel_system.services.style_reference.metrics import (
        TYPE_RATIO_METRICS,
        compute_generated_metrics,
    )

    text = "甲乙丙丁。\n\n这是明显更长的第二段，用来制造段落长度差。"
    generated = compute_generated_metrics(text)
    assert set(PROSE_SHAPE_METRIC_NAMES) <= generated.keys()
    assert set(METRIC_NAMES) <= generated.keys()
    assert generated["paragraphs_per_1k"] > 0
    # 生成稿没有分类器：全部当叙述，段型比例只是占位（候选排序 / 预览都不拿它们对照）
    assert generated["narration_ratio"] == 1.0
    assert len(TYPE_RATIO_METRICS) == 8 and TYPE_RATIO_METRICS <= set(METRIC_NAMES)
    assert compute_generated_metrics("") == {}


import pytest  # noqa: E402  (avoid circular if any)
