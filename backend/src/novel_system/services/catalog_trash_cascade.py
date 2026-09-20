"""随章一起进回收站的计划内场景卡（2026-09-20）——叶子模块，只依赖 ORM 模型。

作者嫌目录里的章乱，先在章节编排里把旧章删了（删章会把章内的场景卡一起送进回收站，盖上和章相同的
``trashed_at``），再回构思里「整理为章节结构」。阶段 Y 之后重新分章铸的是新章号，于是确认写入把这些
卡搬进了新章，卡自己却还躺在回收站里：目录里 4 章只看得见 1 场，另外 16 场「不见了」（真实故障）。
过去只有「目标章恰好就是被取回的那一章」时，随它一起删的卡才跟着回来。

删章是整理结构，不是对场景的裁定——这一版章表里有这一场，确认写入就把它的卡取回。作者**单独**删掉的
场景卡（成稿中心「标待删」、章节编排里删一场）是对那一场的裁定，不动。两者靠删章时盖的时间戳区分：
级联进回收站的卡，``trashed_at`` 和某个回收站里的章一模一样。

物化（``projects.approve_outline_plan``）在落位之前取回；分章面板（``snowflake_chaptering``）用同一条
判定提前告诉作者。两边都引用这里，所以它必须是叶子。
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard

#: 取回时原来的章内序号已被占用的卡先停到这段高位上；随后 ``_CatalogPlacement`` 会统一重新落位
_REVIVE_PARK_OFFSET = 1_000_000


def split_trashed_planned_cards(
    session: Session,
    project_id: str,
    scene_ids: Iterable[str],
    *,
    target_chapter_ids: Iterable[str] = (),
) -> tuple[list[SceneCard], list[SceneCard]]:
    """这一版计划里、此刻在回收站里的场景卡 → ``(随章级联进去的, 作者单独删的)``。"""
    wanted = sorted({str(scene_id or "").strip() for scene_id in scene_ids} - {""})
    if not wanted:
        return [], []
    cards = [
        card
        for card in session.execute(
            select(SceneCard).where(SceneCard.scene_id.in_(wanted), SceneCard.trashed_flag == 1)
        ).scalars()
        if not card.project_id or card.project_id == project_id
    ]
    if not cards:
        return [], []
    targets = sorted({str(chapter_id or "").strip() for chapter_id in target_chapter_ids} - {""})
    scope = ChapterGoal.project_id == project_id
    if targets:
        scope = or_(scope, ChapterGoal.chapter_id.in_(targets))
    chapter_stamps = {
        str(stamp)
        for stamp in session.execute(
            select(ChapterGoal.trashed_at).where(ChapterGoal.trashed_flag == 1, scope)
        ).scalars()
        if stamp
    }
    cascade = [card for card in cards if card.trashed_at and str(card.trashed_at) in chapter_stamps]
    cascade_ids = {card.scene_id for card in cascade}
    individual = [card for card in cards if card.scene_id not in cascade_ids]
    order = lambda card: (str(card.chapter_id), int(card.scene_seq or 0), str(card.scene_id))  # noqa: E731
    return sorted(cascade, key=order), sorted(individual, key=order)


def revive_cascade_trashed_scene_cards(
    session: Session,
    project_id: str,
    scene_ids: Iterable[str],
    *,
    target_chapter_ids: Iterable[str] = (),
) -> list[str]:
    """把随章级联进回收站的计划内场景卡取回，返回取回的 scene_id。必须在 ``_CatalogPlacement`` 之前调用。

    ``(chapter_id, scene_seq)`` 在活跃场景卡上唯一：原来的章内序号还空着就原样取回（保住它和手加场的相对
    位置），已经被别的活跃卡占了的先停到高位——最终序号反正由随后的落位统一重写。
    """
    cascade, _individual = split_trashed_planned_cards(
        session, project_id, scene_ids, target_chapter_ids=target_chapter_ids
    )
    if not cascade:
        return []
    chapter_ids = sorted({str(card.chapter_id) for card in cascade})
    taken: dict[str, set[int]] = {}
    highest = 0
    for chapter_id, scene_seq, trashed_flag in session.execute(
        select(SceneCard.chapter_id, SceneCard.scene_seq, SceneCard.trashed_flag).where(
            SceneCard.chapter_id.in_(chapter_ids)
        )
    ):
        seq = int(scene_seq or 0)
        highest = max(highest, seq)
        if int(trashed_flag or 0) == 0:
            taken.setdefault(str(chapter_id), set()).add(seq)
    park = highest + _REVIVE_PARK_OFFSET
    for card in cascade:
        seqs = taken.setdefault(str(card.chapter_id), set())
        seq = int(card.scene_seq or 0)
        if seq < 1 or seq in seqs:
            park += 1
            seq = park
            card.scene_seq = seq
        seqs.add(seq)
        card.trashed_flag = 0
        card.trashed_at = None
        card.trashed_by = None
    session.flush()
    return [card.scene_id for card in cascade]
