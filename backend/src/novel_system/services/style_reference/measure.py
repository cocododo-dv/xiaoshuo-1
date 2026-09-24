"""风格参考 v3（2026-09-23）— 测量核（``measure_v1``）。

过去同一段文字有三套口径：``voice_signature`` 按全文汇总、旧 ``metrics.MetricsEngine``（v2 的 21 指标包络，
2026-09-24 随候选重排 / 形态修复一起删除）按「逐段算完再求平均」（一句话的段和一百句话的段权重相同——真实参考书上
问句密度 9.27 vs 汇总 4.8、句长标准差 7.45 vs 21.8）、段落形状又是另一套；分段规则也各不相同（空行 / 单换行 /
整场一段），虚词表两套，「字数」三种定义。本模块是唯一的测量入口，一遍算完：

- **分段**（唯一规则，与 ``manuscript_html.manuscript_paragraphs`` 同口径）：HTML（作者稿）先按
  ``p / blockquote`` 取段、剥标签；纯文本按换行切，非空行即一段。``\\n`` 与 ``\\n\\n`` 分段的同一段文字测得
  完全相同。
- **字**：可见字 = 汉字 / 拉丁字母 / 数字（不含标点与空白）。所有「每千字」、句长、段长都以它为单位。
- **句**：``text_utils.split_sentences``（闭引号归前句，丢纯标点片段）；**分句**：句内停顿（，、；：）。
- **汇总口径**：全部比率与均值都在整段文字上汇总（句长均值 = 全部句子的平均；问号密度 = 全文问号数 ÷
  全文字数），不做逐段平均。
- **虚词**：``config/style_reference/function_words.yaml`` 一张表（闭类词，独占计数：长词命中的位置不再
  记入其子串短词）。
- **对白**：引号 “” ‘’ 「」 『』 "" 内的文字（嵌套引号归外层）；对白占比 = 引号内可见字 ÷ 可见字，全系统唯一
  的对白占比定义（窗口索引、读数、声音特征共用）。

对外：``measure_text(text) -> TextMeasure``、``measure_paragraphs(items)``、``kernel_features(measure)``
（``FEATURE_NAMES`` 的全部特征，沿用旧声音特征名并追加几项）、``kernel_paragraphs(text)``；以及读数 / 窗口
典型度共用的单位下限 ``feature_scale_floor`` 与稳健中心 / 尺度 ``robust_center_scale``。
改了任何口径都要升 ``KERNEL_VERSION``（窗口索引、读数按它失效重算）。
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.text_utils import split_sentences

KERNEL_VERSION = "measure_v1"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

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
SPEECH_VERB_LABELS: dict[str, str] = {
    "shuodao": "说道",
    "shuo": "说",
    "dao": "道",
    "wen": "问",
    "da": "答",
    "other": "其他",
}
PUNCT_KEYS: tuple[str, ...] = (
    "comma",
    "enumeration",
    "period",
    "colon",
    "semicolon",
    "exclamation",
    "question",
    "ellipsis",
    "dash",
    "quote_pair",
)

# 前 52 个与 voice_signature_v1 同名同序；measure_v1 追加 5 个（对白字数占比、段长离散、数字、带单位的
# 中文数量、英文词）。
FEATURE_NAMES: tuple[str, ...] = (
    *tuple(f"fw_{group}_per_1k" for group in FUNCTION_WORD_GROUPS),
    "fw_total_per_1k",
    "sentence_final_modal_ratio",
    "sentence_final_classical_ratio",
    *tuple(f"punct_{key}_per_1k" for key in PUNCT_KEYS),
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
    # measure_v1 追加
    "dialogue_char_share",
    "para_len_std",
    "digit_run_per_1k",
    "numeral_unit_per_1k",
    "latin_word_per_1k",
)

# 句长 ≤ 此值（可见字）算短句，用于「短句连打」。
SHORT_SENTENCE_CHARS = 8
# 字级 TTR / 二元组 hapax 的窗口（字）：TTR 随长度衰减，固定窗宽才可比。
LEXICAL_WINDOW_CHARS = 1500
LEXICAL_MAX_WINDOWS = 256
# 对白引导词的检测窗（引语前后各看几个字）
GUIDE_WINDOW_CHARS = 14

_CJK = "㐀-鿿"
_NON_VISIBLE_RE = re.compile(rf"[^A-Za-z0-9{_CJK}]+")
_NON_CJK_RE = re.compile(rf"[^{_CJK}]+")
_FOUR_CHAR_RE = re.compile(rf"(?<![{_CJK}])[{_CJK}]{{4}}(?![{_CJK}])")
_AA_RE = re.compile(rf"([{_CJK}])\1")
_AABB_RE = re.compile(rf"([{_CJK}])\1([{_CJK}])\2")
_ABAB_RE = re.compile(rf"([{_CJK}])([{_CJK}])\1\2")
_ELLIPSIS_RE = re.compile(r"……|…|\.{3,}")
_DASH_RE = re.compile(r"——|—")
# 对白：“” ‘’ 「」 『』 与成对的 ASCII 双引号。‘’ 必须算：早期白话排版（黄金语料里的鲁迅）用 ‘’ 作
# 对白主引号；嵌在 “……” 里的 ‘……’ 被外层一并吃掉（finditer 自左向右、不重叠），不会重复计数。
_QUOTE_SPAN_RE = re.compile(
    r"“([^“”\n]{1,600})”|‘([^‘’\n]{1,600})’|「([^「」\n]{1,600})」|『([^『』\n]{1,600})』"
    r"|\"([^\"\n]{1,600})\""
)
_OPENING_QUOTES: tuple[str, ...] = ("“", "‘", "「", "『", '"')
# 句末「么」前面是这些字时是疑问代词 / 副词(什么、怎么、那么、这么、多么、要么),不是语气词
_MO_QUESTION_WORD_HEADS = frozenset("什怎那这多要")
_PAUSE_CHARS = "，、；：,;:"
_TRAILING_STRIP = "”’」』\"'）)】〕］ \t　。！？.!?…；;"
_PRE_WINDOW_CUT = "。！？；!?;…”’」』\n"
_POST_WINDOW_CUT = "。！？；!?;…“‘「『\"\n"
# 全部标点字符（旧 metrics.punctuation_density_per_1k 的字符集，去重后逐字计数）
PUNCT_CHARS = frozenset("。！？，；：、—…“”\"‘’「」『』《》（）()【】!?,;:.")
_DIGIT_RUN_RE = re.compile(r"[0-9０-９]+(?:[.:：][0-9０-９]+)*")
# 英文词：两个字母起（「阿Q」一类的单字母代号不算），含全角字母。
_LATIN_WORD_RE = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ]{2,}(?:['’][A-Za-z]+)?")
# 中文数字 + 计量单位（「三十秒」「八千公里」「零点五秒」「百分之七」）：只收度量衡 / 时长单位，
# 「一天」「三个」「两年」这类日常说法不算（它们不体现「写具体数值」的手法）；「斤 / 节 / 度」不收（「七斤」「一节」「一度」
# 在小说里多半是人名或「一段」）。
_NUMERAL_UNIT_RE = re.compile(
    r"(?:百分之[零〇一二三四五六七八九十百]+"
    r"|[零〇一二三四五六七八九十百千万亿两半]+(?:点[零〇一二三四五六七八九]+)?"
    r"(?:秒钟|秒|分钟|小时|个小时|钟头|毫秒|米|公里|千米|厘米|毫米|公分|英里|英尺|英寸|码|海里|"
    r"吨|公斤|千克|克|摄氏度|倍|分贝|马力|升|毫升|平方米|立方米|千瓦|伏特|赫兹|帧))"
)

# 文言 / 口语 / 比喻 / 拟人代理（旧 v2 指标的名字，指标包络已删；不进读数特征，只供声音签名的旧读者）
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
    "其下", "其前", "其左", "其右",
)
_CLASSICAL_SENTENCE_TAIL_STRIP = "”’」』\"'）)】》"
_CLASSICAL_PUNCT_STRIP = "。！？!?…；;，,：:"
_CLASSICAL_ZHE_RE = re.compile(r"者(?:也|[，,])")
_COLLOQUIAL_RE = re.compile(r"[吧呢啊嗯哎嘛哦哪呀罢嘞]")
_SIMILE_MARKERS: tuple[str, ...] = ("像", "如同", "仿佛", "犹如", "好似", "恰似", "宛如", "似的", "好像")
# 含「像」而不是比喻的常见词：参与独占计数（吸收掉其中的「像」），自身不计。
_SIMILE_BLOCKERS: tuple[str, ...] = (
    "图像", "影像", "偶像", "画像", "肖像", "录像", "想像", "塑像", "头像", "像素", "佛像", "雕像",
    "镜像", "人像", "音像", "像样",
)
_PERSONIFICATION_MARKERS: tuple[str, ...] = (
    "呜咽", "低语", "私语", "呢喃", "低吟", "怒号", "咆哮", "嘶鸣",
    "苏醒", "沉睡", "沉眠", "苏生", "翩跹", "起舞", "招手", "探头",
    "嬉戏", "依偎", "舒展", "蜷伏",
)

# HTML 作者稿的识别：出现块级 / 换行标签才按 HTML 取段（书里偶发的「<」不会触发）。
_HTML_BLOCK_TAG_RE = re.compile(r"<\s*/?\s*(?:p|br|div|blockquote|li|h[1-6]|pre)\b[^>]*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 词表（唯一的一张：function_words.yaml）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KernelLexicon:
    """从 function_words.yaml 编译出的闭类词表（进程内缓存，只读）。"""

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
    # 人称词及包含它们的更长词（「其他」含「他」）：叙述人称的独占计数只需这个闭包（包含关系可传递），长词在前
    person_closure: tuple[str, ...] = ()


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _containers_for(words: Sequence[str]) -> dict[str, tuple[str, ...]]:
    ordered = sorted(set(words), key=lambda w: (-len(w), w))
    return {
        word: tuple(longer for longer in ordered if len(longer) > len(word) and word in longer)
        for word in ordered
    }


def build_kernel_lexicon(raw: Mapping[str, Any]) -> KernelLexicon:
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
                continue  # 一词只归一组：先出现的组胜出
            seen.add(word)
            words.append(word)
        group_words[group] = tuple(words)
        labels[group] = label

    all_words = sorted(seen, key=lambda w: (-len(w), w))
    sentence_final = raw.get("sentence_final") if isinstance(raw.get("sentence_final"), Mapping) else {}
    person_raw = raw.get("person") if isinstance(raw.get("person"), Mapping) else {}
    person = {
        key: tuple(word for word in _as_str_list(person_raw.get(key)) if word in seen)
        for key in ("first", "second", "third")
    }
    speech_raw = raw.get("speech_verbs") if isinstance(raw.get("speech_verbs"), Mapping) else {}
    main_raw = speech_raw.get("main") if isinstance(speech_raw.get("main"), Mapping) else {}
    speech_main = {
        key: tuple(_as_str_list(main_raw.get(key))) for key in SPEECH_VERB_KEYS if key != "other"
    }
    speech_other = tuple(_as_str_list(speech_raw.get("other")))
    speech_exclusions = tuple(_as_str_list(speech_raw.get("exclusions")))
    all_verbs = sorted(
        {verb for verbs in speech_main.values() for verb in verbs} | set(speech_other),
        key=lambda v: (-len(v), v),
    )
    verb_pattern = "|".join(re.escape(verb) for verb in all_verbs) or r"(?!x)x"
    containers = _containers_for(all_words)
    closure: set[str] = set()
    for words_of_person in person.values():
        for word in words_of_person:
            closure.add(word)
            closure.update(containers.get(word, ()))
    return KernelLexicon(
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
        person_closure=tuple(sorted(closure, key=lambda w: (-len(w), w))),
    )


@lru_cache(maxsize=1)
def _kernel_lexicon() -> KernelLexicon:
    return build_kernel_lexicon(load_yaml_config("function_words"))


def load_kernel_lexicon() -> KernelLexicon:
    """编译后的闭类词表（缓存）。"""
    return _kernel_lexicon()


def clear_kernel_cache() -> None:
    """清空词表与大文本测量缓存（测试配合 ``config_loader.clear_config_cache`` 使用）。"""
    _kernel_lexicon.cache_clear()
    with _CACHE_LOCK:
        _LARGE_MEASURE_CACHE.clear()


@lru_cache(maxsize=1)
def _marker_containers() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    return (
        _containers_for((*_SIMILE_MARKERS, *_SIMILE_BLOCKERS)),
        _containers_for(_PERSONIFICATION_MARKERS),
    )


# ---------------------------------------------------------------------------
# 分段（唯一规则）
# ---------------------------------------------------------------------------


def kernel_paragraphs(text: str | None) -> list[str]:
    """一段文字 → 段落列表（全系统唯一的分段规则）。

    - 作者稿 HTML（含 p / br / div / blockquote … 块级标签）：按 ``manuscript_paragraphs`` 取段（与写作台
      编辑器的 ``p, blockquote`` 一一对应），段内空白压成一个空格；
    - 纯文本：统一换行后按换行切，每个非空行（strip 后）是一段——单换行与空行分段测得相同。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    if _HTML_BLOCK_TAG_RE.search(raw):
        blocks = [" ".join(str(block or "").split()) for block in manuscript_paragraphs(raw)]
        return [block for block in blocks if block]
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in raw.split("\n") if line.strip()]


