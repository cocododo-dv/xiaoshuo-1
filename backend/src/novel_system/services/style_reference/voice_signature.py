"""Style Reference v2 确定性「声音签名」(W3)。

依据 docs/style-imitation-v2-plan-2026-09-05.md §1.1 / §2.W3。

纯函数、无新依赖、只用闭类词与标点——不含任何名词 / 动词等实词,天然内容安全,
产物可以直接渲染进生成提示而不泄露原文,也不会触碰反抄袭红线。

对外契约(W1 落库、W4 注入、W6 漂移共用):

- ``compute_voice_signature(texts)`` → ``{"version", "features", "top_words",
  "deliberate_repetition", "stats"}``;``features`` 全部为有限 float,键名见
  ``FEATURE_NAMES``(稳定 snake_case);空文本返回全 0 且不抛异常。
- ``compute_voice_signature_for_text(text)``:单文本版本(生成侧 / 漂移复用)。
- ``render_voice_habits(features_or_signature, baseline)``:≤12 行中文习惯句,
  只说方向与具体词,不含阿拉伯数字。
- ``load_voice_baseline()``:读 ``config/style_reference/voice_baseline.yaml``
  (缺文件时返回 ``{}``,所有依赖基线的判断优雅退化)。
- ``distinctive_features`` / ``feature_z_scores``:供 W4 选段与 W6 漂移复用。

基线由本模块的 ``__main__`` 子命令 ``build-baseline`` 用
``backend/tests/golden/style_reference/corpus`` 全部文本按 1500 字块生成。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from novel_system.services.style_reference.config_loader import (
    load_optional_yaml_config,
    load_yaml_config,
)
from novel_system.services.style_reference.text_utils import (
    normalize_text,
    split_paragraphs,
    split_sentences,
)

VOICE_SIGNATURE_VERSION = "voice_signature_v1"
VOICE_BASELINE_VERSION = "voice_baseline_v1"

# 基线块尺寸(字符)。与 metrics._VARIANCE_CHUNK_CHARS 一致,≈ 一个场景的长度,
# 因此块间 std 就是 W6 漂移读数所需的「场景级自然波动」尺度。
BASELINE_BLOCK_CHARS = 1500
# 字级 TTR / 二元组 hapax 的窗口。规格写 2k;这里与基线块同宽,否则块级基线
# (1500 字)与整书读数(2000 字窗)会因 TTR 随长度衰减而系统性偏移。
LEXICAL_WINDOW_CHARS = 1500
LEXICAL_MAX_WINDOWS = 256
SHORT_SENTENCE_CHARS = 8
TOP_WORDS_PER_GROUP = 5
MAX_HABIT_LINES = 12
# 基线是块级(1500 字)分布;整书签名是 n 块的聚合均值,块间 std 对它过宽——
# z 值与方向类习惯句按 1/sqrt(min(n, 16)) 收窄 std 与 p15/p85 带(n=1 即字面
# p15/p85,场景级读数不变)。例外:REPETITION_FEATURES(叠词 / 短句连打)始终按字面
# p85 判——规格 §2.W3 明文 deliberate_repetition「≥ p85」,旗标与「叠词多 / 短句连打」
# 习惯句必须同口径;否则整书画像几乎都会被标成刻意重复,放松下游的新鲜度守卫。
Z_MAX_AGGREGATION_BLOCKS = 16
REPETITION_FEATURES: tuple[str, ...] = ("redup_total_per_1k", "sent_short_run_ratio")
# 少于这些可见字符的文本不渲染习惯句(统计无意义)。
MIN_RENDER_CHARS = 200
_Z_CLIP = 8.0

FUNCTION_WORD_GROUPS: tuple[str, ...] = (
    "particle",
    "aspect",
    "connective",
    "adverb",
    "preposition",
    "pronoun",
    "modal",
    "classical",
)
SPEECH_VERB_KEYS: tuple[str, ...] = ("shuodao", "shuo", "dao", "wen", "da", "other")
_SPEECH_VERB_LABELS = {
    "shuodao": "说道",
    "shuo": "说",
    "dao": "道",
    "wen": "问",
    "da": "答",
    "other": "其他",
}

FEATURE_NAMES: tuple[str, ...] = (
    *tuple(f"fw_{group}_per_1k" for group in FUNCTION_WORD_GROUPS),
    "fw_total_per_1k",
    "sentence_final_modal_ratio",
    "sentence_final_classical_ratio",
    "punct_comma_per_1k",
    "punct_enumeration_per_1k",
    "punct_period_per_1k",
    "punct_colon_per_1k",
    "punct_semicolon_per_1k",
    "punct_exclamation_per_1k",
    "punct_question_per_1k",
    "punct_ellipsis_per_1k",
    "punct_dash_per_1k",
    "punct_quote_pair_per_1k",
    "sent_len_mean",
    "sent_len_std",
    "sent_len_p10",
    "sent_len_p90",
    "sent_pauses_mean",
    "clause_len_mean",
    "sent_short_run_mean",
    "sent_short_run_ratio",
    "sent_len_lag1_autocorr",
    "para_len_mean",
    "para_single_sentence_ratio",
    "para_dialogue_ratio",
    "dialogue_guide_pre_share",
    "dialogue_guide_post_share",
    "dialogue_guide_none_share",
    *tuple(f"speech_verb_{key}_share" for key in SPEECH_VERB_KEYS),
    "four_char_segment_per_1k",
    "redup_aa_per_1k",
    "redup_aabb_per_1k",
    "redup_abab_per_1k",
    "redup_total_per_1k",
    "person_first_share",
    "person_second_share",
    "person_third_share",
    "lexical_char_ttr",
    "lexical_bigram_hapax_ratio",
)

# top_words 的组:8 个虚词组 + 句末助词 + 引导动词。
TOP_WORD_GROUPS: tuple[str, ...] = (*FUNCTION_WORD_GROUPS, "sentence_final", "speech_verb")

_CJK = "㐀-鿿"
_NON_VISIBLE_RE = re.compile(rf"[^A-Za-z0-9{_CJK}]+")
_NON_CJK_RE = re.compile(rf"[^{_CJK}]+")
_FOUR_CHAR_RE = re.compile(rf"(?<![{_CJK}])[{_CJK}]{{4}}(?![{_CJK}])")
_AA_RE = re.compile(rf"([{_CJK}])\1")
_AABB_RE = re.compile(rf"([{_CJK}])\1([{_CJK}])\2")
_ABAB_RE = re.compile(rf"([{_CJK}])([{_CJK}])\1\2")
_ELLIPSIS_RE = re.compile(r"……|…|\.{3,}")
_DASH_RE = re.compile(r"——|—")
_QUOTE_SPAN_RE = re.compile(
    r"“([^“”\n]{1,600})”|‘([^‘’\n]{1,600})’|「([^「」\n]{1,600})」|『([^『』\n]{1,600})』|\"([^\"\n]{1,600})\""
)
_OPENING_QUOTES = ("“", "‘", "「", "『", '"')
_PAUSE_CHARS = "，、；：,;:"
_TRAILING_STRIP = "”’」』\"'）)】〕］ \t　。！？.!?…；;"
_PRE_WINDOW_CUT = "。！？；!?;…”’」』\n"
_POST_WINDOW_CUT = "。！？；!?;…“‘「『\"\n"
_GUIDE_WINDOW_CHARS = 14


# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceLexicon:
    """从 function_words.yaml 编译出的闭类词表(缓存,进程内只读)。"""

    group_words: Mapping[str, tuple[str, ...]]
    labels: Mapping[str, str]
    words_by_length_desc: tuple[str, ...]
    containers: Mapping[str, tuple[str, ...]]
    sentence_final_modal: frozenset[str]
    sentence_final_classical: frozenset[str]
    person: Mapping[str, tuple[str, ...]]
    speech_main: Mapping[str, tuple[str, ...]]
    speech_other: tuple[str, ...]
    speech_exclusions: tuple[str, ...]
    speech_verb_regex: re.Pattern[str]


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _build_lexicon(raw: Mapping[str, Any]) -> VoiceLexicon:
    groups_raw = raw.get("groups") if isinstance(raw.get("groups"), Mapping) else {}
    group_words: dict[str, tuple[str, ...]] = {}
    labels: dict[str, str] = {}
    seen: set[str] = set()
    for group in FUNCTION_WORD_GROUPS:
        entry = groups_raw.get(group) if isinstance(groups_raw, Mapping) else None
        words: list[str] = []
        label = group
        if isinstance(entry, Mapping):
            label = str(entry.get("label") or group)
            candidates = _as_str_list(entry.get("words"))
        else:
            candidates = _as_str_list(entry)
        for word in candidates:
            if word in seen:
                continue  # 一词只归一组:先出现的组胜出
            seen.add(word)
            words.append(word)
        group_words[group] = tuple(words)
        labels[group] = label

    all_words = sorted(seen, key=lambda w: (-len(w), w))
    containers: dict[str, tuple[str, ...]] = {}
    for word in all_words:
        containers[word] = tuple(
            longer for longer in all_words if len(longer) > len(word) and word in longer
        )

    sentence_final = raw.get("sentence_final") if isinstance(raw.get("sentence_final"), Mapping) else {}
    person_raw = raw.get("person") if isinstance(raw.get("person"), Mapping) else {}
    person = {
        key: tuple(word for word in _as_str_list(person_raw.get(key)) if word in seen)
        for key in ("first", "second", "third")
    }

    speech_raw = raw.get("speech_verbs") if isinstance(raw.get("speech_verbs"), Mapping) else {}
    main_raw = speech_raw.get("main") if isinstance(speech_raw.get("main"), Mapping) else {}
    speech_main = {
        key: tuple(_as_str_list(main_raw.get(key)))
        for key in SPEECH_VERB_KEYS
        if key != "other"
    }
    speech_other = tuple(_as_str_list(speech_raw.get("other")))
    speech_exclusions = tuple(_as_str_list(speech_raw.get("exclusions")))
    all_verbs = sorted(
        {verb for verbs in speech_main.values() for verb in verbs} | set(speech_other),
        key=lambda v: (-len(v), v),
    )
    verb_pattern = "|".join(re.escape(verb) for verb in all_verbs) or r"(?!x)x"
    return VoiceLexicon(
        group_words=group_words,
        labels=labels,
        words_by_length_desc=tuple(all_words),
        containers=containers,
        sentence_final_modal=frozenset(_as_str_list(sentence_final.get("modal"))),
        sentence_final_classical=frozenset(_as_str_list(sentence_final.get("classical"))),
        person=person,
        speech_main=speech_main,
        speech_other=speech_other,
        speech_exclusions=speech_exclusions,
        speech_verb_regex=re.compile(verb_pattern),
    )


@lru_cache(maxsize=1)
def _lexicon() -> VoiceLexicon:
    return _build_lexicon(load_yaml_config("function_words"))


def load_voice_lexicon() -> VoiceLexicon:
    """返回编译后的闭类词表(缓存)。"""
    return _lexicon()


def clear_voice_signature_cache() -> None:
    """清空词表缓存,供测试用(配合 config_loader.clear_config_cache)。"""
    _lexicon.cache_clear()


def load_voice_baseline() -> dict[str, Any]:
    """读 ``config/style_reference/voice_baseline.yaml``;缺文件或结构不合法时返回 ``{}``。"""
    raw = load_optional_yaml_config("voice_baseline")
    features = raw.get("features") if isinstance(raw, Mapping) else None
    if not isinstance(features, Mapping) or not features:
        return {}
    return dict(raw)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _finite(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _round(value: float) -> float:
    return round(_finite(value), 6)


def _visible_length(text: str) -> int:
    return len(_NON_VISIBLE_RE.sub("", text))


def _quantile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _per_1k(count: float, chars: int) -> float:
    if chars <= 0:
        return 0.0
    return count * 1000.0 / chars


def _share(count: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return count / total


def _visible_sentences(paragraph: str) -> list[str]:
    """复用 text_utils.split_sentences,并丢弃无可见字符的残片(闭引号 / 纯标点)。"""
    return [part for part in split_sentences(paragraph) if _NON_VISIBLE_RE.sub("", part)]


def _exclusive_counts(text: str, lexicon: VoiceLexicon) -> dict[str, int]:
    """词表独占计数:长词命中的位置不再记入其子串短词。

    单遍 ``str.count``(C 速度),再按「长词优先」扣除嵌套命中;结果 ≥ 0。
    """
    raw = {word: text.count(word) for word in lexicon.words_by_length_desc}
    exclusive: dict[str, int] = {}
    for word in lexicon.words_by_length_desc:  # 长 → 短
        count = raw[word]
        for longer in lexicon.containers[word]:
            count -= exclusive[longer] * longer.count(word)
        exclusive[word] = max(0, count)
    return exclusive


def _top_words(counter: Mapping[str, int], limit: int = TOP_WORDS_PER_GROUP) -> list[list[Any]]:
    total = sum(count for count in counter.values() if count > 0)
    if total <= 0:
        return []
    ranked = sorted(
        ((word, count) for word, count in counter.items() if count > 0),
        key=lambda item: (-item[1], item[0]),
    )
    return [[word, _round(count / total)] for word, count in ranked[:limit]]


# ---------------------------------------------------------------------------
# 对白引导
# ---------------------------------------------------------------------------


def _classify_speech_verb(window: str, lexicon: VoiceLexicon) -> str | None:
    clause = window
    for excluded in lexicon.speech_exclusions:
        if excluded in clause:
            clause = clause.replace(excluded, "")
    for key in ("shuodao", "dao", "shuo", "wen", "da"):
        if any(verb in clause for verb in lexicon.speech_main.get(key, ())):
            return key
    if lexicon.speech_verb_regex.search(clause):
        return "other"
    return None


def _cut_pre_window(paragraph: str, start: int) -> str:
    window = paragraph[max(0, start - _GUIDE_WINDOW_CHARS) : start]
    cut = max((window.rfind(char) for char in _PRE_WINDOW_CUT), default=-1)
    if cut >= 0:
        window = window[cut + 1 :]
    return window.strip()


def _cut_post_window(paragraph: str, end: int) -> str:
    window = paragraph[end : end + _GUIDE_WINDOW_CHARS]
    positions = [window.find(char) for char in _POST_WINDOW_CUT]
    cuts = [position for position in positions if position >= 0]
    if cuts:
        window = window[: min(cuts)]
    return window.strip()


def _analyze_dialogue(
    paragraph: str, lexicon: VoiceLexicon
) -> tuple[list[tuple[int, int]], Counter[str], Counter[str]]:
    """返回 (引号跨度, 引导位置计数 pre/post/none, 引导动词计数)。"""
    spans: list[tuple[int, int]] = []
    guides: Counter[str] = Counter()
    verbs: Counter[str] = Counter()
    for match in _QUOTE_SPAN_RE.finditer(paragraph):
        start, end = match.span()
        spans.append((start, end))
        pre = _cut_pre_window(paragraph, start)
        verb: str | None = None
        placement = "none"
        if pre.endswith(("：", ":")):
            placement = "pre"
            verb = _classify_speech_verb(pre, lexicon) or "other"
        elif pre.endswith(("，", ",")):
            verb = _classify_speech_verb(pre, lexicon)
            if verb is not None:
                placement = "pre"
        if placement == "none":
            post = _cut_post_window(paragraph, end)
            verb = _classify_speech_verb(post, lexicon) if post else None
            if verb is not None:
                placement = "post"
        guides[placement] += 1
        if placement != "none" and verb is not None:
            verbs[verb] += 1
    return spans, guides, verbs


def _looks_like_dialogue_paragraph(paragraph: str, spans: Sequence[tuple[int, int]], visible: int) -> bool:
    if paragraph.lstrip().startswith(_OPENING_QUOTES):
        return True
    if not spans or visible <= 0:
        return False
    quoted = sum(_visible_length(paragraph[start:end]) for start, end in spans)
    return quoted * 2 >= visible


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------


def _empty_signature() -> dict[str, Any]:
    return {
        "version": VOICE_SIGNATURE_VERSION,
        "features": {name: 0.0 for name in FEATURE_NAMES},
        "top_words": {group: [] for group in TOP_WORD_GROUPS},
        "deliberate_repetition": False,
        "stats": {"char_count": 0, "sentence_count": 0, "paragraph_count": 0, "quote_count": 0},
    }


def _lexical_windows(cjk_text: str) -> list[str]:
    length = len(cjk_text)
    if length == 0:
        return []
    if length <= LEXICAL_WINDOW_CHARS:
        return [cjk_text]
    total = length // LEXICAL_WINDOW_CHARS
    if total <= LEXICAL_MAX_WINDOWS:
        indices: Iterable[int] = range(total)
    else:
        step = total / LEXICAL_MAX_WINDOWS
        indices = sorted({int(index * step) for index in range(LEXICAL_MAX_WINDOWS)})
    return [
        cjk_text[index * LEXICAL_WINDOW_CHARS : (index + 1) * LEXICAL_WINDOW_CHARS]
        for index in indices
    ]


def _lexical_features(cjk_text: str) -> tuple[float, float]:
    windows = _lexical_windows(cjk_text)
    if not windows:
        return 0.0, 0.0
    ttr_values: list[float] = []
    hapax_values: list[float] = []
    for window in windows:
        ttr_values.append(len(set(window)) / len(window))
        if len(window) >= 2:
            bigrams = Counter(zip(window, window[1:]))
            hapax_values.append(sum(1 for count in bigrams.values() if count == 1) / len(bigrams))
    return (
        statistics.fmean(ttr_values),
        statistics.fmean(hapax_values) if hapax_values else 0.0,
    )


def _sentence_sequence_features(lengths: Sequence[int]) -> dict[str, float]:
    count = len(lengths)
    if count == 0:
        return {
            "sent_len_mean": 0.0,
            "sent_len_std": 0.0,
            "sent_len_p10": 0.0,
            "sent_len_p90": 0.0,
            "sent_short_run_mean": 0.0,
            "sent_short_run_ratio": 0.0,
            "sent_len_lag1_autocorr": 0.0,
        }
    mean = statistics.fmean(lengths)
    std = statistics.pstdev(lengths) if count > 1 else 0.0
    runs: list[int] = []
    run = 0
    for length in lengths:
        if length <= SHORT_SENTENCE_CHARS:
            run += 1
            continue
        if run >= 2:
            runs.append(run)
        run = 0
    if run >= 2:
        runs.append(run)
    autocorr = 0.0
    if count >= 3 and std > 0:
        variance = sum((length - mean) ** 2 for length in lengths)
        covariance = sum(
            (lengths[index] - mean) * (lengths[index + 1] - mean) for index in range(count - 1)
        )
        autocorr = covariance / variance if variance > 0 else 0.0
    return {
        "sent_len_mean": mean,
        "sent_len_std": std,
        "sent_len_p10": _quantile(lengths, 0.10),
        "sent_len_p90": _quantile(lengths, 0.90),
        "sent_short_run_mean": statistics.fmean(runs) if runs else 0.0,
        "sent_short_run_ratio": sum(runs) / count,
        "sent_len_lag1_autocorr": autocorr,
    }


def compute_voice_signature(
    texts: list[str],
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """计算一组段落(通常是一部书的全部段落)的声音签名。

    ``texts`` 每项视为一个段落;``baseline`` 只用于 ``deliberate_repetition``
    的 p85 判定,缺省读 ``voice_baseline.yaml``(缺文件时判 False)。
    单遍扫描,复杂度 O(总字数 × 词表规模常数)。
    """
    lexicon = _lexicon()
    paragraphs = [str(text).strip() for text in (texts or []) if str(text or "").strip()]
    if not paragraphs:
        return _empty_signature()
    joined = "\n".join(paragraphs)
    char_count = _visible_length(joined)
    if char_count == 0:
        return _empty_signature()

    # --- 虚词(独占计数) --------------------------------------------------
    exclusive = _exclusive_counts(joined, lexicon)
    features: dict[str, float] = {}
    top_words: dict[str, list[list[Any]]] = {}
    fw_total = 0
    for group in FUNCTION_WORD_GROUPS:
        counts = {word: exclusive.get(word, 0) for word in lexicon.group_words[group]}
        group_total = sum(counts.values())
        fw_total += group_total
        features[f"fw_{group}_per_1k"] = _per_1k(group_total, char_count)
        top_words[group] = _top_words(counts)
    features["fw_total_per_1k"] = _per_1k(fw_total, char_count)

    # --- 人称 -------------------------------------------------------------
    person_counts = {
        key: sum(exclusive.get(word, 0) for word in words) for key, words in lexicon.person.items()
    }
    person_total = sum(person_counts.values())
    for key in ("first", "second", "third"):
        features[f"person_{key}_share"] = _share(person_counts.get(key, 0), person_total)

    # --- 标点 -------------------------------------------------------------
    punct_counts = {
        "comma": sum(joined.count(char) for char in "，,"),
        "enumeration": joined.count("、"),
        "period": joined.count("。"),
        "colon": sum(joined.count(char) for char in "：:"),
        "semicolon": sum(joined.count(char) for char in "；;"),
        "exclamation": sum(joined.count(char) for char in "！!"),
        "question": sum(joined.count(char) for char in "？?"),
        "ellipsis": len(_ELLIPSIS_RE.findall(joined)),
        "dash": len(_DASH_RE.findall(joined)),
        "quote_pair": sum(joined.count(char) for char in "“‘「『") + joined.count('"') // 2,
    }
    for name, count in punct_counts.items():
        features[f"punct_{name}_per_1k"] = _per_1k(count, char_count)
    pause_total = sum(joined.count(char) for char in _PAUSE_CHARS)

    # --- 四字格 / 叠词 -----------------------------------------------------
    four_char = len(_FOUR_CHAR_RE.findall(joined))
    aabb = sum(1 for match in _AABB_RE.finditer(joined) if match.group(1) != match.group(2))
    abab = sum(1 for match in _ABAB_RE.finditer(joined) if match.group(1) != match.group(2))
    aa = max(0, len(_AA_RE.findall(joined)) - 2 * aabb)
    features["four_char_segment_per_1k"] = _per_1k(four_char, char_count)
    features["redup_aa_per_1k"] = _per_1k(aa, char_count)
    features["redup_aabb_per_1k"] = _per_1k(aabb, char_count)
    features["redup_abab_per_1k"] = _per_1k(abab, char_count)
    features["redup_total_per_1k"] = _per_1k(aa + aabb + abab, char_count)

    # --- 句 / 段 / 对白(逐段单遍) -------------------------------------------
    sentence_lengths: list[int] = []
    final_modal = 0
    final_classical = 0
    final_counter: Counter[str] = Counter()
    paragraph_lengths: list[int] = []
    single_sentence_paragraphs = 0
    dialogue_paragraphs = 0
    guide_counter: Counter[str] = Counter()
    verb_counter: Counter[str] = Counter()
    quote_count = 0
    for paragraph in paragraphs:
        visible = _visible_length(paragraph)
        if visible == 0:
            continue
        paragraph_lengths.append(visible)
        sentences = _visible_sentences(paragraph)
        if len(sentences) <= 1:
            single_sentence_paragraphs += 1
        for sentence in sentences:
            sentence_lengths.append(_visible_length(sentence))
            tail = sentence.rstrip(_TRAILING_STRIP)
            last = tail[-1:] if tail else ""
            if last in lexicon.sentence_final_modal:
                final_modal += 1
                final_counter[last] += 1
            elif last in lexicon.sentence_final_classical:
                final_classical += 1
                final_counter[last] += 1
        spans, guides, verbs = _analyze_dialogue(paragraph, lexicon)
        quote_count += len(spans)
        guide_counter.update(guides)
        verb_counter.update(verbs)
        if _looks_like_dialogue_paragraph(paragraph, spans, visible):
            dialogue_paragraphs += 1

    sentence_count = len(sentence_lengths)
    features.update(_sentence_sequence_features(sentence_lengths))
    features["sent_pauses_mean"] = _share(pause_total, sentence_count)
    features["clause_len_mean"] = _share(char_count, sentence_count + pause_total)
    features["sentence_final_modal_ratio"] = _share(final_modal, sentence_count)
    features["sentence_final_classical_ratio"] = _share(final_classical, sentence_count)
    top_words["sentence_final"] = _top_words(final_counter)

    paragraph_count = len(paragraph_lengths)
    features["para_len_mean"] = statistics.fmean(paragraph_lengths) if paragraph_lengths else 0.0
    features["para_single_sentence_ratio"] = _share(single_sentence_paragraphs, paragraph_count)
    features["para_dialogue_ratio"] = _share(dialogue_paragraphs, paragraph_count)

    for placement in ("pre", "post", "none"):
        features[f"dialogue_guide_{placement}_share"] = _share(guide_counter[placement], quote_count)
    verb_total = sum(verb_counter.values())
    for key in SPEECH_VERB_KEYS:
        features[f"speech_verb_{key}_share"] = _share(verb_counter[key], verb_total)
    top_words["speech_verb"] = _top_words(
        {_SPEECH_VERB_LABELS[key]: verb_counter[key] for key in SPEECH_VERB_KEYS}
    )

    # --- 词汇 -------------------------------------------------------------
    ttr, hapax = _lexical_features(_NON_CJK_RE.sub("", joined))
    features["lexical_char_ttr"] = ttr
    features["lexical_bigram_hapax_ratio"] = hapax

    ordered = {name: _round(features.get(name, 0.0)) for name in FEATURE_NAMES}
    if baseline is None:
        baseline = load_voice_baseline()
    stats = {
        "char_count": char_count,
        "sentence_count": sentence_count,
        "paragraph_count": paragraph_count,
        "quote_count": quote_count,
    }
    return {
        "version": VOICE_SIGNATURE_VERSION,
        "features": ordered,
        "top_words": {group: top_words.get(group, []) for group in TOP_WORD_GROUPS},
        "deliberate_repetition": _deliberate_repetition(ordered, baseline),
        "stats": stats,
    }


def compute_voice_signature_for_text(
    text: str,
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单文本版本:按空行(退化时按单换行)切段后计算,供生成侧 / 漂移读数复用。"""
    normalized = normalize_text(str(text or ""))
    if not normalized:
        return _empty_signature()
    paragraphs = [body for _start, _end, body in split_paragraphs(normalized)] or [normalized]
    return compute_voice_signature(paragraphs, baseline=baseline)


