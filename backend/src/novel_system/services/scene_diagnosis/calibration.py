"""节奏检查（贴邻叠句 / 段落偏长 / 句首重复）按参考作者校准：参考书上的三个读数与 ``CraftCalibration``。

规则维度那一半（词表密度、维度在场级窗口上响的比例）的读数在 ``literary_quality.calibration_source``；写作台读到的
``craft_calibration`` 载荷把两半挂在一起（``CraftCalibration.rules``）。两半的读数按书的版本分别缓存在进程里
（``calibration_source.ReferenceStatsCache``：锁保护、最多几本书、就地重标段落类型后重算），都没缓存时共用一次读库。
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.cache_registry import register_cache_reset
from novel_system.db.models import StyleReferenceInjectionBinding, StyleReferenceProfile
from novel_system.services.literary_quality import DEFAULT_RULE_CALIBRATION, RuleCalibration, dimension_label
from novel_system.services.literary_quality.calibration_source import (
    BoundProfile,
    CorpusLoader,
    ReferenceBookState,
    ReferenceCorpus,
    ReferenceStatsCache,
    bound_profile,
    reference_book_state,
    reference_rule_stats,
    rule_calibration_from_reference,
)
from novel_system.services.maintenance import register_maintenance_task

_LOGGER = logging.getLogger(__name__)

CRAFT_LONG_PARAGRAPH_CHARS = 170
# 参考作者的习惯：每千段里贴邻叠句 / 三句同字开头的段落数到了这个水平，就是这位作者的手法，不提示
CRAFT_ECHO_HABIT_PER_1K = 5.0
CRAFT_SAME_OPENING_HABIT_PER_1K = 10.0
ECHO_RE = re.compile(r"([一-龥]{2,5})([，、；]?)\1")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")


@dataclass(frozen=True)
class CraftCalibration:
    source: str = "default"  # default | reference
    profile_id: str | None = None
    book_id: str | None = None
    book_title: str | None = None
    paragraphs: int = 0
    long_paragraph_chars: int = CRAFT_LONG_PARAGRAPH_CHARS
    echo_per_1k: float | None = None
    same_opening_per_1k: float | None = None
    flag_echo: bool = True
    flag_same_opening: bool = True
    deliberate_repetition: bool = False
    # 2026-09-22 第三轮：规则维度的词表 / 维度校准也挂在这里（写作台读的是同一个 craft_calibration 载荷）
    rules: RuleCalibration = DEFAULT_RULE_CALIBRATION

    @cached_property
    def note(self) -> str:
        # 一份校准在一次请求里给每一场的载荷都写一遍说明；规则那一半的 as_dict 不便宜，算一次记住（冻结的数据类，
        # 字段不会再变）
        if self.source != "reference":
            return ""
        title = f"《{self.book_title}》" if self.book_title else "参考书"
        parts = [f"段落超过 {self.long_paragraph_chars} 字才提示"]
        habits: list[str] = []
        if not self.flag_echo:
            habits.append("贴邻叠句")
        if not self.flag_same_opening:
            habits.append("句首重复")
        if habits:
            reason = "这位作者刻意用重复" if self.deliberate_repetition else "这位作者常这么写"
            parts.append(f"{' / '.join(habits)}不提示（{reason}）")
        if self.rules.active:
            rules = self.rules.as_dict()
            if rules["top_needles"]:
                sample = "、".join(f"{item['term']} {item['per_10k']:g}" for item in rules["top_needles"][:4])
                parts.append(f"词表词按这位作者的密度判（每万字：{sample}…），寻常用法不当毛病")
            if self.rules.habitual_dimensions:
                labels = [dimension_label(item) or item for item in sorted(self.rules.habitual_dimensions)]
                parts.append(f"「{' / '.join(labels[:4])}{'…' if len(labels) > 4 else ''}」是这位作者的常态，只作提示")
            if self.rules.common_dimensions:
                labels = [dimension_label(item) or item for item in sorted(self.rules.common_dimensions)]
                parts.append(f"「{' / '.join(labels[:4])}{'…' if len(labels) > 4 else ''}」在这位作者的场里也常见，按审美看")
            if self.rules.endings_source == "none":
                parts.append("参考书没有章节与场的分界，收尾三条没有校准")
            elif self.rules.endings_source in {"transitions", "units+transitions"}:
                parts.append(f"收尾三条按 {self.rules.endings} 个真实收尾校准（含转场段之前的那一段）")
        return f"按{title}校准：{'；'.join(parts)}。"

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._rendered)

    @cached_property
    def _rendered(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "profile_id": self.profile_id,
            "book_id": self.book_id,
            "book_title": self.book_title,
            "paragraphs": self.paragraphs,
            "long_paragraph_chars": self.long_paragraph_chars,
            "echo_per_1k": self.echo_per_1k,
            "same_opening_per_1k": self.same_opening_per_1k,
            "flag_echo": self.flag_echo,
            "flag_same_opening": self.flag_same_opening,
            "deliberate_repetition": self.deliberate_repetition,
            "rules": self.rules.as_dict() if self.rules.active else None,
            "note": self.note,
        }


DEFAULT_CRAFT_CALIBRATION = CraftCalibration()
# 节奏读数按（书、版本）缓存在进程里；规则读数的那一份在 calibration_source
_CRAFT_STATS = ReferenceStatsCache()
register_cache_reset("scene_diagnosis.reference_craft", _CRAFT_STATS.clear)


def same_opening_hit(paragraph: str) -> dict[str, Any] | None:
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(paragraph) if part.strip()]
    for offset in range(len(sentences) - 2):
        heads = [sentence.strip()[:1] for sentence in sentences[offset : offset + 3]]
        if heads[0] and heads[0] == heads[1] == heads[2]:
            span_text = "".join(sentences[offset : offset + 3])
            start = paragraph.find(span_text)
            return {"head": heads[0], "span_text": span_text, "start": start}
    return None


def compute_reference_craft(paragraphs: list[str]) -> dict[str, Any]:
    """参考书段落表上的三个节奏读数：段长 p95、每千段贴邻叠句数、每千段三句同字开头数。"""

    bodies = [str(paragraph or "").strip() for paragraph in paragraphs]
    bodies = [body for body in bodies if body]
    count = len(bodies)
    if not count:
        return {"paragraphs": 0, "long_paragraph_p95": 0, "echo_per_1k": 0.0, "same_opening_per_1k": 0.0}
    lengths = sorted(len(body) for body in bodies)
    p95 = lengths[min(count - 1, int(round(0.95 * (count - 1))))]
    echo = sum(1 for body in bodies if ECHO_RE.search(body))
    same_opening = sum(1 for body in bodies if same_opening_hit(body) is not None)
    return {
        "paragraphs": count,
        "long_paragraph_p95": int(p95),
        "echo_per_1k": round(1000.0 * echo / count, 2),
        "same_opening_per_1k": round(1000.0 * same_opening / count, 2),
    }


def calibration_from_reference(
    *,
    profile_id: str | None,
    book_id: str | None,
    book_title: str | None,
    stats: dict[str, Any],
    deliberate_repetition: bool,
    rule_stats: dict[str, Any] | None = None,
) -> CraftCalibration:
    echo_rate = float(stats.get("echo_per_1k") or 0.0)
    opening_rate = float(stats.get("same_opening_per_1k") or 0.0)
    return CraftCalibration(
        source="reference",
        profile_id=profile_id,
        book_id=book_id,
        book_title=book_title,
        paragraphs=int(stats.get("paragraphs") or 0),
        long_paragraph_chars=max(CRAFT_LONG_PARAGRAPH_CHARS, int(stats.get("long_paragraph_p95") or 0)),
        echo_per_1k=echo_rate,
        same_opening_per_1k=opening_rate,
        flag_echo=not deliberate_repetition and echo_rate < CRAFT_ECHO_HABIT_PER_1K,
        flag_same_opening=not deliberate_repetition and opening_rate < CRAFT_SAME_OPENING_HABIT_PER_1K,
        deliberate_repetition=deliberate_repetition,
        rules=rule_calibration_from_reference(rule_stats, deliberate_repetition=deliberate_repetition),
    )


def reference_craft_stats(
    session: Session,
    state: ReferenceBookState,
    *,
    corpus: Callable[[], ReferenceCorpus] | None = None,
) -> dict[str, Any]:
    """这本书（这个版本）的节奏读数；``corpus`` 给了就从它取段落（与规则读数共用一次读库）。"""

    load = corpus or CorpusLoader(session, state.book_id)
    return _CRAFT_STATS.get_or_build((state.book_id, state.version), lambda: compute_reference_craft(load().texts))


def craft_calibration_for(session: Session, profile: BoundProfile | None) -> CraftCalibration:
    """按绑定画像的参考书校准节奏检查与规则维度（真实参考书 2.6 万段：节奏读数约 1 s、规则读数约 0.6 s，
    每个进程每本书的每个版本算一次）。没有绑定 / 书没有段落 / 读不出来 → 房风默认（读不出来记 warning，
    不让诊断失败）。"""

    if profile is None or not profile.book_id:
        return DEFAULT_CRAFT_CALIBRATION
    try:
        state = reference_book_state(session, profile.book_id)
        if state is None or state.paragraphs <= 0:
            return DEFAULT_CRAFT_CALIBRATION
        corpus = CorpusLoader(session, profile.book_id)
        stats = reference_craft_stats(session, state, corpus=corpus)
        rule_stats = reference_rule_stats(session, state, corpus=corpus)
        return calibration_from_reference(
            profile_id=profile.profile_id,
            book_id=profile.book_id,
            book_title=state.title,
            stats=stats,
            deliberate_repetition=bool(profile.deliberate_repetition),
            rule_stats=rule_stats,
        )
    except Exception:  # noqa: BLE001 — 校准失败退回默认阈值，不让诊断失败
        _LOGGER.warning("reference calibration unavailable for book %s; using the default thresholds", profile.book_id, exc_info=True)
        return DEFAULT_CRAFT_CALIBRATION


# ---------------------------------------------------------------------------
# 后台预热：绑定着的参考书的两份读数（X01-04）
# ---------------------------------------------------------------------------

REFERENCE_CALIBRATION_WARMUP_TASK = "reference_calibration_warmup"
REFERENCE_CALIBRATION_WARMUP_INTERVAL_SECONDS = 6 * 3600


def bound_profile_ids(session: Session) -> list[str]:
    """活动绑定指着的 active 画像（去重，按画像号排）。"""
    rows = session.execute(
        select(StyleReferenceInjectionBinding.profile_id)
        .join(StyleReferenceProfile, StyleReferenceProfile.profile_id == StyleReferenceInjectionBinding.profile_id)
        .where(StyleReferenceInjectionBinding.status == "active", StyleReferenceProfile.status == "active")
        .distinct()
    ).scalars().all()
    return sorted(str(row) for row in rows)


def warm_reference_calibrations() -> int:
    """全系统维护任务：绑定着的每本参考书，把规则与节奏两份读数算进进程缓存（按书的版本；真实参考书冷算要好几秒，
    2026-10-01 在 perf 副本上量到规则校准冷 8.0 s、整本诊断汇总冷 12.2 s / 热 0.2 s）——写作台深改面板、文学质量与
    成稿门读这本书的校准时就不必现算。书改过（重标段型、重新学习）之后下一轮把新版本算好。

    只读库、只填进程里的缓存，不写任何东西（读路径从不写持久化的读数）。登记时 ``run_at_start=False``、每 6 小时
    一次：后端热加载新代码后的第一拍不做这件重活。返回这一轮算到参考书读数的画像数。"""
    from novel_system.db.session import SessionLocal

    warmed = 0
    with SessionLocal() as session:
        for profile_id in bound_profile_ids(session):
            if craft_calibration_for(session, bound_profile(session, profile_id)).source == "reference":
                warmed += 1
        session.rollback()
    return warmed


register_maintenance_task(
    REFERENCE_CALIBRATION_WARMUP_TASK,
    warm_reference_calibrations,
    interval_seconds=REFERENCE_CALIBRATION_WARMUP_INTERVAL_SECONDS,
    run_at_start=False,
)
