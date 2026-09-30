"""Small value-coercion helpers shared across services (a leaf: standard library only).

Each behaviour has its own explicit name; copies that behaved differently were not
merged. Variants that stay local on purpose (different contract):
``pricing._as_int`` (clamps to >= 0), ``scene_design_context._coerce_int`` (0 → None),
``learn_extract._as_int`` (parses 【12】-style page labels), ``projects._optional_text``
(``str()`` of any non-None value), ``literary_quality._string_list`` (de-duplicates),
``projects._string_list`` (wraps a scalar).
"""

from __future__ import annotations

import math
from typing import Any, Sequence


def finite_or_none(value: Any) -> float | None:
    """A finite float, or ``None`` for bools, non-numbers, NaN and infinities."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def finite_or_none_accepting_bool(value: Any) -> float | None:
    """Like :func:`finite_or_none` but ``True`` / ``False`` read as ``1.0`` / ``0.0``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def finite_or_zero(value: Any) -> float:
    """A finite float, or ``0.0`` for non-numbers, NaN and infinities (bools read as 1 / 0)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def int_or_default(value: Any, default: int) -> int:
    """``int(value)``, or ``default`` when that raises ``TypeError`` / ``ValueError``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def usage_int(value: Any) -> int | None:
    """A provider usage counter: a non-negative whole int / float, else ``None`` (bools rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or int(value) != value:
        return None
    return int(value)


def optional_text(value: Any) -> str | None:
    """The stripped string, or ``None`` for a non-string or blank value."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def coerce_string_list(value: Any) -> list[str]:
    """A string splits by line; a list keeps its non-blank items as stripped text; else ``[]``."""
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def has_value(value: Any) -> bool:
    """Whether a JSON-like value carries anything: non-blank text anywhere inside, or any scalar."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(has_value(item) for item in value)
    if isinstance(value, dict):
        return any(has_value(item) for item in value.values())
    return True


def quantile(values: Sequence[float], ratio: float) -> float:
    """线性插值分位数（空序列为 0.0）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
