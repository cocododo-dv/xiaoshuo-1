from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class PaginationRequest:
    mode: str
    limit: int
    page: int | None
    page_size: int | None
    cursor: str | None


def resolve_pagination_request(
    *,
    page: int | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> PaginationRequest:
    if cursor is not None or (limit is not None and page is None and page_size is None):
        effective_limit = _normalize_limit(limit if limit is not None else page_size)
        return PaginationRequest(
            mode="cursor",
            limit=effective_limit,
            page=None,
            page_size=None,
            cursor=cursor or None,
        )

    effective_limit = _normalize_limit(page_size if page_size is not None else limit)
    return PaginationRequest(
        mode="page",
        limit=effective_limit,
        page=max(page or 1, 1),
        page_size=effective_limit,
        cursor=None,
    )


def paginate_select(
    session: Session,
    statement,
    *,
    request: PaginationRequest,
    order_columns: Sequence[tuple[Any, str]],
    cursor_values: Callable[[T], Sequence[Any]],
) -> tuple[list[T], dict[str, Any]]:
    """Paginate a scalar ORM SELECT without loading the full result set.

    ``order_columns`` is the authoritative deterministic order and also defines
    the keyset cursor. Directions must be ``"asc"`` or ``"desc"``.
    """

    normalized_order: list[tuple[Any, str]] = []
    for column, direction in order_columns:
        normalized_direction = str(direction).lower()
        if normalized_direction not in {"asc", "desc"}:
            raise ValueError(f"unsupported pagination direction: {direction}")
        normalized_order.append((column, normalized_direction))

    unordered = statement.order_by(None)
    total = int(
        session.scalar(select(func.count()).select_from(unordered.subquery())) or 0
    )
    ordered = unordered.order_by(
        *[
            column.asc() if direction == "asc" else column.desc()
            for column, direction in normalized_order
        ]
    )

    if request.mode == "page":
        assert request.page is not None
        start = max(request.page - 1, 0) * request.limit
        page_items = list(
            session.execute(ordered.offset(start).limit(request.limit)).scalars().all()
        )
        has_next = start + len(page_items) < total
        next_cursor = (
            _encode_cursor(cursor_values(page_items[-1]))
            if has_next and page_items
            else None
        )
        return page_items, {
            "mode": "page",
            "limit": request.limit,
            "page": request.page,
            "page_size": request.page_size,
            "returned": len(page_items),
            "total": total,
            "has_next": has_next,
            "next_cursor": next_cursor,
        }

    decoded_cursor = _decode_cursor(request.cursor)
    valid_keyset_cursor = (
        decoded_cursor is not None
        and len(decoded_cursor) == len(normalized_order)
        and all(
            _cursor_value_matches_column(column, value)
            for (column, _direction), value in zip(
                normalized_order, decoded_cursor, strict=True
            )
        )
    )
    if valid_keyset_cursor:
        assert decoded_cursor is not None
        keyset_terms = []
        for index, ((column, direction), value) in enumerate(
            zip(normalized_order, decoded_cursor, strict=True)
        ):
            preceding = [
                prior_column == decoded_cursor[prior_index]
                for prior_index, (prior_column, _prior_direction) in enumerate(
                    normalized_order[:index]
                )
            ]
            comparison = column > value if direction == "asc" else column < value
            keyset_terms.append(and_(*preceding, comparison))
        ordered = ordered.where(or_(*keyset_terms))

    fetched = list(
        session.execute(ordered.limit(request.limit + 1)).scalars().all()
    )
    has_next = len(fetched) > request.limit
    page_items = fetched[: request.limit]
    next_cursor = (
        _encode_cursor(cursor_values(page_items[-1]))
        if has_next and page_items
        else None
    )
    return page_items, {
        "mode": "cursor",
        "limit": request.limit,
        "page": None,
        "page_size": None,
        "returned": len(page_items),
        "total": total,
        "has_next": has_next,
        "next_cursor": next_cursor,
    }


def _normalize_limit(raw_limit: int | None) -> int:
    if raw_limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(raw_limit, MAX_PAGE_SIZE))


def _encode_cursor(values: Sequence[Any]) -> str:
    payload = json.dumps({"values": list(values)}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> list[Any] | None:
    if not cursor:
        return None

    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(f"{cursor}{padding}".encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeError):
        return None

    if not isinstance(payload, dict):
        return None
    values = payload.get("values")
    return values if isinstance(values, list) else None


def _cursor_value_matches_column(column: Any, value: Any) -> bool:
    """Return whether a decoded JSON cursor value is safe for a SQL comparison."""

    if value is None or isinstance(value, (list, dict)):
        return False

    try:
        expected_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return isinstance(value, (str, int, float, bool))

    if expected_type is bool:
        return isinstance(value, bool)
    if expected_type is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected_type)
