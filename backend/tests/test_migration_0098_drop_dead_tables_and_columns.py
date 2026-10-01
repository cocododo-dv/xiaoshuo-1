"""迁移 20260929_0098：删声线卡 / 关系卡两张表（批准 #15，重评 R8）与没有读写者的死列（B12-10、A1、重评 R2）。

- 两张卡片表是空的就直接删；万一还有行（只可能是旧预检补建的占位卡），先原样写进库文件旁边的 JSON 再删，
  不让 ``alembic upgrade head`` 卡住启动脚本；
- 死列一律删掉，同一张表里其余的列、行与索引原样留着；
- 降级按原来的形状把表与列的**结构**加回来（非空列带服务端默认值），删掉的值不回来。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate, table_columns, table_names

PREVIOUS_HEAD = "20260929_0097"
REVISION = "20260929_0098"

DEAD_COLUMNS = {
    "review_items": {"retry_count", "max_retry", "snooze_until"},
    "review_derived_snoozes": {"snooze_until"},
    "snowflake_revision_links": {"resolved_at"},
    "snowflake_scene_plans": {"involved_foreshadowing_json", "downstream_obligations_json"},
    "style_reference_evidences": {"is_synthetic"},
    "scene_bundles": {"execution_mode"},
    "chapter_states": {"aggregate_block_reason", "chapter_backfill_pending_count", "manual_hold_reason"},
    "scene_run_states": {"candidate_dispersion_score"},
}


def _export_path(path: Path) -> Path:
    return path.with_name(path.name + ".0098-voice-relation-cards.json")


def test_0098_drops_the_empty_card_tables_and_the_dead_columns_and_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "dead-schema-0098.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    for table, columns in DEAD_COLUMNS.items():
        assert columns <= table_columns(path, table), table
    with sqlite3.connect(path) as connection:
        insert_minimal_row(connection, "chapter_goals", {"chapter_id": "ch-0098", "chapter_goal": "旧信"})
        insert_minimal_row(
            connection,
            "chapter_states",
            {"chapter_id": "ch-0098", "current_phase": "drafting", "aggregate_block_reason": "none"},
        )
        insert_minimal_row(connection, "scene_cards", {"scene_id": "sc-0098", "chapter_id": "ch-0098", "scene_seq": 1})
        insert_minimal_row(
            connection,
            "scene_bundles",
            {"bundle_id": "bundle-0098", "scene_id": "sc-0098", "chapter_id": "ch-0098", "execution_mode": "P2"},
        )
        # review_items 带生成列：整表重建时不能往生成列里复制（有行时才会暴露）
        insert_minimal_row(
            connection,
            "review_items",
            {"review_id": "review-0098", "item_type": "scene_memory", "status": "pending", "retry_count": 2},
        )

    migrate(path, REVISION, monkeypatch)

    assert {"voice_profiles", "relation_profiles"}.isdisjoint(table_names(path))
    for table, columns in DEAD_COLUMNS.items():
        assert columns.isdisjoint(table_columns(path, table)), table
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT current_phase FROM chapter_states WHERE chapter_id = 'ch-0098'").fetchone() == (
            "drafting",
        )
        assert connection.execute("SELECT scene_id FROM scene_bundles WHERE bundle_id = 'bundle-0098'").fetchone() == (
            "sc-0098",
        )
        assert connection.execute(
            "SELECT target_collection FROM review_items WHERE review_id = 'review-0098'"
        ).fetchone() == ("scene_memories",)
    assert not _export_path(path).exists()

    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)

    assert {"voice_profiles", "relation_profiles"} <= table_names(path)
    for table, columns in DEAD_COLUMNS.items():
        assert columns <= table_columns(path, table), table
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT aggregate_block_reason, chapter_backfill_pending_count FROM chapter_states WHERE chapter_id = 'ch-0098'"
        ).fetchone() == ("none", 0)
        # 加回来的列取服务端默认值（删掉的值不回来）
        assert connection.execute("SELECT execution_mode FROM scene_bundles WHERE bundle_id = 'bundle-0098'").fetchone() == (
            "P2",
        )
        assert connection.execute(
            "SELECT target_collection, retry_count, max_retry FROM review_items WHERE review_id = 'review-0098'"
        ).fetchone() == ("scene_memories", 0, 3)


def test_0098_exports_leftover_card_rows_next_to_the_database_before_dropping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cards-0098.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        insert_minimal_row(
            connection,
            "voice_profiles",
            {"row_id": "voice_profile_v1", "voice_profile_id": "VOICE_A", "character_id": "A", "content": "占位声线"},
        )
        insert_minimal_row(
            connection,
            "relation_profiles",
            {
                "row_id": "relation_profile_v1",
                "relation_profile_id": "REL_A_B",
                "left_character_id": "A",
                "right_character_id": "B",
                "content": "占位关系",
            },
        )

    migrate(path, REVISION, monkeypatch)

    assert {"voice_profiles", "relation_profiles"}.isdisjoint(table_names(path))
    exported = json.loads(_export_path(path).read_text(encoding="utf-8"))
    assert exported["revision"] == REVISION
    assert [row["row_id"] for row in exported["tables"]["voice_profiles"]] == ["voice_profile_v1"]
    assert exported["tables"]["voice_profiles"][0]["content"] == "占位声线"
    assert [row["row_id"] for row in exported["tables"]["relation_profiles"]] == ["relation_profile_v1"]


def test_0098_card_export_never_lands_in_the_public_repository() -> None:
    """导出的卡片行可能带真实人物 id 与文字（旧预检拿人物 id 铸的占位卡）：库默认就在仓库的 backend/ 下，导出文件与库
    文件一样必须被 .gitignore 挡住（复核 P09b-R3）。"""
    from fnmatch import fnmatch

    repo_root = Path(__file__).resolve().parents[2]
    patterns = [
        line.strip()
        for line in (repo_root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "!")) and "/" not in line.strip()
    ]
    exported = _export_path(repo_root / "backend" / "novel_system.db").name
    assert any(fnmatch(exported, pattern) for pattern in patterns), exported
    # 备份清单（*.db.meta.json）按惯例入库，不能被同一条规则顺手挡掉
    assert not any(fnmatch("novel_system_pre_0098.db.meta.json", pattern) for pattern in patterns)
