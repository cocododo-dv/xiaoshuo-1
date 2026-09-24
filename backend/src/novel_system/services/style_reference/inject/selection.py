"""风格参考 v3（2026-09-23）— 按本场挑样例、每场冻结一次（J2 / J3 / J4 / N4，契约文档 §2.5）。

过去选窗依赖被润色那一稿的启发式段型：同一场的首稿、润色、评审、补丁看的窗各不相同（实库 SC01 的几道工序
只共享 3/12 窗、SC02 只有 1/12）；轮换池只有 k×6 = 72 / 522 窗，章首 / 章末场永远是同几窗；还强塞心理窗、
在提示里印英文段型。现在：

- **只看本场设计**，不看任何草稿：窗口索引（``windows.py``，持久化）+ 种子（``scene_id``）+ 章内位置 + 场面
  标签（蓝图给的 ``situation_tags``，没有就由 :func:`derive_situation_tags` 从场景设计推）+ 对白 / 概述倾向 +
  窗数 k + 维度状态 + 改稿维 + 近期偏差维；
- **配额**（k=12，其它 k 按比例）：章首 / 章末位置匹配 ≤3；场面标签匹配 ≈4；维度示范 ≈2（目标维 = 绑定里的
  重点维 ∪ 改稿维 ∪ 近期常见偏差维，按窗口标签的 ``dimensions``——这一窗最能示范的 ≤3 维——重合挑，2026-09-24
  O1 起窗口标签不再有书特有的「手法」）；对白密的场（或概述场）让一半窗口有相应的质地；其余在**全书**按典型度
  加权、按种子抽样——一章至多一窗，不够再放宽（先不相邻，再任意）。没打标签的窗（学习作业还没跑、或还是 v1
  标签）不计入标签 / 维度配额，由典型度抽样补足；
- **冻结**：结果写进 ``style_reference_scene_windows``（``selection_key`` = sha256(bundle_id 或 "live" |
  契约哈希 | scene_id | :data:`SELECTION_VERSION`)），同一 bundle 里这一场以后的每道工序都读这一行——首稿、
  改稿、评审、补丁看到同一组窗（评审取前 4 窗、规划前 3 窗，改稿至多把 2 窗换成示范要改那几维的窗）；
  索引的段落根哈希变了（书被改过）才按当前索引重选并覆盖这一行；
- **没有 bundle 的节点跟着这一场的当前 bundle 走**（M4）：对照检查的评审、写作台的深评 / 局部深评 / 局部补丁 /
  建议都不带 bundle。这一场的 ``SceneRunState.current_bundle_id`` 已经冻结过选窗、且那一行的契约哈希就是现在
  这份策略的契约哈希时，直接用那一组窗（不另写一行 "live"）；否则才按 "live" 选窗、冻结。「每场冻结一次」
  因此是真的每场，不是每个 bundle；
- 选窗结果按**选窗顺序**存（位置 → 场面 → 维度 → 质地 → 典型），渲染时按原书顺序呈现；冻结行的 ``params_json``
  记下 ``book_id`` / ``profile_id``，删书 / 破坏式重分类时 :func:`purge_scene_windows_for_book` 按 ``book_id`` 清掉
  这本书的冻结行（S6）。
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novel_system.db.models import (
    SceneRunState,
    StyleReferenceBook,
    StyleReferenceSceneWindows,
    StyleReferenceWindow,
    utcnow,
)
from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    DIMENSION_EMPHASIZE,
    DIMENSION_EXCLUDE,
)
from novel_system.services.style_reference.inject.gaps import gap_dimensions
from novel_system.services.style_reference.inject.request import (
    POSITION_CLOSING,
    POSITION_OPENING,
    POSITION_WHOLE,
    ROLE_REVISE,
    StyleRenderRequest,
)
from novel_system.services.style_reference.tags import MAX_SITUATIONS, normalize_situation_tags
from novel_system.services.style_reference.windows import (
    WINDOW_INDEX_VERSION,
    ensure_window_index,
    index_marker,
    marker_is_current,
)

logger = logging.getLogger(__name__)

SELECTION_VERSION = "scene_windows_v1"

# 窗口「有对白」/「以叙述为主」：测量核的唯一对白占比（引号内可见字 ÷ 可见字）
DIALOGUE_WINDOW_SHARE = 0.30
NARRATION_WINDOW_SHARE = 0.10
# 典型度加权：w = exp(λ · clip(稳健 z, ±3))——比中位数典型 2 个尺度的窗约 3.3 倍可能，全书每一窗都有机会
TYPICALITY_WEIGHT_LAMBDA = 0.6
TYPICALITY_Z_CLIP = 3.0
# k=12 时的配额基数（其它 k 按比例；位置 ≤3）
POSITION_QUOTA_MAX = 3
SITUATION_QUOTA_BASE = 4
DIMENSION_QUOTA_BASE = 2
QUOTA_BASE_K = 12
# 改稿可换成维度示范窗的窗数
REVISE_SWAP_MAX = 2

# 配额槽位 id（冻结行与 window_refs 里的 ``slot``；前端 STYLE_WINDOW_SLOT_LABELS 按它显示中文）。
# 2026-09-24 O1：``device`` → ``dimension``、``revise_device`` → ``revise_dimension``
SLOT_POSITION = "position"
SLOT_SITUATION = "situation"
SLOT_DIMENSION = "dimension"
SLOT_TEXTURE = "texture"
SLOT_TYPICAL = "typical"
SLOT_REVISE = "revise_dimension"

NOTICE_BOOK_MISSING = "STYLE_REFERENCE_BOOK_MISSING"
NOTICE_NO_WINDOWS = "STYLE_REFERENCE_NO_WINDOWS"


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


def _dimension_keys(values: Any) -> tuple[str, ...]:
    """窗口标签 / 冻结引用里的维度键：只认 16 维的键（v1 标签的 ``devices`` 不在这里，读出来就是空）。"""
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else ():
        key = str(value or "").strip()
        if key in ALL_DIMENSIONS and key not in out:
            out.append(key)
    return tuple(out)


@dataclass(frozen=True)
class IndexWindow:
    """窗口索引的一行（只取选窗要的列，不加载 features_json）。"""

    window_no: int
    start: int
    end: int
    chapter: int
    position: str
    chars: int
    paragraphs: int
    dialogue_share: float
    typicality: float
    situations: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    paragraph_type: str = ""

    @property
    def chapter_key(self) -> int:
        # 没有章号的窗各算一章（不让「第 0 章」挤掉彼此）
        return self.chapter if self.chapter > 0 else -self.window_no


@dataclass(frozen=True)
class WindowRef:
    """冻结的一窗引用（不带正文）。``slot`` 记它是按哪条配额选进来的；``paragraph_type`` 只给审计 / 界面。"""

    window_no: int
    start: int
    end: int
    chapter: int
    position: str
    chars: int
    paragraphs: int
    dialogue_share: float = 0.0
    typicality: float = 0.0
    slot: str = SLOT_TYPICAL
    situations: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    paragraph_type: str = ""

    @classmethod
    def from_index(cls, window: IndexWindow, slot: str) -> "WindowRef":
        return cls(
            window_no=window.window_no,
            start=window.start,
            end=window.end,
            chapter=window.chapter,
            position=window.position,
            chars=window.chars,
            paragraphs=window.paragraphs,
            dialogue_share=round(float(window.dialogue_share), 4),
            typicality=round(float(window.typicality), 4),
            slot=slot,
            situations=tuple(window.situations),
            dimensions=tuple(window.dimensions),
            paragraph_type=window.paragraph_type,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WindowRef | None":
        try:
            start = int(raw.get("start"))
            end = int(raw.get("end"))
            window_no = int(raw.get("window_no"))
        except (TypeError, ValueError):
            return None
        if start < 0 or end < start:
            return None

        def _int(name: str) -> int:
            try:
                return int(raw.get(name) or 0)
            except (TypeError, ValueError):
                return 0

        def _float(name: str) -> float:
            try:
                return float(raw.get(name) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(
            window_no=window_no,
            start=start,
            end=end,
            chapter=_int("chapter"),
            position=str(raw.get("position") or ""),
            chars=_int("chars"),
            paragraphs=_int("paragraphs"),
            dialogue_share=_float("dialogue_share"),
            typicality=_float("typicality"),
            slot=str(raw.get("slot") or SLOT_TYPICAL),
            situations=tuple(str(s) for s in raw.get("situations") or () if str(s or "").strip()),
            dimensions=_dimension_keys(raw.get("dimensions")),
            paragraph_type=str(raw.get("paragraph_type") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_no": self.window_no,
            "start": self.start,
            "end": self.end,
            "chapter": self.chapter,
            "position": self.position,
            "chars": self.chars,
            "paragraphs": self.paragraphs,
            "dialogue_share": self.dialogue_share,
            "typicality": self.typicality,
            "slot": self.slot,
            "situations": list(self.situations),
            "dimensions": list(self.dimensions),
            "paragraph_type": self.paragraph_type,
        }


@dataclass(frozen=True)
class SceneSelection:
    """一场的选窗（按选窗顺序）+ 冻结信息。``index`` 是这次读到的索引（改稿换窗用），不参与比较。"""

    refs: tuple[WindowRef, ...] = ()
    selection_id: str | None = None
    selection_key: str | None = None
    persisted: bool = False
    reused: bool = False
    book_id: str | None = None
    root: str | None = None
    window_count: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)
    notices: tuple[str, ...] = ()
    index: tuple[IndexWindow, ...] = field(default=(), compare=False, repr=False)

    def audit(self) -> dict[str, Any]:
        return {
            "selection_id": self.selection_id,
            "selection_key": self.selection_key,
            "persisted": self.persisted,
            "reused": self.reused,
            "book_id": self.book_id,
            "root": self.root,
            "index_window_count": self.window_count,
            "selected": len(self.refs),
            "slots": dict(Counter(ref.slot for ref in self.refs)),
            "version": SELECTION_VERSION,
        }


EMPTY_SELECTION = SceneSelection()


# ---------------------------------------------------------------------------
# 本场设计 → 选窗输入
# ---------------------------------------------------------------------------


def _brief(scene: Any) -> Mapping[str, Any]:
    brief = getattr(scene, "writer_brief_json", None) if scene is not None else None
    return brief if isinstance(brief, Mapping) else {}


def scene_chapter_position(scene: Any) -> str | None:
    """章内位置：本章第一场 → opening，最后一场 → closing，一章只有一场 → whole，其余 None。"""
    if scene is None:
        return None
    try:
        seq = int(getattr(scene, "scene_seq", 0) or 0)
    except (TypeError, ValueError):
        seq = 0
    last = bool(getattr(scene, "is_chapter_last", False))
    if seq == 1 and last:
        return POSITION_WHOLE
    if seq == 1:
        return POSITION_OPENING
    if last:
        return POSITION_CLOSING
    return None


def scene_rendering_mode(scene: Any) -> str | None:
    mode = str(_brief(scene).get("rendering_mode") or "").strip().lower()
    return mode or None


def scene_form(scene: Any) -> str:
    """proactive / reactive：显式声明优先，否则看填了哪一组三拍（默认 proactive）。"""
    brief = _brief(scene)
    for candidate in (brief.get("scene_form"), brief.get("primary_form"), getattr(scene, "scene_type", None)):
        value = str(candidate or "").strip().lower()
        if value in ("proactive", "reactive"):
            return value
    if any(str(brief.get(key) or "").strip() for key in ("reaction", "dilemma", "decision")) and not any(
        str(brief.get(key) or "").strip() for key in ("goal", "conflict", "setback")
    ):
        return "reactive"
    return "proactive"


def _onstage_others(scene: Any) -> list[str]:
    onstage = getattr(scene, "onstage_chars_json", None) or []
    pov = str(getattr(scene, "pov_character_id", "") or "")
    return [str(c) for c in onstage if str(c or "") and str(c) != pov]


def scene_dialogue_heavy(scene: Any) -> bool:
    """对白配额信号：非概述场，且台上除 POV 外还有人（或是主动场）→ 让一半窗口有对白。"""
    if scene is None:
        return False
    if scene_rendering_mode(scene) == "summary":
        return False
    return bool(_onstage_others(scene)) or scene_form(scene) == "proactive"


# 场面标签的关键词（按表序决平）。蓝图有模型给的 situation_tags 时不用这张表。
_SITUATION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("打斗追逐", ("打斗", "搏斗", "厮杀", "交手", "追杀", "追逐", "追捕", "逃跑", "逃命", "突围", "开枪", "枪战", "战斗", "拼命")),
    ("对峙审问", ("审问", "审讯", "逼问", "盘问", "质问", "对质", "对峙", "威胁", "谈判", "摊牌")),
    ("争吵冲突", ("争吵", "吵架", "争执", "翻脸", "指责", "顶嘴", "闹翻")),
    ("危机应对", ("危机", "危险", "爆炸", "失火", "灾难", "倒计时", "营救", "救人", "受伤", "险境", "失控", "坠落")),
    ("悬疑揭示", ("真相", "秘密", "揭开", "揭露", "线索", "谜团", "疑点", "隐瞒", "暴露")),
    ("计划商议", ("计划", "商量", "商议", "部署", "策划", "筹划", "开会", "对策")),
    ("回忆往事", ("回忆", "往事", "当年", "从前", "小时候", "旧事")),
    ("情感交流", ("告白", "表白", "倾诉", "安慰", "道歉", "和解", "拥抱", "心意", "告别", "思念")),
    ("喜剧桥段", ("好笑", "搞笑", "滑稽", "吐槽", "幽默", "诙谐", "哭笑不得", "出糗", "闹剧")),
    ("说明设定", ("讲解", "解释", "规则", "设定", "来历", "介绍")),
    ("赶路转场", ("赶路", "出发", "路上", "抵达", "旅途", "启程", "返程")),
    ("日常闲谈", ("闲聊", "闲谈", "聊天", "吃饭", "早餐", "午饭", "晚饭", "日常")),
)
_DESIGN_TEXT_KEYS = (
    "goal",
    "conflict",
    "setback",
    "reaction",
    "dilemma",
    "decision",
    "scene_crucible",
    "crucible",
    "expected_reader_emotion",
    "summary",
    "hook",
    "exit_change",
)


def derive_situation_tags(scene: Any) -> tuple[str, ...]:
    """没有蓝图场面标签时，从场景设计（形态、概述与否、台上人物、三拍、读者情绪）推 ≤3 个场面标签。

    只是兜底：关键词命中多的在前；反应场且台上只有 POV → 独处内省；台上 ≥4 人 → 群像场面；概述场 → 赶路转场。
    """
    if scene is None:
        return ()
    brief = _brief(scene)
    parts = [str(brief.get(key) or "") for key in _DESIGN_TEXT_KEYS]
    parts.append(str(getattr(scene, "scene_goal", "") or ""))
    beats = getattr(scene, "beats_json", None)
    if isinstance(beats, (list, tuple)):
        parts.extend(str(item or "") for item in beats)
    text = "\n".join(parts)
    scored: list[tuple[int, int, str]] = []
    for order, (tag, words) in enumerate(_SITUATION_KEYWORDS):
        hits = sum(text.count(word) for word in words)
        if hits:
            scored.append((-hits, order, tag))
    scored.sort()
    tags = [tag for _hits, _order, tag in scored]
    others = _onstage_others(scene)
    if scene_form(scene) == "reactive" and not others:
        tags.insert(0, "独处内省")
    if len(others) + 1 >= 4:
        tags.append("群像场面")
    if scene_rendering_mode(scene) == "summary":
        tags.append("赶路转场")
    return tuple(normalize_situation_tags(tags)[:MAX_SITUATIONS])


def target_dimensions(policy: Any, request: StyleRenderRequest) -> list[str]:
    """维度配额的目标维（按出现顺序去重）：绑定里的重点维 ∪ 改稿维 ∪ 近期常见偏差维；不学的维除外。"""
    states = dict(getattr(policy, "dimension_states", None) or {})
    dims = [dim for dim, state in states.items() if state == DIMENSION_EMPHASIZE]
    for dim in [*request.revise_dimensions, *gap_dimensions(request.recent_gaps)]:
        if dim in ALL_DIMENSIONS and dim not in dims:
            dims.append(dim)
    return [dim for dim in dims if states.get(dim) != DIMENSION_EXCLUDE]


# ---------------------------------------------------------------------------
# 读索引
# ---------------------------------------------------------------------------


def _dominant_type(type_mix: Any) -> str:
    if not isinstance(type_mix, Mapping) or not type_mix:
        return ""
    try:
        return str(max(type_mix, key=lambda name: float(type_mix[name] or 0.0)))
    except (TypeError, ValueError):
        return ""


def load_index(
    session: Session,
    book_id: str,
    *,
    build: bool = True,
    commit: bool = False,
) -> tuple[list[IndexWindow], str | None, tuple[str, ...]]:
    """(当前索引的窗口, 根哈希, 提示)。索引过期 / 缺失时 ``build=True`` 就地建（只 flush，``commit`` 由调用方定）。"""
    book = session.get(StyleReferenceBook, str(book_id))
    if book is None:
        return [], None, (NOTICE_BOOK_MISSING,)
    if not marker_is_current(book.stats_json):
        if not build:
            return [], None, (NOTICE_NO_WINDOWS,)
        ensure_window_index(session, str(book_id), commit=commit)
        book = session.get(StyleReferenceBook, str(book_id))
    marker = index_marker(book.stats_json if book is not None else None) or {}
    root = str(marker.get("root") or "") or None
    if root is None:
        return [], None, (NOTICE_NO_WINDOWS,)
    rows = session.execute(
        select(
            StyleReferenceWindow.window_no,
            StyleReferenceWindow.start_index,
            StyleReferenceWindow.end_index,
            StyleReferenceWindow.chapter_no,
            StyleReferenceWindow.position,
            StyleReferenceWindow.chars,
            StyleReferenceWindow.paragraph_count,
            StyleReferenceWindow.dialogue_share,
            StyleReferenceWindow.typicality,
            StyleReferenceWindow.tags_json,
            StyleReferenceWindow.type_mix_json,
        )
        .where(
            StyleReferenceWindow.book_id == str(book_id),
            StyleReferenceWindow.index_version == WINDOW_INDEX_VERSION,
            StyleReferenceWindow.root_sha256 == root,
        )
        .order_by(StyleReferenceWindow.window_no)
    ).all()
    windows: list[IndexWindow] = []
    for no, start, end, chapter, position, chars, paragraphs, dialogue, typical, tags, type_mix in rows:
        tags = tags if isinstance(tags, Mapping) else {}
        windows.append(
            IndexWindow(
                window_no=int(no),
                start=int(start),
                end=int(end),
                chapter=int(chapter or 0),
                position=str(position or ""),
                chars=int(chars or 0),
                paragraphs=int(paragraphs or 0),
                dialogue_share=float(dialogue or 0.0),
                typicality=float(typical or 0.0),
                situations=tuple(str(s) for s in tags.get("situations") or () if str(s or "").strip()),
                dimensions=_dimension_keys(tags.get("dimensions")),
                paragraph_type=_dominant_type(type_mix),
            )
        )
    notices: tuple[str, ...] = () if windows else (NOTICE_NO_WINDOWS,)
    return windows, root, notices


# ---------------------------------------------------------------------------
# 选窗（纯函数）
# ---------------------------------------------------------------------------


def typicality_weights(windows: Sequence[IndexWindow]) -> list[float]:
    """w = exp(λ · clip((t − 中位数) / 稳健尺度, ±3))：典型的窗更常被抽到，但全书每一窗都有机会。"""
    values = [float(w.typicality) for w in windows]
    if not values:
        return []
    center = statistics.median(values)
    mad = statistics.median(abs(v - center) for v in values) * 1.4826
    if mad <= 1e-9:
        mad = statistics.pstdev(values) if len(values) > 1 else 0.0
    if mad <= 1e-9:
        return [1.0 for _ in values]
    return [
        math.exp(TYPICALITY_WEIGHT_LAMBDA * max(-TYPICALITY_Z_CLIP, min(TYPICALITY_Z_CLIP, (v - center) / mad)))
        for v in values
    ]


def _rng(seed: str) -> random.Random:
    return random.Random(int(hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:16], 16))


def _sampling_keys(windows: Sequence[IndexWindow], seed: str) -> dict[int, float]:
    """按种子的加权抽样键（Efraimidis–Spirakis：log(u) / w，越大越先被抽到）。"""
    rng = _rng(seed)
    keys: dict[int, float] = {}
    for window, weight in zip(windows, typicality_weights(windows)):
        u = max(rng.random(), 1e-12)
        keys[window.window_no] = math.log(u) / max(weight, 1e-9)
    return keys


def selection_quotas(k: int, *, has_position: bool) -> dict[str, int]:
    """k=12 → 位置 3 / 场面 4 / 维度 2，其余按典型度；其它 k 按比例（位置 ≤3）。"""
    k = max(0, int(k))

    def _scaled(base: int) -> int:
        return int(math.floor(k * base / QUOTA_BASE_K + 0.5))

    return {
        "position": min(POSITION_QUOTA_MAX, int(math.ceil(k / 4))) if has_position and k > 0 else 0,
        "situation": _scaled(SITUATION_QUOTA_BASE),
        "dimension": _scaled(DIMENSION_QUOTA_BASE),
    }


def position_matches(window_position: str, scene_position: str | None) -> bool:
    if not scene_position or not window_position:
        return False
    if window_position == POSITION_WHOLE or scene_position == POSITION_WHOLE:
        return window_position in (POSITION_OPENING, POSITION_CLOSING, POSITION_WHOLE)
    return window_position == scene_position


def compute_selection(
    windows: Sequence[IndexWindow],
    *,
    k: int,
    seed: str,
    position: str | None = None,
    situation_tags: Sequence[str] = (),
    dimensions: Sequence[str] = (),
    dialogue_heavy: bool = False,
    rendering_mode: str | None = None,
) -> list[WindowRef]:
    """确定性选窗（纯函数，按选窗顺序返回）。窗数不足 k 的书用全部窗（J13）。"""
    k = max(0, int(k))
    if k <= 0 or not windows:
        return []
    keys = _sampling_keys(windows, seed)
    chosen: list[tuple[IndexWindow, str]] = []
    chosen_nos: set[int] = set()
    chapters: Counter[int] = Counter()

    def _overlaps(window: IndexWindow) -> bool:
        return any(window.start <= other.end + 1 and window.end >= other.start - 1 for other, _slot in chosen)

    def _eligible(window: IndexWindow, level: int) -> bool:
        if window.window_no in chosen_nos:
            return False
        if level == 0:
            return chapters[window.chapter_key] == 0
        if level == 1:
            return not _overlaps(window)
        return True

    def _take(candidates: Iterable[IndexWindow], n: int, slot: str, boost: Callable[[IndexWindow], float] | None = None) -> None:
        if n <= 0:
            return
        pool = list(candidates)
        if not pool:
            return
        # 加权键 log(u)/w 为负：乘上 >1 的匹配强度等于把它除以强度（更接近 0，更先被抽到）
        pool.sort(key=lambda w: (keys[w.window_no] / (boost(w) if boost else 1.0), -w.window_no), reverse=True)
        taken = 0
        for level in (0, 1, 2):
            for window in pool:
                if taken >= n or len(chosen) >= k:
                    return
                if _eligible(window, level):
                    chosen.append((window, slot))
                    chosen_nos.add(window.window_no)
                    chapters[window.chapter_key] += 1
                    taken += 1

    quotas = selection_quotas(k, has_position=bool(position))
    if position:
        _take((w for w in windows if position_matches(w.position, position)), quotas["position"], SLOT_POSITION)
    wanted_situations = set(situation_tags or ())
    if wanted_situations:
        already = sum(1 for w, _slot in chosen if wanted_situations & set(w.situations))
        _take(
            (w for w in windows if wanted_situations & set(w.situations)),
            quotas["situation"] - already,
            SLOT_SITUATION,
            boost=lambda w: 1.0 + 0.5 * (len(wanted_situations & set(w.situations)) - 1),
        )
    wanted_dimensions = set(dimensions or ())
    if wanted_dimensions:
        already = sum(1 for w, _slot in chosen if wanted_dimensions & set(w.dimensions))
        _take(
            (w for w in windows if wanted_dimensions & set(w.dimensions)),
            quotas["dimension"] - already,
            SLOT_DIMENSION,
            boost=lambda w: 1.0 + 0.5 * (len(wanted_dimensions & set(w.dimensions)) - 1),
        )
    half = int(math.ceil(k / 2))
    if str(rendering_mode or "") == "summary":
        have = sum(1 for w, _slot in chosen if w.dialogue_share <= NARRATION_WINDOW_SHARE)
        _take((w for w in windows if w.dialogue_share <= NARRATION_WINDOW_SHARE), half - have, SLOT_TEXTURE)
    elif dialogue_heavy:
        have = sum(1 for w, _slot in chosen if w.dialogue_share >= DIALOGUE_WINDOW_SHARE)
        _take((w for w in windows if w.dialogue_share >= DIALOGUE_WINDOW_SHARE), half - have, SLOT_TEXTURE)
    _take(windows, k - len(chosen), SLOT_TYPICAL)
    return [WindowRef.from_index(window, slot) for window, slot in chosen]


def selection_key(policy: Any, request: StyleRenderRequest, *, bundle_id: str | None = None) -> str:
    """冻结行的键：(bundle_id 或 "live", 契约哈希, scene_id, 版本)。``bundle_id`` 缺省取请求里的。"""
    material = "|".join(
        (
            str(bundle_id or request.bundle_id or "live"),
            str(getattr(policy, "contract_hash", "") or ""),
            str(request.scene_id or ""),
            SELECTION_VERSION,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def selection_seed(policy: Any, request: StyleRenderRequest) -> str:
    """种子只取场景（没有场景时取契约）：同一场在同一本书上每次都选出同一组窗。"""
    anchor = request.scene_id or f"contract:{getattr(policy, 'contract_hash', '') or 'none'}"
    return f"{anchor}|{getattr(policy, 'book_id', '') or ''}|{SELECTION_VERSION}"


def selection_inputs(policy: Any, request: StyleRenderRequest, *, scene: Any = None) -> dict[str, Any]:
    """选窗输入（只来自本场设计 + 绑定 + 请求里的改稿维 / 近期偏差，不看草稿）；请求没给场面标签时由场景设计推。"""
    tags = list(request.situation_tags) or list(derive_situation_tags(scene))
    return {
        "k": int(getattr(policy, "sample_windows", 0) or 0),
        "position": request.position,
        "situation_tags": tags,
        "dimensions": target_dimensions(policy, request),
        "dialogue_heavy": bool(request.dialogue_heavy),
        "rendering_mode": request.rendering_mode,
    }


# ---------------------------------------------------------------------------
# 冻结
# ---------------------------------------------------------------------------


def _refs_from_row(row: StyleReferenceSceneWindows) -> tuple[WindowRef, ...]:
    refs = []
    for item in row.window_refs_json or []:
        if isinstance(item, Mapping):
            ref = WindowRef.from_dict(item)
            if ref is not None:
                refs.append(ref)
    return tuple(refs)


def _store(
    session: Session,
    *,
    key: str,
    policy: Any,
    request: StyleRenderRequest,
    refs: Sequence[WindowRef],
    params: Mapping[str, Any],
    existing: StyleReferenceSceneWindows | None,
) -> tuple[str | None, tuple[WindowRef, ...], bool]:
    """(selection_id, 生效的 refs, 是否复用了别人刚写的行)。并发写同一键时读回先写成功的那一行。

    新建与覆盖（书改过、重选）都在保存点里写（L4）：写失败只回滚保存点，调用方的事务照常可用——原来覆盖分支
    在保存点外 flush、失败又被宽泛的 except 吞掉，下一次提交就是 ``PendingRollbackError``。调用方事务里别的未
    flush 的改动先照常 flush（它们的错误不是这里的错，原样抛出）。"""
    payload = [ref.to_dict() for ref in refs]
    session.flush()
    try:
        with session.begin_nested():
            if existing is not None:
                existing.window_refs_json = payload
                existing.params_json = dict(params)
                existing.contract_hash = getattr(policy, "contract_hash", None)
                row = existing
            else:
                row = StyleReferenceSceneWindows(
                    selection_id=f"srsel_{key[:24]}",
                    selection_key=key,
                    scene_id=request.scene_id,
                    bundle_id=request.bundle_id,
                    contract_hash=getattr(policy, "contract_hash", None),
                    window_refs_json=payload,
                    params_json=dict(params),
                    created_at=utcnow(),
                )
                session.add(row)
        return row.selection_id, tuple(refs), False
    except IntegrityError:
        winner = session.scalar(
            select(StyleReferenceSceneWindows).where(StyleReferenceSceneWindows.selection_key == key)
        )
        if winner is not None:
            return winner.selection_id, _refs_from_row(winner) or tuple(refs), True
        return None, tuple(refs), False
    except Exception:  # noqa: BLE001 — 冻结失败不阻断渲染（这一次按算出的窗走，下次确定性地再算出同一组）；保存点已回滚
        logger.warning("scene window selection could not be persisted", exc_info=True)
        return None, tuple(refs), False


def current_bundle_selection(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    *,
    root: str | None,
) -> tuple[StyleReferenceSceneWindows, tuple[WindowRef, ...]] | None:
    """这一场当前 bundle（``SceneRunState.current_bundle_id``）冻结的选窗——只在那一行的契约哈希就是这份策略的
    契约哈希、选窗时的索引根哈希就是现在的根哈希时才算数；否则 ``None``（调用方按 "live" 自己选窗）。只读。"""
    contract_hash = str(getattr(policy, "contract_hash", "") or "")
    if not contract_hash or not request.scene_id:
        return None
    try:
        state = session.get(SceneRunState, str(request.scene_id))
    except Exception:  # noqa: BLE001 — 读不到运行状态：按 live 选窗
        logger.debug("scene run state unavailable for %s", request.scene_id, exc_info=True)
        return None
    bundle_id = str(getattr(state, "current_bundle_id", "") or "") if state is not None else ""
    if not bundle_id:
        return None
    key = selection_key(policy, request, bundle_id=bundle_id)
    row = session.scalar(select(StyleReferenceSceneWindows).where(StyleReferenceSceneWindows.selection_key == key))
    if row is None or str(row.scene_id or "") != str(request.scene_id) or str(row.contract_hash or "") != contract_hash:
        return None
    refs = _refs_from_row(row)
    if not refs or dict(row.params_json or {}).get("root") != root:
        return None
    return row, refs


def resolve_scene_selection(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    *,
    scene: Any = None,
    persist: bool = True,
    build_index: bool = True,
    commit_index: bool = False,
) -> SceneSelection:
    """这一场的冻结选窗（按选窗顺序，k = 绑定的样例窗数）；有冻结行就读它，没有就算出来并冻结。

    ``persist=False``（预览）：只算不写。没有 ``scene_id`` 的调用（章级 / 项目级的写作台节点）不冻结——
    种子取契约哈希，同一契约每次算出同一组窗。没有 bundle 的调用先看这一场当前 bundle 冻结的选窗（M4，
    :func:`current_bundle_selection`）。
    """
    book_id = str(getattr(policy, "book_id", "") or "")
    k = int(getattr(policy, "sample_windows", 0) or 0)
    if not book_id or k <= 0:
        return SceneSelection(book_id=book_id or None)
    windows, root, notices = load_index(session, book_id, build=build_index, commit=commit_index)
    if not windows:
        return SceneSelection(book_id=book_id, root=root, notices=notices)
    if request.bundle_id is None and request.scene_id:
        shared = current_bundle_selection(session, policy, request, root=root)
        if shared is not None:
            row, refs = shared
            return SceneSelection(
                refs=refs,
                selection_id=row.selection_id,
                selection_key=row.selection_key,
                persisted=True,
                reused=True,
                book_id=book_id,
                root=root,
                window_count=len(windows),
                params=dict(row.params_json or {}),
                notices=notices,
                index=tuple(windows),
            )
    key = selection_key(policy, request) if request.scene_id else None
    existing: StyleReferenceSceneWindows | None = None
    if key is not None and persist:
        existing = session.scalar(
            select(StyleReferenceSceneWindows).where(StyleReferenceSceneWindows.selection_key == key)
        )
        if existing is not None:
            refs = _refs_from_row(existing)
            params = dict(existing.params_json or {})
            if refs and params.get("root") == root:
                return SceneSelection(
                    refs=refs,
                    selection_id=existing.selection_id,
                    selection_key=key,
                    persisted=True,
                    reused=True,
                    book_id=book_id,
                    root=root,
                    window_count=len(windows),
                    params=params,
                    notices=notices,
                    index=tuple(windows),
                )
            logger.info("scene window selection %s is stale (book changed); reselecting", existing.selection_id)
    inputs = selection_inputs(policy, request, scene=scene)
    refs = compute_selection(
        windows,
        k=k,
        seed=selection_seed(policy, request),
        position=inputs["position"],
        situation_tags=inputs["situation_tags"],
        dimensions=inputs["dimensions"],
        dialogue_heavy=inputs["dialogue_heavy"],
        rendering_mode=inputs["rendering_mode"],
    )
    params = {
        **inputs,
        "version": SELECTION_VERSION,
        # 书与画像的 id：删书 / 破坏式重分类按 book_id 清冻结行（S6）；画像 id 只给审计
        "book_id": book_id,
        "profile_id": str(getattr(policy, "profile_id", "") or "") or None,
        "root": root,
        "index_version": WINDOW_INDEX_VERSION,
        "index_window_count": len(windows),
    }
    selection_id: str | None = None
    persisted = False
    reused = False
    if key is not None and persist:
        selection_id, stored_refs, reused = _store(
            session, key=key, policy=policy, request=request, refs=refs, params=params, existing=existing
        )
        persisted = selection_id is not None
        refs = list(stored_refs)
    return SceneSelection(
        refs=tuple(refs),
        selection_id=selection_id,
        selection_key=key,
        persisted=persisted,
        reused=reused,
        book_id=book_id,
        root=root,
        window_count=len(windows),
        params=params,
        notices=notices,
        index=tuple(windows),
    )


def role_windows(policy: Any, request: StyleRenderRequest, selection: SceneSelection) -> list[WindowRef]:
    """这一次渲染用哪几窗：冻结选窗的前 k 窗（评审 4 / 规划 3 / 调用方上限）。

    改稿（``role=revise``，给了 ``revise_dimensions``）：把至多 :data:`REVISE_SWAP_MAX` 窗（从选窗顺序末尾——
    典型度补位的窗——开始换）换成示范这几维（窗口标签 ``dimensions`` 重合）、且不在本场选窗里的窗；换哪几窗
    由种子（scene_id + 维）确定。没有打过 v2 标签的窗时不换。
    """
    k = request.effective_k(getattr(policy, "sample_windows", 0))
    refs = list(selection.refs)[:k]
    if request.role != ROLE_REVISE or not request.revise_dimensions or not refs or not selection.index:
        return refs
    states = dict(getattr(policy, "dimension_states", None) or {})
    wanted = {d for d in request.revise_dimensions if states.get(d) != DIMENSION_EXCLUDE}
    if not wanted:
        return refs
    taken = {ref.window_no for ref in refs}
    candidates = [w for w in selection.index if w.window_no not in taken and wanted & set(w.dimensions)]
    if not candidates:
        return refs
    seed = f"{request.scene_id or 'none'}|revise|{','.join(sorted(request.revise_dimensions))}|{SELECTION_VERSION}"
    keys = _sampling_keys(candidates, seed)
    used_chapters = {ref.chapter for ref in refs if ref.chapter > 0}
    candidates.sort(
        key=lambda w: (
            w.chapter in used_chapters,
            -(keys[w.window_no] / (1.0 + 0.5 * (len(wanted & set(w.dimensions)) - 1))),
            w.window_no,
        )
    )
    swaps = candidates[: min(REVISE_SWAP_MAX, len(refs))]
    # 从选窗顺序末尾（优先级最低）开始换
    slots_to_replace = sorted(
        range(len(refs)),
        key=lambda i: (refs[i].slot not in (SLOT_TYPICAL, SLOT_TEXTURE), -i),
    )[: len(swaps)]
    for index, window in zip(slots_to_replace, swaps):
        refs[index] = WindowRef.from_index(window, SLOT_REVISE)
    return refs


def purge_scene_windows_for_book(session: Session, book_id: str) -> int:
    """删掉这本书的每场冻结选窗（``params_json.book_id``；只 flush，不 commit），返回删了几行。

    删书 / 破坏式重分类时由 ``cleanup.purge_derived_data`` 调用（S6）：书没了或段落全换，冻结的窗号与区间已经指不到
    原文；没有 ``book_id`` 的旧行（写入 ``profile_id`` / ``book_id`` 之前的）不动。
    """
    if not str(book_id or "").strip():
        return 0
    result = session.execute(
        delete(StyleReferenceSceneWindows).where(
            func.json_extract(StyleReferenceSceneWindows.params_json, "$.book_id") == str(book_id)
        )
    )
    session.flush()
    return int(result.rowcount or 0)


__all__ = [
    "DIALOGUE_WINDOW_SHARE",
    "EMPTY_SELECTION",
    "IndexWindow",
    "NARRATION_WINDOW_SHARE",
    "NOTICE_BOOK_MISSING",
    "NOTICE_NO_WINDOWS",
    "REVISE_SWAP_MAX",
    "SELECTION_VERSION",
    "SLOT_DIMENSION",
    "SLOT_POSITION",
    "SLOT_REVISE",
    "SLOT_SITUATION",
    "SLOT_TEXTURE",
    "SLOT_TYPICAL",
    "SceneSelection",
    "WindowRef",
    "compute_selection",
    "current_bundle_selection",
    "derive_situation_tags",
    "load_index",
    "position_matches",
    "purge_scene_windows_for_book",
    "resolve_scene_selection",
    "role_windows",
    "scene_chapter_position",
    "scene_dialogue_heavy",
    "scene_form",
    "scene_rendering_mode",
    "selection_inputs",
    "selection_key",
    "selection_quotas",
    "selection_seed",
    "target_dimensions",
    "typicality_weights",
]
