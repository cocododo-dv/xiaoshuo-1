"""Shared HTTP mutation boundary.

Legacy routes historically committed directly and therefore ignored the
``X-Idempotency-Key`` already sent by the maintained clients.  This helper keeps
unkeyed callers backward-compatible while giving keyed calls durable
claim/replay/conflict semantics and one transaction owner.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from novel_system.api.response import ok
from novel_system.services.errors import DomainError
from novel_system.services.idempotency import (
    execute_with_idempotency,
    execute_with_optional_idempotency,
)
from novel_system.api.deps import request_id_of, actor_ref_of


def idempotent_response(
    request: Request,
    session: Session,
    *,
    method: str,
    path_template: str,
    payload: Any,
    action: Callable[..., dict],
    after_commit: Callable[[dict], None] | None = None,
    owned_failure_callback: Callable[[DomainError], None] | None = None,
) -> JSONResponse:
    """Execute and commit one mutation that *requires* an idempotency key.

    A missing ``X-Idempotency-Key`` still fails with the 400
    ``IDEMPOTENCY_KEY_REQUIRED`` raised by ``execute_with_idempotency``.
    """

    actor_ref = actor_ref_of(request)
    result, status = execute_with_idempotency(
        session,
        idempotency_key=request.headers.get("X-Idempotency-Key"),
        method=method,
        path_template=path_template,
        payload=payload,
        action=action,
        after_commit=after_commit,
        owned_failure_callback=owned_failure_callback,
        actor_ref=actor_ref,
    )
    headers = {"X-Idempotency-Status": status} if status else {}
    return ok(
        result,
        req_id=request_id_of(request),
        headers=headers,
    )


def optional_idempotent_response(
    request: Request,
    session: Session,
    *,
    method: str,
    path_template: str,
    payload: Any,
    action: Callable[..., dict],
) -> JSONResponse:
    """Execute and commit one mutation, replaying it when a key is supplied."""

    actor_ref = actor_ref_of(request)
    result, status = execute_with_optional_idempotency(
        session,
        idempotency_key=request.headers.get("X-Idempotency-Key"),
        method=method,
        path_template=path_template,
        payload=payload,
        action=action,
        actor_ref=actor_ref,
    )
    headers = {"X-Idempotency-Status": status} if status else {}
    return ok(
        result,
        req_id=request_id_of(request),
        headers=headers,
    )
