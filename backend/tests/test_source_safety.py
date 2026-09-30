"""Red→green guard for BUG-002: protected-source-term scan must not be evaded
by trivial variants (intra-term whitespace/punctuation, traditional Chinese).

Source safety is a North-Star red line: a missed leak (false negative) is the
expensive failure mode. These tests pin the variant-detection contract while
also asserting clean original prose is *not* flagged (false-positive guard).
"""

from __future__ import annotations

import json

from novel_system.services.source_safety import (
    scan_source_safety,
)


SOURCE_FIXTURE_TERMS = (
    "灰港学院",
    "欧文灰港",
    "沈既白",
    "青铜与热泉",
    "雨城陈氏",
    "潜龙港",
)


def test_direct_literal_leak_is_still_blocked() -> None:
    """Regression: the plain substring path must keep working unchanged."""
    result = scan_source_safety(
        "第二场错误出现了灰港学院与沈既白。",
        protected_terms=SOURCE_FIXTURE_TERMS,
    )
    assert result["safe"] is False
    # original simplified term + PROTECTED_SOURCE_TERMS iteration order preserved
    assert result["blocked_terms"] == ["灰港学院", "沈既白"]


def test_intra_term_whitespace_variant_is_detected() -> None:
    """BUG-002 repro: a space spliced inside the term ("潜 龙港") must not evade."""
    result = scan_source_safety(
        "反派在终章烧毁了潜 龙港的灯塔。",
        protected_terms=SOURCE_FIXTURE_TERMS,
    )
    assert result["safe"] is False
    assert "潜龙港" in result["blocked_terms"]


def test_intra_term_punctuation_variant_is_detected() -> None:
    """BUG-002 repro: inserted punctuation ("潜-龙港", "灰港·学院") must not evade."""
    dash = scan_source_safety("他立誓要回到潜-龙港。", protected_terms=SOURCE_FIXTURE_TERMS)
    assert dash["safe"] is False
    assert "潜龙港" in dash["blocked_terms"]

    dot = scan_source_safety("传说里的灰港·学院早已覆灭。", protected_terms=SOURCE_FIXTURE_TERMS)
    assert dot["safe"] is False
    assert "灰港学院" in dot["blocked_terms"]


def test_traditional_chinese_variant_is_detected() -> None:
    """BUG-002 repro: traditional forms ("潜龍港", "青銅與熱泉", "雨城陳氏") must not evade."""
    result = scan_source_safety(
        "成稿里赫然写着潜龍港与青銅與熱泉，还有雨城陳氏一词。",
        protected_terms=SOURCE_FIXTURE_TERMS,
    )
    assert result["safe"] is False
    blocked = result["blocked_terms"]
    assert "潜龙港" in blocked
    assert "青铜与热泉" in blocked
    assert "雨城陈氏" in blocked


def test_clean_original_text_is_safe() -> None:
    """False-positive guard: clean original prose stays safe=True."""
    clean = (
        "少年在荒原尽头点燃篝火，雪光映着他疲惫的眼睛，"
        "远处传来狼群低沉的嚎叫，像是替谁守着一场无名的葬礼。"
    )
    result = scan_source_safety(clean)
    assert result["safe"] is True
    assert result["blocked_terms"] == []


def test_named_work_terms_are_not_global_defaults() -> None:
    """A project that did not bind/configure a source must not inherit its names."""
    result = scan_source_safety("镜湖馆长检查了雨城陈氏的记录，又把旧档案交给同伴。")

    assert result["safe"] is True
    assert result["blocked_terms"] == []
    assert result["protected_terms_source"] == "none"
    assert result["coverage"]["semantic_paraphrase"] == {
        "status": "not_evaluated",
        "blocking": False,
        "reason": "deterministic source safety cannot reliably verify semantic or cross-language paraphrase",
        "recommended_action": "use independent semantic review as advisory evidence",
    }


def test_global_terms_require_explicit_json_configuration(monkeypatch) -> None:
    monkeypatch.setenv(
        "NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON",
        json.dumps(["欧文灰港", "镜湖档案馆"], ensure_ascii=False),
    )

    result = scan_source_safety("欧 文 灰 港站在镜·湖档案馆门外。")

    assert result["safe"] is False
    assert result["blocked_terms"] == ["欧文灰港", "镜湖档案馆"]
    assert result["protected_terms_source"] == "environment"


def test_protected_term_spans_survive_variants_and_point_into_the_text() -> None:
    """风格参考 v3：画像的受保护专名由抄袭门按位置报——繁体 + 插空格的变体照样命中，位置指回原文。"""
    from novel_system.services.source_safety import find_protected_term_spans

    text = "他举起了青 銅與熱泉的旗帜。后来青铜与热泉又出现了。"
    spans = find_protected_term_spans(text, ["青铜与热泉"])
    assert [(term, start, end) for term, start, end in spans] == [
        ("青铜与热泉", 4, 10),
        ("青铜与热泉", 16, 21),
    ]
    assert text[4:10] == "青 銅與熱泉" and text[16:21] == "青铜与热泉"
    assert find_protected_term_spans(text, ["不相干"]) == []


def test_protected_term_spans_map_folded_characters_back_to_the_text() -> None:
    """逐字规范化按不同的字符各做一次：一个字可能折成两个（ß → ss、ﬁ → fi），也可能整个去掉（零宽字符、间隔号）；
    几个词一次查，命中位置照样指回原文、按位置排好。"""
    from novel_system.services.source_safety import find_protected_term_spans, normalize_for_term_match

    text = "旧信上写着 Straße 与 ﬁle，灰​港·学院在雨城。"
    spans = find_protected_term_spans(text, ["灰港学院", "strasse", "FILE", "不相干"])

    assert [(term, text[start:end]) for term, start, end in spans] == [
        ("strasse", "Straße"),
        ("FILE", "ﬁle"),
        ("灰港学院", "灰​港·学院"),
    ]
    assert normalize_for_term_match("ＡＢ　Straße，ﬁ·龍") == "abstrassefi龙"


def test_protected_term_spans_skip_the_offset_map_when_nothing_matches(monkeypatch) -> None:
    """原文下标只在真有命中时才算：一章几万字、专名大多不出现，不必为每段文字都铺一张下标表。"""
    from novel_system.services import source_safety

    def fail(*_args):  # noqa: ANN002, ANN202
        raise AssertionError("offsets built without a match")

    monkeypatch.setattr(source_safety, "_raw_offsets", fail)
    assert source_safety.find_protected_term_spans("雨城的钟响了三下。" * 200, ["灰港学院", "欧文"]) == []