def _baseline_stat(baseline: Mapping[str, Any] | None, feature: str, key: str) -> float | None:
    if not baseline:
        return None
    features = baseline.get("features") if isinstance(baseline, Mapping) else None
    entry = features.get(feature) if isinstance(features, Mapping) else None
    if not isinstance(entry, Mapping) or key not in entry:
        return None
    try:
        value = float(entry[key])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _block_count_of(features_or_signature: Mapping[str, Any] | None) -> int:
    """签名覆盖的基线块数(由 stats.char_count 推出);仅 features 时视为 1 块。"""
    if not isinstance(features_or_signature, Mapping):
        return 1
    stats = features_or_signature.get("stats")
    if not isinstance(stats, Mapping):
        return 1
    try:
        char_count = float(stats.get("char_count", 0))
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(char_count) or char_count <= 0:
        return 1
    return max(1, int(round(char_count / BASELINE_BLOCK_CHARS)))


def _aggregation_scale(block_count: int | None) -> float:
    count = 1 if block_count is None else max(1, int(block_count))
    return math.sqrt(min(count, Z_MAX_AGGREGATION_BLOCKS))


def _deliberate_repetition(
    features: Mapping[str, float],
    baseline: Mapping[str, Any] | None,
) -> bool:
    """叠词密度或短句连打高于基线**字面** p85 → True;无基线时 False(fail-closed)。

    规格 §2.W3:「显著高于基线(≥p85)」。这里不套 1/sqrt(n) 聚合收窄——那只属于
    z 值 / 漂移读数;整书签名与场景级读数都对照块级 p85 本身判定,与
    :func:`render_voice_habits` 里「叠词多 / 短句连打」两句同口径。
    """
    return any(_level(features, baseline, name) == "high" for name in REPETITION_FEATURES)


