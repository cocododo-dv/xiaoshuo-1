"""两阶段落位（先停车、再写最终值）的唯一实现——叶子模块，只改 ORM 行的属性、调会话的 flush。

目录的两条唯一索引只管活跃行：``(project_id, display_order)``（章）与 ``(chapter_id, scene_seq)``（场景卡）。
重排时逐行直接写最终值，写到一半就会撞上还没挪走的那一行，所以一律分两步：先把要动的行停到一段不会和
任何最终值撞上的高位（``park``），flush，再写最终值。这套过去在目录、回收站恢复、物化落位、随章取回、
场景重排里各写了一遍，偏移 +1 / +1000 / +1000000 各不相同（B08-15）。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal
from novel_system.services.chapter_approval import is_chapter_approved

#: 停车位离现有最大值的距离。一本书的章数、一章的场数都远到不了这个量级，
#: 所以停着的行既不会互相撞，也不会和随后写下的任何最终值撞上。
PARK_GAP = 1_000_000


def park(session: Session, rows: Sequence[Any], attr: str, *, scope: Iterable[Any] = ()) -> int:
    """把 ``rows`` 的 ``attr`` 依次挪到 ``max(rows ∪ scope 的现值) + PARK_GAP`` 起的高位并 flush。

    ``scope``：和这些行共用同一条唯一索引、这次不动的活跃行——停车位要高过它们。返回下一个空闲的停车位。
    """
    rows = list(rows)
    if not rows:
        return 0
    ceiling = max((int(getattr(row, attr) or 0) for row in (*rows, *scope)), default=0)
    slot = ceiling + PARK_GAP
    for row in rows:
        setattr(row, attr, slot)
        slot += 1
    session.flush()
    return slot


def reseat_scene_seqs(session: Session, ordered_cards: Sequence[Any], *, last_scene_id: str | None = None) -> None:
    """一章的活跃场景卡按给定顺序重写 ``scene_seq = 1..n`` 并重算章末标记。

    章末默认是最后一张；``last_scene_id`` 可以指定另一张（v1 场景重排接口由调用方给出）。
    """
    cards = list(ordered_cards)
    if not cards:
        return
    park(session, cards, "scene_seq")
    last = last_scene_id if last_scene_id is not None else cards[-1].scene_id
    for index, card in enumerate(cards, start=1):
        card.scene_seq = index
        card.is_chapter_last = 1 if card.scene_id == last else 0
    session.flush()


def compact_chapter_orders(session: Session, project_id: str | None) -> bool:
    """作品里活跃章的章序压实成 1..n（按现有顺序：没有章序的排最后，同序按 chapter_id）。返回是否改了。

    要改的行里有已终审的章就整部作品不动——终审章的位置锁着，历史漂移只能走「重新打开」修。
    这条规则过去藏在目录读取里（每次 GET 都可能写库，B08-14）；迁移 0097 把库里已有的漂移补齐之后，
    改动章集合的写入口（建章、调章序、删章 / 恢复章、物化、重新分章后移走空章）各自调它。
    """
    if not project_id:
        return False
    rows = list(
        session.execute(
            select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
        ).scalars()
    )
    rows.sort(key=lambda chapter: (chapter.display_order is None, chapter.display_order or 0, chapter.chapter_id))
    pending = [(chapter, index) for index, chapter in enumerate(rows, start=1) if chapter.display_order != index]
    if not pending or any(is_chapter_approved(session, chapter) for chapter, _ in pending):
        return False
    reseat_display_orders(session, pending, scope=rows)
    return True


def reseat_display_orders(
    session: Session,
    assignments: Sequence[tuple[Any, int]],
    *,
    scope: Iterable[Any] = (),
) -> None:
    """``[(章, 目标章序), …]``：只动章序真的变了的那几章（已终审的章由调用方排除在外），两阶段写入。"""
    changed = [(chapter, int(order)) for chapter, order in assignments if chapter.display_order != int(order)]
    if not changed:
        return
    park(session, [chapter for chapter, _ in changed], "display_order", scope=scope)
    for chapter, order in changed:
        chapter.display_order = order
    session.flush()
