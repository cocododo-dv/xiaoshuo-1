"""Evidence span / quote 对齐：模型给的引文必须是原文里连续、逐字一致的一小段。

学习作业（``learn_extract``）把模型给的「段号 + 引文」映射回原文坐标：先按给定的段对齐，失败时只允许在
整个样本集里**唯一**反查；伪造、拼接、加省略号的引文一律对不上（返回 None）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from novel_system.services.style_reference.text_utils import compact_ws

if TYPE_CHECKING:
    from novel_system.services.style_reference.schemas import ExtractionEvidenceInput


_MODEL_PARAGRAPH_CHAR_LIMIT = 600
_QUOTE_MARK_TRANSLATION = str.maketrans(
    {
        "'": '"',
        "‘": '"',
        "’": '"',
        "“": '"',
        "”": '"',
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
    }
)


def align_paragraph_evidence(
    evidence: ExtractionEvidenceInput,
    paragraph_text: str,
    *,
    prompt_char_limit: int = _MODEL_PARAGRAPH_CHAR_LIMIT,
) -> ExtractionEvidenceInput | None:
    """把模型基于压缩段落返回的引文坐标，安全地映射回原文。

    抽取 prompt 只暴露 ``compact_ws(paragraph_text)[:600]``。因此模型给出的
    quote/span 可能位于压缩后的坐标系，而数据库需要保存原文坐标。这里仅在
    以下任一条件成立时接受：

    1. 给定 span 精确指向某个原文或压缩文本中的匹配项；
    2. quote 在模型实际可见的压缩文本中只出现一次。

    重复短语且 span 无法消歧、跨句拼接的省略式摘要、超出 prompt 可见范围的
    引文都会返回 ``None``。成功时 quote 会被替换为原文精确切片，span 也会
    规范化为原文的 ``[start, end)`` 坐标。
    """
    compact_quote = compact_ws(evidence.quote)
    if not compact_quote:
        return None

    prompt_view, raw_ranges = _compacted_prompt_view(
        paragraph_text,
        limit=prompt_char_limit,
    )
    matches = _all_occurrences(
        prompt_view.translate(_QUOTE_MARK_TRANSLATION),
        compact_quote.translate(_QUOTE_MARK_TRANSLATION),
    )
    if not matches:
        return None

    candidates = [
        (
            start,
            end,
            raw_ranges[start][0],
            raw_ranges[end - 1][1],
        )
        for start, end in matches
    ]

    supplied = evidence.span
    selected: tuple[int, int, int, int] | None = None
    if supplied is not None:
        supplied_start, supplied_end = supplied
        if supplied_start >= 0 and supplied_end >= supplied_start:
            # Provider 可能返回原文坐标，也可能返回 prompt 中的压缩坐标；两种
            # 都只在它精确命中当前 quote 时用于消歧。
            exact = [
                candidate
                for candidate in candidates
                if (candidate[2], candidate[3]) == supplied
                or (candidate[0], candidate[1]) == supplied
            ]
            if len(exact) == 1:
                selected = exact[0]

    if selected is None and len(candidates) == 1:
        selected = candidates[0]
    if selected is None:
        return None

    raw_start, raw_end = selected[2], selected[3]
    return evidence.model_copy(
        update={
            "quote": paragraph_text[raw_start:raw_end],
            "span": (raw_start, raw_end),
        }
    )


def align_evidence_to_paragraph_lookup(
    evidence: ExtractionEvidenceInput,
    paragraph_lookup: dict[str, str],
    *,
    prompt_char_limit: int = _MODEL_PARAGRAPH_CHAR_LIMIT,
) -> ExtractionEvidenceInput | None:
    """优先按显式 paragraph_id 对齐；失败时只允许跨段落唯一反查。

    ``prompt_char_limit`` 是模型实际看到的每段前多少字（学习作业送整段，传一个足够大的值）。
    """
    if evidence.paragraph_id is not None:
        text = paragraph_lookup.get(evidence.paragraph_id)
        if text is not None:
            direct = align_paragraph_evidence(evidence, text, prompt_char_limit=prompt_char_limit)
            if direct is not None:
                return direct

    candidates: list[ExtractionEvidenceInput] = []
    for paragraph_id, text in paragraph_lookup.items():
        candidate = align_paragraph_evidence(
            evidence.model_copy(update={"paragraph_id": paragraph_id}),
            text,
            prompt_char_limit=prompt_char_limit,
        )
        if candidate is not None:
            candidates.append(candidate)
            if len(candidates) > 1:
                return None
    return candidates[0] if candidates else None


def _compacted_prompt_view(text: str, *, limit: int) -> tuple[str, list[tuple[int, int]]]:
    """返回与 ``compact_ws(text)[:limit]`` 等价的文本及逐字符原文映射。"""
    if limit <= 0:
        return "", []

    chars: list[str] = []
    raw_ranges: list[tuple[int, int]] = []
    tokens = list(re.finditer(r"\S+", text))
    for token_index, token in enumerate(tokens):
        if token_index:
            previous = tokens[token_index - 1]
            chars.append(" ")
            raw_ranges.append((previous.end(), token.start()))
            if len(chars) >= limit:
                break
        for raw_index in range(token.start(), token.end()):
            chars.append(text[raw_index])
            raw_ranges.append((raw_index, raw_index + 1))
            if len(chars) >= limit:
                break
        if len(chars) >= limit:
            break
    return "".join(chars), raw_ranges


def _all_occurrences(text: str, quote: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(quote, start)
        if index < 0:
            return matches
        matches.append((index, index + len(quote)))
        start = index + 1
