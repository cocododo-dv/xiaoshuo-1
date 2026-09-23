"""风格参考 v3 测量核(measure.py)单测:唯一分段规则、汇总口径、唯一对白定义、特征表、稳健尺度。"""

from __future__ import annotations

import math

import pytest

from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.style_reference import measure as m

_PARAGRAPHS = [
    "“先别开门。”她压低声音说。",
    "他把手收了回来，站在门外听了很久，屋里没有一点声音。",
    "“你听见了吗？”",
    "他没有回答。走廊尽头的灯闪了两下，灭了。",
]


def test_single_newline_blank_line_and_html_paragraphs_measure_identically() -> None:
    """V4:作者稿用单换行分段;同一段文字用 \\n、\\n\\n、作者稿 HTML 分段,测得完全相同。"""
    single = m.measure_text("\n".join(_PARAGRAPHS))
    blank = m.measure_text("\n\n".join(_PARAGRAPHS))
    crlf = m.measure_text("\r\n\r\n".join(_PARAGRAPHS))
    html = m.measure_text("".join(f"<p>{p}</p>" for p in _PARAGRAPHS))
    assert single.paragraph_count == 4
    assert single == blank == crlf
    assert m.kernel_features(single) == m.kernel_features(blank) == m.kernel_features(html)
    # 段落表(每项一段)与整段文字同一规则
    assert m.kernel_features(m.measure_paragraphs(_PARAGRAPHS)) == m.kernel_features(single)
    # 段内换行也是段界
    assert m.measure_paragraphs(["\n".join(_PARAGRAPHS[:2]), *_PARAGRAPHS[2:]]).paragraph_count == 4


def test_html_drafts_follow_the_editor_paragraphs() -> None:
    html = "<p>第一段&nbsp;开头。</p><p>第二段<br>还是第二段。</p><blockquote>引文一段。</blockquote>"
    assert m.kernel_paragraphs(html) == ["第一段 开头。", "第二段还是第二段。", "引文一段。"]
    assert len(m.kernel_paragraphs(html)) == len(manuscript_paragraphs(html))
    # 纯文本里偶发的「<」不按 HTML 处理
    assert m.kernel_paragraphs("a < b 的时候\n第二行") == ["a < b 的时候", "第二行"]
    assert m.kernel_paragraphs("") == [] and m.kernel_paragraphs("  \n\n ") == []


def test_counts_are_pooled_over_the_whole_text() -> None:
    """一句 30 字的段 + 十句 3 字的段:句长均值 = 60 / 11(汇总),不是逐段平均的 16.5。"""
    text = "甲" * 30 + "。\n" + "乙乙乙。" * 10
    measure = m.measure_text(text)
    features = m.kernel_features(measure)
    assert measure.sentence_count == 11
    assert features["sent_len_mean"] == pytest.approx(60 / 11, abs=1e-6)
    assert features["para_len_mean"] == pytest.approx(30.0)
    # 「每千字」按可见字(不含标点)
    assert measure.char_count == 60
    assert features["punct_period_per_1k"] == pytest.approx(11 * 1000 / 60, abs=1e-6)


def test_dialogue_share_is_quoted_visible_chars_over_visible_chars() -> None:
    measure = m.measure_text("“你好。”他说。")
    assert measure.quote_count == 1 and measure.quoted_chars == 2 and measure.char_count == 4
    assert m.dialogue_char_share(measure) == pytest.approx(0.5)
    assert m.kernel_features(measure)["dialogue_char_share"] == pytest.approx(0.5)
    # 嵌套引号归外层,只算一次;‘’ 作主引号(早期白话排版)也算对白
    nested = m.measure_text("“他说‘走’。”")
    assert nested.quote_count == 1
    single_quoted = m.measure_text("‘走罢。’他说。")
    assert single_quoted.quote_count == 1 and m.dialogue_char_share(single_quoted) == pytest.approx(0.5)


