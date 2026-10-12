"""Story memory: chapter digests, thread marks, editorial reviews; drop volume_summaries.

Revision ID: 20261012_0100
Revises: 20261004_0099
Create Date: 2026-10-12

2026-10-12 故事记忆。起草后面各章时，模型以前从远处只看得到「卷汇总」：每 5 章把各章的章汇总（各场终稿全文）原样
拼成一份，标着「仅供语气，严禁当作事实」，每场提示词只带最新那一卷——几万字原文挤爆输入预算，第 11 章起前 5 章
整体消失，而后面各章真正要的事实（发生过什么、谁知道了什么、还没收的线）它按设计不许带。

- 新表 ``chapter_digests``：一章写完之后由 LLM 写的事实摘要（``services/story_memory.py``），一章同时一条
  ``active``，旧的 ``superseded``；``source_hash`` 绑住生成时的整章正文与上一章那份摘要。
- 新表 ``story_thread_marks``：作者对线索台账里一条线的标记（放下 / 已收）；台账本身由各章摘要推出来，不另存。
- 新表 ``editorial_reviews`` / ``editorial_guidance``：每写完几章一次的编辑通读（问题清单）与它给后面几章的意见；
  作者采纳的意见进那几章的起草与场景蓝图。意见随通读删除（``ON DELETE CASCADE``）。
- 删表 ``volume_summaries``：它是章汇总的派生缓存，没有作者写的文字，删掉的行可以从终稿重新得到，所以不导出；
  卷汇总的生成（``Aggregator.maybe_aggregate_volume``）与提示词槽位同一批删掉。

降级删四张新表、按 0059 的形状重建 ``volume_summaries`` 的**结构**；删掉的行不会回来。
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
    if "story_thread_marks" not in tables:
        op.create_table(
            "story_thread_marks",
            sa.Column("row_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("thread_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("label_snapshot", sa.String(), nullable=False),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("row_id"),
            sa.CheckConstraint("status IN ('dropped','resolved')", name="ck_story_thread_marks_status"),
            sa.UniqueConstraint("project_id", "thread_id", name="uq_story_thread_marks_thread"),
        )
    if "editorial_reviews" not in tables:
        op.create_table(
            "editorial_reviews",
            sa.Column("review_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("through_chapter_id", sa.String(), nullable=False),
            sa.Column("window_chapter_ids_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("origin", sa.String(), nullable=False),
            sa.Column("source_hash", sa.String(), nullable=False),
            sa.Column("review_json", sa.JSON(), nullable=False),
            sa.Column("llm_call_id", sa.String(), nullable=True),
            sa.Column("prompt_version", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("review_id"),
            sa.CheckConstraint("status IN ('active','superseded')", name="ck_editorial_reviews_status"),
            sa.CheckConstraint("origin IN ('auto','manual')", name="ck_editorial_reviews_origin"),
        )
        op.create_index("ix_editorial_reviews_project_status", "editorial_reviews", ["project_id", "status"])
    if "editorial_guidance" not in tables:
        op.create_table(
            "editorial_guidance",
            sa.Column("guidance_id", sa.String(), nullable=False),
            sa.Column("review_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("chapter_id", sa.String(), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("author_text", sa.Text(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("decided_at", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("guidance_id"),
            sa.ForeignKeyConstraint(["review_id"], ["editorial_reviews.review_id"], ondelete="CASCADE"),
            sa.CheckConstraint(
                "status IN ('proposed','accepted','dismissed','superseded')", name="ck_editorial_guidance_status"
            ),
        )
        op.create_index("ix_editorial_guidance_review", "editorial_guidance", ["review_id"])
        op.create_index("ix_editorial_guidance_project_status", "editorial_guidance", ["project_id", "status"])
    if "volume_summaries" in tables:
        op.drop_table("volume_summaries")


def downgrade() -> None:
    tables = _tables()
    if "editorial_guidance" in tables:
        op.drop_index("ix_editorial_guidance_project_status", table_name="editorial_guidance")
        op.drop_index("ix_editorial_guidance_review", table_name="editorial_guidance")
        op.drop_table("editorial_guidance")
    if "editorial_reviews" in tables:
        op.drop_index("ix_editorial_reviews_project_status", table_name="editorial_reviews")
        op.drop_table("editorial_reviews")
    if "story_thread_marks" in tables:
        op.drop_table("story_thread_marks")
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
