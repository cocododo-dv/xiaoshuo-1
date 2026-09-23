"""风格参考 v3（2026-09-23）—「像不像作者」的确定性读数（纯函数 + 一个按书缓存的入口）。

过去的漂移读数拿鲁迅 / 朱自清（1920 年代散文）的基线当尺子，作者自己的书上 95–98% 报警（台账 V2）。
这里的尺子是**作者自己**：参照分布 = 这本书全部样例窗口（``windows.py``）在测量核特征上的分布——

- 每个特征：稳健中心（中位数）与尺度（``measure.robust_scale``：max(1.4826·MAD, (p90−p10)/2.5631, 单位下限)），
  作者几乎从不用的东西（尺度≈0）不会把 z 值炸飞；
- 一段文字的 **distance** = 各特征 |z|（封顶 ``Z_CLIP``）的加权平均：每个可测维度合计权重 1（维度内的特征
  平分；未映射到维度的特征合成一组「其他」），「重点」维 ×2、「不学」维 0；
- **percentile** = 作者自己的窗口里（留一：每窗对「去掉它自己」的分布算）distance ≤ 这段文字的比例（0–100）——
  作者自己的一窗平均落在 50 左右，越高越不像；
- **out_of_band** = |z| ≥ ``OUT_OF_BAND_Z`` 的特征（按 |z| 排序），每条带维度与给作者看的白话短语
  （「句末语气词（吧、呢、啊、嘛）比作者少」）；
- **dimension_scores** = 可测维度的 0–10 分（10 − 2.5 × 该维 |z| 均值）。

``recent_gap_phrases`` 从同一作品最近几次读数里挑出反复越界的短语（「近期常见偏差」）。读数入库只有一个入口
``readings.record_fidelity_reading``（P5）；本模块不写库。
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import threading
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook
from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    DIMENSION_EMPHASIZE,
    DIMENSION_EXCLUDE,
    normalize_dimension_states,
)
from novel_system.services.style_reference.measure import (
    FEATURE_NAMES,
    KERNEL_VERSION,
    TextMeasure,
    kernel_features,
    measure_text,
    quantile,
    robust_scale,
)
from novel_system.services.style_reference.windows import (
    WINDOW_INDEX_VERSION,
    ensure_window_index,
    index_marker,
    marker_is_current,
)

FIDELITY_VERSION = "fidelity_v1"
# 单个特征的 |z| 封顶：一个特征离谱不该压过其余几十个
Z_CLIP = 6.0
# |z| ≥ 此值算越界（作者自己的窗口里约二十分之一的特征会越过它）
OUT_OF_BAND_Z = 2.0
# 维度分：10 − DIMENSION_SCORE_SLOPE × 该维 |z| 均值（截到 0–10）
DIMENSION_SCORE_SLOPE = 2.5
# 少于这些可见字的文字、少于这些窗口的参照：读数照给，但标 reliable=False
MIN_RELIABLE_CHARS = 600
MIN_REFERENCE_WINDOWS = 8
OTHER_GROUP = "_other"

# 比例类特征要有足够的「分母」才值得告诉作者：两句对白里的引导动词份额、十几句里的句长分位都是噪声。
# 证据不够的特征照算进 distance（与参照窗口同口径），只是不进越界清单。
_EVIDENCE_RULES: tuple[tuple[str, str, int], ...] = (
    ("speech_verb_", "guided_quotes", 5),
    ("dialogue_guide_", "quotes", 5),
    ("person_", "person_mentions", 10),
    ("sentence_final_", "sentences", 20),
    ("sent_len_p", "sentences", 20),
    ("sent_len_lag1", "sentences", 20),
    ("sent_short_run", "sentences", 20),
    ("para_len_std", "paragraphs", 5),
    ("para_single_sentence", "paragraphs", 5),
    ("para_dialogue", "paragraphs", 5),
)

# ---------------------------------------------------------------------------
# 特征 → 维度（只收可以确定性测的维；修辞、环境、人物、感官、主题四维由评审模型按样例打分）
# ---------------------------------------------------------------------------

_SENTENCE = "language.sentence_structure"
_VOCABULARY = "language.vocabulary"
_PUNCTUATION = "language.punctuation"
_PERSPECTIVE = "narrative.perspective"
_PACING = "narrative.pacing"
_DENSITY = "narrative.information_density"
_DIALOGUE = "scene.dialogue"

FEATURE_DIMENSIONS: dict[str, str] = {
    # 句式结构：句长分布、句内停顿、结构助词与体标记
    "sent_len_mean": _SENTENCE,
    "sent_len_std": _SENTENCE,
    "sent_len_p10": _SENTENCE,
    "sent_len_p90": _SENTENCE,
    "sent_pauses_mean": _SENTENCE,
    "clause_len_mean": _SENTENCE,
    "fw_particle_per_1k": _SENTENCE,
    "fw_aspect_per_1k": _SENTENCE,
    # 词汇：虚词偏好、句末语气词、四字格与叠词、用字丰富度、夹用英文
    "fw_connective_per_1k": _VOCABULARY,
    "fw_adverb_per_1k": _VOCABULARY,
    "fw_preposition_per_1k": _VOCABULARY,
    "fw_pronoun_per_1k": _VOCABULARY,
    "fw_modal_per_1k": _VOCABULARY,
    "fw_classical_per_1k": _VOCABULARY,
    "sentence_final_modal_ratio": _VOCABULARY,
    "sentence_final_classical_ratio": _VOCABULARY,
    "four_char_segment_per_1k": _VOCABULARY,
    "redup_aa_per_1k": _VOCABULARY,
    "redup_aabb_per_1k": _VOCABULARY,
    "redup_abab_per_1k": _VOCABULARY,
    "redup_total_per_1k": _VOCABULARY,
    "lexical_char_ttr": _VOCABULARY,
    "lexical_bigram_hapax_ratio": _VOCABULARY,
    "latin_word_per_1k": _VOCABULARY,
    # 标点节奏
    "punct_comma_per_1k": _PUNCTUATION,
    "punct_enumeration_per_1k": _PUNCTUATION,
    "punct_period_per_1k": _PUNCTUATION,
    "punct_colon_per_1k": _PUNCTUATION,
    "punct_semicolon_per_1k": _PUNCTUATION,
    "punct_exclamation_per_1k": _PUNCTUATION,
    "punct_question_per_1k": _PUNCTUATION,
    "punct_ellipsis_per_1k": _PUNCTUATION,
    "punct_dash_per_1k": _PUNCTUATION,
    # 叙事视角：叙述里的人称
    "person_first_share": _PERSPECTIVE,
    "person_second_share": _PERSPECTIVE,
    "person_third_share": _PERSPECTIVE,
    # 节奏：段落长短、一句一段、短句连打、长短句起落
    "para_len_mean": _PACING,
    "para_len_std": _PACING,
    "para_single_sentence_ratio": _PACING,
    "sent_short_run_mean": _PACING,
    "sent_short_run_ratio": _PACING,
    "sent_len_lag1_autocorr": _PACING,
    # 信息密度：具体数字与带单位的数量
    "digit_run_per_1k": _DENSITY,
    "numeral_unit_per_1k": _DENSITY,
    # 对话写法：对白比重、对白引导的位置与动词
    "dialogue_char_share": _DIALOGUE,
    "para_dialogue_ratio": _DIALOGUE,
    "punct_quote_pair_per_1k": _DIALOGUE,
    "dialogue_guide_pre_share": _DIALOGUE,
    "dialogue_guide_post_share": _DIALOGUE,
    "dialogue_guide_none_share": _DIALOGUE,
    "speech_verb_shuodao_share": _DIALOGUE,
    "speech_verb_shuo_share": _DIALOGUE,
    "speech_verb_dao_share": _DIALOGUE,
    "speech_verb_wen_share": _DIALOGUE,
    "speech_verb_da_share": _DIALOGUE,
    "speech_verb_other_share": _DIALOGUE,
}
MEASURABLE_DIMENSIONS: tuple[str, ...] = tuple(
    dim for dim in ALL_DIMENSIONS if dim in set(FEATURE_DIMENSIONS.values())
)
# 反查:可测维 → 它的测量核特征(文风卡 ``DimensionEntry.measurable_features`` / 定向修改用)
DIMENSION_FEATURES: dict[str, tuple[str, ...]] = {
    dim: tuple(name for name in FEATURE_NAMES if FEATURE_DIMENSIONS.get(name) == dim) for dim in MEASURABLE_DIMENSIONS
}

# 给作者看、也写进定向修改提示的白话短语：(比作者多时, 比作者少时)。不出现统计术语。
FEATURE_PHRASES: dict[str, dict[str, str]] = {
    "fw_particle_per_1k": {"high": "「的、地、得」比作者用得多，修饰语堆得太满", "low": "「的、地、得」比作者用得少，修饰语太省"},
    "fw_aspect_per_1k": {"high": "「了、着、过」比作者多，动作后面拖着尾巴", "low": "「了、着、过」比作者少，动作交代得太干"},
    "fw_connective_per_1k": {"high": "连接词（而、但、却、于是……）比作者多，句间关系点得太明", "low": "连接词（就、也、还、可是……）比作者少，句子之间缺少作者那样的衔接"},
    "fw_adverb_per_1k": {"high": "副词（很、已经、忽然、似乎……）比作者多", "low": "副词（都、只、已经、终于……）比作者少"},
    "fw_preposition_per_1k": {"high": "介词（在、把、被、对……）比作者多，句子框架偏书面", "low": "介词（在、把、给……）比作者少"},
    "fw_pronoun_per_1k": {"high": "代词（他、这、那、什么……）比作者多", "low": "代词比作者少，人和物总用名字称呼"},
    "fw_modal_per_1k": {"high": "语气词（吧、呢、啊、嘛……）比作者多", "low": "语气词（吧、呢、啊、嘛……）比作者少，口气不如作者松"},
    "fw_classical_per_1k": {"high": "文言虚词（之、其、乃、亦……）比作者多", "low": "文言虚词（之、其、以、于……）比作者少"},
    "fw_total_per_1k": {"high": "虚词整体比作者多，句子偏松", "low": "虚词整体比作者少，句子偏紧、偏书面"},
    "sentence_final_modal_ratio": {"high": "句末语气词（吧、呢、啊、嘛）比作者多", "low": "句末语气词（吧、呢、啊、嘛）比作者少"},
    "sentence_final_classical_ratio": {"high": "句末文言语气（也、矣、乎）比作者多", "low": "句末文言语气（也、矣、乎）比作者少"},
    "punct_comma_per_1k": {"high": "逗号比作者多，一句里停得太碎", "low": "逗号比作者少，句子一口气说到底"},
    "punct_enumeration_per_1k": {"high": "顿号列举比作者多", "low": "顿号列举比作者少"},
    "punct_period_per_1k": {"high": "句号比作者多，句子断得太碎", "low": "句号比作者少，句子拖得太长"},
    "punct_colon_per_1k": {"high": "冒号比作者多", "low": "冒号比作者少"},
    "punct_semicolon_per_1k": {"high": "分号比作者多", "low": "分号比作者少"},
    "punct_exclamation_per_1k": {"high": "感叹号比作者多", "low": "感叹号比作者少，情绪收得比作者紧"},
    "punct_question_per_1k": {"high": "问句比作者多", "low": "问句（包括反问、自问）比作者少"},
    "punct_ellipsis_per_1k": {"high": "省略号比作者多", "low": "省略号比作者少"},
    "punct_dash_per_1k": {"high": "破折号比作者多", "low": "破折号比作者少"},
    "punct_quote_pair_per_1k": {"high": "引号（对白、引语）比作者多", "low": "引号（对白、引语）比作者少"},
    "sent_len_mean": {"high": "句子比作者长", "low": "句子比作者短"},
    "sent_len_std": {"high": "句子长短起伏比作者大", "low": "句子长短比作者均匀，缺少长短交错"},
    "sent_len_p10": {"high": "几个字的极短句比作者少", "low": "几个字的极短句比作者多"},
    "sent_len_p90": {"high": "长句比作者更长", "low": "长句比作者少、也短"},
    "sent_pauses_mean": {"high": "一句里的停顿比作者多", "low": "一句里的停顿比作者少"},
    "clause_len_mean": {"high": "两个停顿之间的话比作者长，读着喘不过气", "low": "两个停顿之间的话比作者短，节奏太碎"},
    "sent_short_run_mean": {"high": "连着的短句串比作者长", "low": "连着的短句串比作者短"},
    "sent_short_run_ratio": {"high": "短句连打比作者多", "low": "短句连打比作者少"},
    "sent_len_lag1_autocorr": {"high": "相邻句子长短比作者接近，缺少一长一短的起落", "low": "相邻句子长短跳得比作者厉害"},
    "para_len_mean": {"high": "段落比作者长，换段太少", "low": "段落比作者短，换段太勤"},
    "para_len_std": {"high": "段落长短差得比作者大", "low": "段落长短比作者整齐"},
    "para_single_sentence_ratio": {"high": "一句一段比作者多", "low": "一句一段比作者少"},
    "para_dialogue_ratio": {"high": "对白段比作者多", "low": "对白段比作者少"},
    "dialogue_guide_pre_share": {"high": "「某某说：」放在话前面的写法比作者多", "low": "「某某说：」放在话前面的写法比作者少"},
    "dialogue_guide_post_share": {"high": "话说完再补「某某说」的写法比作者多", "low": "话说完再补「某某说」的写法比作者少"},
    "dialogue_guide_none_share": {"high": "不加「某某说」、直接让人物开口的对白比作者多", "low": "对白比作者更常加「某某说」"},
    "speech_verb_shuodao_share": {"high": "用「说道」引出对白比作者多", "low": "用「说道」引出对白比作者少"},
    "speech_verb_shuo_share": {"high": "用「说」引出对白比作者多", "low": "用「说」引出对白比作者少"},
    "speech_verb_dao_share": {"high": "用「道」（笑道、问道……）引出对白比作者多", "low": "用「道」（笑道、问道……）引出对白比作者少"},
    "speech_verb_wen_share": {"high": "用「问」引出对白比作者多", "low": "用「问」引出对白比作者少"},
    "speech_verb_da_share": {"high": "用「答」引出对白比作者多", "low": "用「答」引出对白比作者少"},
    "speech_verb_other_share": {"high": "用「喊、笑、骂、叹」一类动词引出对白比作者多", "low": "用「喊、笑、骂、叹」一类动词引出对白比作者少"},
    "four_char_segment_per_1k": {"high": "四字短语、成语比作者多", "low": "四字短语、成语比作者少"},
    "redup_aa_per_1k": {"high": "叠字（慢慢、轻轻）比作者多", "low": "叠字（慢慢、轻轻）比作者少"},
    "redup_aabb_per_1k": {"high": "「干干净净」式的叠词比作者多", "low": "「干干净净」式的叠词比作者少"},
    "redup_abab_per_1k": {"high": "「商量商量」式的叠词比作者多", "low": "「商量商量」式的叠词比作者少"},
    "redup_total_per_1k": {"high": "叠词整体比作者多", "low": "叠词整体比作者少"},
    "person_first_share": {"high": "叙述里的「我」比作者多", "low": "叙述里的「我」比作者少"},
    "person_second_share": {"high": "叙述里的「你」比作者多", "low": "叙述里的「你」比作者少"},
    "person_third_share": {"high": "叙述里的「他 / 她」比作者多", "low": "叙述里的「他 / 她」比作者少"},
    "lexical_char_ttr": {"high": "用字比作者杂，同样的字重复得少", "low": "用字比作者单调，同几个字反复出现"},
    "lexical_bigram_hapax_ratio": {"high": "词语搭配比作者更少重复", "low": "词语搭配比作者重复得多"},
    "dialogue_char_share": {"high": "对白比作者多", "low": "对白比作者少"},
    "digit_run_per_1k": {"high": "阿拉伯数字比作者多", "low": "具体数字（时间、编号、数量）比作者少"},
    "numeral_unit_per_1k": {"high": "带单位的数量（几秒、几米、几公里）比作者多", "low": "带单位的具体数量（几秒、几米、几公里）比作者少"},
    "latin_word_per_1k": {"high": "英文词比作者多", "low": "夹用英文词、字母缩写比作者少"},
}


# ---------------------------------------------------------------------------
# 参照分布
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceDistribution:
    """作者自己一组窗口在测量核特征上的分布（``build_reference_distribution`` 产出，只读）。"""

    features: tuple[str, ...]
    center: Mapping[str, float]
    scale: Mapping[str, float]
    p10: Mapping[str, float]
    p90: Mapping[str, float]
    window_count: int
    # 每窗（按输入顺序）对「去掉它自己」的分布的逐特征 min(|z|, Z_CLIP)——算百分位用
    loo_abs_z: tuple[tuple[float, ...], ...]
    kernel_version: str = KERNEL_VERSION
    reference_version: str = ""
    source: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reliable(self) -> bool:
        return self.window_count >= MIN_REFERENCE_WINDOWS

    def loo_distances(self, dimension_states: Mapping[str, Any] | None = None) -> list[float]:
        """每个参照窗口的留一 distance（与 ``read_fidelity`` 同一套权重）。"""
        weights = feature_weights(self.features, dimension_states)
        total = sum(weights.values())
        if total <= 0:
            weights = feature_weights(self.features, None)
            total = sum(weights.values())
        vector = [weights[name] for name in self.features]
        return [sum(w * z for w, z in zip(vector, row)) / total for row in self.loo_abs_z]

    def summary(self) -> dict[str, Any]:
        """画像 / 界面可存的摘要：每个特征的作者典型值与常态范围（p10–p90）。"""
        return {
            "kernel_version": self.kernel_version,
            "reference_version": self.reference_version,
            "window_count": self.window_count,
            "features": {
                name: {
                    "center": round(float(self.center[name]), 6),
                    "scale": round(float(self.scale[name]), 6),
                    "p10": round(float(self.p10[name]), 6),
                    "p90": round(float(self.p90[name]), 6),
                }
                for name in self.features
            },
        }


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _quantile_without(ordered: Sequence[float], removed: int, ratio: float) -> float:
    """``ordered`` 去掉第 ``removed`` 个元素后的线性插值分位数（与 ``measure.quantile`` 同口径）。"""
    size = len(ordered) - 1
    if size <= 0:
        return 0.0

    def at(k: int) -> float:
        return float(ordered[k if k < removed else k + 1])

    position = (size - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return at(lower)
    fraction = position - lower
    return at(lower) * (1.0 - fraction) + at(upper) * fraction


def _loo_center_scale(name: str, values: Sequence[float]) -> list[tuple[float, float]]:
    """每个元素的留一 (中心, 尺度)：中位数 / MAD / p10 / p90 都按「去掉它」精确计算（O(n log n)）。"""
    n = len(values)
    order = sorted(range(n), key=lambda index: (values[index], index))
    ordered = [values[index] for index in order]
    rank = [0] * n
    for position, index in enumerate(order):
        rank[index] = position
    centers = [_quantile_without(ordered, rank[i], 0.5) for i in range(n)]
    deviations_cache: dict[float, list[float]] = {}
    result: list[tuple[float, float]] = []
    for i in range(n):
        center = centers[i]
        deviations = deviations_cache.get(center)
        if deviations is None:
            deviations = sorted(abs(value - center) for value in values)
            deviations_cache[center] = deviations
        own = bisect_left(deviations, abs(values[i] - center))
        mad = _quantile_without(deviations, own, 0.5)
        p10 = _quantile_without(ordered, rank[i], 0.10)
        p90 = _quantile_without(ordered, rank[i], 0.90)
        result.append((center, robust_scale(name, center, mad, p10, p90)))
    return result


def build_reference_distribution(
    window_features: Sequence[Mapping[str, Any]],
    *,
    source: Mapping[str, Any] | None = None,
) -> ReferenceDistribution:
    """作者自己的窗口特征 → 参照分布（稳健中心 / 尺度 + 每窗的留一 |z|）。

    ``window_features`` 每项是 ``kernel_features`` 的结果（持久化窗口的 ``features_json``）；缺的特征按 0。
    少于 3 窗时没有留一，参照窗口的 |z| 对全体分布算（``reliable`` 为 False）。
    """
    rows = [{name: _finite((item or {}).get(name, 0.0)) for name in FEATURE_NAMES} for item in window_features]
    n = len(rows)
    center: dict[str, float] = {}
    scale: dict[str, float] = {}
    p10: dict[str, float] = {}
    p90: dict[str, float] = {}
    loo: list[list[float]] = [[0.0] * len(FEATURE_NAMES) for _ in range(n)]
    for column, name in enumerate(FEATURE_NAMES):
        values = [row[name] for row in rows]
        if values:
            mid = statistics.median(values)
            mad = statistics.median([abs(value - mid) for value in values])
            low, high = quantile(values, 0.10), quantile(values, 0.90)
        else:
            mid = mad = low = high = 0.0
        center[name] = mid
        p10[name] = low
        p90[name] = high
        scale[name] = robust_scale(name, mid, mad, low, high)
        if n >= 3:
            pairs = _loo_center_scale(name, values)
        else:
            pairs = [(mid, scale[name])] * n
        for i, (loo_center, loo_scale) in enumerate(pairs):
            loo[i][column] = min(abs(values[i] - loo_center) / loo_scale, Z_CLIP)
    fingerprint = json.dumps(
        {
            "kernel": KERNEL_VERSION,
            "fidelity": FIDELITY_VERSION,
            "n": n,
            "center": {name: round(center[name], 6) for name in FEATURE_NAMES},
            "scale": {name: round(scale[name], 6) for name in FEATURE_NAMES},
        },
        sort_keys=True,
    )
    return ReferenceDistribution(
        features=FEATURE_NAMES,
        center=center,
        scale=scale,
        p10=p10,
        p90=p90,
        window_count=n,
        loo_abs_z=tuple(tuple(row) for row in loo),
        kernel_version=KERNEL_VERSION,
        reference_version=f"ref_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]}",
        source=dict(source or {}),
    )


# ---------------------------------------------------------------------------
# 读数
# ---------------------------------------------------------------------------


def feature_weights(features: Iterable[str], dimension_states: Mapping[str, Any] | None = None) -> dict[str, float]:
    """每个特征的权重：每个可测维度合计 1（维内特征平分），未映射的特征合成一组「其他」合计 1；
    「重点」维 ×2，「不学」维 0。"""
    names = list(features)
    states = normalize_dimension_states(dimension_states) if dimension_states is not None else {}
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(FEATURE_DIMENSIONS.get(name, OTHER_GROUP), []).append(name)
    weights: dict[str, float] = {}
    for group, members in groups.items():
        state = states.get(group, "normal") if group != OTHER_GROUP else "normal"
        multiplier = 0.0 if state == DIMENSION_EXCLUDE else (2.0 if state == DIMENSION_EMPHASIZE else 1.0)
        for name in members:
            weights[name] = multiplier / len(members)
    return weights


@dataclass(frozen=True)
class FidelityReading:
    """一段文字对作者参照分布的读数（``to_json()`` 即 ``style_fidelity_readings.reading_json``）。"""

    distance: float
    percentile: float
    out_of_band: list[dict[str, Any]]
    dimension_scores: dict[str, float]
    feature_z: dict[str, float]
    char_count: int
    window_count: int
    reliable: bool
    kernel_version: str
    reference_version: str
    fidelity_version: str = FIDELITY_VERSION
    excluded_dimensions: tuple[str, ...] = ()
    emphasized_dimensions: tuple[str, ...] = ()
    # |z| 越界但分母太小、没进越界清单的特征
    low_evidence: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "fidelity_version": self.fidelity_version,
            "kernel_version": self.kernel_version,
            "reference_version": self.reference_version,
            "distance": self.distance,
            "percentile": self.percentile,
            "out_of_band": [dict(item) for item in self.out_of_band],
            "dimension_scores": dict(self.dimension_scores),
            "feature_z": dict(self.feature_z),
            "char_count": self.char_count,
            "window_count": self.window_count,
            "reliable": self.reliable,
            "excluded_dimensions": list(self.excluded_dimensions),
            "emphasized_dimensions": list(self.emphasized_dimensions),
            "low_evidence": list(self.low_evidence),
        }


def feature_phrase(feature: str, direction: str) -> str:
    """特征 + 方向 → 白话短语；表里没有的特征给一个通用说法。"""
    entry = FEATURE_PHRASES.get(feature)
    if entry and direction in entry:
        return entry[direction]
    return f"{feature}{'比作者多' if direction == 'high' else '比作者少'}"


def measure_evidence(measure: TextMeasure) -> dict[str, int]:
    """读数用的证据量（比例类特征的分母）：引语数、带引导的引语数、叙述人称词数、句数、段数。"""
    return {
        "quotes": int(measure.quote_count),
        "guided_quotes": int(sum(measure.speech_verb_counts.values())),
        "person_mentions": int(sum(measure.person_counts.values())),
        "sentences": int(measure.sentence_count),
        "paragraphs": int(measure.paragraph_count),
    }


def _low_evidence(feature: str, evidence: Mapping[str, Any] | None) -> bool:
    if not evidence:
        return False
    for prefix, key, minimum in _EVIDENCE_RULES:
        if feature.startswith(prefix):
            return int(evidence.get(key, minimum) or 0) < minimum
    return False


def reading_from_features(
    features: Mapping[str, Any],
    dist: ReferenceDistribution,
    *,
    dimension_states: Mapping[str, Any] | None = None,
    char_count: int = 0,
    evidence: Mapping[str, Any] | None = None,
) -> FidelityReading:
    """已算好的测量核特征 → 读数（``read_fidelity`` 的内核；同一段文字多次读数时省一次测量）。

    ``evidence``（``measure_evidence`` 的结果）给了时，分母太小的比例类特征不进越界清单（仍计入 distance）。
    """
    weights = feature_weights(dist.features, dimension_states)
    if sum(weights.values()) <= 0:
        weights = feature_weights(dist.features, None)
    total = sum(weights.values())
    states = normalize_dimension_states(dimension_states) if dimension_states is not None else {}
    z_values: dict[str, float] = {}
    distance = 0.0
    for name in dist.features:
        value = _finite(features.get(name, 0.0))
        z = (value - float(dist.center[name])) / float(dist.scale[name])
        z = max(-Z_CLIP, min(Z_CLIP, z))
        z_values[name] = z
        distance += weights[name] * abs(z)
    distance /= total
    references = dist.loo_distances(dimension_states)
    percentile = 100.0 * sum(1 for ref in references if ref <= distance) / len(references) if references else 100.0

    out_of_band: list[dict[str, Any]] = []
    low_evidence: list[str] = []
    for name in dist.features:
        z = z_values[name]
        if abs(z) < OUT_OF_BAND_Z or weights[name] <= 0:
            continue
        if _low_evidence(name, evidence):
            low_evidence.append(name)
            continue
        direction = "high" if z > 0 else "low"
        out_of_band.append(
            {
                "feature": name,
                "dimension": FEATURE_DIMENSIONS.get(name),
                "z": round(z, 2),
                "direction": direction,
                "phrase": feature_phrase(name, direction),
                "value": round(_finite(features.get(name, 0.0)), 4),
                "author_typical": round(float(dist.center[name]), 4),
            }
        )
    out_of_band.sort(key=lambda item: (-abs(item["z"]), item["feature"]))

    dimension_scores: dict[str, float] = {}
    for dim in MEASURABLE_DIMENSIONS:
        if states.get(dim) == DIMENSION_EXCLUDE:
            continue
        members = [name for name in dist.features if FEATURE_DIMENSIONS.get(name) == dim]
        if not members:
            continue
        mean_abs = sum(abs(z_values[name]) for name in members) / len(members)
        dimension_scores[dim] = round(max(0.0, min(10.0, 10.0 - DIMENSION_SCORE_SLOPE * mean_abs)), 1)

    return FidelityReading(
        distance=round(distance, 4),
        percentile=round(percentile, 1),
        out_of_band=out_of_band,
        dimension_scores=dimension_scores,
        feature_z={name: round(z, 3) for name, z in z_values.items()},
        char_count=int(char_count),
        window_count=dist.window_count,
        reliable=bool(dist.reliable and char_count >= MIN_RELIABLE_CHARS),
        kernel_version=dist.kernel_version,
        reference_version=dist.reference_version,
        excluded_dimensions=tuple(dim for dim, state in states.items() if state == DIMENSION_EXCLUDE),
        emphasized_dimensions=tuple(dim for dim, state in states.items() if state == DIMENSION_EMPHASIZE),
        low_evidence=tuple(low_evidence),
    )


def read_fidelity(
    text: str,
    dist: ReferenceDistribution,
    *,
    dimension_states: Mapping[str, Any] | None = None,
) -> FidelityReading:
    """一段文字（生成稿 / 作者稿 HTML）对作者参照分布的读数（测量核按唯一分段规则测）。"""
    measure = measure_text(text)
    return reading_from_features(
        kernel_features(measure),
        dist,
        dimension_states=dimension_states,
        char_count=measure.char_count,
        evidence=measure_evidence(measure),
    )


# 「在作者正常范围内」的百分位上限。临时默认值:真实参考书上随机 30 段原文九成 ≤ 90;
# 上线前用真实模型小规模 A/B 定(契约 §6 第 5 步),风格步与界面都经 ``within_author_range`` 读这一处。
DEFAULT_MAX_PERCENTILE = 90.0


def within_author_range(
    reading: FidelityReading | Mapping[str, Any],
    *,
    max_percentile: float = DEFAULT_MAX_PERCENTILE,
) -> bool:
    """读数是否在作者正常范围内:百分位 ≤ 上限,且「重点」维没有越界特征(接受读数对象或 ``to_json()``)。"""
    payload = reading.to_json() if isinstance(reading, FidelityReading) else dict(reading or {})
    try:
        percentile = float(payload.get("percentile"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(percentile) or percentile > float(max_percentile):
        return False
    emphasized = set(payload.get("emphasized_dimensions") or ())
    return not any(
        isinstance(item, Mapping) and item.get("dimension") in emphasized for item in payload.get("out_of_band") or ()
    )


# ---------------------------------------------------------------------------
# 按书的参照分布（缓存）
# ---------------------------------------------------------------------------

_DIST_CACHE: OrderedDict[tuple[str, str, str, str], ReferenceDistribution] = OrderedDict()
_DIST_CACHE_LOCK = threading.Lock()
_DIST_CACHE_SIZE = 8


def _cache_get(key: tuple[str, str, str, str]) -> ReferenceDistribution | None:
    with _DIST_CACHE_LOCK:
        cached = _DIST_CACHE.get(key)
        if cached is not None:
            _DIST_CACHE.move_to_end(key)
        return cached


def _cache_put(key: tuple[str, str, str, str], dist: ReferenceDistribution) -> None:
    with _DIST_CACHE_LOCK:
        _DIST_CACHE[key] = dist
        while len(_DIST_CACHE) > _DIST_CACHE_SIZE:
            _DIST_CACHE.popitem(last=False)


def clear_reference_cache() -> None:
    with _DIST_CACHE_LOCK:
        _DIST_CACHE.clear()


def reference_distribution_for_book(session: Session, book_id: str) -> ReferenceDistribution | None:
    """这本书的参照分布（窗口索引缺失 / 过期时先建索引）；按 (书, 根哈希, 索引版本, 测量核版本) 缓存。

    书不存在或没有一个窗口 → None。
    """
    book = session.get(StyleReferenceBook, str(book_id))
    if book is None:
        return None
    if marker_is_current(book.stats_json):
        marker = index_marker(book.stats_json) or {}
        cached = _cache_get((str(book_id), str(marker.get("root") or ""), WINDOW_INDEX_VERSION, KERNEL_VERSION))
        if cached is not None:
            return cached
    windows = ensure_window_index(session, book_id)
    if not windows:
        return None
    root = str(windows[0].root_sha256)
    key = (str(book_id), root, WINDOW_INDEX_VERSION, KERNEL_VERSION)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    dist = build_reference_distribution(
        [dict(window.features_json or {}) for window in windows],
        source={"book_id": str(book_id), "root": root, "index_version": WINDOW_INDEX_VERSION},
    )
    _cache_put(key, dist)
    return dist


# ---------------------------------------------------------------------------
# 近期常见偏差
# ---------------------------------------------------------------------------


def _reading_payload(item: Any) -> tuple[Any, Mapping[str, Any]]:
    """(created_at, reading_json)：接受入库行的 dict / ORM 行，或 ``reading_json`` 本身。"""
    if isinstance(item, Mapping):
        payload = item.get("reading_json") if isinstance(item.get("reading_json"), Mapping) else item
        return item.get("created_at"), payload
    payload = getattr(item, "reading_json", None)
    return getattr(item, "created_at", None), payload if isinstance(payload, Mapping) else {}


def recent_gap_entries(
    readings: Sequence[Any],
    *,
    min_hits: int = 3,
    window: int = 5,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """最近 ``window`` 次读数里越界 ≥ ``min_hits`` 次的特征（按次数、再按平均 |z| 排），每条
    ``{feature, direction, dimension, phrase, hits, window}``。``recent_gap_phrases``（首稿补充强调）与界面
    （文风画像按维标「近期常见偏差」）共用这一份挑选。

    ``readings`` 是同一作品的读数（入库行 dict / ORM 行，或 ``reading_json``）：带 ``created_at`` 时按它取最新的
    ``window`` 条，否则认为列表已是新 → 旧。同一特征两个方向分开计数（「比作者多」与「比作者少」不相抵）。
    """
    items = [_reading_payload(item) for item in readings or ()]
    if any(created for created, _payload in items):
        items.sort(key=lambda pair: str(pair[0] or ""), reverse=True)
    recent = [payload for _created, payload in items[: max(0, int(window))]]
    hits: dict[tuple[str, str], list[float]] = {}
    phrases: dict[tuple[str, str], str] = {}
    dimensions: dict[tuple[str, str], str | None] = {}
    for payload in recent:
        seen: set[tuple[str, str]] = set()
        for entry in payload.get("out_of_band") or []:
            if not isinstance(entry, Mapping):
                continue
            feature = str(entry.get("feature") or "")
            direction = str(entry.get("direction") or ("high" if _finite(entry.get("z")) > 0 else "low"))
            key = (feature, direction)
            if not feature or key in seen:
                continue
            seen.add(key)
            hits.setdefault(key, []).append(abs(_finite(entry.get("z"))))
            phrases.setdefault(key, str(entry.get("phrase") or "") or feature_phrase(feature, direction))
            dimensions.setdefault(key, str(entry.get("dimension") or "") or FEATURE_DIMENSIONS.get(feature))
    ranked = sorted(
        (key for key, values in hits.items() if len(values) >= int(min_hits)),
        key=lambda key: (-len(hits[key]), -sum(hits[key]) / len(hits[key]), key),
    )
    result = [
        {
            "feature": key[0],
            "direction": key[1],
            "dimension": dimensions.get(key),
            "phrase": phrases[key],
            "hits": len(hits[key]),
            "window": len(recent),
        }
        for key in ranked
    ]
    return result[:limit] if limit is not None else result


def recent_gap_phrases(
    readings: Sequence[Any],
    *,
    min_hits: int = 3,
    window: int = 5,
    limit: int | None = None,
) -> list[str]:
    """最近 ``window`` 次读数里越界 ≥ ``min_hits`` 次的特征 → 白话短语（同一份挑选见 ``recent_gap_entries``）。"""
    return [entry["phrase"] for entry in recent_gap_entries(readings, min_hits=min_hits, window=window, limit=limit)]


__all__ = [
    "DEFAULT_MAX_PERCENTILE",
    "DIMENSION_FEATURES",
    "DIMENSION_SCORE_SLOPE",
    "FEATURE_DIMENSIONS",
    "FEATURE_PHRASES",
    "FIDELITY_VERSION",
    "FidelityReading",
    "MEASURABLE_DIMENSIONS",
    "MIN_REFERENCE_WINDOWS",
    "MIN_RELIABLE_CHARS",
    "OUT_OF_BAND_Z",
    "ReferenceDistribution",
    "Z_CLIP",
    "build_reference_distribution",
    "clear_reference_cache",
    "feature_phrase",
    "feature_weights",
    "measure_evidence",
    "read_fidelity",
    "reading_from_features",
    "recent_gap_entries",
    "recent_gap_phrases",
    "reference_distribution_for_book",
    "within_author_range",
]
