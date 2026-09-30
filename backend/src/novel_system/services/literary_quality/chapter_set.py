"""章组复审：伏笔兑现 / 代价 / 下一拍的覆盖、跨章反复出现的写法、受保护专名的出现，与跨章弧线的子分。"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from novel_system.services.literary_quality.lexicons import (
    CHAPTER_SET_NEXT_PULL_TERMS,
    CHOICE_TERMS,
    COST_TERMS,
    NEGATED_ACTION_CONTEXT_TERMS,
    PRESSURE_TERMS,
)
from novel_system.services.literary_quality.text import (
    _compact_ws,
    _ending_slice,
    _excerpt,
    _first_present_term,
)
from novel_system.services.source_safety import find_protected_term_spans

if TYPE_CHECKING:
    from novel_system.db.models import ChapterGoal


def _chapter_set_payoff_reveal_checks(chapters: list[ChapterGoal], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_chapter: dict[str, str] = {}
    for row in source_rows:
        by_chapter[row["chapter_id"]] = "\n".join([by_chapter.get(row["chapter_id"], ""), str(row.get("content") or "")])
    forced_choice_ids: list[str] = []
    cost_ids: list[str] = []
    next_pull_ids: list[str] = []
    for chapter in chapters:
        source_text = _compact_ws(by_chapter.get(chapter.chapter_id, ""))
        fallback_text = _compact_ws(
            "\n".join(
                [
                    chapter.chapter_goal or "",
                    chapter.main_plot_push or "",
                    chapter.emotional_target or "",
                    chapter.ending_effect or "",
                    by_chapter.get(chapter.chapter_id, ""),
                ]
            )
        )
        combined_text = source_text or fallback_text
        if _first_present_term(combined_text, CHOICE_TERMS):
            forced_choice_ids.append(chapter.chapter_id)
        if _first_present_term(combined_text, COST_TERMS) or _first_present_term(combined_text, PRESSURE_TERMS):
            cost_ids.append(chapter.chapter_id)
        if _first_non_negated_present_term(_ending_slice(combined_text), CHAPTER_SET_NEXT_PULL_TERMS):
            next_pull_ids.append(chapter.chapter_id)
    ordered_chapter_ids = [chapter.chapter_id for chapter in chapters]
    missing_forced_choice_ids = [chapter_id for chapter_id in ordered_chapter_ids if chapter_id not in forced_choice_ids]
    missing_cost_ids = [chapter_id for chapter_id in ordered_chapter_ids if chapter_id not in cost_ids]
    missing_next_pull_ids = [chapter_id for chapter_id in ordered_chapter_ids if chapter_id not in next_pull_ids]
    missing_payoff_ids = [
        chapter_id
        for chapter_id in ordered_chapter_ids
        if chapter_id in {*missing_forced_choice_ids, *missing_cost_ids, *missing_next_pull_ids}
    ]
    return {
        "has_forced_choice_count": len(forced_choice_ids),
        "has_cost_count": len(cost_ids),
        "has_next_pull_count": len(next_pull_ids),
        "forced_choice_chapter_ids": forced_choice_ids,
        "cost_chapter_ids": cost_ids,
        "next_pull_chapter_ids": next_pull_ids,
        "missing_forced_choice_chapter_ids": missing_forced_choice_ids,
        "missing_cost_chapter_ids": missing_cost_ids,
        "missing_next_pull_chapter_ids": missing_next_pull_ids,
        "missing_payoff_chapter_ids": missing_payoff_ids,
    }


def _reference_safety_findings(source_rows: list[dict[str, Any]], protected_terms: list[str]) -> list[dict[str, Any]]:
    """作者给的受保护专名在复审文字里的出现，每段文字每个词报一次（按传入的词序）。

    与抄袭门同一套匹配（``source_safety.find_protected_term_spans``：插空格、换标点、繁体、大小写都认得出，批准#12
    的「一种匹配」）；以前这里是裸的 ``term in text``，「灰 港学院」就漏了。

    每段文字只规范化一次、所有词一起找，再按传入的词序各取最早的一处——逐词各找一遍时，规范化的开销要乘上词数，
    十来章配十来个词就是十几秒。"""
    findings: list[dict[str, Any]] = []
    wanted = [term for term in protected_terms if isinstance(term, str) and term.strip()]
    if not wanted:
        return findings
    for row in source_rows:
        text = str(row.get("content") or "")
        first_span: dict[str, tuple[int, int]] = {}
        for span_term, span_start, span_end in find_protected_term_spans(text, wanted):
            first_span.setdefault(span_term, (span_start, span_end))
        if not first_span:
            continue
        lowered = text.lower()
        for term in protected_terms:
            # 匹配器按去掉首尾空白的词报命中
            span = first_span.get(term.strip()) if isinstance(term, str) else None
            if span is None:
                continue
            start, end = span
            findings.append(
                {
                    "term": term,
                    "chapter_id": row.get("chapter_id"),
                    "scene_id": row.get("scene_id"),
                    "object_type": row.get("object_type"),
                    "object_id": row.get("object_id"),
                    "source_ref": row.get("source_ref"),
                    # 字面命中照旧按词取摘录；变体命中按命中的位置取
                    "evidence_excerpt": (
                        _excerpt(text, term)
                        if term.lower() in lowered
                        else _compact_ws(text[max(0, start - 70) : end + 70])
                    ),
                }
            )
    return findings


def _chapter_set_repeated_patterns(source_rows: list[dict[str, Any]], scene_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in scene_items:
        fingerprint = item.get("fingerprint") or {}
        for cluster_type, key in (
            ("action_template", "action_templates"),
            ("image_field", "image_fields"),
            ("syntax_shape", "syntax_shapes"),
        ):
            for token_row in fingerprint.get(key) or []:
                _add_repeated_pattern(
                    grouped,
                    cluster_type=cluster_type,
                    token=str(token_row.get("value") or ""),
                    count=int(token_row.get("count") or 0),
                    chapter_id=item.get("chapter_id"),
                    object_id=item.get("object_id"),
                )
    for row in source_rows:
        text = str(row.get("content") or "")
        for token, count in _cjk_key_term_counts(text).items():
            _add_repeated_pattern(
                grouped,
                cluster_type="key_term",
                token=token,
                count=count,
                chapter_id=row.get("chapter_id"),
                object_id=row.get("object_id"),
            )
    patterns = [
        row
        for row in grouped.values()
        if row["count"] >= 2 and len(row["chapter_ids"]) >= 2
    ]
    patterns = _dedupe_key_term_substrings(patterns)
    return sorted(patterns, key=lambda row: (-row["count"], row["cluster_type"], row["token"]))[:20]


def _dedupe_key_term_substrings(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """key_term n-gram 去碎片：若较短 token 只作为较长 token 的子串出现（计数相等），
    丢弃较短的，保留最长有意义复用词（如保留「玻璃雨」而非「玻璃」「璃雨」）。"""
    key_terms = [p for p in patterns if p["cluster_type"] == "key_term"]
    drop: set[str] = set()
    for short in key_terms:
        for long in key_terms:
            if short is long:
                continue
            st, lt = short["token"], long["token"]
            if len(lt) > len(st) and st in lt and long["count"] >= short["count"]:
                drop.add(st)
                break
    return [p for p in patterns if not (p["cluster_type"] == "key_term" and p["token"] in drop)]


def _add_repeated_pattern(
    grouped: dict[tuple[str, str], dict[str, Any]],
    *,
    cluster_type: str,
    token: str,
    count: int,
    chapter_id: str | None,
    object_id: str | None,
) -> None:
    if not token or count <= 0:
        return
    row = grouped.setdefault(
        (cluster_type, token),
        {"cluster_type": cluster_type, "token": token, "count": 0, "chapter_ids": [], "object_ids": []},
    )
    row["count"] += count
    if chapter_id and chapter_id not in row["chapter_ids"]:
        row["chapter_ids"].append(chapter_id)
    if object_id and object_id not in row["object_ids"]:
        row["object_ids"].append(object_id)


_CJK_STOP_CHARS = frozenset(
    "的了在和这那他她它我你您们是有就也都要会着过被把对与之而其于以为不没"
    "很又再上下里中来去到说做看想能将已并且或如但因所还只本该些个种样头边时"
)


def _cjk_key_term_counts(text: str) -> Counter[str]:
    """通用跨章关键词词频：滑动 2-4 字 CJK n-gram 计数，过滤纯虚词 gram。

    取代旧的 `玻璃雨/零点/证人/反证/名单` demo 硬编码（对真实新书返回空）。
    改为按词频提取每部作品自己的反复关键词，由调用方 count>=2 且 chapter_ids>=2
    过滤 + 子串去重收敛为有意义的跨章复用词。
    """
    counts: Counter[str] = Counter()
    normalized = _compact_ws(text)
    for token in re.findall(r"[\u4e00-\u9fff]{2,6}", normalized):
        run_len = len(token)
        for size in (2, 3, 4):
            if run_len < size:
                break
            for start in range(run_len - size + 1):
                gram = token[start : start + size]
                if all(ch in _CJK_STOP_CHARS for ch in gram):
                    continue
                counts[gram] += 1
    return counts


def _chapter_set_scores(
    mean_score: float | None,
    payoff_reveal_checks: dict[str, Any],
    safety_findings: list[dict[str, Any]],
    chapter_count: int,
    *,
    source_rows: list[dict[str, Any]] | None = None,
    chapters: list[ChapterGoal] | None = None,
    protected_terms: list[str] | None = None,
) -> dict[str, Any]:
    arc_score = _evaluate_cross_chapter_arc(
        payoff_reveal_checks,
        chapter_count,
        source_rows=source_rows,
        chapters=chapters,
    )
    # 诚实性：未提供受保护词 = 未做参考安全扫描 → 返回 None（前端渲染「—」），
    # 不能把「没扫」伪装成绿色满分 1.0。命中=0.0，传词且干净=1.0。
    if safety_findings:
        reference_safety: float | None = 0.0
    elif protected_terms:
        reference_safety = 1.0
    else:
        reference_safety = None
    return {
        "literary_quality": mean_score,
        "cross_chapter_arc": arc_score,
        "reference_safety": reference_safety,
    }


def _evaluate_cross_chapter_arc(
    payoff_reveal_checks: dict[str, Any],
    chapter_count: int,
    *,
    source_rows: list[dict[str, Any]] | None = None,
    chapters: list[ChapterGoal] | None = None,
) -> float:
    """Enhanced cross-chapter arc evaluation.

    Instead of just counting keyword presence, analyze:
    1. **Arc progression** (0-1): do chapters show different decision / pressure
       patterns?  Same pressure+cost terms everywhere means static arcs.
    2. **Foreshadow health** (0-1): ratio of foreshadow lifecycle signals that
       are "touched" or "resolved" vs stale (open for too many chapters).
    3. **Theme expression variety** (0-1): Shannon entropy over the distribution
       of theme expression levels used across chapters.
    4. **Tension dynamics** (0-1): normalised standard deviation of tension
       targets across chapters (higher = more dynamic, better).

    Final score: weighted average of these 4 sub-scores plus the legacy payoff
    baseline (to stay backward-compatible for projects with no source content).
    """
    denominator = max(1, chapter_count)

    # --- legacy baseline: payoff keyword coverage ---
    payoff_score = (
        payoff_reveal_checks["has_forced_choice_count"]
        + payoff_reveal_checks["has_cost_count"]
        + payoff_reveal_checks["has_next_pull_count"]
    ) / (denominator * 3)

    # When no rich source content is available, fall back to the legacy score
    if not source_rows or not chapters or chapter_count < 2:
        return round(payoff_score, 4)

    # Build per-chapter combined text
    by_chapter: dict[str, str] = {}
    for row in source_rows:
        cid = row.get("chapter_id", "")
        by_chapter[cid] = by_chapter.get(cid, "") + "\n" + str(row.get("content") or "")

    chapter_texts: list[str] = []
    for ch in chapters:
        raw = by_chapter.get(ch.chapter_id, "")
        fallback = "\n".join([
            ch.chapter_goal or "",
            ch.main_plot_push or "",
            ch.emotional_target or "",
            ch.ending_effect or "",
            raw,
        ])
        chapter_texts.append(_compact_ws(raw or fallback))

    arc_progression = _arc_progression_score(chapter_texts)
    foreshadow_health = _foreshadow_health_score(chapter_texts)
    theme_variety = _theme_variety_score(chapters)
    tension_dynamics = _tension_dynamics_score(chapters)

    # Weighted combination: payoff baseline still counts, but structural
    # sub-scores contribute the majority when available.
    combined = (
        0.30 * payoff_score
        + 0.25 * arc_progression
        + 0.15 * foreshadow_health
        + 0.15 * theme_variety
        + 0.15 * tension_dynamics
    )
    return round(combined, 4)


def _arc_progression_score(chapter_texts: list[str]) -> float:
    """Measure how different chapters' pressure/cost term sets are from each other.

    If every chapter uses exactly the same pressure and cost vocabulary the arc
    is *static* (score 0). Maximum diversity across pairs yields score 1.
    """
    if len(chapter_texts) < 2:
        return 1.0

    per_chapter_terms: list[set[str]] = []
    combined_terms = (*PRESSURE_TERMS, *COST_TERMS)
    for text in chapter_texts:
        lowered = text.lower()
        present = {t for t in combined_terms if t.lower() in lowered}
        per_chapter_terms.append(present)

    # Average pairwise Jaccard distance
    distances: list[float] = []
    for i in range(len(per_chapter_terms)):
        for j in range(i + 1, len(per_chapter_terms)):
            union = per_chapter_terms[i] | per_chapter_terms[j]
            intersection = per_chapter_terms[i] & per_chapter_terms[j]
            if union:
                distances.append(1.0 - len(intersection) / len(union))
            else:
                distances.append(0.0)
    return sum(distances) / len(distances) if distances else 0.0


def _foreshadow_health_score(chapter_texts: list[str]) -> float:
    """Score how well foreshadowing terms progress across chapters.

    A "foreshadow" is a next-pull term from an earlier chapter that later
    appears as a choice/cost term. Healthy lifecycle: open -> touched -> resolved.
    Terms that stay open for more than half the chapters are considered stale.
    """
    if len(chapter_texts) < 2:
        return 1.0

    # Track which chapters each next-pull term appears in
    pull_term_chapters: dict[str, list[int]] = {}
    for idx, text in enumerate(chapter_texts):
        lowered = text.lower()
        for term in CHAPTER_SET_NEXT_PULL_TERMS:
            if term.lower() in lowered:
                pull_term_chapters.setdefault(term, []).append(idx)

    # Track which chapters resolve or touch those terms via choice/cost vocab
    resolution_terms = (*CHOICE_TERMS, *COST_TERMS)
    resolution_chapters: dict[str, list[int]] = {}
    for idx, text in enumerate(chapter_texts):
        lowered = text.lower()
        for term in resolution_terms:
            if term.lower() in lowered:
                resolution_chapters.setdefault(term, []).append(idx)

    if not pull_term_chapters:
        return 0.5  # no foreshadowing detected at all: neutral

    total_pulls = len(pull_term_chapters)
    healthy = 0
    stale_threshold = len(chapter_texts) // 2

    for term, intro_chapters in pull_term_chapters.items():
        first_intro = min(intro_chapters)
        # Check if any resolution term appears in a later chapter
        resolved = False
        for _res_term, res_chs in resolution_chapters.items():
            if any(ch > first_intro for ch in res_chs):
                resolved = True
                break
        if resolved:
            healthy += 1
        elif len(chapter_texts) - first_intro > stale_threshold:
            # Stale: introduced early, never resolved, still too many chapters ago
            pass  # counts against health
        else:
            healthy += 0.5  # recently introduced, not yet stale

    return healthy / total_pulls if total_pulls > 0 else 0.5


def _theme_variety_score(chapters: list[ChapterGoal]) -> float:
    """Shannon entropy over theme/emotional expression levels across chapters.

    Uses ``emotional_target`` from chapter goals as the expression channel.
    Higher entropy = theme expressed through more diverse emotional tones.
    """
    if not chapters:
        return 0.0

    levels: list[str] = []
    for ch in chapters:
        target = (ch.emotional_target or "").strip().lower()
        if target:
            levels.append(target)

    if len(levels) < 2:
        return 0.5  # too little data to assess diversity

    # Shannon entropy, normalised to [0, 1]
    counter = Counter(levels)
    total = len(levels)
    entropy = -sum((c / total) * math.log2(c / total) for c in counter.values() if c > 0)
    max_entropy = math.log2(len(counter)) if len(counter) > 1 else 1.0
    return entropy / max_entropy if max_entropy > 0 else 0.0


def _tension_dynamics_score(chapters: list[ChapterGoal]) -> float:
    """Normalised standard deviation of tension indicators across chapters.

    Uses ``ending_effect`` length as a rough proxy for tension target intensity
    (chapters ending on high-tension cliffhangers tend to have longer ending
    effect descriptions). A flat curve (all same length) scores low; varied
    curves score higher.
    """
    if not chapters or len(chapters) < 2:
        return 0.5

    # Proxy: length of ending_effect + main_plot_push as tension indicator
    values: list[float] = []
    for ch in chapters:
        tension_proxy = len(ch.ending_effect or "") + len(ch.main_plot_push or "")
        values.append(float(tension_proxy))

    if not values:
        return 0.5

    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.5

    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(variance)
    # Coefficient of variation, capped at 1.0
    cv = min(std / mean, 1.0) if mean > 0 else 0.0
    return cv


def _first_non_negated_present_term(text: str, terms: tuple[str, ...]) -> str:
    lowered = text.lower()
    for term in terms:
        normalized_term = term.lower().strip()
        if not normalized_term:
            continue
        start = 0
        while True:
            index = lowered.find(normalized_term, start)
            if index == -1:
                break
            context = lowered[max(0, index - 18) : index]
            if not any(marker in context for marker in NEGATED_ACTION_CONTEXT_TERMS):
                return term
            start = index + len(normalized_term)
    return ""