# ---------------------------------------------------------------------------
# 基线对照
# ---------------------------------------------------------------------------


def _unpack(features_or_signature: Mapping[str, Any] | None) -> tuple[dict[str, float], dict[str, list[list[Any]]]]:
    """接受整份签名或仅 features;返回 (features, top_words)。"""
    if not isinstance(features_or_signature, Mapping):
        return {}, {}
    inner = features_or_signature.get("features")
    if isinstance(inner, Mapping):
        top_words = features_or_signature.get("top_words")
        return (
            {str(k): _finite(v) for k, v in inner.items()},
            dict(top_words) if isinstance(top_words, Mapping) else {},
        )
    return {str(k): _finite(v) for k, v in features_or_signature.items() if isinstance(v, (int, float))}, {}


def feature_z_scores(
    features: Mapping[str, Any],
    baseline_features: Mapping[str, Any],
    baseline_std: Mapping[str, Any] | None = None,
    *,
    block_count: int | None = None,
) -> dict[str, float]:
    """逐特征 z 值。

    ``baseline_features`` 的值可以是均值数字,也可以是 ``{"mean", "std", ...}``
    映射(voice_baseline.yaml 的形态);``baseline_std`` 显式给出时覆盖 std。
    std 有下限(均值的 5% 或 1e-6)避免除零;结果裁到 ±8 且恒有限。
    缺失的特征(任一侧)跳过。

    基线 std 是块级(1500 字)波动。``features`` 传整份签名时按 ``stats.char_count``
    推出它聚合的块数 n,std 按 1/sqrt(min(n, 16)) 收窄;显式 ``block_count`` 覆盖
    (场景级漂移读数传 1 即得字面块级 z)。仅传 features 时 n=1。
    """
    values, _ = _unpack(features)
    result: dict[str, float] = {}
    if not isinstance(baseline_features, Mapping):
        return result
    scale = _aggregation_scale(block_count if block_count is not None else _block_count_of(features))
    for name, value in values.items():
        entry = baseline_features.get(name)
        if entry is None:
            continue
        if isinstance(entry, Mapping):
            mean = _finite(entry.get("mean", 0.0))
            std = _finite(entry.get("std", 0.0))
        else:
            mean = _finite(entry)
            std = 0.0
        if baseline_std is not None and name in baseline_std:
            std = _finite(baseline_std.get(name))
        floor = max(1e-6, 0.05 * abs(mean))
        effective_std = max(std, floor) / scale
        z = (value - mean) / effective_std
        result[name] = _round(max(-_Z_CLIP, min(_Z_CLIP, z)))
    return result


