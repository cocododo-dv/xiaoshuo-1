"""PR-7 validate / reports endpoint 黑盒测试。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository

PREFIX = "/api/v2/style-reference"


def _seed_profile(seed: str) -> str:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = f"sr_book_{seed}"
        run_id = f"sr_run_{seed}"
        profile_id = f"sr_profile_{seed}"
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_quote(
            quote_id=f"sr_q_{seed}",
            book_id=book_id, paragraph_id=None,
            span_start=0, span_end=10, quote_text="他低头看着脚下",
            illustrates_dims=[], extracted_features={},
        )
        repo.create_profile(
            profile_id=profile_id, book_id=book_id, run_id=run_id, title="t",
            status="active",
            profile_json={
                "narrative_summary": "短句",
                "metrics_baseline": {
                    "avg_sentence_length": {"mean": 10.0, "std": 3.0},
                },
                "style_features": ["短句"],
            },
            coverage_json={},
            source_finding_ids_json=[],
        )
        session.commit()
    return profile_id


def test_validate_endpoint_sync_only_happy(client: TestClient, monkeypatch) -> None:
    """sync_only 应立返 sync_result 且 polling_url=None。"""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    profile_id = _seed_profile("vsync")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/validate",
        json={"generated_text": "随便一段生成文本", "mode": "sync_only"},
        headers={"X-Idempotency-Key": "validate_sync_1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["mode_executed"] == "sync_only"
    assert data["sync_result"] is not None
    assert data["polling_url"] is None
    assert data["report_id"].startswith("sr_rep_")


def test_validate_endpoint_async_returns_polling_url(
    client: TestClient, monkeypatch, fake_validation_llm
) -> None:
    """async_full 应立返 polling_url + sync_result=None。2026-09-15 严格 LLM:全量三路必须有
    LLM,「仅本机」的书要求本地模型——测试给一个假 critic 并把运行时模型标成本地。"""
    import novel_system.api.routes.style_reference as sr_routes
    from novel_system.services.style_reference import policy as policy_module

    fake = fake_validation_llm("with_quote")
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: True)
    profile_id = _seed_profile("vasync")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/validate",
        json={"generated_text": "一段中文文本", "mode": "async_full"},
        headers={"X-Idempotency-Key": "validate_async_1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["mode_executed"] == "async_full"
    assert data["sync_result"] is None
    assert data["polling_url"]
    assert data["polling_url"].endswith(data["report_id"])


def test_async_validation_dispatches_only_after_idempotency_commit(
    client: TestClient, monkeypatch, fake_validation_llm
) -> None:
    from novel_system.db.models import IdempotencyKey, StyleReferenceValidationReport
    import novel_system.api.routes.style_reference as sr_routes
    from novel_system.services.style_reference import policy as policy_module

    # 严格 LLM:async_full 先要有 LLM(这里派发被观察函数顶替,worker 不会真跑)
    fake = fake_validation_llm("with_quote")
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: True)
    observations: list[tuple[str | None, str | None]] = []

    def observe_dispatch(**kwargs) -> None:  # noqa: ANN003
        with SessionLocal() as observer:
            idem = observer.get(IdempotencyKey, "validate_after_commit")
            report = observer.get(StyleReferenceValidationReport, kwargs["report_id"])
            observations.append(
                (
                    idem.status if idem is not None else None,
                    report.status if report is not None else None,
                )
            )

    monkeypatch.setattr(
        sr_routes.profiles,
        "start_style_reference_validation_worker",
        observe_dispatch,
    )
    profile_id = _seed_profile("validate_after_commit")
    request_kwargs = {
        "json": {"generated_text": "一段待验证文本", "mode": "async_full"},
        "headers": {"X-Idempotency-Key": "validate_after_commit"},
    }
    first = client.post(f"{PREFIX}/profiles/{profile_id}/validate", **request_kwargs)
    replay = client.post(f"{PREFIX}/profiles/{profile_id}/validate", **request_kwargs)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Idempotency-Status"] == "replayed"
    assert observations == [("succeeded", "queued"), ("succeeded", "queued")]


def test_validate_endpoint_profile_not_found(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    resp = client.post(
        f"{PREFIX}/profiles/sr_profile_nonexistent/validate",
        json={"generated_text": "x", "mode": "sync_only"},
        headers={"X-Idempotency-Key": "validate_404"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_PROFILE_NOT_FOUND"


def test_validate_endpoint_invalid_mode(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    profile_id = _seed_profile("vinvalid")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/validate",
        json={"generated_text": "x", "mode": "yolo_mode"},
        headers={"X-Idempotency-Key": "validate_bad_mode"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_VALIDATE_PARAM_INVALID"


def test_get_report_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/reports/sr_rep_nonexistent")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_REPORT_NOT_FOUND"


def test_get_report_after_sync_validate(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    profile_id = _seed_profile("getrep")
    post_resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/validate",
        json={"generated_text": "x", "mode": "sync_only"},
        headers={"X-Idempotency-Key": "getrep_1"},
    )
    report_id = post_resp.json()["data"]["report_id"]
    get_resp = client.get(f"{PREFIX}/reports/{report_id}")
    assert get_resp.status_code == 200
    report = get_resp.json()["data"]["report"]
    assert report["report_id"] == report_id
    assert report["mode_executed"] == "sync_only"
    assert report["verdict"]  # 非空


def test_list_reports_by_profile(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    profile_id = _seed_profile("listrep")
    # 触发 2 个 sync_only validate
    for i in range(2):
        client.post(
            f"{PREFIX}/profiles/{profile_id}/validate",
            json={"generated_text": f"text {i}", "mode": "sync_only"},
            headers={"X-Idempotency-Key": f"listrep_{i}"},
        )
    resp = client.get(f"{PREFIX}/profiles/{profile_id}/reports")
    assert resp.status_code == 200
    reports = resp.json()["data"]["reports"]
    assert len(reports) == 2
