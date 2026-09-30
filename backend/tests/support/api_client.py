"""测试用 API 客户端：写请求没带 ``X-Idempotency-Key`` 时自动配一个新键。

产品里每个写接口都要键（``api.mutations.mutate``，缺了 400 ``IDEMPOTENCY_KEY_REQUIRED``），React 客户端与冒烟
脚本每个写请求也都带键；测试里的写请求大多不关心幂等，就由这个客户端每次配一个新键——所以同一个请求发两次
仍是两次独立执行，与以前「不带键执行一次」一样。要测重放就照旧显式带同一个键；要测「缺键 400」用不配键的
原生客户端（conftest 的 ``raw_client``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from starlette.testclient import TestClient

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _header_items(headers: Any) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, Mapping) or hasattr(headers, "items"):
        return list(headers.items())
    return list(headers)


def with_idempotency_key(headers: Any) -> Any:
    """``headers`` 里没有幂等键就补一个 ``test-<uuid>``；已有（哪怕是空串）原样返回。"""

    items = _header_items(headers)
    if any(str(name).lower() == IDEMPOTENCY_HEADER.lower() for name, _value in items):
        return headers
    key = f"test-{uuid4().hex}"
    if headers is None or isinstance(headers, Mapping):
        return {**dict(headers or {}), IDEMPOTENCY_HEADER: key}
    return [*items, (IDEMPOTENCY_HEADER, key)]


class AutoKeyTestClient(TestClient):
    def request(self, method: str, url: Any, *, headers: Any = None, **kwargs: Any):  # type: ignore[override]
        if str(method).upper() in MUTATING_METHODS:
            headers = with_idempotency_key(headers)
        return super().request(method, url, headers=headers, **kwargs)


__all__ = ["AutoKeyTestClient", "IDEMPOTENCY_HEADER", "MUTATING_METHODS", "with_idempotency_key"]
