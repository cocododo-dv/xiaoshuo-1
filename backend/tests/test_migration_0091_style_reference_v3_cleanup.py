"""迁移 20260923_0091：风格参考 v3 清理——删旧回测报告表、发现反馈表与 ``style_reference_findings.base_confidence``。

升级时库里有行（每张表各一行、发现带 base_confidence）：两张表整表删除，发现行保留、列没了，发现表的索引与
唯一约束原样重建；降级只恢复结构（表 / 列 / 索引），删掉的行不回来。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260923_0090"
THIS_REVISION = "20260923_0091"
REPORTS = "style_reference_validation_reports"
FEEDBACK = "style_reference_finding_feedback"
FINDINGS = "style_reference_findings"


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _indexes(path: Path, table: str) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {row[1]: int(row[2]) for row in connection.execute(f"PRAGMA index_list('{table}')")}


def _unique_column_sets(path: Path, table: str) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    with sqlite3.connect(path) as connection:
        for _seq, name, unique, *_rest in connection.execute(f"PRAGMA index_list('{table}')"):
            if int(unique):
                cols = tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{name}')"))
                out.add(cols)
    return out


def _seed(path: Path) -> None:
    """原生 sqlite3 连接不开外键：各表只补必填列即可。"""
    with sqlite3.connect(path) as connection:
        _insert_minimal_row(
            connection,
            FINDINGS,
            {
                "finding_id": "sr_find_0091",
                "book_id": "sr_book_0091",
                "run_id": "sr_run_0091",
                "extraction_id": "sr_ext_0091",
                "sub_dimension": "language.rhythm",
                "finding_kind": "observation",
                "statement": "短句推进",
                "statement_hash": "0091",
                "confidence": "high",
                "base_confidence": "medium",
                "status": "active",
            },
        )
        _insert_minimal_row(
            connection,
            FEEDBACK,
            {"feedback_id": "srfb_0091", "finding_id": "sr_find_0091", "operator_ref": "operator", "vote": "up"},
        )
        _insert_minimal_row(
            connection,
            REPORTS,
            {
                "report_id": "sr_rep_0091",
                "profile_id": "sr_profile_0091",
                "target_kind": "manual",
                "verdict": "pass",
                "mode_executed": "async_full",
            },
        )


def test_0091_drops_the_retired_tables_and_column_then_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "style-reference-v3-0091.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert {REPORTS, FEEDBACK} <= _tables(path)
    assert "base_confidence" in _columns(path, FINDINGS)
    findings_indexes_before = {name for name in _indexes(path, FINDINGS) if not name.startswith("sqlite_autoindex")}
    findings_uniques_before = _unique_column_sets(path, FINDINGS)
    _seed(path)

    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    tables = _tables(path)
    assert REPORTS not in tables and FEEDBACK not in tables
    columns = _columns(path, FINDINGS)
    assert "base_confidence" not in columns
    assert {"finding_id", "statement", "statement_hash", "confidence", "status", "review_id"} <= columns
    # 发现行保留（只少了一列），索引与唯一约束随整表重建原样回来
    with sqlite3.connect(path) as connection:
        assert connection.execute(f"SELECT finding_id, confidence FROM {FINDINGS}").fetchall() == [
            ("sr_find_0091", "high")
        ]
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (THIS_REVISION,)
    findings_indexes_after = {name for name in _indexes(path, FINDINGS) if not name.startswith("sqlite_autoindex")}
    assert findings_indexes_after == findings_indexes_before
    assert {"ix_style_reference_findings_book_sub_kind", "ix_style_reference_findings_run_id"} <= findings_indexes_after
    assert _unique_column_sets(path, FINDINGS) == findings_uniques_before
    assert ("extraction_id", "sub_dimension", "finding_kind", "statement_hash") in findings_uniques_before

    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    tables = _tables(path)
    assert {REPORTS, FEEDBACK} <= tables
    assert "base_confidence" in _columns(path, FINDINGS)
    assert {
        "report_id", "profile_id", "target_kind", "target_ref_id", "verdict", "quantitative_json", "semantic_json",
        "plagiarism_json", "forbidden_hits_json", "mode_executed", "created_at", "status", "error_code", "error_text",
        "retryable", "started_at", "heartbeat_at", "finished_at",
    } == _columns(path, REPORTS)
    assert {"feedback_id", "finding_id", "operator_ref", "vote", "created_at", "updated_at"} == _columns(path, FEEDBACK)
    assert {
        "ix_style_reference_validation_reports_profile_target",
        "ix_style_reference_validation_reports_verdict",
        "ix_style_reference_validation_reports_status",
    } <= set(_indexes(path, REPORTS))
    assert "ix_sr_finding_feedback_finding" in _indexes(path, FEEDBACK)
    assert ("finding_id", "operator_ref") in _unique_column_sets(path, FEEDBACK)
    with sqlite3.connect(path) as connection:
        # 结构回来了，删掉的行不回来
        assert connection.execute(f"SELECT COUNT(*) FROM {REPORTS}").fetchone() == (0,)
        assert connection.execute(f"SELECT COUNT(*) FROM {FEEDBACK}").fetchone() == (0,)
        assert connection.execute(f"SELECT base_confidence FROM {FINDINGS}").fetchall() == [(None,)]

    # 再升一次（降级重建的结构与原来一致，迁移可重复）
    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    assert REPORTS not in _tables(path) and "base_confidence" not in _columns(path, FINDINGS)
