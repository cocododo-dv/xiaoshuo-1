"""Style Reference v1.1 硬指标计算(纯函数,无 LLM)。

依据《风格参考模块重构执行手册 v1.1》§6.3。
贯穿三阶段使用:
  ① 抽取(PR-3)时作为 prompt 上下文注入 `metrics_anchor`
  ② 验证(PR-7)时作为对照(`quantitative` 路径)
  ③ 预览(PR-4)时作为置信度证据

中文分句沿用 `text_utils.split_sentences`。感官词表从
`config/style_reference/sensory_lexicon.yaml` 读。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Callable, Literal

from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.text_utils import split_sentences

# ---------------------------------------------------------------------------
# MetricName 全清单(26 项;与 §6.3 严格对账)
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
    # 感官(5,场景层)
    "sensory_visual_per_1k",
    "sensory_auditory_per_1k",
    "sensory_olfactory_per_1k",
    "sensory_tactile_per_1k",
    "sensory_gustatory_per_1k",
]

METRIC_NAMES: tuple[str, ...] = (
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
    "dialogue_ratio",
    "psychology_ratio",
    "description_env_ratio",
    "description_char_ratio",
    "action_ratio",
    "narration_ratio",
    "transition_ratio",
    "flashback_ratio",
    "sensory_visual_per_1k",
    "sensory_auditory_per_1k",
    "sensory_olfactory_per_1k",
    "sensory_tactile_per_1k",
    "sensory_gustatory_per_1k",
)

# 不并入既有 26 项验证契约，避免改变已冻结的 QC 分母；这些指标单独落入
# book.stats_json.prose_shape_metrics，再与 Profile 的 metrics_baseline 合并供
# 生成注入使用。它们完全由正文换段/标点形状计算，不依赖段型分类器。
PROSE_SHAPE_METRIC_NAMES: tuple[str, ...] = (
    "paragraph_mean_chars",
    "paragraph_length_std_chars",
    "paragraphs_per_1k",
    "single_sentence_paragraph_ratio",
    "quote_led_paragraph_ratio",
)

# 常量词表。
# 2026-07 勘误:原字符串含重复字符(`——` 两个 `—`、`\"\"` 两个 ASCII 引号、
# ASCII `:` 出现两次),`_density_per_1k_chars` 按字符逐个 count 会**双计**;
# 且遗漏全角冒号 `：` 与全角括号 `（）`。现去重并补全,计数函数同时做防御性去重。
_PUNCT_CHARS = "。！？，；：、—…“”\"‘’「」『』《》（）()【】!?,;:."
_CLASSICAL_MARKERS = ("之", "乎", "者", "也", "焉", "矣", "哉", "曰", "兮", "其")
# 2026-09-14 保真修补:文言比例只认文言**用法**。「也 / 其 / 者 / 之」在现代汉语里是最常见的
# 副词、代词与后缀(也许、其他、作者、之后),按字面计数会让一本现代网文得到「文言虚词比例较高频」。
_CLASSICAL_FINAL_PARTICLES = ("也", "矣", "焉", "哉", "乎", "兮", "耳", "欤", "邪", "云")
_CLASSICAL_ZHI_MODERN = (
    "之后", "之前", "之间", "之一", "之中", "之外", "之内", "之上", "之下", "之类", "之所以",
    "之际", "之处", "之余", "之久", "之多", "之大", "之高", "之长", "之远", "之近", "之极",
    "之初", "之末", "之路", "之地", "之心", "之情", "之力", "之意", "之词", "之举", "之物",
    "总之", "反之", "加之", "随之", "因之", "分之", "言之", "换言之", "简言之", "久而久之",
)
_CLASSICAL_QI_MODERN = (
    "其他", "其它", "其实", "其中", "其余", "其次", "尤其", "极其", "与其", "其后", "其间",
    "其一", "其二", "其三", "其内", "其外", "如其", "任其", "令其", "使其", "将其", "把其",
    "对其", "向其", "为其", "自其", "从其", "由其", "及其", "其所", "其数", "其乐", "其谈",
    "其貌", "其名", "其人", "其事", "其父", "其母", "其妻", "其子", "其家", "其身", "其上",
    "其下", "其前", "其后", "其左", "其右",
)
_CLASSICAL_SENTENCE_TAIL_STRIP = "”’」』\"'）)】》"
_CLASSICAL_PUNCT_STRIP = "。！？!?…；;，,：:"
_CLASSICAL_ZHE_RE = re.compile(r"者(?:也|[，,])")
_COLLOQUIAL_MARKERS = ("吧", "呢", "啊", "嗯", "哎", "嘛", "哦", "哪", "呀", "罢", "嘞")
# 比喻关键词;TODO(PR-3):LLM 抽取增强,目前以词表近似
_METAPHOR_MARKERS = ("像", "如同", "仿佛", "犹如", "好似", "恰似", "宛如", "似的", "好像")
# 拟人词表:与 metaphor 同为「词表 substring 密度代理」,无主语判别——既低召回
# (漏掉大量拟人写法),也会把人物真实动作(如「他呜咽」)误计入,只是一个确定性
# 下界,用于避免该子维静默恒 0,并非可靠的拟人度量。真正区分主语的拟人识别需
# LLM 语义抽取(§14 增强项)。
_PERSONIFICATION_MARKERS: tuple[str, ...] = (
    "呜咽", "低语", "私语", "呢喃", "低吟", "怒号", "咆哮", "嘶鸣",
    "苏醒", "沉睡", "沉眠", "苏生", "翩跹", "起舞", "招手", "探头",
    "嬉戏", "依偎", "舒展", "蜷伏",
)


# ---------------------------------------------------------------------------
# ParagraphRecord 与 MetricsEngine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParagraphRecord:
    """metrics 计算的最小段落抽象。

    PR-2 ingest 调 segmentation 拿到结果后用此类型传给 MetricsEngine,
    不依赖 ORM 实例(便于纯函数测试)。
    """

    text: str
    paragraph_type: str  # ParagraphType.value

    @property
    def char_count(self) -> int:
        return len(self.text)


class MetricsEngine:
    """26 项 MetricName 的纯函数实现。

    用法::

        engine = MetricsEngine()
        means = engine.compute_all(paragraphs)               # dict[name, float]
        var   = engine.compute_with_variance(paragraphs)     # dict[name, (mean, std)]
    """

    def __init__(self, sensory_lexicon: dict[str, list[str]] | None = None) -> None:
        self._sensory_lexicon = sensory_lexicon if sensory_lexicon is not None else self._load_sensory_lexicon()

    @staticmethod
    def _load_sensory_lexicon() -> dict[str, list[str]]:
        cfg = load_yaml_config("sensory_lexicon")
        # cfg 顶层是 dict {visual: [...], auditory: [...], ...}
        return {sense: [str(w) for w in cfg.get(sense, [])] for sense in
                ("visual", "auditory", "olfactory", "tactile", "gustatory")}

    def compute_all(self, paragraphs: list[ParagraphRecord]) -> dict[str, float]:
        if not paragraphs:
            return {name: 0.0 for name in METRIC_NAMES}
        return {
            name: statistics.fmean(self._per_paragraph(name, p) for p in paragraphs)
            for name in METRIC_NAMES
        }

    def compute_with_variance(
        self, paragraphs: list[ParagraphRecord]
    ) -> dict[str, tuple[float, float]]:
        """返回 {metric: (mean, std)}。

        - **mean** 为全文单值(== ``compute_all``,逐段值的均值),不随分块变化;
        - **std** 为**块间标准差**(块 ≈ 场景大小,见 ``_chunk_by_chars``),而非逐段标准差。

        为何用块间 std(2026-06 校准修正):逐段 std 噪声极大——段落短、且
        paragraph_type 比例指标逐段取 0/1(段级 std 高达 ~0.5)——使
        ``tolerance = max(std×1.25, floor)`` 宽到几乎不拦截(原作互比 26 项零超容差)。
        回测对照的是「整段生成文本(≈一个场景/块)的单值」,故 tolerance 应反映
        **作者自身块到块的自然波动**,即块间 std。这样量化门才真正有区分力,
        同时 floor 防止低波动指标过紧。
        """
        if not paragraphs:
            return {name: (0.0, 0.0) for name in METRIC_NAMES}
        means = self.compute_all(paragraphs)
        chunks = _chunk_by_chars(paragraphs, _VARIANCE_CHUNK_CHARS)
        result: dict[str, tuple[float, float]] = {}
        for name in METRIC_NAMES:
            if len(chunks) > 1:
                chunk_values = [
                    statistics.fmean(self._per_paragraph(name, p) for p in chunk)
                    for chunk in chunks
                ]
                std = statistics.pstdev(chunk_values)
            else:
                # 单块(短语料):无块间样本,std=0 → tolerance 落到 floor
                std = 0.0
            result[name] = (means[name], std)
        return result

    # ------------------------------------------------------------------ private

    def _per_paragraph(self, name: str, p: ParagraphRecord) -> float:
        if name == "avg_sentence_length":
            return _avg_sentence_length(p.text)
        if name == "sentence_length_std":
            return _sentence_length_std(p.text)
        if name == "short_sentence_ratio":
            return _sentence_length_ratio(p.text, lambda L: L <= 10)
        if name == "long_sentence_ratio":
            return _sentence_length_ratio(p.text, lambda L: L >= 30)
        if name == "punctuation_density_per_1k":
            return _density_per_1k_chars(p.text, _PUNCT_CHARS)
        if name == "dash_em_density_per_1k":
            return _density_per_1k_pattern(p.text, r"——|—")
        if name == "ellipsis_density_per_1k":
            return _density_per_1k_pattern(p.text, r"……|…|\.{3,}")
        # 2026-07 勘误:原为 ";;" / "??"(两个 ASCII 字符)——全角 `；`/`？` 完全
        # 不被统计(中文文本该指标恒 ≈0),ASCII 反被双计。改为全角 + ASCII 各一。
        if name == "semicolon_density_per_1k":
            return _density_per_1k_chars(p.text, "；;")
        if name == "question_density_per_1k":
            return _density_per_1k_chars(p.text, "？?")
        if name == "classical_word_ratio":
            return _classical_usage_ratio(p.text)
        if name == "colloquial_marker_ratio":
            return _word_occurrence_ratio(p.text, _COLLOQUIAL_MARKERS)
        if name == "metaphor_density_per_1k":
            return _density_per_1k_words(p.text, _METAPHOR_MARKERS)
        if name == "personification_density_per_1k":
            return _density_per_1k_words(p.text, _PERSONIFICATION_MARKERS)
        # paragraph_type 比例:按段 0/1 指示
        type_metric_map = {
            "dialogue_ratio": "dialogue",
            "psychology_ratio": "psychology",
            "description_env_ratio": "description_env",
            "description_char_ratio": "description_char",
            "action_ratio": "action",
            "narration_ratio": "narration",
            "transition_ratio": "transition",
            "flashback_ratio": "flashback",
        }
        if name in type_metric_map:
            return 1.0 if p.paragraph_type == type_metric_map[name] else 0.0
        # 感官类
        for sense in ("visual", "auditory", "olfactory", "tactile", "gustatory"):
            if name == f"sensory_{sense}_per_1k":
                words = self._sensory_lexicon.get(sense, [])
                return _density_per_1k_words(p.text, words)
        raise ValueError(f"unknown metric: {name}")


def compute_prose_shape_metrics(
    paragraphs: list[ParagraphRecord],
) -> dict[str, float]:
    """计算可直接作用于生成稿的段落形状，不读取 paragraph_type。"""
    usable = [p for p in paragraphs if str(p.text or "").strip()]
    if not usable:
        return {name: 0.0 for name in PROSE_SHAPE_METRIC_NAMES}
    lengths = [_visible_length(p.text) for p in usable]
    total_chars = max(1, sum(lengths))
    return {
        "paragraph_mean_chars": statistics.fmean(lengths),
        "paragraph_length_std_chars": (
            statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
        ),
        "paragraphs_per_1k": len(usable) * 1000.0 / total_chars,
        "single_sentence_paragraph_ratio": sum(
            1 for p in usable if _paragraph_sentence_count(p.text) <= 1
        )
        / len(usable),
        "quote_led_paragraph_ratio": sum(
            1 for p in usable if _looks_like_quote_led_dialogue(p.text)
        )
        / len(usable),
    }


def compute_prose_shape_with_variance(
    paragraphs: list[ParagraphRecord],
) -> dict[str, tuple[float, float]]:
    """返回全文段落形状均值与约场景大小的块间自然波动。"""
    if not paragraphs:
        return {name: (0.0, 0.0) for name in PROSE_SHAPE_METRIC_NAMES}
    means = compute_prose_shape_metrics(paragraphs)
    chunks = _chunk_by_chars(paragraphs, _VARIANCE_CHUNK_CHARS)
    result: dict[str, tuple[float, float]] = {}
    for name in PROSE_SHAPE_METRIC_NAMES:
        values = [compute_prose_shape_metrics(chunk)[name] for chunk in chunks]
        std = statistics.pstdev(values) if len(values) > 1 else 0.0
        result[name] = (means[name], std)
    return result


def compute_prose_shape_from_text(text: str) -> dict[str, float]:
    """按真实空行切段计算生成稿形状；与隐藏评测的可解释定义对齐。"""
    parts = [
        part.strip()
        for part in re.split(r"\n\s*\n", str(text or ""))
        if part.strip()
    ]
    if not parts and str(text or "").strip():
        parts = [str(text).strip()]
    return compute_prose_shape_metrics(
        [ParagraphRecord(text=part, paragraph_type="narration") for part in parts]
    )


# 块间方差的目标块大小(字符)。≈ 一个场景的长度,与回测对照的「单段生成文本」
# 同粒度;语料按段落累积到该字数即切块,余段并入最后一块。
_VARIANCE_CHUNK_CHARS = 1500


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


# ---------------------------------------------------------------------------
# 辅助纯函数
# ---------------------------------------------------------------------------


def _avg_sentence_length(text: str) -> float:
    sentences = split_sentences(text)
    if not sentences:
        return 0.0
    return statistics.fmean(len(s) for s in sentences)


def _sentence_length_std(text: str) -> float:
    sentences = split_sentences(text)
    if len(sentences) <= 1:
        return 0.0
    return statistics.pstdev(len(s) for s in sentences)


def _sentence_length_ratio(text: str, predicate: Callable[[int], bool]) -> float:
    sentences = split_sentences(text)
    if not sentences:
        return 0.0
    matched = sum(1 for s in sentences if predicate(len(s)))
    return matched / len(sentences)


def _visible_length(text: str) -> int:
    return sum(1 for char in str(text or "") if not char.isspace())


def _looks_like_quote_led_dialogue(text: str) -> bool:
    return str(text or "").lstrip().startswith(("“", "‘", "「", "『", '"', "—"))


def _paragraph_sentence_count(text: str) -> int:
    """忽略分句后残留的闭引号，避免 ``“走吧。”`` 被算成两句。"""
    parts = [
        part
        for part in split_sentences(str(text or ""))
        if re.search(r"[\w\u3400-\u9fff]", part)
    ]
    return len(parts) or (1 if str(text or "").strip() else 0)


def _density_per_1k_chars(text: str, chars: str) -> float:
    if not text:
        return 0.0
    # set() 去重:字符集若含重复字符(历史勘误)不得双计
    count = sum(text.count(c) for c in set(chars))
    return count * 1000.0 / len(text)


def _density_per_1k_words(text: str, words: tuple[str, ...] | list[str]) -> float:
    if not text or not words:
        return 0.0
    count = sum(text.count(w) for w in words)
    return count * 1000.0 / len(text)


def _density_per_1k_pattern(text: str, pattern: str) -> float:
    if not text:
        return 0.0
    matches = re.findall(pattern, text)
    return len(matches) * 1000.0 / len(text)


def _word_occurrence_ratio(text: str, words: tuple[str, ...] | list[str]) -> float:
    """命中任一词的句子占比(用于 classical_word_ratio / colloquial_marker_ratio)。"""
    sentences = split_sentences(text)
    if not sentences:
        return 0.0
    matched = sum(1 for s in sentences if any(w in s for w in words))
    return matched / len(sentences)


def _classical_usage_hit(sentence: str) -> bool:
    """句中是否出现文言用法:句末语气词(也 / 矣 / 焉 / 哉 / 乎 / 兮 …)、「曰」、
    「…者也 / …者，」、不在现代复合词里的「之」「其」。"""
    body = sentence.strip().rstrip(_CLASSICAL_SENTENCE_TAIL_STRIP)
    core = body.rstrip(_CLASSICAL_PUNCT_STRIP).rstrip(_CLASSICAL_SENTENCE_TAIL_STRIP)
    if core and core[-1] in _CLASSICAL_FINAL_PARTICLES:
        return True
    if "曰" in body or _CLASSICAL_ZHE_RE.search(body):
        return True
    zhi = body.count("之") - sum(body.count(word) for word in _CLASSICAL_ZHI_MODERN)
    if zhi > 0:
        return True
    qi = body.count("其") - sum(body.count(word) for word in _CLASSICAL_QI_MODERN)
    return qi > 0


def _classical_usage_ratio(text: str) -> float:
    """含文言用法的句子占比(2026-09-14 起替代 _CLASSICAL_MARKERS 的字面命中)。"""
    sentences = split_sentences(text)
    if not sentences:
        return 0.0
    return sum(1 for s in sentences if _classical_usage_hit(s)) / len(sentences)
