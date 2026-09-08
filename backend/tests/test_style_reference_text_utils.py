"""text_utils.py 单测:清洗 / 分段 / 分句 / checksum / 引号提取。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import pytest

from novel_system.services.errors import DomainError
from novel_system.services.style_reference.text_utils import (
    compact_ws,
    compute_text_checksum,
    decode_text,
    extract_dialogue_spans,
    normalize_text,
    split_paragraphs,
    split_sentences,
)


def test_decode_text_utf8() -> None:
    raw = "鲁迅短篇".encode("utf-8")
    assert decode_text(raw) == "鲁迅短篇"


def test_decode_text_gb18030_fallback() -> None:
    # "你好" 的 GB18030 编码 b'\xc4\xe3\xba\xc3' 不是合法 UTF-8(\xc4\xe3 头部失败),
    # 触发 fallback 到 GB18030 解码,返回 "你好"。
    raw = "你好".encode("gb18030")
    assert decode_text(raw) == "你好"


def test_decode_text_unsupported_raises() -> None:
    raw = b"\xff\xfe\xff\xfe\x00\x01"  # not valid UTF-8 nor GB18030
    with pytest.raises(DomainError) as exc_info:
        decode_text(raw)
    assert exc_info.value.code == "STYLE_REFERENCE_BOOK_ENCODING_UNSUPPORTED"


def test_normalize_text_crlf_to_lf() -> None:
    text = "第一段\r\n第二段\r第三段"
    assert "\r" not in normalize_text(text)


def test_normalize_text_strips_control_chars() -> None:
    text = "正文\x00\x07内容\x1f尾部"
    assert normalize_text(text) == "正文内容尾部"


def test_normalize_text_collapses_blank_lines() -> None:
    text = "第一段\n\n\n\n第二段\n\n\n第三段"
    normalized = normalize_text(text)
    assert "\n\n\n" not in normalized
    assert "第一段\n\n第二段\n\n第三段" == normalized


def test_normalize_text_strips_outer_whitespace() -> None:
    assert normalize_text("  \n  hello  \n  ") == "hello"


def test_compute_text_checksum_deterministic() -> None:
    text = "鲁迅短篇"
    assert compute_text_checksum(text) == compute_text_checksum(text)
    assert len(compute_text_checksum(text)) == 64


def test_compute_text_checksum_differs_on_change() -> None:
    assert compute_text_checksum("文本 A") != compute_text_checksum("文本 B")


def test_split_paragraphs_basic() -> None:
    text = "第一段。\n\n第二段。\n\n第三段。"
    paragraphs = split_paragraphs(text)
    assert [body for _s, _e, body in paragraphs] == ["第一段。", "第二段。", "第三段。"]


def test_split_paragraphs_offsets_correct() -> None:
    text = "第一段。\n\n第二段。"
    paragraphs = split_paragraphs(text)
    for start, end, body in paragraphs:
        assert text[start:end] == body


def test_split_paragraphs_skips_empty() -> None:
    text = "第一段。\n\n\n\n\n\n第二段。"
    paragraphs = split_paragraphs(text)
    assert len(paragraphs) == 2


def test_split_sentences_chinese() -> None:
    text = "天气很好。我出门去!你呢?…还在睡呢"
    sentences = split_sentences(text)
    assert sentences == ["天气很好", "我出门去", "你呢", "还在睡呢"]


def test_split_sentences_ignores_empty() -> None:
    assert split_sentences("。。。") == []
    assert split_sentences("") == []


def test_extract_dialogue_spans_english_quotes() -> None:
    text = '他说:"你好。"她答:"再见。"'
    spans = extract_dialogue_spans(text)
    assert "你好。" in spans
    assert "再见。" in spans


def test_extract_dialogue_spans_chinese_quotes() -> None:
    text = "他说:“你好。”她答:“再见。”"
    spans = extract_dialogue_spans(text)
    assert "你好。" in spans
    assert "再见。" in spans


def test_extract_dialogue_spans_japanese_quotes() -> None:
    text = "「你好。」「再见。」"
    spans = extract_dialogue_spans(text)
    assert "你好。" in spans
    assert "再见。" in spans


def test_compact_ws_collapses_whitespace() -> None:
    assert compact_ws("  hello   world\n\n!") == "hello world !"
    assert compact_ws("") == ""
    assert compact_ws(None) == ""  # type: ignore[arg-type]


def test_split_paragraphs_falls_back_to_single_newline_for_chapter_blob() -> None:
    """单换行分段的网文 TXT(无空行):空行切分退化为整章一段时,
    自动改按单换行切分。"""
    # 20 个 200 字"段落"以单换行连接,仅章节间有空行 → 空行切分平均段长 4000+
    para = "这是一个标准长度的中文叙述段落,用来模拟网络下载文本的常见排版习惯。" * 5
    chapter = "\n".join(para for _ in range(20))
    text = f"第一章\n{chapter}\n\n第二章\n{chapter}"
    paragraphs = split_paragraphs(text)
    assert len(paragraphs) >= 40, "应按单换行切出全部段落"
    bodies = [body for _s, _e, body in paragraphs]
    assert all(len(b) < 1000 for b in bodies)
    # offset 仍须回指原文
    for start, end, body in paragraphs[:5]:
        assert text[start:end] == body


def test_split_paragraphs_keeps_blank_line_mode_for_normal_text() -> None:
    """正常空行分段文本不触发兜底(黄金语料路径零影响)。"""
    text = "第一段内容。\n\n第二段内容,稍微长一点。\n\n第三段。"
    paragraphs = split_paragraphs(text)
    assert [b for _s, _e, b in paragraphs] == [
        "第一段内容。", "第二段内容,稍微长一点。", "第三段。",
    ]


# ---------------------------------------------------------------------------
# split_sentences v2(风格模仿 v2 · W2):闭引号归并 / ASCII 句点 / 无纯标点片段
# ---------------------------------------------------------------------------


def test_split_sentences_merges_closing_quote_into_previous_sentence() -> None:
    """`“走吧。”` 此前会切出只含 `”` 的幽灵句(拉低 avg_sentence_length);
    闭引号必须归并到前一句而不是开启下一句。"""
    assert split_sentences("他说：“走吧。”她点头。") == ["他说：“走吧”", "她点头"]
    assert split_sentences("‘对么？’") == ["‘对么’"]
    assert split_sentences("他不以为然了。含含胡胡的答道，‘不……’") == [
        "他不以为然了",
        "含含胡胡的答道，‘不’",
    ]
    assert split_sentences('他说:"你好。"她答:"再见。"') == ['他说:"你好"', '她答:"再见"']


def test_split_sentences_merges_closing_bracket() -> None:
    assert split_sentences("他走了。（她没有回头。）后来呢") == ["他走了", "（她没有回头）", "后来呢"]


def test_split_sentences_never_yields_pure_punctuation_fragment() -> None:
    import re

    for text in ("“不……”他愣住了。“……”", "。。。”", "——。", "他走了。……", "”", "“”"):
        for piece in split_sentences(text):
            assert re.search(r"[\w㐀-鿿]", piece), (text, piece)
    assert split_sentences("“不……”他愣住了。“……”") == ["“不”", "他愣住了"]


def test_split_sentences_ascii_period_only_before_whitespace_or_end() -> None:
    assert split_sentences("价格是3.5元。") == ["价格是3.5元"]
    assert split_sentences("www.example.com is a site. Really.") == [
        "www.example.com is a site",
        "Really",
    ]
    assert split_sentences("Hello.\nWorld") == ["Hello", "World"]
    assert split_sentences('She said "no." Then left.') == ['She said "no"', "Then left"]
    assert split_sentences("The end.") == ["The end"]


def test_split_sentences_golden_corpus_has_no_ghost_sentences() -> None:
    """鲁迅语料 500+ 处 `。’` / `？’`:分句后不得出现只含闭引号的片段。"""
    from pathlib import Path

    corpus = (
        Path(__file__).resolve().parent
        / "golden" / "style_reference" / "corpus" / "luxun_short_stories.txt"
    )
    text = normalize_text(corpus.read_text(encoding="utf-8"))
    ghosts = [
        s for s in split_sentences(text)
        if s.strip("”’」』）)\"' ") == ""
    ]
    assert ghosts == []
