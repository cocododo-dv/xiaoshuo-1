"""风格参考 v3 · 提示里参考的各块怎么写（从 ``inject/render.py`` 拆出；拼装与入口仍在 ``render``）。

- 文风卡（:class:`DimensionCardSource`，可按单元去掉重渲的卡，预算拟合照 ``drop_order`` 去）与 :func:`build_card_source`；
- ``[声音特征]``（:func:`voice_block`，「不学」的维的习惯句不带）与近期常见偏差的「不学」维过滤；
- 红线（:func:`red_line_block`，反抄袭模板 + 生成期禁用词，永不截断）；
- ``card_only`` 下卡句后面 ≤11 字的原话例子（:func:`card_examples` / :func:`card_example_clause`）；
- 样例窗的小工具：卫生处理（:func:`safe_reference_text`）、位置标签（:func:`window_position_tag`）、句边界截断、字数口径。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.binding_config import (
    DIMENSION_EXCLUDE,
    REFERENCE_MODE_CARD_ONLY,
)
from novel_system.services.style_reference.budget_config import injection_budget
from novel_system.services.style_reference.card import (
    EXAMPLE_UNIT_PREFIX,
    LINE_STATE_EXCLUDED,
    UNIT_GAP,
    DimensionCard,
    card_from_profile_json,
    line_states_from_profile_json,
    plan_card_block,
)
from novel_system.services.style_reference.config_loader import load_text_template
from novel_system.services.style_reference.fidelity import FEATURE_DIMENSIONS
from novel_system.services.style_reference.inject.gaps import gap_dimensions
from novel_system.services.style_reference.inject.request import (
    POSITION_CLOSING,
    POSITION_OPENING,
    POSITION_WHOLE,
    ROLE_DRAFT,
    ROLE_PLAN,
    ROLE_REVIEW,
    ROLE_REVISE,
    StyleRenderRequest,
)
from novel_system.services.style_reference.inject.selection import (
    WindowRef,
)
from novel_system.services.style_reference.untrusted_data import (
    FEW_SHOT_FRAME_END,
    NEUTRALIZED_MARK,
    frame_reference_samples,
)

logger = logging.getLogger(__name__)

# 只用文风卡时卡句后面的原话例子：至多 11 个字（界面的承诺「卡上的例子至多 11 个字」），太短的片段不当例子
CARD_EXAMPLE_MAX_CHARS = 11
CARD_EXAMPLE_MIN_CHARS = 4
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？；：、,.!?;:…—\n\r\t「」『』“”‘’\"'（）()《》〈〉【】\[\]〔〕]+")
_ESCAPED_BOUNDARY_TOKEN = "UNTRUSTED_BOUNDARY_ESCAPED"
_SENTENCE_END = "。！？!?…"
_CLOSERS = "”’」』\"'）)】"

VOICE_HEADERS: dict[str, str] = {
    ROLE_DRAFT: "[声音特征](这位作者用词、标点、对白引导与句子节奏的实际习惯；照这个手感写，不数数、不堆砌)",
    ROLE_REVISE: "[声音特征](这位作者用词、标点、对白引导与句子节奏的实际习惯；改稿时往这个手感上靠)",
    ROLE_REVIEW: "[声音特征](评审时对照：稿子的用词、标点、对白引导与句子节奏是否接近这些习惯)",
    ROLE_PLAN: "[声音特征](这位作者行文的手感，规划时心里有数即可)",
}
CARD_HEADERS: dict[str, str] = {
    ROLE_PLAN: (
        "[文风卡](这位作者与通用写法不同的地方；规划时按这里的气质与手法设想每一场的情绪、钩子与收场，"
        "设计文字的调性不改变这里的写法)"
    ),
    ROLE_REVISE: (
        "[文风卡](改稿时逐维对照：只改不像这位作者的地方，改完要更像；样例是权威，标「必须」的每场都要体现)"
    ),
}
_FALLBACK_RED_LINE = """## 严格禁止
- 复用或微改任何参考样本中的完整句子;连续 12 字以上与参考原文相同即视为抄袭
- 搬用参考样本中的人物、地名、专名、事件与情节
- 参考样本中承载象征意义的独特意象不得原样搬用;学取象的方式,象与句子都必须是你自己的
- 作者的用词习惯、句式、节奏、叙述姿态可以学、应该学;抄的是句子,学的是手法

