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
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph

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
        digest.update(hashlib.sha256(str(text or "").encode("utf-8")).hexdigest().encode("utf-8"))
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


__all__ = [
    "COUNT_KEY",
    "ROOT_KEY",
    "compute_paragraph_root_fast",
    "ensure_paragraph_root",
    "patch_book_stats",
    "stored_paragraph_root",
]
