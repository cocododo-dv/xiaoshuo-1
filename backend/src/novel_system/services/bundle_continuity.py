"""跨场景的连续性上下文：上一章、章间过渡、前文声音锚、上一场正文的结尾节选（B03-19 从 bundle_builder 拆出）。"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.bundle_draft_lineage import latest_styled_draft_for_scene
from novel_system.services.planning_queries import current_final_scenes
from novel_system.services.style_reference.budget_config import injection_budget

# 2026-09 风格模仿 v2（W5，规格 §1.3）——前文声音锚 section 的登记名。风格参考 v3 删掉了漂移校准段
# （``style_drift_calibration``）与漂移优先选窗（``_drift_ptype_priority``）：归档读数不再回灌进下一场。
VOICE_ANCHOR_SECTION_KEY = "previous_scene_voice_anchor"
_SENTENCE_END_RE = re.compile(r"[。！？!?…]+[”’」』）)]*")


def load_continuity_budget() -> dict[str, int]:
    """``config/style_reference/injection_budget.yaml`` 的跨场景连续性预算键（``continuity_anchor_max_chars``，
    ``budget_config`` 解析）；文件或键缺失、值不是正整数时回到规格 §1.4 的默认值（900 字）。
    """
    return {"continuity_anchor_max_chars": injection_budget().continuity_anchor_max_chars}


def tail_at_sentence_boundary(text: str, max_chars: int) -> str:
    """取 ``text`` 尾部 ≤``max_chars`` 字，并让片段从一个完整句子起头。"""
    normalized = str(text or "").strip()
    if not normalized or max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    tail = normalized[-max_chars:]
    match = _SENTENCE_END_RE.search(tail)
    if match is not None and match.end() < len(tail):
        tail = tail[match.end():]
    return tail.strip()


def previous_live_chapter(session: Session, scene: SceneCard) -> ChapterGoal | None:
    """本章之前最近的一章（按 display_order，没有就按 chapter_id）；回收站里的章跳过。"""
    current = session.get(ChapterGoal, scene.chapter_id)
    if current is None:
        return None
    if current.display_order is not None:
        stmt = (
            select(ChapterGoal)
            .where(
                ChapterGoal.project_id == scene.project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.display_order < current.display_order,
            )
            .order_by(ChapterGoal.display_order.desc())
        )
    else:
        stmt = (
            select(ChapterGoal)
            .where(
                ChapterGoal.project_id == scene.project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.chapter_id < scene.chapter_id,
            )
            .order_by(ChapterGoal.chapter_id.desc())
        )
    return session.execute(stmt).scalars().first()


REFERENCE_FIRST_MEMORY_NOTE = (
    "（上一场结尾节选，只用于衔接事实、位置、道具与时序；文风以参考样例为准，不要延续这段文字的腔调）"
)


def reference_first_memory_digest(content: str, *, max_chars: int | None = None) -> str:
    """2026-09-22 风格参考优先:style_first 下上一场正文只留结尾节选给衔接,不再整篇进提示。

    节选长度沿用 ``continuity_anchor_max_chars``(缺省 900);尽量从段落边界起。空正文 → 空串。
    """
    limit = (
        int(max_chars)
        if max_chars is not None
        else int(load_continuity_budget()["continuity_anchor_max_chars"])
    )
    text = str(content or "").strip()
    if limit <= 0 or not text:
        return ""
    tail = text if len(text) <= limit else text[-limit:]
    if len(text) > limit:
        cut = tail.find("\n")
        if 0 < cut < len(tail) // 2:
            tail = tail[cut + 1 :].lstrip()
    return f"{REFERENCE_FIRST_MEMORY_NOTE}\n{tail}"


def previous_scene_voice_anchor(
    session: Session,
    scene: SceneCard,
    *,
    max_chars: int | None = None,
) -> dict[str, Any] | None:
    """规格 §1.3「前文声音锚」：同章上一场（按 scene_seq 最近）的最新已风格化稿尾部。

    同章没有已风格化前文时退到上一章最后一场；都没有 → ``None``（不登记）。
    返回 ``{"text", "source_scene_id", "source_draft_row_id", "source_stage",
    "max_chars"}``。
    """
    limit = (
        int(max_chars)
        if max_chars is not None
        else load_continuity_budget()["continuity_anchor_max_chars"]
    )
    if limit <= 0:
        return None
    candidates = list(
        session.execute(
            select(SceneCard)
            .where(
                SceneCard.chapter_id == scene.chapter_id,
                SceneCard.trashed_flag == 0,
                SceneCard.scene_seq < scene.scene_seq,
            )
            .order_by(SceneCard.scene_seq.desc())
        )
        .scalars()
        .all()
    )
    if not candidates:
        previous_chapter = previous_live_chapter(session, scene)
        if previous_chapter is not None:
            candidates = list(
                session.execute(
                    select(SceneCard)
                    .where(
                        SceneCard.chapter_id == previous_chapter.chapter_id,
                        SceneCard.trashed_flag == 0,
                    )
                    .order_by(SceneCard.scene_seq.desc())
                )
                .scalars()
                .all()
            )
    for candidate in candidates:
        draft = latest_styled_draft_for_scene(session, candidate.scene_id)
        if draft is None:
            continue
        text = tail_at_sentence_boundary(draft.content or "", limit)
        if not text:
            continue
        return {
            "text": text,
            "source_scene_id": candidate.scene_id,
            "source_draft_row_id": draft.row_id,
            "source_stage": draft.stage,
            "max_chars": limit,
        }
    return None


def chapter_transition_text(session: Session, scene: SceneCard) -> str | None:
    """Blueprint §3: inject last 500-1000 chars of previous chapter as continuity anchor.

    Only fires for the first scene of a chapter (scene_seq == 1).
    上一章按 ``previous_live_chapter``（跳过回收站里的章），取它最后一场的当前正文（B03-04 / B03-05）。
    """
    if scene.scene_seq and scene.scene_seq > 1:
        return None
    # 上一章跳过回收站里的章（清空后自动入回收站的章，B03-05），与前文声音锚同一口径
    prev_chapter = previous_live_chapter(session, scene)
    if prev_chapter is None:
        return None
    prev_scene_ids = list(
        session.execute(
            select(SceneCard.scene_id)
            .where(
                SceneCard.chapter_id == prev_chapter.chapter_id,
                SceneCard.trashed_flag == 0,
            )
            .order_by(SceneCard.scene_seq.desc(), SceneCard.scene_id.desc())
        ).scalars()
    )
    current_finals = current_final_scenes(session, prev_scene_ids)
    last_final = next(
        (current_finals[scene_id] for scene_id in prev_scene_ids if scene_id in current_finals),
        None,
    )
    if last_final and last_final.content:
        tail = last_final.content[-800:]
        return f"## Chapter Transition Buffer (previous chapter ending — maintain tone continuity)\n\n{tail}"
    return None
