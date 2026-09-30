from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from novel_system.api.deps import request_id_of


def request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def ok(data: dict, *, req_id: str | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "data": data, "error": None, "request_id": req_id or request_id()},
        headers=headers or {},
    )


def respond(request: Request, data: dict, *, headers: dict[str, str] | None = None) -> JSONResponse:
    """路由的成功信封：``request_id`` 取中间件给这次请求记下的编号（与 ``X-Request-Id`` 响应头一致）。"""
    return ok(data, req_id=request_id_of(request), headers=headers)


def error(code: str, message: str, *, status_code: int, details: dict | None = None, req_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "data": None,
            "error": {"code": code, "message": message, "details": details or {}},
            "request_id": req_id or request_id(),
        },
        status_code=status_code,
    )
