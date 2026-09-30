"""Drop the dead voice / relation card tables and the dead columns (refactor schema batch).

Revision ID: 20260929_0098
Revises: 20260929_0097
Create Date: 2026-09-29

2026-09-29 全系统重构的表结构批次（B12-10 + 重评 R8 + A1 + 重评 R2）。删掉的都没有写入者也没有读者，实库里都是空的
或只有默认值：

- ``voice_profiles`` / ``relation_profiles``：声线卡 / 关系卡（批准 #15，重评 R8）。产品里没有任何地方能写它们；
  唯一的来源是 2026-09-20 以前的预检补建的占位卡。万一库里还有行，先把它们原样写进库文件旁边的
  ``<库文件>.0098-voice-relation-cards.json`` 再删表（不让 ``alembic upgrade head`` 卡住启动脚本）；
  库不是文件（内存库）又有行时才拒绝升级。
- ``review_items.retry_count`` / ``max_retry`` / ``snooze_until``、``review_derived_snoozes.snooze_until``、
  ``snowflake_revision_links.resolved_at``、``snowflake_scene_plans.involved_foreshadowing_json`` /
  ``downstream_obligations_json``（规划器读的是字典键，不是这两列）、``style_reference_evidences.is_synthetic``、
  ``scene_bundles.execution_mode``（恒写 ``"P2"``）：B12-10。
- ``chapter_states.aggregate_block_reason`` / ``chapter_backfill_pending_count`` / ``manual_hold_reason``：章汇总读时
  现拼（A1）之后只剩默认值的写入。
- ``scene_run_states.candidate_dispersion_score``：候选离散度随先中性后润色的多稿一起退役（重评 R2），不再写也不再读。

这些列都不在索引、CHECK 或外键里；SQLite 删列走整表重建（索引与约束按反射原样重建），涉及的表都很小。
``review_items`` 有一个生成列（``target_collection``）：整表重建会把它当普通列往新表里复制，SQLite 拒绝往生成列里写，
所以同一个批里先删掉它、再按库里反射出来的表达式原样加回（0003 / 0016 同一个做法）。

SQLite 上 DDL 不在事务里：先做可能失败的整表重建，最后才导出并删两张卡片表——重建失败时卡片表原样还在。
降级按原来的形状重建**结构**（非空列带服务端默认值，好在有行的表上加回来）；删掉的值不会回来。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "20260929_0098"
down_revision = "20260929_0097"
branch_labels = None
depends_on = None

CARD_TABLES = ("voice_profiles", "relation_profiles")
CARD_EXPORT_SUFFIX = ".0098-voice-relation-cards.json"

# 表 → 要删的列（升级按这个次序；降级按原来的类型与默认值加回来）
DEAD_COLUMNS: dict[str, tuple[str, ...]] = {
    "review_items": ("retry_count", "max_retry", "snooze_until"),
    "review_derived_snoozes": ("snooze_until",),
    "snowflake_revision_links": ("resolved_at",),
    "snowflake_scene_plans": ("involved_foreshadowing_json", "downstream_obligations_json"),
    "style_reference_evidences": ("is_synthetic",),
    "scene_bundles": ("execution_mode",),
    "chapter_states": ("aggregate_block_reason", "chapter_backfill_pending_count", "manual_hold_reason"),
    "scene_run_states": ("candidate_dispersion_score",),
}


def _restored_column(table: str, column: str) -> sa.Column:
    """降级时加回来的列：类型与原迁移一致；原来非空的带服务端默认值（有行的表上才加得回来）。"""
    specs: dict[tuple[str, str], sa.Column] = {
        ("review_items", "retry_count"): sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        ("review_items", "max_retry"): sa.Column("max_retry", sa.Integer(), nullable=False, server_default="3"),
        ("review_items", "snooze_until"): sa.Column("snooze_until", sa.String(), nullable=True),
        ("review_derived_snoozes", "snooze_until"): sa.Column("snooze_until", sa.String(), nullable=True),
        ("snowflake_revision_links", "resolved_at"): sa.Column("resolved_at", sa.String(), nullable=True),
        ("snowflake_scene_plans", "involved_foreshadowing_json"): sa.Column(
            "involved_foreshadowing_json", sa.JSON(), nullable=True, server_default="[]"
        ),
        ("snowflake_scene_plans", "downstream_obligations_json"): sa.Column(
            "downstream_obligations_json", sa.JSON(), nullable=True, server_default="[]"
        ),
        ("style_reference_evidences", "is_synthetic"): sa.Column(
            "is_synthetic", sa.Integer(), nullable=False, server_default="0"
        ),
        ("scene_bundles", "execution_mode"): sa.Column(
            "execution_mode", sa.String(), nullable=False, server_default="P2"
        ),
        ("chapter_states", "aggregate_block_reason"): sa.Column(
            "aggregate_block_reason", sa.String(), nullable=False, server_default="none"
        ),
        ("chapter_states", "chapter_backfill_pending_count"): sa.Column(
            "chapter_backfill_pending_count", sa.Integer(), nullable=False, server_default="0"
        ),
        ("chapter_states", "manual_hold_reason"): sa.Column("manual_hold_reason", sa.Text(), nullable=True),
        ("scene_run_states", "candidate_dispersion_score"): sa.Column(
            "candidate_dispersion_score", sa.Float(), nullable=True
        ),
    }
    return specs[(table, column)]


def _inspector():
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set[str]:
    inspector = _inspector()
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _generated_columns(table: str) -> list[sa.Column]:
    """表里的生成列，按库里反射出来的表达式重新写成列定义（整表重建时要先删后加）。"""
    columns: list[sa.Column] = []
    for column in _inspector().get_columns(table):
        computed = column.get("computed")
        if computed:
            columns.append(
                sa.Column(
                    column["name"],
                    column["type"],
                    sa.Computed(computed["sqltext"], persisted=computed.get("persisted")),
                    nullable=column.get("nullable", True),
                )
            )
    return columns


def _rebuild(table: str, *, drop: Sequence[str] = (), add: Sequence[sa.Column] = ()) -> None:
    """删 / 加列的整表重建；生成列在同一个批里先删后按原表达式加回，不让复制往生成列里写。"""
    generated = _generated_columns(table)
    with op.batch_alter_table(table, recreate="always") as batch_op:
        for column in drop:
            batch_op.drop_column(column)
        for column in add:
            batch_op.add_column(column)
        for column in generated:
            batch_op.drop_column(column.name)
            batch_op.add_column(column)


def _card_rows(bind, tables: set[str]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    for table in CARD_TABLES:
        if table not in tables:
            continue
        found = [dict(row) for row in bind.execute(sa.text(f'SELECT * FROM "{table}"')).mappings()]
        if found:
            rows[table] = found
    return rows


def _export_card_rows(bind, rows: dict[str, list[dict]]) -> None:
    """库里还有卡片行（只可能是旧预检补建的占位卡）：原样写进库文件旁边的 JSON，再删表。"""
    database = bind.engine.url.database
    if not database or database == ":memory:":
        counts = "、".join(f"{table} {len(found)} 行" for table, found in rows.items())
        raise RuntimeError(
            f"迁移 0098 要删声线卡 / 关系卡两张表，但库里还有行（{counts}），而这个库不是文件、没处导出。"
            "先把这些行导出或删掉，再升级。"
        )
    target = Path(database).with_name(Path(database).name + CARD_EXPORT_SUFFIX)
    target.write_text(
        json.dumps({"revision": revision, "tables": rows}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )


def upgrade() -> None:
    for table, columns in DEAD_COLUMNS.items():
        present = [column for column in columns if column in _columns(table)]
        if present:
            _rebuild(table, drop=present)

    bind = op.get_bind()
    tables = _tables()
    rows = _card_rows(bind, tables)
    if rows:
        _export_card_rows(bind, rows)
    for table in CARD_TABLES:
        if table in tables:
            op.drop_table(table)


def downgrade() -> None:
    for table, columns in DEAD_COLUMNS.items():
        if table not in _tables():
            continue
        missing = [column for column in columns if column not in _columns(table)]
        if missing:
            _rebuild(table, add=[_restored_column(table, column) for column in missing])

    tables = _tables()
    # 形状照 0002 + 0003 之后的两张卡片表（0003 用整表重建加的列排在后面）
    if "voice_profiles" not in tables:
        op.create_table(
            "voice_profiles",
            sa.Column("row_id", sa.String(), nullable=False),
            sa.Column("voice_profile_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("character_id", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("active_flag", sa.Integer(), nullable=False),
            sa.Column("source_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("runtime_eligible", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("runtime_eligibility_basis", sa.String(), nullable=False, server_default="stage_blocked"),
            sa.Column("effective_at", sa.String(), nullable=True),
            sa.Column("source_review_id", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("row_id"),
        )
    if "relation_profiles" not in tables:
        op.create_table(
            "relation_profiles",
            sa.Column("row_id", sa.String(), nullable=False),
            sa.Column("relation_profile_id", sa.String(), nullable=False),
            sa.Column("left_character_id", sa.String(), nullable=False),
            sa.Column("right_character_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("active_flag", sa.Integer(), nullable=False),
            sa.Column("source_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("runtime_eligible", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("runtime_eligibility_basis", sa.String(), nullable=False, server_default="stage_blocked"),
            sa.Column("effective_at", sa.String(), nullable=True),
            sa.Column("source_review_id", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("row_id"),
        )
