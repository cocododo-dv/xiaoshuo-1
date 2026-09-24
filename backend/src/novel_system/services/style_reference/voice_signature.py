"""风格参考「声音签名」——测量核之上的整书 / 单文本签名与习惯句。

2026-09-23（风格参考 v3）：全部计数改由 ``measure.py`` 测量核一遍算出（唯一分段规则、汇总口径、唯一虚词表、
唯一对白定义），本模块只做三件事：

- ``compute_voice_signature(texts)`` / ``compute_voice_signature_for_text(text)``：测量核特征（``FEATURE_NAMES``
  = ``measure.FEATURE_NAMES``，旧的 52 个名字保留、另加 5 个）+ 各组高频词（``top_words``）+ 统计量，
  形状与 v1 相同（``{"version", "features", "top_words", "deliberate_repetition", "stats"}``）；
- ``render_voice_habits(signature)``：≤12 行**绝对、具体**的习惯句——作者自己的高频词与大致频率（「连接多用
  就、也、还、可是」「句末常带吧、呢、啊（大约每十句一次）」「几乎不用分号」），**不再**拿 1920 年代的鲁迅 /
  朱自清基线比「偏多 / 偏少」（v1 对真实网文说「连接词整体偏少」，而生成稿用得比作者还少得多）；不含阿拉伯数字；
- 基线（``voice_baseline.yaml``）在管线里只剩一处用途：``deliberate_repetition``（叠词 / 短句连打 ≥ 基线字面 p85）。
  z 值接口（``feature_z_scores`` / ``distinctive_features``）已没有管线调用方——样例窗口的典型度在 ``windows.py``、
  「像不像作者」的读数看作者自己的窗口分布（``fidelity.py``）——只留作检验基线本身的工具（黄金语料测试用）。

基线由运维工具 ``python -m novel_system.tools.build_voice_baseline build-baseline`` 用
``backend/tests/golden/style_reference/corpus`` 全部文本按 1500 字块生成（2026-09-24 从本模块的 ``__main__`` 搬过去）；
测量口径变了（``measure.KERNEL_VERSION``）就要重跑。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from novel_system.services.style_reference.config_loader import load_optional_yaml_config
from novel_system.services.style_reference.measure import (
    FEATURE_NAMES,
    FUNCTION_WORD_GROUPS,
    KERNEL_VERSION,
    LEXICAL_MAX_WINDOWS,
    LEXICAL_WINDOW_CHARS,
    SHORT_SENTENCE_CHARS,
    SPEECH_VERB_KEYS,
    SPEECH_VERB_LABELS,
    KernelLexicon,
    TextMeasure,
    kernel_features,
    load_kernel_lexicon,
    measure_paragraphs,
    measure_text,
)

# v2（2026-09-23）：测量核口径（段内换行即段界、引号不含 ‘’、人称只数叙述、追加 5 个特征）。
VOICE_SIGNATURE_VERSION = "voice_signature_v2"
VOICE_BASELINE_VERSION = "voice_baseline_v2"

# 基线块尺寸(字符)。≈ 一个场景的长度。
BASELINE_BLOCK_CHARS = 1500
TOP_WORDS_PER_GROUP = 5
MAX_HABIT_LINES = 12
# 整书签名是 n 块的聚合,块间 std 对它过宽——旧 z 值接口按 1/sqrt(min(n, 16)) 收窄;
# REPETITION_FEATURES(叠词 / 短句连打)始终按字面 p85 判(deliberate_repetition 的规格口径)。
Z_MAX_AGGREGATION_BLOCKS = 16
REPETITION_FEATURES: tuple[str, ...] = ("redup_total_per_1k", "sent_short_run_ratio")
# 少于这些可见字符的文本不渲染习惯句(统计无意义)。
MIN_RENDER_CHARS = 200
_Z_CLIP = 8.0

# top_words 的组:8 个虚词组 + 句末助词 + 引导动词。
TOP_WORD_GROUPS: tuple[str, ...] = (*FUNCTION_WORD_GROUPS, "sentence_final", "speech_verb")
_SPEECH_VERB_LABELS = SPEECH_VERB_LABELS


def load_voice_lexicon() -> KernelLexicon:
    """返回编译后的闭类词表(测量核里的唯一一张;清缓存用 ``measure.clear_kernel_cache``)。"""
    return load_kernel_lexicon()


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


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _round(value: float) -> float:
    return round(_finite(value), 6)


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
# 签名
# ---------------------------------------------------------------------------


def _empty_signature() -> dict[str, Any]:
    return {
        "version": VOICE_SIGNATURE_VERSION,
        "kernel_version": KERNEL_VERSION,
        "features": {name: 0.0 for name in FEATURE_NAMES},
        "top_words": {group: [] for group in TOP_WORD_GROUPS},
        "deliberate_repetition": False,
        "stats": {"char_count": 0, "sentence_count": 0, "paragraph_count": 0, "quote_count": 0},
    }


def signature_from_measure(
    measure: TextMeasure,
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """测量结果 → 声音签名(``baseline`` 只用于 ``deliberate_repetition``,缺省读 yaml)。"""
    if measure.char_count <= 0:
        return _empty_signature()
    lexicon = load_kernel_lexicon()
    features = kernel_features(measure)
    top_words = {group: _top_words(measure.group_counts(group, lexicon)) for group in FUNCTION_WORD_GROUPS}
    top_words["sentence_final"] = _top_words(measure.sentence_final_counts)
    top_words["speech_verb"] = _top_words(
        {SPEECH_VERB_LABELS[key]: int(measure.speech_verb_counts.get(key, 0)) for key in SPEECH_VERB_KEYS}
    )
    if baseline is None:
        baseline = load_voice_baseline()
    return {
        "version": VOICE_SIGNATURE_VERSION,
        "kernel_version": KERNEL_VERSION,
        "features": features,
        "top_words": {group: top_words.get(group, []) for group in TOP_WORD_GROUPS},
        "deliberate_repetition": _deliberate_repetition(features, baseline),
        "stats": {
            "char_count": measure.char_count,
            "sentence_count": measure.sentence_count,
            "paragraph_count": measure.paragraph_count,
            "quote_count": measure.quote_count,
        },
    }


def compute_voice_signature(
    texts: list[str],
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一组段落(通常是一部书的全部段落)的声音签名。

    每项按换行再切一次(测量核的唯一分段规则);``baseline`` 只用于 ``deliberate_repetition`` 的 p85 判定,
    缺省读 ``voice_baseline.yaml``(缺文件时判 False)。
    """
    return signature_from_measure(measure_paragraphs(texts or []), baseline=baseline)


