"""故事记忆的读与渲染：起草提示词的「前情」（Story So Far）、线索台账与成稿中心的章摘要列表都从这里取（只读）。

章摘要（``ChapterDigest``）由 ``services/story_memory.py`` 在一章写完之后写：发生了什么、章末停在哪、人物的持久
变化，以及这一章对**线索台账**的更新（``threads``：埋下了哪几条线、推进 / 兑现了台账里的哪几条）。这里管四件事：

- 摘要的标准形状（``normalize_digest``，LLM 输出与作者修改都过它）与内容哈希（下一章判旧用）；
- 一部作品的活章按故事顺序（``live_chapters_in_order``，与章间过渡、前文声音锚同一口径：``display_order`` 再
  ``chapter_id``，跳过回收站里的章）与它们各自当前的那一份摘要；
- **线索台账**（2026-10-12）：不另存，按故事顺序把各章摘要里的线索更新叠起来（``fold_threads``）——每条线有稳定
  的 id（``<埋下它的章 id>-T<n>``）、在哪章埋下、最近在哪章被推进、计划在哪章兑现（章摘要对照后面几章的计划给出）、
  收了没有。作者的标记（``StoryThreadMark``：放下 / 已收）叠在上面一层。一条还开着的线超过 ``DORMANT_AFTER_CHAPTERS``
  章没碰、后面也没计划兑现，或计划兑现的那一章已经写过了还没兑现，就带一个提醒（``thread_alert``）；
- 起草某一场时的「前情」：这一章之前各章的摘要（最近 ``RECENT_FULL_CHAPTERS`` 章给全文摘要、章末与人物变化，更早的
  只给一句）+ 至今未收的线（台账里还开着、作者没放下的，带埋下 / 推进 / 计划兑现的章与提醒）。预算紧时
  ``compress_story_so_far`` 只留最近 ``COMPRESSED_RECENT_CHAPTERS`` 章与未收的线（``context_budget.BUDGET_LADDER``）。

本模块只依赖 ORM、哈希与目录章名两个叶子：``bundle_builder`` 与 ``context_budget`` 都引它。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterDigest, ChapterGoal, SceneCard, StoryThreadMark
from novel_system.services.catalog_labels import chapter_title
from novel_system.services.hash_engine import sha256_json_normalized

STORY_SO_FAR_SECTION_KEY = "story_so_far"

# 最近这么多章给全文摘要、章末与人物变化；更早的章只给摘要的第一句
RECENT_FULL_CHAPTERS = 8
# 预算紧时只留最近这么多章（与未收的线）
COMPRESSED_RECENT_CHAPTERS = 3
_FIRST_SENTENCE_MAX_CHARS = 80

# 渲染出来的「前情」由空行隔开的块组成：开头一块说明、每章一块、最后一块未收的线（压缩器按块取舍）
STORY_SO_FAR_HEADER = (
    "【前情：此前各章的摘要（AI 在每章写完后整理，作者可改）。这些是已经写进正文的事实，本场不得与之矛盾；"
    "与前文正文冲突时以正文为准】"
)
OPEN_THREADS_HEADER = "【至今未收的线（只是提醒，不要求本场处理）】"
_SENTENCE_END_RE = re.compile(r"[。！？!?…]+[”’」』）)]*")

_TEXT_LIMITS = {"summary": 600, "ending_state": 240}
_ITEM_LIMIT = 30

# 一章对台账的更新：opened（这一章埋下的新线）/ advanced（推进了台账里的一条）/ resolved（兑现了）
THREAD_UPDATE_STATUSES: tuple[str, ...] = ("opened", "advanced", "resolved")
_THREAD_UPDATE_LIMIT = 40
# 一条还开着的线这么多章没被碰过、后面也没有计划兑现的章 → 提醒「冷了」
DORMANT_AFTER_CHAPTERS = 4
# 「前情」里最多列这么多条未收的线（有提醒的排前面，其余按最近被推进的先后）
MAX_DRAFTING_THREADS = 20
THREAD_MARK_STATUSES: tuple[str, ...] = ("dropped", "resolved")


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _items(value: Any, fields: Mapping[str, int]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        item = {key: _text(raw.get(key), limit) for key, limit in fields.items()}
        if all(item.values()):
            items.append(item)
        if len(items) >= _ITEM_LIMIT:
            break
    return items


def _thread_updates(value: Any) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        thread_id = _text(raw.get("id"), 120)
        label = _text(raw.get("thread"), 120)
        status = _text(raw.get("status"), 20)
        if not thread_id or not label or status not in THREAD_UPDATE_STATUSES or thread_id in seen:
            continue
        seen.add(thread_id)
        updates.append(
            {
                "id": thread_id,
                "thread": label,
                "status": status,
                "note": _text(raw.get("note"), 160),
                "payoff_chapter_id": _text(raw.get("payoff_chapter_id"), 120) or None,
            }
        )
        if len(updates) >= _THREAD_UPDATE_LIMIT:
            break
    return updates


def normalize_digest(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """章摘要的标准形状：四个键都在，文字去首尾空白并截到上限，列表成员缺字段的丢掉。

    ``threads`` 的成员 = ``{id, thread, status, note, payoff_chapter_id}``：id 由 ``story_memory`` 在存之前定好（台账里
    已有的线用它的 id，新埋下的按 ``<章 id>-T<n>`` 编），这里不编号。
    """
    raw = raw or {}
    return {
        "summary": _text(raw.get("summary"), _TEXT_LIMITS["summary"]),
        "ending_state": _text(raw.get("ending_state"), _TEXT_LIMITS["ending_state"]),
        "character_changes": _items(raw.get("character_changes"), {"character": 40, "change": 160}),
        "threads": _thread_updates(raw.get("threads")),
    }


def digest_content_hash(digest_json: Mapping[str, Any]) -> str:
    return sha256_json_normalized(normalize_digest(digest_json))


def live_chapters_in_order(session: Session, project_id: str) -> list[ChapterGoal]:
    """作品的活章按故事顺序（``display_order`` 再 ``chapter_id``；回收站里的章不算）。"""
    return list(
        session.execute(
            select(ChapterGoal)
            .where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
            .order_by(ChapterGoal.display_order.asc(), ChapterGoal.chapter_id.asc())
        ).scalars()
    )


def active_digests(session: Session, chapter_ids: Iterable[str]) -> dict[str, ChapterDigest]:
    """每章当前那一份摘要（``active``；万一有两条取最新的）。"""
    ids = list(dict.fromkeys(chapter_id for chapter_id in chapter_ids if chapter_id))
    if not ids:
        return {}
    rows = session.execute(
        select(ChapterDigest)
        .where(ChapterDigest.chapter_id.in_(ids), ChapterDigest.status == "active")
        .order_by(ChapterDigest.created_at.asc(), ChapterDigest.row_id.asc())
    ).scalars()
    return {row.chapter_id: row for row in rows}


def chapter_display_title(chapter: ChapterGoal) -> str:
    """章名（与目录同一条规则，``catalog_labels.chapter_title``）；什么都没起、只剩章 id 时为空。"""
    title = chapter_title(chapter)
    return "" if title == chapter.chapter_id else title


def chapter_label(number: int, chapter: ChapterGoal) -> str:
    title = chapter_display_title(chapter)
    return f"第{number}章《{title}》" if title else f"第{number}章"


# ---------------------------------------------------------------------- 线索台账
@dataclass(slots=True)
class StoryThread:
    """台账里的一条线（由各章摘要的 ``threads`` 叠出来）。章号是它在活章序里的位置（从 1 起）。"""

    thread_id: str
    label: str
    opened_chapter_id: str
    opened_number: int
    last_number: int
    status: str = "open"  # 按各章摘要：open / resolved
    resolved_number: int | None = None
    payoff_chapter_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    mark: str | None = None  # 作者的标记（label 对得上时才生效）：dropped / resolved
    mark_note: str = ""

    @property
    def is_open(self) -> bool:
        """还开着：各章摘要没说它收了，作者也没放下 / 判它收了。"""
        return self.status == "open" and self.mark is None


def fold_threads(
    entries: Iterable[tuple[int, str, Mapping[str, Any]]],
    threads: dict[str, StoryThread] | None = None,
) -> dict[str, StoryThread]:
    """按故事顺序叠各章摘要的线索更新：``entries`` = (章号, 章 id, 摘要)；返回 id → 线（按埋下的先后）。给了
    ``threads`` 就接着它往下叠（原地改、也返回它）。

    认不出 id 的「推进」当作这一章新埋下的线（前面那一章的摘要重写过、编号变了）；认不出 id 的「兑现」不算数。
    兑现之后又被推进 = 重新打开。
    """
    threads = {} if threads is None else threads
    for number, chapter_id, digest in entries:
        for update in digest.get("threads") or []:
            thread_id = str(update.get("id") or "")
            status = str(update.get("status") or "")
            if not thread_id or status not in THREAD_UPDATE_STATUSES:
                continue
            thread = threads.get(thread_id)
            if thread is None:
                if status == "resolved":
                    continue
                thread = StoryThread(
                    thread_id=thread_id,
                    label=str(update.get("thread") or ""),
                    opened_chapter_id=chapter_id,
                    opened_number=number,
                    last_number=number,
                )
                threads[thread_id] = thread
            else:
                thread.last_number = number
            if status == "resolved":
                thread.status, thread.resolved_number = "resolved", number
            elif thread.status == "resolved":
                thread.status, thread.resolved_number = "open", None
            if update.get("payoff_chapter_id"):
                thread.payoff_chapter_id = str(update["payoff_chapter_id"])
            thread.history.append({"number": number, "status": status, "note": str(update.get("note") or "")})
    return threads


def thread_marks(session: Session, project_id: str) -> dict[str, StoryThreadMark]:
    return {
        row.thread_id: row
        for row in session.execute(select(StoryThreadMark).where(StoryThreadMark.project_id == project_id)).scalars()
    }


def apply_thread_marks(threads: Iterable[StoryThread], marks: Mapping[str, StoryThreadMark]) -> None:
    """作者的标记叠上去：标记时那条线的文字与现在对得上才生效（摘要重写后同一个 id 可能换成了另一条线）。"""
    for thread in threads:
        mark = marks.get(thread.thread_id)
        if mark is not None and mark.status in THREAD_MARK_STATUSES and mark.label_snapshot == thread.label:
            thread.mark, thread.mark_note = mark.status, str(mark.note or "")


def thread_alert(thread: StoryThread, current_number: int, payoff_number: int | None) -> tuple[str | None, int]:
    """(提醒, 几章没碰)。``current_number`` = 写到第几章了。提醒：``payoff_missed``（计划兑现的那一章写过了还没兑现）
    / ``cold``（``DORMANT_AFTER_CHAPTERS`` 章没碰、后面也没有计划兑现的章）/ ``None``。"""
    if not thread.is_open:
        return None, 0
    dormant = max(0, current_number - thread.last_number)
    if payoff_number is not None and payoff_number <= current_number:
        return "payoff_missed", dormant
    if payoff_number is None and dormant >= DORMANT_AFTER_CHAPTERS:
        return "cold", dormant
    return None, dormant


def project_threads(
    session: Session,
    project_id: str,
    *,
    chapters: list[ChapterGoal] | None = None,
    digests: Mapping[str, ChapterDigest] | None = None,
    before_index: int | None = None,
) -> tuple[list[StoryThread], dict[str, int]]:
    """一部作品的台账（作者标记已叠上）：前 ``before_index`` 章（默认全部）的摘要叠出来；另给章 id → 章号。"""
    chapters = live_chapters_in_order(session, project_id) if chapters is None else chapters
    if digests is None:
        digests = active_digests(session, [chapter.chapter_id for chapter in chapters])
    numbers = {chapter.chapter_id: index + 1 for index, chapter in enumerate(chapters)}
    scope = chapters if before_index is None else chapters[:before_index]
    threads = fold_threads(
        (index + 1, chapter.chapter_id, normalize_digest(digests[chapter.chapter_id].digest_json))
        for index, chapter in enumerate(scope)
        if chapter.chapter_id in digests
    )
    apply_thread_marks(threads.values(), thread_marks(session, project_id))
    return list(threads.values()), numbers


def _open_threads_block(threads: list[StoryThread], numbers: Mapping[str, int], current_number: int) -> str | None:
    rows: list[tuple[int, int, str]] = []
    for thread in threads:
        if not thread.is_open:
            continue
        payoff_number = numbers.get(thread.payoff_chapter_id or "")
        alert, dormant = thread_alert(thread, current_number, payoff_number)
        detail = [f"第{thread.opened_number}章埋下"]
        if thread.last_number != thread.opened_number:
            detail.append(f"第{thread.last_number}章最近推进")
        if payoff_number is not None and payoff_number > current_number:
            detail.append(f"计划第{payoff_number}章兑现")
        line = f"- {thread.label}（{'；'.join(detail)}）"
        if alert == "payoff_missed":
            line += f"［计划第{payoff_number}章兑现，还没兑现］"
        elif alert == "cold":
            line += f"［已经 {dormant} 章没碰］"
        rows.append((0 if alert else 1, -thread.last_number, line))
    if not rows:
        return None
    rows.sort(key=lambda row: (row[0], row[1]))
    lines = [row[2] for row in rows[:MAX_DRAFTING_THREADS]]
    if len(rows) > MAX_DRAFTING_THREADS:
        lines.append(f"- 另有 {len(rows) - MAX_DRAFTING_THREADS} 条较早的线（略）")
    return "\n".join([OPEN_THREADS_HEADER, *lines])


def _first_sentence(text: str) -> str:
    match = _SENTENCE_END_RE.search(text)
    sentence = text[: match.end()] if match is not None else text
    return sentence if len(sentence) <= _FIRST_SENTENCE_MAX_CHARS else sentence[:_FIRST_SENTENCE_MAX_CHARS] + "…"


def _chapter_block(label: str, digest: Mapping[str, Any], *, full: bool) -> str:
    summary = str(digest.get("summary") or "").strip()
    if not full:
        return f"{label}：{_first_sentence(summary)}"
    lines = [f"{label}：{summary}"]
    ending = str(digest.get("ending_state") or "").strip()
    if ending:
        lines.append(f"章末：{ending}")
    changes = [
        f"{item['character']}——{item['change']}"
        for item in digest.get("character_changes") or []
        if item.get("character") and item.get("change")
    ]
    if changes:
        lines.append("人物变化：" + "；".join(changes))
    return "\n".join(lines)


def story_so_far(session: Session, scene: SceneCard) -> dict[str, Any] | None:
    """写这一场时的「前情」：这一章之前各活章的摘要 + 台账里至今未收的线（作者放下 / 判收的不列）。

    返回 ``{"text", "digest_row_ids", "missing_chapter_ids"}``；这一章之前没有任何一章有摘要 → ``None``。还没写好
    摘要的章照样占一行（「摘要还没写好」），让模型知道那里有一段它看不见的故事。
    """
    if not scene.project_id or not scene.chapter_id:
        return None
    chapters = live_chapters_in_order(session, scene.project_id)
    position = next((index for index, chapter in enumerate(chapters) if chapter.chapter_id == scene.chapter_id), None)
    if position is None or position == 0:
        return None
    earlier = chapters[:position]
    digests = active_digests(session, [chapter.chapter_id for chapter in earlier])
    if not digests:
        return None
    blocks = [STORY_SO_FAR_HEADER]
    missing: list[str] = []
    full_from = max(0, len(earlier) - RECENT_FULL_CHAPTERS)
    for index, chapter in enumerate(earlier):
        label = chapter_label(index + 1, chapter)
        row = digests.get(chapter.chapter_id)
        if row is None:
            missing.append(chapter.chapter_id)
            blocks.append(f"{label}：（这一章的摘要还没写好）")
            continue
        blocks.append(_chapter_block(label, normalize_digest(row.digest_json), full=index >= full_from))
    ledger, numbers = project_threads(session, scene.project_id, chapters=chapters, digests=digests, before_index=position)
    threads = _open_threads_block(ledger, numbers, current_number=position)
    if threads:
        blocks.append(threads)
    return {
        "text": "\n\n".join(blocks),
        "digest_row_ids": [digests[chapter.chapter_id].row_id for chapter in earlier if chapter.chapter_id in digests],
        "missing_chapter_ids": missing,
    }


def compress_story_so_far(text: str) -> str:
    """预算紧时的「前情」：说明、最近 ``COMPRESSED_RECENT_CHAPTERS`` 章、未收的线。"""
    blocks = [block for block in str(text or "").split("\n\n") if block.strip()]
    if not blocks:
        return ""
    header = blocks[0] if blocks[0] == STORY_SO_FAR_HEADER else None
    threads = blocks[-1] if blocks[-1].startswith(OPEN_THREADS_HEADER) else None
    chapters = blocks[(1 if header else 0) : (len(blocks) - 1 if threads else len(blocks))]
    kept = [block for block in (header, *chapters[-COMPRESSED_RECENT_CHAPTERS:], threads) if block]
    return "\n\n".join(kept)
