"""风格参考 v3（P5b，L1 / N6）— 风格步与软补丁「只改不像的维、不更像就不采用」的决定（纯函数 + 阈值）。

证据：真实运行里旧的「复读 / 润色」步 3/3 场把稿子改得离参考更远（opus 首稿只被改动 1.8%，却花了一次 6.4 万
token 的调用）。v3 的风格步（``policy.style_first`` 时）：

1. 读首稿（``readings.reading_for_text``）：读数在作者正常范围内（百分位 ≤ ``style_step_max_percentile`` 且重点维
   没有越界）→ **不调模型**，首稿就是风格稿；读不出（书没有参照分布）或不可靠（正文不到 600 可见字、参照窗口
   不到 8 个）同样保留首稿——没有可信的尺子，就不去改；
2. 否则**定向修改**：只改越界特征所在的维（重点维在前，至多 4 维），给出测得的差异（白话短语 + 作者的典型
   水平）与这几维的文风卡句；改完再读——``distance`` 至少比首稿小 ``revision_min_improvement``、且过了唯一抄袭门
   与确定性安全门才采用，否则保留首稿。

软补丁同理：补丁后软 QC 再评一次，评审总分（0–1）比补丁前低 ``judge_tolerance`` 以上、或确定性 ``distance``
变大超过 ``patch_max_distance_increase`` 而评审分没有提高 → 退回补丁前的稿子。

阈值在 ``config/style_reference/injection_budget.yaml`` 的 ``fidelity:`` 段，都是**临时值**，上线前用真实模型
小规模 A/B 定（契约 §6 第 5 步）。本模块只依赖读数 / 文风卡 / 配置，不碰管线。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    DIMENSION_EMPHASIZE,
    DIMENSION_EXCLUDE,
    normalize_dimension_states,
)
from novel_system.services.style_reference.card import (
    DIMENSION_LABELS,
    LINE_STATE_EXCLUDED,
    LINE_STATE_PINNED,
    DimensionCard,
)
from novel_system.services.style_reference.config_loader import load_optional_yaml_config
from novel_system.services.style_reference.fidelity import (
    DEFAULT_MAX_PERCENTILE,
    MEASURABLE_DIMENSIONS,
    FidelityReading,
    feature_phrase,
    within_author_range,
)
from novel_system.services.style_reference.voice_signature import (
    _cn_count,
    _cn_int,
    _rate_phrase,
    _tenths_phrase,
)

FIDELITY_CONFIG_SECTION = "fidelity"
# 软补丁的去留记在这一步的尝试上（编排器写，工作台 / 读数接口读）。
STYLE_PATCH_KEEP_STEP = "style_patch_keep"
MAX_REVISE_DIMENSIONS = 4
MAX_DIFFERENCE_LINES = 8
MAX_CARD_LINES_PER_DIMENSION = 3

# 风格步的决定（AttemptTracker.details_json.style_step.decision）与内容来源（details_json.content_source）
DECISION_FIRST_DRAFT_ACCEPTED = "first_draft_accepted"
DECISION_REVISION_KEPT = "revision_kept"
DECISION_REVISION_REJECTED = "revision_rejected"
CONTENT_SOURCE_FIRST_DRAFT_ACCEPTED = "first_draft_accepted"
CONTENT_SOURCE_TARGETED_REVISION = "targeted_revision"
CONTENT_SOURCE_REVISION_NOT_CLOSER = "revision_not_closer"

# 首稿不改的原因
REASON_WITHIN_RANGE = "within_author_range"
REASON_READING_UNAVAILABLE = "reading_unavailable"
# 读数本身出错（异常）——与「参考书没有可用的尺子」（reading_unavailable）分开说（L8）
REASON_READING_FAILED = "reading_failed"
REASON_READING_UNRELIABLE = "reading_unreliable"
REASON_OUT_OF_RANGE = "out_of_author_range"
REASON_CANDIDATE_SLOT = "best_of_n_candidate"
# 定向修改的去留
REASON_CLOSER = "closer_to_author"
REASON_NOT_CLOSER = "not_closer"
REASON_COPY_BLOCKED = "copy_gate_blocked"
REASON_BASE_UNSAFE = "base_safety_failed"
REASON_REVISION_UNREADABLE = "revision_reading_unavailable"
REASON_TEMPLATE_MISSING = "revision_template_missing"
# 补丁的去留
PATCH_DECISION_KEPT = "kept"
PATCH_DECISION_REVERTED = "reverted"
PATCH_REASON_JUDGE_WORSE = "judge_worse"
PATCH_REASON_DISTANCE_WORSE = "distance_worse_without_judge_gain"
PATCH_REASON_NOT_WORSE = "not_worse"
PATCH_REASON_NO_EVIDENCE = "no_comparable_evidence"


@dataclass(frozen=True)
class FidelityThresholds:
    style_step_max_percentile: float = DEFAULT_MAX_PERCENTILE
    revision_min_improvement: float = 0.03
    patch_max_distance_increase: float = 0.05
    # 参考评审总分（0–1）的波动容差：0.1 = 评审 10 分制上的 1 分。评审按整数 / 半分给分，两次独立评审的噪声常有
    # 半分到一分；0.02（0.2 分）比评审的粒度还细，会把没变差的补丁当成变差退回（M3）
    judge_tolerance: float = 0.1

    def audit(self) -> dict[str, float]:
        return {key: float(value) for key, value in asdict(self).items()}


DEFAULT_THRESHOLDS = FidelityThresholds()


def _number(value: Any, default: float, *, low: float = 0.0, high: float = math.inf) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < low or number > high:
        return default
    return number


def fidelity_thresholds() -> FidelityThresholds:
    """``injection_budget.yaml`` 的 ``fidelity:`` 段（缺键 / 坏值按默认）。"""
    try:
        raw = load_optional_yaml_config("injection_budget").get(FIDELITY_CONFIG_SECTION)
    except Exception:  # noqa: BLE001 — 坏配置按默认
        raw = None
    section = raw if isinstance(raw, Mapping) else {}
    return FidelityThresholds(
        style_step_max_percentile=_number(
            section.get("style_step_max_percentile"), DEFAULT_THRESHOLDS.style_step_max_percentile, high=100.0
        ),
        revision_min_improvement=_number(
            section.get("revision_min_improvement"), DEFAULT_THRESHOLDS.revision_min_improvement
        ),
        patch_max_distance_increase=_number(
            section.get("patch_max_distance_increase"), DEFAULT_THRESHOLDS.patch_max_distance_increase
        ),
        judge_tolerance=_number(section.get("judge_tolerance"), DEFAULT_THRESHOLDS.judge_tolerance, high=1.0),
    )


# ---------------------------------------------------------------------------
# 风格步：要不要改、改哪几维、告诉模型什么
# ---------------------------------------------------------------------------


def style_step_gate(
    reading: FidelityReading | None,
    thresholds: FidelityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[bool, str]:
    """(要不要定向修改, 原因)。读不出 / 不可靠 / 在范围内 → 不改（不调模型）。"""
    if reading is None:
        return False, REASON_READING_UNAVAILABLE
    if not reading.reliable:
        return False, REASON_READING_UNRELIABLE
    if within_author_range(reading, max_percentile=thresholds.style_step_max_percentile):
        return False, REASON_WITHIN_RANGE
    return True, REASON_OUT_OF_RANGE


def revision_dimensions(
    reading: FidelityReading,
    dimension_states: Mapping[str, Any] | None = None,
    *,
    limit: int = MAX_REVISE_DIMENSIONS,
) -> list[str]:
    """要改的维：越界特征所在的维（重点维在前，再按该维最大的 |z|），「不学」维不改，至多 ``limit`` 维。

    没有越界特征（百分位超了、但没有一个特征单独越界）时取确定性分最低的几维（低于 10 分的）。
    """
    states = normalize_dimension_states(dimension_states) if dimension_states is not None else {}
    worst: dict[str, float] = {}
    for item in reading.out_of_band:
        dim = str(item.get("dimension") or "")
        if dim not in ALL_DIMENSIONS or states.get(dim) == DIMENSION_EXCLUDE:
            continue
        worst[dim] = max(worst.get(dim, 0.0), abs(float(item.get("z") or 0.0)))
    if worst:
        ordered = sorted(worst, key=lambda dim: (states.get(dim) != DIMENSION_EMPHASIZE, -worst[dim], dim))
        return ordered[: max(0, int(limit))]
    scored = [
        (score, dim)
        for dim, score in reading.dimension_scores.items()
        if dim in MEASURABLE_DIMENSIONS and states.get(dim) != DIMENSION_EXCLUDE and float(score) < 10.0
    ]
    scored.sort(key=lambda pair: (states.get(pair[1]) != DIMENSION_EMPHASIZE, pair[0], pair[1]))
    return [dim for _score, dim in scored[: max(0, int(limit))]]


_SENTENCE_FINAL = ("sentence_final_modal_ratio", "sentence_final_classical_ratio")
_CHAR_LENGTHS = (
    "sent_len_mean",
    "sent_len_std",
    "sent_len_p10",
    "sent_len_p90",
    "clause_len_mean",
    "para_len_mean",
    "para_len_std",
)


def _every_n_sentences(ratio: float) -> str:
    if ratio <= 0.005:
        return "几乎没有"
    n = max(1, int(round(1.0 / ratio)))
    return "几乎每句都有" if n <= 1 else f"大约每{_cn_count(n)}句一次"


def level_words(feature: str, value: Any) -> str:
    """一个特征的取值 → 给作者 / 模型看的中文说法（没有阿拉伯数字）；说不清楚的特征返回空串。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    if feature.endswith("_per_1k"):
        unit = "个" if feature.startswith(("punct_", "fw_", "latin_")) else "处"
        phrase = _rate_phrase(number, unit)
        return phrase or "几乎没有"
    if feature in _SENTENCE_FINAL:
        return _every_n_sentences(number)
    if feature in _CHAR_LENGTHS:
        return f"约{_cn_int(round(max(0.0, number)))}字"
    if feature == "sent_pauses_mean":
        return f"每句约{_cn_count(round(max(0.0, number)))}处停顿"
    if feature == "sent_short_run_mean":
        return f"短句一串约{_cn_count(round(max(0.0, number)))}句"
    if feature.endswith("_share") or feature in ("para_single_sentence_ratio", "para_dialogue_ratio", "sent_short_run_ratio"):
        return _tenths_phrase(number)
    return ""


