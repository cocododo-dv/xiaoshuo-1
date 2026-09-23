"""风格参考硬指标（纯函数，无 LLM）——测量核之上的旧指标名。

2026-09-23（风格参考 v3）：全部文本指标改由 ``measure.py`` 测量核计算，**汇总口径**（全文句子 / 全文字数上
直接算，不再「逐段算完求平均」——一句话的段和一百句话的段权重相同，真实参考书上问号密度 9.27 vs 汇总 4.8、
句长标准差 7.45 vs 21.8）、**唯一分段规则**（换行即段界，``\\n`` 与 ``\\n\\n`` 分段同值）、**可见字**为单位
（汉字 / 字母 / 数字，不含标点）。指标名保持不变，消费方不用改；五个感官词表指标（子串匹配把人名里的「明」
也算成视觉词）已删除，词表文件随之删除。

- ``MetricsEngine.compute_all(paragraphs)``：21 项指标（13 项文本 + 8 项段型比例；段型比例来自段落标签，
  其余来自测量核）；
- ``MetricsEngine.compute_with_variance(paragraphs)``：``{name: (全文值, 块间标准差)}``，块 ≈1500 字
  （≈ 一个场景），块间 std 是作者自己场景到场景的自然波动；
- ``compute_prose_shape_*``：5 项段落形状指标（不依赖段型标签）。
"""

from __future__ import annotations

import hashlib
import statistics
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from novel_system.services.style_reference.measure import (
    PUNCT_CHARS,
    TextMeasure,
    kernel_paragraphs,
    measure_paragraphs,
    per_1k,
    share,
)

# ---------------------------------------------------------------------------
# MetricName 全清单(21 项)
# ---------------------------------------------------------------------------

MetricName = Literal[
    # 语言层(13)
    "avg_sentence_length",
    "sentence_length_std",
    "short_sentence_ratio",
    "long_sentence_ratio",
    "punctuation_density_per_1k",
    "dash_em_density_per_1k",
    "ellipsis_density_per_1k",
    "semicolon_density_per_1k",
    "question_density_per_1k",
    "classical_word_ratio",
    "colloquial_marker_ratio",
    "metaphor_density_per_1k",
    "personification_density_per_1k",
    # 叙事层(8,paragraph_type 比例)
    "dialogue_ratio",
    "psychology_ratio",
    "description_env_ratio",
    "description_char_ratio",
    "action_ratio",
    "narration_ratio",
    "transition_ratio",
    "flashback_ratio",
]

TEXT_METRIC_NAMES: tuple[str, ...] = (
    "avg_sentence_length",
    "sentence_length_std",
    "short_sentence_ratio",
    "long_sentence_ratio",
    "punctuation_density_per_1k",
    "dash_em_density_per_1k",
    "ellipsis_density_per_1k",
    "semicolon_density_per_1k",
    "question_density_per_1k",
    "classical_word_ratio",
    "colloquial_marker_ratio",
    "metaphor_density_per_1k",
    "personification_density_per_1k",
)
_TYPE_METRICS: dict[str, str] = {
    "dialogue_ratio": "dialogue",
    "psychology_ratio": "psychology",
    "description_env_ratio": "description_env",
    "description_char_ratio": "description_char",
    "action_ratio": "action",
    "narration_ratio": "narration",
    "transition_ratio": "transition",
    "flashback_ratio": "flashback",
}
TYPE_METRIC_NAMES: tuple[str, ...] = tuple(_TYPE_METRICS)
METRIC_NAMES: tuple[str, ...] = (*TEXT_METRIC_NAMES, *TYPE_METRIC_NAMES)

# 与 METRIC_NAMES 分开存放（book.stats_json.prose_shape_metrics），再与画像的 metrics_baseline 合并。
# 完全由正文换段 / 标点形状计算，不依赖段型分类器。
PROSE_SHAPE_METRIC_NAMES: tuple[str, ...] = (
    "paragraph_mean_chars",
    "paragraph_length_std_chars",
    "paragraphs_per_1k",
    "single_sentence_paragraph_ratio",
    "quote_led_paragraph_ratio",
)

