"""风格参考 v3（2026-09-23）— 把一份风格策略渲染成提示里的参考（``render_style``）。

一份 :class:`~novel_system.services.style_policy.StylePolicy`（冻结契约或现解析的契约）+ 一个
:class:`~novel_system.services.style_reference.inject.request.StyleRenderRequest` → :class:`RenderedStyle`
（system 前缀、user 尾块、实际用到的窗、读数、不含正文的审计）。

**参考方式说到做到**（N8 / J8，``policy.reference_mode``，渲染时再按书**现在**的云策略压一次——v1 契约的书快照
没有策略，``segments_only`` 的书照样只送文风卡，L1）：

- ``full``：文风卡 + 声音 + 本场冻结的样例窗 + 红线；
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

（2026-09-30：各块怎么写在 ``inject/blocks``，缓存在 ``inject/cache``；这里是拼装与入口，原来的名字照旧从这里转出。）
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook
from novel_system.services.style_reference.binding_config import (
    effective_reference_mode,
)
from novel_system.services.style_reference.binding_config import sends_card as mode_sends_card
from novel_system.services.style_reference.binding_config import sends_samples as mode_sends_samples
from novel_system.services.style_reference.budget_config import injection_budget
from novel_system.services.style_reference.inject.audit import build_audit, block_digest
from novel_system.services.style_reference.inject.blocks import (
    CARD_EXAMPLE_MAX_CHARS,
    CARD_EXAMPLE_MIN_CHARS,
    CARD_HEADERS,
    FIT_EXAMPLE_PREFIX,
    FIT_GAPS_UNIT,
    VOICE_HEADERS,
    CardSource,
    DimensionCardSource,
    build_card_source,
    card_example_clause,
    card_examples,
    clip_at_sentence,
    count_card_lines,
    example_protected_terms,
    filter_recent_gaps,
    habit_dimension,
    mapping_or_empty,
    nonspace_chars,
    red_line_block,
    safe_reference_text,
    voice_block,
    window_position_tag,
)
from novel_system.services.style_reference.inject.cache import (  # noqa: F401 — _CACHE：缓存复位测试直接读写
    _CACHE,
    cache_get,
    cache_key,
    cache_put,
    reset_render_cache,
)
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
from novel_system.services.style_reference.structure_render import chapter_boundary_habits
from novel_system.services.style_reference.untrusted_data import (
    FEW_SHOT_FRAME_END,
)
from novel_system.services.style_reference.windows import marker_is_current, window_texts

logger = logging.getLogger(__name__)

NOTICE_BOOK_CHANGED = "STYLE_REFERENCE_BOOK_CHANGED"
NOTICE_SAMPLES_BLOCKED = "STYLE_REFERENCE_SAMPLES_BLOCKED"

STYLE_REFERENCE_OPEN = "[STYLE_REFERENCE]\n"
STYLE_REFERENCE_CLOSE = "\n[/STYLE_REFERENCE]\n\n"

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


def _layer(contract: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return contract_layer(contract)


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
# 入口
# ---------------------------------------------------------------------------


def keep_priority(refs: Sequence[WindowRef]) -> list[WindowRef]:
    """预算拟合的保留次序（先保的在前）：改稿换进来的「示范要改那几维手法」的窗最先保（它们是这一次修改的
    依据，L3——原来它们占着选窗顺序末尾的位置，拟合第一个就去掉它们），其余按选窗顺序（位置 → 场面 → 维度示范 →
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
        text = clip_at_sentence(text, injection_budget().sample_window_max_chars)
        safe = safe_reference_text(text)
        windows.append(
            SampleWindow(
                ref=ref,
                line=f"- ({window_position_tag(ref)})「{safe}」",
                chars=nonspace_chars(text),
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


@dataclass(frozen=True)
class _RenderPlan:
    """一次渲染在建块之前定下的事：契约里最具体的一层、书（及冻结时的快照）、接收节点的路由判定、参考方式。"""

    layer: Mapping[str, Any]
    profile_json: Mapping[str, Any]
    book_snapshot: Mapping[str, Any]
    book_id: str
    book: StyleReferenceBook | None
    route: Any
    reference_mode: str
    notices: tuple[str, ...]

    @property
    def sends_samples(self) -> bool:
        return mode_sends_samples(self.reference_mode)

    @property
    def sends_card(self) -> bool:
        return mode_sends_card(self.reference_mode)

    @property
    def samples_allowed(self) -> bool:
        return bool(self.route.send_samples)

    def live_root(self) -> str | None:
        """当前窗口索引的根哈希（索引还没建 / 已过期 → None：这一次会建索引，不查缓存）。"""
        if self.book is None or not marker_is_current(self.book.stats_json):
            return None
        return str(mapping_or_empty(mapping_or_empty(self.book.stats_json).get("window_index")).get("root") or "") or None


def _plan_render(session: Session, policy: Any, request: StyleRenderRequest) -> _RenderPlan:
    """画像 / 书 / 路由判定 / 参考方式；接收节点的路由不许送这本书派生的东西 → 抛 409（H1，``raise_if_blocked``）。"""
    layer = _layer(policy.contract)
    profile = mapping_or_empty(layer.get("profile"))
    book_snapshot = mapping_or_empty(layer.get("book"))
    book_id = str(getattr(policy, "book_id", "") or book_snapshot.get("book_id") or "")
    book = session.get(StyleReferenceBook, book_id) if book_id else None
    route = decide_reference_route(
        book,
        node_ids=request.node_ids,
        frozen_book=book_snapshot,
        operation=f"style_reference_{request.role}",
    )
    route.raise_if_blocked()
    return _RenderPlan(
        layer=layer,
        profile_json=mapping_or_empty(profile.get("profile_json")),
        book_snapshot=book_snapshot,
        book_id=book_id,
        book=book,
        route=route,
        reference_mode=effective_render_mode(policy, book, book_snapshot),
        # L5：书已删除——先说书不在（原来被当成「原文被云策略挡下」报）
        notices=(NOTICE_BOOK_MISSING,) if book is None else (),
    )


def _nothing_sent(policy: Any, request: StyleRenderRequest, plan: _RenderPlan) -> RenderedStyle:
    """书不在、冻结快照里又说不清它的云策略：由这本书派生的东西一概不送（不报错——书是作者删的），只留审计。"""
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
            notices=list(plan.notices),
            reference_mode=plan.reference_mode,
            route=plan.route.audit(),
        )
    )


