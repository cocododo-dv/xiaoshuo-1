"""Drop the retired 待办 inbox tables: review_items and review_derived_snoozes.

Revision ID: 20261021_0102
Revises: 20261021_0101
Create Date: 2026-10-21

待办收件箱 2026-10-21 退役（页面、``/api/v1/review-items`` 与卡片服务一起删掉，提醒改由前端从目录现算，进主页的
「等你处理」）。两张表从此没有读写者：

- ``review_items``：收件箱的持久卡（``item_type = 'fe_card'``）与更早的审阅候选行（风格观察、写作偏好……，生产者早已
  退役）；只剩重置工具、场景搬家与删除检查的清单还认得它。
- ``review_derived_snoozes``：实时派生卡的「稍后」记录。

两张表都没有外键指向别处，也没有别的表指向它们；SQLite 删表时连同索引一起删。哪张表还有行，先把行原样写进库文件
旁边的 ``<库文件>.0102-<表名>.json``（``{"revision", "table", "rows"}``）再删——不让 ``alembic upgrade head`` 卡住
启动脚本；库不是文件（内存库）又有行时才拒绝升级，而且在删任何一张表之前就拒绝。生成列 ``target_collection`` 只是
``item_type`` 的派生，不导出。这些行可能带作者的文字：导出文件与库文件一样不入仓库（``.gitignore``）。

降级按 0101 时的形状重建两张表的**结构**（列、生成列、主键、CHECK、三个索引）；删掉的行不会回来。
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "20261021_0102"
down_revision = "20261021_0101"
branch_labels = None
depends_on = None

DEAD_TABLES = ("review_items", "review_derived_snoozes")
EXPORT_INFIX = ".0102-"
GENERATED_COLUMNS = {"review_items": ("target_collection",)}

# 0101 时 review_items.target_collection 的生成式（模型里的 Computed，原样冻结在这里）
TARGET_COLLECTION_SQL = (
    "CASE "
    "WHEN item_type = 'style_observation' THEN 'style_observations' "
    "WHEN item_type = 'style_rule_set' THEN 'style_rules' "
    "WHEN item_type = 'banned_rule_cluster' THEN 'banned_rule_clusters' "
    "WHEN item_type = 'narrative_pattern' THEN 'narrative_patterns' "
    "WHEN item_type = 'voice_card_candidate' THEN 'voice_cards' "
    "WHEN item_type = 'relation_card_candidate' THEN 'relation_cards' "
    "WHEN item_type = 'world_rule' THEN 'world_rules' "
    "WHEN item_type = 'calibration_candidate' THEN 'calibration_lines' "
    "WHEN item_type = 'foreshadow_open' THEN 'foreshadow_tracker' "
    "WHEN item_type = 'foreshadow_touch' THEN 'foreshadow_tracker' "
    "WHEN item_type = 'foreshadow_resolve' THEN 'foreshadow_tracker' "
    "WHEN item_type = 'scene_memory' THEN 'scene_memories' "
    "WHEN item_type = 'scene_summary' THEN 'scene_memories' "
    "WHEN item_type = 'chapter_summary' THEN 'chapter_memories' "
    "WHEN item_type = 'author_preference_profile' THEN 'author_preference_profiles' "
    "WHEN item_type = 'longform_structure_guidance' THEN 'longform_structure_guidance' "
    "ELSE 'review_items' END"
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _leftover_rows(bind, tables: set[str]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    for table in DEAD_TABLES:
        if table not in tables:
            continue
        found = [dict(row) for row in bind.execute(sa.text(f'SELECT * FROM "{table}"')).mappings()]
        for row in found:
            for column in GENERATED_COLUMNS.get(table, ()):
                row.pop(column, None)
        if found:
            rows[table] = found
    return rows


def _export_leftover_rows(bind, rows: dict[str, list[dict]]) -> None:
    """表里还有行：每张表原样写进库文件旁边的一个 JSON，再删表。"""
    database = bind.engine.url.database
    if not database or database == ":memory:":
        counts = "、".join(f"{table} {len(found)} 行" for table, found in rows.items())
        raise RuntimeError(
            f"迁移 0102 要删待办收件箱的两张旧表，但库里还有行（{counts}），而这个库不是文件、没处导出。"
            "先把这些行导出或删掉，再升级。"
        )
    for table, found in rows.items():
        target = Path(database).with_name(Path(database).name + f"{EXPORT_INFIX}{table}.json")
        target.write_text(
            json.dumps({"revision": revision, "table": table, "rows": found}, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    # 先导出全部（内存库有行时在这里就拒绝，一张表都还没删），再删表
    rows = _leftover_rows(bind, tables)
    if rows:
        _export_leftover_rows(bind, rows)
    for table in DEAD_TABLES:
        if table in tables:
            op.drop_table(table)


def downgrade() -> None:
    tables = _tables()
    # 形状照 0101（0050 的去重键、0060 的两个热路径索引、0098 删掉三个死列之后）
    if "review_items" not in tables:
        op.create_table(
            "review_items",
            sa.Column("review_id", sa.String(), nullable=False),
            sa.Column("scene_id", sa.String(), nullable=True),
            sa.Column("chapter_id", sa.String(), nullable=True),
            sa.Column("item_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("candidate_text", sa.Text(), nullable=False),
            sa.Column("candidate_payload_json", sa.JSON(), nullable=False),
            sa.Column("active_on_approve", sa.Integer(), nullable=False),
            sa.Column("materialize_status", sa.String(), nullable=False),
            sa.Column("approved_item_row_id", sa.String(), nullable=True),
            sa.Column("approved_item_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=True),
            sa.Column("kind", sa.String(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=True),
            sa.Column("provenance_json", sa.JSON(), nullable=True),
            sa.Column("card_json", sa.JSON(), nullable=True),
            sa.Column("actions_json", sa.JSON(), nullable=True),
            sa.Column("state", sa.String(), nullable=True),
            sa.Column("resolved_action_index", sa.Integer(), nullable=True),
            sa.Column("dedupe_key", sa.String(), nullable=True),
            sa.Column("target_collection", sa.String(), sa.Computed(TARGET_COLLECTION_SQL, persisted=True)),
            sa.PrimaryKeyConstraint("review_id"),
            sa.CheckConstraint("status IN ('pending','approved','rejected')", name="ck_review_items_status"),
        )
        op.create_index("ix_review_items_project_state", "review_items", ["project_id", "state"])
        op.create_index("ix_review_items_scene", "review_items", ["scene_id"])
        op.create_index("ux_review_items_project_dedupe", "review_items", ["project_id", "dedupe_key"], unique=True)
    if "review_derived_snoozes" not in tables:
        op.create_table(
            "review_derived_snoozes",
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("project_id", "fingerprint"),
        )
