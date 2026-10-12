"""故事记忆 · 各章此刻的状态（只读推导）：一章写完没有、它的章摘要是最新的还是旧了、写它之前台账里还开着哪几条线。

``story_memory``（写章摘要、后台工人、成稿中心的总览）与 ``story_editorial``（编辑通读：什么时候该读、读到哪一章）都从
这里取同一份状态。``source_hash`` 绑住整章正文、上一份摘要的内容与写它之前台账里还开着的线（编号与文字）：任一变了
摘要就旧了。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterDigest, ChapterGoal
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_plain
from novel_system.services.scene_lookup import require_project_chapter
from novel_system.services.story_memory_digest import (
    StoryThread,
    active_digests,
    chapter_display_title,
    fold_threads,
    live_chapters_in_order,
    normalize_digest,
)


@dataclass(slots=True)
class ChapterMemoryState:
    """一章此刻的故事记忆状态（只读推导）。"""

    chapter: ChapterGoal
    number: int
    body: str
    body_hash: str
    scene_count: int
    written_scene_count: int
    eligible: bool
    previous: ChapterDigest | None
    previous_number: int | None
    # 写这一章之前台账里还开着的线（输入给模型的那一份，作者标记不算）：{ref, thread_id, thread, opened_in, planned_payoff_in}
    thread_rows: list[dict[str, Any]]
    source_hash: str
    digest: ChapterDigest | None

    @property
    def status(self) -> str:
        """``not_ready``（还没写完）/ ``missing`` / ``fresh`` / ``stale``（正文或上一份摘要变了）。"""
        if self.digest is None:
            return "missing" if self.eligible else "not_ready"
        return "fresh" if self.digest.source_hash == self.source_hash else "stale"

    @property
    def auto_target(self) -> bool:
        """后台该写这一章：写完了、没有摘要，或摘要旧了而且不是作者改过的。"""
        if not self.eligible:
            return False
        status = self.status
        return status == "missing" or (status == "stale" and self.digest is not None and self.digest.origin == "llm")


def _source_hash(body_hash: str, previous: ChapterDigest | None, thread_rows: list[dict[str, Any]]) -> str:
    return sha256_json_plain(
        {
            "body": body_hash,
            "previous": previous.content_hash if previous else None,
            "threads": [[row["thread_id"], row["thread"], row["opened_in"], row["planned_payoff_in"]] for row in thread_rows],
        }
    )


def _thread_rows(threads: dict[str, StoryThread], numbers: Mapping[str, int]) -> list[dict[str, Any]]:
    """台账里还开着的线（按各章摘要；作者标记不算——模型照样跟踪它们），按埋下的先后编 ``T1`` …。"""
    rows: list[dict[str, Any]] = []
    for thread in threads.values():
        if thread.status != "open":
            continue
        rows.append(
            {
                "ref": f"T{len(rows) + 1}",
                "thread_id": thread.thread_id,
                "thread": thread.label,
                "opened_in": thread.opened_number,
                "planned_payoff_in": numbers.get(thread.payoff_chapter_id or ""),
            }
        )
    return rows


def chapter_memory_states(session: Session, project_id: str) -> list[ChapterMemoryState]:
    """作品各活章按故事顺序的故事记忆状态。

    一章「写完」= 至少一场有当前终稿，且各活场都有，或后面某一章已经有终稿（作者往后写了，这一章缺的场不再等）。
    上一份摘要 = 往前最近一章的当前摘要（中间没写的章跳过）。
    """
    chapters = live_chapters_in_order(session, project_id)
    if not chapters:
        return []
    manuscripts = ChapterManuscriptService(session)
    bodies = [manuscripts.assembled_body(chapter.chapter_id) for chapter in chapters]
    digests = active_digests(session, [chapter.chapter_id for chapter in chapters])
    later_written = [False] * len(chapters)
    seen = False
    for index in range(len(chapters) - 1, -1, -1):
        later_written[index] = seen
        seen = seen or bool(bodies[index]["generated_scene_count"])
    states: list[ChapterMemoryState] = []
    previous: ChapterDigest | None = None
    previous_number: int | None = None
    numbers = {chapter.chapter_id: index + 1 for index, chapter in enumerate(chapters)}
    threads: dict[str, StoryThread] = {}
    for index, (chapter, body) in enumerate(zip(chapters, bodies)):
        written = int(body["generated_scene_count"] or 0)
        complete = written > 0 and not body["missing_scene_ids"]
        digest = digests.get(chapter.chapter_id)
        thread_rows = _thread_rows(threads, numbers)
        states.append(
            ChapterMemoryState(
                chapter=chapter,
                number=index + 1,
                body=str(body["content"] or ""),
                body_hash=str(body["body_hash"] or ""),
                scene_count=int(body["scene_count"] or 0),
                written_scene_count=written,
                eligible=written > 0 and (complete or later_written[index]),
                previous=previous,
                previous_number=previous_number,
                thread_rows=thread_rows,
                source_hash=_source_hash(str(body["body_hash"] or ""), previous, thread_rows),
                digest=digest,
            )
        )
        if digest is not None:
            previous, previous_number = digest, index + 1
            fold_threads([(index + 1, chapter.chapter_id, normalize_digest(digest.digest_json))], threads)
    return states


def serialize_chapter_state(state: ChapterMemoryState) -> dict[str, Any]:
    digest = state.digest
    return {
        "chapter_id": state.chapter.chapter_id,
        "number": state.number,
        "title": chapter_display_title(state.chapter),
        "status": state.status,
        "eligible": state.eligible,
        "scene_count": state.scene_count,
        "written_scene_count": state.written_scene_count,
        "digest_row_id": digest.row_id if digest else None,
        "origin": digest.origin if digest else None,
        "updated_at": digest.created_at if digest else None,
        "digest": normalize_digest(digest.digest_json) if digest else None,
    }


def chapter_state(session: Session, project_id: str, chapter_id: str) -> ChapterMemoryState:
    """一章的状态；章不在这部作品里 → 404（``require_project_chapter``），在回收站里 → 404 ``CHAPTER_NOT_FOUND``。"""
    require_project_chapter(session, project_id, chapter_id)
    for state in chapter_memory_states(session, project_id):
        if state.chapter.chapter_id == chapter_id:
            return state
    raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)


__all__ = [
    "ChapterMemoryState",
    "chapter_display_title",
    "chapter_memory_states",
    "chapter_state",
    "serialize_chapter_state",
]
