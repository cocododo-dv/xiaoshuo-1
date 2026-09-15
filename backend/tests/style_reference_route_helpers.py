"""风格参考路由测试的共用助手(2026-09-15 严格 LLM)。

产品路由的导入 / 重新分类必须有 LLM:测试用确定性的假分类器临时顶替路由的运行时客户端,
并等后台分类任务把书置 ``ready``。
"""

from __future__ import annotations

import io
import json
import time
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

PREFIX = "/api/v2/style-reference"

SAMPLE_TXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

记得那年她还在的时候。

雪花从天空飘落。
""".encode("utf-8")


@contextmanager
def fake_import_llm(fake: Any | None = None):
    """在 with 块内让风格参考路由把 ``fake`` 当作已启用的运行时 LLM(默认建一个假分类器)。"""
    import novel_system.api.routes.style_reference as sr_routes
    from tests.conftest import build_fake_paragraph_classifier

    client = fake if fake is not None else build_fake_paragraph_classifier()(rule="default")
    original = sr_routes._get_llm_client_and_enabled
    sr_routes._get_llm_client_and_enabled = lambda: (client, True)
    try:
        yield client
    finally:
        sr_routes._get_llm_client_and_enabled = original


def wait_book_status(
    client: TestClient, book_id: str, *, statuses: tuple[str, ...] = ("ready",), seconds: float = 20.0
) -> dict[str, Any]:
    """轮询到书的状态进入 ``statuses``(后台分类任务用假分类器只需毫秒)。"""
    deadline = time.monotonic() + seconds
    book: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{PREFIX}/books/{book_id}")
        if resp.status_code == 200:
            book = resp.json()["data"]["book"]
            if book.get("status") in statuses:
                return book
        time.sleep(0.02)
    raise AssertionError(f"book {book_id} never reached {statuses}: last status {book.get('status')!r}")


def import_book(
    client: TestClient,
    *,
    key: str = "imp_1",
    title: str = "测试",
    cloud_policy: str = "segments_only",
    text: bytes = SAMPLE_TXT,
    fake: Any | None = None,
    wait: bool = True,
) -> str:
    """经路由导入一本书:假 LLM 顶替运行时客户端,默认等分类任务完成(书 ready)。"""
    with fake_import_llm(fake):
        data: dict[str, Any] = {"title": title, "cloud_policy": cloud_policy}
        if cloud_policy != "local_only":
            data["rights_declaration"] = json.dumps({"analysis_rights": True, "send_rights": True})
        resp = client.post(
            f"{PREFIX}/books/import-upload",
            files={"file": ("sample.txt", io.BytesIO(text), "text/plain")},
            data=data,
            headers={"X-Idempotency-Key": key},
        )
    assert resp.status_code == 200, resp.text
    book_id = resp.json()["data"]["book"]["book_id"]
    if wait:
        wait_book_status(client, book_id)
    return book_id


__all__ = ["PREFIX", "SAMPLE_TXT", "fake_import_llm", "import_book", "wait_book_status"]
