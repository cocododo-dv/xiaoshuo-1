"""Style reference v3 cleanup: drop the retired validation reports, finding feedback and base_confidence.

Revision ID: 20260923_0091
Revises: 20260923_0090
Create Date: 2026-09-23

2026-09-23 风格参考 v3 收尾（docs/style-reference-v3-2026-09-23.md §2.2 清理迁移）——写这些行的功能已随代码删除：
- ``style_reference_validation_reports``：旧「回测」（量化 / 语义 / 抄袭三路校验）的报告行。「对照检查」改成作业表上的
  check 作业，结果写进 ``style_fidelity_readings``（读数 + 参考评审 + 抄袭门）；
- ``style_reference_finding_feedback``：发现的 👍/👎 票（单作者永远到不了调档阈值），并入文风卡行的 ✓ / ✗；
- ``style_reference_findings.base_confidence``：👍/👎 调档用的合成基线置信度，随反馈表一起没了用处。

降级按它们最后的形状（0037 + 0071 的回测报告表、0058 的反馈表与列）重建**结构**；删掉的行不会回来。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260923_0091"
down_revision = "20260923_0090"
branch_labels = None
depends_on = None

FEEDBACK = "style_reference_finding_feedback"
REPORTS = "style_reference_validation_reports"
FINDINGS = "style_reference_findings"
BASE_CONFIDENCE = "base_confidence"


def _inspector():
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set[str]:
    inspector = _inspector()
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    tables = _tables()
    # 反馈表指向发现表：先删它（迁移连接上外键检查是关的，次序只是为了清楚）
    if FEEDBACK in tables:
        op.drop_table(FEEDBACK)
    if REPORTS in tables:
        op.drop_table(REPORTS)
    if BASE_CONFIDENCE in _columns(FINDINGS):
        # SQLite 删列走整表重建（索引与唯一约束按反射原样重建）
        with op.batch_alter_table(FINDINGS) as batch_op:
            batch_op.drop_column(BASE_CONFIDENCE)


def downgrade() -> None:
    if FINDINGS in _tables() and BASE_CONFIDENCE not in _columns(FINDINGS):
        op.add_column(FINDINGS, sa.Column(BASE_CONFIDENCE, sa.String(), nullable=True))
    tables = _tables()
    if REPORTS not in tables:
        op.create_table(
            REPORTS,
            sa.Column("report_id", sa.String(), nullable=False),
            sa.Column("profile_id", sa.String(), nullable=False),
            sa.Column("target_kind", sa.String(), nullable=False),
            sa.Column("target_ref_id", sa.String(), nullable=True),
            sa.Column("verdict", sa.String(), nullable=False),
            sa.Column("quantitative_json", sa.JSON(), nullable=False),
            sa.Column("semantic_json", sa.JSON(), nullable=False),
            sa.Column("plagiarism_json", sa.JSON(), nullable=False),
            sa.Column("forbidden_hits_json", sa.JSON(), nullable=False),
            sa.Column("mode_executed", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="completed"),
            sa.Column("error_code", sa.String(), nullable=True),
            sa.Column("error_text", sa.Text(), nullable=True),
            sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("started_at", sa.String(), nullable=True),
            sa.Column("heartbeat_at", sa.String(), nullable=True),
            sa.Column("finished_at", sa.String(), nullable=True),
            sa.ForeignKeyConstraint(["profile_id"], ["style_reference_profiles.profile_id"]),
            sa.PrimaryKeyConstraint("report_id"),
        )
        op.create_index(
            "ix_style_reference_validation_reports_profile_target", REPORTS, ["profile_id", "target_ref_id"]
        )
        op.create_index("ix_style_reference_validation_reports_verdict", REPORTS, ["verdict"])
        op.create_index("ix_style_reference_validation_reports_status", REPORTS, ["status"])
    if FEEDBACK not in tables:
        op.create_table(
            FEEDBACK,
            sa.Column("feedback_id", sa.String(), nullable=False),
            sa.Column("finding_id", sa.String(), nullable=False),
            sa.Column("operator_ref", sa.String(), nullable=False),
            sa.Column("vote", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["finding_id"], ["style_reference_findings.finding_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("feedback_id"),
            sa.UniqueConstraint(
                "finding_id",
                "operator_ref",
                name="uq_style_reference_finding_feedback_finding_operator",
            ),
        )
        op.create_index("ix_sr_finding_feedback_finding", FEEDBACK, ["finding_id"])