# 旧名：全部标点字符集（测量核里唯一一份）
_PUNCT_CHARS = PUNCT_CHARS
# 短句 / 长句门槛（可见字）
SHORT_SENTENCE_MAX_CHARS = 10
LONG_SENTENCE_MIN_CHARS = 30
# 块间方差的目标块大小(字符)。≈ 一个场景的长度;段落按累计字数到该值即切块,余段并入最后一块。
_VARIANCE_CHUNK_CHARS = 1500


# ---------------------------------------------------------------------------
# ParagraphRecord 与 MetricsEngine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParagraphRecord:
    """metrics 计算的最小段落抽象（正文 + 段型标签），不依赖 ORM 实例。"""

    text: str
    paragraph_type: str  # ParagraphType.value

    @property
    def char_count(self) -> int:
        return len(self.text)


def text_metrics(measure: TextMeasure) -> dict[str, float]:
    """测量结果 → 13 项文本指标（汇总口径）。"""
    chars = measure.char_count
    lengths = measure.sentence_chars
    count = len(lengths)
    punct = measure.punct_counts
    return {
        "avg_sentence_length": statistics.fmean(lengths) if count else 0.0,
        "sentence_length_std": statistics.pstdev(lengths) if count > 1 else 0.0,
        "short_sentence_ratio": share(sum(1 for n in lengths if n <= SHORT_SENTENCE_MAX_CHARS), count),
        "long_sentence_ratio": share(sum(1 for n in lengths if n >= LONG_SENTENCE_MIN_CHARS), count),
        "punctuation_density_per_1k": per_1k(measure.punct_total, chars),
        "dash_em_density_per_1k": per_1k(punct.get("dash", 0), chars),
        "ellipsis_density_per_1k": per_1k(punct.get("ellipsis", 0), chars),
        "semicolon_density_per_1k": per_1k(punct.get("semicolon", 0), chars),
        "question_density_per_1k": per_1k(punct.get("question", 0), chars),
        "classical_word_ratio": share(measure.classical_sentences, count),
        "colloquial_marker_ratio": share(measure.colloquial_sentences, count),
        "metaphor_density_per_1k": per_1k(measure.simile_markers, chars),
        "personification_density_per_1k": per_1k(measure.personification_markers, chars),
    }


def prose_shape_metrics(measure: TextMeasure) -> dict[str, float]:
    """测量结果 → 5 项段落形状指标。"""
    lengths = measure.paragraph_chars
    count = len(lengths)
    if not count:
        return {name: 0.0 for name in PROSE_SHAPE_METRIC_NAMES}
    return {
        "paragraph_mean_chars": statistics.fmean(lengths),
        "paragraph_length_std_chars": statistics.pstdev(lengths) if count > 1 else 0.0,
        "paragraphs_per_1k": per_1k(count, measure.char_count),
        "single_sentence_paragraph_ratio": share(
            sum(1 for sentences in measure.paragraph_sentences if sentences <= 1), count
        ),
        "quote_led_paragraph_ratio": share(measure.quote_led_paragraphs, count),
    }


def _type_ratios(paragraphs: list[ParagraphRecord]) -> dict[str, float]:
    usable = [p for p in paragraphs if str(p.text or "").strip()]
    total = len(usable)
    return {
        name: share(sum(1 for p in usable if p.paragraph_type == ptype), total)
        for name, ptype in _TYPE_METRICS.items()
    }


def _chunk_spans(paragraphs: list[ParagraphRecord], target_chars: int) -> list[tuple[int, int]]:
    """``_chunk_by_chars`` 的下标版：[(start, end)]，end 不含。"""
    spans: list[tuple[int, int]] = []
    start = 0
    current_chars = 0
    for index, p in enumerate(paragraphs):
        current_chars += p.char_count
        if current_chars >= target_chars:
            spans.append((start, index + 1))
            start = index + 1
            current_chars = 0
    if start < len(paragraphs):
        if spans:
            spans[-1] = (spans[-1][0], len(paragraphs))  # 残尾并入最后一块
        else:
            spans.append((start, len(paragraphs)))
    return spans