def distinctive_features(
    features: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
    *,
    min_abs_z: float = 1.0,
    block_count: int | None = None,
) -> list[dict[str, Any]]:
    """相对基线偏离显著(|z| ≥ min_abs_z)的特征,按 |z| 降序。

    返回 ``[{"feature", "z", "direction": "high"|"low"}]``;无基线时返回空表。
    ``block_count`` 语义同 :func:`feature_z_scores`。
    """
    if baseline is None:
        baseline = load_voice_baseline()
    baseline_features = baseline.get("features") if isinstance(baseline, Mapping) else None
    if not isinstance(baseline_features, Mapping):
        return []
    scores = feature_z_scores(features, baseline_features, block_count=block_count)
    selected = [
        {"feature": name, "z": z, "direction": "high" if z > 0 else "low"}
        for name, z in scores.items()
        if abs(z) >= min_abs_z
    ]
    selected.sort(key=lambda item: (-abs(item["z"]), item["feature"]))
    return selected


def _level(
    features: Mapping[str, float],
    baseline: Mapping[str, Any] | None,
    name: str,
    *,
    scale: float = 1.0,
) -> str | None:
    """对照 p15 / p85 判「偏低 / 偏高」;无基线或落在中段返回 None。

    ``scale`` > 1 时(整书聚合签名)把 p15 / p85 带按 1/scale 向 p50 收窄,
    与 :func:`feature_z_scores` 的 std 收窄同一口径;scale=1 即字面 p15 / p85。
    """
    if name not in features:
        return None
    p15 = _baseline_stat(baseline, name, "p15")
    p50 = _baseline_stat(baseline, name, "p50")
    p85 = _baseline_stat(baseline, name, "p85")
    if p15 is None or p85 is None or p85 <= p15:
        return None
    if p50 is None or not (p15 <= p50 <= p85):
        p50 = (p15 + p85) / 2.0
    factor = max(1.0, float(scale))
    high_bound = p50 + (p85 - p50) / factor
    low_bound = p50 - (p50 - p15) / factor
    value = features[name]
    if value > high_bound and high_bound > low_bound:
        return "high"
    if value < low_bound and high_bound > low_bound:
        return "low"
    return None


