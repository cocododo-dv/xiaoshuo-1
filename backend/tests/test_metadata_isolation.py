from __future__ import annotations

import ast
import logging
from pathlib import Path

import sqlalchemy as sa

from novel_system.db import models  # noqa: F401 — register every mapped table (db/__init__ no longer does)
from novel_system.db.base import Base


def test_revision_files_do_not_import_live_orm_metadata() -> None:
    """Historical revisions must be deterministic under future model changes."""
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    offenders: list[str] = []

    for migration_path in sorted(versions_dir.glob("*.py")):
        tree = ast.parse(migration_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "novel_system.db"
            ):
                offenders.append(f"{migration_path.name}:{node.lineno}: application DB import")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("novel_system.db"):
                        offenders.append(
                            f"{migration_path.name}:{node.lineno}: application DB import"
                        )
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "metadata"
                and isinstance(node.value, ast.Name)
                and node.value.id == "Base"
            ):
                offenders.append(f"{migration_path.name}:{node.lineno}: live Base.metadata")

    assert not offenders, (
        "Alembic revisions must use frozen, explicit DDL instead of importing the live ORM: "
        + "; ".join(offenders)
    )


def test_primary_metadata_excludes_legacy_reference_tables() -> None:
    assert "style_reference_books" in Base.metadata.tables
    assert "style_reference_profiles" in Base.metadata.tables
    assert "reference_books" not in Base.metadata.tables
    assert "reference_profiles" not in Base.metadata.tables


def test_create_all_only_builds_main_schema(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "metadata_isolation.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{db_path}")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(bind=engine)
        tables = set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "style_reference_books" in tables
    assert "style_reference_profiles" in tables
    assert "llm_call_attempts" in tables
    assert "reference_books" not in tables
    assert "reference_profiles" not in tables


def _normalize_sql(text: str) -> str:
    # 大小写折叠 + 空白折叠：迁移侧冻结 DDL 与 ORM 侧的 IN/in 大小写、换行缩进差异是已知噪音，
    # 不构成真实漂移。
    return " ".join((text or "").split()).casefold()


# 已知 CHECK 漂移豁免（勿扩充；每一项必须写明冲突与出路）。当前为空：
# evaluation_experiments 的 human-only CHECK 已随产品拍板在 ORM 侧声明对齐
# （迁移 0075 契约），机制本身保留给未来真实待决冲突。
_KNOWN_CHECK_DRIFT_ALLOWANCE: set[tuple[str, str]] = set()

# 已知的 NOT NULL 漂移（B12-11）：ORM 声明非空、迁移建出来（也就是实库）允许 NULL 的列——实库里这些列一个 NULL 都没有。
# 这是一道只收紧的棘轮：新的漂移会变红；某一列修好了（迁移改成非空，或列删掉了）也会变红，从这里删掉它。
# 以后一次表结构迁移把它们改成 NOT NULL（整表重建；涉及的表都很小）。
_KNOWN_NULLABLE_DRIFT: frozenset[tuple[str, str]] = frozenset(
    {
        ("author_draft_proposals", "merge_status"),
        ("author_draft_proposals", "proposal_kind"),
        ("chapter_goals", "state"),
        ("human_review_events", "details_json"),
        ("outline_plans", "plan_json"),
        ("review_items", "target_collection"),
        ("revision_candidates", "apply_mode"),
        ("scene_cards", "state"),
        ("scene_cards", "words_current"),
        ("scene_execution_contracts", "missing_fields_json"),
        ("scene_execution_contracts", "payload_json"),
        ("snowflake_artifacts", "artifact_json"),
        ("snowflake_assistant_turns", "suggestions_json"),
        ("snowflake_direction_briefs", "lines_json"),
        ("snowflake_scene_plans", "beats_json"),
        ("snowflake_scene_plans", "onstage_chars_json"),
        ("snowflake_scene_plans", "orphaned_flag"),
        ("snowflake_scene_triage_items", "fix_steps_json"),
        ("snowflake_scene_triage_items", "missing_fields_json"),
        ("snowflake_scene_triage_items", "pressure_flags_json"),
        ("snowflake_scene_triage_items", "repair_patch_json"),
        ("snowflake_step_runs", "draft_json"),
        ("story_projects", "approved_chapter_ids_json"),
        ("story_projects", "trashed_flag"),
    }
)


