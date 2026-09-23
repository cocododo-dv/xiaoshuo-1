"""风格参考路由测试的共用助手(2026-09-15 严格 LLM;2026-09-23 v3 作业表)。

产品路由的导入 / 重新分类必须有 LLM:测试用确定性的假分类器临时顶替路由的运行时客户端,
并等分类作业把书置 ``ready``。v3 起分类作业在**作业开始时**按当前配置取客户端
(``import_job.resolve_classification_client``,不在请求里捕获),所以假客户端要同时顶替路由与作业两处,
并且在作业开始之前一直有效:``import_book`` 在 ``fake_import_llm`` 里面等书就绪;要在用例整个生命周期里
都有效(例如先导入、之后再发重新分类 / 继续分类)用 ``install_fake_classifier(monkeypatch, fake)``。
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


def _default_fake() -> Any:
    from tests.conftest import build_fake_paragraph_classifier

    return build_fake_paragraph_classifier()(rule="default")


@contextmanager
def fake_import_llm(fake: Any | None = None):
    """在 with 块内让风格参考路由与分类作业都把 ``fake`` 当作已启用的运行时 LLM(默认建一个假分类器)。"""
    import novel_system.api.routes.style_reference as sr_routes
    from novel_system.services.style_reference import import_job

    client = fake if fake is not None else _default_fake()
    original_route = sr_routes._get_llm_client_and_enabled
    original_job = import_job.resolve_classification_client
    sr_routes._get_llm_client_and_enabled = lambda: (client, True)
    import_job.resolve_classification_client = lambda: (client, True)
    try:
        yield client
    finally:
        sr_routes._get_llm_client_and_enabled = original_route
        import_job.resolve_classification_client = original_job


def install_fake_classifier(monkeypatch: Any, fake: Any | None = None) -> Any:
    """整个用例里(monkeypatch 撤销前)让路由与分类作业都用 ``fake``;返回它。"""
    import novel_system.api.routes.style_reference as sr_routes
    from novel_system.services.style_reference import import_job

    client = fake if fake is not None else _default_fake()
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (client, True))
    monkeypatch.setattr(import_job, "resolve_classification_client", lambda: (client, True))
    return client


def wait_book_status(
    client: TestClient, book_id: str, *, statuses: tuple[str, ...] = ("ready",), seconds: float = 20.0
) -> dict[str, Any]:
    """轮询到书的状态进入 ``statuses``(分类作业用假分类器只需毫秒)。"""
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


def wait_classification_state(
    client: TestClient, book_id: str, states: tuple[str, ...], *, seconds: float = 20.0
) -> dict[str, Any]:
    """轮询到书最近一个分类作业进入 ``states``(queued / running / succeeded / failed / cancelled)。"""
    deadline = time.monotonic() + seconds
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{PREFIX}/books/{book_id}")
        if resp.status_code == 200:
            payload = resp.json()["data"]["book"].get("classification") or {}
            if payload.get("state") in states:
                return payload
        time.sleep(0.02)
    raise AssertionError(f"classification of {book_id} never reached {states}: last {payload.get('state')!r}")


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
    """经路由导入一本书:假 LLM 顶替路由与分类作业的运行时客户端,默认等分类作业完成(书 ready)。

    ``wait=False`` 时假客户端只在请求期间有效;需要作业也用它时先 ``install_fake_classifier``。
    """
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


__all__ = [
    "PREFIX",
    "SAMPLE_TXT",
    "fake_import_llm",
    "import_book",
    "install_fake_classifier",
    "wait_book_status",
    "wait_classification_state",
]
