"""章汇总与卷汇总（§2 摘要塔）。

章汇总（``ChapterMemory``，``aggregate_stage = final``）= 这一章各场有效的场景记忆按场序拼起来。它只是一份派生
缓存（重评 R13 + 主管补充，[批准#21]）：章级读者——终审读通包、文学质量的章源、章级准终稿评审——一律读各场当前
终稿现拼，不读它；文学质量显式挑「章记忆终稿」这一层时也按 :func:`derive_chapter_aggregate` 读时现拼。存下来的
这一份只由晋升（每次，重建不成只记日志）与流水线（章末那一场）重建，卷汇总从它卷起。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    SceneCard,
    SceneMemory,
    VolumeSummary,
)
from novel_system.services.chapter_state import ensure_chapter_state

# §2 summary tower: roll chapters up into a volume every N chapters so long books
# (50+ scenes) have a far-horizon ATMOSPHERE context the chapter layer is too fine for.
VOLUME_CHAPTER_SPAN = 5

# 章汇总里场与场之间的分隔：拼（ChapterAggregateDerivation.content）与复验（is_chapter_aggregate_of）同一个
_SEPARATOR = "\n"


@dataclass(frozen=True)
class ChapterAggregateDerivation:
    """这一章此刻的章汇总应当是什么（读时现拼，不落库）。

    ``status``：``derived``——``memories`` 按场序，``content`` 就是汇总正文；``no_op``——这一章没有归档过的场；
    ``blocked``——位置对不上（``scene_ids`` 是出问题的场）。``inputs`` 是算进这一章的有效记忆（``row_id`` 序），
    不论拼不拼得出来——流水线归档第 8 步把它记成产品的输入清单。
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
    ordered = sorted(
        inputs,
        key=lambda memory: (
            int(placed[memory.scene_id].scene_seq or 0),
            memory.scene_id,
            memory.created_at or "",
            memory.row_id,
        ),
    )
    return ChapterAggregateDerivation(
        "derived", "scene_memories_aggregated", inputs=inputs, memories=tuple(ordered)
    )


def _in_trash(card: SceneCard | None) -> bool:
    return card is not None and bool(card.trashed_flag)


def is_chapter_aggregate_of(content: str, parts: Sequence[str]) -> bool:
    """``content`` 是不是 ``parts`` 各用一次、按某个次序拼成的章汇总（拼法同 :attr:`ChapterAggregateDerivation.content`）。

    流水线第 8 步的产品只记输入清单（``row_id`` 序），不记拼的次序（场序，归档之后还可能再改），复验就只认这一点。
    一场的全文可能也出现在别的场里、或正是另一场的开头（短短的收尾场、重复的正文），所以不能按「在汇总里第一次出现的
    位置」排——那样对的汇总反被判损坏。这里从头逐段对，一段接不上就退回去换一段；走不通的「位置 + 还剩哪几段」记下来
    不再重走，几段互为开头时也不会一路试遍所有次序。
    """
    if len(content) != sum(map(len, parts)) + len(_SEPARATOR) * max(len(parts) - 1, 0):
        return False
    remaining = Counter(parts)
    candidates = sorted(remaining, key=len, reverse=True)
    dead_ends: set[tuple[int, tuple[int, ...]]] = set()

    def walk(pos: int, left: int) -> bool:
        if not left:
            return pos == len(content)
        state = (pos, tuple(remaining[text] for text in candidates))
        if state in dead_ends:
            return False
        for text in candidates:
            if not remaining[text] or not content.startswith(text, pos):
                continue
            end = pos + len(text)
            if left > 1:
                if not content.startswith(_SEPARATOR, end):
                    continue
                end += len(_SEPARATOR)
            remaining[text] -= 1
            found = walk(end, left - 1)
            remaining[text] += 1
            if found:
                return True
        dead_ends.add(state)
        return False

    return walk(0, len(parts))


