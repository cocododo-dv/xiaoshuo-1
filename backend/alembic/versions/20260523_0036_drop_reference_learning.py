"""drop legacy reference_learning tables (style_reference v1.1, PR-1)

Revision ID: 20260523_0036
Revises: 20260515_0035
Create Date: 2026-05-23

PR-1 决策(plans/style-reference-v1-1-fancy-shannon.md §"已敲定决策"):
- 旧 reference_learning 表里还有数据时,upgrade() 先断言 backups/style_reference_legacy_*.json
  至少存在一个,不存在则 raise。2026-09-29(B12-14)起只在旧表真有行时才要:新库和旧表本来就空的库
  没有可丢的东西,不再需要仓库里放一个占位备份文件。
- 三路并清 review_items 残留行:review_reffind_% / review_apply_% / 兜底 source orphan。
- 反向 drop 旧 6 张表。
- downgrade() schema-only 重建空旧表(列定义照搬 20260419_0011)。
"""

from __future__ import annotations

import os
import pathlib

import sqlalchemy as sa
from alembic import op

revision = "20260523_0036"
down_revision = "20260515_0035"
branch_labels = None
depends_on = None


_LEGACY_BACKUP_GLOB = "style_reference_legacy_*.json"

_LEGACY_TABLES_DROP_ORDER = [
    "reference_profiles",
    "reference_findings",
    "reference_learning_rounds",
    "reference_learning_runs",
    "reference_book_segments",
    "reference_books",
]


def _repo_root() -> pathlib.Path:
    # 测试场景下允许通过 env 覆盖根目录(避免触碰真实 backups/)
    override = os.environ.get("STYLE_REFERENCE_REPO_ROOT")
    if override:
        return pathlib.Path(override)
    # backend/alembic/versions/<this>.py → parents[3] = codex repo root
    return pathlib.Path(__file__).resolve().parents[3]


def _legacy_tables_with_rows(bind: sa.engine.Connection, existing_tables: set[str]) -> list[str]:
    return [
        table_name
        for table_name in _LEGACY_TABLES_DROP_ORDER
        if table_name in existing_tables
        and bind.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first() is not None
    ]


def _assert_backup_present(tables_with_rows: list[str]) -> None:
    backups_dir = _repo_root() / "backups"
    matches = list(backups_dir.glob(_LEGACY_BACKUP_GLOB)) if backups_dir.exists() else []
    if not matches:
        raise RuntimeError(
            "[style_reference PR-1] 旧 reference_learning 表里还有数据("
            + "、".join(tables_with_rows)
            + f"),drop 之前必须存在 backups/{_LEGACY_BACKUP_GLOB}。未在 {backups_dir!s} 找到任何匹配。"
            " 请先停服务、用 `python -m novel_system.tools.db_backup --backup <库> <备份>` 备份整库,"
            f"在 backups/ 下放一个 {_LEGACY_BACKUP_GLOB} 记下备份位置,再 alembic upgrade。"
        )


