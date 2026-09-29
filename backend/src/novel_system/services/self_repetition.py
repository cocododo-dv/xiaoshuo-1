"""Cross-scene repetition guidance for drafting — prompt guidance only, never a gate.

Feeds the bundle's literary freshness budget (``bundle_freshness``): n-grams that recur across the recent
scenes of a chapter, semantic repetition against the recent corpus (metaphors, scene openers, action habits,
four-character emotional idioms — blueprint §9), and the whole-book list of expressions already used. Nothing
here blocks or scores a draft; the in-passage ``self_repetition`` dimension is a rule of ``literary_quality``.

The corpus is the scenes' current final text (the run state's ``current_final_scene_row_id``), read in one
query per list of scenes.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, FinalScene, SceneCard, SceneRunState
from novel_system.services.narrative_position import NarrativePositionService
from novel_system.services.style_reference.validation.plagiarism import normalize_text_for_matching


@dataclass(slots=True)
class SemanticRepetitionHit:
    pattern_type: str  # "metaphor" / "scene_opener" / "action_habit" / "emotional_expression"
    current_text: str
    previous_text: str
    source_scene_id: str


def _final_texts(session: Session, scene_ids: Iterable[str]) -> dict[str, str]:
    """场景 → 运行状态指着的那份终稿的正文（一次查询；没有指针、或正文为空的场景不在结果里）。"""
    ids = [scene_id for scene_id in dict.fromkeys(scene_ids) if scene_id]
    if not ids:
        return {}
    rows = session.execute(
        select(SceneRunState.scene_id, FinalScene.content)
        .join(FinalScene, FinalScene.row_id == SceneRunState.current_final_scene_row_id)
        .where(SceneRunState.scene_id.in_(ids))
    ).all()
    return {scene_id: content for scene_id, content in rows if content}


class SelfRepetitionDetector:
    def __init__(self, session: Session) -> None:
        self.session = session

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

    def recent_corpus(
        self,
        current_scene_id: str,
        chapter_id: str,
        *,
        lookback_scenes: int,
    ) -> tuple[list[str], list[str]]:
        """(终稿正文, 场景号)：本章其余各场（场序倒序）最多 ``lookback_scenes`` 场，不够时补上一章的末几场；
        没有终稿的场景占名额但不进语料。"""
        scene_ids = list(self.session.execute(
            select(SceneCard.scene_id)
            .where(
                SceneCard.chapter_id == chapter_id,
                SceneCard.trashed_flag == 0,
                SceneCard.scene_id != current_scene_id,
            )
            .order_by(SceneCard.scene_seq.desc(), SceneCard.scene_id.desc())
            .limit(lookback_scenes)
        ).scalars().all())

        remaining = lookback_scenes - len(scene_ids)
        previous_chapter_id = self._previous_chapter_id(chapter_id) if remaining > 0 else None
        if previous_chapter_id:
            scene_ids.extend(self.session.execute(
                select(SceneCard.scene_id)
                .where(
                    SceneCard.chapter_id == previous_chapter_id,
                    SceneCard.trashed_flag == 0,
                )
                .order_by(SceneCard.scene_seq.desc())
                .limit(remaining)
            ).scalars().all())

        finals = _final_texts(self.session, scene_ids)
        corpus = [(finals[scene_id], scene_id) for scene_id in scene_ids if scene_id in finals]
        return [text for text, _ in corpus], [scene_id for _, scene_id in corpus]

    # 兼容：bundle 的新鲜度预算（bundle_freshness）还按这个旧的私有名调用；它改用 recent_corpus 之后删掉。
    _load_corpus = recent_corpus

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
        scene_ids = self.session.execute(
            select(SceneCard.scene_id)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.desc())
            .limit(lookback)
        ).scalars().all()
        finals = _final_texts(self.session, scene_ids)
        return [finals[scene_id] for scene_id in scene_ids if scene_id in finals]


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
    """Expressions already used across ALL finalized scenes of a project, read fresh on every call
    (one query for the scene ids, one for their current final texts)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_lifetime_avoidance_guidance(self, project_id: str) -> str:
        """Format top-20 most frequently used patterns as avoidance guidance."""
        acc = self._aggregate(self._snapshots(project_id))
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

    def _snapshots(self, project_id: str) -> list[SceneExpressionSnapshot]:
        scene_ids = self.session.execute(
            select(SceneCard.scene_id).where(
                SceneCard.project_id == project_id, SceneCard.trashed_flag == 0,
            )
        ).scalars().all()
        finals = _final_texts(self.session, scene_ids)
        return [extract_scene_expressions(scene_id, finals[scene_id]) for scene_id in scene_ids if scene_id in finals]

    @staticmethod
    def _aggregate(snapshots: list[SceneExpressionSnapshot]) -> dict[str, Counter]:
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
