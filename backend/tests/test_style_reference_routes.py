"""Style Reference 18 端点黑盒测试(PR-4)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novel_system.api.app import create_app
from novel_system.db.models import StyleReferenceProfile
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository


SAMPLE_TXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

记得那年她还在的时候。

雪花从天空飘落。
""".encode("utf-8")


PREFIX = "/api/v2/style-reference"
from tests.style_reference_route_helpers import (  # noqa: E402
    fake_import_llm,
    import_book,
    install_fake_classifier,
    wait_book_status,
)


def test_legacy_reference_books_routes_are_never_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_ENABLE_LEGACY_REFERENCE_BOOKS", "true")
    with TestClient(create_app()) as client:
        paths = {getattr(route, "path", "") for route in client.app.routes}

    assert "/api/v1/reference-books" not in paths
    assert not any(path.startswith("/api/v1/reference-books/") for path in paths)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_book(client: TestClient, fake: Any | None = None) -> str:
    """2026-09-15 严格 LLM:导入必须有 LLM——用假分类器顶替运行时客户端,并等后台分类完成。"""
    return import_book(client, text=SAMPLE_TXT, fake=fake)


def test_import_upload_rejects_malformed_rights_json(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("sample.txt", io.BytesIO(SAMPLE_TXT), "text/plain")},
        data={
            "title": "invalid rights",
            "cloud_policy": "local_only",
            "rights_declaration": "{not-json}",
        },
        headers={"X-Idempotency-Key": "invalid-rights-json"},
    )

    assert response.status_code == 400
    assert (
        response.json()["error"]["code"]
        == "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID"
    )


