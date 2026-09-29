from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_fixture_import_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # 旧的维护开关打开也不能让这个入口回来（conftest 以前对每个用例都打开它；开关本身已没有读者）
    monkeypatch.setenv("NOVEL_SYSTEM_ENABLE_FIXTURE_IMPORT", "true")


def test_fixture_import_endpoint_is_gone(client) -> None:
    response = client.post(
        "/api/v1/review-items/import-demo",
        json={"review_id": "must-not-be-created", "item_type": "style_observation", "candidate_text": "fixture"},
    )

    assert response.status_code in (404, 405)
    assert "/api/v1/review-items/import-demo" not in client.get("/openapi.json").json()["paths"]