def _schema_snapshot(engine) -> dict:
    """Reflect tables, per-table column names, named indexes, CHECK constraints,
    and computed (generated) column expressions.

    Both compared databases are reflected through the same SQLite inspector, so
    SQLite-specific reflection quirks (FK/nullable under-reporting) appear identically
    on both sides and cancel out. Auto-generated ``sqlite_autoindex_*`` entries are
    dropped (they track inline UNIQUE/PK constraints, not user-declared indexes).
    CHECK/computed SQL text is compared casefolded with whitespace collapsed (see
    ``_normalize_sql``).
    """
    inspector = sa.inspect(engine)
    tables = {t for t in inspector.get_table_names() if t != "alembic_version"}
    columns = {t: frozenset(c["name"] for c in inspector.get_columns(t)) for t in tables}
    nullable = {(t, c["name"]): bool(c["nullable"]) for t in tables for c in inspector.get_columns(t)}
    foreign_keys = {
        t: frozenset(
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(t)
        )
        for t in tables
    }
    indexes = {}
    check_constraints = {}
    computed_columns = {}
    for table_name in tables:
        rows = set()
        for index in inspector.get_indexes(table_name):
            name = index.get("name") or ""
            if name.startswith("sqlite_autoindex"):
                continue
            rows.add((name, tuple(index.get("column_names") or ()), bool(index.get("unique"))))
        indexes[table_name] = frozenset(rows)
        check_constraints[table_name] = frozenset(
            (check.get("name") or "", _normalize_sql(check.get("sqltext") or ""))
            for check in inspector.get_check_constraints(table_name)
            if (table_name, check.get("name") or "") not in _KNOWN_CHECK_DRIFT_ALLOWANCE
        )
        computed_columns[table_name] = frozenset(
            (column["name"], _normalize_sql(column["computed"].get("sqltext") or ""))
            for column in inspector.get_columns(table_name)
            if column.get("computed")
        )
    return {
        "tables": tables,
        "columns": columns,
        "nullable": nullable,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
        "check_constraints": check_constraints,
        "computed_columns": computed_columns,
    }


