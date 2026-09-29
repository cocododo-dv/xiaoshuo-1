"""API 的异常处理器：每种错误都以标准信封回给前端，带上请求编号。

四个处理器：领域错误、请求校验、数据库（忙 / 失败）、兜底的未处理异常。路由里抛出的未处理异常在 CORS 里面
就已经由 ``novel_system.api.middleware.UnhandledErrorMiddleware`` 变成了 ``INTERNAL_ERROR`` 信封（B12-03）；
它随后照旧往外抛，Starlette 最外层的 ``ServerErrorMiddleware`` 仍会调兜底处理器，但响应已经发出，不再发第二份，
日志也只记一次。兜底处理器自己发出的信封只在那个中间件外面（请求编号中间件、CORS 本身）出错时才轮到。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from novel_system.api.deps import request_id_of
from novel_system.api.response import error
from novel_system.services.database_errors import is_database_busy_error
from novel_system.services.errors import DomainError

logger = logging.getLogger(__name__)

# ``request.state`` 上记下已经记过日志的那个异常：它在 CORS 里面变成信封后照旧往外抛，外层兜底处理器再见到
# 同一个异常时不再记第二遍
_LOGGED_EXCEPTION_STATE = "unhandled_error_logged"

# 不回显 Pydantic 的 ``input``：它可能是整场正文或密钥。字段路径与稳定的错误类型足够客户端改正请求。
_VALIDATION_MESSAGES = {
    "extra_forbidden": "unexpected field",
    "field_required": "required field is missing",
    "int_type": "value must be an integer",
    "list_type": "value must be a list",
    "string_type": "value must be a string",
    "string_too_long": "string exceeds the allowed length",
    "string_too_short": "string is shorter than the allowed length",
    "too_long": "collection exceeds the allowed length",
    "greater_than_equal": "value is below the allowed minimum",
    "less_than_equal": "value exceeds the allowed maximum",
}


def internal_error_response(request: Request, exc: Exception, *, expose_error_detail: bool) -> JSONResponse:
    """未处理异常的标准信封（500 ``INTERNAL_ERROR``）；同一个异常只记一次日志。"""

    req_id = request_id_of(request)
    if getattr(request.state, _LOGGED_EXCEPTION_STATE, None) is not exc:
        setattr(request.state, _LOGGED_EXCEPTION_STATE, exc)
        logger.error("Unhandled API error request_id=%s", req_id, exc_info=exc)
    return error(
        "INTERNAL_ERROR",
        str(exc) if expose_error_detail else "internal server error",
        status_code=500,
        details={"retryable": False},
        req_id=req_id,
    )


def install_exception_handlers(app: FastAPI, *, expose_error_detail: bool) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return error(
            exc.code,
            exc.message,
            status_code=exc.status_code,
            details=exc.details,
            req_id=request_id_of(request),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        issues = []
        for item in exc.errors()[:32]:
            issue_type = str(item.get("type") or "validation_error")
            issues.append(
                {
                    "field": ".".join(str(part) for part in item.get("loc", ())),
                    "type": issue_type,
                    "message": _VALIDATION_MESSAGES.get(issue_type, "invalid value"),
                }
            )
        return error(
            "REQUEST_VALIDATION_FAILED",
            "request validation failed",
            status_code=422,
            details={
                "issues": issues,
                "issue_count": len(exc.errors()),
                "truncated": len(exc.errors()) > len(issues),
            },
            req_id=request_id_of(request),
        )

    @app.exception_handler(OperationalError)
    async def operational_error_handler(request: Request, exc: OperationalError):
        if is_database_busy_error(exc):
            return error(
                "DATABASE_BUSY",
                "database is busy; retry after the current long-running operation finishes",
                status_code=503,
                details={"retryable": True},
                req_id=request_id_of(request),
            )
        return error(
            "DATABASE_OPERATION_FAILED",
            "database operation failed",
            status_code=500,
            details={"retryable": False},
            req_id=request_id_of(request),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        return internal_error_response(request, exc, expose_error_detail=expose_error_detail)


__all__ = ["install_exception_handlers", "internal_error_response"]
