"""删除参考书及其一切派生数据(2026-09-23 风格参考 v3,台账 L6:实库里 22 本测试写进去的「匿名参考」书)。

每本书:把它的活动作业收尾为 cancelled(旧工人的条件写落空),``cleanup.purge_derived_data`` 清掉
全部派生数据(抽取 run / 发现 / 引文 / 证据 / 画像 / 绑定 / 禁用词 / 回测报告 / 发现反馈 / 作业 /
窗口索引 / 相关待办行 / RAG 索引),再删段落行与书本身——与书库「删除」同一条路径,一本一个事务。

选书必须显式:``--book ID``(可重复)或 ``--id-prefix PREFIX``(可重复,至少 4 个字符,例如
``v2_book_``);两者可以同时给。默认干跑,只列出将删的书与各表行数(有生效绑定的书会特别标出);
``--execute`` 才写库。上线前先 ``db_backup`` 备份实库,且在服务停止时执行。

用法(backend 目录下):
    python -m novel_system.tools.purge_style_reference_books --id-prefix v2_book_            # 干跑
    python -m novel_system.tools.purge_style_reference_books --id-prefix v2_book_ --execute
    python -m novel_system.tools.purge_style_reference_books --book sr_book_xxx --execute
"""

from __future__ import annotations

import argparse
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    StyleReferenceWindow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.jobs import StyleJobService
from novel_system.services.style_reference.repository import StyleReferenceRepository

MIN_PREFIX_CHARS = 4


def select_books(
    session: Session,
    *,
    book_ids: list[str] | None = None,
    prefixes: list[str] | None = None,
) -> list[StyleReferenceBook]:
    """按显式 id 与 id 前缀选书(去重,按创建时间)。"""
    wanted: dict[str, StyleReferenceBook] = {}
    for book_id in book_ids or []:
        book = session.get(StyleReferenceBook, book_id)
        if book is not None:
            wanted[book.book_id] = book
    for prefix in prefixes or []:
        for book in session.scalars(
            select(StyleReferenceBook).where(StyleReferenceBook.book_id.startswith(prefix, autoescape=True))
        ):
            wanted[book.book_id] = book
    return sorted(wanted.values(), key=lambda book: (str(book.created_at or ""), book.book_id))


def _count(session: Session, model: Any, column: Any, value: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(column == value)) or 0)


def describe_book(session: Session, book: StyleReferenceBook) -> dict[str, Any]:
    profile_ids = list(
        session.scalars(select(StyleReferenceProfile.profile_id).where(StyleReferenceProfile.book_id == book.book_id))
    )
    active_bindings = 0
    if profile_ids:
        active_bindings = int(
            session.scalar(
                select(func.count())
                .select_from(StyleReferenceInjectionBinding)
                .where(
                    StyleReferenceInjectionBinding.profile_id.in_(profile_ids),
                    StyleReferenceInjectionBinding.status == "active",
                )
            )
            or 0
        )
    return {
        "book_id": book.book_id,
        "title": book.title,
        "status": book.status,
        "paragraphs": _count(session, StyleReferenceParagraph, StyleReferenceParagraph.book_id, book.book_id),
        "runs": _count(session, StyleReferenceRun, StyleReferenceRun.book_id, book.book_id),
        "profiles": len(profile_ids),
        "active_bindings": active_bindings,
        "jobs": _count(session, StyleReferenceJob, StyleReferenceJob.book_id, book.book_id),
        "windows": _count(session, StyleReferenceWindow, StyleReferenceWindow.book_id, book.book_id),
    }


def purge_book(session: Session, book_id: str) -> dict[str, int]:
    """删一本书及其全部派生数据(flush 不 commit;与 ``DELETE /books/{id}`` 同一顺序)。"""
    StyleJobService(session).cancel_all_for_book(book_id)
    counts = dict(purge_derived_data(session, book_id))
    repo = StyleReferenceRepository(session)
    counts["paragraphs"] = repo.delete_paragraphs_for_book(book_id)
    counts["books"] = repo.delete_book(book_id)
    session.flush()
    return counts


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", action="append", default=None, help="删除这本书(可重复)")
    parser.add_argument(
        "--id-prefix", action="append", default=None, help="删除 book_id 以此开头的书(可重复,≥4 个字符)"
    )
    parser.add_argument("--execute", action="store_true", help="真正删除(默认只干跑)")
    args = parser.parse_args(argv)
    if not args.book and not args.id_prefix:
        parser.error("必须用 --book 或 --id-prefix 显式选书")
    for prefix in args.id_prefix or []:
        if len(prefix.strip()) < MIN_PREFIX_CHARS:
            parser.error(f"--id-prefix 至少 {MIN_PREFIX_CHARS} 个字符:{prefix!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with SessionLocal() as session:
        books = select_books(
            session,
            book_ids=list(dict.fromkeys(args.book or [])),
            prefixes=[prefix.strip() for prefix in (args.id_prefix or [])],
        )
        if not books:
            print("没有匹配的参考书。")
            return 0
        for book in books:
            info = describe_book(session, book)
            warn = f"  ⚠ 有 {info['active_bindings']} 条生效绑定" if info["active_bindings"] else ""
            print(
                f"{info['book_id']}  《{info['title']}》  状态 {info['status']}  段落 {info['paragraphs']}  "
                f"抽取 {info['runs']}  画像 {info['profiles']}  作业 {info['jobs']}  窗口 {info['windows']}{warn}"
            )
        if not args.execute:
            print(f"\n干跑：将删除 {len(books)} 本书；加 --execute 执行。")
            return 0
        deleted = 0
        for book in books:
            purge_book(session, book.book_id)
            session.commit()
            deleted += 1
        print(f"\n已删除 {deleted} 本书及其派生数据。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
