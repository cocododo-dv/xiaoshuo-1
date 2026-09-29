"""Cross-scene self-repetition detection.

Reuses the Rabin-Karp n-gram engine from plagiarism.py to detect
when a new scene reuses phrases from recent scenes in the same novel.

Blueprint §9 extension: semantic-level repetition detection for
metaphor reuse, scene opener patterns, action habits, and
four-character emotional expressions.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, FinalScene, SceneCard, SceneRunState, StoryProject
from novel_system.services.narrative_position import NarrativePositionService
from novel_system.services.style_reference.validation.plagiarism import (
    check_plagiarism,
    normalize_text_for_matching,
)


@dataclass(slots=True)
class SelfRepetitionHit:
    matched_text: str
    position: int
    matched_length: int
    source_scene_id: str


@dataclass(slots=True)
class SemanticRepetitionHit:
    pattern_type: str  # "metaphor" / "scene_opener" / "action_habit" / "emotional_expression"
    current_text: str
    previous_text: str
    source_scene_id: str


@dataclass(slots=True)
class SelfRepetitionReport:
    passed: bool
    hits: list[SelfRepetitionHit] = field(default_factory=list)
    semantic_hits: list[SemanticRepetitionHit] = field(default_factory=list)
    corpus_scene_count: int = 0
    score: float = 1.0


class SelfRepetitionDetector:
    def __init__(self, session: Session) -> None:
        self.session = session

    def check(
        self,
        new_text: str,
        scene_id: str,
        chapter_id: str,
        *,
        lookback_scenes: int = 10,
        ngram_size: int = 8,
        threshold_chars: int = 12,
    ) -> SelfRepetitionReport:
        if not new_text or not new_text.strip():
            return SelfRepetitionReport(passed=True)

        corpus_texts, source_scene_ids = self._load_corpus(
            scene_id, chapter_id, lookback_scenes=lookback_scenes,
        )
        if not corpus_texts:
            return SelfRepetitionReport(passed=True)

        report = check_plagiarism(
            new_text,
            corpus_texts,
            ngram_size=ngram_size,
            threshold_chars=threshold_chars,
        )

        hits: list[SelfRepetitionHit] = []
        for plag_hit in report.hits:
            source_sid = self._find_source_scene(
                plag_hit.matched_text, corpus_texts, source_scene_ids,
            )
            hits.append(SelfRepetitionHit(
                matched_text=plag_hit.matched_text,
                position=plag_hit.position,
                matched_length=plag_hit.matched_length,
                source_scene_id=source_sid,
            ))

        semantic_hits = check_semantic_repetition(
            new_text, corpus_texts, source_scene_ids,
        )

        all_hit_count = len(hits) + len(semantic_hits)
        score = max(0.0, round(1.0 - 0.15 * len(hits) - 0.10 * len(semantic_hits), 4))

        return SelfRepetitionReport(
            passed=all_hit_count == 0,
            hits=hits,
            semantic_hits=semantic_hits,
            corpus_scene_count=len(corpus_texts),
            score=score,
        )

    def top_repeated_ngrams(
        self,
        chapter_id: str,
        *,
        lookback_scenes: int = 10,
        top_n: int = 10,
    ) -> list[str]:
        """N-grams appearing in 2+ recent scenes — candidates for negative guidance."""
        scenes = self._recent_scene_texts(chapter_id, lookback_scenes)
        if len(scenes) < 2:
            return []

        ngram_size = 8
        ngram_scenes: dict[str, set[int]] = {}
        for idx, text in enumerate(scenes):
            norm = normalize_text_for_matching(text)
            seen_in_scene: set[str] = set()
            for i in range(len(norm) - ngram_size + 1):
                ng = norm[i : i + ngram_size]
                if ng not in seen_in_scene:
                    seen_in_scene.add(ng)
                    ngram_scenes.setdefault(ng, set()).add(idx)

        repeated = [
            (ng, len(scene_set))
            for ng, scene_set in ngram_scenes.items()
            if len(scene_set) >= 2
        ]
        repeated.sort(key=lambda pair: pair[1], reverse=True)
        return [ng for ng, _ in repeated[:top_n]]

    def _load_corpus(
        self,
        current_scene_id: str,
        chapter_id: str,
        *,
        lookback_scenes: int,
    ) -> tuple[list[str], list[str]]:
        scene_cards = list(self.session.execute(
            select(SceneCard)
            .where(
                SceneCard.chapter_id == chapter_id,
                SceneCard.trashed_flag == 0,
                SceneCard.scene_id != current_scene_id,
            )
            .order_by(SceneCard.scene_seq.desc(), SceneCard.scene_id.desc())
            .limit(lookback_scenes)
        ).scalars().all())

        previous_chapter_id = self._previous_chapter_id(chapter_id)
        for prev_ch_id in [previous_chapter_id] if previous_chapter_id else []:
            remaining = lookback_scenes - len(scene_cards)
            if remaining <= 0:
                break
            prev_scenes = list(self.session.execute(
                select(SceneCard)
                .where(
                    SceneCard.chapter_id == prev_ch_id,
                    SceneCard.trashed_flag == 0,
                )
                .order_by(SceneCard.scene_seq.desc())
                .limit(remaining)
            ).scalars().all())
            scene_cards.extend(prev_scenes)

        texts: list[str] = []
        scene_ids: list[str] = []
        for sc in scene_cards:
            state = self.session.get(SceneRunState, sc.scene_id)
            if state is None or not state.current_final_scene_row_id:
                continue
            final = self.session.get(FinalScene, state.current_final_scene_row_id)
            if final is None or not final.content:
                continue
            texts.append(final.content)
            scene_ids.append(sc.scene_id)

        return texts, scene_ids

    def _previous_chapter_id(self, chapter_id: str) -> str | None:
        """同一部作品里排在 ``chapter_id`` 前面的那一章（目录次序：display_order，缺序的排后，同序按 chapter_id）。

        以前按 chapter_id 的字典序在**所有**作品里找「比它小的那个」——阶段 Y 之后章号是钉住的流水号、不是书里的
        次序，一部作品的第一章还会拿到别的作品的章，把那本书的终稿混进复读检查的语料（B04-10）。"""
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None:
            return None
        missing = NarrativePositionService.chapter_missing_expr()
        order = NarrativePositionService.chapter_order_expr()
        own_missing = 1 if chapter.display_order is None else 0
        own_order = int(chapter.display_order or 0)
        same_project = (
            ChapterGoal.project_id.is_(None)
            if chapter.project_id is None
            else ChapterGoal.project_id == chapter.project_id
        )
        return self.session.execute(
            select(ChapterGoal.chapter_id)
            .where(
                same_project,
                ChapterGoal.trashed_flag == 0,
                or_(
                    missing < own_missing,
                    and_(missing == own_missing, order < own_order),
                    and_(missing == own_missing, order == own_order, ChapterGoal.chapter_id < chapter.chapter_id),
                ),
            )
            .order_by(missing.desc(), order.desc(), ChapterGoal.chapter_id.desc())
            .limit(1)
        ).scalars().first()

    def _recent_scene_texts(self, chapter_id: str, lookback: int) -> list[str]:
        scene_cards = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.desc())
            .limit(lookback)
        ).scalars().all()
        texts: list[str] = []
        for sc in scene_cards:
            state = self.session.get(SceneRunState, sc.scene_id)
            if state and state.current_final_scene_row_id:
                final = self.session.get(FinalScene, state.current_final_scene_row_id)
                if final and final.content:
                    texts.append(final.content)
        return texts

    @staticmethod
    def _find_source_scene(
        matched_text: str,
        corpus_texts: list[str],
        source_scene_ids: list[str],
    ) -> str:
        for text, sid in zip(corpus_texts, source_scene_ids):
            if matched_text in text:
                return sid
        return source_scene_ids[0] if source_scene_ids else "unknown"


# ---------------------------------------------------------------------------
# Semantic-level repetition detection — blueprint §9 extension
# ---------------------------------------------------------------------------

_METAPHOR_MARKERS = ("像", "如同", "仿佛", "犹如", "好似", "恰似", "宛如", "似的", "好像")

_ACTION_HABITS = (
    "轻叹", "摇头", "皱眉", "抿唇", "握拳", "咬牙", "点了点头", "叹了口气",
    "深吸一口气", "微微一笑", "嘴角微扬", "眼神一暗", "眉头紧锁", "不由自主",
    "下意识", "微微颔首", "攥紧了拳头", "垂下眼帘", "嘴唇微动", "身体一僵",
    "喉结上下滚动", "缓缓闭上眼", "嘴角勾起", "眼眶微红", "鼻尖一酸",
)

_EMOTIONAL_IDIOMS = (
    "心如刀割", "泪如雨下", "痛不欲生", "万念俱灰", "悲痛欲绝", "肝肠寸断",
    "心如死灰", "怒不可遏", "喜极而泣", "如释重负", "心乱如麻", "忐忑不安",
    "心惊胆战", "毛骨悚然", "不寒而栗", "五味杂陈", "百感交集", "刻骨铭心",
    "撕心裂肺", "心潮澎湃", "黯然神伤", "怅然若失", "恍然大悟", "如梦初醒",
    "心有余悸", "胆战心惊", "魂飞魄散", "惊魂未定", "义愤填膺", "热泪盈眶",
)

_METAPHOR_CONTEXT_CHARS = 10
_METAPHOR_JACCARD_THRESHOLD = 0.5
_ACTION_FREQUENCY_THRESHOLD = 2


def check_semantic_repetition(
    new_text: str,
    corpus_texts: list[str],
    source_scene_ids: list[str],
) -> list[SemanticRepetitionHit]:
    """Detect semantic-level repetition patterns across scenes."""
    if not new_text or not corpus_texts:
        return []
    hits: list[SemanticRepetitionHit] = []
    hits.extend(_detect_metaphor_reuse(new_text, corpus_texts, source_scene_ids))
    hits.extend(_detect_scene_opener_reuse(new_text, corpus_texts, source_scene_ids))
    hits.extend(_detect_action_habit_reuse(new_text, corpus_texts, source_scene_ids))
    hits.extend(_detect_emotional_expression_reuse(new_text, corpus_texts, source_scene_ids))
    return hits


def format_semantic_repetition_guidance(hits: list[SemanticRepetitionHit]) -> str:
    """Format semantic repetition findings as avoidance guidance for prompt injection."""
    if not hits:
        return ""
    lines = ["## Semantic Repetition Alert (avoid these patterns)"]
    type_labels = {
        "metaphor": "Metaphor reuse",
        "scene_opener": "Scene opener pattern",
        "action_habit": "Action habit repetition",
        "emotional_expression": "Emotional idiom reuse",
    }
    for hit in hits:
        label = type_labels.get(hit.pattern_type, hit.pattern_type)
        lines.append(
            f"- [{label}] Already used: \"{hit.previous_text}\" — "
            f"find a fresh alternative for \"{hit.current_text}\""
        )
    return "\n".join(lines)


def _extract_metaphors(text: str) -> list[str]:
    """Extract metaphor phrases: marker + following context."""
    results: list[str] = []
    for marker in _METAPHOR_MARKERS:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            end = min(idx + len(marker) + _METAPHOR_CONTEXT_CHARS, len(text))
            results.append(text[idx:end])
            start = idx + len(marker)
    return results


def _char_ngrams(text: str, n: int = 4) -> set[str]:
    return {text[i:i + n] for i in range(len(text) - n + 1)} if len(text) >= n else set()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _detect_metaphor_reuse(
    new_text: str,
    corpus_texts: list[str],
    source_scene_ids: list[str],
) -> list[SemanticRepetitionHit]:
    new_metaphors = _extract_metaphors(new_text)
    if not new_metaphors:
        return []
    hits: list[SemanticRepetitionHit] = []
    seen: set[str] = set()
    for text, sid in zip(corpus_texts, source_scene_ids):
        corpus_metaphors = _extract_metaphors(text)
        for nm in new_metaphors:
            nm_grams = _char_ngrams(nm)
            for cm in corpus_metaphors:
                if _jaccard(nm_grams, _char_ngrams(cm)) >= _METAPHOR_JACCARD_THRESHOLD:
                    key = nm[:8]
                    if key not in seen:
                        seen.add(key)
                        hits.append(SemanticRepetitionHit(
                            pattern_type="metaphor",
                            current_text=nm,
                            previous_text=cm,
                            source_scene_id=sid,
                        ))
    return hits[:5]


def _first_sentence(text: str) -> str:
    match = re.search(r'^(.+?)[。！？!?\n]', text.strip())
    return match.group(1).strip() if match else text.strip()[:30]


def _detect_scene_opener_reuse(
    new_text: str,
    corpus_texts: list[str],
    source_scene_ids: list[str],
) -> list[SemanticRepetitionHit]:
    new_opener = _first_sentence(new_text)
    if len(new_opener) < 4:
        return []
    new_prefix = new_opener[:4]
    hits: list[SemanticRepetitionHit] = []
    for text, sid in zip(corpus_texts, source_scene_ids):
        corpus_opener = _first_sentence(text)
        if len(corpus_opener) >= 4 and corpus_opener[:4] == new_prefix:
            hits.append(SemanticRepetitionHit(
                pattern_type="scene_opener",
                current_text=new_opener[:30],
                previous_text=corpus_opener[:30],
                source_scene_id=sid,
            ))
            break
    return hits[:2]


def _detect_action_habit_reuse(
    new_text: str,
    corpus_texts: list[str],
    source_scene_ids: list[str],
) -> list[SemanticRepetitionHit]:
    new_actions = {a for a in _ACTION_HABITS if a in new_text}
    if not new_actions:
        return []
    corpus_action_counts: dict[str, list[str]] = {}
    for text, sid in zip(corpus_texts, source_scene_ids):
        for action in new_actions:
            if action in text:
                corpus_action_counts.setdefault(action, []).append(sid)

    hits: list[SemanticRepetitionHit] = []
    for action, sids in corpus_action_counts.items():
        if len(sids) >= _ACTION_FREQUENCY_THRESHOLD:
            hits.append(SemanticRepetitionHit(
                pattern_type="action_habit",
                current_text=action,
                previous_text=f"{action} (appeared in {len(sids)} recent scenes)",
                source_scene_id=sids[0],
            ))
    return sorted(hits, key=lambda h: h.current_text)[:5]


def _detect_emotional_expression_reuse(
    new_text: str,
    corpus_texts: list[str],
    source_scene_ids: list[str],
) -> list[SemanticRepetitionHit]:
    new_idioms = {idiom for idiom in _EMOTIONAL_IDIOMS if idiom in new_text}
    if not new_idioms:
        return []
    hits: list[SemanticRepetitionHit] = []
    seen: set[str] = set()
    for text, sid in zip(corpus_texts, source_scene_ids):
        for idiom in new_idioms:
            if idiom in text and idiom not in seen:
                seen.add(idiom)
                hits.append(SemanticRepetitionHit(
                    pattern_type="emotional_expression",
                    current_text=idiom,
                    previous_text=idiom,
                    source_scene_id=sid,
                ))
    return hits[:5]


# ---------------------------------------------------------------------------
# Lifetime expression registry — blueprint §9 "跨全书累积禁用表达列表"
# ---------------------------------------------------------------------------

LIFETIME_TOP_LIMIT = 20
CROSS_PROJECT_TOP_LIMIT = 15
DEFAULT_CROSS_PROJECT_PENALTY = 0.5
DEFAULT_MAX_SIBLING_PROJECTS = 5


@dataclass
class SceneExpressionSnapshot:
    """Extracted expression fingerprint for a single finalized scene."""
    scene_id: str
    metaphors: list[str] = field(default_factory=list)
    opener: str = ""
    action_habits: list[str] = field(default_factory=list)
    emotional_idioms: list[str] = field(default_factory=list)


def extract_scene_expressions(scene_id: str, text: str) -> SceneExpressionSnapshot:
    """Build a :class:`SceneExpressionSnapshot` from raw scene text."""
    metaphors = _extract_metaphors(text)
    opener = _first_sentence(text)
    action_habits = [a for a in _ACTION_HABITS if a in text]
    emotional_idioms = [e for e in _EMOTIONAL_IDIOMS if e in text]
    return SceneExpressionSnapshot(
        scene_id=scene_id, metaphors=metaphors, opener=opener,
        action_habits=action_habits, emotional_idioms=emotional_idioms,
    )


class LifetimeExpressionRegistry:
    """Accumulates expression usage across ALL finalized scenes of a project.

    Hydrates lazily from FinalScene rows on first query per project.
    Call record_scene_expressions() after scene finalization for incremental update.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self._cache: dict[str, list[SceneExpressionSnapshot]] = {}

    def record_scene_expressions(
        self, scene_id: str, text: str, project_id: str | None = None,
    ) -> SceneExpressionSnapshot:
        snapshot = extract_scene_expressions(scene_id, text)
        pid = project_id or self._resolve_project_id(scene_id)
        if pid:
            snapshots = self._cache.setdefault(pid, [])
            self._cache[pid] = [s for s in snapshots if s.scene_id != scene_id]
            self._cache[pid].append(snapshot)
        return snapshot

    def get_lifetime_avoidance_guidance(self, project_id: str) -> str:
        """Format top-20 most frequently used patterns as avoidance guidance."""
        self._ensure_hydrated(project_id)
        acc = self._aggregate(project_id)
        if not any(acc.values()):
            return ""
        parts: list[str] = ["【全书已用表达禁用清单 -- 请勿在新场景中重复使用】"]
        for label, key in [
            ("已用过的比喻/意象", "metaphors"),
            ("已用过的场景开头方式", "openers"),
            ("已用过的角色动作口癖", "action_habits"),
            ("已用过的情绪惯用语", "emotional_idioms"),
        ]:
            counter = acc[key]
            if counter:
                parts.append(f"{label}：")
                for expr, count in counter.most_common(LIFETIME_TOP_LIMIT):
                    parts.append(f"  - {expr} (x{count})")
        return "\n".join(parts)

    def format_lifetime_banned_expressions(self, project_id: str) -> list[str]:
        """Flat list sorted by frequency, capped at LIFETIME_TOP_LIMIT."""
        self._ensure_hydrated(project_id)
        acc = self._aggregate(project_id)
        merged: Counter[str] = Counter()
        for key in ("metaphors", "openers", "action_habits", "emotional_idioms"):
            merged.update(acc.get(key, Counter()))
        return [expr for expr, _ in merged.most_common(LIFETIME_TOP_LIMIT)]

    def invalidate(self, project_id: str) -> None:
        self._cache.pop(project_id, None)

    def _resolve_project_id(self, scene_id: str) -> str | None:
        card = self.session.get(SceneCard, scene_id)
        return card.project_id if card else None

    def _ensure_hydrated(self, project_id: str) -> None:
        if project_id in self._cache:
            return
        snapshots: list[SceneExpressionSnapshot] = []
        scene_ids = self.session.execute(
            select(SceneCard.scene_id).where(
                SceneCard.project_id == project_id, SceneCard.trashed_flag == 0,
            )
        ).scalars().all()
        for sid in scene_ids:
            state = self.session.get(SceneRunState, sid)
            if state and state.current_final_scene_row_id:
                final = self.session.get(FinalScene, state.current_final_scene_row_id)
                if final and final.content:
                    snapshots.append(extract_scene_expressions(sid, final.content))
        self._cache[project_id] = snapshots

    def _aggregate(self, project_id: str) -> dict[str, Counter]:
        snapshots = self._cache.get(project_id, [])
        result: dict[str, Counter] = {
            "metaphors": Counter(), "openers": Counter(),
            "action_habits": Counter(), "emotional_idioms": Counter(),
        }
        for snap in snapshots:
            for m in snap.metaphors:
                result["metaphors"][m] += 1
            if snap.opener:
                result["openers"][snap.opener] += 1
            for h in snap.action_habits:
                result["action_habits"][h] += 1
            for i in snap.emotional_idioms:
                result["emotional_idioms"][i] += 1
        return result

    # ------------------------------------------------------------------
    # Cross-project (series-level) repetition detection
    # ------------------------------------------------------------------

    def cross_project_banned_expressions(
        self,
        project_id: str,
        *,
        max_sibling_projects: int = DEFAULT_MAX_SIBLING_PROJECTS,
    ) -> CrossProjectExpressionBudget:
        """Collect banned expressions from sibling projects.

        Since StoryProject has no explicit author/group field, all
        non-trashed projects other than *project_id* are treated as
        siblings (the single-author assumption: one DB instance serves
        one author, so every project is part of the same creative
        corpus).

        Results are capped to *max_sibling_projects* most-recently-
        updated projects to keep hydration cost bounded.

        Returns a :class:`CrossProjectExpressionBudget` that the caller
        can merge with the per-project budget at a reduced penalty
        weight (series-level repetition is less severe than within-book
        repetition).
        """
        sibling_ids = self._find_sibling_project_ids(
            project_id, max_sibling_projects=max_sibling_projects,
        )
        if not sibling_ids:
            return CrossProjectExpressionBudget(
                project_id=project_id,
                sibling_project_ids=[],
                expressions_by_category={
                    "metaphors": Counter(),
                    "openers": Counter(),
                    "action_habits": Counter(),
                    "emotional_idioms": Counter(),
                },
                flat_expressions=[],
            )

        merged: dict[str, Counter] = {
            "metaphors": Counter(),
            "openers": Counter(),
            "action_habits": Counter(),
            "emotional_idioms": Counter(),
        }
        for sid in sibling_ids:
            self._ensure_hydrated(sid)
            acc = self._aggregate(sid)
            for key in merged:
                merged[key].update(acc.get(key, Counter()))

        all_counts: Counter = Counter()
        for key in ("metaphors", "openers", "action_habits", "emotional_idioms"):
            all_counts.update(merged[key])
        flat = [expr for expr, _ in all_counts.most_common(CROSS_PROJECT_TOP_LIMIT)]

        return CrossProjectExpressionBudget(
            project_id=project_id,
            sibling_project_ids=sibling_ids,
            expressions_by_category=merged,
            flat_expressions=flat,
        )

    def get_cross_project_avoidance_guidance(
        self,
        project_id: str,
        *,
        max_sibling_projects: int = DEFAULT_MAX_SIBLING_PROJECTS,
    ) -> str:
        """Format cross-project banned expressions as avoidance guidance.

        Returns an empty string when there are no sibling projects or no
        overlapping expressions, so callers can simply test truthiness.
        """
        budget = self.cross_project_banned_expressions(
            project_id, max_sibling_projects=max_sibling_projects,
        )
        if not budget.flat_expressions:
            return ""

        parts: list[str] = [
            "【跨作品系列级表达禁用清单 -- 在其他作品中已反复出现，请寻找替代】",
            f"(来源：{len(budget.sibling_project_ids)} 部关联作品)",
        ]
        for label, key in [
            ("其他作品已用的比喻/意象", "metaphors"),
            ("其他作品已用的场景开头方式", "openers"),
            ("其他作品已用的角色动作口癖", "action_habits"),
            ("其他作品已用的情绪惯用语", "emotional_idioms"),
        ]:
            counter = budget.expressions_by_category.get(key, Counter())
            if counter:
                top = counter.most_common(CROSS_PROJECT_TOP_LIMIT)
                parts.append(f"{label}：")
                for expr, count in top:
                    parts.append(f"  - {expr} (x{count})")
        return "\n".join(parts)

    def _find_sibling_project_ids(
        self,
        project_id: str,
        *,
        max_sibling_projects: int,
    ) -> list[str]:
        """Return IDs of the most-recently-updated sibling projects.

        All non-trashed projects other than *project_id* qualify;
        ordered by ``updated_at`` descending so the most active
        projects take priority when the cap is applied.
        """
        rows = self.session.execute(
            select(StoryProject.project_id)
            .where(
                StoryProject.project_id != project_id,
                StoryProject.trashed_flag == 0,
            )
            .order_by(StoryProject.updated_at.desc())
            .limit(max_sibling_projects)
        ).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# Cross-project expression budget — data container
