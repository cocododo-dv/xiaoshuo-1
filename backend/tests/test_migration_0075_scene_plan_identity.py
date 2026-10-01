"""迁移 20260725_0075 的修复路径守卫。

新库跑迁移链时 ``0001_init_schema`` 的 ``Base.metadata.create_all`` 已经按当前 ORM
建好了新列和唯一索引，所以 0075 里真正的数据修复逻辑在新库上是空转的 —— 只有
历史库才会走到。这里手工造一个 0074 形态、且带着撞号数据的库，验证：

1. 已物化的行（``scene_cards`` 里有对应主键）保住原 ``scene_id``，不动任何既有关联；
2. 撞号组里其余行重铸为 row_uid 基的新 id；
3. 从未物化的行一律迁到新方案；
4. ``row_uid`` 为空的历史行先补铸锚点；
5. 唯一索引最终建得上（这是「撞号从静默丢场变硬错误」的结构保证）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from tests.support.migrations import alembic_config

_LEGACY_SCENE_PLANS = """
CREATE TABLE snowflake_scene_plans (
    scene_plan_id VARCHAR NOT NULL PRIMARY KEY,
    project_id VARCHAR NOT NULL,
    row_uid VARCHAR,
    scene_id VARCHAR NOT NULL,
    chapter_id VARCHAR NOT NULL,
    scene_seq INTEGER NOT NULL DEFAULT 1,
    title VARCHAR,
    summary TEXT,
    status VARCHAR NOT NULL DEFAULT 'draft',
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
)
"""

_LEGACY_SCENE_CARDS = """
CREATE TABLE scene_cards (
    scene_id VARCHAR NOT NULL PRIMARY KEY,
    project_id VARCHAR,
    chapter_id VARCHAR NOT NULL,
    scene_seq INTEGER NOT NULL,
    scene_goal TEXT
)
"""


def _insert_plan(conn, *, pk: str, project: str, row_uid: str | None, scene_id: str, seq: int, summary: str, created: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO snowflake_scene_plans "
            "(scene_plan_id, project_id, row_uid, scene_id, chapter_id, scene_seq, title, summary, status, created_at, updated_at) "
            "VALUES (:pk, :project, :row_uid, :scene_id, :chapter, :seq, :summary, :summary, 'draft', :created, :created)"
        ),
        {
            "pk": pk,
            "project": project,
            "row_uid": row_uid,
            "scene_id": scene_id,
            "chapter": f"{project}_CH01",
            "seq": seq,
            "summary": summary,
            "created": created,
        },
    )


def test_migration_repairs_duplicate_scene_ids_on_a_legacy_database(tmp_path, monkeypatch) -> None:
    from novel_system.db.session import reset_engine

    legacy_db = tmp_path / "legacy.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{legacy_db}")
    reset_engine()

    engine = sa.create_engine(f"sqlite:///{legacy_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_LEGACY_SCENE_PLANS))
            conn.execute(sa.text(_LEGACY_SCENE_CARDS))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('20260722_0074')"))

            # P1：撞号组 —— 已物化的 SC04 与后加的新场共用同一个 scene_id
            _insert_plan(conn, pk="sp_a1", project="PRJ_A", row_uid="S04", scene_id="PRJ_A_CH01_SC04", seq=3, summary="事件4", created="2026-07-01T00:00:00Z")
            _insert_plan(conn, pk="sp_a2", project="PRJ_A", row_uid="S05", scene_id="PRJ_A_CH01_SC04", seq=4, summary="新加的一场", created="2026-07-02T00:00:00Z")
            # 同项目里一条正常的、也已物化的行
            _insert_plan(conn, pk="sp_a0", project="PRJ_A", row_uid="S01", scene_id="PRJ_A_CH01_SC01", seq=1, summary="事件1", created="2026-06-30T00:00:00Z")
            # P2：从未物化的项目，且有一行缺 row_uid
            _insert_plan(conn, pk="sp_b1", project="PRJ_B", row_uid=None, scene_id="PRJ_B_CH01_SC01", seq=1, summary="B 第一场", created="2026-07-03T00:00:00Z")
            _insert_plan(conn, pk="sp_b2", project="PRJ_B", row_uid="S02", scene_id="PRJ_B_CH01_SC02", seq=2, summary="B 第二场", created="2026-07-04T00:00:00Z")

            for scene_id in ("PRJ_A_CH01_SC01", "PRJ_A_CH01_SC04"):
                conn.execute(
                    sa.text(
                        "INSERT INTO scene_cards (scene_id, project_id, chapter_id, scene_seq, scene_goal) "
                        "VALUES (:scene_id, 'PRJ_A', 'PRJ_A_CH01', 1, 'already written')"
                    ),
                    {"scene_id": scene_id},
                )

        cfg = alembic_config()
        command.upgrade(cfg, "20260725_0075")

        with engine.begin() as conn:
            rows = {
                pk: (project, row_uid, scene_id)
                for pk, project, row_uid, scene_id in conn.execute(
                    sa.text("SELECT scene_plan_id, project_id, row_uid, scene_id FROM snowflake_scene_plans")
                )
            }

            # 1. 已物化的行原封不动 —— 它们的 scene_cards 关联不能移位
            assert rows["sp_a0"][2] == "PRJ_A_CH01_SC01"
            assert rows["sp_a1"][2] == "PRJ_A_CH01_SC04"
            # 2. 撞号组里未物化的那条重铸为 row_uid 基
            assert rows["sp_a2"][2] == "PRJ_A_SC_S05"
            # 3. 从未物化的项目整体迁到新方案
            assert rows["sp_b2"][2] == "PRJ_B_SC_S02"
            # 4. 缺 row_uid 的历史行补铸了锚点，scene_id 随之派生
            b1_row_uid = rows["sp_b1"][1]
            assert b1_row_uid and b1_row_uid.startswith("row_")
            assert rows["sp_b1"][2] == f"PRJ_B_SC_{b1_row_uid}"

            # 5. 唯一索引建上了，且确实拦得住新的撞号
            index_names = {row[1] for row in conn.execute(sa.text("PRAGMA index_list('snowflake_scene_plans')"))}
            assert "ix_snowflake_scene_plans_scene_id" in index_names

            all_scene_ids = list(
                conn.execute(sa.text("SELECT project_id, scene_id FROM snowflake_scene_plans"))
            )
            assert len(all_scene_ids) == len(set(all_scene_ids))

            # 新列可用
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM snowflake_scene_plans WHERE removed_at IS NULL AND orphaned_flag = 0")
            ).scalar() == 5

        with engine.begin() as conn:
            try:
                _insert_plan(conn, pk="sp_dup", project="PRJ_B", row_uid="S99", scene_id="PRJ_B_SC_S02", seq=9, summary="撞号", created="2026-07-05T00:00:00Z")
            except sa.exc.IntegrityError:
                pass
            else:
                raise AssertionError("唯一索引没有拦住重复的 (project_id, scene_id)")
    finally:
        engine.dispose()
        reset_engine()


def test_the_keeper_follows_the_materialized_card_not_merely_the_oldest_row(tmp_path, monkeypatch) -> None:
    """撞号组里保住 scene_id 的必须是场景卡真正对应的那一行。

    原实现想按「这一行是不是已经物化了」挑，但探测语句写在推导式里、每一行绑的都是
    **组**的 scene_id/project_id —— 同一组的每一行绑定值完全相同，所以它要么对全组都
    返回真、要么全假，keeper 恒等于 rows[0]（最老的那行），N 次查询白跑。真正能判别的
    信号是场景卡自己带的位置 (chapter_id, scene_seq)。

    这里把卡的位置对齐**后加**的那一行：正确实现必须让它保住 scene_id，把最老的那行重铸。
    """
    from novel_system.db.session import reset_engine

    legacy_db = tmp_path / "legacy-keeper.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{legacy_db}")
    reset_engine()

    engine = sa.create_engine(f"sqlite:///{legacy_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_LEGACY_SCENE_PLANS))
            conn.execute(sa.text(_LEGACY_SCENE_CARDS))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('20260722_0074')"))

            # 最老的一行在第 7 位；后加的一行在第 2 位，而场景卡就写在第 2 位
            _insert_plan(conn, pk="sp_old", project="PRJ_K", row_uid="R_OLD", scene_id="PRJ_K_CH01_SC02",
                         seq=7, summary="老行", created="2026-07-01T00:00:00Z")
            _insert_plan(conn, pk="sp_card", project="PRJ_K", row_uid="R_CARD", scene_id="PRJ_K_CH01_SC02",
                         seq=2, summary="卡对应的那行", created="2026-07-09T00:00:00Z")
            conn.execute(sa.text(
                "INSERT INTO scene_cards (scene_id, project_id, chapter_id, scene_seq, scene_goal) "
                "VALUES ('PRJ_K_CH01_SC02', 'PRJ_K', 'PRJ_K_CH01', 2, '已经写好的正文')"
            ))

        cfg = alembic_config()
        command.upgrade(cfg, "20260725_0075")

        with engine.begin() as conn:
            rows = {
                pk: scene_id
                for pk, scene_id in conn.execute(
                    sa.text("SELECT scene_plan_id, scene_id FROM snowflake_scene_plans WHERE project_id = 'PRJ_K'")
                )
            }
        assert rows["sp_card"] == "PRJ_K_CH01_SC02", "场景卡对应的那一行被重铸了，正文的关联断在这里"
        assert rows["sp_old"] == "PRJ_K_SC_R_OLD"
    finally:
        engine.dispose()
        reset_engine()
