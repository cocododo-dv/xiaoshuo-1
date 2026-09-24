"""风格参考 v3（2026-09-23）— 持久化的全书样例窗口索引（``style_reference_windows``）。

过去窗口索引是 ``profile_json.exemplar_windows`` 里的一坨 JSON（画像 68% 的体积，段落表一变就过期），或者每个进程
惰性重算一遍、不落库。现在一本书一组行：

- **切窗**：``structure.split_book_chapters``（唯一切章器）+ 本模块的 :func:`cut_chapter_windows`（≤60 段 /
  ≤4,000 字、不跨章题与场界、<600 字不要），章号 / 位置（opening / closing / middle / whole）与结构画像一致
  （切窗规则原在 ``exemplar_index.py``，P7 并入这里）；
- **特征**：每窗 ``features_json`` = 测量核在这一窗正文上的全部特征——「像不像」读数的参照分布就是作者自己
  这些窗口的分布（``fidelity.py``）；``dialogue_share`` 取测量核的唯一对白占比；``type_mix_json`` 是段型构成；
- **典型度**：这一窗在本书自己的窗口分布里有多典型（各特征稳健 z 的 |z| 均值取负，越大越典型）——不是离
  鲁迅 / 朱自清基线多远；
- **标签**：学习作业给每窗打的场面 / 情绪 / 维度（``set_window_tags``，词表见 ``tags.py``；v2 起不再有手法）。

失效（契约文档 §3.1）：``book.stats_json["window_index"] = {version, root, types_revision, kernel_version}``。
根哈希或索引版本变了整组重建；只有段落类型变了，就地重算段型构成、保留标签；测量核版本变了，重算特征与
典型度、保留标签（窗口边界只取决于正文与切窗规则）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceParagraph,
    StyleReferenceWindow,
    utcnow,
)
from novel_system.services.style_reference.measure import (
    FEATURE_NAMES,
    KERNEL_VERSION,
    kernel_features,
    measure_text,
    robust_center_scale,
)
from novel_system.services.style_reference.paragraph_root import (
    COUNT_KEY,
    ROOT_KEY,
    compute_paragraph_root_fast,
    patch_book_stats,
    stored_paragraph_root,
)
from novel_system.services.style_reference.structure import non_body_kind, split_book_chapters
from novel_system.services.style_reference.tags import normalize_window_tags

logger = logging.getLogger(__name__)

WINDOW_INDEX_VERSION = "exemplar_windows_v3"
INDEX_MARKER_KEY = "window_index"
TYPES_REVISION_KEY = "paragraph_types_revision"
# 典型度里单个特征的 |z| 上限（一个特征离谱不该压过其余几十个）
TYPICALITY_Z_CLIP = 6.0

_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def _book_lock(book_id: str) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.get(book_id)
        if lock is None:
            lock = _BUILD_LOCKS[book_id] = threading.Lock()
        return lock


def _types_revision(stats: Mapping[str, Any]) -> int:
    try:
        return int(stats.get(TYPES_REVISION_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _marker(root: str, types_revision: int, window_count: int) -> dict[str, Any]:
    return {
        "version": WINDOW_INDEX_VERSION,
        "root": root,
        "types_revision": int(types_revision),
        "kernel_version": KERNEL_VERSION,
        "window_count": int(window_count),
        "built_at": utcnow(),
    }


def index_marker(stats_json: Any) -> dict[str, Any] | None:
    """``stats_json`` 里的窗口索引标记（缺失 / 形状不对 → None）。"""
    if not isinstance(stats_json, Mapping):
        return None
    marker = stats_json.get(INDEX_MARKER_KEY)
    return dict(marker) if isinstance(marker, Mapping) else None


def marker_is_current(stats_json: Any) -> bool:
    """标记与书的现状（存好的根哈希、类型版本、索引 / 测量核版本）是否一致——一致即可直接读行。"""
    marker = index_marker(stats_json)
    stored = stored_paragraph_root(stats_json if isinstance(stats_json, dict) else None)
    if marker is None or stored is None:
        return False
    return (
        marker.get("version") == WINDOW_INDEX_VERSION
        and marker.get("root") == stored[0]
        and int(marker.get("types_revision") or 0) == _types_revision(stats_json)
        and marker.get("kernel_version") == KERNEL_VERSION
    )


# ---------------------------------------------------------------------------
# 切窗（确定性：同一张段落表永远切出同一组窗）
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_PARAGRAPHS = 60
DEFAULT_WINDOW_MAX_CHARS = 4000
DEFAULT_MIN_WINDOW_CHARS = 600
# 短尾窗并入前一窗时允许超出单窗上限的比例(否则丢弃短尾窗)
_TAIL_MERGE_SLACK = 1.25
# 位置标签
POSITION_OPENING = "opening"
POSITION_CLOSING = "closing"
POSITION_MIDDLE = "middle"
POSITION_WHOLE = "whole"


def cut_chapter_windows(
    chapter: list[dict[str, Any]],
    *,
    window_paragraphs: int = DEFAULT_WINDOW_PARAGRAPHS,
    window_max_chars: int = DEFAULT_WINDOW_MAX_CHARS,
    min_window_chars: int = DEFAULT_MIN_WINDOW_CHARS,
) -> list[list[dict[str, Any]]]:
    """一章正文 → 窗口:按段落顺序贪心切,字数或段数将超限时封窗,遇场界(``break_after``)封窗;
    短尾窗并入前一窗(放宽 25%)或丢弃;最后短于 ``min_window_chars`` 的窗口不要(章只有一窗时也一样——
    几百字的「章」多半是卷首语 / 内容简介 / 目录残片,不是作者的场景)。"""
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for row in chapter:
        if current and (
            current_chars + row["chars"] > window_max_chars or len(current) >= window_paragraphs
        ):
            windows.append(current)
            current = []
            current_chars = 0
        current.append(row)
        current_chars += row["chars"]
        if row.get("break_after"):
            # 场界:窗口不跨场(下一段另起一窗)
            windows.append(current)
            current = []
            current_chars = 0
    if current:
        windows.append(current)
    if len(windows) >= 2 and sum(r["chars"] for r in windows[-1]) < min_window_chars:
        tail = windows.pop()
        previous = windows[-1]
        merged_chars = sum(r["chars"] for r in previous) + sum(r["chars"] for r in tail)
        if merged_chars <= int(window_max_chars * _TAIL_MERGE_SLACK) and len(previous) + len(tail) <= int(
            window_paragraphs * _TAIL_MERGE_SLACK
        ):
            previous.extend(tail)
    return [win for win in windows if sum(r["chars"] for r in win) >= min_window_chars]


def window_position(position_index: int, count: int) -> str:
    """章内第 ``position_index`` 个窗口(共 ``count`` 个)的位置标签。"""
    if count == 1:
        return POSITION_WHOLE
    if position_index == 0:
        return POSITION_OPENING
    if position_index == count - 1:
        return POSITION_CLOSING
    return POSITION_MIDDLE


def book_windows(
    paragraphs: Iterable[Any],
    *,
    scene_breaks: Iterable[int] | None = None,
    window_paragraphs: int = DEFAULT_WINDOW_PARAGRAPHS,
    window_max_chars: int = DEFAULT_WINDOW_MAX_CHARS,
    min_window_chars: int = DEFAULT_MIN_WINDOW_CHARS,
) -> tuple[list[tuple[int, str, list[dict[str, Any]]]], int, int]:
    """整本书 → [(章号, 位置, 窗口正文行)] + (非空段数, 章数)。切章走 ``structure.split_book_chapters``。"""
    items = list(paragraphs)
    non_empty = sum(1 for item in items if str(_attr(item, "text") or "").strip())
    chapters, _markers = split_book_chapters(items, scene_breaks=scene_breaks)
    window_paragraphs = max(1, int(window_paragraphs))
    window_max_chars = max(200, int(window_max_chars))
    min_window_chars = max(0, int(min_window_chars))
    result: list[tuple[int, str, list[dict[str, Any]]]] = []
    for chapter in chapters:
        kept = cut_chapter_windows(
            chapter.rows,
            window_paragraphs=window_paragraphs,
            window_max_chars=window_max_chars,
            min_window_chars=min_window_chars,
        )
        for position_index, win in enumerate(kept):
            result.append((chapter.chapter_no, window_position(position_index, len(kept)), win))
    return result, non_empty, len(chapters)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _rows_for(session: Session, book_id: str, *, root: str | None = None) -> list[StyleReferenceWindow]:
    stmt = select(StyleReferenceWindow).where(
        StyleReferenceWindow.book_id == str(book_id),
        StyleReferenceWindow.index_version == WINDOW_INDEX_VERSION,
    )
    if root is not None:
        stmt = stmt.where(StyleReferenceWindow.root_sha256 == root)
    return list(session.scalars(stmt.order_by(StyleReferenceWindow.window_no)).all())


def load_windows(session: Session, book_id: str) -> list[StyleReferenceWindow]:
    """当前索引版本（与标记里的根哈希）下这本书的窗口行，按 window_no；不建索引。"""
    book = session.get(StyleReferenceBook, str(book_id))
    marker = index_marker(book.stats_json if book is not None else None)
    root = str(marker.get("root") or "") if marker else None
    return _rows_for(session, book_id, root=root or None)


def _paragraph_rows(session: Session, book_id: str, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
    stmt = select(
        StyleReferenceParagraph.paragraph_index,
        StyleReferenceParagraph.paragraph_type,
        StyleReferenceParagraph.text,
        StyleReferenceParagraph.paragraph_id,
    ).where(StyleReferenceParagraph.book_id == str(book_id))
    if start is not None:
        stmt = stmt.where(StyleReferenceParagraph.paragraph_index >= int(start))
    if end is not None:
        stmt = stmt.where(StyleReferenceParagraph.paragraph_index <= int(end))
    return [
        {"paragraph_index": index, "paragraph_type": ptype, "text": text, "paragraph_id": pid}
        for index, ptype, text, pid in session.execute(stmt.order_by(StyleReferenceParagraph.paragraph_index))
    ]


def _body_texts(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """窗口范围里的正文段（跳过章题 / 场分隔 / 脚注 / 落款——与切章器同一条判断）。"""
    texts: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if text and non_body_kind(text) is None:
            texts.append(text)
    return texts


def window_text(session: Session, window: StyleReferenceWindow | Mapping[str, Any]) -> str:
    """一窗的正文（段落以 ``\\n`` 相连，跳过章题 / 场分隔 / 脚注 / 落款）——``features_json`` 就是在这段文字上测的。"""
    book_id = _attr(window, "book_id")
    start = int(_attr(window, "start_index") or 0)
    end = int(_attr(window, "end_index") or start)
    return "\n".join(_body_texts(_paragraph_rows(session, str(book_id), start, end)))


# 批量取窗口正文时,相邻窗口之间隔着不到这么多段就并成一次查询(学习作业取全书 → 一次;起草选 12 窗 → 各查各的)
_MERGE_GAP_PARAGRAPHS = 200


def window_texts(session: Session, windows: Sequence[StyleReferenceWindow]) -> dict[int, str]:
    """一批窗口的正文（``{window_no: text}``；挨得近的窗口合并成一次范围查询）。"""
    result: dict[int, str] = {}
    by_book: dict[str, list[StyleReferenceWindow]] = {}
    for window in windows:
        by_book.setdefault(str(window.book_id), []).append(window)
    for book_id, items in by_book.items():
        items = sorted(items, key=lambda w: int(w.start_index))
        groups: list[list[StyleReferenceWindow]] = []
        for window in items:
            if groups and int(window.start_index) - max(int(w.end_index) for w in groups[-1]) <= _MERGE_GAP_PARAGRAPHS:
                groups[-1].append(window)
            else:
                groups.append([window])
        for group in groups:
            low = min(int(w.start_index) for w in group)
            high = max(int(w.end_index) for w in group)
            rows = _paragraph_rows(session, book_id, low, high)
            indices = [int(row["paragraph_index"]) for row in rows]
            for window in group:
                first = bisect_left(indices, int(window.start_index))
                last = bisect_right(indices, int(window.end_index))
                result[int(window.window_no)] = "\n".join(_body_texts(rows[first:last]))
    return result


def window_ref(window: StyleReferenceWindow) -> dict[str, Any]:
    """选窗 / 审计用的轻量引用（不带正文）。"""
    return {
        "window_no": int(window.window_no),
        "start": int(window.start_index),
        "end": int(window.end_index),
        "chapter": int(window.chapter_no or 0),
        "position": str(window.position or ""),
        "chars": int(window.chars or 0),
        "paragraphs": int(window.paragraph_count or 0),
        "dialogue_share": float(window.dialogue_share or 0.0),
        "typicality": float(window.typicality or 0.0),
    }


def _attr(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


# ---------------------------------------------------------------------------
# 建 / 刷新
# ---------------------------------------------------------------------------


def _type_mix(ptypes: Iterable[str]) -> dict[str, float]:
    counts = Counter(str(p or "narration") for p in ptypes)
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {ptype: round(count / total, 4) for ptype, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))}


def window_typicality(features_list: Sequence[Mapping[str, Any]]) -> list[float]:
    """每窗在这组窗口自己的分布里有多典型：−mean(min(|稳健 z|, 6))（越大越典型）。"""
    if not features_list:
        return []
    stats: list[tuple[str, float, float]] = []
    for name in FEATURE_NAMES:
        center, scale = robust_center_scale([float(f.get(name, 0.0) or 0.0) for f in features_list], name)
        stats.append((name, center, scale))
    result: list[float] = []
    for features in features_list:
        total = 0.0
        for name, center, scale in stats:
            z = (float(features.get(name, 0.0) or 0.0) - center) / scale
            total += min(abs(z), TYPICALITY_Z_CLIP)
        result.append(round(-total / len(stats), 4))
    return result


def _window_id(book_id: str, root: str, window_no: int) -> str:
    digest = hashlib.sha1(f"{book_id}\x1f{WINDOW_INDEX_VERSION}\x1f{root}".encode("utf-8")).hexdigest()[:12]
    return f"srwin_{digest}_{window_no:05d}"


def _build(
    session: Session,
    book: StyleReferenceBook,
    root: str,
    types_revision: int,
    previous: Sequence[StyleReferenceWindow],
) -> list[StyleReferenceWindow]:
    book_id = str(book.book_id)
    stats = dict(book.stats_json or {})
    paragraphs = _paragraph_rows(session, book_id)
    cut, _paragraph_count, _chapter_count = book_windows(paragraphs, scene_breaks=stats.get("scene_breaks"))
    measured: list[tuple[int, str, list[dict[str, Any]], dict[str, float]]] = []
    for chapter_no, position, rows in cut:
        text = "\n".join(str(row["text"]) for row in rows)
        measured.append((chapter_no, position, rows, kernel_features(measure_text(text))))
    typicality = window_typicality([features for *_rest, features in measured])
    # 同一段落表(根哈希不变)上窗口边界只取决于切窗规则:边界相同的窗口沿用原来的标签
    carried = {
        (int(w.start_index), int(w.end_index)): (w.tags_json, w.tags_version)
        for w in previous
        if w.root_sha256 == root and w.tags_json
    }
    session.flush()
    session.execute(delete(StyleReferenceWindow).where(StyleReferenceWindow.book_id == book_id))
    created: list[StyleReferenceWindow] = []
    now = utcnow()
    for window_no, ((chapter_no, position, rows, features), typical) in enumerate(
        zip(measured, typicality), start=1
    ):
        start, end = int(rows[0]["index"]), int(rows[-1]["index"])
        tags, tags_version = carried.get((start, end), (None, None))
        created.append(
            StyleReferenceWindow(
                window_id=_window_id(book_id, root, window_no),
                book_id=book_id,
                index_version=WINDOW_INDEX_VERSION,
                root_sha256=root,
                window_no=window_no,
                start_index=start,
                end_index=end,
                chapter_no=int(chapter_no),
                position=str(position),
                chars=sum(int(row["chars"]) for row in rows),
                paragraph_count=len(rows),
                type_mix_json=_type_mix(row["ptype"] for row in rows),
                dialogue_share=float(features.get("dialogue_char_share", 0.0)),
                typicality=float(typical),
                features_json=dict(features),
                tags_json=dict(tags) if isinstance(tags, Mapping) else None,
                tags_version=tags_version,
                created_at=now,
                updated_at=now,
            )
        )
    session.add_all(created)
    session.flush()
    patch_book_stats(session, book_id, {INDEX_MARKER_KEY: _marker(root, types_revision, len(created))})
    return created


def _refresh_types(
    session: Session, book_id: str, windows: Sequence[StyleReferenceWindow], root: str, types_revision: int
) -> list[StyleReferenceWindow]:
    """只有段落类型变了：就地重算段型构成，标签、特征、边界都不动。"""
    body = [
        (int(index), str(ptype or "narration"))
        for index, ptype, text in session.execute(
            select(
                StyleReferenceParagraph.paragraph_index,
                StyleReferenceParagraph.paragraph_type,
                StyleReferenceParagraph.text,
            )
            .where(StyleReferenceParagraph.book_id == str(book_id))
            .order_by(StyleReferenceParagraph.paragraph_index)
        )
        if str(text or "").strip() and non_body_kind(text) is None
    ]
    indices = [index for index, _ptype in body]
    now = utcnow()
    for window in windows:
        first = bisect_left(indices, int(window.start_index))
        last = bisect_right(indices, int(window.end_index))
        window.type_mix_json = _type_mix(ptype for _index, ptype in body[first:last])
        window.updated_at = now
    session.flush()
    patch_book_stats(session, book_id, {INDEX_MARKER_KEY: _marker(root, types_revision, len(windows))})
    return list(windows)


def ensure_window_index(session: Session, book_id: str, *, commit: bool = False) -> list[StyleReferenceWindow]:
    """这本书当前的窗口行（按 window_no）；标记与书的现状不符时就地刷新或重建。

    - 根哈希缺失时现算（只读 index / text 两列）并写回 ``stats_json``；
    - 标记一致 → 直接读行；只有类型版本变了 → 就地重算段型构成（保留标签）；其余情况 → 重建（根哈希没变时
      边界相同的窗口沿用标签）；
    - 只 flush 不 commit（调用方的事务）；``commit=True`` 时建完即提交，适合请求路径上的首次惰性建索引。
    书不存在 / 没有段落 → ``[]``。
    """
    book = session.get(StyleReferenceBook, str(book_id))
    if book is None:
        return []
    with _book_lock(str(book_id)):
        stats = dict(book.stats_json or {})
        stored = stored_paragraph_root(stats)
        if stored is None:
            root, count = compute_paragraph_root_fast(session, book_id)
            if not root:
                return []
            patch_book_stats(session, book_id, {ROOT_KEY: root, COUNT_KEY: count})
            stats = dict(session.get(StyleReferenceBook, str(book_id)).stats_json or {})
        else:
            root = stored[0]
        types_revision = _types_revision(stats)
        marker = index_marker(stats) or {}
        existing = _rows_for(session, book_id)
        same_root_rows = [w for w in existing if w.root_sha256 == root]
        # 行与标记对得上(一窗都切不出的小书:标记记着 0 窗,也算对得上,不必每次重切)
        recorded_empty = marker.get("window_count") == 0
        rows_match = len(same_root_rows) == len(existing) and (bool(same_root_rows) or recorded_empty)
        same_shape = (
            marker.get("version") == WINDOW_INDEX_VERSION
            and marker.get("root") == root
            and marker.get("kernel_version") == KERNEL_VERSION
            and rows_match
        )
        if same_shape and int(marker.get("types_revision") or 0) == types_revision:
            return same_root_rows
        if same_shape:
            result = _refresh_types(session, book_id, same_root_rows, root, types_revision)
        else:
            result = _build(session, book, root, types_revision, existing)
        if commit:
            session.commit()
        return result


def set_window_tags(
    session: Session,
    book_id: str,
    tags_by_window_no: Mapping[int, Mapping[str, Any]],
    *,
    tags_version: str,
) -> int:
    """写学习作业给窗口打的标签（经 ``tags.normalize_window_tags`` 规整成 v2 形状），返回写了几窗。

    只写当前索引版本 + 当前根哈希的行；不认识的窗号忽略。
    """
    windows = {int(w.window_no): w for w in load_windows(session, book_id)}
    now = utcnow()
    written = 0
    for raw_no, raw_tags in (tags_by_window_no or {}).items():
        try:
            window_no = int(raw_no)
        except (TypeError, ValueError):
            continue
        window = windows.get(window_no)
        if window is None:
            continue
        window.tags_json = normalize_window_tags(raw_tags)
        window.tags_version = str(tags_version)
        window.updated_at = now
        written += 1
    session.flush()
    return written


__all__ = [
    "DEFAULT_MIN_WINDOW_CHARS",
    "DEFAULT_WINDOW_MAX_CHARS",
    "DEFAULT_WINDOW_PARAGRAPHS",
    "INDEX_MARKER_KEY",
    "POSITION_CLOSING",
    "POSITION_MIDDLE",
    "POSITION_OPENING",
    "POSITION_WHOLE",
    "TYPES_REVISION_KEY",
    "WINDOW_INDEX_VERSION",
    "book_windows",
    "cut_chapter_windows",
    "ensure_window_index",
    "index_marker",
    "load_windows",
    "marker_is_current",
    "set_window_tags",
    "window_ref",
    "window_text",
    "window_texts",
    "window_position",
    "window_typicality",
]