# ---------------------------------------------------------------------------
# 测量结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextMeasure:
    """一段文字的全部原始计数（汇总口径的特征都由它算出）。"""

    paragraphs: tuple[str, ...] = ()
    char_count: int = 0
    paragraph_chars: tuple[int, ...] = ()
    paragraph_sentences: tuple[int, ...] = ()
    sentence_chars: tuple[int, ...] = ()
    pause_count: int = 0
    punct_counts: Mapping[str, int] = field(default_factory=dict)
    punct_total: int = 0
    word_counts: Mapping[str, int] = field(default_factory=dict)
    person_counts: Mapping[str, int] = field(default_factory=dict)
    sentence_final_counts: Mapping[str, int] = field(default_factory=dict)
    sentence_final_modal: int = 0
    sentence_final_classical: int = 0
    quote_count: int = 0
    quoted_chars: int = 0
    guide_counts: Mapping[str, int] = field(default_factory=dict)
    speech_verb_counts: Mapping[str, int] = field(default_factory=dict)
    dialogue_paragraphs: int = 0
    quote_led_paragraphs: int = 0
    four_char_segments: int = 0
    redup_aa: int = 0
    redup_aabb: int = 0
    redup_abab: int = 0
    lexical_ttr: float = 0.0
    lexical_hapax: float = 0.0
    digit_runs: int = 0
    latin_words: int = 0
    numeral_units: int = 0
    classical_sentences: int = 0
    colloquial_sentences: int = 0
    simile_markers: int = 0
    personification_markers: int = 0
    kernel_version: str = KERNEL_VERSION
    # False = 轻量测量（只有句 / 段 / 标点 / 旧指标代理，供块间方差用）；kernel_features 只接受完整测量
    detailed: bool = True

    @property
    def sentence_count(self) -> int:
        return len(self.sentence_chars)

    @property
    def paragraph_count(self) -> int:
        """有可见字的段数（纯标点行——场分隔、独占一行的省略号——不算段，但其标点照计）。"""
        return len(self.paragraph_chars)

    def group_counts(self, group: str, lexicon: KernelLexicon | None = None) -> dict[str, int]:
        """某一虚词组里每个词的独占次数（>0 的）。"""
        lex = lexicon or load_kernel_lexicon()
        return {
            word: int(self.word_counts.get(word, 0))
            for word in lex.group_words.get(group, ())
            if int(self.word_counts.get(word, 0)) > 0
        }


