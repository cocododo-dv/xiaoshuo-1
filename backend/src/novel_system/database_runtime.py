"""Database bootstrap settings with no dependency on services or ORM modules."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from novel_system.env_parsing import bool_env


BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = BACKEND_ROOT / "novel_system.db"


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    database_url: str
    sqlite_foreign_keys_enabled: bool


def load_database_runtime() -> DatabaseRuntime:
    return DatabaseRuntime(
        database_url=os.environ.get(
            "NOVEL_SYSTEM_DATABASE_URL",
            f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}",
        ),
        sqlite_foreign_keys_enabled=bool_env(
            "NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED",
            True,
            strict=True,
        ),
    )
