"""迁移 20260929_0094：开机时扫的两件旧版遗留修复改成迁移里做一次（B03-29）。

升级前的库里有：四条 run（旧流程排队中 / 旧流程运行中 / 学习作业的血缘 run / 已完成）、四本书（没有作业的
ingesting / 没有作业的 cancelling / 有排队分类作业的 ingesting / ready）。升级只改数据；降级是空操作；再升一次
不再改动（幂等）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_migration_0084_scene_plan_rendering_mode import _columns, _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260929_0093"
THIS_REVISION = "20260929_0094"
RUNS = "style_reference_runs"
BOOKS = "style_reference_books"
JOBS = "style_reference_jobs"


def _seed(path: Path) -> None:
    """原生 sqlite3 连接不开外键：各表只补必填列即可。"""
    with sqlite3.connect(path) as connection:
        for book_id, status in (
            ("book_orphan_ingesting", "ingesting"),
            ("book_orphan_cancelling", "cancelling"),
            ("book_live_classification", "ingesting"),
            ("book_ready", "ready"),
        ):
            _insert_minimal_row(
                connection,
                BOOKS,
                {
                    "book_id": book_id,
                    "title": book_id,
                    "source_kind": "upload",
                    "cloud_policy": "segments_only",
                    "text_checksum": f"checksum-{book_id}",
                    "status": status,
                    "updated_at": "2026-09-01T00:00:00+00:00",
                },
            )
        _insert_minimal_row(
            connection,
            JOBS,
            {
                "job_id": "sr_job_live",
                "kind": "classify",
                "book_id": "book_live_classification",
                "state": "queued",
            },
        )
        for run_id, status, dispatch_state in (
            ("run_legacy_queued", "running", "queued"),
            ("run_legacy_running", "running", "running"),
            ("run_learn_lineage", "running", "learn_job"),
            ("run_done", "done", "completed"),
        ):
            _insert_minimal_row(
                connection,
                RUNS,
                {
                    "run_id": run_id,
                    "book_id": "book_ready",
                    "status": status,
                    "phase": "extract",
                    "dispatch_state": dispatch_state,
                    "coverage_json": json.dumps({"layers": ["language"]}),
                },
            )


def _runs(path: Path) -> dict[str, tuple]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]: (row[1], row[2], row[3], row[4], json.loads(row[5] or "{}"))
            for row in connection.execute(
                f"SELECT run_id, status, dispatch_state, error_code, retryable, coverage_json FROM {RUNS}"
            )
        }


def _book_status(path: Path) -> dict[str, str]:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute(f"SELECT book_id, status FROM {BOOKS}").fetchall())


def test_0094_retires_legacy_runs_and_orphaned_classifications_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "boot-repairs-0094.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    columns_before = {table: _columns(path, table) for table in (RUNS, BOOKS, JOBS)}
    _seed(path)

    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (THIS_REVISION,)
    assert {table: _columns(path, table) for table in (RUNS, BOOKS, JOBS)} == columns_before

    runs = _runs(path)
    for run_id in ("run_legacy_queued", "run_legacy_running"):
        status, dispatch_state, error_code, retryable, coverage = runs[run_id]
        assert (status, dispatch_state, error_code, retryable) == ("failed", "failed", "STYLE_REFERENCE_RUN_RETIRED", 1)
        assert coverage == {"layers": ["language"], "failure_reason": "STYLE_REFERENCE_RUN_RETIRED", "retryable": True}
    assert runs["run_learn_lineage"][:2] == ("running", "learn_job")
    assert runs["run_done"][:2] == ("done", "completed")
    assert _book_status(path) == {
        "book_orphan_ingesting": "failed",
        "book_orphan_cancelling": "failed",
        "book_live_classification": "ingesting",
        "book_ready": "ready",
    }

    # 降级是空操作；再升一次没有候选、不再改动
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    assert _runs(path) == runs
    _migrate(path, THIS_REVISION, monkeypatch, tmp_path)
    assert _runs(path) == runs and _book_status(path)["book_live_classification"] == "ingesting"