def test_migration_built_schema_matches_orm_models(tmp_path, monkeypatch) -> None:
    """Guard against model/migration drift (the class that broke ``start-dev``).

    Production and dev build their schema via ``alembic upgrade head`` (the app
    never builds it itself), while the test suite builds it via
    ``Base.metadata.create_all``. When the two diverge — e.g. a new ORM column or a
    migration-only index — every existing test still passes (it runs on the ORM-built
    schema) yet runtime 500s (``OperationalError: no such column ...``) or silently
    loses a constraint. This builds the schema BOTH ways and diffs them, so either
    direction of drift fails loudly: write the missing migration, or declare the
    missing index on the model.

    Compared dimensions: tables, columns, foreign keys, named indexes (incl. uniqueness),
    CHECK constraints (name + normalized sqltext), computed-column expressions
    (normalized), and NOT NULL flags against the frozen ``_KNOWN_NULLABLE_DRIFT`` ratchet
    (SQLite reflects NOT NULL and FKs faithfully; B12-11). Server defaults stay an
    allowed difference: migrations write them, the ORM keeps Python-side defaults.
    """
    from alembic import command
    from alembic.config import Config

    from novel_system.db.session import reset_engine

    # Backup stub so migration 0036 (drop legacy reference_learning) passes its guard
    # without touching the real, gitignored backups directory.
    fake_root = tmp_path / "repo_root"
    (fake_root / "backups").mkdir(parents=True)
    (fake_root / "backups" / "style_reference_legacy_test.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("STYLE_REFERENCE_REPO_ROOT", str(fake_root))

    migrated_db = tmp_path / "migrated.db"
    create_all_db = tmp_path / "create_all.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{migrated_db}")
    reset_engine()  # force env.py's get_settings().database_url to read the new URL

    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    audit_logger = logging.getLogger(
        "novel_system.services.metadata_isolation_audit_sentinel"
    )
    original_logger_disabled = audit_logger.disabled
    audit_logger.disabled = False
    audit_logger_preserved = False
    try:
        command.upgrade(cfg, "head")
        audit_logger_preserved = not audit_logger.disabled
        migrated_engine = sa.create_engine(f"sqlite:///{migrated_db}")
        create_all_engine = sa.create_engine(f"sqlite:///{create_all_db}")
        try:
            Base.metadata.create_all(bind=create_all_engine)
            from_migrations = _schema_snapshot(migrated_engine)
            from_models = _schema_snapshot(create_all_engine)
        finally:
            migrated_engine.dispose()
            create_all_engine.dispose()
    finally:
        audit_logger.disabled = original_logger_disabled
        reset_engine()  # restore engine singleton for subsequent tests

    assert audit_logger_preserved, (
        "in-process Alembic migration disabled an existing application audit logger"
    )
    assert from_migrations["tables"] == from_models["tables"], (
        "Tables differ between migrations and ORM models. "
        f"only-in-migrations={sorted(from_migrations['tables'] - from_models['tables'])}, "
        f"only-in-models={sorted(from_models['tables'] - from_migrations['tables'])}"
    )

    foreign_key_drift = {
        t: {
            "only-in-migrations": sorted(from_migrations["foreign_keys"][t] - from_models["foreign_keys"][t]),
            "only-in-models": sorted(from_models["foreign_keys"][t] - from_migrations["foreign_keys"][t]),
        }
        for t in from_migrations["tables"]
        if from_migrations["foreign_keys"][t] != from_models["foreign_keys"][t]
    }
    assert not foreign_key_drift, (
        "Foreign-key drift between migrations and ORM models — declare the FK on the model "
        f"or add a migration. Drift: {foreign_key_drift}"
    )

    nullable_drift = {
        key
        for key, migrated_nullable in from_migrations["nullable"].items()
        if key in from_models["nullable"] and migrated_nullable != from_models["nullable"][key]
    }
    assert nullable_drift - _KNOWN_NULLABLE_DRIFT == set(), (
        "New NOT NULL drift between migrations and ORM models — make the migration and the model agree: "
        f"{sorted(nullable_drift - _KNOWN_NULLABLE_DRIFT)}"
    )
    assert _KNOWN_NULLABLE_DRIFT - nullable_drift == set(), (
        "These columns no longer drift: remove them from _KNOWN_NULLABLE_DRIFT (the ratchet only shrinks): "
        f"{sorted(_KNOWN_NULLABLE_DRIFT - nullable_drift)}"
    )

    column_drift = {
        t: sorted(from_migrations["columns"][t] ^ from_models["columns"][t])
        for t in from_migrations["tables"]
        if from_migrations["columns"][t] != from_models["columns"][t]
    }
    assert not column_drift, (
        "Column drift between migrations and ORM models — an ORM model changed without "
        f"a matching Alembic migration (or vice versa). Write one. Drift: {column_drift}"
    )

    index_drift = {
        t: {
            "only-in-migrations": sorted(from_migrations["indexes"][t] - from_models["indexes"][t]),
            "only-in-models": sorted(from_models["indexes"][t] - from_migrations["indexes"][t]),
        }
        for t in from_migrations["tables"]
        if from_migrations["indexes"][t] != from_models["indexes"][t]
    }
    assert not index_drift, (
        "Index drift between migrations and ORM models — declare the index on the model "
        f"(__table_args__) or add a migration. Drift: {index_drift}"
    )

    check_drift = {
        t: {
            "only-in-migrations": sorted(
                from_migrations["check_constraints"][t] - from_models["check_constraints"][t]
            ),
            "only-in-models": sorted(
                from_models["check_constraints"][t] - from_migrations["check_constraints"][t]
            ),
        }
        for t in from_migrations["tables"]
        if from_migrations["check_constraints"][t] != from_models["check_constraints"][t]
    }
    assert not check_drift, (
        "CHECK-constraint drift between migrations and ORM models — align the constraint "
        f"name/expression on the model (__table_args__) or add a migration. Drift: {check_drift}"
    )

    computed_drift = {
        t: {
            "only-in-migrations": sorted(
                from_migrations["computed_columns"][t] - from_models["computed_columns"][t]
            ),
            "only-in-models": sorted(
                from_models["computed_columns"][t] - from_migrations["computed_columns"][t]
            ),
        }
        for t in from_migrations["tables"]
        if from_migrations["computed_columns"][t] != from_models["computed_columns"][t]
    }
    assert not computed_drift, (
        "Computed-column expression drift between migrations and ORM models — align the "
        f"Computed() sqltext on the model or add a migration. Drift: {computed_drift}"
    )
