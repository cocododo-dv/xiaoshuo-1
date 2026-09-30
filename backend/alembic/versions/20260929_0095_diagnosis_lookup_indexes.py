"""Index the per-scene lookups of the diagnosis / deep review / author draft paths.

Revision ID: 20260929_0095
Revises: 20260924_0092
Create Date: 2026-09-29

2026-09-29 重构（审计 B05-02 / X01-13）：场景诊断每读一场都要查「这一场最新的评审」「这一轮深评的各镜头行」
「这一场的局部改写候选」「这一场的当前作者稿」，这四张表却没有一条二级索引——每次都是整表扫描加临时排序
（``EXPLAIN QUERY PLAN``：``SCAN writer_evaluations`` + ``USE TEMP B-TREE FOR ORDER BY``）。评审行随每次起草
（准定稿一行）与每次深评（1 + 5 行）只增不减。实测 100 场 / 2 万评审行：全书诊断计数 2,571 ms → 572 ms。

只加索引、不动数据：可逆，对现有库安全（已存在同名索引时跳过，重复升级无害）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260929_0095"
down_revision = "20260924_0092"
branch_labels = None
depends_on = None

# (索引名, 表, 列)：与 db/models.py 里各模型 __table_args__ 声明的逐一相同（元数据隔离守卫比对具名索引）
INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_writer_evaluations_object", "writer_evaluations", ("object_type", "object_id", "rubric_id", "created_at")),
    ("ix_writer_evaluations_parent", "writer_evaluations", ("parent_evaluation_id",)),
    ("ix_passage_patch_candidates_object", "passage_patch_candidates", ("object_type", "object_id", "created_at")),
    ("ix_author_drafts_object", "author_drafts", ("object_type", "object_id", "status", "updated_at")),
)


def _existing_indexes(inspector: sa.Inspector, table: str) -> set[str] | None:
    if table not in set(inspector.get_table_names()):
        return None
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, columns in INDEXES:
        existing = _existing_indexes(inspector, table)
        if existing is None or name in existing:
            continue
        op.create_index(name, table, list(columns))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, _columns in reversed(INDEXES):
        existing = _existing_indexes(inspector, table)
        if existing is None or name not in existing:
            continue
        op.drop_index(name, table_name=table)
