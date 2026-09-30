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

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

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


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _export_path(path: Path) -> Path:
    return path.with_name(path.name + ".0098-voice-relation-cards.json")


def test_0098_drops_the_empty_card_tables_and_the_dead_columns_and_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "dead-schema-0098.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    for table, columns in DEAD_COLUMNS.items():
        assert columns <= _columns(path, table), table
    with sqlite3.connect(path) as connection:
        _insert_minimal_row(connection, "chapter_goals", {"chapter_id": "ch-0098", "chapter_goal": "旧信"})
        _insert_minimal_row(
            connection,
            "chapter_states",
            {"chapter_id": "ch-0098", "current_phase": "drafting", "aggregate_block_reason": "none"},
        )
        _insert_minimal_row(connection, "scene_cards", {"scene_id": "sc-0098", "chapter_id": "ch-0098", "scene_seq": 1})
        _insert_minimal_row(
            connection,
            "scene_bundles",
            {"bundle_id": "bundle-0098", "scene_id": "sc-0098", "chapter_id": "ch-0098", "execution_mode": "P2"},
        )
        # review_items 带生成列：整表重建时不能往生成列里复制（有行时才会暴露）
        _insert_minimal_row(
            connection,
            "review_items",
            {"review_id": "review-0098", "item_type": "scene_memory", "status": "pending", "retry_count": 2},
        )

    _migrate(path, REVISION, monkeypatch, tmp_path)

    assert {"voice_profiles", "relation_profiles"}.isdisjoint(_tables(path))
    for table, columns in DEAD_COLUMNS.items():
        assert columns.isdisjoint(_columns(path, table)), table
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

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)

    assert {"voice_profiles", "relation_profiles"} <= _tables(path)
    for table, columns in DEAD_COLUMNS.items():
        assert columns <= _columns(path, table), table
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
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        _insert_minimal_row(
            connection,
            "voice_profiles",
            {"row_id": "voice_profile_v1", "voice_profile_id": "VOICE_A", "character_id": "A", "content": "占位声线"},
        )
        _insert_minimal_row(
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

    _migrate(path, REVISION, monkeypatch, tmp_path)

    assert {"voice_profiles", "relation_profiles"}.isdisjoint(_tables(path))
    exported = json.loads(_export_path(path).read_text(encoding="utf-8"))
    assert exported["revision"] == REVISION
    assert [row["row_id"] for row in exported["tables"]["voice_profiles"]] == ["voice_profile_v1"]
    assert exported["tables"]["voice_profiles"][0]["content"] == "占位声线"
    assert [row["row_id"] for row in exported["tables"]["relation_profiles"]] == ["relation_profile_v1"]