def _chunk_measures(paragraphs: list[ParagraphRecord]) -> list[tuple[int, int, TextMeasure]]:
    """≈1500 字块的轻量测量（按内容哈希记忆最近两份：导入统计先后要指标与段落形状的块间方差）。"""
    spans = _chunk_spans(paragraphs, _VARIANCE_CHUNK_CHARS)
    key = (
        hashlib.sha1("\x1e".join(p.text for p in paragraphs).encode("utf-8")).hexdigest(),
        _VARIANCE_CHUNK_CHARS,
    )
    with _CHUNK_CACHE_LOCK:
        cached = _CHUNK_CACHE.get(key)
        if cached is not None:
            _CHUNK_CACHE.move_to_end(key)
            return cached
    result = [
        (start, end, measure_paragraphs((p.text for p in paragraphs[start:end]), detailed=False))
        for start, end in spans
    ]
    with _CHUNK_CACHE_LOCK:
        _CHUNK_CACHE[key] = result
        while len(_CHUNK_CACHE) > 2:
            _CHUNK_CACHE.popitem(last=False)
    return result


_CHUNK_CACHE_LOCK = threading.Lock()
_CHUNK_CACHE: OrderedDict[tuple[str, int], list[tuple[int, int, TextMeasure]]] = OrderedDict()


class MetricsEngine:
    """21 项 MetricName 的纯函数实现（文本指标来自测量核，段型比例来自段落标签）。

    用法::

        engine = MetricsEngine()
        values = engine.compute_all(paragraphs)              # dict[name, float]
        var    = engine.compute_with_variance(paragraphs)    # dict[name, (value, chunk_std)]
    """

    def compute_all(self, paragraphs: list[ParagraphRecord]) -> dict[str, float]:
        if not paragraphs:
            return {name: 0.0 for name in METRIC_NAMES}
        measure = measure_paragraphs(p.text for p in paragraphs)
        return {**text_metrics(measure), **_type_ratios(paragraphs)}

    def compute_with_variance(
        self, paragraphs: list[ParagraphRecord]
    ) -> dict[str, tuple[float, float]]:
        """返回 {metric: (全文值, 块间标准差)}。

        全文值 == ``compute_all``（汇总口径），不随分块变化；std 是 ≈1500 字块（≈ 一个场景）之间的标准差——
        画像的 ``metrics_baseline`` 拿它对照「一整场生成文本」的单值（候选评分核的目标包络），容差应反映作者自己
        场景到场景的自然波动。单块（短语料）时 std=0，
        由 tolerance floor 兜底。
        """
        if not paragraphs:
            return {name: (0.0, 0.0) for name in METRIC_NAMES}
        values = self.compute_all(paragraphs)
        chunks = _chunk_measures(paragraphs)
        if len(chunks) <= 1:
            return {name: (values[name], 0.0) for name in METRIC_NAMES}
        chunk_values = [
            {**text_metrics(measure), **_type_ratios(paragraphs[start:end])} for start, end, measure in chunks
        ]
        return {
            name: (values[name], statistics.pstdev(chunk[name] for chunk in chunk_values))
            for name in METRIC_NAMES
        }

    def _per_paragraph(self, name: str, p: ParagraphRecord) -> float:
        """单段的指标值（旧逐段口径；仅供对照测试）。"""
        if name not in METRIC_NAMES:
            raise ValueError(f"unknown metric: {name}")
        return self.compute_all([p])[name]


