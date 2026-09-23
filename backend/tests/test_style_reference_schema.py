"""Alembic 0036 / 0037 双向 migration + glob 断言失败路径单测。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。

本文件覆盖父 conftest 的 `isolated_database` autouse fixture(它会调
Base.metadata.create_all,与 alembic up/down 互相干扰),改用一个仅
monkeypatch DATABASE_URL 的轻量替代。
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REVISION_BASE = "20260515_0035"
REVISION_DROP_LEGACY = "20260523_0036"
REVISION_NEW_SCHEMA = "20260523_0037"
REVISION_FINDINGS_HASH = "20260523_0038"
REVISION_METRIC_EVENTS = "20260524_0039"
REVISION_DROP_PROJECT_REFERENCE_PROFILE_IDS = "20260531_0040"

NEW_TABLES = [
    "style_reference_books",
    "style_reference_paragraphs",
    "style_reference_runs",
    "style_reference_extractions",
    "style_reference_quotes",
    "style_reference_findings",
    "style_reference_evidences",
    "style_reference_profiles",
    "style_reference_injection_bindings",
    "style_reference_validation_reports",
    "style_reference_banned_terms",
]
# 0037 建的表里,回测报告表已由 20260923_0091(风格参考 v3 清理迁移)删除:head 上只剩其余十张
DROPPED_AT_HEAD = {"style_reference_validation_reports"}
HEAD_TABLES = [tbl for tbl in NEW_TABLES if tbl not in DROPPED_AT_HEAD]

LEGACY_TABLES = [
    "reference_books",
    "reference_book_segments",
    "reference_learning_runs",
    "reference_learning_rounds",
    "reference_findings",
    "reference_profiles",
]


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(_backend_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_backend_root() / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Path, None, None]:
    """覆盖父 conftest 的同名 fixture:仅设 DB URL,不调 create_all。"""
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", db_url)
    monkeypatch.setenv("NOVEL_SYSTEM_VECTOR_BACKEND", "memory")
    monkeypatch.setenv("NOVEL_SYSTEM_CHROMA_DIR", str(tmp_path / "chroma"))
    from novel_system.db.session import reset_engine

    reset_engine()
    yield db_path
    reset_engine()


@pytest.fixture
def fake_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """伪造 backups/style_reference_legacy_*.json,让 0036 的 glob 断言通过。

    0036 的 `_repo_root()` 优先读 `STYLE_REFERENCE_REPO_ROOT` 环境变量,
    便于测试与真实 backups/ 隔离。
    """
    fake_root = tmp_path / "fake_repo"
    fake_root.mkdir()
    backup_dir = fake_root / "backups"
    backup_dir.mkdir()
    (backup_dir / "style_reference_legacy_20260523_000000.json").write_text(
        '{"row_count": 0, "profiles": []}',
        encoding="utf-8",
    )
    monkeypatch.setenv("STYLE_REFERENCE_REPO_ROOT", str(fake_root))
    return fake_root


def _existing_tables(db_url: str) -> set[str]:
    engine = sa.create_engine(db_url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _list_indexes(db_url: str, table_name: str) -> set[str]:
    engine = sa.create_engine(db_url)
    try:
        return {idx["name"] for idx in sa.inspect(engine).get_indexes(table_name)}
    finally:
        engine.dispose()


def _list_columns(db_url: str, table_name: str) -> set[str]:
    engine = sa.create_engine(db_url)
    try:
        return {col["name"] for col in sa.inspect(engine).get_columns(table_name)}
    finally:
        engine.dispose()


def _list_unique_constraints(db_url: str, table_name: str) -> list[dict]:
    engine = sa.create_engine(db_url)
    try:
        return sa.inspect(engine).get_unique_constraints(table_name)
    finally:
        engine.dispose()


def test_upgrade_creates_the_style_reference_tables(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    tables = _existing_tables(db_url)
    for tbl in HEAD_TABLES:
        assert tbl in tables, f"{tbl} 未创建"
    for tbl in DROPPED_AT_HEAD:
        assert tbl not in tables, f"{tbl} 应已被 0091 删除"
    # 旧表已被 0036 drop
    for tbl in LEGACY_TABLES:
        assert tbl not in tables, f"{tbl} 应已被 0036 drop"


def test_indexes_present(isolated_database: Path, fake_backup: Path) -> None:
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    assert "ix_style_reference_books_status_updated_at" in _list_indexes(
        db_url, "style_reference_books"
    )
    assert "ix_style_reference_findings_book_sub_kind" in _list_indexes(
        db_url, "style_reference_findings"
    )
    assert "ix_style_reference_extractions_book_layer_sub" in _list_indexes(
        db_url, "style_reference_extractions"
    )


def test_upgrade_without_backup_raises(
    isolated_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 覆盖 session-scoped backup stub:指向一个空目录,让 0036 的 glob 断言失败
    empty_root = tmp_path / "no_backup_repo"
    empty_root.mkdir()
    monkeypatch.setenv("STYLE_REFERENCE_REPO_ROOT", str(empty_root))

    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    with pytest.raises(RuntimeError, match=r"backups/style_reference_legacy_"):
        command.upgrade(cfg, "head")


def test_findings_statement_hash_column_present(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    """0038 hotfix:findings 表必须有 statement_hash 列。"""
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    columns = _list_columns(db_url, "style_reference_findings")
    assert "statement_hash" in columns, "0038 应为 findings 添加 statement_hash 列"


def test_findings_unique_constraint_includes_statement_hash(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    """0038 hotfix:UNIQUE 复合 4 列 (extraction_id, sub_dim, finding_kind, statement_hash)。"""
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    uniques = _list_unique_constraints(db_url, "style_reference_findings")
    # 应找到名为 ..._extract_sub_kind_hash 的约束
    matching = [u for u in uniques if "statement_hash" in (u.get("column_names") or [])]
    assert matching, f"UNIQUE 应含 statement_hash 列;实际:{uniques}"
    target = matching[0]
    assert set(target["column_names"]) == {
        "extraction_id",
        "sub_dimension",
        "finding_kind",
        "statement_hash",
    }


def test_downgrade_0038_removes_statement_hash(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    """0038 downgrade 后 statement_hash 列消失,旧 UNIQUE 3 列恢复。"""
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, REVISION_FINDINGS_HASH)
    command.downgrade(cfg, REVISION_NEW_SCHEMA)
    columns = _list_columns(db_url, "style_reference_findings")
    assert "statement_hash" not in columns


def test_downgrade_drops_new_tables_then_recreates_legacy(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, REVISION_NEW_SCHEMA)
    assert "style_reference_books" in _existing_tables(db_url)

    # 退到 0036(11 张新表消失,旧表仍不存在)
    command.downgrade(cfg, REVISION_DROP_LEGACY)
    tables_after_drop_new = _existing_tables(db_url)
    for tbl in NEW_TABLES:
        assert tbl not in tables_after_drop_new
    for tbl in LEGACY_TABLES:
        assert tbl not in tables_after_drop_new

    # 退到 0035(0036 downgrade 重建空旧表)
    command.downgrade(cfg, REVISION_BASE)
    tables_after_full_down = _existing_tables(db_url)
    for tbl in NEW_TABLES:
        assert tbl not in tables_after_full_down
    for tbl in LEGACY_TABLES:
        assert tbl in tables_after_full_down, f"{tbl} 应被 0036.downgrade 重建"

    # 再次升至 head 应成功
    command.upgrade(cfg, "head")
    tables_after_up = _existing_tables(db_url)
    for tbl in HEAD_TABLES:
        assert tbl in tables_after_up


def test_head_drops_story_project_reference_profile_ids_column(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, "head")

    story_project_columns = _list_columns(db_url, "story_projects")
    assert "reference_profile_ids_json" not in story_project_columns


def test_downgrade_0040_to_0039_restores_story_project_reference_profile_ids(
    isolated_database: Path,
    fake_backup: Path,
) -> None:
    db_url = f"sqlite:///{isolated_database}"
    cfg = _alembic_config(db_url)

    command.upgrade(cfg, REVISION_DROP_PROJECT_REFERENCE_PROFILE_IDS)
    command.downgrade(cfg, REVISION_METRIC_EVENTS)

    story_project_columns = _list_columns(db_url, "story_projects")
    assert "reference_profile_ids_json" in story_project_columns
