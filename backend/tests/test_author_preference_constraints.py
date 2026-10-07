"""迁移 20260716_0072：给作者偏好档案表加 CHECK 之前先数非法值，有就拒绝升级、不悄悄改标签。

偏好档案表本身随写作偏好学习退役（批准 #6，R5），由迁移 20261004_0099 删掉，ORM 模型也已删除；这里只留历史迁移
0072 的失败即停行为——每个新装库仍要按顺序跑过它。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import migrate

REVISION_0071 = "20260716_0071"
REVISION_0072 = "20260716_0072"


def test_0072_migration_fails_closed_instead_of_relabeling_invalid_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "invalid-preference-values.db"
    migrate(database_path, REVISION_0072, monkeypatch)
    migrate(database_path, REVISION_0071, monkeypatch, down=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO author_preference_profiles (
                profile_id, scope_type, scope_ref_id, status,
                runtime_eligible, summary_json, source_patch_ids_json,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid_legacy_profile",
                "workspace",
                "legacy",
                "draft",
                2,
                "{}",
                "[]",
                "migration_test",
                "2026-07-16T00:00:00Z",
                "2026-07-16T00:00:00Z",
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="invalid_scope_rows=1, invalid_runtime_eligible_rows=1",
    ):
        migrate(database_path, REVISION_0072, monkeypatch)