# ---------------------------------------------------------------------------
# 计数工具
# ---------------------------------------------------------------------------


def visible_length(text: str) -> int:
    """可见字数（汉字 / 字母 / 数字）。"""
    return len(_NON_VISIBLE_RE.sub("", str(text or "")))


def _exclusive_counts(
    text: str, words_by_length_desc: Sequence[str], containers: Mapping[str, Sequence[str]]
) -> dict[str, int]:
    """词表独占计数：长词命中的位置不再记入其子串短词（单遍 ``str.count``，C 速度）。"""
    raw = {word: text.count(word) for word in words_by_length_desc}
    exclusive: dict[str, int] = {}
    for word in words_by_length_desc:  # 长 → 短
        count = raw[word]
        for longer in containers.get(word, ()):
            count -= exclusive[longer] * longer.count(word)
        exclusive[word] = max(0, count)
    return exclusive


def _visible_sentences(paragraph: str) -> list[str]:
    return [part for part in split_sentences(paragraph) if _NON_VISIBLE_RE.sub("", part)]


def _classify_speech_verb(window: str, lexicon: KernelLexicon) -> str | None:
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
    window = paragraph[max(0, start - GUIDE_WINDOW_CHARS) : start]
    cut = max((window.rfind(char) for char in _PRE_WINDOW_CUT), default=-1)
    if cut >= 0:
        window = window[cut + 1 :]
    return window.strip()


