"""故事记忆的读与渲染：起草提示词的「前情」（Story So Far）与成稿中心的章摘要列表都从这里取（只读）。

章摘要（``ChapterDigest``）由 ``services/story_memory.py`` 在一章写完之后写：发生了什么、章末停在哪、人物的持久
变化、还没收的线（一章章往下带）。这里只管三件事：

- 摘要的标准形状（``normalize_digest``，LLM 输出与作者修改都过它）与内容哈希（下一章判旧用）；
- 一部作品的活章按故事顺序（``live_chapters_in_order``，与章间过渡、前文声音锚同一口径：``display_order`` 再
  ``chapter_id``，跳过回收站里的章）与它们各自当前的那一份摘要；
- 起草某一场时的「前情」：这一章之前各章的摘要（最近 ``RECENT_FULL_CHAPTERS`` 章给全文摘要、章末与人物变化，更早的
  只给一句）+ 最近一份摘要里至今未收的线。预算紧时 ``compress_story_so_far`` 只留最近 ``COMPRESSED_RECENT_CHAPTERS``
  章与未收的线（``context_budget.BUDGET_LADDER``）。

本模块只依赖 ORM、哈希与目录章名两个叶子：``bundle_builder`` 与 ``context_budget`` 都引它。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterDigest, ChapterGoal, SceneCard
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


def _opened_in(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def normalize_digest(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """章摘要的标准形状：五个键都在，文字去首尾空白并截到上限，列表成员缺字段的丢掉。"""
    raw = raw or {}
    open_threads = []
    for item in raw.get("open_threads") if isinstance(raw.get("open_threads"), list) else []:
        if not isinstance(item, Mapping):
            continue
        thread = _text(item.get("thread"), 120)
        if thread:
            open_threads.append({"thread": thread, "opened_in": _opened_in(item.get("opened_in"))})
        if len(open_threads) >= _ITEM_LIMIT:
            break
    return {
        "summary": _text(raw.get("summary"), _TEXT_LIMITS["summary"]),
        "ending_state": _text(raw.get("ending_state"), _TEXT_LIMITS["ending_state"]),
        "character_changes": _items(raw.get("character_changes"), {"character": 40, "change": 160}),
        "open_threads": open_threads,
        "resolved_threads": _items(raw.get("resolved_threads"), {"thread": 120, "resolution": 160}),
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


def _open_threads_block(threads: list[Mapping[str, Any]]) -> str | None:
    lines = []
    for item in threads:
        thread = str(item.get("thread") or "").strip()
        if not thread:
            continue
        opened_in = item.get("opened_in")
        lines.append(f"- {thread}（第{opened_in}章起）" if opened_in else f"- {thread}")
    return "\n".join([OPEN_THREADS_HEADER, *lines]) if lines else None


def story_so_far(session: Session, scene: SceneCard) -> dict[str, Any] | None:
    """写这一场时的「前情」：这一章之前各活章的摘要 + 最近一份摘要里未收的线。

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
    latest: ChapterDigest | None = None
    full_from = max(0, len(earlier) - RECENT_FULL_CHAPTERS)
    for index, chapter in enumerate(earlier):
        label = chapter_label(index + 1, chapter)
        row = digests.get(chapter.chapter_id)
        if row is None:
            missing.append(chapter.chapter_id)
            blocks.append(f"{label}：（这一章的摘要还没写好）")
            continue
        latest = row
        blocks.append(_chapter_block(label, normalize_digest(row.digest_json), full=index >= full_from))
    threads = _open_threads_block(normalize_digest(latest.digest_json)["open_threads"]) if latest else None
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
