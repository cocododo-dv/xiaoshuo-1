"""两阶段落位（先停车、再写最终值）的唯一实现——叶子模块，只改 ORM 行的属性、调会话的 flush。

目录的两条唯一索引只管活跃行：``(project_id, display_order)``（章）与 ``(chapter_id, scene_seq)``（场景卡）。
重排时逐行直接写最终值，写到一半就会撞上还没挪走的那一行，所以一律分两步：先把要动的行停到一段不会和
任何最终值撞上的高位（``park``），flush，再写最终值。这套过去在目录、回收站恢复、物化落位、随章取回、
场景重排里各写了一遍，偏移 +1 / +1000 / +1000000 各不相同（B08-15）。
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session

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
