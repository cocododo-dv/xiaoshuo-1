"""节奏检查（贴邻叠句 / 段落偏长 / 句首重复）按参考作者校准：参考书上的三个读数与 ``CraftCalibration``。

规则维度那一半（词表密度、维度在场级窗口上响的比例）的读数在 ``literary_quality.calibration_source``；写作台读到的
``craft_calibration`` 载荷把两半挂在一起（``CraftCalibration.rules``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from novel_system.cache_registry import register_cache_reset
from novel_system.services.literary_quality import DEFAULT_RULE_CALIBRATION, RuleCalibration, dimension_label
from novel_system.services.literary_quality.calibration_source import rule_calibration_from_reference

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

    @property
    def note(self) -> str:
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
_REFERENCE_CRAFT_CACHE: dict[tuple[str, int, str], dict[str, Any]] = {}
register_cache_reset("scene_diagnosis.reference_craft", _REFERENCE_CRAFT_CACHE.clear)


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