class Aggregator:
    def __init__(self, session: Session) -> None:
        self.session = session

    def maybe_aggregate_volume(self, chapter_id: str) -> dict | None:
        """§2 summary tower: roll up a volume when a chapter completes a span boundary.

        Triggered at chapter finalization. When the just-finished chapter is the Nth
        (VOLUME_CHAPTER_SPAN) in display order with no later volume covering it, build a
        volume-level atmosphere summary from the chapters' final memories. Idempotent:
        re-running for the same window supersedes the prior volume summary.
        """
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.project_id is None or chapter.display_order is None:
            return {"status": "no_op", "reason": "chapter_unmapped"}

        project_id = chapter.project_id
        # Ordered, non-trashed chapters of this project up to and including the current one.
        ordered = list(self.session.execute(
            select(ChapterGoal)
            .where(
                ChapterGoal.project_id == project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.display_order.isnot(None),
                ChapterGoal.display_order <= chapter.display_order,
            )
            .order_by(ChapterGoal.display_order.asc())
        ).scalars().all())
        if len(ordered) % VOLUME_CHAPTER_SPAN != 0:
            return {"status": "no_op", "reason": "not_at_volume_boundary", "chapter_count": len(ordered)}

        volume_seq = len(ordered) // VOLUME_CHAPTER_SPAN
        window = ordered[-VOLUME_CHAPTER_SPAN:]
        return self.aggregate_volume_summary(project_id, volume_seq, [c.chapter_id for c in window])

    def aggregate_volume_summary(
        self, project_id: str, volume_seq: int, chapter_ids: list[str],
    ) -> dict | None:
        """Build a volume-level atmosphere summary from the chapters' final memories.

        Deterministic by default (concatenated, clearly labeled as atmosphere — NOT
        facts, per §2). Supersedes any prior volume summary for the same (project,seq).
        """
        if not chapter_ids:
            return {"status": "no_op", "reason": "empty_window"}

        memories = list(self.session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id.in_(chapter_ids),
                ChapterMemory.aggregate_stage == "final",
                ChapterMemory.active_flag == 1,
            )
        ).scalars().all())
        if not memories:
            return {"status": "no_op", "reason": "no_chapter_memories"}

        # Preserve chapter order in the window for a coherent far-horizon arc.
        order = {cid: i for i, cid in enumerate(chapter_ids)}
        memories.sort(key=lambda m: order.get(m.chapter_id, 0))
        atmosphere = "\n\n".join(
            f"【第{order.get(m.chapter_id, 0) + 1}章 氛围】{m.content}".strip()
            for m in memories if m.content
        )

        # Supersede prior volume summary for this (project, seq).
        prior = list(self.session.execute(
            select(VolumeSummary).where(
                VolumeSummary.project_id == project_id,
                VolumeSummary.volume_seq == volume_seq,
                VolumeSummary.active_flag == 1,
            )
        ).scalars().all())
        for old in prior:
            old.active_flag = 0
            old.runtime_eligible = 0
            old.runtime_eligibility_basis = "superseded"

        row_id = f"volume_summary_{project_id}_v{volume_seq}_{len(prior) + 1}"
        summary = VolumeSummary(
            row_id=row_id,
            project_id=project_id,
            volume_seq=volume_seq,
            chapter_id_start=chapter_ids[0],
            chapter_id_end=chapter_ids[-1],
            chapter_count=len(chapter_ids),
            atmosphere_summary=atmosphere,
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
        self.session.add(summary)
        self.session.flush()
        return {
            "status": "created",
            "reason": "volume_rolled_up",
            "volume_summary_row_id": row_id,
            "volume_seq": volume_seq,
            "chapter_count": len(chapter_ids),
        }

    def derive_final_aggregate(self, chapter_id: str) -> ChapterAggregateDerivation:
        """这一章此刻的章汇总（读时现拼，只读）。"""
        return self.derive_final_aggregates([chapter_id])[chapter_id]

    def derive_final_aggregates(self, chapter_ids: Iterable[str]) -> dict[str, ChapterAggregateDerivation]:
        """:meth:`derive_final_aggregate` 的批量版本（文学质量巡检一次看全书）：章数多少都是两条查询。"""
        ids = list(dict.fromkeys(chapter_id for chapter_id in chapter_ids if chapter_id))
        if not ids:
            return {}
        memories = list(self.session.execute(
            select(SceneMemory).where(SceneMemory.chapter_id.in_(ids), SceneMemory.active_flag == 1)
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
        by_chapter: dict[str, list[SceneMemory]] = {chapter_id: [] for chapter_id in ids}
        for memory in memories:
            by_chapter[memory.chapter_id].append(memory)
        return {
            chapter_id: derive_chapter_aggregate(chapter_id, members, cards)
            for chapter_id, members in by_chapter.items()
        }

    def run_final_aggregate(self, chapter_id: str) -> dict | None:
        """重建这一章存下来的章汇总：拼得出来就落一版新的、旧的标为被取代；拼不出来原样回报（``no_op`` / ``blocked``）。"""
        # 目录冷启动章可能没有状态行（审计 P-1）：缺行补建。
        # 以前这里还有一道「回填 / 回溯中」闸门（aggregate_block_reason、chapter_backfill_pending_count）：写它们的
        # 章回填 / 手动挂起在 2026-09 减法里删了，库里只剩默认值 none / 0，闸门随之删掉（B03-18）。
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
