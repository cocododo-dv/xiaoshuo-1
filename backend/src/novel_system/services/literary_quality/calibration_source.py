"""规则维度按绑定的参考书校准——参考书那一侧的读数（审计 B04-21：从场景诊断搬进规则引擎的包）。

``calibration`` 管「拿到读数之后怎么判」（``RuleCalibration``、泊松尾 / Wilson 下界、按一稿校准词表）；这里管读数
本身：把参考书切成单元与场级窗口，量每条规则在窗口上响的比例、「命中即毛病」词表里每个词在书里每万字的密度
（``compute_reference_rules``），以及一场绑定的画像（``BoundProfile``：画像、它的书、「刻意复沓」标记）。
写作台深改面板、文学质量视图、成稿门用的是同一份；节奏检查的读数（段长、叠句、句首重复）另算，在
``scene_diagnosis.calibration``。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceProfile
from novel_system.services.literary_quality.calibration import (
    DEFAULT_RULE_CALIBRATION,
    RULE_ENDING_DIMENSIONS,
    RuleCalibration,
    dimension_level,
)
from novel_system.services.literary_quality.lexicons import FAULT_LEXICONS
from novel_system.services.literary_quality.rules import analyze_literary_quality
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
# 读库：绑定的画像
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


__all__ = [
    "RULE_CALIBRATION_MAX_ENDINGS",
    "RULE_CALIBRATION_MAX_WINDOWS",
    "RULE_CALIBRATION_MIN_ENDINGS",
    "RULE_CALIBRATION_MIN_WINDOWS",
    "RULE_CALIBRATION_WINDOW_CHARS",
    "TRANSITION_PARAGRAPH_TYPE",
    "BoundProfile",
    "bound_profile",
    "bound_profile_for_policy",
    "compute_reference_rules",
    "rule_calibration_from_reference",
]
