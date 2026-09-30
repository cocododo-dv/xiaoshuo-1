"""Plagiarism 检测:规范化 n-gram 索引 + 扩展匹配。

参见《风格参考模块重构执行手册 v1.1》§7.3。
- ngram_size: 8(规范化后字符,短 n-gram 用于初步定位)
- threshold_chars: 12(命中后向后扩展,规范化后 ≥12 字符连续才算 plagiarism)

两个关键设计:
1. **规范化匹配** — 比较前去除空白与标点(Unicode P* 类别)并统一小写,
   防止「插空格 / 换标点」式微改绕过;命中位置通过偏移映射回原文。
2. **倒排索引建在 generated_text 上** — generated(通常 1-3k 字)远小于
   语料(全书段落,可达 30 万字)。对小文本建 n-gram dict、对语料单遍扫描,
   复杂度 O(len(generated) + sum(corpus_lengths)),全书语料也不超预算。
"""

from __future__ import annotations

import array
import bisect
import unicodedata
from collections.abc import Iterable

from novel_system.services.style_reference.schemas import (
    PlagiarismHit,
    PlagiarismReport,
)


def _is_ignorable(ch: str) -> bool:
    """规范化时丢弃的字符:空白 + 标点/符号(Unicode P* / S* 类别)。"""
    if ch.isspace():
        return True
    cat = unicodedata.category(ch)
    return cat.startswith("P") or cat.startswith("S")


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回 (规范化文本, 每个规范化字符在原文中的下标)。"""
    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        if _is_ignorable(ch):
            continue
        chars.append(ch.lower())
        index_map.append(i)
    return "".join(chars), index_map


def normalize_text_for_matching(text: str) -> str:
    """Normalize by lowering and stripping whitespace/punctuation for comparison."""
    return "".join(ch.lower() for ch in text if not _is_ignorable(ch))


def normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """与 :func:`normalize_text_for_matching` 同一规则，另返回每个规范化字符在原文里的下标
    （风格参考 v3 的抄袭门据此把命中区间映射回被检查的文字）。"""
    return _normalize_with_map(text)


_normalize = normalize_text_for_matching


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠/相邻的 [start, end) 区间,保持升序。"""
    if not intervals:
        return []
    intervals.sort()
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def check_plagiarism(
    generated_text: str,
    corpus_texts: list[str],
    *,
    ngram_size: int = 8,
    threshold_chars: int = 12,
) -> PlagiarismReport:
    """扫描 generated_text 是否与 corpus_texts(全书段落)存在
    规范化后 ≥threshold_chars 的连续重叠。"""
    empty = PlagiarismReport(
        passed=True,
        hits=[],
        ngram_size=ngram_size,
        threshold_chars=threshold_chars,
    )
    if not generated_text or not corpus_texts:
        return empty

    gen_norm, gen_map = _normalize_with_map(generated_text)
    if len(gen_norm) < ngram_size:
        return empty

    # 倒排索引:generated 的 n-gram → 规范化起点列表
    gen_ngrams: dict[str, list[int]] = {}
    for i in range(len(gen_norm) - ngram_size + 1):
        gen_ngrams.setdefault(gen_norm[i : i + ngram_size], []).append(i)

    # 对每段语料单遍扫描;命中区间记录在 generated 的规范化坐标系
    hit_intervals: list[tuple[int, int]] = []
    for corpus in corpus_texts:
        if not corpus:
            continue
        c_norm = _normalize(corpus)
        if len(c_norm) < ngram_size:
            continue
        j = 0
        while j <= len(c_norm) - ngram_size:
            window = c_norm[j : j + ngram_size]
            positions = gen_ngrams.get(window)
            if positions is None:
                j += 1
                continue
            best_len = 0
            for gpos in positions:
                match_len = ngram_size
                while (
                    j + match_len < len(c_norm)
                    and gpos + match_len < len(gen_norm)
                    and c_norm[j + match_len] == gen_norm[gpos + match_len]
                ):
                    match_len += 1
                if match_len >= threshold_chars:
                    hit_intervals.append((gpos, gpos + match_len))
                if match_len > best_len:
                    best_len = match_len
            # 跳过已匹配段,避免同一重叠重复报告
            j += best_len if best_len >= threshold_chars else 1

    if not hit_intervals:
        return empty

    hits: list[PlagiarismHit] = []
    for norm_start, norm_end in _merge_intervals(hit_intervals):
        orig_start = gen_map[norm_start]
        orig_end = gen_map[norm_end - 1] + 1
        hits.append(
            PlagiarismHit(
                matched_text=generated_text[orig_start:orig_end],
                position=orig_start,
                matched_length=norm_end - norm_start,
            )
        )

    return PlagiarismReport(
        passed=False,
        hits=hits,
        ngram_size=ngram_size,
        threshold_chars=threshold_chars,
    )


