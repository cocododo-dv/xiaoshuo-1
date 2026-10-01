"""迁移 20260929_0095：场景诊断 / 深评 / 作者稿的逐场查询加索引（只加索引，可逆，重复升级无害）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate

PREVIOUS_HEAD = "20260929_0094"
CURRENT_HEAD = "20260929_0095"
EXPECTED = {
    "writer_evaluations": {
        "ix_writer_evaluations_object": ["object_type", "object_id", "rubric_id", "created_at"],
        "ix_writer_evaluations_parent": ["parent_evaluation_id"],
    },
    "passage_patch_candidates": {
        "ix_passage_patch_candidates_object": ["object_type", "object_id", "created_at"],
    },
    "author_drafts": {
        "ix_author_drafts_object": ["object_type", "object_id", "status", "updated_at"],
    },
}


def _indexes(path: Path, table: str) -> dict[str, list[str]]:
    with sqlite3.connect(path) as connection:
        names = [row[1] for row in connection.execute(f'PRAGMA index_list("{table}")')]
        return {
            name: [row[2] for row in connection.execute(f'PRAGMA index_info("{name}")')]
            for name in names
            if not name.startswith("sqlite_autoindex")
        }


def test_0095_indexes_the_diagnosis_lookups_and_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "diagnosis-indexes-0095.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    for table, indexes in EXPECTED.items():
        assert not (set(indexes) & set(_indexes(path, table)))

    # 升级前就有的行：只加索引，行原样留着（原生 sqlite3 不开外键，不必先建场景行）
    with sqlite3.connect(path) as connection:
        insert_minimal_row(
            connection,
            "writer_evaluations",
            {"evaluation_id": "eval-0095", "object_type": "scene", "object_id": "scene-0095", "rubric_id": "literary_revision_v1"},
        )
        connection.commit()

    migrate(path, CURRENT_HEAD, monkeypatch)  # 升到本迁移自己的版本：后续迁移不必回头改这里
    for table, indexes in EXPECTED.items():
        present = _indexes(path, table)
        for name, columns in indexes.items():
            assert present.get(name) == columns, (table, name, present)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        assert connection.execute("SELECT count(*) FROM writer_evaluations").fetchone() == (1,)
        # 逐场「最新评审」的查询真的走索引，不再整表扫描
        plan = " ".join(
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT evaluation_id FROM writer_evaluations WHERE object_type = 'scene' "
                "AND object_id = 'scene-0095' AND rubric_id = 'literary_revision_v1' ORDER BY created_at DESC"
            )
        )
        assert "ix_writer_evaluations_object" in plan and "SCAN writer_evaluations" not in plan, plan

    # 重复升级（版本号退回、索引已在）无害：跳过已有的同名索引
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = ?", (PREVIOUS_HEAD,))
        connection.commit()
    migrate(path, CURRENT_HEAD, monkeypatch)
    for table, indexes in EXPECTED.items():
        present = _indexes(path, table)
        assert all(present.get(name) == columns for name, columns in indexes.items()), (table, present)

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    for table, indexes in EXPECTED.items():
        assert not (set(indexes) & set(_indexes(path, table)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert connection.execute("SELECT count(*) FROM writer_evaluations").fetchone() == (1,)
