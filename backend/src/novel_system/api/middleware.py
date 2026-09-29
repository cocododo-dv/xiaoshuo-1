"""API 外壳的 ASGI 中间件。

``UnhandledErrorMiddleware``（B12-03）：路由抛出的未处理异常以前由 Starlette 最外层的 ``ServerErrorMiddleware``
接住——它在 CORS 与请求编号中间件之外，500 回到浏览器时既没有 ``Access-Control-Allow-Origin`` 也没有
``X-Request-Id``，浏览器只报网络错误，前端显示「连接接口失败，请确认 API 地址…」。这个中间件装在 CORS 里面：
异常在这里变成标准信封的 ``INTERNAL_ERROR``，照常经过 CORS 与请求编号中间件出去（前端读得到错误码与请求编号），
然后照旧往外抛——服务器照常记录，测试客户端照常抛出。响应已经开始发送时不再补发，原样往外抛。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request

from novel_system.api.errors import internal_error_response

AsgiScope = dict[str, Any]
AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


class UnhandledErrorMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], *, expose_error_detail: bool) -> None:
        self.app = app
        self.expose_error_detail = expose_error_detail

    async def __call__(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_tracking_start(message: AsgiMessage) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking_start)
        except Exception as exc:
            if not response_started:
                response = internal_error_response(
                    Request(scope),
                    exc,
                    expose_error_detail=self.expose_error_detail,
                )
                await response(scope, receive, send)
            raise


__all__ = ["UnhandledErrorMiddleware"]