def test_person_counts_only_look_at_narration() -> None:
    measure = m.measure_text("他说：“我来了，你走吧。”其他人都没动。")
    # 引号里的「我 / 你」是人物在说话;「其他」里的「他」不是代词
    assert measure.person_counts == {"first": 0, "second": 0, "third": 1}
    features = m.kernel_features(measure)
    assert features["person_third_share"] == 1.0


def test_feature_table_keeps_the_voice_names_and_is_finite() -> None:
    assert len(m.FEATURE_NAMES) == 57 == len(set(m.FEATURE_NAMES))
    assert m.FEATURE_NAMES[0] == "fw_particle_per_1k" and m.FEATURE_NAMES[51] == "lexical_bigram_hapax_ratio"
    assert m.FEATURE_NAMES[52:] == (
        "dialogue_char_share",
        "para_len_std",
        "digit_run_per_1k",
        "numeral_unit_per_1k",
        "latin_word_per_1k",
    )
    features = m.text_features("\n".join(_PARAGRAPHS * 3))
    assert tuple(features) == m.FEATURE_NAMES
    assert all(isinstance(value, float) and math.isfinite(value) for value in features.values())
    assert m.text_features("") == {name: 0.0 for name in m.FEATURE_NAMES}
    assert m.text_features("。。！！——……") == {name: 0.0 for name in m.FEATURE_NAMES}


def test_quantities_and_latin_words() -> None:
    text = "三十秒后，他跑了八千公里，看了一眼04:24的表，说OK。阿Q在门口。七斤嫂一度以为是一天。"
    measure = m.measure_text(text)
    assert measure.digit_runs == 1  # 04:24
    assert measure.numeral_units == 2  # 三十秒、八千公里(「七斤」「一度」「一天」不算)
    assert measure.latin_words == 1  # OK(单字母「Q」不算)


def test_light_measures_cannot_feed_kernel_features() -> None:
    light = m.measure_paragraphs(_PARAGRAPHS, detailed=False)
    assert light.detailed is False and light.sentence_count > 0
    with pytest.raises(ValueError):
        m.kernel_features(light)
    full = m.measure_paragraphs(_PARAGRAPHS)
    # 轻量测量的共有字段与完整测量一致
    assert light.sentence_chars == full.sentence_chars
    assert light.punct_counts == full.punct_counts
    assert light.quote_led_paragraphs == full.quote_led_paragraphs


def test_large_inputs_are_measured_once() -> None:
    m.clear_kernel_cache()
    big = [paragraph + f"第{index}回。" for index in range(1200) for paragraph in _PARAGRAPHS]
    first = m.measure_paragraphs(big)
    assert m.measure_paragraphs(list(big)) is first
    m.clear_kernel_cache()
    assert m.measure_paragraphs(big) is not first


def test_robust_scale_has_unit_floors() -> None:
    # 作者从来不用的东西(全 0):尺度落到单位下限,不会把 z 值炸飞
    center, scale = m.robust_center_scale([0.0] * 40, "punct_semicolon_per_1k")
    assert center == 0.0 and scale == pytest.approx(0.3)
    center, scale = m.robust_center_scale([0.0] * 40, "person_second_share")
    assert scale == pytest.approx(0.02)
    # 一半以上为 0 的稀有特征:MAD 为 0,由 p10–p90 跨度兜住
    values = [0.0] * 30 + [3.0] * 10
    center, scale = m.robust_center_scale(values, "punct_dash_per_1k")
    assert center == 0.0 and scale == pytest.approx(3.0 / m.P10_P90_TO_SIGMA)
    # 常规特征:1.4826 × MAD
    values = [10.0, 11.0, 12.0, 13.0, 14.0]
    center, scale = m.robust_center_scale(values, "sent_len_mean")
    assert center == 12.0 and scale == pytest.approx(max(1.4826 * 1.0, 3.2 / m.P10_P90_TO_SIGMA, 0.6))
    assert m.feature_kind("sent_len_lag1_autocorr") == "corr"
    assert m.feature_kind("lexical_char_ttr") == "lexical"
    assert m.feature_kind("sent_pauses_mean") == "count"