def _cut_post_window(paragraph: str, end: int) -> str:
    window = paragraph[end : end + GUIDE_WINDOW_CHARS]
    positions = [window.find(char) for char in _POST_WINDOW_CUT]
    cuts = [position for position in positions if position >= 0]
    if cuts:
        window = window[: min(cuts)]
    return window.strip()


def quote_spans(paragraph: str) -> list[tuple[int, int, int]]:
    """段内引语跨度 [(start, end, 引号内可见字)]——全系统唯一的对白定义(嵌套引号归外层)。"""
    spans: list[tuple[int, int, int]] = []
    for match in _QUOTE_SPAN_RE.finditer(paragraph):
        inner = next((group for group in match.groups() if group is not None), "")
        spans.append((match.start(), match.end(), visible_length(inner)))
    return spans


def quoted_char_share(paragraphs: Iterable[str]) -> float:
    """引号内可见字 ÷ 可见字:与 ``kernel_features`` 的 ``dialogue_char_share`` 同一定义、同一分段规则,
    不做完整测量(结构画像这类只要这一个数的地方用)。"""
    quoted = 0
    total = 0
    for item in paragraphs or ():
        raw = str(item or "").replace("\r\n", "\n").replace("\r", "\n")
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            total += visible_length(line)
            quoted += sum(inner for _start, _end, inner in quote_spans(line))
    return _round(share(quoted, total))


def _analyze_dialogue(
    paragraph: str,
    lexicon: KernelLexicon,
    guides: Counter[str],
    verbs: Counter[str],
) -> list[tuple[int, int, int]]:
    """段内引语跨度(``quote_spans``);引导位置(pre / post / none)与引导动词记进传入的计数器。"""
    spans = quote_spans(paragraph)
    for start, end, _inner in spans:
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
    return spans


