"""Environment-variable parsers shared by settings and the *_runtime loaders.

A leaf: imports only the standard library, so the Alembic bootstrap
(``database_runtime``) and the low-level ledger (``llm_accounting_runtime``) can use it
without pulling in services.

Two boolean rules exist on purpose and stay per variable exactly as before:
``bool_env(strict=False)`` treats anything outside ``1/true/yes/on`` as ``False``
(a typo silently disables), ``bool_env(strict=True)`` raises on a value outside the two
recognised sets.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def bool_env(name: str, default: bool, *, strict: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if not strict:
        return normalized in _TRUE_VALUES
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off)")


def positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def quota_int_env(name: str, default: int) -> int:
    """Parse an optional hard-fence bound, where ``0`` disables the fence."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    message = f"{name} must be a non-negative integer (0 disables the limit)"
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if value < 0:
        raise ValueError(message)
    return value


def non_negative_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid number") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def list_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated list; an unset or all-blank value yields ``default``."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    items = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    return items or default


def path_list_env(
    name: str,
    resolve: Callable[[str], Path],
    default: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    """``os.pathsep``-separated paths, each passed through ``resolve``."""
    raw_value = os.environ.get(name, "")
    if not raw_value.strip():
        return default
    return tuple(
        resolve(item.strip())
        for item in raw_value.split(os.pathsep)
        if item.strip()
    )