def _purge_review_items(bind: sa.engine.Connection) -> None:
    # 三路并清(plans §"ReviewItem 清理依据"):
    for prefix in ("review_reffind_", "review_apply_"):
        bind.execute(
            sa.text("DELETE FROM review_items WHERE review_id LIKE :pattern"),
            {"pattern": f"{prefix}%"},
        )
    # 兜底:source orphan(且不在前两路前缀里)
    bind.execute(
        sa.text(
            "DELETE FROM review_items WHERE "
            "json_extract(candidate_payload_json, '$.source') IN "
            "('reference_book_learning', 'reference_profile_apply') "
            "AND review_id NOT LIKE 'review_reffind_%' "
            "AND review_id NOT LIKE 'review_apply_%'"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 只有旧表里真有数据才要求先备份:新库(或旧表本来就空的库)没有可丢的东西
    tables_with_rows = _legacy_tables_with_rows(bind, existing_tables)
    if tables_with_rows:
        _assert_backup_present(tables_with_rows)

    if "review_items" in existing_tables:
        _purge_review_items(bind)

    for table_name in _LEGACY_TABLES_DROP_ORDER:
        if table_name in existing_tables:
            op.drop_table(table_name)


def downgrade() -> None:
    """schema-only 重建空旧表(列定义照搬 20260419_0011_reference_learning.py)。

    用于:0037 单独 downgrade 后,继续 downgrade 0036 时确保旧表 schema 概念可重建,
    便于工程师在 0035 之后重新尝试。重建后的表为空。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "reference_books" not in tables:
        op.create_table(
            "reference_books",
            sa.Column("book_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("author_label", sa.String(), nullable=True),
            sa.Column("source_kind", sa.String(), nullable=False),
            sa.Column("source_path", sa.Text(), nullable=True),
            sa.Column("file_name", sa.String(), nullable=True),
            sa.Column("cloud_policy", sa.String(), nullable=False),
            sa.Column("analysis_focus", sa.String(), nullable=False),
            sa.Column("text_checksum", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("total_chars", sa.Integer(), nullable=False),
            sa.Column("total_segments", sa.Integer(), nullable=False),
            sa.Column("stats_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("book_id"),
        )

    if "reference_book_segments" not in tables:
        op.create_table(
            "reference_book_segments",
            sa.Column("segment_id", sa.String(), nullable=False),
            sa.Column("book_id", sa.String(), nullable=False),
            sa.Column("segment_index", sa.Integer(), nullable=False),
            sa.Column("chapter_hint", sa.String(), nullable=True),
            sa.Column("segment_kind", sa.String(), nullable=False),
            sa.Column("start_offset", sa.Integer(), nullable=False),
            sa.Column("end_offset", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("selected_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["book_id"], ["reference_books.book_id"]),
            sa.PrimaryKeyConstraint("segment_id"),
        )

    if "reference_learning_runs" not in tables:
        op.create_table(
            "reference_learning_runs",
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("book_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("batch_size", sa.Integer(), nullable=False),
            sa.Column("coverage_json", sa.JSON(), nullable=False),
            sa.Column("round_count", sa.Integer(), nullable=False),
            sa.Column("profile_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["book_id"], ["reference_books.book_id"]),
            sa.PrimaryKeyConstraint("run_id"),
        )

    if "reference_learning_rounds" not in tables:
        op.create_table(
            "reference_learning_rounds",
            sa.Column("round_id", sa.String(), nullable=False),
            sa.Column("book_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("round_index", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("segment_ids_json", sa.JSON(), nullable=False),
            sa.Column("finding_ids_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["book_id"], ["reference_books.book_id"]),
            sa.ForeignKeyConstraint(["run_id"], ["reference_learning_runs.run_id"]),
            sa.PrimaryKeyConstraint("round_id"),
        )

    if "reference_findings" not in tables:
        op.create_table(
            "reference_findings",
            sa.Column("finding_id", sa.String(), nullable=False),
            sa.Column("book_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("round_id", sa.String(), nullable=False),
            sa.Column("segment_id", sa.String(), nullable=False),
            sa.Column("review_id", sa.String(), nullable=False),
            sa.Column("finding_type", sa.String(), nullable=False),
            sa.Column("dimension", sa.String(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("evidence_preview", sa.Text(), nullable=False),
            sa.Column("candidate_payload_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["book_id"], ["reference_books.book_id"]),
            sa.ForeignKeyConstraint(["run_id"], ["reference_learning_runs.run_id"]),
            sa.ForeignKeyConstraint(["round_id"], ["reference_learning_rounds.round_id"]),
            sa.ForeignKeyConstraint(["segment_id"], ["reference_book_segments.segment_id"]),
            sa.PrimaryKeyConstraint("finding_id"),
        )

    if "reference_profiles" not in tables:
        op.create_table(
            "reference_profiles",
            sa.Column("profile_id", sa.String(), nullable=False),
            sa.Column("book_id", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("profile_json", sa.JSON(), nullable=False),
            sa.Column("coverage_json", sa.JSON(), nullable=False),
            sa.Column("source_finding_ids_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["book_id"], ["reference_books.book_id"]),
            sa.ForeignKeyConstraint(["run_id"], ["reference_learning_runs.run_id"]),
            sa.PrimaryKeyConstraint("profile_id"),
        )