class BookNgramIndex:
    """一组段落（一本书）的 ``threshold_chars`` 字元哈希索引 + 规范化全文：一次建好，逐行 O(len(line)) 判重合。

    学习作业给文风卡 / 规划陈述逐行做原文重合过滤用它：每行调一次 ``check_plagiarism`` 要重新规范化并单遍扫描整本书
    （190 万字实测约 2 s/行，一次合成几分钟耗在这里）。抄袭门（``reference_copy_gate``）的书索引是同一个结构，
    :meth:`overlap_spans` 与 ``book_id`` 是给它按区间找命中、按书标命中用的。

    - :meth:`contains`：规范化后的一个 t 字元是不是某段的子串——先查有序哈希数组（二分；否定精确），命中再在
      规范化全文里逐字复核，哈希碰撞不会误报。段落之间用换行相连：规范化文本里没有空白，复核不会跨段拼出假命中。
    - :meth:`overlaps`：与 ``not check_plagiarism(text, corpus, ngram_size=k, threshold_chars=t).passed``（``k ≤ t``）
      同值——存在 ≥ t 个规范化字符的连续重叠 ⇔ 这行某个 t 字元是某段的子串。
    - :meth:`overlap_spans`：被检查文字（已规范化）里所有命中 t 字元的并集，合并成区间。
    """

    __slots__ = ("book_id", "threshold_chars", "hashes", "joined", "paragraph_count")

    def __init__(self, texts: Iterable[str], *, threshold_chars: int = 12, book_id: str | None = None) -> None:
        width = max(1, int(threshold_chars))
        norms = [_normalize(text) for text in texts if text]
        raw = array.array("q")
        append = raw.append
        for norm in norms:
            for i in range(len(norm) - width + 1):
                append(hash(norm[i : i + width]))
        self.book_id = book_id
        self.threshold_chars = width
        self.hashes = array.array("q", sorted(raw))
        self.joined = "\n".join(norms)
        self.paragraph_count = len(norms)

    def contains(self, gram: str) -> bool:
        """``gram``（已规范化的一个 t 字元）是不是某段的子串。"""
        value = hash(gram)
        position = bisect.bisect_left(self.hashes, value)
        if position >= len(self.hashes) or self.hashes[position] != value:
            return False
        return gram in self.joined

    def overlaps(self, text: str) -> bool:
        """这行与语料有没有 ≥ t 个规范化字符的连续重合（与 ``check_plagiarism`` 判定同值）。"""
        if not text or not text.strip():
            return False
        norm = _normalize(text)
        width = self.threshold_chars
        return any(self.contains(norm[i : i + width]) for i in range(len(norm) - width + 1))

    def overlap_spans(self, normalized: str) -> list[tuple[int, int]]:
        """``normalized``（已按 :func:`normalize_text_for_matching` 规范化）里所有命中 t 字元的并集：升序、合并后的
        ``[start, end)`` 区间（规范化坐标）。"""
        width = self.threshold_chars
        spans: list[list[int]] = []
        for start in range(len(normalized) - width + 1):
            if not self.contains(normalized[start : start + width]):
                continue
            if spans and start <= spans[-1][1]:
                spans[-1][1] = start + width
            else:
                spans.append([start, start + width])
        return [(begin, end) for begin, end in spans]
