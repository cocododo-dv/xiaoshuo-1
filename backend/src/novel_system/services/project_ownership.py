"""Single source of truth for rows directly owned by a story project.

Every ORM table that carries ``project_id`` is project-scoped by construction.
Keeping this inventory derived from SQLAlchemy metadata prevents purge/reset
code from silently missing a newly introduced project model.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from functools import lru_cache
from typing import Any

from sqlalchemy import Table

from novel_system.db import models as _models  # noqa: F401 - register all mappers
from novel_system.db.base import Base


def tables_child_first(key_columns: Iterable[str], *, exclude: Collection[str] = ()) -> tuple[Table, ...]:
    """Every table except ``story_projects`` (and ``exclude``) that carries any of ``key_columns``,
    children before parents (FK-safe delete order).

    The one metadata walk behind the author-state reset (``project_id``) and the permanent purge
    (``project_purge.PURGE_KEY_COLUMNS``).
    """

    columns = tuple(key_columns)
    return tuple(
        table
        for table in reversed(Base.metadata.sorted_tables)
        if table.name != "story_projects"
        and table.name not in exclude
        and any(column in table.c for column in columns)
    )


@lru_cache(maxsize=1)
def project_owned_tables_child_first() -> tuple[Table, ...]:
    """Return every non-root table with ``project_id`` in FK-safe delete order."""

    return tables_child_first(("project_id",))


@lru_cache(maxsize=1)
def project_owned_models_child_first() -> tuple[type[Any], ...]:
    model_by_table = {
        mapper.local_table: mapper.class_
        for mapper in Base.registry.mappers
    }
    missing_mappers = [
        table.name
        for table in project_owned_tables_child_first()
        if table not in model_by_table
    ]
    if missing_mappers:
        raise RuntimeError(
            "project-owned tables are missing ORM mappers: "
            + ", ".join(sorted(missing_mappers))
        )
    return tuple(
        model_by_table[table] for table in project_owned_tables_child_first()
    )
