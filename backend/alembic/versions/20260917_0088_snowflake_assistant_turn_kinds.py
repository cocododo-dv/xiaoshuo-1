"""Add snowflake_assistant_turns.turn_kind / candidates_json / brief_delta_json / adoption_json.

Revision ID: 20260917_0088
Revises: 20260916_0087
Create Date: 2026-09-17

2026-09-17 阶段 U：教练与「候选」不再是两个互不知情的页签。「先看 3 个方向」成为教练日志里的一种回合
（turn_kind = candidates，方向存 candidates_json），教练下一轮就看得到作者看过哪些方向、选了哪个；
每轮对作者意图要点的差异随回合落表（brief_delta_json），日志自己会说话；一次生成采纳了哪一回合的
方向记在 adoption_json（{step_run_id, candidate_index, adopted_at}），界面据此打「已按此生成」。
历史行 turn_kind 回填 chat，其余三列可空、不回填。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260917_0088"
down_revision = "20260916_0087"
branch_labels = None
depends_on = None

TABLE = "snowflake_assistant_turns"
NULLABLE_JSON_COLUMNS = ("candidates_json", "brief_delta_json", "adoption_json")


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing_columns()
    if not existing:
        return
    with op.batch_alter_table(TABLE) as batch_op:
        if "turn_kind" not in existing:
            batch_op.add_column(sa.Column("turn_kind", sa.String(), nullable=False, server_default="chat"))
        for column in NULLABLE_JSON_COLUMNS:
            if column not in existing:
                batch_op.add_column(sa.Column(column, sa.JSON(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    if not existing:
        return
    with op.batch_alter_table(TABLE) as batch_op:
        for column in ("turn_kind", *NULLABLE_JSON_COLUMNS):
            if column in existing:
                batch_op.drop_column(column)
