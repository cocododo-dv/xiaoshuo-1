"""风格参考 v3（2026-09-23）— 把一份风格策略渲染成提示里的参考（``render_style``）。

一份 :class:`~novel_system.services.style_policy.StylePolicy`（冻结契约或现解析的契约）+ 一个
:class:`~novel_system.services.style_reference.inject.request.StyleRenderRequest` → :class:`RenderedStyle`
（system 前缀、user 尾块、实际用到的窗、读数、不含正文的审计）。

**参考方式说到做到**（N8 / J8，``policy.reference_mode``）：

- ``full``：文风卡（或旧画像的卡替身）+ 声音 + 本场冻结的样例窗 + 红线；
- ``samples_only``：样例窗 + 红线；
- ``card_only``：文风卡 + 声音 + 每条卡句至多一句 ≤60 字的证据例句 + 红线，**不送窗口**（``segments_only``
  的书由 ``effective_reference_mode`` 强制走这里）。

**按角色的口径**（J16）：起草 / 改稿把样例放在 user 消息末尾、紧挨输出（收口指令 + 章首 / 章末补充），system
里留文风卡、声音、红线与一句指路；评审 / 规划的样例留在 system 里，标题是评审 / 规划的口径，不是「写本场时以
这些片段的手笔为准」。

**旧画像**（还没有 ``dimension_card``——学习作业跑之前的所有画像）：旧的正向特征 / 叙事模式 / 偏离校准与禁忌
陈述渲染成简单的 ``[正向风格特征]`` / ``[禁忌模式]`` 替身，**含数字的行整行不要**（不再有量化软化机器，J11），
审计记 ``legacy_profile: true``。

**红线**：反抄袭模板 + 画像的生成期禁用词（含学习作业自动登记的受保护专名 ``source="protected_auto"``），
只要有任一块参考就随注、永不截断。

书被改过（冻结契约的段落根哈希 ≠ 当前窗口索引的根哈希）：按当前索引渲染并在审计里记
``STYLE_REFERENCE_BOOK_CHANGED``——不再悄悄砍掉 87% 的样例（J6）。同一 (契约, 场景, 角色, 窗数, 参考方式,
近期偏差……) 的渲染结果进程内缓存（J1），一场的几道工序不重复渲染。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook
from novel_system.services.style_reference.binding_config import (
    DIMENSION_EXCLUDE,
    REFERENCE_MODE_CARD_ONLY,
)
from novel_system.services.style_reference.card import (
    DEFAULT_CARD_BUDGET_CHARS,
    LINE_STATE_EXCLUDED,
    LINE_STATE_PINNED,
    DimensionCard,
    card_from_profile_json,
    line_states_from_profile_json,
    render_card_block,
)
from novel_system.services.style_reference.config_loader import (
    load_optional_yaml_config,
    load_text_template,
)
from novel_system.services.style_reference.inject.audit import build_audit, block_digest
from novel_system.services.style_reference.inject.request import (
    PLACEMENT_USER_TAIL,
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
    EMPTY_SELECTION,
    SceneSelection,
    WindowRef,
    resolve_scene_selection,
    role_windows,
)
from novel_system.services.style_reference.policy import cloud_llm_allowed
from novel_system.services.style_reference.schemas import (
    FEW_SHOT_CLOSING_MANDATE,
    FEW_SHOT_CLOSING_MANDATE_FINAL,
    FEW_SHOT_IN_USER_MESSAGE_NOTE,
)
from novel_system.services.style_reference.structure import chapter_boundary_habits
from novel_system.services.style_reference.untrusted_data import (
    FEW_SHOT_FRAME_END,
    frame_reference_samples,
)
from novel_system.services.style_reference.windows import marker_is_current, window_texts

logger = logging.getLogger(__name__)

NOTICE_BOOK_CHANGED = "STYLE_REFERENCE_BOOK_CHANGED"
NOTICE_SAMPLES_BLOCKED = "STYLE_REFERENCE_SAMPLES_BLOCKED"

STYLE_REFERENCE_OPEN = "[STYLE_REFERENCE]\n"
STYLE_REFERENCE_CLOSE = "\n[/STYLE_REFERENCE]\n\n"

# 单窗正文上限（切窗规则 ≤4,000 字，短尾窗并入时可放宽 25%；超长的单段窗在句边界截断）
SAMPLE_WINDOW_MAX_CHARS = 5000
CARD_EXAMPLE_MAX_CHARS = 60
_DIGIT_RE = re.compile(r"[0-9０-９]")
_SENTENCE_END = "。！？!?…"
_CLOSERS = "”’」』\"'）)】"

# ---------------------------------------------------------------------------
# 标题 / 收口（按角色）
# ---------------------------------------------------------------------------

SAMPLE_HEADERS: dict[str, str] = {
    ROLE_DRAFT: (
        "[风格样例](以下是参考作者的原文片段，按原书顺序排列，是本场唯一的文风权威。"
        "写本场时以这些片段的手笔为准：用它的用词习惯与口头禅、意象取向、句式长短与停顿、"
        "叙述姿态与旁白口吻、对白的写法与换段来写，敢于用这位作者会用的词和他会打的比方；"
        "人物、地名、事件与专名一律用本书的，不用样例里的；不整句照搬样例；样例长度不代表输出长度)"
    ),
    ROLE_REVISE: (
        "[风格样例](以下是参考作者的原文片段，按原书顺序排列，是这次修改唯一的文风权威。"
        "改稿时以这些片段的手笔为准：只改不像这位作者的地方——用词与口头禅、意象取向、句式长短与停顿、"
        "叙述姿态与旁白口吻、对白的写法与换段——已经像的句子原样留下；"
        "人物、地名、事件与专名一律用本书的，不用样例里的；不整句照搬样例)"
    ),
    ROLE_REVIEW: (
        "[风格样例](以下是参考作者的原文片段，按原书顺序排列，是这次评审的标准。"
        "逐维对照稿子与这些片段：用词与口头禅、意象取向、句式长短与停顿、叙述姿态与旁白口吻、对白写法与换段"
        "像不像这位作者；只按参考判，不拿通用的写作规范或本系统的偏好来要求稿子)"
    ),
    ROLE_PLAN: (
        "[风格样例](以下是参考作者的原文片段，按原书顺序排列：看他怎样开场、推进、放出信息、安排对白与收场。"
        "规划时按这位作者的方式设想场面与节奏；只学手法，不复用样例里的人物、地名、事件与句子)"
    ),
}
FEW_SHOT_CLOSING_MANDATE_REVISE = (
    "以上 [风格样例] 是这次修改唯一的文风权威。现在按前文的要求改这一场，改完要比原稿更像这位作者："
    "用词与口头禅、意象取向、句式长短与停顿、叙述姿态与旁白的口吻、对白的写法与换段都照样例来，"
    "已经像的地方不要动；人物、地名、事件与专名一律用本书的，不用样例里的；"
    "不整句照搬样例（连续 12 字以上与样例相同即视为照搬）。"
    + FEW_SHOT_CLOSING_MANDATE_FINAL
)
CLOSING_MANDATES: dict[str, str] = {
    ROLE_DRAFT: FEW_SHOT_CLOSING_MANDATE,
    ROLE_REVISE: FEW_SHOT_CLOSING_MANDATE_REVISE,
}
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
LEGACY_POSITIVE_HEADERS: dict[str, str] = {
    ROLE_DRAFT: "[正向风格特征](这位作者的写法；样例是权威，这里帮你不漏掉)",
    ROLE_REVISE: "[正向风格特征](改稿时对照这些写法，只改不像的地方)",
    ROLE_REVIEW: "[正向风格特征](评审时对照：稿子有没有写出这些手法)",
    ROLE_PLAN: "[正向风格特征](规划时按这些手法设想场面、情绪与收场)",
}
LEGACY_FORBIDDEN_HEADERS: dict[str, str] = {
    ROLE_DRAFT: "[禁忌模式](这位作者不这么写)",
    ROLE_REVISE: "[禁忌模式](这位作者不这么写)",
    ROLE_REVIEW: "[禁忌模式](评审时对照：稿子有没有落进这些模式)",
    ROLE_PLAN: "[禁忌模式](这位作者不这么写)",
}
RECENT_GAPS_HEADER = "[近期常见偏差](前几场草稿里反复出现的不像之处，这一场特别注意)"
_FALLBACK_RED_LINE = """## 严格禁止
- 复用或微改任何参考样本中的完整句子;连续 12 字以上与参考原文相同即视为抄袭
- 搬用参考样本中的人物、地名、专名、事件与情节
- 参考样本中承载象征意义的独特意象不得原样搬用;学取象的方式,象与句子都必须是你自己的
- 作者的用词习惯、句式、节奏、叙述姿态可以学、应该学;抄的是句子,学的是手法