def revision_differences(
    reading: FidelityReading,
    dimensions: Sequence[str],
    *,
    limit: int = MAX_DIFFERENCE_LINES,
) -> list[dict[str, Any]]:
    """这几维里测得的差异：白话短语 + 作者的典型水平 + 这一稿的水平（都是中文说法，不给统计量）。"""
    wanted = set(dimensions)
    lines: list[dict[str, Any]] = []
    for item in reading.out_of_band:
        dim = str(item.get("dimension") or "")
        if dim not in wanted:
            continue
        feature = str(item.get("feature") or "")
        direction = str(item.get("direction") or "")
        phrase = str(item.get("phrase") or "") or feature_phrase(feature, direction)
        author = level_words(feature, item.get("author_typical"))
        draft = level_words(feature, item.get("value"))
        text = phrase
        if author and draft and author != draft:
            text = f"{phrase}（作者一般{author}，这一稿{draft}）"
        elif author:
            text = f"{phrase}（作者一般{author}）"
        lines.append(
            {
                "feature": feature,
                "dimension": dim,
                "direction": direction,
                "phrase": phrase,
                "author_level": author,
                "draft_level": draft,
                "text": text,
            }
        )
        if len(lines) >= limit:
            break
    return lines


def card_lines_for(
    card: DimensionCard | None,
    dimensions: Iterable[str],
    *,
    line_states: Mapping[str, str] | None = None,
    per_dimension: int = MAX_CARD_LINES_PER_DIMENSION,
) -> dict[str, list[str]]:
    """文风卡里这几维的句子（钉住的在前，再必须、再辨识度；✗ 的不要）；「作者不这么写」的句子带「不」字头。"""
    if card is None:
        return {}
    states = dict(line_states or {})
    out: dict[str, list[str]] = {}
    for dimension in dimensions:
        entry = card.entry(str(dimension))
        if entry is None:
            continue
        usable = [line for line in entry.lines if states.get(line.line_id) != LINE_STATE_EXCLUDED]
        do_lines = sorted(
            (line for line in usable if line.kind == "do"),
            key=lambda line: (
                states.get(line.line_id) != LINE_STATE_PINNED,
                not line.mandatory,
                -line.distinctiveness,
                line.line_id,
            ),
        )
        avoid_lines = sorted(
            (line for line in usable if line.kind == "avoid"),
            key=lambda line: (states.get(line.line_id) != LINE_STATE_PINNED, -line.distinctiveness, line.line_id),
        )
        texts = [line.text for line in do_lines[: max(0, per_dimension)]]
        texts.extend(f"（作者不这么写）{line.text}" for line in avoid_lines[:1])
        if texts:
            out[str(dimension)] = texts
    return out


