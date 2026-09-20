"""Add snowflake_chapter_plans.catalog_chapter_id and pin the chapters that are already materialized.

Revision ID: 20260920_0089
Revises: 20260917_0088
Create Date: 2026-09-20

2026-09-20 阶段 Y：目录里的章 id 不再跟着章序走。过去物化目标是位置式的 ``{project}_CH{seq:02d}``——
重新分章（拆一章、并一章、换一个每章场数）之后，第 3 章可能已经是另一组场，可目录里那一行
（章状态、字数目标、戏剧卡、运行任务、终审）还留在「第 3 个位置」上，从拆点往后的每一张场景卡都要换章；
而 11 张场景运行时表（草稿 / QC / 定稿 / 正史…）上冗余的 chapter_id 不会跟着走。

现在章计划行自己钉住它在目录里的 id（``catalog_chapter_id``）：一经铸出就不再改，拆章只多一行新章，
其余的章原地不动。已有作品在这里一次钉好——一章的全部场景计划都指着同一个、确实存在于目录里的章，
且这个章只被这一章认领时才钉；对不上的（旧数据里交错洗过的归属）留空，下一次保存分章时另铸新号。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260920_0089"
down_revision = "20260917_0088"
branch_labels = None
depends_on = None

TABLE = "snowflake_chapter_plans"
COLUMN = "catalog_chapter_id"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns() -> set[str]:
    if TABLE not in _tables():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing_columns()
    if not existing:
        return
    if COLUMN not in existing:
        with op.batch_alter_table(TABLE) as batch_op:
            batch_op.add_column(sa.Column(COLUMN, sa.String(), nullable=True))
    if not {"snowflake_scene_plans", "chapter_goals"} <= _tables():
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT cp.chapter_plan_id, cp.project_id, sp.chapter_id
            FROM snowflake_chapter_plans cp
            JOIN snowflake_scene_plans sp
              ON sp.chapter_plan_id = cp.chapter_plan_id AND sp.removed_at IS NULL
            WHERE cp.removed_at IS NULL AND cp.catalog_chapter_id IS NULL
            """
        )
    ).fetchall()
    stamped: dict[str, tuple[str, set[str]]] = {}
    for chapter_plan_id, project_id, chapter_id in rows:
        entry = stamped.setdefault(chapter_plan_id, (project_id, set()))
        entry[1].add(str(chapter_id or "").strip())
    candidates: dict[str, tuple[str, str]] = {}
    for chapter_plan_id, (project_id, chapter_ids) in stamped.items():
        if len(chapter_ids) == 1 and "" not in chapter_ids:
            candidates[chapter_plan_id] = (project_id, next(iter(chapter_ids)))
    claims: dict[tuple[str, str], int] = {}
    for project_id, chapter_id in candidates.values():
        claims[(project_id, chapter_id)] = claims.get((project_id, chapter_id), 0) + 1
    for chapter_plan_id, (project_id, chapter_id) in candidates.items():
        if claims[(project_id, chapter_id)] != 1:
            continue
        exists = bind.execute(
            sa.text("SELECT 1 FROM chapter_goals WHERE chapter_id = :chapter_id AND project_id = :project_id"),
            {"chapter_id": chapter_id, "project_id": project_id},
        ).first()
        if exists is None:
            continue
        bind.execute(
            sa.text("UPDATE snowflake_chapter_plans SET catalog_chapter_id = :chapter_id WHERE chapter_plan_id = :plan_id"),
            {"chapter_id": chapter_id, "plan_id": chapter_plan_id},
        )


def downgrade() -> None:
    if COLUMN not in _existing_columns():
        return
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.drop_column(COLUMN)
