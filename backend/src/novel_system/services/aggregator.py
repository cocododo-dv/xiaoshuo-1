"""章汇总（§2 摘要塔的章这一层）。

章汇总（``ChapterMemory``，``aggregate_stage = final``）= 这一章各场有效的场景记忆按场序拼起来。它只是一份派生
缓存（重评 R13 + 主管补充，[批准#21]）：章级读者——终审读通包、文学质量的章源、AI 通读本章——一律读各场当前
终稿现拼，不读它（文学质量以前的「章记忆终稿」一层已于 2026-10-03 按作者的决定删掉）。存下来的
这一份只由晋升（每次，重建不成只记日志）与流水线（章末那一场的归档第 4 步）重建。以前的卷汇总从它卷起，
2026-10-12 换成故事记忆的章摘要（``services/story_memory.py``）后删掉。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterMemory,
    SceneCard,
    SceneMemory,
)
from novel_system.services.chapter_state import ensure_chapter_state

# 章汇总里场与场之间的分隔（ChapterAggregateDerivation.content）
_SEPARATOR = "\n"


@dataclass(frozen=True)
class ChapterAggregateDerivation:
    """这一章此刻的章汇总应当是什么（读时现拼，不落库）。

    ``status``：``derived``——``memories`` 按场序，``content`` 就是汇总正文；``no_op``——这一章没有归档过的场；
    ``blocked``——位置对不上（``scene_ids`` 是出问题的场）。``inputs`` 是算进这一章的有效记忆（``row_id`` 序），
    不论拼不拼得出来。
    """

    status: str
    reason: str
    inputs: tuple[SceneMemory, ...] = ()
    memories: tuple[SceneMemory, ...] = ()
    scene_ids: tuple[str, ...] = ()

    @property
    def content(self) -> str:
        return _SEPARATOR.join(memory.content for memory in self.memories)


def derive_chapter_aggregate(
    chapter_id: str,
    memories: Iterable[SceneMemory],
    cards: Mapping[str, SceneCard],
) -> ChapterAggregateDerivation:
    """``memories``：记在这一章（``chapter_id``）下的有效场景记忆；``cards``：这些记忆的场的场景卡（按 ``scene_id``，
    没有卡的就不在里面）。

    回收站里的场不在这一章里，它的记忆不算（R13：以前算成「位置孤儿」，汇总就此卡死，同一章别的场也晋升不了；
    恢复之后它又算进来）。没有卡、或卡在别的章的记忆是真的不一致，照旧拦下（``scene_memory_position_orphan``）；
    一场两条有效记忆也拦（``active_scene_memory_ambiguous``）。
    """
    inputs = tuple(
        sorted(
            (memory for memory in memories if not _in_trash(cards.get(memory.scene_id))),
            key=lambda memory: memory.row_id,
        )
    )
    if not inputs:
        return ChapterAggregateDerivation("no_op", "no_scene_memories")
    placed = {
        scene_id: card
        for scene_id, card in cards.items()
        if card.chapter_id == chapter_id and not _in_trash(card)
    }
    orphan_ids = sorted({memory.scene_id for memory in inputs} - set(placed))
    if orphan_ids:
        return ChapterAggregateDerivation(
            "blocked", "scene_memory_position_orphan", inputs=inputs, scene_ids=tuple(orphan_ids)
        )
    counts = Counter(memory.scene_id for memory in inputs)
    ambiguous_ids = sorted(scene_id for scene_id, count in counts.items() if count > 1)
    if ambiguous_ids:
        return ChapterAggregateDerivation(
            "blocked", "active_scene_memory_ambiguous", inputs=inputs, scene_ids=tuple(ambiguous_ids)
        )
    return ChapterAggregateDerivation(
        "derived",
        "scene_memories_aggregated",
        inputs=inputs,
        memories=tuple(chapter_aggregate_order(inputs, placed)),
    )


def chapter_aggregate_order(
    memories: Iterable[SceneMemory], cards: Mapping[str, SceneCard]
) -> list[SceneMemory]:
    """章汇总里各场的先后：场序，同场序再按场 id、建立时间、``row_id``（:func:`derive_chapter_aggregate` 按它拼）。
    ``cards`` 里没有的场按场序 0 排。"""

    def key(memory: SceneMemory) -> tuple[int, str, str, str]:
        card = cards.get(memory.scene_id)
        return (
            int((card.scene_seq if card is not None else 0) or 0),
            memory.scene_id,
            memory.created_at or "",
            memory.row_id,
        )

    return sorted(memories, key=key)


def _in_trash(card: SceneCard | None) -> bool:
    return card is not None and bool(card.trashed_flag)


class Aggregator:
    def __init__(self, session: Session) -> None:
        self.session = session

    def derive_final_aggregate(self, chapter_id: str) -> ChapterAggregateDerivation:
        """这一章此刻的章汇总（读时现拼，只读）：两条查询——这一章有效的场景记忆、它们的场景卡。"""
        memories = list(self.session.execute(
            select(SceneMemory).where(SceneMemory.chapter_id == chapter_id, SceneMemory.active_flag == 1)
        ).scalars().all())
        scene_ids = {memory.scene_id for memory in memories}
        cards = (
            {
                card.scene_id: card
                for card in self.session.execute(
                    select(SceneCard).where(SceneCard.scene_id.in_(scene_ids))
                ).scalars().all()
            }
            if scene_ids
            else {}
        )
        return derive_chapter_aggregate(chapter_id, memories, cards)

    def run_final_aggregate(self, chapter_id: str) -> dict | None:
        """重建这一章存下来的章汇总：拼得出来就落一版新的、旧的标为被取代；拼不出来原样回报（``no_op`` / ``blocked``）。"""
        # 目录冷启动章可能没有状态行（审计 P-1）：缺行补建。
        # 以前这里还有一道「回填 / 回溯中」闸门（aggregate_block_reason、chapter_backfill_pending_count）：写它们的
        # 章回填 / 手动挂起在 2026-09 减法里删了，库里只剩默认值 none / 0，闸门随之删掉（B03-18），列由迁移 0098 删掉。
        chapter_state = ensure_chapter_state(self.session, chapter_id)
        derivation = self.derive_final_aggregate(chapter_id)
        if derivation.status != "derived":
            result: dict = {
                "status": derivation.status,
                "reason": derivation.reason,
                "chapter_memory_row_id": None,
            }
            if derivation.scene_ids:
                result["scene_ids"] = list(derivation.scene_ids)
            return result

        content = derivation.content
        existing_finals = self.session.execute(
            select(ChapterMemory)
            .where(ChapterMemory.chapter_id == chapter_id, ChapterMemory.aggregate_stage == "final")
            .order_by(ChapterMemory.row_id.asc())
        ).scalars().all()
        for existing in existing_finals:
            if existing.active_flag == 1:
                existing.active_flag = 0
                existing.runtime_eligible = 0
                existing.runtime_eligibility_basis = "superseded"

        row_id = f"chapter_memory_final_{chapter_id}_v{len(existing_finals) + 1}"
        memory = ChapterMemory(
            row_id=row_id,
            chapter_id=chapter_id,
            aggregate_stage="final",
            content=content,
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
        self.session.add(memory)
        chapter_state.last_final_memory_row_id = row_id
        return {"status": "created", "reason": "scene_memories_aggregated", "chapter_memory_row_id": row_id}