def compute_prose_shape_metrics(
    paragraphs: list[ParagraphRecord],
) -> dict[str, float]:
    """段落形状（不读 paragraph_type）。"""
    usable = [p for p in paragraphs if str(p.text or "").strip()]
    if not usable:
        return {name: 0.0 for name in PROSE_SHAPE_METRIC_NAMES}
    return prose_shape_metrics(measure_paragraphs(p.text for p in usable))


def compute_prose_shape_with_variance(
    paragraphs: list[ParagraphRecord],
) -> dict[str, tuple[float, float]]:
    """全文段落形状与约场景大小的块间自然波动。"""
    if not paragraphs:
        return {name: (0.0, 0.0) for name in PROSE_SHAPE_METRIC_NAMES}
    values = compute_prose_shape_metrics(paragraphs)
    chunks = _chunk_measures(paragraphs)
    if len(chunks) <= 1:
        return {name: (values[name], 0.0) for name in PROSE_SHAPE_METRIC_NAMES}
    chunk_values = [prose_shape_metrics(measure) for _start, _end, measure in chunks]
    return {
        name: (values[name], statistics.pstdev(chunk[name] for chunk in chunk_values))
        for name in PROSE_SHAPE_METRIC_NAMES
    }


def compute_prose_shape_from_text(text: str) -> dict[str, float]:
    """生成稿 / 作者稿的段落形状：按测量核的唯一分段规则（换行即段界，HTML 先取段）。"""
    return prose_shape_metrics(measure_paragraphs(kernel_paragraphs(text)))


def _chunk_by_chars(
    paragraphs: list[ParagraphRecord], target_chars: int
) -> list[list[ParagraphRecord]]:
    """把段落按累计字数切成 ≈target_chars 的块(块间 std 的样本单位)。

    末尾不足 target_chars 的残块:若已有其它块则并入最后一块(避免短尾块拉偏
    方差),否则自成一块。整体字数 < target_chars 时返回单块。
    """
    chunks: list[list[ParagraphRecord]] = []
    current: list[ParagraphRecord] = []
    current_chars = 0
    for p in paragraphs:
        current.append(p)
        current_chars += p.char_count
        if current_chars >= target_chars:
            chunks.append(current)
            current = []
            current_chars = 0
    if current:
        if chunks:
            chunks[-1].extend(current)  # 残尾并入最后一块
        else:
            chunks.append(current)
    return chunks


# 2026-09-23 风格参考 v3（P5b）：旧校验层删除时从 ``validation/quantitative.py`` 搬来——候选贴合读数与
# neutral_first 的形状包络还在用这两样（生成稿的纯文本指标）。分类器标签依赖的 8 项比例指标对生成稿无意义
#（生成稿没有段落类型，全部当叙述），不参与任何对照。
DEFAULT_FLOOR = 0.1
TYPE_RATIO_METRICS: frozenset[str] = frozenset(TYPE_METRIC_NAMES)


def compute_generated_metrics(generated_text: str) -> dict[str, float]:
    """生成稿（或作者稿 HTML）的全部可观测指标：按测量核的唯一分段规则切段、全部当叙述。"""
    paragraphs = [ParagraphRecord(text=p, paragraph_type="narration") for p in kernel_paragraphs(generated_text)]
    if not paragraphs:
        return {}
    metrics = MetricsEngine().compute_all(paragraphs)
    metrics.update(compute_prose_shape_metrics(paragraphs))
    return metrics


__all__ = [
    "DEFAULT_FLOOR",
    "LONG_SENTENCE_MIN_CHARS",
    "METRIC_NAMES",
    "MetricName",
    "MetricsEngine",
    "PROSE_SHAPE_METRIC_NAMES",
    "ParagraphRecord",
    "SHORT_SENTENCE_MAX_CHARS",
    "TEXT_METRIC_NAMES",
    "TYPE_METRIC_NAMES",
    "TYPE_RATIO_METRICS",
    "compute_generated_metrics",
    "compute_prose_shape_from_text",
    "compute_prose_shape_metrics",
    "compute_prose_shape_with_variance",
    "prose_shape_metrics",
    "text_metrics",
]
