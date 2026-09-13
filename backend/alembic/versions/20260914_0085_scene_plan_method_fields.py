"""Add snowflake_scene_plans.expected_reader_emotion / story_time (the method's own scene columns).

Revision ID: 20260914_0085
Revises: 20260913_0084
Create Date: 2026-09-14

2026-09-14 雪花评估 · 阶段 J：Ingermanson 的场景表有时间戳一栏，场景规划要写下「这一场想让读者
经历什么」（分诊第 5 步），并列出在场人物。在场人物已有 onstage_chars_json；这里补另外两列，
都可空，历史行不回填。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260914_0085"
down_revision = "20260913_0084"
branch_labels = None
depends_on = None

TABLE = "snowflake_scene_plans"
COLUMNS = (
    ("expected_reader_emotion", sa.Text()),
    ("story_time", sa.String()),
)


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing_columns()
    missing = [(name, kind) for name, kind in COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch_op:
        for name, kind in missing:
            batch_op.add_column(sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    present = [name for name, _kind in COLUMNS if name in existing]
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch_op:
        for name in present:
            batch_op.drop_column(name)
