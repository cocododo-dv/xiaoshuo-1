"""风格参考 v3 — 段落根哈希的快速路径（叶子模块）。

根哈希的口径与 ``runtime_contract.compute_paragraph_root`` 完全相同（按 paragraph_index 升序，
root = sha256(Σ ``f"{index}\\x1f"`` + sha256(text) + ``"\\x1e"``)），但只取 ``paragraph_index`` / ``text``
两列、不建 ORM 对象（真实参考书 2.6 万段：0.6 秒 → 约 0.1 秒）；再往上一层先读 ``book.stats_json`` 里存好的
``paragraph_root_sha256`` / ``paragraph_count``（契约文档 §3.1：改段落文本或行的写入者负责 pop 这两个键）。

写回用 SQLite 的 ``json_set`` 原子合并，只动这两个键——同一行 ``stats_json`` 还有分类作业在写
（``paragraph_types_revision`` 等），整列读改写会互相覆盖。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph
from novel_system.services.hash_engine import sha256_text

ROOT_KEY = "paragraph_root_sha256"
COUNT_KEY = "paragraph_count"


def compute_paragraph_root_fast(session: Session, book_id: str) -> tuple[str, int]:
    """(根哈希, 段数)，书没有段落时 ("", 0)。与 ``runtime_contract.compute_paragraph_root`` 逐位相同。"""
    digest = hashlib.sha256()
    count = 0
    rows = session.execute(
        select(StyleReferenceParagraph.paragraph_index, StyleReferenceParagraph.text)
        .where(StyleReferenceParagraph.book_id == str(book_id))
        .order_by(StyleReferenceParagraph.paragraph_index)
    )
    for index, text in rows:
        try:
            number = int(index or 0)
        except (TypeError, ValueError):
            number = 0
        digest.update(f"{number}\x1f".encode("utf-8"))
        digest.update(sha256_text(text).encode("utf-8"))
        digest.update(b"\x1e")
        count += 1
    if count == 0:
        return "", 0
    return digest.hexdigest(), count


def stored_paragraph_root(stats_json: Any) -> tuple[str, int] | None:
    """``stats_json`` 里存好的 (根哈希, 段数)；缺失 / 形状不对 → None。"""
    if not isinstance(stats_json, dict):
        return None
    root = stats_json.get(ROOT_KEY)
    count = stats_json.get(COUNT_KEY)
    if not isinstance(root, str) or not root:
        return None
    try:
        return root, int(count)
    except (TypeError, ValueError):
        return None


def patch_book_stats(session: Session, book_id: str, values: dict[str, Any]) -> None:
    """原子地把几个顶层键写进 ``book.stats_json``（``json_set``，不覆盖别的键），并让会话里的书对象重读。"""
    if not values:
        return
    session.flush()
    args: list[Any] = []
    for key, value in values.items():
        args.extend([f"$.{key}", func.json(json.dumps(value, ensure_ascii=False))])
    session.execute(
        update(StyleReferenceBook)
        .where(StyleReferenceBook.book_id == str(book_id))
        .values(stats_json=func.json_set(func.coalesce(StyleReferenceBook.stats_json, "{}"), *args))
        .execution_options(synchronize_session=False)
    )
    book = session.get(StyleReferenceBook, str(book_id))
    if book is not None:
        session.expire(book, ["stats_json"])


def ensure_paragraph_root(session: Session, book_id: str) -> tuple[str, int]:
    """书的 (根哈希, 段数)：先读 stats_json，缺失时现算并写回（原子合并）。"""
    book = session.get(StyleReferenceBook, str(book_id))
    stored = stored_paragraph_root(book.stats_json if book is not None else None)
    if stored is not None:
        return stored
    root, count = compute_paragraph_root_fast(session, book_id)
    if book is not None and root:
        patch_book_stats(session, book_id, {ROOT_KEY: root, COUNT_KEY: count})
    return root, count


@dataclass(frozen=True)
class BookVersion:
    """一本参考书现在是哪一版（B04-21 / X01-09）：参考书读数的几个进程缓存——抄袭闸的段落索引与结果、规则 / 节奏
    校准的读数——都从这一次读库拼自己的键，不再各查各的。

    书的统计里存着段落根哈希时就用它（改段落文本或行的写入者负责把它 pop 掉，契约 §3.1），不数段落表；没存时现数
    段数、总字数与最新段落时间（一条聚合查询）。都现读库：会话在提交时不过期对象，身份映射里的书可能是别的连接改之前
    的样子。"""

    book_id: str
    title: str | None
    root: str  # 统计里存的段落根哈希；没存 → ""
    stored_count: Any  # 统计里存的段数（原样，可能是 None）
    paragraphs: int  # 段数：存了根哈希取存的数，没存现数
    total_chars: int  # 段落总字数（只在没存根哈希时现加）
    latest: str  # 最新段落时间（同上）
    types_revision: Any  # 分类作业每次成功加一（就地重标段型不改段数也不改时间）
    scene_breaks: Any  # 导入时记下的场界（原样）
    checksum: str
    created_at: str

    @property
    def text_key(self) -> tuple[Any, ...]:
        """段落**文本**的版本（抄袭闸的索引 / 结果缓存键）；同一个书号删了重导入也认得出来（校验和、建书时间）。"""
        identity = (self.checksum, self.created_at)
        if self.root:
            return ("root", self.root, str(self.stored_count if self.stored_count is not None else ""), *identity)
        return ("scan", self.paragraphs, self.total_chars, self.latest, *identity)


def read_book_version(session: Session, book_id: str) -> BookVersion | None:
    """书的版本信息；书不存在 → None。"""
    row = session.execute(
        select(
            StyleReferenceBook.title,
            func.json_extract(StyleReferenceBook.stats_json, f"$.{ROOT_KEY}"),
            func.json_extract(StyleReferenceBook.stats_json, f"$.{COUNT_KEY}"),
            func.json_extract(StyleReferenceBook.stats_json, "$.paragraph_types_revision"),
            func.json_extract(StyleReferenceBook.stats_json, "$.scene_breaks"),
            StyleReferenceBook.text_checksum,
            StyleReferenceBook.created_at,
        ).where(StyleReferenceBook.book_id == str(book_id))
    ).one_or_none()
    if row is None:
        return None
    title, root, stored_count, types_revision, scene_breaks, checksum, created_at = row
    if isinstance(root, str) and root:
        try:
            paragraphs = int(stored_count or 0)
        except (TypeError, ValueError):
            paragraphs = 0
        total, latest = 0, ""
    else:
        root = ""
        count, total, latest = session.execute(
            select(
                func.count(StyleReferenceParagraph.paragraph_id),
                func.coalesce(func.sum(func.length(StyleReferenceParagraph.text)), 0),
                func.max(StyleReferenceParagraph.created_at),
            ).where(StyleReferenceParagraph.book_id == str(book_id))
        ).one()
        paragraphs, total, latest = int(count or 0), int(total or 0), str(latest or "")
    return BookVersion(
        book_id=str(book_id),
        title=title,
        root=root,
        stored_count=stored_count,
        paragraphs=paragraphs,
        total_chars=total,
        latest=latest,
        types_revision=types_revision,
        scene_breaks=scene_breaks,
        checksum=str(checksum or ""),
        created_at=str(created_at or ""),
    )


__all__ = [
    "COUNT_KEY",
    "ROOT_KEY",
    "BookVersion",
    "compute_paragraph_root_fast",
    "ensure_paragraph_root",
    "patch_book_stats",
    "read_book_version",
    "stored_paragraph_root",
]