def dimension_label(dimension: str) -> str:
    return DIMENSION_LABELS.get(str(dimension), str(dimension))


def revision_brief_sections(
    *,
    dimensions: Sequence[str],
    differences: Sequence[Mapping[str, Any]],
    card_lines: Mapping[str, Sequence[str]],
) -> str:
    """定向修改提示里的三段（要改的维 / 测得的差异 / 这几维的文风卡句），接在首稿之后。"""
    parts = ["## Dimensions To Move Toward The Author"]
    parts.extend(f"- {dimension_label(dim)}（{dim}）" for dim in dimensions)
    parts.append("")
    parts.append("## Measured Differences (this draft against the author's own passages)")
    if differences:
        parts.extend(f"- {item.get('text') or item.get('phrase')}" for item in differences)
    else:
        parts.append("- （这几维没有单独越界的量；整体读起来仍比作者自己的段落远，按样例的手法校正这几维）")
    lines = [(dim, card_lines.get(dim) or []) for dim in dimensions]
    if any(texts for _dim, texts in lines):
        parts.append("")
        parts.append("## Style Card Lines For These Dimensions")
        for dim, texts in lines:
            for text in texts:
                parts.append(f"- {dimension_label(dim)}：{text}")
    return "\n".join(parts)


def revision_keep_decision(
    first: FidelityReading | None,
    revision: FidelityReading | None,
    *,
    copy_blocked: bool,
    base_safe: bool,
    thresholds: FidelityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[bool, str]:
    """(采用修改稿?, 原因)：过安全门、过抄袭门、读得出、且比首稿近至少 ``revision_min_improvement``。"""
    if not base_safe:
        return False, REASON_BASE_UNSAFE
    if copy_blocked:
        return False, REASON_COPY_BLOCKED
    if first is None or revision is None:
        return False, REASON_REVISION_UNREADABLE
    if float(revision.distance) <= float(first.distance) - float(thresholds.revision_min_improvement):
        return True, REASON_CLOSER
    return False, REASON_NOT_CLOSER


def patch_keep_decision(
    *,
    before_judge: float | None,
    after_judge: float | None,
    before_distance: float | None,
    after_distance: float | None,
    thresholds: FidelityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[str, str]:
    """软补丁的去留 → (kept | reverted, 原因)。评审分在 0–1（``review_scores`` 的契约尺度）。

    - 评审总分比补丁前低 ``judge_tolerance`` 以上 → 退回；
    - 确定性 distance 变大超过 ``patch_max_distance_increase``、而评审分没有提高（高出 ``judge_tolerance`` 以上）→ 退回；
    - 否则留下补丁。两边都没有可比的证据 → 留下（与旧行为一致，不凭空退回）。
    """
    judge_comparable = before_judge is not None and after_judge is not None
    distance_comparable = before_distance is not None and after_distance is not None
    if not judge_comparable and not distance_comparable:
        return PATCH_DECISION_KEPT, PATCH_REASON_NO_EVIDENCE
    tolerance = float(thresholds.judge_tolerance)
    if judge_comparable and float(after_judge) < float(before_judge) - tolerance:
        return PATCH_DECISION_REVERTED, PATCH_REASON_JUDGE_WORSE
    judge_gain = judge_comparable and float(after_judge) > float(before_judge) + tolerance
    if (
        distance_comparable
        and float(after_distance) > float(before_distance) + float(thresholds.patch_max_distance_increase)
        and not judge_gain
    ):
        return PATCH_DECISION_REVERTED, PATCH_REASON_DISTANCE_WORSE
    return PATCH_DECISION_KEPT, PATCH_REASON_NOT_WORSE


def reading_brief(reading: FidelityReading | None, *, reading_id: str | None = None) -> dict[str, Any] | None:
    """尝试记录 / 决定里存的读数摘要（不含逐特征 z 值）。"""
    if reading is None:
        return None
    return {
        "reading_id": reading_id,
        "distance": reading.distance,
        "percentile": reading.percentile,
        "reliable": reading.reliable,
        "char_count": reading.char_count,
        "out_of_band": [
            {
                "feature": item.get("feature"),
                "dimension": item.get("dimension"),
                "direction": item.get("direction"),
                "z": item.get("z"),
            }
            for item in reading.out_of_band[:MAX_DIFFERENCE_LINES]
        ],
    }


__all__ = [
    "CONTENT_SOURCE_FIRST_DRAFT_ACCEPTED",
    "CONTENT_SOURCE_REVISION_NOT_CLOSER",
    "CONTENT_SOURCE_TARGETED_REVISION",
    "DECISION_FIRST_DRAFT_ACCEPTED",
    "DECISION_REVISION_KEPT",
    "DECISION_REVISION_REJECTED",
    "DEFAULT_THRESHOLDS",
    "FIDELITY_CONFIG_SECTION",
    "FidelityThresholds",
    "MAX_REVISE_DIMENSIONS",
    "PATCH_DECISION_KEPT",
    "PATCH_DECISION_REVERTED",
    "PATCH_REASON_DISTANCE_WORSE",
    "PATCH_REASON_JUDGE_WORSE",
    "PATCH_REASON_NOT_WORSE",
    "PATCH_REASON_NO_EVIDENCE",
    "REASON_BASE_UNSAFE",
    "REASON_CANDIDATE_SLOT",
    "REASON_CLOSER",
    "REASON_COPY_BLOCKED",
    "REASON_NOT_CLOSER",
    "REASON_OUT_OF_RANGE",
    "REASON_READING_FAILED",
    "REASON_READING_UNAVAILABLE",
    "REASON_READING_UNRELIABLE",
    "REASON_REVISION_UNREADABLE",
    "REASON_TEMPLATE_MISSING",
    "REASON_WITHIN_RANGE",
    "STYLE_PATCH_KEEP_STEP",
    "card_lines_for",
    "dimension_label",
    "fidelity_thresholds",
    "level_words",
    "patch_keep_decision",
    "reading_brief",
    "revision_brief_sections",
    "revision_differences",
    "revision_dimensions",
    "revision_keep_decision",
    "style_step_gate",
]
