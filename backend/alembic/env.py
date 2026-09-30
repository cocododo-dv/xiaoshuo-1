from __future__ import annotations

import logging
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, event, pool

import novel_system

# Checkout guard (B12-08). Git worktrees share one venv whose editable install
# points at another checkout. ``python -m alembic`` run from a worktree's
# ``backend/`` without ``PYTHONPATH=src`` would read this checkout's revisions
# but import the other checkout's ``novel_system`` -- including its default
# database path, i.e. that checkout's real ``backend/novel_system.db``. Refuse
# before anything below imports the application or opens a database. The
# check is written out here on purpose: it must not depend on the package it
# is checking. Both sides are compared fully resolved: a checkout whose
# backend/src is a symlink (or a Windows junction) imports the package through
# the link, and the loaded dirs below are resolved too.
_THIS_CHECKOUT_BACKEND = Path(__file__).resolve().parents[1]
_THIS_CHECKOUT_PACKAGE = (_THIS_CHECKOUT_BACKEND / "src" / "novel_system").resolve()


def _loaded_novel_system_dirs() -> set[Path]:
    dirs = {Path(entry).resolve() for entry in getattr(novel_system, "__path__", ())}
    origin = getattr(novel_system, "__file__", None)
    if origin:
        dirs.add(Path(origin).resolve().parent)
    return dirs


def _refuse_foreign_novel_system() -> None:
    loaded = _loaded_novel_system_dirs()
    if loaded != {_THIS_CHECKOUT_PACKAGE}:
        # The hint names this checkout's own src dir, not a link target.
        backend_dir = _THIS_CHECKOUT_BACKEND
        raise RuntimeError(
            "checkout mismatch: these migrations belong to "
            f"{backend_dir}, but novel_system was imported from "
            f"{', '.join(sorted(str(path) for path in loaded)) or '<unknown>'}. "
            f"Run Alembic with PYTHONPATH={backend_dir / 'src'} (from {backend_dir}: "
            "PYTHONPATH=src python -m alembic ...)."
        )


_refuse_foreign_novel_system()

from novel_system.env_config import load_database_runtime  # noqa: E402
from novel_system.db.base import Base  # noqa: E402
from novel_system.db import models  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    # Alembic is also invoked in-process by maintenance tools and tests.  The
    # logging module's default would disable every already-imported application
    # logger that is absent from alembic.ini, silently removing later audit
    # events from the host process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Migrations must be able to bootstrap an empty or historical database before
# the runtime configuration tables exist. Only environment-level DB settings
# are valid at this layer.
config.set_main_option(
    "sqlalchemy.url",
    load_database_runtime().database_url,
)

target_metadata = Base.metadata

# How many offending (table -> parent) pairs a failed foreign-key check names.
_FOREIGN_KEY_REPORT_LIMIT = 12


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _foreign_key_violations(connection) -> dict[tuple[str, str], int]:
    """``PRAGMA foreign_key_check`` counted per (child table, parent table)."""

    counts: dict[tuple[str, str], int] = {}
    for table, _rowid, parent, _fkid in connection.exec_driver_sql("PRAGMA foreign_key_check"):
        key = (str(table), str(parent))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _describe_violations(counts: dict[tuple[str, str], int]) -> str:
    shown = sorted(counts.items())[:_FOREIGN_KEY_REPORT_LIMIT]
    summary = ", ".join(f"{table} -> {parent} x{count}" for (table, parent), count in shown)
    if len(counts) > len(shown):
        summary += f", ... ({len(counts) - len(shown)} more)"
    return summary


def _revisions_may_run(migration_context) -> bool:
    """Whether this command can apply a revision -- only then is the FK scan worth it.

    ``upgrade`` / ``downgrade`` / ``stamp`` carry a destination; ``current``,
    ``check``, ``history`` and ``show`` never run a revision. A destination the
    database is already at applies nothing either: that is every launcher start
    (``upgrade head`` on an up-to-date database), where a cold scan of a
    ~400 MB database costs seconds for nothing. A destination that cannot be
    compared here (``-1``, ``+2``, a partial id) counts as "may run".
    """

    if migration_context.opts.get("destination_rev") is None:
        return False
    destination = context.get_revision_argument()
    if destination is None:  # "base"
        wanted: set[str] = set()
    elif isinstance(destination, tuple):  # "heads"
        wanted = set(destination)
    else:
        wanted = {destination}
    return set(migration_context.get_current_heads()) != wanted


def _refuse_foreign_keys_broken_by_this_run(
    before: dict[tuple[str, str], int],
    after: dict[tuple[str, str], int],
) -> None:
    """Fail the run when it left rows violating a declared FK (B12-14).

    Migrations run with enforcement off (table rebuilds need it), so nothing
    else notices rows a revision orphaned. SQLite commits every revision on its
    own, so this runs after the last one: the revisions stay applied and the
    command exits non-zero, which stops the launcher that ran it before its
    backend starts. The failure fires once: a second ``upgrade head`` finds
    nothing to apply, skips the scan (``_revisions_may_run``) and lets the
    backend start on the orphaned rows -- so the error tells the operator to
    restore the backup taken before the upgrade or repair the rows first.
    Violations the database already had before a run that applies revisions are
    reported but do not fail it -- a migration did not cause them, and refusing
    would only keep the author out of an otherwise working install.
    """

    introduced = {
        key: count - before.get(key, 0) for key, count in after.items() if count > before.get(key, 0)
    }
    if introduced:
        raise RuntimeError(
            f"sqlite_foreign_key_check_failed: {sum(introduced.values())} row(s) violate a "
            f"foreign key after migrating: {_describe_violations(introduced)}. "
            "These revisions are already applied (SQLite commits each one), so running the "
            "upgrade again finds nothing to apply and will not stop the backend from starting "
            "on these rows. Before starting it, restore the backup taken before this upgrade "
            "(python -m novel_system.tools.db_backup --restore BACKUP DST) or repair the listed rows."
        )
    if after:
        logging.getLogger("alembic.env").warning(
            "database already violated foreign keys before this migration run (left as is): %s",
            _describe_violations(after),
        )


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    is_sqlite = str(config.get_main_option("sqlalchemy.url")).startswith("sqlite")
    if is_sqlite:
        # SQLite table-rebuild migrations need FK enforcement disabled on their
        # dedicated connection. Runtime application connections use the opposite
        # fail-closed default in db/session.py. When revisions may run, a full
        # PRAGMA foreign_key_check runs before the first and after the last one
        # (``_refuse_foreign_keys_broken_by_this_run``).
        @event.listens_for(connectable, "connect")
        def configure_sqlite_migration_connection(
            dbapi_connection,
            _connection_record,
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("PRAGMA foreign_keys")
                row = cursor.fetchone()
                if row is None or int(row[0]) != 0:
                    raise RuntimeError("sqlite_migration_foreign_keys_not_disabled")
            finally:
                cursor.close()
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        check_foreign_keys = is_sqlite and _revisions_may_run(context.get_context())
        violations_before = _foreign_key_violations(connection) if check_foreign_keys else {}
        with context.begin_transaction():
            context.run_migrations()
        if check_foreign_keys:
            _refuse_foreign_keys_broken_by_this_run(violations_before, _foreign_key_violations(connection))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
