"""Style reference v3: durable jobs, persisted window index, fidelity readings, frozen scene windows.

Revision ID: 20260923_0090
Revises: 20260920_0089
Create Date: 2026-09-23

2026-09-23 风格参考 v3（docs/style-reference-v3-2026-09-23.md）：
- ``style_reference_jobs``：分类 / 学习文风 / 对照检查统一进一张持久作业表，取代进程内登记簿、书上的
  JSON 游标与 run / report 行各自的心跳——重启或 ``--reload`` 之后由清扫线程接着跑，不再成孤儿。
- ``style_reference_windows``：全书样例窗口索引落表（不再塞进 profile_json、每次进程重算），带测量核特征
  （读数的参照分布）与模型打的场面 / 情绪 / 手法标签。
- ``style_fidelity_readings``：每份文字「像不像参考」的读数（确定性百分位 + 越界特征 + 参考评审按维打分）。
- ``style_reference_scene_windows``：每场冻结一次的选窗，同一场所有工序看同一组窗。
全部是新表，可降级。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260923_0090"
down_revision = "20260920_0089"
branch_labels = None
depends_on = None

JOBS = "style_reference_jobs"
WINDOWS = "style_reference_windows"
READINGS = "style_fidelity_readings"
SCENE_WINDOWS = "style_reference_scene_windows"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if JOBS not in tables:
        op.create_table(
            JOBS,
            sa.Column("job_id", sa.String(), primary_key=True),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column(
                "book_id",
                sa.String(),
                sa.ForeignKey("style_reference_books.book_id"),
                nullable=True,
            ),
            sa.Column("profile_id", sa.String(), nullable=True),
            sa.Column("op_key", sa.String(), nullable=True),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("phase", sa.String(), nullable=True),
            sa.Column("cancel_requested", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("owner_token", sa.String(), nullable=True),
            sa.Column("heartbeat_at", sa.String(), nullable=True),
            sa.Column("params_json", sa.JSON(), nullable=False),
            sa.Column("cursor_json", sa.JSON(), nullable=False),
            sa.Column("progress_json", sa.JSON(), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("started_at", sa.String(), nullable=True),
            sa.Column("finished_at", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_style_reference_jobs_book_kind_state", JOBS, ["book_id", "kind", "state"]
        )
        op.create_index(
            "ix_style_reference_jobs_state_heartbeat", JOBS, ["state", "heartbeat_at"]
        )
    if WINDOWS not in tables:
        op.create_table(
            WINDOWS,
            sa.Column("window_id", sa.String(), primary_key=True),
            sa.Column(
                "book_id",
                sa.String(),
                sa.ForeignKey("style_reference_books.book_id"),
                nullable=False,
            ),
            sa.Column("index_version", sa.String(), nullable=False),
            sa.Column("root_sha256", sa.String(), nullable=False),
            sa.Column("window_no", sa.Integer(), nullable=False),
            sa.Column("start_index", sa.Integer(), nullable=False),
            sa.Column("end_index", sa.Integer(), nullable=False),
            sa.Column("chapter_no", sa.Integer(), nullable=False),
            sa.Column("position", sa.String(), nullable=False),
            sa.Column("chars", sa.Integer(), nullable=False),
            sa.Column("paragraph_count", sa.Integer(), nullable=False),
            sa.Column("type_mix_json", sa.JSON(), nullable=False),
            sa.Column("dialogue_share", sa.Float(), nullable=False),
            sa.Column("typicality", sa.Float(), nullable=False),
            sa.Column("features_json", sa.JSON(), nullable=False),
            sa.Column("tags_json", sa.JSON(), nullable=True),
            sa.Column("tags_version", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.UniqueConstraint(
                "book_id",
                "index_version",
                "window_no",
                name="uq_style_reference_windows_book_version_no",
            ),
        )
        op.create_index(
            "ix_style_reference_windows_book_version_chapter",
            WINDOWS,
            ["book_id", "index_version", "chapter_no"],
        )
    if READINGS not in tables:
        op.create_table(
            READINGS,
            sa.Column("reading_id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), nullable=True),
            sa.Column("scene_id", sa.String(), nullable=True),
            sa.Column("profile_id", sa.String(), nullable=True),
            sa.Column("binding_id", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("stage", sa.String(), nullable=False),
            sa.Column("draft_ref", sa.String(), nullable=True),
            sa.Column("text_sha256", sa.String(), nullable=False),
            sa.Column("char_count", sa.Integer(), nullable=False),
            sa.Column("percentile", sa.Float(), nullable=True),
            sa.Column("distance", sa.Float(), nullable=True),
            sa.Column("reading_json", sa.JSON(), nullable=False),
            sa.Column("judge_json", sa.JSON(), nullable=True),
            sa.Column("copy_check_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
        )
        op.create_index(
            "ix_style_fidelity_readings_scene_created", READINGS, ["scene_id", "created_at"]
        )
        op.create_index(
            "ix_style_fidelity_readings_project_created", READINGS, ["project_id", "created_at"]
        )
        op.create_index(
            "ix_style_fidelity_readings_profile_created", READINGS, ["profile_id", "created_at"]
        )
    if SCENE_WINDOWS not in tables:
        op.create_table(
            SCENE_WINDOWS,
            sa.Column("selection_id", sa.String(), primary_key=True),
            sa.Column("selection_key", sa.String(), nullable=False),
            sa.Column("scene_id", sa.String(), nullable=True),
            sa.Column("bundle_id", sa.String(), nullable=True),
            sa.Column("contract_hash", sa.String(), nullable=True),
            sa.Column("window_refs_json", sa.JSON(), nullable=False),
            sa.Column("params_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.UniqueConstraint("selection_key", name="uq_style_reference_scene_windows_key"),
        )
        op.create_index(
            "ix_style_reference_scene_windows_scene", SCENE_WINDOWS, ["scene_id", "created_at"]
        )


def downgrade() -> None:
    tables = _tables()
    if SCENE_WINDOWS in tables:
        op.drop_index("ix_style_reference_scene_windows_scene", table_name=SCENE_WINDOWS)
        op.drop_table(SCENE_WINDOWS)
    if READINGS in tables:
        op.drop_index("ix_style_fidelity_readings_profile_created", table_name=READINGS)
        op.drop_index("ix_style_fidelity_readings_project_created", table_name=READINGS)
        op.drop_index("ix_style_fidelity_readings_scene_created", table_name=READINGS)
        op.drop_table(READINGS)
    if WINDOWS in tables:
        op.drop_index("ix_style_reference_windows_book_version_chapter", table_name=WINDOWS)
        op.drop_table(WINDOWS)
    if JOBS in tables:
        op.drop_index("ix_style_reference_jobs_state_heartbeat", table_name=JOBS)
        op.drop_index("ix_style_reference_jobs_book_kind_state", table_name=JOBS)
        op.drop_table(JOBS)
