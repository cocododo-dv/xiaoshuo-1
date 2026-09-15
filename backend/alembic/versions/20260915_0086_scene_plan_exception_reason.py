"""Add snowflake_scene_plans.exception_reason (the author's stated reason for breaking a scene rule).

Revision ID: 20260915_0086
Revises: 20260914_0085
Create Date: 2026-09-15

2026-09-15 雪花评估第三轮 · 阶段 N：Ingermanson 自己的样例里有「冲突：无」的收尾课和没有三拍的
叙述收尾场，规矩是「不过关也可以放行，但我要知道理由」。这一列存作者写下的破例理由；有理由时
规则层不再把缺三拍 / 缺坩埚记成缺失，结构简报把它带给起草与 QC。可空，历史行不回填。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260915_0086"
down_revision = "20260914_0085"
branch_labels = None
depends_on = None

TABLE = "snowflake_scene_plans"
COLUMN = "exception_reason"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN in _existing_columns():
        return
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    if COLUMN not in _existing_columns():
        return
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.drop_column(COLUMN)
