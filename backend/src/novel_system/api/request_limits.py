from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from novel_system.api.error_catalog import localized_message
from novel_system.api.response import error


AsgiScope = dict[str, Any]
AsgiMessage = dict[str, Any]
AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
AsgiSend = Callable[[AsgiMessage], Awaitable[None]]


class RequestBodyLimitMiddleware:
    """Bound every HTTP body, including streams without ``Content-Length``.

    FastAPI validates a body only after Starlette has read it.  Checking only
    the header therefore leaves chunked requests unbounded.  This middleware
    buffers at most the configured limit, then replays the body to the normal
    parser.  The service is a single-author desktop API and already parses its
    JSON/multipart inputs in memory, so one bounded buffer is an acceptable and
    predictable trade-off.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: AsgiScope,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value.strip()
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            if len(set(content_lengths)) != 1:
                await self._reject(
                    scope,
                    receive,
                    send,
                    code="INVALID_CONTENT_LENGTH",
                    message="conflicting Content-Length headers",
                    status_code=400,
                )
                return
            try:
                declared_length = int(content_lengths[0])
            except (TypeError, ValueError):
                declared_length = -1
            if declared_length < 0:
                await self._reject(
                    scope,
                    receive,
                    send,
                    code="INVALID_CONTENT_LENGTH",
                    message="Content-Length must be a non-negative integer",
                    status_code=400,
                )
                return
            if declared_length > self.max_bytes:
                await self._too_large(scope, receive, send)
                return
            if declared_length == 0:
                await self.app(scope, receive, send)
                return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                disconnected = True
                break
            if message_type != "http.request":
                continue
            chunk = message.get("body", b"")
            if chunk:
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    await self._too_large(scope, receive, send)
                    return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> AsgiMessage:
            nonlocal replayed
            if not replayed:
                replayed = True
                if disconnected:
                    return {"type": "http.disconnect"}
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    async def _too_large(
        self,
        scope: AsgiScope,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        await self._reject(
            scope,
            receive,
            send,
            code="REQUEST_BODY_TOO_LARGE",
            message="request body exceeds the configured size limit",
            status_code=413,
            details={"max_bytes": self.max_bytes},
        )

    @staticmethod
    async def _reject(
        scope: AsgiScope,
        receive: AsgiReceive,
        send: AsgiSend,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        request_id = (scope.get("state") or {}).get("request_id")
        response = error(
            code,
            localized_message(code, message)[0],
            status_code=status_code,
            details=details,
            req_id=request_id,
        )
        await response(scope, receive, send)