# ---------------------------------------------------------------------------

@dataclass
class CrossProjectExpressionBudget:
    """Aggregated expression usage from sibling projects.

    Returned by
    :meth:`LifetimeExpressionRegistry.cross_project_banned_expressions`.
    The caller decides how to weight these entries relative to the
    within-book budget (see :func:`merge_freshness_budgets`).
    """
    project_id: str
    sibling_project_ids: list[str]
    expressions_by_category: dict[str, Counter] = field(default_factory=dict)
    flat_expressions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Budget merging — combine per-project + cross-project banned expressions
# ---------------------------------------------------------------------------

@dataclass
class MergedFreshnessBudget:
    """Combined per-project and cross-project freshness budget.

    *project_expressions* are the within-book banned expressions
    (full penalty).  *cross_project_expressions* come from sibling
    projects (reduced penalty via *cross_project_penalty*).
    *combined_expressions* is the deduplicated, penalty-weighted
    union sorted by effective weight descending.
    """
    project_expressions: list[str]
    cross_project_expressions: list[str]
    cross_project_penalty: float
    combined_expressions: list[tuple[str, float]] = field(default_factory=list)


def merge_freshness_budgets(
    project_budget: list[str],
    cross_project_budget: CrossProjectExpressionBudget,
    *,
    cross_project_penalty: float = DEFAULT_CROSS_PROJECT_PENALTY,
) -> MergedFreshnessBudget:
    """Merge per-project and cross-project banned expressions.

    Within-book expressions carry a weight of ``1.0``; cross-project
    expressions carry *cross_project_penalty* (default ``0.5``,
    reflecting the lesser severity of series-level repetition).

    If an expression appears in BOTH the current project and sibling
    projects, it keeps the higher (``1.0``) weight, avoiding double
    counting.

    The *combined_expressions* list is sorted by effective weight
    descending, then alphabetically, and is suitable for prompt
    injection as a prioritized avoidance list.

    Parameters
    ----------
    project_budget:
        Flat list of banned expressions from the current project
        (e.g. from ``format_lifetime_banned_expressions``).
    cross_project_budget:
        :class:`CrossProjectExpressionBudget` from
        ``cross_project_banned_expressions``.
    cross_project_penalty:
        Weight multiplier for cross-project expressions (0.0-1.0).
    """
    weights: dict[str, float] = {}

    for expr in project_budget:
        weights[expr] = 1.0

    for expr in cross_project_budget.flat_expressions:
        if expr not in weights:
            weights[expr] = cross_project_penalty

    combined = sorted(
        weights.items(),
        key=lambda pair: (-pair[1], pair[0]),
    )

    return MergedFreshnessBudget(
        project_expressions=project_budget,
        cross_project_expressions=cross_project_budget.flat_expressions,
        cross_project_penalty=cross_project_penalty,
        combined_expressions=combined,
    )


def format_merged_freshness_guidance(merged: MergedFreshnessBudget) -> str:
    """Format a :class:`MergedFreshnessBudget` as prompt-injectable guidance.

    High-weight (within-book) expressions are listed under a strict
    "do not use" heading; lower-weight (cross-project) expressions
    appear under a softer "try to avoid" heading.
    """
    if not merged.combined_expressions:
        return ""

    strict = [expr for expr, w in merged.combined_expressions if w >= 1.0]
    soft = [expr for expr, w in merged.combined_expressions if w < 1.0]

    parts: list[str] = []
    if strict:
        parts.append("【本书已用表达 -- 禁止重复】")
        for expr in strict:
            parts.append(f"  - {expr}")
    if soft:
        parts.append("【系列其他作品已用表达 -- 尽量避免】")
        for expr in soft:
            parts.append(f"  - {expr}")

    return "\n".join(parts)