def _selection_anchor(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    plan: _RenderPlan,
    root: str | None,
    memo: dict[str, str],
) -> str:
    """没有 bundle 的场景渲染：用的是这一场当前 bundle 冻结的选窗，还是自己的 live 行（M4）——进缓存键。
    同一个根哈希只查一次（查缓存与存缓存各要一次；渲染本身只会写这一场自己的 live 行，不会改当前 bundle 的那一行）。"""
    if request.bundle_id is not None or not request.scene_id or not plan.samples_allowed or root is None:
        return ""
    if root not in memo:
        shared = current_bundle_selection(session, policy, request, root=root)
        memo[root] = f"bundle:{shared[0].selection_id}" if shared is not None else "own"
    return memo[root]


def _build_rendered(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    plan: _RenderPlan,
    *,
    scene: Any,
    persist_selection: bool,
    build_index: bool,
    commit_index: bool,
) -> tuple[RenderedStyle, SceneSelection, int]:
    """建各块（文风卡 / 声音 / 本场冻结的样例窗 / 红线 / 收口）→ 拼装 → 读数与审计；返回 (结果, 选窗, 窗数上限)。"""
    notices = list(plan.notices)
    states = dict(getattr(policy, "dimension_states", None) or {})
    recent_gaps = filter_recent_gaps(request.recent_gaps, states)
    card_source: CardSource | None = None
    voice = ""
    if plan.sends_card:
        card_source = build_card_source(
            session,
            policy,
            request,
            plan.profile_json,
            examples_allowed=plan.samples_allowed,
            reference_mode=plan.reference_mode,
            recent_gaps=recent_gaps,
            protected_terms=example_protected_terms(plan.layer),
        )
        voice = voice_block(plan.profile_json, request.role, dimension_states=states)
    k = request.effective_k(getattr(policy, "sample_windows", 0))
    selection: SceneSelection = EMPTY_SELECTION
    windows: list[SampleWindow] = []
    samples_blocked = (
        plan.route.reason if (plan.sends_samples and not plan.samples_allowed and plan.book is not None) else None
    )
    if samples_blocked:
        notices.append(NOTICE_SAMPLES_BLOCKED)
    if plan.sends_samples and plan.samples_allowed and k > 0:
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
        frozen_root = str(plan.book_snapshot.get("paragraph_root_sha256") or "") or None
        if frozen_root and selection.root and frozen_root != selection.root:
            notices.append(NOTICE_BOOK_CHANGED)
        refs = role_windows(policy, request, selection)
        windows = _sample_windows(session, plan.book_id, refs)
    red_line = red_line_block([str(t) for t in plan.layer.get("banned_terms") or []])
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
        reference_mode=plan.reference_mode,
        route=plan.route.audit(),
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
    return rendered, selection, k


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
    plan = _plan_render(session, policy, request)
    if not plan.route.send_book:
        return _nothing_sent(policy, request, plan)
    anchors: dict[str, str] = {}

    def key_for(root: str | None) -> str:
        return cache_key(
            session,
            policy,
            request,
            root=root,
            reference_mode=plan.reference_mode,
            route_token=plan.route.cache_token,
            selection_anchor=_selection_anchor(session, policy, request, plan, root, anchors),
        )

    # 缓存键带当前窗口索引的根哈希：索引还没建 / 已过期时不查缓存（这一次会建索引），渲染完按建好的根哈希存
    live_root = plan.live_root()
    if use_cache and live_root is not None:
        cached = cache_get(key_for(live_root))
        if cached is not None:
            return cached
    rendered, selection, k = _build_rendered(
        session,
        policy,
        request,
        plan,
        scene=scene,
        persist_selection=persist_selection,
        build_index=build_index,
        commit_index=commit_index,
    )
    if use_cache:
        stored_root = selection.root or live_root or plan.live_root()
        if stored_root is not None or not (plan.sends_samples and plan.samples_allowed and k > 0):
            cache_put(key_for(stored_root), rendered)
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
    "RenderParts",
    "RenderedStyle",
    "SAMPLE_HEADERS",
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
