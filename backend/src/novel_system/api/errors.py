"""API 的异常处理器：每种错误都以标准信封回给前端，带上请求编号。

说明文字按 ``api/error_catalog.py`` 换成作者看的中文（B12-04，批准 #27）：原说明已经是中文的原样保留；被换掉的
英文原文（未处理异常则是异常文字）只在 ``expose_error_detail`` 打开时放进 ``details.debug_message``。

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
from novel_system.api.error_catalog import localized_message
from novel_system.api.response import error
from novel_system.services.database_errors import is_database_busy_error
from novel_system.services.errors import DomainError

logger = logging.getLogger(__name__)

# ``request.state`` 上记下已经记过日志的那个异常：它在 CORS 里面变成信封后照旧往外抛，外层兜底处理器再见到
# 同一个异常时不再记第二遍
_LOGGED_EXCEPTION_STATE = "unhandled_error_logged"

# 不回显 Pydantic 的 ``input``：它可能是整场正文或密钥。字段路径与稳定的错误类型足够客户端改正请求。
_VALIDATION_MESSAGES = {
    "extra_forbidden": "不认识的字段",
    "field_required": "缺少必填字段",
    "int_type": "要填整数",
    "list_type": "要填列表",
    "string_type": "要填文字",
    "string_too_long": "文字太长",
    "string_too_short": "文字太短",
    "too_long": "条目太多",
    "greater_than_equal": "小于允许的最小值",
    "less_than_equal": "超过允许的最大值",
}
_VALIDATION_FALLBACK_MESSAGE = "取值不合法"


def _envelope(
    request: Request,
    code: str,
    message: str,
    *,
    status_code: int,
    details: dict | None,
    expose_error_detail: bool,
    debug_message: str | None = None,
) -> JSONResponse:
    """错误信封；说明文字按错误码换成中文，被换掉的原文只在开了 ``expose_error_detail`` 时随 ``details`` 带回。"""

    text, replaced = localized_message(code, message)
    payload = dict(details or {})
    original = debug_message if debug_message is not None else (message if replaced else None)
    if expose_error_detail and original:
        payload["debug_message"] = original
    return error(code, text, status_code=status_code, details=payload, req_id=request_id_of(request))


def internal_error_response(request: Request, exc: Exception, *, expose_error_detail: bool) -> JSONResponse:
    """未处理异常的标准信封（500 ``INTERNAL_ERROR``）；同一个异常只记一次日志。"""

    req_id = request_id_of(request)
    if getattr(request.state, _LOGGED_EXCEPTION_STATE, None) is not exc:
        setattr(request.state, _LOGGED_EXCEPTION_STATE, exc)
        logger.error("Unhandled API error request_id=%s", req_id, exc_info=exc)
    # 异常文字可能带密钥、SQL 或正文片段：只在显式打开 expose_error_detail 时随 details 带回
    return _envelope(
        request,
        "INTERNAL_ERROR",
        "internal server error",
        status_code=500,
        details={"retryable": False},
        expose_error_detail=expose_error_detail,
        debug_message=str(exc),
    )


def install_exception_handlers(app: FastAPI, *, expose_error_detail: bool) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return _envelope(
            request,
            exc.code,
            exc.message,
            status_code=exc.status_code,
            details=exc.details,
            expose_error_detail=expose_error_detail,
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
                    "message": _VALIDATION_MESSAGES.get(issue_type, _VALIDATION_FALLBACK_MESSAGE),
                }
            )
        return _envelope(
            request,
            "REQUEST_VALIDATION_FAILED",
            "request validation failed",
            status_code=422,
            details={
                "issues": issues,
                "issue_count": len(exc.errors()),
                "truncated": len(exc.errors()) > len(issues),
            },
            expose_error_detail=False,
        )

    @app.exception_handler(OperationalError)
    async def operational_error_handler(request: Request, exc: OperationalError):
        if is_database_busy_error(exc):
            return _envelope(
                request,
                "DATABASE_BUSY",
                "database is busy; retry after the current long-running operation finishes",
                status_code=503,
                details={"retryable": True},
                expose_error_detail=False,
            )
        return _envelope(
            request,
            "DATABASE_OPERATION_FAILED",
            "database operation failed",
            status_code=500,
            details={"retryable": False},
            expose_error_detail=False,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        return internal_error_response(request, exc, expose_error_detail=expose_error_detail)


__all__ = ["install_exception_handlers", "internal_error_response"]
