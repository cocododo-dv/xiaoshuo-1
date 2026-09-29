from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from novel_system.db.session import get_db


def get_session(session: Session = Depends(get_db)) -> Session:
    return session


def request_id_of(request: Request) -> str | None:
    """The per-request id the middleware stamps (``None`` outside the middleware)."""
    return getattr(request.state, "request_id", None)


def actor_ref_of(request: Request) -> str:
    """The operator reference for audit trails (``"operator"`` when none was stamped)."""
    return getattr(request.state, "operator_ref", None) or "operator"