# ---------------------------------------------------------------------------
# 习惯句渲染
# ---------------------------------------------------------------------------


def _join_words(words: Sequence[str]) -> str:
    return "、".join(words)


def _author_words(top_words: Mapping[str, Any], group: str, *, limit: int = 3, min_share: float = 0.06) -> list[str]:
    entries = top_words.get(group) if isinstance(top_words, Mapping) else None
    result: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        word, share = str(entry[0]), _finite(entry[1])
        if word and share >= min_share:
            result.append(word)
        if len(result) >= limit:
            break
    return result


def _underused_words(
    top_words: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    group: str,
    *,
    limit: int = 2,
) -> list[str]:
    """基线里显著、作者却少用的词:基线 top 词中作者份额不足其一半者。

    作者侧只知道 top-N 词的份额;不在表内的词,其份额必 ≤ 表内最小份额,
    因此只有当「表内最小份额 < 基线份额的一半」时才能可靠断言少用。
    """
    if not baseline:
        return []
    baseline_top = baseline.get("top_words") if isinstance(baseline, Mapping) else None
    entries = baseline_top.get(group) if isinstance(baseline_top, Mapping) else None
    if not entries:
        return []
    author_shares: dict[str, float] = {}
    for entry in top_words.get(group) or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            author_shares[str(entry[0])] = _finite(entry[1])
    if not author_shares:
        return []
    author_floor = min(author_shares.values())
    author_top = set(_author_words(top_words, group))
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        word, share = str(entry[0]), _finite(entry[1])
        if word in author_top or share < 0.04:
            continue
        author_share = author_shares.get(word, author_floor)
        if author_share < share * 0.5:
            result.append(word)
        if len(result) >= limit:
            break
    return result


