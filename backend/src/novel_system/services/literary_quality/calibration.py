"""按绑定的参考书校准规则维度（2026-09-22 第三轮）：泊松尾概率 / Wilson 下界、``RuleCalibration``、按一稿
校准「命中即毛病」的词表、按参考作者的常态给发现降级。参考书那一侧的统计（场级窗口、词频）由场景诊断算好传进来。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novel_system.services.literary_quality.lexicons import FAULT_LEXICONS


# ---------------------------------------------------------------------------
# 2026-09-22 第三轮：按参考书校准规则维度（词表 + 维度）
# ---------------------------------------------------------------------------

RULE_NEEDLE_TAIL_ALPHA = 0.10           # 词表词：稿子里的次数在参考作者的密度下出现概率 < 1/10 才算反常（照提示）


RULE_JUDGE_WINDOW_CHARS = 2400          # 按「一场的量」判：稿子短于这个数也按这个量算期望（校准量的就是这种窗口）


RULE_DIMENSION_HABIT_SHARE = 0.5        # 规则在参考作者一半以上的场级窗口上都响（八成把握的下界）：这位作者的常态 → info


RULE_DIMENSION_COMMON_SHARE = 0.25      # 四分之一以上：这位作者也常见 → taste


RULE_CALIBRATION_CONFIDENCE_Z = 1.2816  # 80% 单侧置信的 Wilson 下界：窗口少的书要观察到更高的比例才算数


RULE_ENDING_DIMENSIONS: frozenset[str] = frozenset({"summary_ending", "ending_drive", "false_poetic_closure"})


RULE_REPETITION_DIMENSIONS: frozenset[str] = frozenset(
    {"repetitive_action", "template_action_reuse", "self_repetition", "image_homogeneity", "image_field_reuse"}
)


_SEVERITY_ORDER: tuple[str, ...] = ("blocking", "revision", "taste", "info")


def poisson_tail(count: int, expected: float) -> float:
    """P(X ≥ count)，X ~ Poisson(expected)：稿子里出现 ``count`` 次在参考作者的密度下有多不寻常。"""

    if count <= 0:
        return 1.0
    if expected <= 0:
        return 0.0
    log_expected = math.log(expected)
    cdf = 0.0
    for index in range(int(count)):
        cdf += math.exp(index * log_expected - expected - math.lgamma(index + 1))
    return max(0.0, min(1.0, 1.0 - cdf))


def wilson_lower_bound(fired: int, total: int, *, z: float = RULE_CALIBRATION_CONFIDENCE_Z) -> float:
    """比例的 Wilson 下界：``total`` 越小，同样的观测比例给出的下界越低（样本少就别轻易说「常态」）。"""

    if total <= 0:
        return 0.0
    share = max(0.0, min(1.0, fired / total))
    zz = (z * z) / total
    center = (share + zz / 2.0) / (1.0 + zz)
    half = (z / (1.0 + zz)) * math.sqrt(share * (1.0 - share) / total + zz / (4.0 * total))
    return max(0.0, center - half)


def dimension_level(fired: int, total: int) -> tuple[str | None, float]:
    """一条规则在参考书窗口上响的比例 → ``habit``（常态）/ ``common``（常见）/ None，按 Wilson 下界定档。"""

    lower = wilson_lower_bound(fired, total)
    if lower >= RULE_DIMENSION_HABIT_SHARE:
        return "habit", lower
    if lower >= RULE_DIMENSION_COMMON_SHARE:
        return "common", lower
    return None, lower


@dataclass(frozen=True)
class RuleCalibration:
    """按绑定的参考书校准规则维度（QUALITY_DIMENSIONS）。阈值不是定值：

    * **词表词**（只校准「命中即毛病」的词表 ``FAULT_LEXICONS``；抉择 / 压力 / 代价 / 收尾动作这些「缺席才是
      毛病」的词表不动）：记参考作者每万字用某个词的次数 ``needle_rates``；诊断一稿时按这位作者的密度算这个词
      在一场的量（至少 ``RULE_JUDGE_WINDOW_CHARS`` 字）里的期望次数，稿子里的次数在泊松分布下出现概率 ≥
      ``RULE_NEEDLE_TAIL_ALPHA`` 就是这位作者的寻常用法，不提示；概率更小（作者从不用的词，或用得比作者密得多）
      才照提示。没有「每万字 N 次」这样的定值——判的是这一稿这一个词。
    * **维度**：每条规则在参考书场级窗口上响的比例，按 80% 置信的 Wilson 下界定档——下界 ≥ 一半是这位作者的
      常态（发现降为 ``info``），≥ 四分之一是常见（降为 ``taste``）；窗口少的书要观察到更高的比例才算数。
      收尾三条只在真实的收尾（章末 / 场界 / 转场前）上量。画像标了 ``deliberate_repetition`` 时，重复一族的规则
      （``RULE_REPETITION_DIMENSIONS``）不看比例，直接是常态——那是画像说的。
    """

    source: str = "default"  # default | reference
    needle_rates: Mapping[str, float] = field(default_factory=dict)                 # 每万字次数（只记 > 0 的）
    dimension_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # dim → {fired, n, share, lower_bound, level}
    deliberate_repetition: bool = False
    windows: int = 0
    endings: int = 0
    endings_source: str = "none"  # units | units+transitions | transitions | none
    chars: int = 0

    @property
    def active(self) -> bool:
        return self.source == "reference" and bool(self.needle_rates or self.dimension_stats or self.deliberate_repetition)

    def level_for(self, dimension: str) -> str | None:
        stats = self.dimension_stats.get(dimension) or {}
        level = stats.get("level")
        if level == "habit":
            return "habit"
        if self.deliberate_repetition and dimension in RULE_REPETITION_DIMENSIONS:
            return "habit"
        return "common" if level == "common" else None

    @property
    def habitual_dimensions(self) -> frozenset[str]:
        dims = {dim for dim in self.dimension_stats if self.level_for(dim) == "habit"}
        if self.deliberate_repetition:
            dims |= RULE_REPETITION_DIMENSIONS
        return frozenset(dims)

    @property
    def common_dimensions(self) -> frozenset[str]:
        return frozenset(dim for dim in self.dimension_stats if self.level_for(dim) == "common")

    @property
    def habitual_needles(self) -> frozenset[str]:
        """这位作者的常用词：一场的量里期望至少用到一次（只用于展示；放不放过看每一稿的次数）。"""

        return frozenset(term for term, rate in self.needle_rates.items() if float(rate) * RULE_JUDGE_WINDOW_CHARS / 10000.0 >= 1.0)

    def expected_in(self, term: str, chars: int) -> float:
        rate = float(self.needle_rates.get(term) or 0.0)
        return rate * max(int(chars or 0), RULE_JUDGE_WINDOW_CHARS) / 10000.0

    @property
    def signature(self) -> str:
        if not self.active:
            return "default"
        payload = "|".join(f"{term}={float(rate):.3f}" for term, rate in sorted(self.needle_rates.items()))
        payload += "#" + "|".join(f"{dim}={self.level_for(dim)}" for dim in sorted(self.dimension_stats))
        payload += f"#{int(self.deliberate_repetition)}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict[str, Any]:
        top = sorted(self.needle_rates.items(), key=lambda item: (-float(item[1]), item[0]))
        habitual_needles = self.habitual_needles  # 属性每次现算整张表：取一次，别在下面逐词再算
        return {
            "source": self.source,
            "windows": self.windows,
            "endings": self.endings,
            "endings_source": self.endings_source,
            "chars": self.chars,
            "deliberate_repetition": self.deliberate_repetition,
            "thresholds": {
                "needle_tail_alpha": RULE_NEEDLE_TAIL_ALPHA,
                "judge_window_chars": RULE_JUDGE_WINDOW_CHARS,
                "dimension_habit_share": RULE_DIMENSION_HABIT_SHARE,
                "dimension_common_share": RULE_DIMENSION_COMMON_SHARE,
                "confidence_z": RULE_CALIBRATION_CONFIDENCE_Z,
            },
            "needle_count": len(self.needle_rates),
            "top_needles": [{"term": term, "per_10k": round(float(rate), 2)} for term, rate in top[:12]],
            "habitual_needles": [term for term, _rate in top if term in habitual_needles],
            "habitual_dimensions": sorted(self.habitual_dimensions),
            "common_dimensions": sorted(self.common_dimensions),
            "dimension_levels": {
                dim: {
                    "fired": int(stats.get("fired") or 0),
                    "n": int(stats.get("n") or 0),
                    "share": round(float(stats.get("share") or 0.0), 3),
                    "lower_bound": round(float(stats.get("lower_bound") or 0.0), 3),
                    "level": self.level_for(dim),
                }
                for dim, stats in sorted(self.dimension_stats.items())
            },
        }


DEFAULT_RULE_CALIBRATION = RuleCalibration()


def calibrate_lexicons(calibration: RuleCalibration | None, text: str) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    """规则要读的「命中即毛病」词表，去掉在这一稿里属于参考作者寻常用法的词；同时返回放过了哪些词
    （每个词：稿子里的次数、参考作者每万字的次数、按一场的量算的期望次数、这个次数的概率）。"""

    lexicons = dict(FAULT_LEXICONS)
    if calibration is None or not calibration.active or not calibration.needle_rates:
        return lexicons, []
    lowered = str(text or "").lower()
    chars = len(re.sub(r"\s+", "", str(text or "")))
    decisions: dict[str, bool] = {}
    waived: list[dict[str, Any]] = []

    def keep(term: str) -> bool:
        if term in decisions:
            return decisions[term]
        verdict = True
        rate = float(calibration.needle_rates.get(term) or 0.0)
        if rate > 0:
            count = lowered.count(term.lower())
            if count:
                expected = calibration.expected_in(term, chars)
                probability = poisson_tail(count, expected)
                verdict = probability < RULE_NEEDLE_TAIL_ALPHA
                if not verdict:
                    waived.append(
                        {
                            "term": term,
                            "count": count,
                            "reference_per_10k": round(rate, 2),
                            "expected": round(expected, 2),
                            "probability": round(probability, 3),
                        }
                    )
        decisions[term] = verdict
        return verdict

    calibrated = {name: tuple(term for term in terms if keep(term)) for name, terms in lexicons.items()}
    waived.sort(key=lambda item: (-int(item["count"]), item["term"]))
    return calibrated, waived


def calibrated_lexicons(calibration: RuleCalibration | None, text: str) -> dict[str, tuple[str, ...]]:
    return calibrate_lexicons(calibration, text)[0]


def _apply_dimension_calibration(findings: list[dict[str, Any]], calibration: RuleCalibration | None) -> None:
    if calibration is None or not calibration.active:
        return
    for finding in findings:
        dimension = str(finding.get("dimension") or "")
        level = calibration.level_for(dimension)
        if level is None:
            continue
        stats = calibration.dimension_stats.get(dimension) or {}
        target = "info" if level == "habit" else "taste"
        base = str(finding.get("severity") or "revision")
        if base not in _SEVERITY_ORDER or _SEVERITY_ORDER.index(target) > _SEVERITY_ORDER.index(base):
            finding["severity"] = target  # 只降不升
        from_profile = calibration.deliberate_repetition and dimension in RULE_REPETITION_DIMENSIONS and stats.get("level") != "habit"
        finding["calibrated"] = {
            "kind": "profile_deliberate_repetition" if from_profile else "dimension_habit",
            "level": level,
            "share": round(float(stats.get("share") or 0.0), 3),
            "lower_bound": round(float(stats.get("lower_bound") or 0.0), 3),
            "n": int(stats.get("n") or 0),
        }
