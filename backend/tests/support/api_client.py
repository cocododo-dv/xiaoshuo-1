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


def key_header(key: str | None) -> dict[str, str] | None:
    """显式幂等键的请求头（同键同载荷即重放）；``None`` 不带，由 AutoKeyTestClient 每次配一个新键。"""
    return None if key is None else {IDEMPOTENCY_HEADER: key}


def create_project(client, *, key: str | None = None, **fields: Any) -> dict:
    """``POST /api/v2/projects`` 建一部作品：``fields`` 原样作请求体，回新作品的 ``project``。"""
    response = client.post("/api/v2/projects", json=fields, headers=key_header(key))
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def validation_issues(response) -> list[dict[str, str]]:
    """一个被请求校验挡下的回包（``REQUEST_VALIDATION_FAILED``）里逐字段的问题清单。"""
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    return payload["error"]["details"]["issues"]


__all__ = [
    "AutoKeyTestClient",
    "IDEMPOTENCY_HEADER",
    "MUTATING_METHODS",
    "create_project",
    "key_header",
    "validation_issues",
    "with_idempotency_key",
]