def render_voice_habits(
    features: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
) -> list[str]:
    """把声音签名渲染成 ≤12 行生成器可执行的中文习惯句。

    ``features`` 可以是整份签名(利用 top_words 给出具体词)或仅 features。
    对照 ``baseline`` 的 p15 / p85 判方向;无基线时只输出不需要方向的行。
    输出不含阿拉伯数字。
    """
    values, top_words = _unpack(features)
    if not values or not any(value != 0.0 for value in values.values()):
        return []
    stats = features.get("stats") if isinstance(features, Mapping) else None
    if isinstance(stats, Mapping):
        try:
            if float(stats.get("char_count", MIN_RENDER_CHARS)) < MIN_RENDER_CHARS:
                return []
        except (TypeError, ValueError):
            pass
    if baseline is None:
        baseline = load_voice_baseline()
    baseline_features = baseline.get("features") if isinstance(baseline, Mapping) else {}
    block_count = _block_count_of(features)
    scale = _aggregation_scale(block_count)
    scores = feature_z_scores(values, baseline_features or {}, block_count=block_count)
    lexicon = _lexicon()
    labels = lexicon.labels

    def level(name: str) -> str | None:
        # 重复族特征与 deliberate_repetition 同口径:字面 p15 / p85,不随块数收窄。
        return _level(values, baseline, name, scale=1.0 if name in REPETITION_FEATURES else scale)

    def weight(*names: str, floor: float = 0.0) -> float:
        return max([abs(scores.get(name, 0.0)) for name in names] + [floor])

    candidates: list[tuple[int, float, str]] = []

    def add(order: int, priority: float, line: str) -> None:
        if line:
            candidates.append((order, priority, line))

    # 1. 连接词
    connective_words = _author_words(top_words, "connective")
    connective_level = level("fw_connective_per_1k")
    if connective_words or connective_level:
        parts: list[str] = []
        label = labels["connective"]
        joined_words = _join_words(connective_words)
        if connective_level == "high":
            parts.append(f"{label}偏多，句间关系多靠{label}点明")
            if connective_words:
                parts.append(f"多用{joined_words}")
        elif connective_level == "low":
            parts.append(f"{label}整体偏少，句子多靠并置推进")
            if connective_words:
                parts.append(f"用到时多是{joined_words}")
        elif connective_words:
            parts.append(f"{label}多用{joined_words}")
        if connective_words:
            underused = _underused_words(top_words, baseline, "connective")
            if underused:
                parts.append(f"少用{_join_words(underused)}")
        add(10, weight("fw_connective_per_1k", floor=0.9), "，".join(parts))

    # 2. 结构助词 / 体标记
    particle_level = level("fw_particle_per_1k")
    if particle_level == "high":
        add(20, weight("fw_particle_per_1k"), "结构助词密集，「的」字不避重，定语层层叠加")
    elif particle_level == "low":
        add(20, weight("fw_particle_per_1k"), "省用「的」等结构助词，定语短，名词直接相接")
    aspect_level = level("fw_aspect_per_1k")
    aspect_words = _author_words(top_words, "aspect", limit=2)
    if aspect_level == "high":
        detail = f"，尤其是{_join_words(aspect_words)}" if aspect_words else ""
        add(21, weight("fw_aspect_per_1k"), f"体标记多{detail}，动作常带着状态尾巴")
    elif aspect_level == "low":
        add(21, weight("fw_aspect_per_1k"), "体标记偏少，动作多不带「了、着」，干净落地")

    # 3. 副词
    adverb_level = level("fw_adverb_per_1k")
    adverb_words = _author_words(top_words, "adverb")
    if adverb_level == "high":
        detail = f"，偏好{_join_words(adverb_words)}" if adverb_words else ""
        add(30, weight("fw_adverb_per_1k"), f"副词多{detail}，程度和转折都由副词点出")
    elif adverb_level == "low":
        add(30, weight("fw_adverb_per_1k"), "副词克制，少加程度修饰，让动作自己说话")
    elif adverb_words:
        add(30, 0.4, f"副词偏好{_join_words(adverb_words)}")

    # 4. 介词
    preposition_level = level("fw_preposition_per_1k")
    preposition_words = _author_words(top_words, "preposition")
    if preposition_level == "high":
        detail = f"，常用{_join_words(preposition_words)}" if preposition_words else ""
        add(35, weight("fw_preposition_per_1k"), f"介词框架多{detail}，句内成分靠介词铺展")
    elif preposition_level == "low":
        add(35, weight("fw_preposition_per_1k"), "少用介词框架，方位和对象多直接并置")

    # 5. 语气词 / 句末助词
    modal_level = level("sentence_final_modal_ratio") or level("fw_modal_per_1k")
    final_words = _author_words(top_words, "sentence_final", limit=3, min_share=0.08)
    modal_words = [word for word in final_words if word in lexicon.sentence_final_modal] or _author_words(
        top_words, "modal", limit=2
    )
    if modal_level == "high":
        detail = f"，多用{_join_words(modal_words)}" if modal_words else ""
        add(40, weight("sentence_final_modal_ratio", "fw_modal_per_1k"), f"句末常带语气词{detail}，口气松而近")
    elif modal_level == "low":
        add(40, weight("sentence_final_modal_ratio", "fw_modal_per_1k"), "句末几乎不带语气词，话说完就停")
    classical_final_level = level("sentence_final_classical_ratio")
    classical_final_words = [word for word in final_words if word in lexicon.sentence_final_classical]
    if classical_final_level == "high":
        detail = f"，如{_join_words(classical_final_words)}" if classical_final_words else ""
        add(41, weight("sentence_final_classical_ratio"), f"偶用文言句末语气{detail}")

    # 6. 文言词
    classical_level = level("fw_classical_per_1k")
    classical_words = _author_words(top_words, "classical")
    if classical_level == "high":
        detail = f"，如{_join_words(classical_words)}" if classical_words else ""
        add(50, weight("fw_classical_per_1k"), f"夹用文言虚词{detail}，白话里带着旧句式的骨架")
    elif classical_level == "low":
        add(50, weight("fw_classical_per_1k"), "白话到底，不夹文言虚词")

    # 7. 标点
    comma_level = level("punct_comma_per_1k")
    period_level = level("punct_period_per_1k")
    if comma_level == "high" and period_level == "low":
        add(60, weight("punct_comma_per_1k", "punct_period_per_1k"), "逗号密集、句号稀疏，一句常含多个停顿再落句号")
    elif comma_level == "high":
        add(60, weight("punct_comma_per_1k"), "逗号密集，句内停顿多")
    elif comma_level == "low" and period_level == "high":
        add(60, weight("punct_comma_per_1k", "punct_period_per_1k"), "句号多、逗号少，短句一个接一个落地")
    elif comma_level == "low":
        add(60, weight("punct_comma_per_1k"), "逗号稀疏，一句到底不多停")
    elif period_level == "high":
        add(60, weight("punct_period_per_1k"), "句号多，句子短促成串")
    elif period_level == "low":
        add(60, weight("punct_period_per_1k"), "句号稀疏，长句一路推到底")
    punct_lines = {
        "punct_semicolon_per_1k": ("常用分号把并列分句挂在一句里", "不用分号，并列分句直接断开"),
        "punct_enumeration_per_1k": ("顿号多，喜排列并举", "几乎不用顿号排列"),
        "punct_colon_per_1k": ("冒号多，常用它引出下文或对白", None),
        "punct_ellipsis_per_1k": ("常用省略号留白、吞句", "不用省略号，话说尽即止"),
        "punct_dash_per_1k": ("常用破折号插入补语或急转", "不用破折号"),
        "punct_exclamation_per_1k": ("感叹号多，语气外露", "几乎不用感叹号，情绪压在句里"),
        "punct_question_per_1k": ("多设问、反问，疑问句频繁", "少用问句"),
    }
    for index, (name, (high_line, low_line)) in enumerate(punct_lines.items()):
        punct_level = level(name)
        if punct_level == "high":
            add(61 + index, weight(name), high_line)
        elif punct_level == "low" and low_line:
            add(61 + index, weight(name), low_line)

    # 8. 句长与节奏
    mean_level = level("sent_len_mean")
    if mean_level == "high":
        add(70, weight("sent_len_mean"), "句子偏长，多由几个短语连缀成句")
    elif mean_level == "low":
        add(70, weight("sent_len_mean"), "句子短，一句一个动作或画面")
    std_level = level("sent_len_std")
    if std_level == "high":
        add(71, weight("sent_len_std"), "长短句交错明显，长句后常接极短句")
    elif std_level == "low":
        add(71, weight("sent_len_std"), "句长均匀，节奏平稳少起落")
    if level("sent_short_run_ratio") == "high":
        add(72, weight("sent_short_run_ratio", "sent_short_run_mean"), "短句连打，几个短句紧接着推进")
    autocorr_level = level("sent_len_lag1_autocorr")
    if autocorr_level == "high":
        add(73, weight("sent_len_lag1_autocorr"), "句长成段地相近，短句成串、长句成串，不逐句交替")
    elif autocorr_level == "low":
        add(73, weight("sent_len_lag1_autocorr"), "长句之后接短句，节奏起落分明")
    pauses_level = level("sent_pauses_mean")
    clause_level = level("clause_len_mean")
    if pauses_level == "high" and comma_level != "high":
        add(74, weight("sent_pauses_mean"), "一句里停顿多，逗号把句子切成好几截")
    if clause_level == "high":
        add(75, weight("clause_len_mean"), "停顿之间的短语偏长，一口气说完一层意思")
    elif clause_level == "low":
        add(75, weight("clause_len_mean"), "停顿之间的短语很短，读来急促")

    # 9. 段落
    para_level = level("para_len_mean")
    if para_level == "high":
        add(80, weight("para_len_mean"), "段落长，一段承载多个动作或转折")
    elif para_level == "low":
        add(80, weight("para_len_mean"), "段落短，频繁换段")
    if level("para_single_sentence_ratio") == "high":
        add(81, weight("para_single_sentence_ratio"), "常一句成段，让单句独立站住")
    dialogue_level = level("para_dialogue_ratio")
    if dialogue_level == "high":
        add(82, weight("para_dialogue_ratio"), "对白段多，叙述常让位给说话")
    elif dialogue_level == "low":
        add(82, weight("para_dialogue_ratio"), "对白段少，以叙述为主")

    # 10. 对白引导
    guide_shares = {
        placement: values.get(f"dialogue_guide_{placement}_share", 0.0) for placement in ("pre", "post", "none")
    }
    if sum(guide_shares.values()) > 0:
        dominant = max(guide_shares, key=lambda key: guide_shares[key])
        guide_line = {
            "none": "对白多无引导词，说话人靠上下文辨认",
            "pre": "对白引导词置于引语前，先点出谁说，再引出话",
            "post": "对白引导词多置于引语后，话说完再补上是谁说的",
        }[dominant]
        if guide_shares[dominant] >= 0.5:
            add(90, weight(f"dialogue_guide_{dominant}_share", floor=0.9), guide_line)
        elif guide_shares["none"] < 0.5:
            add(
                90,
                weight("dialogue_guide_pre_share", "dialogue_guide_post_share", floor=0.9),
                "对白引导词前置、后置都有，位置随语气变化",
            )
        verb_shares = {
            key: values.get(f"speech_verb_{key}_share", 0.0) for key in SPEECH_VERB_KEYS if key != "other"
        }
        if sum(verb_shares.values()) > 0:
            top_key = max(verb_shares, key=lambda key: verb_shares[key])
            if verb_shares[top_key] >= 0.4:
                line = f"引导动词偏好「{_SPEECH_VERB_LABELS[top_key]}」"
                top_label = _SPEECH_VERB_LABELS[top_key]
                underused_verbs = [
                    verb
                    for verb in _underused_words(top_words, baseline, "speech_verb", limit=2)
                    if verb not in (top_label, "其他")
                ]
                if underused_verbs:
                    line += f"，少用「{underused_verbs[0]}」"
                add(91, weight(f"speech_verb_{top_key}_share", floor=0.8), line)

    # 11. 四字格 / 叠词
    four_level = level("four_char_segment_per_1k")
    if four_level == "high":
        add(100, weight("four_char_segment_per_1k"), "四字格偏多，好用成语和四字短语收束句子")
    elif four_level == "low":
        add(100, weight("four_char_segment_per_1k"), "四字格偏低，不堆成语")
    redup_level = level("redup_total_per_1k")
    if redup_level == "high":
        add(110, weight("redup_total_per_1k"), "叠词多，双声叠字的形容与状语常见")
    elif redup_level == "low":
        add(110, weight("redup_total_per_1k"), "少用叠词")

    # 12. 人称
    first = values.get("person_first_share", 0.0)
    third = values.get("person_third_share", 0.0)
    second = values.get("person_second_share", 0.0)
    if first + second + third > 0:
        if first >= 0.55:
            add(120, weight("person_first_share", floor=0.6), "第一人称叙述为主，「我」贯穿全篇")
        elif third >= 0.6:
            pronoun_words = [
                word
                for word in _author_words(top_words, "pronoun", limit=5, min_share=0.03)
                if word in lexicon.person.get("third", ())
            ]
            detail = f"，多用{_join_words(pronoun_words[:2])}" if pronoun_words else ""
            add(120, weight("person_third_share", floor=0.6), f"第三人称叙述为主{detail}")
        elif second >= 0.4:
            add(120, weight("person_second_share", floor=0.6), "第二人称「你」的呼告频繁")

    # 13. 词汇
    ttr_level = level("lexical_char_ttr")
    if ttr_level == "high":
        add(130, weight("lexical_char_ttr"), "用字丰富，少重复同一批字词")
    elif ttr_level == "low":
        add(130, weight("lexical_char_ttr"), "用字克制，常重复同一批字词")

    if not candidates:
        return []
    chosen = sorted(candidates, key=lambda item: (-item[1], item[0]))[:MAX_HABIT_LINES]
    chosen.sort(key=lambda item: item[0])
    return [line for _order, _priority, line in chosen]


