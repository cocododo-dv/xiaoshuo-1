"""Add snowflake_direction_briefs (the author's per-step intent brief distilled from the coach).

Revision ID: 20260916_0087
Revises: 20260915_0086
Create Date: 2026-09-16

2026-09-16 阶段 T：驻场教练与 AI 生成不再各是各的。每一轮教练对话在同一次调用里重述作者对本步
的意图（决定 / 否决 / 约束 / 待定，本步 / 全书），落成这张表里作者可编辑的「本步要点」；
整步生成、三候选、分诊把活动条目（加上游各步的全书级条目）作为受保护的提示键读入。
一步一行，(project_id, step_key) 唯一。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260916_0087"
down_revision = "20260915_0086"
branch_labels = None
depends_on = None

TABLE = "snowflake_direction_briefs"
INDEX = "ix_snowflake_direction_briefs_step"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(
        TABLE,
        sa.Column("brief_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("story_projects.project_id"), nullable=False),
        sa.Column("step_key", sa.String(), nullable=False),
        sa.Column("lines_json", sa.JSON(), nullable=True),
        sa.Column("inherit_upstream", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_turn_ids_json", sa.JSON(), nullable=True),
        sa.Column("author_edited_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(INDEX, TABLE, ["project_id", "step_key"], unique=True)


def downgrade() -> None:
    if TABLE not in _tables():
        return
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