def compute_voice_signature_for_text(
    text: str,
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单文本版本(生成稿 / 作者稿 HTML / 窗口):按测量核规则分段后计算。"""
    return signature_from_measure(measure_text(text), baseline=baseline)


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
    z 值接口;整书签名对照块级 p85 本身判定。旗标只放松下游的新鲜度守卫(作者本就爱叠词 /
    连打短句时,不把重复当毛病),不进习惯句。
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
    (传 1 即得字面块级 z)。仅传 features 时 n=1。
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
# 习惯句渲染(绝对、具体:作者自己的高频词与大致频率,不与任何基线比)
# ---------------------------------------------------------------------------

_CN_DIGITS = "零一二三四五六七八九"


def _cn_int(value: int) -> str:
    """0–999 的中文读法(「两」用于量词前由调用方处理);超出按「上千」。"""
    number = max(0, int(value))
    if number < 10:
        return _CN_DIGITS[number]
    if number < 20:
        return "十" + (_CN_DIGITS[number % 10] if number % 10 else "")
    if number < 100:
        tens, ones = divmod(number, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        head = _CN_DIGITS[hundreds] + "百"
        if rest == 0:
            return head
        if rest < 10:
            return head + "零" + _CN_DIGITS[rest]
        tens, ones = divmod(rest, 10)
        return head + _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
    return "上千"


def _cn_count(value: int) -> str:
    """量词前的数:2 → 「两」,其余同 :func:`_cn_int`。"""
    return "两" if int(value) == 2 else _cn_int(value)


def _rate_phrase(rate: float, unit: str) -> str:
    """每千字的频率 → 「每千字约三个」/「每两千字约一处」/ ""(几乎没有)。"""
    value = _finite(rate)
    if value >= 1.0:
        return f"每千字约{_cn_count(round(value))}{unit}"
    if value >= 0.2:
        return f"每{_cn_count(round(1.0 / value))}千字约一{unit}"
    return ""


def _every_n_sentences(ratio: float) -> str:
    value = _finite(ratio)
    if value <= 0:
        return ""
    n = max(1, int(round(1.0 / value)))
    if n <= 1:
        return "几乎每句都有"
    return f"大约每{_cn_count(n)}句一次"


def _tenths_phrase(share: float) -> str:
    """0–1 的比例 → 「约四成」/「不到一成」/「几乎全部」。"""
    value = _finite(share)
    if value >= 0.95:
        return "几乎全部"
    if value < 0.05:
        return "不到一成"
    return f"约{_cn_int(max(1, round(value * 10)))}成"


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


# 标点「常用 / 很少用」的绝对门槛(每千字):低于 rare 算几乎不用,高于 frequent 算常用。
_PUNCT_HABITS: tuple[tuple[str, str, float, float], ...] = (
    ("punct_ellipsis_per_1k", "省略号", 0.2, 2.0),
    ("punct_dash_per_1k", "破折号", 0.2, 1.5),
    ("punct_semicolon_per_1k", "分号", 0.2, 1.0),
    ("punct_exclamation_per_1k", "感叹号", 0.5, 4.0),
    ("punct_question_per_1k", "问号", 0.5, 6.0),
    ("punct_colon_per_1k", "冒号", 0.3, 4.0),
    ("punct_enumeration_per_1k", "顿号", 0.3, 5.0),
)


def render_voice_habits(features: Mapping[str, Any]) -> list[str]:
    """把声音签名渲染成 ≤12 行生成器可执行的中文习惯句。

    只描述作者自己:高频词(``top_words``,整份签名时才有)与大致频率(中文数字),绝不说「比一般作家偏多 /
    偏少」——不与任何基线比较(v2 起;从前的 ``baseline`` 参数已删)。输出不含阿拉伯数字。
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

    def value(name: str) -> float:
        return _finite(values.get(name, 0.0))

    lexicon = load_kernel_lexicon()
    lines: list[str] = []  # 按重要性排列,超过 MAX_HABIT_LINES 从末尾截

    # 1. 句长与起伏:平均几个字、短句与长句大约多长
    mean = value("sent_len_mean")
    if mean > 0:
        line = f"句子平均约{_cn_int(round(mean))}字"
        short, long_ = value("sent_len_p10"), value("sent_len_p90")
        if long_ > short > 0:
            line += f"，短的{_cn_int(round(short))}字上下、长的{_cn_int(round(long_))}字上下"
        spread = value("sent_len_std") / mean
        if spread >= 0.75:
            line += "，长短交错明显"
        elif 0 < spread <= 0.45:
            line += "，长短比较均匀"
        lines.append(line)

    # 2. 段落
    para_mean = value("para_len_mean")
    if para_mean > 0:
        line = f"段落平均约{_cn_int(round(para_mean))}字"
        single = value("para_single_sentence_ratio")
        if single >= 0.05:
            line += f"，{_tenths_phrase(single)}的段落只有一句"
        lines.append(line)

    # 3. 对白比重与引导
    if "dialogue_char_share" in values:
        dialogue = value("dialogue_char_share")
        if dialogue >= 0.95:
            lines.append("几乎通篇是对白")
        elif dialogue >= 0.05:
            lines.append(f"对白约占全文字数的{_cn_int(max(1, round(dialogue * 10)))}成")
        else:
            lines.append("几乎没有对白，以叙述为主")
    guide = {placement: value(f"dialogue_guide_{placement}_share") for placement in ("pre", "post", "none")}
    if sum(guide.values()) > 0:
        dominant = max(guide, key=lambda key: guide[key])
        verbs = {key: value(f"speech_verb_{key}_share") for key in SPEECH_VERB_KEYS if key != "other"}
        top_verb = max(verbs, key=lambda key: verbs[key]) if any(verbs.values()) else None
        verb_note = (
            f"，引导动词多用「{SPEECH_VERB_LABELS[top_verb]}」"
            if top_verb is not None and verbs[top_verb] >= 0.4
            else ""
        )
        if guide[dominant] >= 0.5 and dominant == "none":
            lines.append("对白多不加「某某说」，靠上下文分辨是谁在说话")
        elif guide[dominant] >= 0.5 and dominant == "pre":
            lines.append("对白前常先点出谁说，再引出话" + verb_note)
        elif guide[dominant] >= 0.5:
            lines.append("对白后才补上是谁说的" + verb_note)
        else:
            lines.append("对白有的先点出说话人、有的不点，随语气变" + verb_note)

    # 4. 句末语气词
    if "sentence_final_modal_ratio" in values:
        modal_ratio = value("sentence_final_modal_ratio")
        final_words = [
            word
            for word in _author_words(top_words, "sentence_final", limit=5, min_share=0.05)
            if word in lexicon.sentence_final_modal
        ][:3]
        if modal_ratio >= 0.02:
            detail = _join_words(final_words) if final_words else "语气词"
            lines.append(f"句末常带{detail}（{_every_n_sentences(modal_ratio)}）")
        else:
            lines.append("句末几乎不带语气词，话说完就停")

    # 5. 连接词(作者自己的高频词 + 频率)
    connective_words = _author_words(top_words, "connective", limit=4)
    connective_rate = _rate_phrase(value("fw_connective_per_1k"), "个")
    if connective_words:
        lines.append(
            f"连接多用{_join_words(connective_words)}" + (f"（连接词{connective_rate}）" if connective_rate else "")
        )
    elif connective_rate:
        lines.append(f"连接词{connective_rate}")

    # 6. 标点:常用的与几乎不用的
    frequent = [label for name, label, _low, high in _PUNCT_HABITS if name in values and value(name) >= high]
    rare = [label for name, label, low, _high in _PUNCT_HABITS if name in values and value(name) < low]
    if frequent:
        lines.append(f"常用{_join_words(frequent[:3])}")
    if rare:
        lines.append(f"几乎不用{_join_words(rare[:3])}")

    # 7. 人称(只看叙述)
    first = value("person_first_share")
    second = value("person_second_share")
    third = value("person_third_share")
    if first + second + third > 0:
        if first >= 0.55:
            lines.append("第一人称叙述，「我」贯穿全篇")
        elif third >= 0.6 and first < 0.15:
            lines.append("第三人称叙述，「我」「你」只在对白里出现")
        elif third >= 0.6:
            lines.append("以第三人称叙述为主")
        elif second >= 0.4:
            lines.append("叙述里常用第二人称「你」呼告")
        else:
            lines.append("叙述人称混用")

    # 8. 具体数字、英文词(只在确实常见时说)
    quantities = value("digit_run_per_1k") + value("numeral_unit_per_1k")
    if quantities >= 1.0:
        lines.append(f"常写具体数字与计量（{_rate_phrase(quantities, '处')}）")
    latin = value("latin_word_per_1k")
    if latin >= 0.5:
        rate = _rate_phrase(latin, "个")
        lines.append("叙述和对白里常夹英文词" + (f"（{rate}）" if rate else ""))

    # 9. 副词 / 体标记 / 短句连打 / 四字格 / 叠词
    adverb_words = _author_words(top_words, "adverb", limit=4)
    if adverb_words:
        lines.append(f"常用副词：{_join_words(adverb_words)}")
    if value("sent_short_run_ratio") >= 0.15:
        lines.append("常把几个极短的句子连着用")
    aspect_words = _author_words(top_words, "aspect", limit=2)
    if aspect_words and value("fw_aspect_per_1k") >= 15:
        lines.append(f"动作后常带{_join_words(aspect_words)}")
    if value("four_char_segment_per_1k") >= 10:
        lines.append("常用四字短语收束句子")
    if value("redup_total_per_1k") >= 10:
        lines.append("常用叠词")

    deduped: list[str] = []
    for line in lines:
        if line and line not in deduped:
            deduped.append(line)
    return deduped[:MAX_HABIT_LINES]


def quantile(values: Sequence[float], ratio: float) -> float:
    """块间分位数(线性插值);基线生成工具 ``tools.build_voice_baseline`` 用它算 p15 / p50 / p85。"""
    return _quantile(values, ratio)


def round_stat(value: float) -> float:
    """基线里的统计量统一保留六位小数(与 ``voice_baseline.yaml`` 的写法一致)。"""
    return _round(value)


__all__ = [
    "BASELINE_BLOCK_CHARS",
    "KERNEL_VERSION",
    "SHORT_SENTENCE_CHARS",
    "LEXICAL_MAX_WINDOWS",
    "TOP_WORDS_PER_GROUP",
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
    "compute_voice_signature",
    "compute_voice_signature_for_text",
    "distinctive_features",
    "feature_z_scores",
    "load_voice_baseline",
    "load_voice_lexicon",
    "quantile",
    "render_voice_habits",
    "round_stat",
    "signature_from_measure",
]