def _seed_full_chain(book_id: str) -> tuple[str, str, str]:
    """直接用 service 层快速建 run + finding(含 2 evidence)+ profile,绕过 LLM 调用。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        run_id = f"sr_run_route_{book_id[-6:]}"
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        extraction_id = f"sr_ext_route_{book_id[-6:]}"
        repo.create_extraction(
            extraction_id=extraction_id,
            book_id=book_id,
            run_id=run_id,
            layer="language",
            sub_dimension="language.rhetoric",
            raw_payload_json={},
            status="done",
            validation_errors_json=[],
            purpose="extract",
        )
        finding_id = f"sr_find_route_{book_id[-6:]}"
        repo.create_finding(
            finding_id=finding_id,
            book_id=book_id,
            run_id=run_id,
            extraction_id=extraction_id,
            sub_dimension="language.rhetoric",
            finding_kind="observation",
            statement="测试 observation 描述",
            confidence="high",
            status="pending",
        )
        # 2 evidence(≥2 强约束):1 条真实段落引文 + 1 条合成反例
        paragraphs = repo.list_paragraphs(book_id)
        repo.create_quote(
            quote_id=f"sr_quote_route_a_{book_id[-6:]}",
            book_id=book_id,
            paragraph_id=paragraphs[0].paragraph_id if paragraphs else None,
            span_start=0,
            span_end=10,
            quote_text="真实段落引文文本",
            illustrates_dims=["language.rhetoric"],
            extracted_features={},
        )
        repo.create_quote(
            quote_id=f"sr_quote_route_b_{book_id[-6:]}",
            book_id=book_id,
            paragraph_id=None,
            span_start=0,
            span_end=8,
            quote_text="合成反例文本",
            illustrates_dims=["language.rhetoric"],
            extracted_features={},
        )
        repo.create_evidence(
            evidence_id=f"sr_ev_route_a_{book_id[-6:]}",
            finding_id=finding_id,
            quote_id=f"sr_quote_route_a_{book_id[-6:]}",
            anchor_kind="paragraph_quote",
            is_synthetic=0,
        )
        repo.create_evidence(
            evidence_id=f"sr_ev_route_b_{book_id[-6:]}",
            finding_id=finding_id,
            quote_id=f"sr_quote_route_b_{book_id[-6:]}",
            anchor_kind="counter_example",
            is_synthetic=1,
        )
        profile_id = f"sr_profile_route_{book_id[-6:]}"
        repo.create_profile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="测试 profile",
            status="draft",
            profile_json={
                "narrative_summary": "ns",
                "scene_samples_index": {},
                "calibration_guidance": ["calib A"],
            },
            coverage_json={},
            source_finding_ids_json=[finding_id],
        )
        session.commit()
    return run_id, finding_id, profile_id


# ---------------------------------------------------------------------------
# Books endpoints
# ---------------------------------------------------------------------------


def test_import_upload_segments_only_requires_rights_declaration(
    client: TestClient,
) -> None:
    files = {"file": ("undeclared.txt", io.BytesIO(SAMPLE_TXT), "text/plain")}
    with fake_import_llm():
        resp = client.post(
            f"{PREFIX}/books/import-upload",
            files=files,
            data={"title": "未声明", "cloud_policy": "segments_only"},
            headers={"X-Idempotency-Key": "imp_undeclared"},
        )
    assert resp.status_code == 400
    assert (
        resp.json()["error"]["code"]
        == "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED"
    )


def test_import_upload_happy(client: TestClient) -> None:
    book_id = _import_book(client)
    assert book_id.startswith("sr_book_")


def test_import_upload_idempotency_replay(client: TestClient) -> None:
    files = {"file": ("a.txt", io.BytesIO(SAMPLE_TXT), "text/plain")}
    headers = {"X-Idempotency-Key": "imp_dup"}
    data = {
        "title": "x",
        "cloud_policy": "segments_only",
        "rights_declaration": json.dumps(
            {"analysis_rights": True, "send_rights": True}
        ),
    }
    with fake_import_llm():
        r1 = client.post(
            f"{PREFIX}/books/import-upload", files=files, data=data, headers=headers
        )
        files2 = {"file": ("a.txt", io.BytesIO(SAMPLE_TXT), "text/plain")}
        r2 = client.post(
            f"{PREFIX}/books/import-upload", files=files2, data=data, headers=headers
        )
        # 重放不会再建一个分类作业(同一个作业;重复派发由认领的条件写挡住),书最终 ready
        wait_book_status(client, r1.json()["data"]["book"]["book_id"])
    assert r1.status_code == 200
    assert r2.status_code == 200
    # idempotency replay 应返回 X-Idempotency-Status="replayed"(或 "stored" 首次)
    assert "X-Idempotency-Status" in r2.headers
    assert r2.json()["data"]["job_id"] == r1.json()["data"]["job_id"]
    with SessionLocal() as session:
        from novel_system.db.models import StyleReferenceJob

        assert session.query(StyleReferenceJob).count() == 1


def test_list_books(client: TestClient) -> None:
    _import_book(client)
    resp = client.get(f"{PREFIX}/books")
    assert resp.status_code == 200
    assert len(resp.json()["data"]["books"]) >= 1


def test_get_book_happy(client: TestClient) -> None:
    book_id = _import_book(client)
    resp = client.get(f"{PREFIX}/books/{book_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["book"]["book_id"] == book_id


def test_get_book_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/books/sr_book_nonexistent")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_NOT_FOUND"


def test_delete_book(client: TestClient) -> None:
    book_id = _import_book(client)
    resp = client.delete(
        f"{PREFIX}/books/{book_id}", headers={"X-Idempotency-Key": "del_1"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    resp2 = client.get(f"{PREFIX}/books/{book_id}")
    assert resp2.status_code == 404


def test_delete_book_purges_entire_derived_chain(client: TestClient) -> None:
    """删书路由必须级联清除「全部」派生数据,不留孤儿。

    `_seed_full_chain` 覆盖 run/extraction/finding/2 quotes/2 evidences/profile;
    本测试再补 binding / validation_report / banned_term / finding_feedback 四条
    `purge_derived_data` 分支,删后逐表断言对该 book/profile/finding 零残留。
    防止某条 delete 分支被悄悄删掉而 `test_delete_book`(无派生数据)仍通过。
    """
    from novel_system.db.models import (
        StyleReferenceBannedTerm,
        StyleReferenceEvidence,
        StyleReferenceExtraction,
        StyleReferenceFinding,
        StyleReferenceFindingFeedback,
        StyleReferenceInjectionBinding,
        StyleReferenceParagraph,
        StyleReferenceProfile,
        StyleReferenceQuote,
        StyleReferenceRun,
        StyleReferenceValidationReport,
    )
    from novel_system.services.style_reference.finding_feedback import apply_feedback

    book_id = _import_book(client)
    run_id, finding_id, profile_id = _seed_full_chain(book_id)
    suffix = book_id[-6:]

    # 追加 _seed_full_chain 未覆盖的 profile / finding 级派生
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id=f"sr_bind_del_{suffix}",
            profile_id=profile_id,
            scope="project",
            scope_ref_id="proj_del",
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        repo.create_validation_report(
            report_id=f"sr_rep_del_{suffix}",
            profile_id=profile_id,
            target_kind="manual",
            target_ref_id=None,
            verdict="pass",
            quantitative_json=[],
            semantic_json=[],
            plagiarism_json={},
            forbidden_hits_json=[],
            mode_executed="async_full",
        )
        repo.create_banned_term(
            term_id=f"sr_term_del_{suffix}",
            profile_id=profile_id,
            term="禁词",
            replacement_hint=None,
            source="user",
            scope="generation",
        )
        apply_feedback(session, finding_id, operator_ref="u1", vote="up")
        session.commit()

    # 删前确认四类派生确有数据(否则后面的「零残留」断言会失去意义)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        assert repo.list_finding_feedback(finding_id)
        assert repo.list_bindings(profile_id=profile_id)
        assert repo.list_validation_reports(profile_id=profile_id)
        assert repo.list_banned_terms(profile_id)

    resp = client.delete(
        f"{PREFIX}/books/{book_id}",
        headers={"X-Idempotency-Key": f"del_chain_{suffix}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] is True
    assert client.get(f"{PREFIX}/books/{book_id}").status_code == 404

    # 全部派生表对该 book / profile / finding 零残留
    with SessionLocal() as session:
        def _count(model, column, value) -> int:
            return session.query(model).filter(column == value).count()

        assert _count(StyleReferenceParagraph, StyleReferenceParagraph.book_id, book_id) == 0
        assert _count(StyleReferenceRun, StyleReferenceRun.book_id, book_id) == 0
        assert _count(StyleReferenceExtraction, StyleReferenceExtraction.book_id, book_id) == 0
        assert _count(StyleReferenceQuote, StyleReferenceQuote.book_id, book_id) == 0
        assert _count(StyleReferenceFinding, StyleReferenceFinding.book_id, book_id) == 0
        assert _count(StyleReferenceProfile, StyleReferenceProfile.book_id, book_id) == 0
        assert _count(StyleReferenceEvidence, StyleReferenceEvidence.finding_id, finding_id) == 0
        assert _count(StyleReferenceFindingFeedback, StyleReferenceFindingFeedback.finding_id, finding_id) == 0
        assert _count(StyleReferenceInjectionBinding, StyleReferenceInjectionBinding.profile_id, profile_id) == 0
        assert _count(StyleReferenceValidationReport, StyleReferenceValidationReport.profile_id, profile_id) == 0
        assert _count(StyleReferenceBannedTerm, StyleReferenceBannedTerm.profile_id, profile_id) == 0


def test_reclassify_llm_required_when_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    book_id = _import_book(client)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        headers={"X-Idempotency-Key": "rec_disabled"},
    )
    # 2026-09-15 严格 LLM:没有 LLM 就 409 + author_action,绝不启发式兜底
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]


def test_reclassify_executes_and_purges_derived_data(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    """PR-23 — reclassify 真实执行:旧 run/finding/profile 消失,paragraphs 仍在,
    stats_json 回写 paragraph_type_distribution / classifier_calibration。"""
    fake = install_fake_classifier(monkeypatch, fake_paragraph_classifier(rule="default"))
    book_id = _import_book(client, fake)
    run_id, finding_id, profile_id = _seed_full_chain(book_id)

    # 破坏式重新分类:请求里清派生数据、书置 ingesting,分类作业逐批分类
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        headers={"X-Idempotency-Key": "rec_real"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "classifying"
    assert data["book"]["status"] == "ingesting"
    assert data["paragraphs_count"] >= 1
    assert data["classification"]["kind"] == "reclassify"
    assert data["classification"]["mode"] == "reclassify" and data["mode"] == "reclassify"

    # 派生数据全部消失
    assert client.get(f"{PREFIX}/runs/{run_id}").status_code == 404
    assert client.get(f"{PREFIX}/profiles/{profile_id}").status_code == 404
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        assert repo.list_findings(book_id=book_id) == []
        assert repo.get_finding(finding_id) is None
        # paragraphs 与 book 保留
        assert len(repo.list_paragraphs(book_id)) == data["paragraphs_count"]

    book = wait_book_status(client, book_id)
    assert book["stats_json"]["paragraph_type_distribution"]
    assert book["stats_json"]["classifier_calibration"]["fallback_to_heuristic"] is False
    assert book["classification"]["state"] == "succeeded"
    assert book["classification"]["batches_done"] == book["classification"]["batches_total"] >= 1
    assert book["paragraph_types_revision"] == 2  # 导入一次 + 重新分类一次


# ---------------------------------------------------------------------------
# Runs endpoints
# ---------------------------------------------------------------------------


def test_start_run_llm_required_when_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    book_id = _import_book(client)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/runs",
        json={},
        headers={"X-Idempotency-Key": "run_disabled"},
    )
    # LLM 未启用 → LLMRequiredError(DomainError, status_code=409)→ 精确契约:
    # 409 + 错误码 + author_action(镜像 test_style_reference_hardening::
    # test_llm_required_maps_to_409_with_author_action)。弱断言 >= 400 会放过
    # 「丢掉 DomainError 基类 → 退回通用 500、丢失 author_action 导航」的回归。
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]


def test_get_run_happy(client: TestClient) -> None:
    book_id = _import_book(client)
    run_id, _, _ = _seed_full_chain(book_id)
    resp = client.get(f"{PREFIX}/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["run"]["run_id"] == run_id


def test_get_run_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/runs/sr_run_nonexistent")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_RUN_NOT_FOUND"


def test_cancel_run(client: TestClient) -> None:
    """只有 pending/running 的 run 能取消；已完成（done/failed）的 run 是终态，取消要 409。"""
    book_id = _import_book(client)
    done_run_id, _, _ = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/runs/{done_run_id}/cancel", headers={"X-Idempotency-Key": "cancel_done_1"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_RUN_CANCEL_CONFLICT"
    with SessionLocal() as session:
        assert StyleReferenceRepository(session).get_run(done_run_id).status == "done"

    pending_run_id = f"sr_run_pending_{book_id[-6:]}"
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_run(
            run_id=pending_run_id, book_id=book_id, status="pending", phase="extract"
        )
        session.commit()
    resp = client.post(
        f"{PREFIX}/runs/{pending_run_id}/cancel", headers={"X-Idempotency-Key": "cancel_pending_1"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"


def test_list_run_findings(client: TestClient) -> None:
    book_id = _import_book(client)
    run_id, _, _ = _seed_full_chain(book_id)
    resp = client.get(f"{PREFIX}/runs/{run_id}/findings")
    assert resp.status_code == 200
    findings = resp.json()["data"]["findings"]
    assert len(findings) == 1
    # PR-23 — 不带 include 时响应里没有 evidence 键(零回归)
    assert "evidence" not in findings[0]


def test_start_run_defaults_to_all_four_layers(
    client: TestClient, monkeypatch, fake_extractor_llm
) -> None:
    """PR-23 — POST runs 不带 layers → 全 4 层 + 16 sub_dim_results。"""
    import novel_system.api.routes.style_reference as sr_routes

    fake = fake_extractor_llm("default")
    monkeypatch.setattr(
        sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True)
    )
    book_id = _import_book(client)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/runs",
        # 测试书仅几十字(input_assessment 全 skip),force 绕过 §6.4 输入量门槛,
        # 本用例只锁定「不带 layers → 默认全 4 层」的契约
        json={"force": True},
        headers={"X-Idempotency-Key": "run_default_layers"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["layers"] == ["language", "narrative", "scene", "theme"]
    assert len(data["sub_dim_results"]) == 16


def test_background_run_dispatches_only_after_idempotency_commit(
    client: TestClient, monkeypatch, fake_extractor_llm
) -> None:
    from novel_system.db.models import IdempotencyKey, StyleReferenceRun
    import novel_system.api.routes.style_reference as sr_routes

    fake = fake_extractor_llm("default")
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    observations: list[tuple[str | None, str | None]] = []

    def observe_dispatch(**kwargs) -> None:  # noqa: ANN003
        with SessionLocal() as observer:
            idem = observer.get(IdempotencyKey, "run_after_commit")
            run = observer.get(StyleReferenceRun, kwargs["run_id"])
            observations.append(
                (
                    idem.status if idem is not None else None,
                    run.dispatch_state if run is not None else None,
                )
            )

    monkeypatch.setattr(sr_routes, "start_style_reference_run_worker", observe_dispatch)
    book_id = _import_book(client)
    request_kwargs = {
        "json": {"background": True, "force": True},
        "headers": {"X-Idempotency-Key": "run_after_commit"},
    }
    first = client.post(f"{PREFIX}/books/{book_id}/runs", **request_kwargs)
    replay = client.post(f"{PREFIX}/books/{book_id}/runs", **request_kwargs)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Idempotency-Status"] == "replayed"
    assert observations == [("succeeded", "queued"), ("succeeded", "queued")]


def test_list_run_findings_include_evidence(client: TestClient) -> None:
    """PR-23 — ?include=evidence:每条 finding 带 ≥2 evidence 且含 quote_text。"""
    book_id = _import_book(client)
    run_id, _, _ = _seed_full_chain(book_id)
    resp = client.get(f"{PREFIX}/runs/{run_id}/findings?include=evidence")
    assert resp.status_code == 200
    findings = resp.json()["data"]["findings"]
    assert len(findings) == 1
    evidence = findings[0]["evidence"]
    assert len(evidence) >= 2
    assert all(e["quote_text"] for e in evidence)
    assert {e["anchor_kind"] for e in evidence} == {"paragraph_quote", "counter_example"}
    synthetic = next(e for e in evidence if e["anchor_kind"] == "counter_example")
    assert synthetic["is_synthetic"] == 1
    assert synthetic["paragraph_id"] is None
    real = next(e for e in evidence if e["anchor_kind"] == "paragraph_quote")
    assert real["paragraph_id"]
    assert real["span"] == [0, 10]


# ---------------------------------------------------------------------------
# Findings review
# ---------------------------------------------------------------------------


def test_finding_review_happy(client: TestClient) -> None:
    book_id = _import_book(client)
    _, finding_id, _ = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/findings/{finding_id}/review",
        json={"decision": "approved", "comment": "looks good"},
        headers={"X-Idempotency-Key": "rev_1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["decision"] == "approved"
    assert resp.json()["data"]["review_id"].startswith("review_style_ref_finding_")


def test_rejecting_finding_invalidates_profiles_derived_from_same_run(
    client: TestClient,
) -> None:
    book_id = _import_book(client)
    _, finding_id, profile_id = _seed_full_chain(book_id)
    with SessionLocal() as session:
        profile = session.get(StyleReferenceProfile, profile_id)
        profile.status = "active"
        session.commit()

    resp = client.post(
        f"{PREFIX}/findings/{finding_id}/review",
        json={"decision": "rejected", "comment": "evidence does not support it"},
        headers={"X-Idempotency-Key": "rev_invalidates_profile"},
    )

    assert resp.status_code == 200
    assert profile_id in resp.json()["data"]["invalidated_profile_ids"]
    with SessionLocal() as session:
        profile = session.get(StyleReferenceProfile, profile_id)
        assert profile.status == "draft"
        assert profile.coverage_json["stale"] is True
        assert profile.coverage_json["stale_finding_id"] == finding_id


def test_finding_review_invalid_decision(client: TestClient) -> None:
    book_id = _import_book(client)
    _, finding_id, _ = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/findings/{finding_id}/review",
        json={"decision": "yolo"},
        headers={"X-Idempotency-Key": "rev_invalid"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_REVIEW_DECISION_INVALID"


# ---------------------------------------------------------------------------
# Profiles endpoints
# ---------------------------------------------------------------------------


def test_list_profiles(client: TestClient) -> None:
    book_id = _import_book(client)
    _seed_full_chain(book_id)
    resp = client.get(f"{PREFIX}/profiles")
    assert resp.status_code == 200
    assert len(resp.json()["data"]["profiles"]) >= 1


def test_get_profile_happy(client: TestClient) -> None:
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    resp = client.get(f"{PREFIX}/profiles/{profile_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["profile"]["profile_id"] == profile_id


def test_get_profile_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/profiles/sr_profile_nonexistent")
    assert resp.status_code == 404


def test_apply_profile(client: TestClient) -> None:
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "project", "scope_ref_id": "proj_x"},
        headers={"X-Idempotency-Key": "apply_1"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["binding_id"]
    # finding observation + language → style_rule_set;+ calibration_candidate(2 lines profile_json 含 1)
    assert "item_type_counts" not in data and "review_ids" not in data  # 2026-09-14 减法
    assert data["binding_id"]
    with SessionLocal() as session:
        binding = StyleReferenceRepository(session).get_binding(data["binding_id"])
        assert binding is not None
        assert binding.strategy == "mixed"


def test_list_bindings(client: TestClient) -> None:
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    # 先 apply 才有 binding
    client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "project", "scope_ref_id": "proj_y"},
        headers={"X-Idempotency-Key": "apply_2"},
    )
    resp = client.get(f"{PREFIX}/profiles/{profile_id}/bindings")
    assert resp.status_code == 200
    assert len(resp.json()["data"]["bindings"]) >= 1


def test_delete_binding(client: TestClient) -> None:
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    apply_resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "scene", "scope_ref_id": "scene_99"},
        headers={"X-Idempotency-Key": "apply_3"},
    )
    binding_id = apply_resp.json()["data"]["binding_id"]
    resp = client.delete(
        f"{PREFIX}/bindings/{binding_id}",
        headers={"X-Idempotency-Key": "del_bind_1"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True


def test_delete_binding_404(client: TestClient) -> None:
    resp = client.delete(
        f"{PREFIX}/bindings/sr_bind_nonexistent",
        headers={"X-Idempotency-Key": "del_bind_404"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Preview / Synthesize 需要 LLM client(本测试不启用 LLM,确认错误码语义即可)
# ---------------------------------------------------------------------------


def test_synthesize_llm_required_when_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    book_id = _import_book(client)
    run_id, _, _ = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/runs/{run_id}/synthesize",
        headers={"X-Idempotency-Key": "synth_disabled"},
    )
    # synthesize 委托 ProfileSynthesizer → LLMRequiredError(409)。钉精确契约,
    # 不用弱 >= 400(否则 409 退化成通用 500、丢 author_action 也照样绿)。
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]


def test_preview_llm_required_when_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/preview",
        headers={"X-Idempotency-Key": "preview_disabled"},
    )
    # preview 委托 PreviewService → LLMRequiredError(409)。钉精确契约,
    # 不用弱 >= 400(否则 409 退化成通用 500、丢 author_action 也照样绿)。
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]


def test_synthesize_failure_maps_to_409_with_reason_code_and_author_action(
    client: TestClient, monkeypatch
) -> None:
    """2026-09 v2 · W1:SynthesizeError 同时是 DomainError,路由零改动即透传为 409。"""
    from novel_system.services.style_reference.profile_synthesizer import (
        ProfileSynthesizer,
        SynthesizeError,
    )

    def _boom(self, book_id, run_id):  # noqa: ANN001
        raise SynthesizeError(
            "style profile synthesis input cannot fit input_token_budget",
            reason_code="budget_unfit",
            details={"target_input_tokens": 39000, "estimated_floor": 40123},
        )

    monkeypatch.setattr(ProfileSynthesizer, "synthesize", _boom)
    book_id = _import_book(client)
    run_id, _, _ = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/runs/{run_id}/synthesize",
        headers={"X-Idempotency-Key": "synth_budget_unfit"},
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    err = body["error"]
    assert err["code"] == "STYLE_REFERENCE_SYNTHESIZE_FAILED"
    assert err["details"]["reason_code"] == "budget_unfit"
    assert err["details"]["target_input_tokens"] == 39000
    assert err["details"]["author_action"]["view"] == "styleref"
    assert err["details"]["author_action"]["action"] == "review_dimension_matrix_then_synthesize"


# ---------------------------------------------------------------------------
# v2(风格模仿 v2 · W2):导入路由按运行时 LLM 配置 + cloud_policy 选分类器
# ---------------------------------------------------------------------------


def _book_calibration(client: TestClient, book_id: str) -> dict[str, Any]:
    book = client.get(f"{PREFIX}/books/{book_id}").json()["data"]["book"]
    return book["stats_json"]["classifier_calibration"]


def test_import_upload_uses_runtime_llm_classifier_when_enabled(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    import novel_system.api.routes.style_reference as sr_routes

    fake = fake_paragraph_classifier(rule="default")
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    book_id = _import_book(client, fake)  # segments_only + 送出权 → 走 LLM 分类
    # 样本只有几段(不超过锚定集):只有强模型一遍,没有余段就不做快模型对照
    assert fake.call_count >= 1, "至少一次分类调用"
    calibration = _book_calibration(client, book_id)
    assert calibration["fallback_to_heuristic"] is False
    assert calibration["llm_classified_paragraphs"] >= 1
    assert calibration["heuristic_classified_paragraphs"] == 0


def test_import_upload_local_only_book_needs_a_local_llm(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    """2026-09-15 严格 LLM:「仅本机」没有启发式兜底——分类节点走云端接入 409,走本机模型才分类
    (2026-09-23 v3:按分类节点的实际路由判断,不看全局 provider)。"""
    from novel_system.services.style_reference import policy as policy_module

    fake = install_fake_classifier(monkeypatch, fake_paragraph_classifier(rule="default"))
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: False)
    resp = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("local.txt", io.BytesIO(SAMPLE_TXT), "text/plain")},
        data={"title": "local", "cloud_policy": "local_only"},
        headers={"X-Idempotency-Key": "imp_local_cloud"},
    )
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert err["details"]["author_action"]["view"] == "systemConfig"
    assert fake.call_count == 0, "云端模型不得碰「仅本机」的段落"

    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: True)
    resp = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("local.txt", io.BytesIO(SAMPLE_TXT), "text/plain")},
        data={"title": "local", "cloud_policy": "local_only"},
        headers={"X-Idempotency-Key": "imp_local_local"},
    )
    assert resp.status_code == 200, resp.text
    book_id = resp.json()["data"]["book"]["book_id"]
    wait_book_status(client, book_id)
    assert fake.call_count >= 1, "本地模型分类「仅本机」的书"
    assert _book_calibration(client, book_id)["fallback_to_heuristic"] is False


def test_import_upload_without_llm_is_refused(client: TestClient, monkeypatch) -> None:
    """2026-09-15 严格 LLM:没有 LLM 不导入(409 + author_action),不再启发式兜底。"""
    import novel_system.api.routes.style_reference as sr_routes

    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (None, False))
    resp = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("sample.txt", io.BytesIO(SAMPLE_TXT), "text/plain")},
        data={"title": "无模型", "cloud_policy": "local_only"},
        headers={"X-Idempotency-Key": "imp_no_llm"},
    )
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]["view"] == "systemConfig"
    assert client.get(f"{PREFIX}/books").json()["data"]["books"] == []