# ---------------------------------------------------------------------------
# 基线生成(__main__ 子命令)
# ---------------------------------------------------------------------------


def _chunk_paragraphs(paragraphs: Sequence[str], block_chars: int) -> list[list[str]]:
    """按累计字数把段落切成 ≈block_chars 的块;残尾并入最后一块(与 metrics 一致)。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in paragraphs:
        current.append(paragraph)
        current_chars += len(paragraph)
        if current_chars >= block_chars:
            blocks.append(current)
            current = []
            current_chars = 0
    if current:
        if blocks:
            blocks[-1].extend(current)
        else:
            blocks.append(current)
    return blocks


def build_voice_baseline(
    corpus_dir: str | Path,
    *,
    block_chars: int = BASELINE_BLOCK_CHARS,
) -> dict[str, Any]:
    """用 corpus_dir 下全部 ``*.txt`` 按 block_chars 字块计算每个特征的 mean/std/p15/p50/p85。"""
    directory = Path(corpus_dir)
    files = sorted(path for path in directory.glob("*.txt") if path.is_file())
    if not files:
        raise FileNotFoundError(f"no *.txt corpus files under {directory}")
    block_features: list[Mapping[str, float]] = []
    all_paragraphs: list[str] = []
    corpus_records: list[dict[str, Any]] = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        normalized = normalize_text(raw)
        paragraphs = [body for _start, _end, body in split_paragraphs(normalized)]
        blocks = _chunk_paragraphs(paragraphs, block_chars)
        for block in blocks:
            block_features.append(compute_voice_signature(block, baseline={})["features"])
        all_paragraphs.extend(paragraphs)
        corpus_records.append(
            {
                "file": path.name,
                "chars": len(normalized),
                "paragraphs": len(paragraphs),
                "blocks": len(blocks),
                "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            }
        )
    corpus_signature = compute_voice_signature(all_paragraphs, baseline={})
    stats: dict[str, dict[str, float]] = {}
    for name in FEATURE_NAMES:
        values = [float(block.get(name, 0.0)) for block in block_features]
        stats[name] = {
            "mean": _round(statistics.fmean(values)),
            "std": _round(statistics.pstdev(values) if len(values) > 1 else 0.0),
            "p15": _round(_quantile(values, 0.15)),
            "p50": _round(_quantile(values, 0.50)),
            "p85": _round(_quantile(values, 0.85)),
        }
    return {
        "version": VOICE_BASELINE_VERSION,
        "signature_version": VOICE_SIGNATURE_VERSION,
        "block_chars": int(block_chars),
        "block_count": len(block_features),
        "corpus": corpus_records,
        "features": stats,
        "top_words": corpus_signature["top_words"],
    }


def render_voice_baseline_yaml(baseline: Mapping[str, Any], *, command: str, generated_at: str) -> str:
    """把基线写成带来源注释的 YAML(手工排版,保证稳定与可读)。"""
    lines = [
        "# Style Reference v2 声音签名基线(voice_signature.py 消费,勿手改数值)。",
        "# 「一般中文小说」基线:用 backend/tests/golden/style_reference/corpus 全部公版文本",
        "# (鲁迅短篇 + 朱自清散文;luxun_kongyiji / zhuziqing_essays 与主集有重叠,按规格「全部文本」照收)",
        f"# 按 {baseline['block_chars']} 字块切分,对每个特征取块间 mean / std / p15 / p50 / p85。",
        "# render_voice_habits 以 p15 / p85 判「偏低 / 偏高」;feature_z_scores 用 mean / std。",
        "# 再生成(backend 目录下):",
        f"#   {command}",
        f"# generated_at: {generated_at}",
        f"version: {baseline['version']}",
        f"signature_version: {baseline['signature_version']}",
        f"block_chars: {baseline['block_chars']}",
        f"block_count: {baseline['block_count']}",
        "corpus:",
    ]
    for record in baseline["corpus"]:
        lines.append(
            f"  - {{file: {json.dumps(record['file'], ensure_ascii=False)}, chars: {record['chars']}, "
            f"paragraphs: {record['paragraphs']}, blocks: {record['blocks']}, "
            f"normalized_sha256: {record['normalized_sha256']}}}"
        )
    lines.append("features:")
    for name in FEATURE_NAMES:
        entry = baseline["features"][name]
        lines.append(
            f"  {name}: {{mean: {entry['mean']}, std: {entry['std']}, "
            f"p15: {entry['p15']}, p50: {entry['p50']}, p85: {entry['p85']}}}"
        )
    lines.append("top_words:")
    for group in TOP_WORD_GROUPS:
        entries = baseline["top_words"].get(group) or []
        rendered = ", ".join(
            f"[{json.dumps(str(word), ensure_ascii=False)}, {_round(share)}]" for word, share in entries
        )
        lines.append(f"  {group}: [{rendered}]")
    return "\n".join(lines) + "\n"


def _default_corpus_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "tests" / "golden" / "style_reference" / "corpus"


def _default_baseline_path() -> Path:
    return Path(__file__).resolve().parents[5] / "config" / "style_reference" / "voice_baseline.yaml"


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m novel_system.services.style_reference.voice_signature",
        description="声音签名工具:生成基线 / 检视单个文本的签名与习惯句。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-baseline", help="用黄金语料生成 voice_baseline.yaml")
    build.add_argument("--corpus-dir", default=str(_default_corpus_dir()))
    build.add_argument("--output", default=str(_default_baseline_path()))
    build.add_argument("--block-chars", type=int, default=BASELINE_BLOCK_CHARS)
    inspect = sub.add_parser("inspect", help="打印一个文本文件的签名与习惯句")
    inspect.add_argument("path")
    args = parser.parse_args(argv)

    if args.command == "build-baseline":
        import datetime

        baseline = build_voice_baseline(args.corpus_dir, block_chars=args.block_chars)
        command = (
            "python -m novel_system.services.style_reference.voice_signature build-baseline"
            f" --block-chars {args.block_chars}"
        )
        generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        output = Path(args.output)
        output.write_text(
            render_voice_baseline_yaml(baseline, command=command, generated_at=generated_at),
            encoding="utf-8",
        )
        sys.stdout.write(f"=> {output} ({baseline['block_count']} blocks, {len(FEATURE_NAMES)} features)\n")
        return 0
    if args.command == "inspect":
        text = Path(args.path).read_text(encoding="utf-8")
        signature = compute_voice_signature_for_text(text)
        sys.stdout.write(json.dumps(signature, ensure_ascii=False, indent=2) + "\n")
        for line in render_voice_habits(signature):
            sys.stdout.write(f"- {line}\n")
        return 0
    return 2


__all__ = [
    "BASELINE_BLOCK_CHARS",
    "FEATURE_NAMES",
    "FUNCTION_WORD_GROUPS",
    "LEXICAL_WINDOW_CHARS",
    "MAX_HABIT_LINES",
    "MIN_RENDER_CHARS",
    "REPETITION_FEATURES",
    "Z_MAX_AGGREGATION_BLOCKS",
    "SPEECH_VERB_KEYS",
    "TOP_WORD_GROUPS",
    "VOICE_BASELINE_VERSION",
    "VOICE_SIGNATURE_VERSION",
    "VoiceLexicon",
    "build_voice_baseline",
    "clear_voice_signature_cache",
    "compute_voice_signature",
    "compute_voice_signature_for_text",
    "distinctive_features",
    "feature_z_scores",
    "load_voice_baseline",
    "load_voice_lexicon",
    "render_voice_baseline_yaml",
    "render_voice_habits",
]


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(_main())
