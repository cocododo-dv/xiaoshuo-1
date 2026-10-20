"""风格参考 v3（P5b，L1 / N6）— 风格步与软补丁「只改不像的维、不更像就不采用」的决定（纯函数 + 阈值）。

证据：真实运行里旧的「复读 / 润色」步 3/3 场把稿子改得离参考更远（opus 首稿只被改动 1.8%，却花了一次 6.4 万
token 的调用）。v3 的风格步（``policy.style_first`` 时）：

1. 读首稿（``readings.reading_for_text``）：读数在作者正常范围内（百分位 ≤ ``style_step_max_percentile`` 且重点维
   没有越界）→ **不调模型**，首稿就是风格稿；读不出（书没有参照分布）或不可靠（正文不到 600 可见字、参照窗口
   不到 8 个）同样保留首稿——没有可信的尺子，就不去改；
2. 否则**定向修改**：只改越界特征所在的维（重点维在前，至多 4 维），给出测得的差异（白话短语 + 作者的典型
   水平）与这几维的文风卡句；改完再读——``distance`` 至少比首稿小 ``revision_min_improvement``、且过了唯一抄袭门
   与确定性安全门才采用，否则保留首稿。

3. 读数说修改稿更近时，再问一次参考评审「首稿与修改稿哪一个更像」（成对比较，两个次序各问一次、一致才算数，
   ``judge.compare_verdict``）：明确说首稿更像 → 仍保留首稿（读数只量得到表层，不能让表层数字盖过读者的判断）。

软补丁（2026-10-14 评审统一）：补丁稿与补丁前的稿子做同样的成对比较——明确说补丁前更像 → 退回；明确说补丁更像
→ 留下；打平 / 比不成 → 看确定性 ``distance``，变大超过 ``patch_max_distance_increase`` 就退回。不再拿补丁前后两次
绝对评审分相减（评审半分到一分的噪声就能翻转结论，旧的 ``judge_tolerance`` 已退役）。

阈值在 ``config/style_reference/injection_budget.yaml`` 的 ``fidelity:`` 段（评审放行与校准的几项也在那里，
:meth:`FidelityThresholds.judge` 交给 ``judge`` 模块）。本模块只依赖读数 / 文风卡 / 配置，不碰管线。
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
from novel_system.services.style_reference.budget_config import injection_budget
from novel_system.services.style_reference.card import (
    DIMENSION_LABELS,
    LINE_STATE_EXCLUDED,
    LINE_STATE_PINNED,
    DimensionCard,
)
from novel_system.services.style_reference.cn_phrases import (
    cn_count,
    cn_int,
    every_n_sentences,
    rate_phrase,
    tenths_phrase,
)
from novel_system.services.style_reference.judge import COMPARE_BASE, COMPARE_CANDIDATE, JudgeThresholds
from novel_system.services.style_reference.fidelity import (
    DEFAULT_MAX_PERCENTILE,
    MEASURABLE_DIMENSIONS,
    FidelityReading,
    feature_phrase,
    within_author_range,
)

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
# 读数说更近、成对比较却明确说首稿更像
REASON_PAIRWISE_FIRST_DRAFT = "pairwise_first_draft_closer"
# 补丁的去留
PATCH_DECISION_KEPT = "kept"
PATCH_DECISION_REVERTED = "reverted"
PATCH_REASON_PAIRWISE_ORIGINAL = "pairwise_original_closer"
PATCH_REASON_PAIRWISE_PATCH = "pairwise_patch_closer"
PATCH_REASON_DISTANCE_WORSE = "distance_worse"
PATCH_REASON_NOT_WORSE = "not_worse"
PATCH_REASON_NO_EVIDENCE = "no_comparable_evidence"


@dataclass(frozen=True)
class FidelityThresholds:
    style_step_max_percentile: float = DEFAULT_MAX_PERCENTILE
    revision_min_improvement: float = 0.03
    patch_max_distance_increase: float = 0.05
    # 评审放行与校准（2026-10-14 评审统一；含义见 judge.JudgeThresholds 与 injection_budget.yaml 的说明）
    judge_pass_score: float = JudgeThresholds.pass_score
    judge_raw_pass_score: float = JudgeThresholds.raw_pass_score
    judge_min_separation: float = JudgeThresholds.min_separation
    judge_min_dimension_separation: float = JudgeThresholds.min_dimension_separation
    judge_calibration_anchors: int = JudgeThresholds.calibration_anchors
    judge_samples: int = JudgeThresholds.samples

    def audit(self) -> dict[str, float]:
        return {key: float(value) for key, value in asdict(self).items()}

    def judge(self) -> JudgeThresholds:
        return JudgeThresholds(
            pass_score=float(self.judge_pass_score),
            raw_pass_score=float(self.judge_raw_pass_score),
            min_separation=float(self.judge_min_separation),
            min_dimension_separation=float(self.judge_min_dimension_separation),
            calibration_anchors=int(self.judge_calibration_anchors),
            samples=int(self.judge_samples),
        )


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
    """``injection_budget.yaml`` 的 ``fidelity:`` 段（``budget_config`` 读文件；缺键 / 坏值按默认）。"""
    section = injection_budget().fidelity
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
        judge_pass_score=_number(section.get("judge_pass_score"), DEFAULT_THRESHOLDS.judge_pass_score, high=10.0),
        judge_raw_pass_score=_number(
            section.get("judge_raw_pass_score"), DEFAULT_THRESHOLDS.judge_raw_pass_score, high=10.0
        ),
        judge_min_separation=_number(
            section.get("judge_min_separation"), DEFAULT_THRESHOLDS.judge_min_separation, high=10.0
        ),
        judge_min_dimension_separation=_number(
            section.get("judge_min_dimension_separation"), DEFAULT_THRESHOLDS.judge_min_dimension_separation, high=10.0
        ),
        judge_calibration_anchors=int(
            _number(section.get("judge_calibration_anchors"), DEFAULT_THRESHOLDS.judge_calibration_anchors, low=1.0, high=8.0)
        ),
        judge_samples=int(_number(section.get("judge_samples"), DEFAULT_THRESHOLDS.judge_samples, low=1.0, high=5.0)),
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
    """同习惯句的说法，只是比例极低时说「几乎没有」（差距说明里要说出来，习惯句里干脆不提）。"""
    if ratio <= 0.005:
        return "几乎没有"
    return every_n_sentences(ratio)


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
        phrase = rate_phrase(number, unit)
        return phrase or "几乎没有"
    if feature in _SENTENCE_FINAL:
        return _every_n_sentences(number)
    if feature in _CHAR_LENGTHS:
        return f"约{cn_int(round(max(0.0, number)))}字"
    if feature == "sent_pauses_mean":
        return f"每句约{cn_count(round(max(0.0, number)))}处停顿"
    if feature == "sent_short_run_mean":
        return f"短句一串约{cn_count(round(max(0.0, number)))}句"
    if feature.endswith("_share") or feature in ("para_single_sentence_ratio", "para_dialogue_ratio", "sent_short_run_ratio"):
        return tenths_phrase(number)
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


def judge_revision_brief_sections(
    *,
    dimensions: Sequence[str],
    judged: Mapping[str, Mapping[str, Any]],
    differences: Sequence[Mapping[str, Any]],
    card_lines: Mapping[str, Sequence[str]],
) -> str:
    """软 QC 评审判出不像的维 → 按维整场修改的提示段（2026-10-14 评审统一第 2 项）：每一维带评审的分、说明与改法；
    读数在这几维有越界的量就列出来；再给这几维的文风卡句。``judged`` = 评审记录的 ``dimensions``（作数的分、说明、改法）。"""
    parts = ["## Dimensions To Move Toward The Author"]
    for dim in dimensions:
        entry = judged.get(dim) or {}
        score = entry.get("score")
        head = f"- {dimension_label(dim)}（{dim}）"
        if isinstance(score, (int, float)):
            head += f"：评审 {float(score):.1f} / 10"
        note = str(entry.get("note") or "").strip()
        if note:
            head += f"——{note}"
        parts.append(head)
        fix = str(entry.get("fix") or "").strip()
        if fix:
            anchor = str(entry.get("anchor") or "").strip()
            parts.append(f"  改法：{fix}" + (f"（位置：「{anchor}」）" if anchor and anchor not in fix else ""))
    if differences:
        parts.append("")
        parts.append("## Measured Differences (this draft against the author's own passages)")
        parts.extend(f"- {item.get('text') or item.get('phrase')}" for item in differences)
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
    comparison: str | None = None,
) -> tuple[bool, str]:
    """(采用修改稿?, 原因)：过安全门、过抄袭门、读得出、比首稿近至少 ``revision_min_improvement``，且成对比较
    （``comparison``，``judge.compare_verdict`` 的结论；没比就是 None）没有明确说首稿更像。"""
    if not base_safe:
        return False, REASON_BASE_UNSAFE
    if copy_blocked:
        return False, REASON_COPY_BLOCKED
    if first is None or revision is None:
        return False, REASON_REVISION_UNREADABLE
    if float(revision.distance) <= float(first.distance) - float(thresholds.revision_min_improvement):
        if comparison == COMPARE_BASE:
            return False, REASON_PAIRWISE_FIRST_DRAFT
        return True, REASON_CLOSER
    return False, REASON_NOT_CLOSER


def revision_needs_comparison(
    first: FidelityReading | None,
    revision: FidelityReading | None,
    *,
    copy_blocked: bool,
    base_safe: bool,
    thresholds: FidelityThresholds = DEFAULT_THRESHOLDS,
) -> bool:
    """只有读数会采用修改稿时才值得花两次成对比较（读数已经说不采用，比较改变不了结论）。"""
    keep, _reason = revision_keep_decision(
        first, revision, copy_blocked=copy_blocked, base_safe=base_safe, thresholds=thresholds
    )
    return keep


def patch_keep_decision(
    *,
    comparison: str | None,
    before_distance: float | None,
    after_distance: float | None,
    thresholds: FidelityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[str, str]:
    """软补丁的去留 → (kept | reverted, 原因)。``comparison`` 是成对比较的结论（``judge.compare_verdict``：
    base = 补丁前的稿子更像，candidate = 补丁稿更像，tie / unavailable / None = 没有明确结论）。

    - 明确说补丁前更像 → 退回；明确说补丁更像 → 留下；
    - 没有明确结论：确定性 distance 变大超过 ``patch_max_distance_increase`` → 退回，否则留下；
    - 两边都没有证据 → 留下（不凭空退回）。
    """
    if comparison == COMPARE_BASE:
        return PATCH_DECISION_REVERTED, PATCH_REASON_PAIRWISE_ORIGINAL
    if comparison == COMPARE_CANDIDATE:
        return PATCH_DECISION_KEPT, PATCH_REASON_PAIRWISE_PATCH
    if before_distance is None or after_distance is None:
        return PATCH_DECISION_KEPT, PATCH_REASON_NO_EVIDENCE
    if float(after_distance) > float(before_distance) + float(thresholds.patch_max_distance_increase):
        return PATCH_DECISION_REVERTED, PATCH_REASON_DISTANCE_WORSE
    return PATCH_DECISION_KEPT, PATCH_REASON_NOT_WORSE


# 软 QC 报告的改稿意见里，参考评审那一条的 kind（10 分制的 style_score + 16 维分数）
REFERENCE_JUDGE_KIND = "reference_judge"


def report_reference_judge(report: Any) -> dict[str, Any] | None:
    """软 QC 报告（``QcReport``：只读 ``rewrite_brief_json``）里的参考评审条目（原样的一份拷贝）；没有 → None。

    软补丁的去留、归档时的终稿读数、像不像视图都从这里取（B01-16：以前三处各写一遍）。"""
    for entry in (getattr(report, "rewrite_brief_json", None) or []) if report is not None else []:
        if isinstance(entry, dict) and entry.get("kind") == REFERENCE_JUDGE_KIND:
            return dict(entry)
    return None


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
    "FidelityThresholds",
    "MAX_REVISE_DIMENSIONS",
    "PATCH_DECISION_KEPT",
    "PATCH_DECISION_REVERTED",
    "PATCH_REASON_DISTANCE_WORSE",
    "PATCH_REASON_NOT_WORSE",
    "PATCH_REASON_NO_EVIDENCE",
    "PATCH_REASON_PAIRWISE_ORIGINAL",
    "PATCH_REASON_PAIRWISE_PATCH",
    "REASON_BASE_UNSAFE",
    "REFERENCE_JUDGE_KIND",
    "REASON_CANDIDATE_SLOT",
    "REASON_CLOSER",
    "REASON_COPY_BLOCKED",
    "REASON_NOT_CLOSER",
    "REASON_OUT_OF_RANGE",
    "REASON_PAIRWISE_FIRST_DRAFT",
    "REASON_READING_FAILED",
    "REASON_READING_UNAVAILABLE",
    "REASON_READING_UNRELIABLE",
    "REASON_REVISION_UNREADABLE",
    "REASON_WITHIN_RANGE",
    "STYLE_PATCH_KEEP_STEP",
    "card_lines_for",
    "dimension_label",
    "fidelity_thresholds",
    "judge_revision_brief_sections",
    "level_words",
    "patch_keep_decision",
    "reading_brief",
    "report_reference_judge",
    "revision_brief_sections",
    "revision_differences",
    "revision_dimensions",
    "revision_keep_decision",
    "revision_needs_comparison",
    "style_step_gate",
]