def _classical_usage_hit(sentence: str) -> bool:
    """句中是否有文言用法：句末语气词（也 / 矣 / 焉 …）、「曰」、「…者也 / …者，」、不在现代复合词里的
    「之」「其」（「也许 / 其他 / 作者 / 之后」这类现代用法不算）。"""
    body = sentence.strip().rstrip(_CLASSICAL_SENTENCE_TAIL_STRIP)
    core = body.rstrip(_CLASSICAL_PUNCT_STRIP).rstrip(_CLASSICAL_SENTENCE_TAIL_STRIP)
    if core and core[-1] in _CLASSICAL_FINAL_PARTICLES:
        return True
    if "曰" in body or _CLASSICAL_ZHE_RE.search(body):
        return True
    if "之" in body:
        zhi = body.count("之") - sum(body.count(word) for word in _CLASSICAL_ZHI_MODERN)
        if zhi > 0:
            return True
    if "其" in body:
        qi = body.count("其") - sum(body.count(word) for word in _CLASSICAL_QI_MODERN)
        if qi > 0:
            return True
    return False


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
    return [cjk_text[index * LEXICAL_WINDOW_CHARS : (index + 1) * LEXICAL_WINDOW_CHARS] for index in indices]


def _lexical_stats(cjk_text: str) -> tuple[float, float]:
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


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------


def measure_text(text: str | None) -> TextMeasure:
    """一段文字（作者稿 HTML、生成稿、参考书窗口……）→ 全部原始计数。"""
    return _measure(kernel_paragraphs(text))


def measure_paragraphs(paragraphs: Iterable[str], *, detailed: bool = True) -> TextMeasure:
    """已切好的段落（参考书段落表）→ 全部原始计数。

    每项再按换行切一次（段内换行也是段界，同一条规则）；不做 HTML 识别——段落表是纯文本。
    整本书这种大输入（≥ ``_CACHE_MIN_CHARS`` 字）的完整测量按内容哈希记忆最近两份：导入时统计、声音签名、
    块间方差先后测同一本书，只测一遍。``detailed=False`` 是块间方差用的轻量测量（不数虚词 / 人称 / 对白引导 /
    词汇丰富度），不能拿去算 ``kernel_features``。
    """
    lines: list[str] = []
    for item in paragraphs or ():
        raw = str(item or "").replace("\r\n", "\n").replace("\r", "\n")
        lines.extend(line.strip() for line in raw.split("\n") if line.strip())
    if not detailed:
        return _measure(lines, detailed=False)
    total = sum(len(line) for line in lines)
    if total < _CACHE_MIN_CHARS:
        return _measure(lines)
    key = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()
    with _CACHE_LOCK:
        cached = _LARGE_MEASURE_CACHE.get(key)
        if cached is not None:
            _LARGE_MEASURE_CACHE.move_to_end(key)
            return cached
    result = _measure(lines)
    with _CACHE_LOCK:
        _LARGE_MEASURE_CACHE[key] = result
        while len(_LARGE_MEASURE_CACHE) > _CACHE_SIZE:
            _LARGE_MEASURE_CACHE.popitem(last=False)
    return result


_CACHE_MIN_CHARS = 100_000
_CACHE_SIZE = 2
_CACHE_LOCK = threading.Lock()
_LARGE_MEASURE_CACHE: OrderedDict[str, TextMeasure] = OrderedDict()


