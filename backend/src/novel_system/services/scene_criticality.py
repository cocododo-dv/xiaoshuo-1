"""Scene criticality classification — blueprint §13 cost differentiation.

Classifies scenes as critical / standard / transition based on structural
signals. Critical scenes get full pipeline (Best-of-N N=3, critique pass, human gate);
transition scenes skip multi-path and critique for cost savings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.scene_form import text


GOLDEN_CHAPTER_COUNT = 3


@dataclass(slots=True)
class SceneCriticality:
    level: str  # "critical" | "standard" | "transition"
    reasons: list[str]
    best_of_n: int  # 候选上限（= max_best_of_n；保留旧字段名兼容既有消费方）
    skip_critique: bool  # advisory: transition scenes may skip proactive editor passes
    human_gate: bool  # critical scenes pause for author terminal selection (Wave 3 §5.5)
    # Wave 3（治理 §5.5 成本分配）：Best-of-N 的候选数——关键 3、标准 2、过渡恒 1（只有作者手笔直起才出多稿）。
    # 2026-09-30 [批准#2] 删了低分散补候选，上限 best_of_n（5 / 3）不再补到，只剩编排器的上限转手还在读。
    initial_best_of_n: int = 1

    @property
    def max_best_of_n(self) -> int:
        return self.best_of_n


def classify_scene(
    scene: SceneCard,
    *,
    chapter_seq: int | None = None,
    consecutive_transition_count: int | None = None,
    constraint_intensity: float | None = None,
) -> SceneCriticality:
    """Classify a scene's criticality from its spec fields.

    Signals that elevate to critical:
    - is_chapter_last (chapter climax position)
    - scene_crucible is substantial (>30 chars — complex dramatic premise)
    - proactive scene form
    - Golden chapter (first 3 chapters — §10 黄金三章)

    ``function_tag`` / ``tension_target`` 曾经也算信号，但只有已退役的 v1 规划器写过它们（写作简报的 v2 归一化
    也不留这两个键），2026-09-29 删掉。``constraint_intensity``（§16 呼吸阀）是场景卡上的一列，产品里没有写入者，
    只在测试里设。

    Returns criticality with recommended pipeline settings.
    """
    reasons: list[str] = []
    score = 0

    writer_brief: dict[str, Any] = scene.writer_brief_json or {}

    is_golden = False
    if chapter_seq is not None:
        is_golden = chapter_seq <= GOLDEN_CHAPTER_COUNT
    if is_golden:
        score += 2
        reasons.append("golden_chapter")

    if scene.is_chapter_last == 1:
        score += 2
        reasons.append("chapter_climax")

    scene_crucible = writer_brief.get("scene_crucible") or ""
    if len(scene_crucible) > 30:
        score += 1
        reasons.append("substantial_crucible")

    # 只认显式声明的形态（简报 scene_form、场景卡 scene_type），不按三拍推断：关键度还决定过渡场跳不跳批判，
    # 推断会把只填了三拍的手写场升一档。
    declared_form = text(writer_brief.get("scene_form")) or text(scene.scene_type)
    if declared_form == "proactive":
        score += 1
        reasons.append("proactive_scene")

    # §16 "breathing gap" — per-scene constraint_intensity slider overrides criticality
    if constraint_intensity is not None:
        if constraint_intensity <= 0.2:
            return SceneCriticality(
                level="transition",
                reasons=reasons + ["constraint_intensity_free_flow"],
                best_of_n=1,
                skip_critique=True,
                human_gate=False,
                initial_best_of_n=1,
            )
        elif constraint_intensity >= 0.8:
            return SceneCriticality(
                level="critical",
                reasons=reasons + ["constraint_intensity_full_rigor"],
                best_of_n=5,
                skip_critique=False,
                human_gate=True,
                initial_best_of_n=3,
            )
        else:
            return SceneCriticality(
                level="standard",
                reasons=reasons + [f"constraint_intensity={constraint_intensity:.1f}"],
                best_of_n=3,
                skip_critique=False,
                human_gate=False,
                initial_best_of_n=2,
            )

    if score >= 4:
        return SceneCriticality(
            level="critical",
            reasons=reasons,
            best_of_n=5,
            skip_critique=False,
            human_gate=True,
            initial_best_of_n=3,
        )
    elif score >= 2:
        return SceneCriticality(
            level="standard",
            reasons=reasons,
            best_of_n=3,
            skip_critique=False,
            human_gate=False,
            initial_best_of_n=2,
        )
    else:
        # §6.4 mitigation: probabilistic promotion after consecutive transitions
        # prevents systematic "温吞" (tepidness) in long stretches of transition scenes
        if consecutive_transition_count is not None and consecutive_transition_count >= 3:
            return SceneCriticality(
                level="standard",
                reasons=reasons + ["promoted_after_3_consecutive_transitions"],
                best_of_n=3,
                skip_critique=False,
                human_gate=False,
                initial_best_of_n=2,
            )
        return SceneCriticality(
            level="transition",
            reasons=reasons or ["default_transition"],
            best_of_n=1,
            skip_critique=True,
            human_gate=False,
            initial_best_of_n=1,
        )


def _consecutive_transition_count(session: Session, scene: SceneCard) -> int:
    """Count how many consecutive transition-level scenes precede *scene* in this chapter.

    Used by §6.4 probabilistic promotion: after 3+ consecutive transitions,
    the next transition is elevated to standard to break the "温吞" rhythm.
    """
    preceding = list(
        session.execute(
            select(SceneCard)
            .where(
                SceneCard.chapter_id == scene.chapter_id,
                SceneCard.scene_seq < scene.scene_seq,
                SceneCard.trashed_flag == 0,
            )
            .order_by(SceneCard.scene_seq.desc())
        )
        .scalars()
        .all()
    )
    count = 0
    for prev in preceding:
        crit = classify_scene(prev)  # quick: no DB queries, pure field inspection
        if crit.level == "transition":
            count += 1
        else:
            break
    return count


def classify_scene_with_context(session: Session, scene: SceneCard) -> SceneCriticality:
    """带 DB 上下文的统一判定入口——主管线与崩溃续跑必须走同一条路。

    自行推导 chapter_seq（§10 黄金三章）、连续过渡计数（§6.4 提升）与
    constraint_intensity（§16 呼吸阀），再委托纯函数 classify_scene；
    调用方不得各自拼装这些上下文参数，否则续跑判定会与首跑分叉。
    """
    chapter = session.get(ChapterGoal, scene.chapter_id)
    chapter_seq = (
        chapter.display_order
        if chapter is not None and chapter.display_order is not None
        else None
    )
    return classify_scene(
        scene,
        chapter_seq=chapter_seq,
        consecutive_transition_count=_consecutive_transition_count(session, scene),
        constraint_intensity=getattr(scene, "constraint_intensity", None),
    )
