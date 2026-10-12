"""Story memory: add chapter_digests, drop volume_summaries.

Revision ID: 20261012_0100
Revises: 20261004_0099
Create Date: 2026-10-12

2026-10-12 故事记忆。起草后面各章时，模型以前从远处只看得到「卷汇总」：每 5 章把各章的章汇总（各场终稿全文）原样
拼成一份，标着「仅供语气，严禁当作事实」，每场提示词只带最新那一卷——几万字原文挤爆输入预算，第 11 章起前 5 章
整体消失，而后面各章真正要的事实（发生过什么、谁知道了什么、还没收的线）它按设计不许带。

- 新表 ``chapter_digests``：一章写完之后由 LLM 写的事实摘要（``services/story_memory.py``），一章同时一条
  ``active``，旧的 ``superseded``；``source_hash`` 绑住生成时的整章正文与上一章那份摘要。
- 删表 ``volume_summaries``：它是章汇总的派生缓存，没有作者写的文字，删掉的行可以从终稿重新得到，所以不导出；
  卷汇总的生成（``Aggregator.maybe_aggregate_volume``）与提示词槽位同一批删掉。

降级删 ``chapter_digests``、按 0059 的形状重建 ``volume_summaries`` 的**结构**；删掉的行不会回来。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261012_0100"
down_revision = "20261004_0099"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "chapter_digests" not in tables:
        op.create_table(
            "chapter_digests",
            sa.Column("row_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("chapter_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("origin", sa.String(), nullable=False),
            sa.Column("body_hash", sa.String(), nullable=False),
            sa.Column("previous_digest_row_id", sa.String(), nullable=True),
            sa.Column("previous_digest_hash", sa.String(), nullable=True),
            sa.Column("source_hash", sa.String(), nullable=False),
            sa.Column("digest_json", sa.JSON(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=False),
            sa.Column("llm_call_id", sa.String(), nullable=True),
            sa.Column("prompt_version", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("row_id"),
            sa.CheckConstraint("status IN ('active','superseded')", name="ck_chapter_digests_status"),
            sa.CheckConstraint("origin IN ('llm','author')", name="ck_chapter_digests_origin"),
        )
        op.create_index("ix_chapter_digests_chapter_status", "chapter_digests", ["chapter_id", "status"])
        op.create_index("ix_chapter_digests_project", "chapter_digests", ["project_id"])
    if "volume_summaries" in tables:
        op.drop_table("volume_summaries")


def downgrade() -> None:
    tables = _tables()
    if "chapter_digests" in tables:
        op.drop_index("ix_chapter_digests_project", table_name="chapter_digests")
        op.drop_index("ix_chapter_digests_chapter_status", table_name="chapter_digests")
        op.drop_table("chapter_digests")
    if "volume_summaries" not in tables:
        # 形状照 0059
        op.create_table(
            "volume_summaries",
            sa.Column("row_id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("volume_seq", sa.Integer(), nullable=False),
            sa.Column("chapter_id_start", sa.String(), nullable=True),
            sa.Column("chapter_id_end", sa.String(), nullable=True),
            sa.Column("chapter_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("atmosphere_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("factual_digest", sa.Text(), nullable=True),
            sa.Column("active_flag", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("runtime_eligible", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "runtime_eligibility_basis",
                sa.String(),
                nullable=False,
                server_default="direct_read",
            ),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
        )