def _measure(paragraphs: Sequence[str], *, detailed: bool = True) -> TextMeasure:
    if not paragraphs:
        return TextMeasure(detailed=detailed)
    lexicon = load_kernel_lexicon()
    joined = "\n".join(paragraphs)
    char_count = visible_length(joined)
    if char_count == 0:
        return TextMeasure(paragraphs=tuple(paragraphs), detailed=detailed)

    punct_counts = {
        "comma": joined.count("，") + joined.count(","),
        "enumeration": joined.count("、"),
        "period": joined.count("。"),
        "colon": joined.count("：") + joined.count(":"),
        "semicolon": joined.count("；") + joined.count(";"),
        "exclamation": joined.count("！") + joined.count("!"),
        "question": joined.count("？") + joined.count("?"),
        "ellipsis": len(_ELLIPSIS_RE.findall(joined)),
        "dash": len(_DASH_RE.findall(joined)),
        # 对白引号对数（与对白跨度同一组引号：“ ‘ 「 『 与成对的 "）
        "quote_pair": joined.count("“") + joined.count("‘") + joined.count("「") + joined.count("『")
        + joined.count('"') // 2,
    }
    punct_total = sum(joined.count(char) for char in PUNCT_CHARS)
    pause_count = sum(joined.count(char) for char in _PAUSE_CHARS)
    simile_containers, personification_containers = _marker_containers()
    simile_counts = _exclusive_counts(joined, tuple(simile_containers), simile_containers)
    personification_counts = _exclusive_counts(
        joined, tuple(personification_containers), personification_containers
    )

    paragraph_chars: list[int] = []
    paragraph_sentences: list[int] = []
    sentence_chars: list[int] = []
    final_counter: Counter[str] = Counter()
    final_modal = 0
    final_classical = 0
    classical_sentences = 0
    colloquial_sentences = 0
    guide_counter: Counter[str] = Counter()
    verb_counter: Counter[str] = Counter()
    quote_count = 0
    quoted_chars = 0
    dialogue_paragraphs = 0
    quote_led = 0
    narration_parts: list[str] = []
    for paragraph in paragraphs:
        visible = visible_length(paragraph)
        if detailed:
            spans = _analyze_dialogue(paragraph, lexicon, guide_counter, verb_counter)
            quote_count += len(spans)
            paragraph_quoted = sum(inner for _start, _end, inner in spans)
            quoted_chars += paragraph_quoted
            if spans:
                pieces: list[str] = []
                cursor = 0
                for start, end, _inner in spans:
                    pieces.append(paragraph[cursor:start])
                    cursor = end
                pieces.append(paragraph[cursor:])
                narration_parts.append("".join(pieces))
            else:
                narration_parts.append(paragraph)
        else:
            spans = []
            paragraph_quoted = 0
        if visible == 0:
            continue
        paragraph_chars.append(visible)
        sentences = _visible_sentences(paragraph)
        paragraph_sentences.append(len(sentences))
        for sentence in sentences:
            sentence_chars.append(visible_length(sentence))
            tail = sentence.rstrip(_TRAILING_STRIP)
            last = tail[-1:] if tail else ""
            if last == "么" and tail[-2:-1] in _MO_QUESTION_WORD_HEADS:
                last = ""  # 「什么 / 怎么 / 那么」收尾的问句:疑问代词,不是句末语气词
            if last in lexicon.sentence_final_modal:
                final_modal += 1
                final_counter[last] += 1
            elif last in lexicon.sentence_final_classical:
                final_classical += 1
                final_counter[last] += 1
            if _classical_usage_hit(sentence):
                classical_sentences += 1
            if _COLLOQUIAL_RE.search(sentence):
                colloquial_sentences += 1
        if paragraph.lstrip().startswith(_OPENING_QUOTES):
            quote_led += 1
            dialogue_paragraphs += 1
        elif spans and paragraph_quoted * 2 >= visible:
            dialogue_paragraphs += 1

    light = TextMeasure(
        paragraphs=tuple(paragraphs),
        char_count=char_count,
        paragraph_chars=tuple(paragraph_chars),
        paragraph_sentences=tuple(paragraph_sentences),
        sentence_chars=tuple(sentence_chars),
        pause_count=pause_count,
        punct_counts=punct_counts,
        punct_total=punct_total,
        sentence_final_counts=dict(final_counter),
        sentence_final_modal=final_modal,
        sentence_final_classical=final_classical,
        quote_led_paragraphs=quote_led,
        classical_sentences=classical_sentences,
        colloquial_sentences=colloquial_sentences,
        simile_markers=sum(simile_counts.get(word, 0) for word in _SIMILE_MARKERS),
        personification_markers=sum(personification_counts.values()),
        detailed=False,
    )
    if not detailed:
        return light

    word_counts = _exclusive_counts(joined, lexicon.words_by_length_desc, lexicon.containers)
    four_char = len(_FOUR_CHAR_RE.findall(joined))
    aabb = sum(1 for match in _AABB_RE.finditer(joined) if match.group(1) != match.group(2))
    abab = sum(1 for match in _ABAB_RE.finditer(joined) if match.group(1) != match.group(2))
    aa = max(0, len(_AA_RE.findall(joined)) - 2 * aabb)
    # 人称只看叙述：剥掉引号内的对白再数（对白占六成的第三人称小说里「我 / 你」几乎都是人物在说话）
    narration = "\n".join(narration_parts)
    narration_counts = (
        _exclusive_counts(narration, lexicon.person_closure, lexicon.containers)
        if narration.strip()
        else word_counts
    )
    person_counts = {
        key: sum(narration_counts.get(word, 0) for word in words) for key, words in lexicon.person.items()
    }
    ttr, hapax = _lexical_stats(_NON_CJK_RE.sub("", joined))
    return replace(
        light,
        word_counts={word: count for word, count in word_counts.items() if count > 0},
        person_counts=person_counts,
        quote_count=quote_count,
        quoted_chars=quoted_chars,
        guide_counts={key: int(guide_counter.get(key, 0)) for key in ("pre", "post", "none")},
        speech_verb_counts={key: int(verb_counter.get(key, 0)) for key in SPEECH_VERB_KEYS},
        dialogue_paragraphs=dialogue_paragraphs,
        four_char_segments=four_char,
        redup_aa=aa,
        redup_aabb=aabb,
        redup_abab=abab,
        lexical_ttr=ttr,
        lexical_hapax=hapax,
        digit_runs=len(_DIGIT_RUN_RE.findall(joined)),
        latin_words=len(_LATIN_WORD_RE.findall(joined)),
        numeral_units=len(_NUMERAL_UNIT_RE.findall(joined)),
        detailed=True,
    )


