"""规则维度按绑定的参考书校准——参考书那一侧的读数从哪来（审计 B04-21：从场景诊断搬进规则引擎的包）。

``calibration`` 管「拿到读数之后怎么判」（``RuleCalibration``、泊松尾 / Wilson 下界、按一稿校准词表）；这里管读数
本身：把参考书切成单元与场级窗口，量每条规则在窗口上响的比例、「命中即毛病」词表里每个词在书里每万字的密度
（``compute_reference_rules``），读库拿一本书的段落与场界，按书的版本缓存读数（``rule_calibration_for_book`` /
``rule_calibration_for_policy``）。写作台深改面板、文学质量视图、成稿门用的是同一份；节奏检查的读数（段长、
叠句、句首重复）另算，在 ``scene_diagnosis.calibration``。

**缓存**（进程级，锁保护，最多 ``REFERENCE_STATS_BOOKS`` 本书——两部作品绑两本书不会互相挤掉）：键是书的版本
（``reference_book_state``）：段落文本的版本与抄袭闸共用（``paragraph_root.read_book_version``：书的统计里存着段落
根哈希时用它，写段落表的人负责把它 pop 掉，契约 §3.1；否则现数段数 / 总字数 / 最新段落时间；再加书的校验和与建书
时间），再加段型修订号（分类作业每次成功把 ``paragraph_types_revision`` 加一——就地重标段落类型不改文本，收尾
读数却按「转场段之前的那一段」取）与导入时记下的场界。同一个键第一次算时别的线程等它算完再取，不重复算（真实
参考书 2.6 万段：一次几秒）。

版本读一次库（一条按主键的查询，没存根哈希的书再聚合一遍段落）；一次请求里逐场调用时由调用方记住结果
（``SceneDiagnosisService`` 每个实例按画像记一份，成稿门与文学质量视图用 ``PolicyRuleCalibrations``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.cache_registry import register_cache_reset
from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph, StyleReferenceProfile
from novel_system.services.config_cache import ContentKeyedCache
from novel_system.services.literary_quality.calibration import (
    DEFAULT_RULE_CALIBRATION,
    RULE_ENDING_DIMENSIONS,
    RuleCalibration,
    dimension_level,
)
from novel_system.services.literary_quality.lexicons import FAULT_LEXICONS
from novel_system.services.literary_quality.rules import analyze_literary_quality
from novel_system.services.style_reference.paragraph_root import read_book_version
from novel_system.services.style_reference.segmentation.heuristic import is_title_paragraph
from novel_system.services.style_reference.text_utils import is_scene_break_paragraph

_LOGGER = logging.getLogger(__name__)

# 规则维度的校准：参考书按标题段 / 场分隔行 / 导入时记下的场界切成单元，单元内按 ~2400 字（一场的量）切窗口；
# 一般维度在最多 96 个窗口上量「响的比例」，收尾三条只在最多 96 个真实收尾（章末 / 场界，不够时补转场段之前的
# 那一段）上量；窗口 / 收尾都至少要 4 个才算数（更少的样本连 Wilson 下界也撑不起来）
RULE_CALIBRATION_WINDOW_CHARS = 2400
RULE_CALIBRATION_MAX_WINDOWS = 96
RULE_CALIBRATION_MAX_ENDINGS = 96
RULE_CALIBRATION_MIN_WINDOWS = 4
RULE_CALIBRATION_MIN_ENDINGS = 4
TRANSITION_PARAGRAPH_TYPE = "transition"
# 进程里同时记住几本书的读数（一本书的读数几百 KB）
REFERENCE_STATS_BOOKS = 4
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# 参考书上的读数（纯函数）
# ---------------------------------------------------------------------------


def _evenly(items: list[str], limit: int) -> list[str]:
    if len(items) <= limit:
        return list(items)
    step = len(items) / float(limit)
    return [items[int(index * step)] for index in range(limit)]


def _needle_rates(corpus: str, chars: int) -> dict[str, float]:
    """「命中即毛病」词表里每个词在参考书里每万字的次数（一遍正则；短词加上含它的长词的命中）。"""

    needles = sorted({term for terms in FAULT_LEXICONS.values() for term in terms if term.strip()}, key=len, reverse=True)
    if not needles or chars <= 0:
        return {}
    pattern = re.compile("|".join(re.escape(term.lower()) for term in needles))
    counts: Counter[str] = Counter(match.group(0) for match in pattern.finditer(corpus.lower()))
    rates: dict[str, float] = {}
    for term in needles:
        lowered = term.lower()
        count = counts.get(lowered, 0) + sum(hits for hit, hits in counts.items() if hit != lowered and lowered in hit)
        if count:
            rates[term] = round(10000.0 * count / chars, 3)
    return rates


def _reference_units(
    paragraphs: list[str],
    *,
    paragraph_types: list[str] | None,
    scene_breaks: Iterable[int] | None,
) -> tuple[list[list[str]], int, int]:
    """参考书切成单元：标题段 / 纯符号分隔行 / 导入时记下的场界（含空行分界）都是结构分界。
    结构分界太少时（不到 ``RULE_CALIBRATION_MIN_ENDINGS`` 个真实收尾）再按转场段补：分类器标为
    ``transition`` 的段之前的那一段也算一个收尾。返回 (单元, 结构分界数, 转场补充数)。"""

    types = list(paragraph_types or [])
    break_after = {int(index) for index in (scene_breaks or ()) if isinstance(index, int) and not isinstance(index, bool)}
    kept: list[tuple[int, str, str]] = []  # (原索引, 正文, 段型)
    for index, paragraph in enumerate(paragraphs):
        body = str(paragraph or "").strip()
        if body:
            kept.append((index, body, str(types[index] if index < len(types) else "") or ""))

    def cut(use_transitions: bool) -> tuple[list[list[str]], int, int]:
        units: list[list[str]] = []
        current: list[str] = []
        structural = 0
        transitional = 0
        for position, (index, body, paragraph_type) in enumerate(kept):
            if is_title_paragraph(body) or is_scene_break_paragraph(body):
                if current:
                    units.append(current)
                    current = []
                    structural += 1
                continue
            if use_transitions and paragraph_type == TRANSITION_PARAGRAPH_TYPE and current and position > 0:
                units.append(current)
                current = []
                transitional += 1
            current.append(body)
            if index in break_after:
                units.append(current)
                current = []
                structural += 1
        if current:
            units.append(current)
        return units, structural, transitional

    units, structural, _ = cut(False)
    if structural + 1 >= RULE_CALIBRATION_MIN_ENDINGS:
        return units, structural, 0
    with_transitions, structural_again, transitional = cut(True)
    if transitional:
        return with_transitions, structural_again, transitional
    return units, structural, 0


def compute_reference_rules(
    paragraphs: list[str],
    *,
    paragraph_types: list[str] | None = None,
    scene_breaks: Iterable[int] | None = None,
) -> dict[str, Any]:
    """参考书上的规则维度读数：词表词的密度（每万字）与每条规则在场级窗口上「响」的次数。

    参考书按 ``_reference_units`` 切成单元，单元内按 ~2400 字切窗口；收尾三条（summary_ending /
    ending_drive / false_poetic_closure）只在真实的单元末尾上量——随手切的窗口末尾不是收尾。
    ``endings_source`` 记收尾从哪来：``units``（章末 / 场界）、``units+transitions`` / ``transitions``
    （补了转场段之前的那一段）、``none``（不够 4 个真实收尾：收尾三条不校准）。
    """

    units, structural, transitional = _reference_units(paragraphs, paragraph_types=paragraph_types, scene_breaks=scene_breaks)
    corpus = "\n".join(paragraph for unit in units for paragraph in unit)
    chars = len(_WS_RE.sub("", corpus))
    empty = {"chars": 0, "windows": 0, "endings": 0, "endings_source": "none", "needle_rates": {}, "dimension_stats": {}, "dimension_shares": {}}
    if not chars:
        return empty

    windows: list[str] = []
    endings: list[str] = []
    for unit in units:
        chunks: list[str] = []
        buffer: list[str] = []
        size = 0
        for paragraph in unit:
            buffer.append(paragraph)
            size += len(paragraph)
            if size >= RULE_CALIBRATION_WINDOW_CHARS:
                chunks.append(" ".join(buffer))
                buffer, size = [], 0
        if buffer:
            if chunks and size < RULE_CALIBRATION_WINDOW_CHARS // 4:
                chunks[-1] = chunks[-1] + " " + " ".join(buffer)
            else:
                chunks.append(" ".join(buffer))
        if not chunks:
            continue
        endings.append(chunks[-1])
        windows.extend(chunks[:-1])
    if not windows:
        windows = list(endings)
    windows = _evenly(windows, RULE_CALIBRATION_MAX_WINDOWS)
    endings = _evenly(endings, RULE_CALIBRATION_MAX_ENDINGS)
    endings_usable = len(endings) >= RULE_CALIBRATION_MIN_ENDINGS
    if not endings_usable:
        endings_source = "none"
    elif transitional and structural:
        endings_source = "units+transitions"
    elif transitional:
        endings_source = "transitions"
    else:
        endings_source = "units"

    stats: dict[str, dict[str, int]] = {}
    if len(windows) >= RULE_CALIBRATION_MIN_WINDOWS:
        fired: Counter[str] = Counter()
        for window in windows:
            _, findings = analyze_literary_quality(window)
            for dimension in {str(item.get("dimension") or "") for item in findings}:
                if dimension and dimension not in RULE_ENDING_DIMENSIONS:
                    fired[dimension] += 1
        stats.update({dimension: {"fired": count, "n": len(windows)} for dimension, count in fired.items()})
    if endings_usable:
        ending_fired: Counter[str] = Counter()
        for window in endings:
            _, findings = analyze_literary_quality(window)
            for dimension in {str(item.get("dimension") or "") for item in findings}:
                if dimension in RULE_ENDING_DIMENSIONS:
                    ending_fired[dimension] += 1
        stats.update({dimension: {"fired": count, "n": len(endings)} for dimension, count in ending_fired.items()})
    return {
        "chars": chars,
        "windows": len(windows),
        "endings": len(endings) if endings_usable else 0,
        "endings_source": endings_source,
        "needle_rates": _needle_rates(corpus, chars),
        "dimension_stats": stats,
        "dimension_shares": {dimension: round(item["fired"] / item["n"], 3) for dimension, item in stats.items() if item["n"]},
    }


def rule_calibration_from_reference(stats: dict[str, Any] | None, *, deliberate_repetition: bool = False) -> RuleCalibration:
    if not stats or not int(stats.get("chars") or 0):
        return DEFAULT_RULE_CALIBRATION
    rates = {str(key): float(value) for key, value in (stats.get("needle_rates") or {}).items()}
    dimension_stats: dict[str, dict[str, Any]] = {}
    for dimension, item in (stats.get("dimension_stats") or {}).items():
        fired = int((item or {}).get("fired") or 0)
        total = int((item or {}).get("n") or 0)
        if total <= 0:
            continue
        level, lower = dimension_level(fired, total)
        dimension_stats[str(dimension)] = {
            "fired": fired,
            "n": total,
            "share": round(fired / total, 3),
            "lower_bound": round(lower, 3),
            "level": level,
        }
    return RuleCalibration(
        source="reference",
        needle_rates=rates,
        dimension_stats=dimension_stats,
        deliberate_repetition=bool(deliberate_repetition),
        windows=int(stats.get("windows") or 0),
        endings=int(stats.get("endings") or 0),
        endings_source=str(stats.get("endings_source") or "none"),
        chars=int(stats.get("chars") or 0),
    )


# ---------------------------------------------------------------------------
# 读库：绑定的画像、书的版本、书的段落
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundProfile:
    """一场绑定的（最具体那一层的）画像——只带校准要用的三样，不加载整个 profile_json（几十万字的窗口索引）。"""

    profile_id: str
    book_id: str | None
    deliberate_repetition: bool = False


class _PolicyLike(Protocol):
    """``style_policy.StylePolicy`` 里校准要读的三样（这里不 import style_policy：它在本包之上）。"""

    bound: bool
    profile_id: str | None
    book_id: str | None


def bound_profile(session: Session, profile_id: str) -> BoundProfile:
    """画像 → 它的书与「刻意复沓」标记：只读两列（``json_extract``），不加载整份 profile_json。"""

    try:
        row = session.execute(
            select(
                StyleReferenceProfile.book_id,
                func.json_extract(StyleReferenceProfile.profile_json, "$.voice_signature.deliberate_repetition"),
            ).where(StyleReferenceProfile.profile_id == profile_id)
        ).first()
        if row is not None:
            return BoundProfile(profile_id=profile_id, book_id=row[0], deliberate_repetition=bool(row[1]) and str(row[1]) not in {"0", "false"})
    except Exception:  # noqa: BLE001 — 没有 json_extract 的库：退回整行
        _LOGGER.warning("light profile lookup failed for %s; loading the whole profile row", profile_id, exc_info=True)
    profile = session.get(StyleReferenceProfile, profile_id)
    if profile is None:
        return BoundProfile(profile_id=profile_id, book_id=None)
    voice = (profile.profile_json or {}).get("voice_signature") if isinstance(profile.profile_json, dict) else None
    return BoundProfile(
        profile_id=profile_id,
        book_id=profile.book_id,
        deliberate_repetition=bool(voice.get("deliberate_repetition")) if isinstance(voice, dict) else False,
    )


def bound_profile_for_policy(session: Session, policy: _PolicyLike) -> BoundProfile | None:
    """策略绑定的画像（校准要用的三样）；未绑定 → None。书以策略为准（冻结契约记下的那本）。"""

    if not policy.bound or not policy.profile_id:
        return None
    profile = bound_profile(session, policy.profile_id)
    return BoundProfile(
        profile_id=profile.profile_id,
        book_id=policy.book_id or profile.book_id,
        deliberate_repetition=profile.deliberate_repetition,
    )


@dataclass(frozen=True)
class ReferenceBookState:
    """一本参考书现在的样子：缓存键（``version``）、段落数、书名（校准说明里要）。"""

    book_id: str
    title: str | None
    paragraphs: int
    version: tuple[Any, ...]


def reference_book_state(session: Session, book_id: str) -> ReferenceBookState | None:
    """书的版本（读数缓存的键）。书不存在 → None；没有段落时 ``paragraphs == 0``。

    段落文本的版本与抄袭闸共用一次读库（``paragraph_root.read_book_version``：存着段落根哈希时不数段落表，没存时现数
    段数 / 总字数 / 最新段落时间，同一个书号删了重导入也认得出来）；这里再加上段型修订号与导入时记下的场界——就地
    重标段型不改文本，收尾读数却按「转场段之前的那一段」取。"""

    version = read_book_version(session, book_id)
    if version is None:
        return None
    breaks_digest = hashlib.sha1(str(version.scene_breaks or "").encode("utf-8")).hexdigest()[:12]
    return ReferenceBookState(
        book_id=book_id,
        title=version.title,
        paragraphs=version.paragraphs,
        version=(*version.text_key, str(version.types_revision or 0), breaks_digest),
    )


@dataclass(frozen=True)
class ReferenceCorpus:
    """一本书的段落正文、段型与导入时记下的场界（「其后有场界」的段落索引）。"""

    texts: list[str]
    types: list[str]
    scene_breaks: list[int] | None


def load_reference_corpus(session: Session, book_id: str) -> ReferenceCorpus:
    rows = session.execute(
        select(StyleReferenceParagraph.text, StyleReferenceParagraph.paragraph_type)
        .where(StyleReferenceParagraph.book_id == book_id)
        .order_by(StyleReferenceParagraph.paragraph_index.asc())
    ).all()
    raw_breaks = session.execute(
        select(func.json_extract(StyleReferenceBook.stats_json, "$.scene_breaks")).where(StyleReferenceBook.book_id == book_id)
    ).scalar_one_or_none()
    scene_breaks: list[int] | None = None
    if isinstance(raw_breaks, str) and raw_breaks.strip().startswith("["):
        try:
            parsed = json.loads(raw_breaks)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            scene_breaks = [item for item in parsed if isinstance(item, int) and not isinstance(item, bool)]
    return ReferenceCorpus(
        texts=[str(row[0] or "") for row in rows],
        types=[str(row[1] or "") for row in rows],
        scene_breaks=scene_breaks,
    )


class CorpusLoader:
    """一次请求里一本书的段落只读一遍：规则读数与节奏读数都没缓存时，两边共用这一份。"""

    def __init__(self, session: Session, book_id: str) -> None:
        self._session = session
        self._book_id = book_id
        self._corpus: ReferenceCorpus | None = None

    def __call__(self) -> ReferenceCorpus:
        if self._corpus is None:
            self._corpus = load_reference_corpus(self._session, self._book_id)
        return self._corpus


# ---------------------------------------------------------------------------
# 按书的版本缓存的读数
# ---------------------------------------------------------------------------


class ReferenceStatsCache:
    """按（书、版本）记住读数的小容量 LRU（``ContentKeyedCache``）：同一个键第一次算时别的线程等它算完再取，
    不重复算；不同的键互不等待。只记成功算出的读数。"""

    def __init__(self, *, maxsize: int = REFERENCE_STATS_BOOKS) -> None:
        self._entries = ContentKeyedCache(maxsize=maxsize)
        self._guard = threading.Lock()
        self._building: dict[Hashable, threading.Lock] = {}

    @property
    def builds(self) -> int:
        return self._entries.builds

    def get_or_build(self, key: Hashable, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._guard:
            lock = self._building.setdefault(key, threading.Lock())
        with lock:
            try:
                return self._entries.get_or_build(key, build)
            finally:
                with self._guard:
                    if self._building.get(key) is lock:
                        self._building.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_RULE_STATS = ReferenceStatsCache()
register_cache_reset("literary_quality.calibration_source.rule_stats", _RULE_STATS.clear)


def reference_rule_stats(
    session: Session,
    state: ReferenceBookState,
    *,
    corpus: Callable[[], ReferenceCorpus] | None = None,
) -> dict[str, Any]:
    """这本书（这个版本）的规则读数；``corpus`` 给了就从它取段落（与节奏读数共用一次读库）。"""

    load = corpus or CorpusLoader(session, state.book_id)

    def build() -> dict[str, Any]:
        loaded = load()
        return compute_reference_rules(loaded.texts, paragraph_types=loaded.types, scene_breaks=loaded.scene_breaks)

    return _RULE_STATS.get_or_build((state.book_id, state.version), build)


def rule_calibration_for_book(
    session: Session,
    book_id: str | None,
    *,
    deliberate_repetition: bool = False,
    state: ReferenceBookState | None = None,
    corpus: Callable[[], ReferenceCorpus] | None = None,
) -> RuleCalibration:
    """按这本参考书校准的规则维度；书不存在或没有段落 → 房风默认（不校准）。"""

    if not book_id:
        return DEFAULT_RULE_CALIBRATION
    state = state if state is not None else reference_book_state(session, book_id)
    if state is None or state.paragraphs <= 0:
        return DEFAULT_RULE_CALIBRATION
    stats = reference_rule_stats(session, state, corpus=corpus)
    return rule_calibration_from_reference(stats, deliberate_repetition=deliberate_repetition)


def rule_calibration_for_policy(session: Session, policy: _PolicyLike) -> RuleCalibration | None:
    """成稿门 / 文学质量视图用：按策略绑定的书校准的规则维度；未绑定 / 校准不可用 → None。"""

    profile = bound_profile_for_policy(session, policy)
    if profile is None:
        return None
    rules = rule_calibration_for_book(session, profile.book_id, deliberate_repetition=profile.deliberate_repetition)
    return rules if rules.active else None


class PolicyRuleCalibrations:
    """一次请求里按策略取规则校准（文学质量视图逐场调用）：同一份画像、同一本书只查一次库、只取一次读数；
    校准读不出按未校准处理（None），不让调用方失败。"""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._memo: dict[tuple[str, str | None], RuleCalibration | None] = {}

    def for_policy(self, policy: _PolicyLike) -> RuleCalibration | None:
        if not policy.bound or not policy.profile_id:
            return None
        key = (str(policy.profile_id), policy.book_id)
        if key not in self._memo:
            try:
                self._memo[key] = rule_calibration_for_policy(self._session, policy)
            except Exception:  # noqa: BLE001 — 校准读不出：按未校准处理
                _LOGGER.warning("rule calibration unavailable for profile %s", policy.profile_id, exc_info=True)
                self._memo[key] = None
        return self._memo[key]


__all__ = [
    "RULE_CALIBRATION_MAX_ENDINGS",
    "RULE_CALIBRATION_MAX_WINDOWS",
    "RULE_CALIBRATION_MIN_ENDINGS",
    "RULE_CALIBRATION_MIN_WINDOWS",
    "RULE_CALIBRATION_WINDOW_CHARS",
    "REFERENCE_STATS_BOOKS",
    "TRANSITION_PARAGRAPH_TYPE",
    "BoundProfile",
    "CorpusLoader",
    "PolicyRuleCalibrations",
    "ReferenceBookState",
    "ReferenceCorpus",
    "ReferenceStatsCache",
    "bound_profile",
    "bound_profile_for_policy",
    "compute_reference_rules",
    "load_reference_corpus",
    "reference_book_state",
    "reference_rule_stats",
    "rule_calibration_for_book",
    "rule_calibration_for_policy",
    "rule_calibration_from_reference",
]
