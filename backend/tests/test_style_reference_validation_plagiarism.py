"""Plagiarism Rabin-Karp 单测(PR-4)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import random
import time

import pytest

from novel_system.services.reference_copy_gate import check_reference_copy
from novel_system.services.style_reference.validation.plagiarism import (
    BookNgramIndex,
    check_plagiarism,
    normalize_text_for_matching,
    normalize_with_offsets,
)
from tests.style_reference_factories import make_book


def test_plagiarism_hit_long_overlap() -> None:
    quote = "暮色四合,街口的雾气还没散尽,行人三三两两走过。"  # 25 字
    text = f"今儿是个好天气。暮色四合,街口的雾气还没散尽,行人三三两两走过。"
    report = check_plagiarism(text, [quote])
    assert not report.passed
    assert len(report.hits) == 1
    assert report.hits[0].matched_length >= 12


def test_plagiarism_below_threshold_eleven_chars_no_hit() -> None:
    """11 字符连续匹配不应命中(threshold=12)。"""
    quote = "暮色四合街口的雾气X"  # 字符位:暮色四合街口的雾气 = 9 字 + X = 10 字
    text = "今儿是个好天气暮色四合街口的Y"  # 与 quote 前 7 字相同
    report = check_plagiarism(text, [quote])
    assert report.passed
    assert report.hits == []


def test_plagiarism_exactly_twelve_chars_hit() -> None:
    """恰好 12 字符连续匹配应命中。"""
    quote = "暮色四合街口的雾气还没散"  # 12 字
    text = f"开头三个字。{quote}尾巴。"
    report = check_plagiarism(text, [quote])
    assert not report.passed
    assert len(report.hits) == 1
    assert report.hits[0].matched_length >= 12


def test_plagiarism_empty_inputs() -> None:
    assert check_plagiarism("", ["something"]).passed is True
    assert check_plagiarism("anything", []).passed is True
    assert check_plagiarism("", []).passed is True


def test_plagiarism_short_text_below_ngram_size() -> None:
    """generated_text 长度 < ngram_size 直接 pass。"""
    quote = "暮色四合,街口的雾气还没散尽"
    text = "短文本"
    report = check_plagiarism(text, [quote])
    assert report.passed
    assert report.hits == []


def test_plagiarism_performance_50k_quote() -> None:
    """5 万字 profile × 1000 字 generated 扫描软线 < 200ms。"""
    quote_50k = "暮色四合街口的雾气还没散尽行人三三两两走过" * 1200  # ~25k 字
    generated_1k = "今儿是个好天气。" * 50  # ~500 字
    start = time.perf_counter()
    report = check_plagiarism(generated_1k, [quote_50k])
    elapsed = (time.perf_counter() - start) * 1000
    assert elapsed < 200.0, f"plagiarism scan 耗时 {elapsed:.1f}ms 超过软线 200ms"
    # 不论 passed 与否,关键是性能
    assert isinstance(report.passed, bool)


# ---------------------------------------------------------------- BookNgramIndex（一本书一份的 t 字元索引）


def _random_case(seed: int, *, threshold: int) -> tuple[list[str], list[str]]:
    """小字母表的随机「书」与随机检查行：一半从书里截（长短跨过门槛），一半随机拼，另加标点 / 空白 / 空行。"""
    rng = random.Random(seed)
    alphabet = "的一是在不了有和人这中大为上个国我以要他时来用们"
    corpus = ["".join(rng.choice(alphabet) for _ in range(rng.randint(30, 80))) for _ in range(40)]
    # 书里的标点 / 空白同样在规范化时去掉：隔几段插一个
    corpus = [text[:9] + "，" + text[9:15] + " " + text[15:] if n % 4 == 0 else text for n, text in enumerate(corpus)]
    corpus.append("")  # 空段不进索引
    lines: list[str] = []
    for _ in range(150):
        if rng.random() < 0.5:
            src = rng.choice([text for text in corpus if text])
            start = rng.randint(0, len(src) - threshold - 4)
            piece = src[start : start + rng.randint(threshold - 3, threshold + 4)]
            # 插空格 / 换标点：规范化之后仍是原文
            if rng.random() < 0.3 and len(piece) > 4:
                piece = piece[:2] + "，" + piece[2:4] + " " + piece[4:]
            lines.append(piece)
        else:
            lines.append("".join(rng.choice(alphabet) for _ in range(rng.randint(5, 30))))
    lines.extend(["，。！", "", "   ", "他，的 一 是"])
    return corpus, lines


def _brute_force_spans(normalized: str, corpus: list[str], threshold: int) -> list[tuple[int, int]]:
    """定义本身：被检查文字里每个 t 字元只要是某段（规范化后）的子串就算命中，命中窗口的并集合并成区间。"""
    norms = [normalize_text_for_matching(text) for text in corpus if text]
    spans: list[list[int]] = []
    for start in range(len(normalized) - threshold + 1):
        gram = normalized[start : start + threshold]
        if not any(gram in norm for norm in norms):
            continue
        if spans and start <= spans[-1][1]:
            spans[-1][1] = start + threshold
        else:
            spans.append([start, start + threshold])
    return [(begin, end) for begin, end in spans]


@pytest.mark.parametrize("seed", [7, 11, 2026])
@pytest.mark.parametrize("threshold, ngram_size", [(8, 6), (12, 8)])
def test_book_ngram_index_matches_check_plagiarism_exactly(seed: int, threshold: int, ngram_size: int) -> None:
    corpus, lines = _random_case(seed, threshold=threshold)
    index = BookNgramIndex(corpus, threshold_chars=threshold)
    assert index.paragraph_count == 40 and len(index.hashes) > 0
    positives = 0
    for line in lines:
        expected = not check_plagiarism(line, corpus, ngram_size=ngram_size, threshold_chars=threshold).passed
        assert index.overlaps(line) is expected, line
        normalized = normalize_text_for_matching(line)
        spans = index.overlap_spans(normalized)
        assert spans == _brute_force_spans(normalized, corpus, threshold), line
        assert bool(spans) is expected, line
        positives += int(expected)
    assert 0 < positives < len(lines)


def test_book_ngram_index_edges() -> None:
    assert BookNgramIndex([], threshold_chars=8).overlaps("随便一行，足够长的一行字") is False
    index = BookNgramIndex(["甲乙丙丁戊己庚辛壬癸子丑"], threshold_chars=12)
    assert index.overlaps("前缀甲乙丙丁戊己庚辛壬癸子丑后缀") is True
    assert index.overlaps("甲乙丙丁戊己庚辛壬癸子") is False  # 11 字不到门槛
    assert index.overlaps("") is False and index.overlaps("   ") is False
    # 段与段之间不会拼出假命中：两段各 6 字，连起来的 12 字不算
    split = BookNgramIndex(["甲乙丙丁戊己", "庚辛壬癸子丑"], threshold_chars=12)
    assert split.overlaps("甲乙丙丁戊己庚辛壬癸子丑") is False
    assert split.contains("甲乙丙丁戊己庚辛壬癸子丑") is False
    # 抄袭门按书标命中：book_id 只是标签，缺省为空
    assert index.book_id is None
    assert BookNgramIndex(["甲乙丙丁戊己庚辛壬癸子丑"], book_id="book-1").book_id == "book-1"


# ---------------------------------------------------------------- 小写后变长的字符（复核 P07-R4）

# 书里的一行（14 字）与几种把「İ」（U+0130，小写成 i + U+0307 两个码位）放在命中前 / 后 / 里面的检查文字；
# 第三项是命中映射回原文后应得的那一段。
_OLD_LETTER = "他在雨城的案卷里找到一封旧信"
_DOTTED_I_CASES = [
    # 「İ」在前、重合一直延伸到末尾：下标表短一截时越界（IndexError）
    pytest.param(_OLD_LETTER, "İ" + _OLD_LETTER, _OLD_LETTER, id="before-to-end"),
    # 「İ」在前、重合在中间：下标表短一截时命中整体错后一个字
    pytest.param(_OLD_LETTER, "İstanbul来信：" + _OLD_LETTER + "，信纸已经发黄", _OLD_LETTER, id="before-middle"),
    pytest.param(_OLD_LETTER, _OLD_LETTER + "İstanbul", _OLD_LETTER, id="after"),
    # 书里与检查文字都有「İ」：命中跨过它，映射回原文仍是完整的一段
    pytest.param("林昭在İzmir的案卷里找到一封旧信", "那天林昭在İzmir的案卷里找到一封旧信。", "林昭在İzmir的案卷里找到一封旧信", id="inside"),
]


def test_normalize_with_offsets_stays_aligned_when_lowercasing_adds_a_code_point() -> None:
    text = "İ他在 雨城，İzmir"
    normalized, offsets = normalize_with_offsets(text)
    assert normalized == normalize_text_for_matching(text)
    assert len(offsets) == len(normalized)
    # 每个规范化码位都指回它来自的那个原文字符
    assert all(char in text[offset].lower() for char, offset in zip(normalized, offsets, strict=True))
    assert offsets[:3] == [0, 0, 1]


@pytest.mark.parametrize("book_line, text, expected", _DOTTED_I_CASES)
def test_check_plagiarism_maps_hits_back_across_a_lowercase_expansion(book_line, text, expected) -> None:
    report = check_plagiarism(text, [book_line])
    assert not report.passed
    assert [hit.matched_text for hit in report.hits] == [expected]
    assert report.hits[0].position == text.index(expected)


@pytest.mark.parametrize("book_line, text, expected", _DOTTED_I_CASES)
def test_copy_gate_maps_hits_back_across_a_lowercase_expansion(session, book_line, text, expected) -> None:
    """抄袭门用同一个规范化与下标表：命中位置指回被检查文字里的原样一段，不越界（越界是归档 / 采纳 / 成稿门的 500）。"""
    book_id = make_book(session, "sr_book_dotted_i", paragraphs=[book_line, "灯下的人把信折好又打开，终于没有寄出去。"])
    session.commit()
    check = check_reference_copy(session, text, book_ids=[book_id])
    assert check.blocked
    assert [text[hit.start : hit.end] for hit in check.hits] == [expected]