# ---------------------------------------------------------------------------
# 特征
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _round(value: float) -> float:
    return round(_finite(value), 6)


def per_1k(count: float, chars: int) -> float:
    return count * 1000.0 / chars if chars > 0 else 0.0


def share(count: float, total: float) -> float:
    return count / total if total > 0 else 0.0


def quantile(values: Sequence[float], ratio: float) -> float:
    """线性插值分位数（与旧 voice_signature / structure 同口径）。"""
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
        covariance = sum((lengths[i] - mean) * (lengths[i + 1] - mean) for i in range(count - 1))
        autocorr = covariance / variance if variance > 0 else 0.0
    return {
        "sent_len_mean": mean,
        "sent_len_std": std,
        "sent_len_p10": quantile(lengths, 0.10),
        "sent_len_p90": quantile(lengths, 0.90),
        "sent_short_run_mean": statistics.fmean(runs) if runs else 0.0,
        "sent_short_run_ratio": sum(runs) / count,
        "sent_len_lag1_autocorr": autocorr,
    }


def kernel_features(measure: TextMeasure) -> dict[str, float]:
    """``FEATURE_NAMES`` 全部特征（有限 float，按名字顺序；空文本全 0）。"""
    if measure.char_count <= 0:
        return {name: 0.0 for name in FEATURE_NAMES}
    if not measure.detailed:
        raise ValueError("kernel_features needs a detailed measure (measure_text / measure_paragraphs)")
    lexicon = load_kernel_lexicon()
    chars = measure.char_count
    features: dict[str, float] = {}
    fw_total = 0
    for group in FUNCTION_WORD_GROUPS:
        group_total = sum(int(measure.word_counts.get(word, 0)) for word in lexicon.group_words.get(group, ()))
        fw_total += group_total
        features[f"fw_{group}_per_1k"] = per_1k(group_total, chars)
    features["fw_total_per_1k"] = per_1k(fw_total, chars)

    sentence_count = measure.sentence_count
    features["sentence_final_modal_ratio"] = share(measure.sentence_final_modal, sentence_count)
    features["sentence_final_classical_ratio"] = share(measure.sentence_final_classical, sentence_count)
    for key in PUNCT_KEYS:
        features[f"punct_{key}_per_1k"] = per_1k(measure.punct_counts.get(key, 0), chars)

    features.update(_sentence_sequence_features(measure.sentence_chars))
    features["sent_pauses_mean"] = share(measure.pause_count, sentence_count)
    features["clause_len_mean"] = share(chars, sentence_count + measure.pause_count)

    paragraph_count = measure.paragraph_count
    features["para_len_mean"] = statistics.fmean(measure.paragraph_chars) if paragraph_count else 0.0
    features["para_len_std"] = statistics.pstdev(measure.paragraph_chars) if paragraph_count > 1 else 0.0
    features["para_single_sentence_ratio"] = share(
        sum(1 for count in measure.paragraph_sentences if count <= 1), paragraph_count
    )
    features["para_dialogue_ratio"] = share(measure.dialogue_paragraphs, paragraph_count)

    for placement in ("pre", "post", "none"):
        features[f"dialogue_guide_{placement}_share"] = share(
            measure.guide_counts.get(placement, 0), measure.quote_count
        )
    verb_total = sum(measure.speech_verb_counts.values())
    for key in SPEECH_VERB_KEYS:
        features[f"speech_verb_{key}_share"] = share(measure.speech_verb_counts.get(key, 0), verb_total)

    features["four_char_segment_per_1k"] = per_1k(measure.four_char_segments, chars)
    features["redup_aa_per_1k"] = per_1k(measure.redup_aa, chars)
    features["redup_aabb_per_1k"] = per_1k(measure.redup_aabb, chars)
    features["redup_abab_per_1k"] = per_1k(measure.redup_abab, chars)
    features["redup_total_per_1k"] = per_1k(measure.redup_aa + measure.redup_aabb + measure.redup_abab, chars)

    person_total = sum(measure.person_counts.values())
    for key in ("first", "second", "third"):
        features[f"person_{key}_share"] = share(measure.person_counts.get(key, 0), person_total)

    features["lexical_char_ttr"] = measure.lexical_ttr
    features["lexical_bigram_hapax_ratio"] = measure.lexical_hapax

    features["dialogue_char_share"] = share(measure.quoted_chars, chars)
    features["digit_run_per_1k"] = per_1k(measure.digit_runs, chars)
    features["numeral_unit_per_1k"] = per_1k(measure.numeral_units, chars)
    features["latin_word_per_1k"] = per_1k(measure.latin_words, chars)
    return {name: _round(features.get(name, 0.0)) for name in FEATURE_NAMES}


