"""抄袭门 / 原文重合检查的规范化（``style_reference.validation.plagiarism``）换成查表之后逐字节不变（审计 X01-04）。

规范化是安全门的一部分：丢哪些字、怎么转小写，一个码位都不能变。这里拿原来的逐字写法当参照，对整个 BMP 与几段
混排文字比较 ``normalize_text_for_matching`` 与带下标的 ``normalize_with_offsets``。
"""

from __future__ import annotations

import unicodedata

from novel_system.services.style_reference.validation.plagiarism import (
    normalize_text_for_matching,
    normalize_with_offsets,
)


def _reference_ignorable(ch: str) -> bool:
    if ch.isspace():
        return True
    category = unicodedata.category(ch)
    return category.startswith("P") or category.startswith("S")


def _reference_normalize(text: str) -> str:
    return "".join(ch.lower() for ch in text if not _reference_ignorable(ch))


def _reference_with_map(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    index_map: list[int] = []
    for index, ch in enumerate(text):
        if _reference_ignorable(ch):
            continue
        chars.append(ch.lower())
        index_map.append(index)
    return "".join(chars), index_map


SAMPLES = (
    "门外很安静，安静到能听见潮水。她突然意识到——自己一直在等这一刻！",
    "全角：ＡＢＣ　ａｂｃ　１２３，。！？「」『』【】《》……——～",
    "Mixed: Hello, WORLD! İstanbul ΣΑΣ straße ﬁ œ Æ 1,024.5% $€¥ ©®™ ←→↑↓ ★☆ ♪♫",
    "空白：\t\n\r\x0b\x0c         　\x1c\x1d\x1e\x1f 结束",
    "表情与增补平面：😀👍🏽🀄 𠀀𪚥 𝔘𝔫𝔦𝔠𝔬𝔡𝔢",
    "",
)


def test_normalization_is_byte_identical_over_the_whole_bmp() -> None:
    bmp = "".join(chr(codepoint) for codepoint in range(0x10000) if not 0xD800 <= codepoint <= 0xDFFF)
    assert normalize_text_for_matching(bmp) == _reference_normalize(bmp)
    assert normalize_with_offsets(bmp) == _reference_with_map(bmp)


def test_normalization_is_byte_identical_on_mixed_script_samples() -> None:
    for sample in SAMPLES:
        assert normalize_text_for_matching(sample) == _reference_normalize(sample), sample
        assert normalize_with_offsets(sample) == _reference_with_map(sample), sample
    # 同一个表反复用：第二遍（全部命中已记住的码位）结果不变
    for sample in SAMPLES:
        assert normalize_text_for_matching(sample) == _reference_normalize(sample), sample
