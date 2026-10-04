"""Drop three dead tables: the v1 planner's artifacts, snowflake revision links, author preference profiles.

Revision ID: 20261004_0099
Revises: 20260929_0098
Create Date: 2026-10-04

2026-10-04 第二轮清理的表结构批次（G3 ART + 它的附注 2）。三张表都没有读者，也早已没有写入者：

- ``snowflake_artifacts``：旧版 v1 雪花规划器的步骤版本表；规划器与它的路由 2026-09-30 退役（R9），之后没有任何代码
  读写它。
- ``snowflake_revision_links``：雪花失效级联以前每个受影响的步骤 / 场景计划各写一行，B06-14 起改成一条操作日志，
  这张表从来没有读者。
- ``author_preference_profiles``：写作偏好学习退役（批准 #6，R5）后留下的偏好档案表，R5 把删表留给之后的表结构批次。

三张表都只是 ``story_projects`` 的子表（或没有外键），没有别的表指向它们；SQLite 删表时连同它们的索引一起删。
哪张表还有行，先把行原样写进库文件旁边的 ``<库文件>.0099-<表名>.json``（``{"revision", "table", "rows"}``）再删——
不让 ``alembic upgrade head`` 卡住启动脚本；库不是文件（内存库）又有行时才拒绝升级，而且在删任何一张表之前就拒绝。
这些行可能带作者的构思与偏好文字：导出文件与库文件一样不入仓库（``.gitignore`` 的 ``*.0099-*.json``）。

降级按 0098 时的形状重建三张表的**结构**（列、默认值、主键、外键、CHECK、外键查找索引），好让更早的降级（0098 加回
``resolved_at``、0081 删查找索引、0072 删 CHECK）照常找到它们；删掉的行不会回来。
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "20261004_0099"
down_revision = "20260929_0098"
branch_labels = None
depends_on = None

DEAD_TABLES = ("snowflake_artifacts", "snowflake_revision_links", "author_preference_profiles")
EXPORT_INFIX = ".0099-"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _leftover_rows(bind, tables: set[str]) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    for table in DEAD_TABLES:
        if table not in tables:
            continue
        found = [dict(row) for row in bind.execute(sa.text(f'SELECT * FROM "{table}"')).mappings()]
        if found:
            rows[table] = found
    return rows


def _export_leftover_rows(bind, rows: dict[str, list[dict]]) -> None:
    """表里还有行：每张表原样写进库文件旁边的一个 JSON，再删表。"""
    database = bind.engine.url.database
    if not database or database == ":memory:":
        counts = "、".join(f"{table} {len(found)} 行" for table, found in rows.items())
        raise RuntimeError(
            f"迁移 0099 要删三张没有读写者的旧表，但库里还有行（{counts}），而这个库不是文件、没处导出。"
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
    # 形状照 0026 + 0043（consumed_input_sigs_json 排在最后）+ 0081（project_id 查找索引）
    if "snowflake_artifacts" not in tables:
        op.create_table(
            "snowflake_artifacts",
            sa.Column("artifact_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("step_key", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(), nullable=False, server_default="pending_review"),
            sa.Column("artifact_json", sa.JSON(), nullable=True),
            sa.Column("input_refs_json", sa.JSON(), nullable=True),
            sa.Column("diagnosis_json", sa.JSON(), nullable=True),
            sa.Column("llm_call_id", sa.String(), nullable=True),
            sa.Column("approved_at", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("consumed_input_sigs_json", sa.JSON(), nullable=True),
            sa.PrimaryKeyConstraint("artifact_id"),
            sa.ForeignKeyConstraint(["project_id"], ["story_projects.project_id"]),
        )
        op.create_index("ix_snowflake_artifacts_project_id", "snowflake_artifacts", ["project_id"])
    # 形状照 0030 减去 0098 删掉的 resolved_at（0098 的降级再把它加回来）+ 0081 的查找索引
    if "snowflake_revision_links" not in tables:
        op.create_table(
            "snowflake_revision_links",
            sa.Column("revision_link_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("source_step_key", sa.String(), nullable=False),
            sa.Column("source_step_run_id", sa.String(), nullable=True),
            sa.Column("affected_kind", sa.String(), nullable=False),
            sa.Column("affected_id", sa.String(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="open"),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("revision_link_id"),
            sa.ForeignKeyConstraint(["project_id"], ["story_projects.project_id"]),
        )
        op.create_index("ix_snowflake_revision_links_project_id", "snowflake_revision_links", ["project_id"])
    # 形状照 0014 + 0072（三条具名 CHECK）；没有外键，也没有主键以外的索引
    if "author_preference_profiles" not in tables:
        op.create_table(
            "author_preference_profiles",
            sa.Column("profile_id", sa.String(), nullable=False),
            sa.Column("scope_type", sa.String(), nullable=False),
            sa.Column("scope_ref_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("runtime_eligible", sa.Integer(), nullable=False),
            sa.Column("summary_json", sa.JSON(), nullable=False),
            sa.Column("source_patch_ids_json", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("profile_id"),
            sa.CheckConstraint(
                "status IN ('draft','approved','rejected','superseded')",
                name="ck_author_preference_profiles_status",
            ),
            sa.CheckConstraint(
                "scope_type IN ('global','genre','project','chapter')",
                name="ck_author_preference_profiles_scope_type",
            ),
            sa.CheckConstraint("runtime_eligible IN (0,1)", name="ck_author_preference_profiles_runtime_eligible"),
        )
