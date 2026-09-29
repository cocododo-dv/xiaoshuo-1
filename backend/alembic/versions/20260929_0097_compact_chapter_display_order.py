"""Compact chapter display_order once, so the catalog read no longer writes.

Revision ID: 20260929_0097
Revises: 20260924_0092
Create Date: 2026-09-29

目录读取（``GET …/catalog``、主页、待办、章节规划都要读）过去在读的时候顺手把每部作品活跃章的
``display_order`` 压实成 1..n——一次读请求里发 UPDATE，SQLite 在读的时候拿写锁，还可能在另一次写入
进行到一半时撞上 ``(project_id, display_order)`` 唯一索引（B08-14）。现在所有改动章集合的写入口自己
压实章序，读取不再写；这里把库里已有的漂移按**完全相同的规则**一次补齐：

- 每部作品的活跃章按（没有章序的排最后，章序，chapter_id）排好，要改的行改成它的位置；
- 要改的行里有已终审的章（``state = 'approved'`` 或在作品的 ``approved_chapter_ids_json`` 里）就整部作品不动
  ——终审章的位置锁着，历史漂移只能走「重新打开」修；
- 两阶段写入：先把要改的行停到「现有最大章序 + 1000000」起的高位，再写最终值，不撞唯一索引。

只改数据，可重复执行（第二次没有要改的行）。降级是空操作：旧代码读目录时会得出同样的章序。
历史迁移是冻结的显式 SQL，不导入应用 ORM。
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260929_0097"
down_revision = "20260924_0092"
branch_labels = None
depends_on = None

PARK_GAP = 1_000_000


def _approved_ids(raw: Any) -> set[str]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return set()
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}


def _compact_project(bind: sa.engine.Connection, project_id: str, approved_ids: set[str]) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT chapter_id, display_order, state FROM chapter_goals "
            "WHERE project_id = :project_id AND trashed_flag = 0"
        ),
        {"project_id": project_id},
    ).fetchall()
    rows = sorted(rows, key=lambda row: (row.display_order is None, row.display_order or 0, row.chapter_id))
    pending = [(row, index) for index, row in enumerate(rows, start=1) if row.display_order != index]
    if not pending:
        return
    if any(
        str(row.state or "").strip() == "approved" or row.chapter_id in approved_ids for row, _index in pending
    ):
        return
    update = sa.text("UPDATE chapter_goals SET display_order = :display_order WHERE chapter_id = :chapter_id")
    slot = max(int(row.display_order or 0) for row in rows) + PARK_GAP
    for row, _index in pending:
        bind.execute(update, {"display_order": slot, "chapter_id": row.chapter_id})
        slot += 1
    for row, index in pending:
        bind.execute(update, {"display_order": index, "chapter_id": row.chapter_id})


def upgrade() -> None:
    bind = op.get_bind()
    project_ids = [
        row.project_id
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT project_id FROM chapter_goals "
                "WHERE trashed_flag = 0 AND project_id IS NOT NULL ORDER BY project_id"
            )
        )
    ]
    for project_id in project_ids:
        approved_raw = bind.execute(
            sa.text("SELECT approved_chapter_ids_json FROM story_projects WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar()
        _compact_project(bind, project_id, _approved_ids(approved_raw))


def downgrade() -> None:
    # 只压实了章序（旧代码读目录时会得出同样的值），没有可还原的结构
    pass
