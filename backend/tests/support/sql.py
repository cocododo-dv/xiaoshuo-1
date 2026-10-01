"""数一段代码发了多少条 SQL（查询预算类用例：作品列表、工作台载荷不随章 / 场数线性增长）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from novel_system.db.session import engine


@contextmanager
def count_statements() -> Iterator[dict[str, int]]:
    """``with count_statements() as counter:`` 块里每条发到库的语句给 ``counter["n"]`` 加一。"""
    counter = {"n": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        counter["n"] += 1

    target = engine()
    event.listen(target, "before_cursor_execute", _count)
    try:
        yield counter
    finally:
        event.remove(target, "before_cursor_execute", _count)