此外,以下专有名词严禁出现在生成文本中(可能引发版权或角色混淆):
{banned_terms_list}"""
_WINDOW_POSITION_LABELS = {POSITION_OPENING: "章首", POSITION_CLOSING: "章末", POSITION_WHOLE: "整章"}


def _budget() -> dict[str, Any]:
    try:
        return load_optional_yaml_config("injection_budget")
    except Exception:  # noqa: BLE001 — 坏配置按默认值
        return {}


def _budget_int(name: str, default: int) -> int:
    try:
        return max(0, int(_budget().get(name, default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 章首 / 章末补充（从 style_prompt_injection 搬来；那里继续重导出）
# ---------------------------------------------------------------------------


def chapter_position_mandate(position: str | None, contract: Mapping[str, Any] | None) -> str:
    """章首 / 章末场的收口补充——开章 / 收章照样例里标着「章首」「章末」的窗口，以及参考作者开章 / 收章最常用
    的段型（结构画像）；中间场返回空串。"""
    if position not in (POSITION_OPENING, POSITION_CLOSING, POSITION_WHOLE):
        return ""
    card = None
    layers = contract.get("layers") if isinstance(contract, Mapping) else None
    if isinstance(layers, list) and layers and isinstance(layers[-1], Mapping):
        profile = layers[-1].get("profile") if isinstance(layers[-1].get("profile"), Mapping) else {}
        profile_json = profile.get("profile_json") if isinstance(profile.get("profile_json"), Mapping) else {}
        card = profile_json.get("structure_card") if isinstance(profile_json.get("structure_card"), Mapping) else None
    habits = chapter_boundary_habits(card)
    parts: list[str] = []
    if position in (POSITION_OPENING, POSITION_WHOLE):
        habit = f"（这位作者的章多以{habits['opening']}起手）" if habits.get("opening") else ""
        parts.append(
            "本场是本章的第一场：怎样开章，照样例里标着「章首」的窗口来"
            f"{habit}，用这位作者开章的方式起手，不用总结式或交代式的开头。"
        )
    if position in (POSITION_CLOSING, POSITION_WHOLE):
        habit = f"（这位作者的章多以{habits['closing']}收束）" if habits.get("closing") else ""
        parts.append(
            "本场是本章的最后一场：怎样收章，照样例里标着「章末」的窗口来"
            f"{habit}，收在场景结构定下的那一拍上，用这位作者收章的方式收束。"
        )
    return "".join(parts)


def attach_chapter_position_mandate(user_tail: str, mandate: str) -> str:
    """把开章 / 收章补充插在收口指令最后一句（篇幅 / 只返回 JSON）之前；没有那一句就接在尾巴末尾。"""
    if not mandate or not user_tail:
        return user_tail
    if FEW_SHOT_CLOSING_MANDATE_FINAL in user_tail:
        head, _sep, rest = user_tail.rpartition(FEW_SHOT_CLOSING_MANDATE_FINAL)
        return head + mandate + FEW_SHOT_CLOSING_MANDATE_FINAL + rest
    return user_tail.rstrip("\n") + "\n" + mandate + "\n"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _visible_chars(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _clip_at_sentence(text: str, max_chars: int) -> str:
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


def _has_digit(text: str) -> bool:
    return bool(_DIGIT_RE.search(text))


def _layer(contract: Mapping[str, Any] | None) -> Mapping[str, Any]:
    layers = contract.get("layers") if isinstance(contract, Mapping) else None
    if isinstance(layers, list) and layers and isinstance(layers[-1], Mapping):
        return layers[-1]
    return {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------------------
# 卡（v3 文风卡 / 旧画像替身）
# ---------------------------------------------------------------------------


class CardSource:
    """可按「去掉哪些单元」重新渲染的卡。``drop_order`` 是预算不够时的去掉顺序（先去的在前）。"""

    kind = "none"
    drop_order: tuple[str, ...] = ()

    def render(self, excluded: frozenset[str] = frozenset()) -> str:  # pragma: no cover - 接口
        return ""


class DimensionCardSource(CardSource):
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
        if examples:
            dimensions = []
            for entry in card.dimensions:
                lines = [
                    line.model_copy(update={"text": f"{line.text}（例：「{examples[line.line_id]}」）"})
                    if line.kind == "do" and line.line_id in examples
                    else line
                    for line in entry.lines
                ]
                dimensions.append(entry.model_copy(update={"lines": lines}))
            card = card.model_copy(update={"dimensions": dimensions})
        self.card = card
        self.role = role
        self.dimension_states = dict(dimension_states)
        self.line_states = dict(line_states)
        self.recent_gaps = tuple(recent_gaps)
        self.budget_chars = budget_chars
        self.example_count = len(examples or {})
        # 预算不够时：先去辨识度最低的非必须、未钉住的句，再去必须 / 钉住的，再去近期偏差，最后去气质
        ranked = sorted(
            (
                (
                    line.mandatory or self.line_states.get(line.line_id) == LINE_STATE_PINNED,
                    line.distinctiveness,
                    entry.distinctiveness,
                    line.line_id,
                )
                for entry in card.dimensions
                if self.dimension_states.get(entry.dimension) != DIMENSION_EXCLUDE
                for line in entry.lines
                if self.line_states.get(line.line_id) != LINE_STATE_EXCLUDED
            )
        )
        order = [item[3] for item in ranked]
        if self.recent_gaps:
            order.append("__gaps__")
        if card.temperament:
            order.append("__temperament__")
        self.drop_order = tuple(order)

    def render(self, excluded: frozenset[str] = frozenset()) -> str:
        states = dict(self.line_states)
        for line_id in excluded:
            if not line_id.startswith("__"):
                states[line_id] = LINE_STATE_EXCLUDED
        card = self.card
        if "__temperament__" in excluded and card.temperament:
            card = card.model_copy(update={"temperament": []})
        block = render_card_block(
            card,
            dimension_states=self.dimension_states,
            line_states=states,
            recent_gaps=() if "__gaps__" in excluded else self.recent_gaps,
            budget_chars=self.budget_chars,
            role=ROLE_REVIEW if self.role == ROLE_REVIEW else ROLE_DRAFT,
        )
        header = CARD_HEADERS.get(self.role)
        if block and header:
            _first, _sep, rest = block.partition("\n")
            block = header + ("\n" + rest if rest else "")
        return block


class LegacyCardSource(CardSource):
    """旧画像（没有 dimension_card）的卡替身：正向特征 / 叙事模式 / 偏离校准 + 禁忌陈述；含数字的行整行不要。"""

    kind = "legacy"

    def __init__(
        self,
        profile_json: Mapping[str, Any],
        *,
        forbidden_findings: Sequence[Mapping[str, Any]],
        dimension_states: Mapping[str, str],
        role: str,
        recent_gaps: Sequence[str],
    ) -> None:
        self.role = role
        self.dropped_digit_lines = 0

        def _clean(values: Any) -> list[str]:
            out: list[str] = []
            for value in values if isinstance(values, (list, tuple)) else []:
                text = " ".join(str(value or "").split())
                if not text:
                    continue
                if _has_digit(text):
                    self.dropped_digit_lines += 1
                    continue
                if text not in out:
                    out.append(text)
            return out

        summary = " ".join(str(profile_json.get("qualitative_summary") or "").split())
        if summary and _has_digit(summary):
            self.dropped_digit_lines += 1
            summary = ""
        self.summary = summary
        features = _clean(profile_json.get("style_features"))
        patterns = _clean(profile_json.get("narrative_patterns"))
        calibration = _clean(profile_json.get("calibration_guidance"))
        positive: list[tuple[str, str]] = []
        for index in range(max(len(features), len(patterns), len(calibration))):
            if index < len(features):
                positive.append((f"pos:{len(positive)}", f"- [表达机制] {features[index]}"))
            if index < len(patterns):
                positive.append((f"pos:{len(positive)}", f"- [叙事机制] {patterns[index]}"))
            if index < len(calibration):
                positive.append((f"pos:{len(positive)}", f"- [偏离校准] {calibration[index]}"))
        states = dict(dimension_states)
        forbidden_texts = _clean(profile_json.get("banned_replication_rules"))
        finding_texts = _clean(
            [
                item.get("statement")
                for item in forbidden_findings
                if isinstance(item, Mapping)
                and str(item.get("status") or "") != "rejected"
                and states.get(str(item.get("sub_dimension") or "")) != DIMENSION_EXCLUDE
            ]
        )
        for text in finding_texts:
            if text not in forbidden_texts:
                forbidden_texts.append(text)
        self.positive = positive
        self.forbidden = [(f"neg:{i}", f"- {text}") for i, text in enumerate(forbidden_texts)]
        self.gaps = [(f"gap:{i}", f"- {text}") for i, text in enumerate(recent_gaps)]
        order = [uid for uid, _line in reversed(self.positive)]
        order.extend(uid for uid, _line in reversed(self.forbidden))
        if self.summary:
            order.append("summary")
        order.extend(uid for uid, _line in reversed(self.gaps))
        self.drop_order = tuple(order)

    @property
    def is_empty(self) -> bool:
        return not (self.summary or self.positive or self.forbidden)

    def render(self, excluded: frozenset[str] = frozenset()) -> str:
        sections: list[str] = []
        positive = [line for uid, line in self.positive if uid not in excluded]
        summary = self.summary if self.summary and "summary" not in excluded else ""
        if summary or positive:
            lines = [LEGACY_POSITIVE_HEADERS.get(self.role, LEGACY_POSITIVE_HEADERS[ROLE_DRAFT])]
            if summary:
                lines.append(f"概述：{summary}")
            lines.extend(positive)
            sections.append("\n".join(lines))
        forbidden = [line for uid, line in self.forbidden if uid not in excluded]
        if forbidden:
            sections.append("\n".join([LEGACY_FORBIDDEN_HEADERS.get(self.role, LEGACY_FORBIDDEN_HEADERS[ROLE_DRAFT]), *forbidden]))
        gaps = [line for uid, line in self.gaps if uid not in excluded]
        if gaps and sections:
            sections.append("\n".join([RECENT_GAPS_HEADER, *gaps]))
        return "\n\n".join(sections)


_POSITIVE_SECTIONS = ("[文风卡]", "[正向风格特征]")
_FORBIDDEN_SECTIONS = ("[作者不这么写]", "[禁忌模式]")


def count_card_lines(block: str) -> tuple[int, int]:
    """(正向条数, 禁忌条数)：``- `` 开头的条目行按所在小节归类；气质行算一条正向。"""
    positive = forbidden = 0
    section = ""
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section.startswith(_POSITIVE_SECTIONS) and (stripped.startswith("- ") or stripped.startswith("气质")):
            positive += 1
        elif section.startswith(_FORBIDDEN_SECTIONS) and stripped.startswith("- "):
            forbidden += 1
    return positive, forbidden


# ---------------------------------------------------------------------------
# 声音 / 红线 / 证据例句
# ---------------------------------------------------------------------------


def voice_block(profile_json: Mapping[str, Any], role: str) -> str:
    """``[声音特征]``：画像里的具体习惯句（v3 ``voice.habits``，旧画像 ``voice_signature.habits``）。"""
    habits: Any = None
    for key in ("voice", "voice_signature"):
        candidate = _mapping(profile_json.get(key)).get("habits")
        if isinstance(candidate, list) and candidate:
            habits = candidate
            break
    lines: list[str] = []
    for item in habits or []:
        text = " ".join(str(item or "").split())
        if text and f"- {text}" not in lines:
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


def card_examples(session: Session, card: DimensionCard) -> dict[str, str]:
    """``card_only``：每条卡句至多一句 ≤60 字的证据例句（卡句的 ``evidence_quote_ids`` → 引文表）。"""
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
            text = quotes.get(quote_id, "")
            if not text:
                continue
            if len(text) > CARD_EXAMPLE_MAX_CHARS:
                text = text[: CARD_EXAMPLE_MAX_CHARS - 1] + "…"
            examples[line_id] = safe_reference_text(text)
            break
    return examples


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleWindow:
    ref: WindowRef
    line: str
    chars: int
    priority: int


@dataclass(frozen=True)
class RenderParts:
    """渲染的结构化部件——``fit`` 按它整窗 / 整句地去掉内容再重新拼装。"""

    role: str
    placement: str
    samples_header: str
    windows: tuple[SampleWindow, ...]  # 原书顺序
    card: CardSource | None
    voice: str
    red_line: str
    closing: str  # user 尾块的收口（含章首 / 章末补充）；system 落点时为空

    def samples_block(self, keep: frozenset[int] | None = None) -> tuple[str, list[SampleWindow]]:
        used = [w for w in self.windows if keep is None or w.priority in keep]
        if not used:
            return "", []
        return "\n".join([self.samples_header, *(w.line for w in used), FEW_SHOT_FRAME_END]), used

    def assemble(
        self,
        *,
        keep: frozenset[int] | None = None,
        excluded: frozenset[str] = frozenset(),
        include_voice: bool = True,
        include_card: bool = True,
    ) -> tuple[str, str, dict[str, Any]]:
        """(system 前缀, user 尾块, 各块正文)。任一块非空 → 红线随注。"""
        samples, used = self.samples_block(keep)
        card = self.card.render(excluded) if (self.card is not None and include_card) else ""
        voice = self.voice if include_voice else ""
        red_line = self.red_line if (samples or card or voice) else ""
        tail = ""
        if self.placement == PLACEMENT_USER_TAIL and samples:
            system_blocks = [FEW_SHOT_IN_USER_MESSAGE_NOTE, card, voice, red_line]
            tail = "\n\n" + samples + ("\n\n" + self.closing if self.closing else "") + "\n"
        else:
            system_blocks = [samples, card, voice, red_line]
        blocks = [block for block in system_blocks if block and block.strip()]
        prefix = STYLE_REFERENCE_OPEN + "\n\n".join(blocks) + STYLE_REFERENCE_CLOSE if (samples or card or voice) else ""
        return prefix, tail, {"samples": samples, "card": card, "voice": voice, "red_line": red_line, "windows": used}


@dataclass(frozen=True)
class RenderedStyle:
    system_prefix: str = ""
    user_tail: str = ""
    window_refs: tuple[dict[str, Any], ...] = ()
    stats: Mapping[str, Any] = field(default_factory=dict)
    audit: Mapping[str, Any] = field(default_factory=dict)
    parts: RenderParts | None = field(default=None, compare=False, repr=False)

    @property
    def empty(self) -> bool:
        return not (self.system_prefix or self.user_tail)


def render_stats(
    *,
    system_prefix: str,
    user_tail: str,
    blocks: Mapping[str, Any],
    k: int,
) -> dict[str, int]:
    """读数（``InjectionPreviewStats`` 的字段；抽象块只有卡 / 声音，量化指标与检索片段恒为 0）。"""
    positive, forbidden = count_card_lines(str(blocks.get("card") or ""))
    windows = list(blocks.get("windows") or [])
    return {
        "positive_lines": positive,
        "forbidden_lines": forbidden,
        "metric_lines": 0,
        "voice_lines": sum(1 for line in str(blocks.get("voice") or "").splitlines() if line.startswith("- ")),
        "few_shot_windows": len(windows),
        "few_shot_chars": sum(int(w.chars) for w in windows),
        "rag_snippets": 0,
        "total_prefix_chars": len(system_prefix) + len(user_tail),
        "intensity_effective_total_chars": len(str(blocks.get("card") or "")),
        "few_shot_k": int(k),
    }


def rendered_window_refs(windows: Sequence[SampleWindow]) -> tuple[dict[str, Any], ...]:
    """审计 / 界面用的窗口引用（原书顺序；不含正文；``paragraph_type`` 只给界面，提示里没有英文段型）。"""
    out = []
    for window in windows:
        ref = window.ref.to_dict()
        ref["chars"] = int(window.chars)
        out.append(ref)
    return tuple(out)


# ---------------------------------------------------------------------------
# 缓存（J1：一场的几道工序不重复渲染）
# ---------------------------------------------------------------------------

_CACHE_MAX = 64
_CACHE: "OrderedDict[str, RenderedStyle]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def reset_render_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_key(session: Session, policy: Any, request: StyleRenderRequest, *, root: str | None, samples_allowed: bool) -> str:
    try:
        db = str(session.get_bind().url)
    except Exception:  # noqa: BLE001
        db = str(id(session))
    material = "|".join(
        [
            db,
            str(getattr(policy, "contract_hash", "") or ""),
            str(getattr(policy, "mode", "") or ""),
            str(getattr(policy, "reference_mode", "") or ""),
            str(request.bundle_id or "live"),
            str(request.scene_id or ""),
            request.role,
            request.placement,
            str(request.effective_k(getattr(policy, "sample_windows", 0))),
            str(request.position or ""),
            ",".join(request.situation_tags),
            str(request.dialogue_heavy),
            str(request.rendering_mode or ""),
            ",".join(request.revise_dimensions),
            hashlib.sha256("\x1f".join(request.recent_gaps).encode("utf-8")).hexdigest()[:16],
            str(root or ""),
            str(samples_allowed),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> RenderedStyle | None:
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
        return cached


def _cache_put(key: str, value: RenderedStyle) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _sample_windows(session: Session, book_id: str, refs: Sequence[WindowRef]) -> list[SampleWindow]:
    if not refs:
        return []
    texts = window_texts(
        session,
        [
            SimpleNamespace(book_id=book_id, start_index=ref.start, end_index=ref.end, window_no=ref.window_no)
            for ref in refs
        ],
    )
    windows: list[SampleWindow] = []
    for priority, ref in enumerate(refs):
        text = str(texts.get(ref.window_no) or "").strip()
        if not text:
            continue
        text = _clip_at_sentence(text, _budget_int("sample_window_max_chars", SAMPLE_WINDOW_MAX_CHARS))
        safe = safe_reference_text(text)
        windows.append(
            SampleWindow(
                ref=ref,
                line=f"- ({window_position_tag(ref)})「{safe}」",
                chars=_visible_chars(text),
                priority=priority,
            )
        )
    windows.sort(key=lambda w: (w.ref.start, w.ref.window_no))
    return windows


def _samples_allowed(policy: Any, book: StyleReferenceBook | None, book_snapshot: Mapping[str, Any]) -> tuple[bool, str | None]:
    if book is None:
        return False, "book_missing"
    if book_snapshot.get("cloud_llm_allowed_at_freeze") is False:
        return False, "cloud_policy_at_freeze"
    if not cloud_llm_allowed(book):
        return False, "cloud_policy_now"
    return True, None


def build_card_source(
    session: Session | None,
    policy: Any,
    request: StyleRenderRequest,
    profile_json: Mapping[str, Any],
    *,
    forbidden_findings: Sequence[Mapping[str, Any]],
    examples_allowed: bool,
) -> CardSource | None:
    """v3 文风卡 → :class:`DimensionCardSource`；旧画像 → :class:`LegacyCardSource`（空替身 → None）。"""
    card = card_from_profile_json(profile_json)
    states = dict(getattr(policy, "dimension_states", None) or {})
    if card is not None:
        examples: dict[str, str] = {}
        if getattr(policy, "reference_mode", None) == REFERENCE_MODE_CARD_ONLY and examples_allowed and session is not None:
            examples = card_examples(session, card)
        return DimensionCardSource(
            card,
            role=request.role,
            dimension_states=states,
            line_states=line_states_from_profile_json(profile_json),
            recent_gaps=request.recent_gaps,
            budget_chars=_budget_int("card_budget_chars", DEFAULT_CARD_BUDGET_CHARS),
            examples=examples,
        )
    legacy = LegacyCardSource(
        profile_json,
        forbidden_findings=forbidden_findings,
        dimension_states=states,
        role=request.role,
        recent_gaps=request.recent_gaps,
    )
    return None if legacy.is_empty else legacy


def render_style(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    *,
    scene: Any = None,
    persist_selection: bool = True,
    use_cache: bool = True,
    build_index: bool = True,
    commit_index: bool = False,
) -> RenderedStyle:
    """一份策略 + 一个请求 → system 前缀 / user 尾块 / 实际用到的窗 / 读数 / 审计。

    策略未绑定（或没有契约）→ 空结果（审计 ``outcome="none"``）。样例的选窗每场冻结（``resolve_scene_selection``），
    预览传 ``persist_selection=False``。
    """
    if not getattr(policy, "bound", False) or not isinstance(getattr(policy, "contract", None), Mapping):
        return RenderedStyle(audit={"outcome": "none", "policy": policy.audit() if hasattr(policy, "audit") else {}})
    layer = _layer(policy.contract)
    profile = _mapping(layer.get("profile"))
    profile_json = _mapping(profile.get("profile_json"))
    book_snapshot = _mapping(layer.get("book"))
    book_id = str(getattr(policy, "book_id", "") or book_snapshot.get("book_id") or "")
    book = session.get(StyleReferenceBook, book_id) if book_id else None
    samples_allowed, blocked_reason = _samples_allowed(policy, book, book_snapshot)
    # 缓存键带当前窗口索引的根哈希：索引还没建 / 已过期时不查缓存（这一次会建索引），渲染完按建好的根哈希存
    live_root = None
    if book is not None and marker_is_current(book.stats_json):
        live_root = str(_mapping(_mapping(book.stats_json).get("window_index")).get("root") or "") or None
    if use_cache and live_root is not None:
        cached = _cache_get(_cache_key(session, policy, request, root=live_root, samples_allowed=samples_allowed))
        if cached is not None:
            return cached

    sends_samples = bool(getattr(policy, "sends_samples", False))
    sends_card = bool(getattr(policy, "sends_card", False))
    notices: list[str] = []
    card_source: CardSource | None = None
    voice = ""
    if sends_card:
        card_source = build_card_source(
            session,
            policy,
            request,
            profile_json,
            forbidden_findings=[item for item in layer.get("forbidden_findings") or [] if isinstance(item, Mapping)],
            examples_allowed=samples_allowed,
        )
        voice = voice_block(profile_json, request.role)
    k = request.effective_k(getattr(policy, "sample_windows", 0))
    selection: SceneSelection = EMPTY_SELECTION
    windows: list[SampleWindow] = []
    if sends_samples and not samples_allowed and blocked_reason:
        notices.append(NOTICE_SAMPLES_BLOCKED)
    if sends_samples and samples_allowed and k > 0:
        selection = resolve_scene_selection(
            session,
            policy,
            request,
            scene=scene,
            persist=persist_selection,
            build_index=build_index,
            commit_index=commit_index,
        )
        notices.extend(n for n in selection.notices if n not in notices)
        frozen_root = str(book_snapshot.get("paragraph_root_sha256") or "") or None
        if frozen_root and selection.root and frozen_root != selection.root:
            notices.append(NOTICE_BOOK_CHANGED)
        refs = role_windows(policy, request, selection, card=getattr(card_source, "card", None))
        windows = _sample_windows(session, book_id, refs)
    red_line = red_line_block([str(t) for t in layer.get("banned_terms") or []])
    closing = ""
    if request.placement == PLACEMENT_USER_TAIL and windows:
        closing = attach_chapter_position_mandate(
            CLOSING_MANDATES.get(request.role, FEW_SHOT_CLOSING_MANDATE),
            chapter_position_mandate(request.position, policy.contract),
        )
    parts = RenderParts(
        role=request.role,
        placement=request.placement,
        samples_header=SAMPLE_HEADERS.get(request.role, SAMPLE_HEADERS[ROLE_DRAFT]),
        windows=tuple(windows),
        card=card_source,
        voice=voice,
        red_line=red_line,
        closing=closing,
    )
    system_prefix, user_tail, blocks = parts.assemble()
    stats = render_stats(system_prefix=system_prefix, user_tail=user_tail, blocks=blocks, k=k)
    refs_out = rendered_window_refs(blocks["windows"])
    audit = build_audit(
        policy=policy,
        request=request,
        selection=selection,
        system_prefix=system_prefix,
        user_tail=user_tail,
        blocks={name: block_digest(str(blocks.get(name) or "")) for name in ("samples", "card", "voice", "red_line")},
        window_refs=refs_out,
        stats=stats,
        notices=notices,
        legacy_profile=isinstance(card_source, LegacyCardSource),
        legacy_digit_lines_dropped=getattr(card_source, "dropped_digit_lines", 0) if card_source else 0,
        card_examples=getattr(card_source, "example_count", 0) if card_source else 0,
        samples_blocked=blocked_reason if (sends_samples and not samples_allowed) else None,
    )
    rendered = RenderedStyle(
        system_prefix=system_prefix,
        user_tail=user_tail,
        window_refs=refs_out,
        stats=stats,
        audit=audit,
        parts=parts,
    )
    if use_cache:
        stored_root = selection.root or live_root
        if stored_root is None and book is not None and marker_is_current(book.stats_json):
            stored_root = str(_mapping(_mapping(book.stats_json).get("window_index")).get("root") or "") or None
        if stored_root is not None or not (sends_samples and samples_allowed and k > 0):
            _cache_put(_cache_key(session, policy, request, root=stored_root, samples_allowed=samples_allowed), rendered)
    return rendered


__all__ = [
    "CARD_EXAMPLE_MAX_CHARS",
    "CARD_HEADERS",
    "CLOSING_MANDATES",
    "CardSource",
    "DimensionCardSource",
    "FEW_SHOT_CLOSING_MANDATE_REVISE",
    "LEGACY_FORBIDDEN_HEADERS",
    "LEGACY_POSITIVE_HEADERS",
    "LegacyCardSource",
    "NOTICE_BOOK_CHANGED",
    "NOTICE_SAMPLES_BLOCKED",
    "RECENT_GAPS_HEADER",
    "RenderParts",
    "RenderedStyle",
    "SAMPLE_HEADERS",
    "SAMPLE_WINDOW_MAX_CHARS",
    "STYLE_REFERENCE_CLOSE",
    "STYLE_REFERENCE_OPEN",
    "SampleWindow",
    "VOICE_HEADERS",
    "attach_chapter_position_mandate",
    "build_card_source",
    "card_examples",
    "chapter_position_mandate",
    "count_card_lines",
    "red_line_block",
    "render_stats",
    "render_style",
    "rendered_window_refs",
    "reset_render_cache",
    "safe_reference_text",
    "voice_block",
    "window_position_tag",
]
