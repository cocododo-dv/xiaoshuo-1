"""风格参考 v3（2026-09-23）— 把一份风格策略渲染成提示里的参考（``render_style``）。

一份 :class:`~novel_system.services.style_policy.StylePolicy`（冻结契约或现解析的契约）+ 一个
:class:`~novel_system.services.style_reference.inject.request.StyleRenderRequest` → :class:`RenderedStyle`
（system 前缀、user 尾块、实际用到的窗、读数、不含正文的审计）。

**参考方式说到做到**（N8 / J8，``policy.reference_mode``，渲染时再按书**现在**的云策略压一次——v1 契约的书快照
没有策略，``segments_only`` 的书照样只送文风卡，L1）：

- ``full``：文风卡（或旧画像的卡替身）+ 声音 + 本场冻结的样例窗 + 红线；
- ``samples_only``：样例窗 + 红线；
- ``card_only``：文风卡 + 声音 + 红线，**不送窗口**；卡句后面至多挂一句 ≤11 字的原话例子（取自这句的证据引文，
  含受保护专名 / 禁用词或疑似指令的片段不用，M3）。``segments_only`` 的书由 ``effective_reference_mode`` 强制走这里。

**云策略按接收提示的节点判**（H1，``policy.decide_reference_route``）：请求带 ``node_ids``（适配器按模板推）；
「仅本机」的书遇云端节点、或说不出节点 → 抛 409 ``STYLE_REFERENCE_CLOUD_POLICY_BLOCKED``，由这本书派生的东西
（样例、文风卡、声音、专名表）一个字都不送；判定的结果进缓存键（同一场换了路由不会拿到旧渲染）。

**按角色的口径**（J16）：起草 / 改稿把样例放在 user 消息末尾、紧挨输出（收口指令 + 章首 / 章末补充），system
里留文风卡、声音、红线与一句指路；评审 / 规划的样例留在 system 里，标题是评审 / 规划的口径，不是「写本场时以
这些片段的手笔为准」。**这一次一窗样例都没有**（只用文风卡、原文不许送、窗口还没建、拟合把窗全去掉了）时，在
样例原本的位置写明「本次没有附原文样例，照文风卡与声音特征写」（M5）——起草模板说样例在消息末尾，不能让模型
去找一块不存在的样例。

**没有文风卡的画像**（迁移 0092 已把学习作业跑之前的旧画像归档，绑定它的作品按「画像未启用」降级）：不再有
卡替身（2026-09-24 清理删掉了 ``[正向风格特征]`` / ``[禁忌模式]`` 替身与它的数字行剔除）——这样的画像只送声音块
（若有）、样例与红线。

**不学的维**（``dimension_states == exclude``）：卡里整维不出现，声音块里属于这一维的习惯句、近期常见偏差里
属于这一维的条目也不带（L8）。

**红线**：反抄袭模板 + 画像的生成期禁用词（含学习作业自动登记的受保护专名 ``source="protected_auto"``），
只要有任一块参考就随注、永不截断。

书被改过（冻结契约的段落根哈希 ≠ 当前窗口索引的根哈希）：按当前索引渲染并在审计里记
``STYLE_REFERENCE_BOOK_CHANGED``——不再悄悄砍掉 87% 的样例（J6）；书已删除记 ``STYLE_REFERENCE_BOOK_MISSING``
（L5）。同一 (契约, 场景, 角色, 窗数, 参考方式, 接收节点与路由, 近期偏差……) 的渲染结果进程内缓存（J1），
一场的几道工序不重复渲染。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import unicodedata
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
    effective_reference_mode,
)
from novel_system.services.style_reference.binding_config import sends_card as mode_sends_card
from novel_system.services.style_reference.binding_config import sends_samples as mode_sends_samples
from novel_system.services.style_reference.card import (
    DEFAULT_CARD_BUDGET_CHARS,
    EXAMPLE_UNIT_PREFIX,
    LINE_STATE_EXCLUDED,
    UNIT_GAP,
    DimensionCard,
    card_from_profile_json,
    line_states_from_profile_json,
    plan_card_block,
)
from novel_system.services.style_reference.config_loader import (
    load_optional_yaml_config,
    load_text_template,
)
from novel_system.services.style_reference.fidelity import FEATURE_DIMENSIONS
from novel_system.services.style_reference.inject.audit import build_audit, block_digest
from novel_system.services.style_reference.inject.gaps import gap_dimensions
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
    NOTICE_BOOK_MISSING,
    SLOT_REVISE,
    SceneSelection,
    WindowRef,
    current_bundle_selection,
    resolve_scene_selection,
    role_windows,
)
from novel_system.services.style_reference.policy import decide_reference_route
from novel_system.services.style_reference.runtime_contract import contract_layer
from novel_system.services.style_reference.schemas import (
    FEW_SHOT_CLOSING_MANDATE,
    FEW_SHOT_CLOSING_MANDATE_FINAL,
    FEW_SHOT_IN_USER_MESSAGE_NOTE,
)
from novel_system.services.style_reference.structure import chapter_boundary_habits
from novel_system.services.style_reference.untrusted_data import (
    FEW_SHOT_FRAME_END,
    NEUTRALIZED_MARK,
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
# 只用文风卡时卡句后面的原话例子：至多 11 个字（界面的承诺「卡上的例子至多 11 个字」），太短的片段不当例子
CARD_EXAMPLE_MAX_CHARS = 11
CARD_EXAMPLE_MIN_CHARS = 4
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？；：、,.!?;:…—\n\r\t「」『』“”‘’\"'（）()《》〈〉【】\[\]〔〕]+")
_ESCAPED_BOUNDARY_TOKEN = "UNTRUSTED_BOUNDARY_ESCAPED"
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
# M5：这一次一窗样例都没带（只用文风卡 / 原文不许送 / 还没有窗口 / 预算拟合去光了窗）时，写在样例原本的位置——
# 起草 / 改稿模板说样例在消息末尾，不能让模型去找一块不存在的样例，也不能让它以为参考只剩抽象描述时可以随便写。
# 不用 ``[风格样例]`` 这个标签（有这个标签 = 真的附了原文样例，各处都按它认）。
NO_SAMPLES_TAIL_NOTES: dict[str, str] = {
    ROLE_DRAFT: (
        "（本次没有附参考作者的原文样例：前文说放在这条消息末尾的样例片段，这一次没有。"
        "照 system 提示 [STYLE_REFERENCE] 里的文风卡与声音特征，用这位作者的手笔写这一场。）"
    ),
    ROLE_REVISE: (
        "（本次没有附参考作者的原文样例：前文说放在这条消息末尾的样例片段，这一次没有。"
        "改稿时对照 system 提示 [STYLE_REFERENCE] 里的文风卡与声音特征，只改不像这位作者的地方。）"
    ),
}
NO_SAMPLES_SYSTEM_NOTES: dict[str, str] = {
    ROLE_DRAFT: "（本次没有附参考作者的原文样例：照下面的文风卡与声音特征，用这位作者的手笔写。）",
    ROLE_REVISE: "（本次没有附参考作者的原文样例：改稿时对照下面的文风卡与声音特征，只改不像这位作者的地方。）",
    ROLE_REVIEW: (
        "（本次没有附参考作者的原文样例：评审时对照下面的文风卡与声音特征判断像不像这位作者，"
        "不因为没有样例就改用通用的写作规范。）"
    ),
    ROLE_PLAN: "（本次没有附参考作者的原文样例：规划时按下面的文风卡与声音特征设想这位作者的手法。）",
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
    layer = contract_layer(contract)
    if layer:
        profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
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


def _letter_chars(text: str) -> int:
    return sum(1 for char in text if unicodedata.category(char)[0] in ("L", "N"))


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


def _layer(contract: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return contract_layer(contract)


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
        candidate = _mapping(profile_json.get(key)).get("habits")
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
        chars = _visible_chars(candidate)
        # 上限按可见字（标点也算一个字，只会更严）；下限按字母数字（「嗯，好，走」不算一个像样的例子）
        if _letter_chars(candidate) < CARD_EXAMPLE_MIN_CHARS or chars > CARD_EXAMPLE_MAX_CHARS or chars <= best_chars:
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

    def no_samples_note(self) -> str:
        """一窗样例都没带时写在样例位置的那句话（M5；按落点与角色）。"""
        if self.placement == PLACEMENT_USER_TAIL:
            note = NO_SAMPLES_TAIL_NOTES.get(self.role, NO_SAMPLES_TAIL_NOTES[ROLE_DRAFT])
            return note + "\n" + FEW_SHOT_CLOSING_MANDATE_FINAL
        return NO_SAMPLES_SYSTEM_NOTES.get(self.role, NO_SAMPLES_SYSTEM_NOTES[ROLE_DRAFT])

    def assemble(
        self,
        *,
        keep: frozenset[int] | None = None,
        excluded: frozenset[str] = frozenset(),
        include_voice: bool = True,
        include_card: bool = True,
    ) -> tuple[str, str, dict[str, Any]]:
        """(system 前缀, user 尾块, 各块正文)。任一块非空 → 红线随注；有卡 / 声音而一窗样例都没有 → 在样例原本的
        位置写明「本次没有附原文样例」（M5：起草 / 改稿写在 user 尾部，评审 / 规划写在 system 前缀里）。"""
        samples, used = self.samples_block(keep)
        card = self.card.render(excluded) if (self.card is not None and include_card) else ""
        voice = self.voice if include_voice else ""
        red_line = self.red_line if (samples or card or voice) else ""
        note = self.no_samples_note() if (not samples and (card or voice)) else ""
        tail = ""
        if self.placement == PLACEMENT_USER_TAIL:
            if samples:
                system_blocks = [FEW_SHOT_IN_USER_MESSAGE_NOTE, card, voice, red_line]
                tail = "\n\n" + samples + ("\n\n" + self.closing if self.closing else "") + "\n"
            else:
                system_blocks = [card, voice, red_line]
                tail = "\n\n" + note + "\n" if note else ""
        else:
            system_blocks = [samples or note, card, voice, red_line]
        blocks = [block for block in system_blocks if block and block.strip()]
        prefix = STYLE_REFERENCE_OPEN + "\n\n".join(blocks) + STYLE_REFERENCE_CLOSE if (samples or card or voice) else ""
        return prefix, tail, {
            "samples": samples,
            "card": card,
            "voice": voice,
            "red_line": red_line,
            "windows": used,
            "no_samples_note": note,
        }


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
    """读数（``InjectionPreviewStats`` 的字段）：卡的正向条数 / 「作者不这么写」条数、声音行数、样例窗数与字数、
    前缀总字数、卡的字数、窗数上限。"""
    positive, avoid = count_card_lines(str(blocks.get("card") or ""))
    windows = list(blocks.get("windows") or [])
    card = str(blocks.get("card") or "")
    return {
        "positive_lines": positive,
        "avoid_lines": avoid,
        "voice_lines": sum(1 for line in str(blocks.get("voice") or "").splitlines() if line.startswith("- ")),
        "few_shot_windows": len(windows),
        "few_shot_chars": sum(int(w.chars) for w in windows),
        "total_prefix_chars": len(system_prefix) + len(user_tail),
        "card_chars": len(card),
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


def _cache_key(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    *,
    root: str | None,
    reference_mode: str,
    route_token: str,
    selection_anchor: str = "",
) -> str:
    try:
        db = str(session.get_bind().url)
    except Exception:  # noqa: BLE001
        db = str(id(session))
    material = "|".join(
        [
            db,
            str(getattr(policy, "contract_hash", "") or ""),
            str(getattr(policy, "mode", "") or ""),
            str(reference_mode or ""),
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
            # 接收提示的节点、路由是否本机、这一次送什么（H1：换了节点路由不会拿到旧渲染）
            route_token,
            # 没有 bundle 的渲染用的是哪一份冻结选窗（这一场当前 bundle 的，还是自己的 live 行，M4）
            selection_anchor,
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


def keep_priority(refs: Sequence[WindowRef]) -> list[WindowRef]:
    """预算拟合的保留次序（先保的在前）：改稿换进来的「示范要改那几维手法」的窗最先保（它们是这一次修改的
    依据，L3——原来它们占着选窗顺序末尾的位置，拟合第一个就去掉它们），其余按选窗顺序（位置 → 场面 → 手法 →
    质地 → 典型）。"""
    revise = [ref for ref in refs if ref.slot == SLOT_REVISE]
    return revise + [ref for ref in refs if ref.slot != SLOT_REVISE]


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
    for priority, ref in enumerate(keep_priority(refs)):
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


def effective_render_mode(policy: Any, book: StyleReferenceBook | None, book_snapshot: Mapping[str, Any]) -> str:
    """这一次渲染的参考方式：绑定的参考方式，再按书冻结时与**现在**的云策略各压一次（L1：v1 契约的书快照没有
    ``cloud_policy``，``policy_from_contract`` 压不到；书现在是 ``segments_only`` 就只送文风卡）。"""
    mode = str(getattr(policy, "reference_mode", "") or "")
    mode = effective_reference_mode(mode, cloud_policy=str(book_snapshot.get("cloud_policy") or "") or None)
    if book is not None:
        mode = effective_reference_mode(mode, cloud_policy=str(getattr(book, "cloud_policy", "") or "") or None)
    return mode


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
        budget_chars=_budget_int("card_budget_chars", DEFAULT_CARD_BUDGET_CHARS),
        examples=examples,
    )


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

    云策略按 ``request.node_ids`` 的实际路由判（H1，``policy.decide_reference_route``）：「仅本机」的书遇云端节点
    （或说不出节点）、未知策略的书遇云端节点 → 抛 409（``CloudPolicyBlockedError`` / ``CloudPolicyInvalidError``），
    由这本书派生的东西一个字都不渲染。
    """
    if not getattr(policy, "bound", False) or not isinstance(getattr(policy, "contract", None), Mapping):
        return RenderedStyle(audit={"outcome": "none", "policy": policy.audit() if hasattr(policy, "audit") else {}})
    layer = _layer(policy.contract)
    profile = _mapping(layer.get("profile"))
    profile_json = _mapping(profile.get("profile_json"))
    book_snapshot = _mapping(layer.get("book"))
    book_id = str(getattr(policy, "book_id", "") or book_snapshot.get("book_id") or "")
    book = session.get(StyleReferenceBook, book_id) if book_id else None
    route = decide_reference_route(
        book,
        node_ids=request.node_ids,
        frozen_book=book_snapshot,
        operation=f"style_reference_{request.role}",
    )
    route.raise_if_blocked()
    reference_mode = effective_render_mode(policy, book, book_snapshot)
    sends_samples = mode_sends_samples(reference_mode)
    sends_card = mode_sends_card(reference_mode)
    samples_allowed = route.send_samples
    notices: list[str] = []
    if book is None:
        # L5：书已删除——先说书不在（原来被当成「原文被云策略挡下」报）
        notices.append(NOTICE_BOOK_MISSING)
    if not route.send_book:
        # 书不在、冻结快照里又说不清它的云策略：由这本书派生的东西一概不送（不报错——书是作者删的）
        return RenderedStyle(
            audit=build_audit(
                policy=policy,
                request=request,
                selection=EMPTY_SELECTION,
                system_prefix="",
                user_tail="",
                blocks={name: block_digest("") for name in ("samples", "card", "voice", "red_line")},
                window_refs=(),
                stats=render_stats(system_prefix="", user_tail="", blocks={}, k=0),
                notices=notices,
                reference_mode=reference_mode,
                route=route.audit(),
            )
        )
    # 缓存键带当前窗口索引的根哈希：索引还没建 / 已过期时不查缓存（这一次会建索引），渲染完按建好的根哈希存
    live_root = None
    if book is not None and marker_is_current(book.stats_json):
        live_root = str(_mapping(_mapping(book.stats_json).get("window_index")).get("root") or "") or None

    def _selection_anchor(root: str | None) -> str:
        # 没有 bundle 的场景渲染：用的是这一场当前 bundle 冻结的选窗，还是自己的 live 行（M4）——进缓存键
        if request.bundle_id is not None or not request.scene_id or not samples_allowed or root is None:
            return ""
        shared = current_bundle_selection(session, policy, request, root=root)
        return f"bundle:{shared[0].selection_id}" if shared is not None else "own"

    if use_cache and live_root is not None:
        cached = _cache_get(
            _cache_key(
                session,
                policy,
                request,
                root=live_root,
                reference_mode=reference_mode,
                route_token=route.cache_token,
                selection_anchor=_selection_anchor(live_root),
            )
        )
        if cached is not None:
            return cached

    states = dict(getattr(policy, "dimension_states", None) or {})
    recent_gaps = filter_recent_gaps(request.recent_gaps, states)
    card_source: CardSource | None = None
    voice = ""
    if sends_card:
        card_source = build_card_source(
            session,
            policy,
            request,
            profile_json,
            examples_allowed=samples_allowed,
            reference_mode=reference_mode,
            recent_gaps=recent_gaps,
            protected_terms=example_protected_terms(layer),
        )
        voice = voice_block(profile_json, request.role, dimension_states=states)
    k = request.effective_k(getattr(policy, "sample_windows", 0))
    selection: SceneSelection = EMPTY_SELECTION
    windows: list[SampleWindow] = []
    samples_blocked = route.reason if (sends_samples and not samples_allowed and book is not None) else None
    if samples_blocked:
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
        refs = role_windows(policy, request, selection)
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
        card_examples=getattr(card_source, "example_count", 0) if card_source else 0,
        samples_blocked=samples_blocked,
        reference_mode=reference_mode,
        route=route.audit(),
        no_samples_note=bool(blocks.get("no_samples_note")),
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
            _cache_put(
                _cache_key(
                    session,
                    policy,
                    request,
                    root=stored_root,
                    reference_mode=reference_mode,
                    route_token=route.cache_token,
                    selection_anchor=_selection_anchor(stored_root),
                ),
                rendered,
            )
    return rendered


__all__ = [
    "CARD_EXAMPLE_MAX_CHARS",
    "CARD_EXAMPLE_MIN_CHARS",
    "CARD_HEADERS",
    "CLOSING_MANDATES",
    "CardSource",
    "DimensionCardSource",
    "FEW_SHOT_CLOSING_MANDATE_REVISE",
    "FIT_EXAMPLE_PREFIX",
    "FIT_GAPS_UNIT",
    "NOTICE_BOOK_CHANGED",
    "NOTICE_BOOK_MISSING",
    "NOTICE_SAMPLES_BLOCKED",
    "NO_SAMPLES_SYSTEM_NOTES",
    "NO_SAMPLES_TAIL_NOTES",
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
    "card_example_clause",
    "card_examples",
    "chapter_position_mandate",
    "count_card_lines",
    "effective_render_mode",
    "example_protected_terms",
    "filter_recent_gaps",
    "habit_dimension",
    "keep_priority",
    "red_line_block",
    "render_stats",
    "render_style",
    "rendered_window_refs",
    "reset_render_cache",
    "safe_reference_text",
    "voice_block",
    "window_position_tag",
]
