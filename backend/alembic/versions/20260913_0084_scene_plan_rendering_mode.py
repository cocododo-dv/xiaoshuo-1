"""Add snowflake_scene_plans.rendering_mode (reactive scene rendering: full / summary).

Revision ID: 20260913_0084
Revises: 20260904_0083
Create Date: 2026-09-13

2026-09-13 雪花评估 · 阶段 C：Ingermanson 说反应场可以整场写、缩成两段概述、或干脆略过。
第 10 步给反应场一个呈现方式（full / summary），物化时 summary 场拿到 200–500 字的数值篇幅带，
起草模板按概述写。主动场恒为 full。历史行全部回填 full。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260913_0084"
down_revision = "20260904_0083"
branch_labels = None
depends_on = None

TABLE = "snowflake_scene_plans"
COLUMN = "rendering_mode"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in set(inspector.get_table_names()):
        return False
    return COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.String(), nullable=False, server_default="full"))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.drop_column(COLUMN)