此外,以下专有名词严禁出现在生成文本中(可能引发版权或角色混淆):
{banned_terms_list}"""
_WINDOW_POSITION_LABELS = {POSITION_OPENING: "章首", POSITION_CLOSING: "章末", POSITION_WHOLE: "整章"}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def nonspace_chars(text: str) -> int:
    """样例窗的「字数」：每个非空白字符（标点也算）——只给预算与界面用；测量核的「可见字」是 ``measure.visible_length``。"""
    return sum(1 for char in text if not char.isspace())


def letter_chars(text: str) -> int:
    return sum(1 for char in text if unicodedata.category(char)[0] in ("L", "N"))


def clip_at_sentence(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    window = text[: max_chars - 1]
    cut = -1
    for index in range(len(window) - 1, -1, -1):
        if window[index] in _SENTENCE_END:
            end = index + 1
            while end < len(window) and window[end] in _CLOSERS:
                end += 1
            cut = end
            break
    if cut < max_chars // 3:
        cut = len(window)
    return window[:cut].rstrip() + "…"


def safe_reference_text(text: str) -> str:
    """原文进提示前的卫生处理（与 ``frame_reference_samples`` 同一处理：中和注入模式、转义伪造边界），不加框。"""
    framed = frame_reference_samples(text)
    suffix = "\n" + FEW_SHOT_FRAME_END
    return framed[: -len(suffix)] if framed.endswith(suffix) else framed


def window_position_tag(ref: WindowRef) -> str:
    """样例行开头的中文位置标签：「第N章·章首」「第N章·章末」「第N章·整章」、中间窗口「第N章」。"""
    label = _WINDOW_POSITION_LABELS.get(ref.position)
    if ref.chapter > 0:
        return f"第{ref.chapter}章·{label}" if label else f"第{ref.chapter}章"
    return label or "样例"


def mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# 卡（v3 文风卡）
# ---------------------------------------------------------------------------


class CardSource:
    """可按「去掉哪些单元」重新渲染的卡。``drop_order`` 是预算不够时的去掉顺序（先去的在前）。"""

    kind = "none"
    drop_order: tuple[str, ...] = ()

    def render(self, excluded: frozenset[str] = frozenset()) -> str:  # pragma: no cover - 接口
        return ""


FIT_EXAMPLE_PREFIX = "__example__:"
FIT_GAPS_UNIT = "__gaps__"


class DimensionCardSource(CardSource):
    """v3 文风卡。卡自己的预算取舍在 ``card.plan_card_block``（钉住 / 必须的句永远带上，「作者不这么写」有保底，
    例子先于整维被去掉）；``drop_order`` 是那份保留次序倒过来——外层预算拟合（``inject.fit``）照它一个单元一个
    单元地去：先去保底之外的「作者不这么写」、多出来的句与它们的例子，再去例子，最后才去每一维的第一句（整维
    消失）、近期偏差与保底的「作者不这么写」。钉住 / 必须的句与气质不在里面——要去只能整张卡不发（M2）。"""

    kind = "card"

    def __init__(
        self,
        card: DimensionCard,
        *,
        role: str,
        dimension_states: Mapping[str, str],
        line_states: Mapping[str, str],
        recent_gaps: Sequence[str],
        budget_chars: int,
        examples: Mapping[str, str] | None = None,
    ) -> None:
        self.card = card
        self.role = role
        self.dimension_states = dict(dimension_states)
        self.line_states = dict(line_states)
        self.recent_gaps = tuple(recent_gaps)
        self.budget_chars = budget_chars
        self.examples = {str(k): str(v) for k, v in (examples or {}).items() if str(v or "").strip()}
        plan = self._plan(frozenset())
        self.example_count = plan.example_count
        order: list[str] = []
        for unit in reversed(plan.priority):
            if unit.kind == UNIT_GAP:
                unit_id = FIT_GAPS_UNIT
            elif unit.unit_id.startswith(EXAMPLE_UNIT_PREFIX):
                unit_id = FIT_EXAMPLE_PREFIX + unit.unit_id[len(EXAMPLE_UNIT_PREFIX) :]
            else:
                unit_id = unit.unit_id
            if unit_id not in order:
                order.append(unit_id)
        self.drop_order = tuple(order)

    def _plan(self, excluded: frozenset[str]):
        states = dict(self.line_states)
        examples = dict(self.examples)
        for unit_id in excluded:
            if unit_id.startswith(FIT_EXAMPLE_PREFIX):
                examples.pop(unit_id[len(FIT_EXAMPLE_PREFIX) :], None)
            elif not unit_id.startswith("__"):
                states[unit_id] = LINE_STATE_EXCLUDED
        card = self.card
        if "__temperament__" in excluded and card.temperament:
            card = card.model_copy(update={"temperament": []})
        return plan_card_block(
            card,
            dimension_states=self.dimension_states,
            line_states=states,
            recent_gaps=() if FIT_GAPS_UNIT in excluded else self.recent_gaps,
            budget_chars=self.budget_chars,
            role=ROLE_REVIEW if self.role == ROLE_REVIEW else ROLE_DRAFT,
            examples=examples,
        )

    def render(self, excluded: frozenset[str] = frozenset()) -> str:
        block = self._plan(excluded).text
        header = CARD_HEADERS.get(self.role)
        if block and header:
            _first, _sep, rest = block.partition("\n")
            block = header + ("\n" + rest if rest else "")
        return block


_POSITIVE_SECTIONS = ("[文风卡]",)
_AVOID_SECTIONS = ("[作者不这么写]",)


def count_card_lines(block: str) -> tuple[int, int]:
    """(正向条数, 「作者不这么写」条数)：``- `` 开头的条目行按所在小节归类；气质行算一条正向。"""
    positive = forbidden = 0
    section = ""
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section.startswith(_POSITIVE_SECTIONS) and (stripped.startswith("- ") or stripped.startswith("气质")):
            positive += 1
        elif section.startswith(_AVOID_SECTIONS) and stripped.startswith("- "):
            forbidden += 1
    return positive, forbidden


# ---------------------------------------------------------------------------
# 声音 / 红线 / 证据例句
# ---------------------------------------------------------------------------


# 声音习惯句 → 维度（L8：「不学」的维，声音块里它的习惯句也不带）。v3 的习惯句由
# ``voice_signature.render_voice_habits`` 按固定句式写出，先按句首认出它说的是哪个测量核特征，再按
# ``fidelity.FEATURE_DIMENSIONS`` 归维；认不出的旧式习惯句按关键词认；都认不出 → 不知道是哪一维，照带。
_HABIT_FEATURE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^句子平均"), "sent_len_mean"),
    (re.compile(r"^段落平均"), "para_len_mean"),
    (re.compile(r"^(?:几乎通篇是对白|对白约占|几乎没有对白)"), "dialogue_char_share"),
    (re.compile(r"^对白"), "dialogue_guide_none_share"),
    (re.compile(r"^句末"), "sentence_final_modal_ratio"),
    (re.compile(r"^连接"), "fw_connective_per_1k"),
    (re.compile(r"^(?:常用|几乎不用)(?:省略号|破折号|分号|感叹号|问号|冒号|顿号)"), "punct_comma_per_1k"),
    (re.compile(r"^(?:第[一二三]人称|以第三人称|叙述里常用第二人称|叙述人称)"), "person_third_share"),
    (re.compile(r"^常写具体数字"), "digit_run_per_1k"),
    (re.compile(r"英文词"), "latin_word_per_1k"),
    (re.compile(r"^常用副词"), "fw_adverb_per_1k"),
    (re.compile(r"^常把几个极短的句子"), "sent_short_run_ratio"),
    (re.compile(r"^动作后常带"), "fw_aspect_per_1k"),
    (re.compile(r"^常用四字"), "four_char_segment_per_1k"),
    (re.compile(r"^常用叠词"), "redup_total_per_1k"),
)
_HABIT_KEYWORD_DIMENSIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("对白", "引导词", "说话人"), "scene.dialogue"),
    (("逗号", "句号", "省略号", "破折号", "分号", "感叹号", "问号", "冒号", "顿号", "标点"), "language.punctuation"),
    (("人称",), "narrative.perspective"),
    (("段落", "换段", "分段"), "narrative.pacing"),
    (("具体数字", "计量"), "narrative.information_density"),
    (("语气词", "副词", "连接词", "叠词", "四字", "英文词", "文言"), "language.vocabulary"),
)


def habit_dimension(habit: str) -> str | None:
    """一句声音习惯属于哪一维（认不出 → ``None``）。"""
    text = " ".join(str(habit or "").split())
    if not text:
        return None
    for pattern, feature in _HABIT_FEATURE_PATTERNS:
        if pattern.search(text):
            return FEATURE_DIMENSIONS.get(feature)
    for keywords, dimension in _HABIT_KEYWORD_DIMENSIONS:
        if any(keyword in text for keyword in keywords):
            return dimension
    return None


def _excluded_dimensions(dimension_states: Mapping[str, str] | None) -> set[str]:
    return {str(dim) for dim, state in (dimension_states or {}).items() if state == DIMENSION_EXCLUDE}


def filter_recent_gaps(gaps: Sequence[str], dimension_states: Mapping[str, str] | None) -> tuple[str, ...]:
    """近期常见偏差去掉「不学」维的条目（L8；认不出维的短语照带）。"""
    excluded = _excluded_dimensions(dimension_states)
    if not excluded:
        return tuple(gaps)
    kept: list[str] = []
    for gap in gaps:
        dims = gap_dimensions([gap])
        if dims and dims[0] in excluded:
            continue
        kept.append(gap)
    return tuple(kept)


def voice_block(
    profile_json: Mapping[str, Any],
    role: str,
    *,
    dimension_states: Mapping[str, str] | None = None,
) -> str:
    """``[声音特征]``：画像里的具体习惯句（v3 ``voice.habits``，旧画像 ``voice_signature.habits``）；
    「不学」的维的习惯句不带（L8）。"""
    habits: Any = None
    for key in ("voice", "voice_signature"):
        candidate = mapping_or_empty(profile_json.get(key)).get("habits")
        if isinstance(candidate, list) and candidate:
            habits = candidate
            break
    excluded = _excluded_dimensions(dimension_states)
    lines: list[str] = []
    for item in habits or []:
        text = " ".join(str(item or "").split())
        if not text or f"- {text}" in lines:
            continue
        if excluded and habit_dimension(text) in excluded:
            continue
        lines.append(f"- {text}")
    if not lines:
        return ""
    return "\n".join([VOICE_HEADERS.get(role, VOICE_HEADERS[ROLE_DRAFT]), *lines])


def red_line_block(banned_terms: Sequence[str]) -> str:
    """反抄袭红线：固定模板 + 生成期禁用词（含受保护专名）；模板缺失时用内置兜底——红线不能因缺配置消失。"""
    try:
        template = load_text_template("anti_plagiarism_template")
    except FileNotFoundError:
        template = _FALLBACK_RED_LINE
    terms = sorted({str(term or "").strip() for term in banned_terms if str(term or "").strip()})
    if terms:
        terms_text = "\n".join(f"- {term}" for term in terms)
    else:
        template = template.split("此外,")[0].rstrip()
        terms_text = ""
    return template.replace("{banned_terms_list}", terms_text).strip()


def _term_key(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def card_example_clause(text: str, *, protected_terms: Sequence[str] = ()) -> str:
    """一条证据引文 → 至多 :data:`CARD_EXAMPLE_MAX_CHARS` 个字的原话例子（M3）。

    整句够短就用整句，否则取最长的一个分句（同长取靠前的）；过短（< :data:`CARD_EXAMPLE_MIN_CHARS`）或含受保护
    专名 / 禁用词的片段不用——取不出 → ``""``（这一句不挂例子）。整句先过注入中和（中和规则要看到整句上下文）：
    里面有被中和的疑似指令或被转义的伪造边界的引文，一个字都不拿来当例子（切成分句会把中和标记切碎、把指令的
    后半截留下来）。字数按可见字算。
    """
    original = " ".join(str(text or "").split())
    safe = safe_reference_text(original)
    if not safe.strip() or safe != original:
        return ""
    terms = [key for key in (_term_key(term) for term in protected_terms) if key]
    best = ""
    best_chars = 0
    for candidate in [safe, *_CLAUSE_SPLIT_RE.split(safe)]:
        candidate = candidate.strip()
        chars = nonspace_chars(candidate)
        # 上限按可见字（标点也算一个字，只会更严）；下限按字母数字（「嗯，好，走」不算一个像样的例子）
        if letter_chars(candidate) < CARD_EXAMPLE_MIN_CHARS or chars > CARD_EXAMPLE_MAX_CHARS or chars <= best_chars:
            continue
        if NEUTRALIZED_MARK in candidate or _ESCAPED_BOUNDARY_TOKEN in candidate:
            continue
        key = _term_key(candidate)
        if any(term in key for term in terms):
            continue
        best, best_chars = candidate, chars
    return best


def example_protected_terms(layer: Mapping[str, Any]) -> list[str]:
    """卡句例子不许带的词：契约冻结的生成期禁用词（含受保护专名）+ 环境变量里的全局受保护专名。"""
    terms = [str(term) for term in layer.get("banned_terms") or [] if str(term or "").strip()]
    try:
        from novel_system.services.source_safety import configured_protected_source_terms

        terms.extend(str(term) for term in configured_protected_source_terms() if str(term or "").strip())
    except Exception:  # noqa: BLE001 — 全局词表读不出来时只用画像的禁用词
        logger.debug("configured protected source terms unavailable", exc_info=True)
    return terms


def card_examples(
    session: Session,
    card: DimensionCard,
    *,
    protected_terms: Sequence[str] = (),
) -> dict[str, str]:
    """``card_only``：每条卡句至多一个 ≤11 字的原话例子（卡句的 ``evidence_quote_ids`` → 引文表 →
    :func:`card_example_clause`）；含受保护专名 / 禁用词的不用。"""
    from novel_system.services.style_reference.repository import StyleReferenceRepository

    wanted: dict[str, list[str]] = {}
    for entry in card.dimensions:
        for line in entry.lines:
            if line.kind == "do" and line.evidence_quote_ids:
                wanted[line.line_id] = [str(q) for q in line.evidence_quote_ids if str(q or "").strip()]
    quote_ids = sorted({qid for ids in wanted.values() for qid in ids})
    if not quote_ids:
        return {}
    try:
        quotes = {
            str(q.quote_id): " ".join(str(q.quote_text or "").split())
            for q in StyleReferenceRepository(session).list_quotes_by_ids(quote_ids)
        }
    except Exception:  # noqa: BLE001 — 例句只是补充
        logger.debug("card evidence quotes unavailable", exc_info=True)
        return {}
    examples: dict[str, str] = {}
    for line_id, ids in wanted.items():
        for quote_id in ids:
            example = card_example_clause(quotes.get(quote_id, ""), protected_terms=protected_terms)
            if example:
                examples[line_id] = example
                break
    return examples


def build_card_source(
    session: Session | None,
    policy: Any,
    request: StyleRenderRequest,
    profile_json: Mapping[str, Any],
    *,
    examples_allowed: bool,
    reference_mode: str | None = None,
    recent_gaps: Sequence[str] | None = None,
    protected_terms: Sequence[str] = (),
) -> CardSource | None:
    """v3 文风卡 → :class:`DimensionCardSource`；画像没有文风卡 → ``None``（没有卡替身，2026-09-24）。

    ``card_only`` 且允许送原文时给卡句挂 ≤11 字的原话例子（不含 ``protected_terms``，M3）；``recent_gaps``
    缺省用请求里的（渲染入口传去掉「不学」维之后的，L8）。"""
    card = card_from_profile_json(profile_json)
    if card is None:
        return None
    states = dict(getattr(policy, "dimension_states", None) or {})
    mode = reference_mode if reference_mode is not None else getattr(policy, "reference_mode", None)
    gaps = tuple(request.recent_gaps if recent_gaps is None else recent_gaps)
    examples: dict[str, str] = {}
    if mode == REFERENCE_MODE_CARD_ONLY and examples_allowed and session is not None:
        examples = card_examples(session, card, protected_terms=protected_terms)
    return DimensionCardSource(
        card,
        role=request.role,
        dimension_states=states,
        line_states=line_states_from_profile_json(profile_json),
        recent_gaps=gaps,
        budget_chars=injection_budget().card_budget_chars,
        examples=examples,
    )


__all__ = [
    "CARD_EXAMPLE_MAX_CHARS",
    "CARD_EXAMPLE_MIN_CHARS",
    "CARD_HEADERS",
    "CardSource",
    "DimensionCardSource",
    "FIT_EXAMPLE_PREFIX",
    "FIT_GAPS_UNIT",
    "VOICE_HEADERS",
    "build_card_source",
    "card_example_clause",
    "card_examples",
    "clip_at_sentence",
    "count_card_lines",
    "example_protected_terms",
    "filter_recent_gaps",
    "habit_dimension",
    "letter_chars",
    "mapping_or_empty",
    "nonspace_chars",
    "red_line_block",
    "safe_reference_text",
    "voice_block",
    "window_position_tag",
]