def text_features(text: str | None) -> dict[str, float]:
    """``kernel_features(measure_text(text))`` 的简写。"""
    return kernel_features(measure_text(text))


def dialogue_char_share(measure: TextMeasure) -> float:
    """唯一的对白占比：引号内可见字 ÷ 可见字。"""
    return _round(share(measure.quoted_chars, measure.char_count))


# ---------------------------------------------------------------------------
# 单位下限与稳健尺度（读数与窗口典型度共用）
# ---------------------------------------------------------------------------

# 特征的单位决定「差一点」有多大：每千字的频率差一次出现（约每三千字一处）、比例差两个百分点、
# 长度差半个字都只是分辨率以内的抖动。作者在书里几乎从不用的东西（尺度≈0）因此不会把 z 值炸飞。
_RATE_FLOOR = 0.3
_SHARE_FLOOR = 0.02
_LENGTH_FLOOR = 0.5
_COUNT_FLOOR = 0.1
_LEXICAL_FLOOR = 0.01
_CORR_FLOOR = 0.05
_RELATIVE_FLOOR = 0.05
# MAD → 正态标准差；p10–p90 → 标准差（正态下 p90 − p10 = 2.5631σ）
MAD_TO_SIGMA = 1.4826
P10_P90_TO_SIGMA = 2.5631


def feature_kind(name: str) -> str:
    """rate（每千字）/ share（0–1 比例）/ length（字）/ count（每句次数）/ lexical / corr。"""
    if name.endswith("_per_1k"):
        return "rate"
    if name.startswith("lexical_"):
        return "lexical"
    if name == "sent_len_lag1_autocorr":
        return "corr"
    if name.endswith(("_share", "_ratio")):
        return "share"
    if name in ("sent_pauses_mean", "sent_short_run_mean"):
        return "count"
    return "length"


def feature_scale_floor(name: str, center: float) -> float:
    """特征尺度的下限：单位分辨率与中心值 5% 取大（z 值分母永不趋零）。"""
    kind = feature_kind(name)
    base = {
        "rate": _RATE_FLOOR,
        "share": _SHARE_FLOOR,
        "length": _LENGTH_FLOOR,
        "count": _COUNT_FLOOR,
        "lexical": _LEXICAL_FLOOR,
        "corr": _CORR_FLOOR,
    }[kind]
    relative = 0.02 if kind == "lexical" else _RELATIVE_FLOOR
    return max(base, relative * abs(_finite(center)))


def robust_scale(name: str, center: float, mad: float, p10: float, p90: float) -> float:
    """尺度 = max(1.4826·MAD, (p90 − p10)/2.5631, 单位下限)。

    MAD 对长尾稳健；一半以上窗口为 0 的稀有特征 MAD 为 0，此时由 p10–p90 的跨度兜住；两者都为 0
    （作者几乎从不用）时落到单位下限。
    """
    return max(
        MAD_TO_SIGMA * max(0.0, _finite(mad)),
        max(0.0, _finite(p90) - _finite(p10)) / P10_P90_TO_SIGMA,
        feature_scale_floor(name, center),
    )


def robust_center_scale(values: Sequence[float], name: str) -> tuple[float, float]:
    """一组窗口上某特征的稳健中心（中位数）与尺度（见 :func:`robust_scale`）。"""
    cleaned = [_finite(value) for value in values]
    if not cleaned:
        return 0.0, feature_scale_floor(name, 0.0)
    center = statistics.median(cleaned)
    mad = statistics.median([abs(value - center) for value in cleaned])
    return center, robust_scale(name, center, mad, quantile(cleaned, 0.10), quantile(cleaned, 0.90))


__all__ = [
    "FEATURE_NAMES",
    "FUNCTION_WORD_GROUPS",
    "GUIDE_WINDOW_CHARS",
    "KERNEL_VERSION",
    "KernelLexicon",
    "LEXICAL_MAX_WINDOWS",
    "LEXICAL_WINDOW_CHARS",
    "MAD_TO_SIGMA",
    "P10_P90_TO_SIGMA",
    "PUNCT_CHARS",
    "PUNCT_KEYS",
    "SHORT_SENTENCE_CHARS",
    "SPEECH_VERB_KEYS",
    "SPEECH_VERB_LABELS",
    "TextMeasure",
    "build_kernel_lexicon",
    "clear_kernel_cache",
    "dialogue_char_share",
    "feature_kind",
    "feature_scale_floor",
    "kernel_features",
    "kernel_paragraphs",
    "load_kernel_lexicon",
    "measure_paragraphs",
    "measure_text",
    "per_1k",
    "quantile",
    "quote_spans",
    "quoted_char_share",
    "robust_center_scale",
    "robust_scale",
    "share",
    "text_features",
    "visible_length",
]
