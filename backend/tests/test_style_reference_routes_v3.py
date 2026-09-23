"""风格参考 v3 · P6a 接口:书库摘要、文风画像详情、直接绑定、作品的生效绑定、批量删除、服务器路径导入。

钉住(台账 U1 / U4 / U9 / U10 / U15 / U16 / E10 / N2 / N9):
- ``GET /books`` 每本书带画像摘要(``needs_relearn`` 三种原因)与 ``applied_projects``,不带 ``stats_json``;详情带;
- ``GET /profiles/{id}``:规范化的 16 维文风卡,每句带 ✓ / ✗ 状态与依据引文(发现 → 证据 → 引文 + 直接引文,去重、
  段号),气质、声音、结构摘要;✓ / ✗ 写回后详情里就能看到;
- ``POST /profiles/{id}/apply`` → ``PATCH /bindings/{id}`` → ``GET /projects/{id}/style-binding`` → ``DELETE``;
  一个作品只有一条生效的绑定(换书时响应里 ``replaced``);请求体校验(旧参数不收、未知维不收);
- ``POST /books/bulk-delete``:逐本结果,不存在的书单独报错不影响其余;
- ``POST /books/import-path``:管理令牌 + 配置过的导入根目录;
- 删掉的端点一律 404 / 405。
全部是合成内容(无真实作者原文 / 作品人物)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from novel_system.db.models import (
    SceneBlueprint,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from novel_system.services.style_reference.jobs import JOB_KIND_CLASSIFY, StyleJobService
from novel_system.services.style_reference.paragraph_root import ensure_paragraph_root
from novel_system.services.style_reference.repository import StyleReferenceRepository
from tests.style_reference_inject_helpers import card_payload, seed_reference, seed_scene
from tests.style_reference_route_helpers import fake_import_llm, wait_book_status
from tests.test_style_reference_windows import synthetic_rows

PREFIX = "/api/v2/style-reference"
_KEYS = iter(range(10_000))


def _key(tag: str) -> dict[str, str]:
    return {"X-Idempotency-Key": f"v3-{tag}-{next(_KEYS)}"}


def _project(project_id: str, title: str = "合成作品") -> str:
    with SessionLocal() as session:
        if session.get(StoryProject, project_id) is None:
            session.add(StoryProject(project_id=project_id, title=title, outline_text=""))
            session.commit()
    return project_id


def _v3_reference(key: str, *, types_revision: int = 0, stats_revision: int = 0, cloud_policy: str = "allow_full_cloud") -> tuple[str, str]:
    """合成书 + v3 画像(文风卡的第一句有一条直接引文,另一句挂着一条带两条证据的发现)。"""
    with SessionLocal() as session:
        book_id, profile_id = seed_reference(session, key, chapters=3, per_chapter=30, cloud_policy=cloud_policy)
        root, _count = ensure_paragraph_root(session, book_id)
        book = session.get(StyleReferenceBook, book_id)
        book.stats_json = {**dict(book.stats_json or {}), "paragraph_types_revision": stats_revision}
        repo = StyleReferenceRepository(session)
        run_id = f"inj_run_{key}"
        repo.create_extraction(
            extraction_id=f"v3_ext_{key}",
            book_id=book_id,
            run_id=run_id,
            layer="narrative",
            sub_dimension="narrative.pacing",
            raw_payload_json={},
            status="done",
            validation_errors_json=[],
            purpose="extract",
        )
        repo.create_finding(
            finding_id=f"v3_find_{key}",
            book_id=book_id,
            run_id=run_id,
            extraction_id=f"v3_ext_{key}",
            sub_dimension="narrative.pacing",
            finding_kind="observation",
            statement="危急时刻把时间切成一格一格往前推",
            confidence="high",
            status="pending",
        )
        for index, paragraph in enumerate((5, 9)):
            repo.create_quote(
                quote_id=f"v3_quote_{key}_{index}",
                book_id=book_id,
                paragraph_id=f"{book_id}_p{paragraph:05d}",
                span_start=0,
                span_end=8,
                quote_text=f"合成引文第{index + 1}句,钟摆又往前挪了一格",
                illustrates_dims=["narrative.pacing"],
                extracted_features={},
            )
            repo.create_evidence(
                evidence_id=f"v3_ev_{key}_{index}",
                finding_id=f"v3_find_{key}",
                quote_id=f"v3_quote_{key}_{index}",
                anchor_kind="paragraph_quote",
                is_synthetic=0,
            )
        card = card_payload(with_evidence=True, quote_id=f"inj_quote_{key}")
        card["dimensions"][1]["lines"][0]["finding_ids"] = [f"v3_find_{key}"]
        card["dimensions"][1]["summary"] = "节奏靠倒计时推"
        card["dimensions"][1]["model_default"] = "按时间顺序平铺"
        profile = session.get(StyleReferenceProfile, profile_id)
        profile.profile_json = {
            **dict(profile.profile_json or {}),
            "profile_version": "style_profile_v3",
            "dimension_card": card,
            "card_line_states": {},
            "voice": {"habits": ["短句接长句", "对白不加引导词"], "deliberate_repetition": False},
            "learned_from": {"types_revision": types_revision, "root": root, "learned_at": "2026-09-23T00:00:00+00:00"},
            "sub_dimensions": {"narrative.pacing": {"observation_count": 1, "forbidden_pattern_count": 0, "quote_count": 2}},
        }
        profile.coverage_json = {"card_lines": 7, "findings_count": 1, "quotes_count": 3}
        session.commit()
    return book_id, profile_id


# ---------------------------------------------------------------------------
# 书库摘要
# ---------------------------------------------------------------------------


def test_books_list_is_a_summary_with_profile_and_applied_projects(client: TestClient) -> None:
    fresh_book, fresh_profile = _v3_reference("sum_fresh")
    stale_book, _stale_profile = _v3_reference("sum_stale", types_revision=0, stats_revision=2)
    project_id = _project("PRJ_SUM", "北岸")
    resp = client.post(
        f"{PREFIX}/profiles/{fresh_profile}/apply",
        json={"scope": "project", "scope_ref_id": project_id, "config": {"sample_windows": 8}},
        headers=_key("apply"),
    )
    assert resp.status_code == 200, resp.text

    books = client.get(f"{PREFIX}/books").json()["data"]["books"]
    by_id = {book["book_id"]: book for book in books}
    # 按导入时间排序(U15)
    assert [b["book_id"] for b in books] == [fresh_book, stale_book]
    fresh = by_id[fresh_book]
    assert "stats_json" not in fresh
    assert fresh["profile"]["profile_id"] == fresh_profile and fresh["profile"]["card_lines"] == 7
    assert fresh["profile"]["profile_version"] == "style_profile_v3"
    assert fresh["profile"]["learned_at"] == "2026-09-23T00:00:00+00:00"
    assert fresh["profile"]["needs_relearn"] is False and fresh["profile"]["relearn_reason"] is None
    assert fresh["profile_count"] == 1
    assert fresh["applied_projects"] == [
        {
            "project_id": project_id,
            "project_title": "北岸",
            "binding_id": resp.json()["data"]["binding"]["binding_id"],
            "profile_id": fresh_profile,
            "config": {
                "reference_mode": "full",
                "sample_windows": 8,
                "dimension_states": {dim: "normal" for dim in ALL_DIMENSIONS},
                "draft_mode": "style_first",
            },
        }
    ]
    # 段落类型在学完之后又更新过 → 建议重新学习
    stale = by_id[stale_book]
    assert stale["profile"]["needs_relearn"] is True and stale["profile"]["relearn_reason"] == "types_changed"
    assert stale["applied_projects"] == [] and stale["paragraph_types_revision"] == 2

    detail = client.get(f"{PREFIX}/books/{fresh_book}").json()["data"]["book"]
    assert detail["stats_json"]["paragraph_types_revision"] == 0
    assert detail["profile"]["profile_id"] == fresh_profile and detail["applied_projects"][0]["project_id"] == project_id


def test_profile_needs_relearn_when_the_text_changed(client: TestClient) -> None:
    book_id, profile_id = _v3_reference("sum_text")
    with SessionLocal() as session:
        book = session.get(StyleReferenceBook, book_id)
        book.stats_json = {**dict(book.stats_json or {}), "paragraph_root_sha256": "0" * 64}
        session.commit()
    profiles = client.get(f"{PREFIX}/profiles", params={"book_id": book_id}).json()["data"]["profiles"]
    assert [p["profile_id"] for p in profiles] == [profile_id]
    assert profiles[0]["relearn_reason"] == "text_changed" and "profile_json" not in profiles[0]


# ---------------------------------------------------------------------------
# 文风画像详情
# ---------------------------------------------------------------------------


def test_profile_detail_has_the_card_states_and_evidence(client: TestClient) -> None:
    book_id, profile_id = _v3_reference("detail")
    profile = client.get(f"{PREFIX}/profiles/{profile_id}").json()["data"]["profile"]
    assert profile["has_card"] is True and profile["needs_relearn"] is False and profile["legacy"] is None
    assert profile["temperament"] == ["危急关头用自嘲冲淡紧张"]
    assert profile["voice"]["habits"] == ["短句接长句", "对白不加引导词"]
    dims = profile["dimensions"]
    assert len(dims) == 16 and {d["dimension"] for d in dims} == set(ALL_DIMENSIONS)
    # 按辨识度排序:修辞(0.95)在最前
    assert dims[0]["dimension"] == "language.rhetoric" and dims[0]["label"] == "修辞手法" and dims[0]["layer"] == "language"
    rhetoric_lines = dims[0]["lines"]
    first = rhetoric_lines[0]
    assert first["mandatory"] is True and first["kind"] == "do" and first["state"] is None
    assert first["evidence_count"] == 1 and first["evidence"][0]["paragraph_index"] == 3
    avoid = [line for line in rhetoric_lines if line["kind"] == "avoid"]
    assert [line["text"] for line in avoid] == ["不让天气替人伤心"]
    pacing = next(d for d in dims if d["dimension"] == "narrative.pacing")
    assert pacing["summary"] == "节奏靠倒计时推" and pacing["model_default"] == "按时间顺序平铺"
    assert pacing["devices"] == ["倒计时"] and pacing["quote_count"] == 2
    line = pacing["lines"][0]
    # 发现 → 证据 → 引文:两条原话,段号来自段落表
    assert line["evidence_count"] == 2 and [e["paragraph_index"] for e in line["evidence"]] == [5, 9]
    assert line["sources"] == [
        {"finding_id": "v3_find_detail", "statement": "危急时刻把时间切成一格一格往前推", "kind": "observation"}
    ]
    empty = next(d for d in dims if d["dimension"] == "theme.values")
    assert empty["lines"] == [] and empty["label"] == "价值取向"

    # ✓ 写回后详情里就能看到(不重新学习、画像不失效)
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/card-lines/{first['line_id']}",
        json={"state": "pinned"},
        headers=_key("pin"),
    )
    assert resp.status_code == 200, resp.text
    again = client.get(f"{PREFIX}/profiles/{profile_id}").json()["data"]["profile"]
    assert again["dimensions"][0]["lines"][0]["state"] == "pinned"
    assert again["card_line_states"] == {first["line_id"]: "pinned"} and again["status"] == "active"
    assert book_id == again["book_id"]


def test_legacy_profile_detail_shows_what_drafting_still_reads(client: TestClient) -> None:
    with SessionLocal() as session:
        _book_id, profile_id = seed_reference(session, "legacy_detail", chapters=2, per_chapter=20, card=False, legacy=True)
    profile = client.get(f"{PREFIX}/profiles/{profile_id}").json()["data"]["profile"]
    assert profile["has_card"] is False and profile["relearn_reason"] == "legacy_profile"
    groups = {group["key"]: group for group in profile["legacy"]["groups"]}
    features = groups["style_features"]["lines"]
    # 含数字的句子起草时整句不带:如实标出
    assert any(item["dropped_in_drafting"] for item in features)
    assert any(not item["dropped_in_drafting"] for item in features)
    assert all(not d["lines"] for d in profile["dimensions"])


# ---------------------------------------------------------------------------
# 直接绑定 / 改配置 / 作品的生效绑定 / 解除
# ---------------------------------------------------------------------------


def test_apply_patch_read_and_unbind_a_project(client: TestClient) -> None:
    _book_a, profile_a = _v3_reference("bind_a")
    _book_b, profile_b = _v3_reference("bind_b")
    project_id = _project("PRJ_BIND")
    empty = client.get(f"{PREFIX}/projects/{project_id}/style-binding").json()["data"]
    assert empty["binding"] is None and empty["policy"]["bound"] is False

    first = client.post(
        f"{PREFIX}/profiles/{profile_a}/apply",
        json={"scope": "project", "scope_ref_id": project_id, "config": {"reference_mode": "samples_only"}},
        headers=_key("apply-a"),
    ).json()["data"]
    binding_id = first["binding"]["binding_id"]
    assert first["created"] is True and first["binding"]["config"]["reference_mode"] == "samples_only"

    patched = client.patch(
        f"{PREFIX}/bindings/{binding_id}",
        json={"config": {"dimension_states": {"scene.dialogue": "emphasize"}, "draft_mode": "neutral_first"}},
        headers=_key("patch"),
    )
    assert patched.status_code == 200, patched.text
    config = patched.json()["data"]["binding"]["config"]
    assert config["dimension_states"]["scene.dialogue"] == "emphasize"
    assert config["draft_mode"] == "neutral_first" and config["reference_mode"] == "samples_only"

    read = client.get(f"{PREFIX}/projects/{project_id}/style-binding").json()["data"]
    assert read["binding"]["binding_id"] == binding_id and read["profile"]["profile_id"] == profile_a
    assert read["policy"]["bound"] is True and read["policy"]["draft_mode"] == "neutral_first"
    assert read["book"]["title"] == "合成书"

    # 换一本:旧的那条停用,响应里说出来
    second = client.post(
        f"{PREFIX}/profiles/{profile_b}/apply",
        json={"scope": "project", "scope_ref_id": project_id},
        headers=_key("apply-b"),
    ).json()["data"]
    assert [item["binding_id"] for item in second["replaced"]] == [binding_id]
    read = client.get(f"{PREFIX}/projects/{project_id}/style-binding").json()["data"]
    assert read["profile"]["profile_id"] == profile_b
    listed = client.get(f"{PREFIX}/profiles/{profile_a}/bindings").json()["data"]["bindings"]
    assert [b["status"] for b in listed] == ["disabled"]

    gone = client.delete(f"{PREFIX}/bindings/{second['binding']['binding_id']}", headers=_key("unbind"))
    assert gone.status_code == 200 and gone.json()["data"]["deleted"] is True
    assert client.get(f"{PREFIX}/projects/{project_id}/style-binding").json()["data"]["binding"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "global", "scope_ref_id": "x"},
        {"scope": "project", "scope_ref_id": "PRJ_V", "strategy": "A"},
        {"scope": "project", "scope_ref_id": "PRJ_V", "intensity": 40},
        {"scope": "project", "scope_ref_id": "PRJ_V", "config": {"sample_windows": 17}},
        {"scope": "project", "scope_ref_id": "PRJ_V", "config": {"reference_mode": "rag"}},
        {"scope": "project", "scope_ref_id": "PRJ_V", "config": {"dimension_states": {"made.up": "exclude"}}},
        {"scope": "project", "scope_ref_id": "PRJ_V", "config": {"dimension_states": {"scene.dialogue": "loud"}}},
        {"scope": "project"},
    ],
)
def test_apply_rejects_old_or_invalid_parameters(client: TestClient, body: dict) -> None:
    # 请求体在找画像之前就校验(旧参数 / 未知维 / 越界一律 422)
    resp = client.post(f"{PREFIX}/profiles/sr_profile_any/apply", json=body, headers=_key("bad"))
    assert resp.status_code == 422, resp.text


def test_apply_to_a_missing_project_is_404(client: TestClient) -> None:
    _book, profile_id = _v3_reference("missing_target")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "project", "scope_ref_id": "PRJ_NOPE"},
        headers=_key("missing"),
    )
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "STYLE_REFERENCE_APPLY_TARGET_NOT_FOUND"
    assert client.get(f"{PREFIX}/projects/PRJ_NOPE/style-binding").status_code == 404
    patch = client.patch(f"{PREFIX}/bindings/sr_bind_nope", json={"config": {}}, headers=_key("patch-missing"))
    assert patch.status_code == 404


def test_apply_to_a_scene(client: TestClient) -> None:
    _book, profile_id = _v3_reference("scene_bind")
    with SessionLocal() as session:
        scene = seed_scene(session, "V3_SC_1", project_id="PRJ_V3_SCENE", chapter_id="V3_CH_1")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "scene", "scope_ref_id": scene.scene_id},
        headers=_key("scene"),
    )
    assert resp.status_code == 200, resp.text
    read = client.get(f"{PREFIX}/projects/PRJ_V3_SCENE/style-binding").json()["data"]
    assert read["binding"] is None and read["scene_bindings"] == 1


# ---------------------------------------------------------------------------
# 批量删除
# ---------------------------------------------------------------------------


def test_bulk_delete_reports_each_book(client: TestClient) -> None:
    book_a, profile_a = _v3_reference("bulk_a")
    book_b, _profile_b = _v3_reference("bulk_b")
    keep, _keep_profile = _v3_reference("bulk_keep")
    with SessionLocal() as session:
        scene = seed_scene(session, "BULK_SC", project_id="PRJ_BULK", chapter_id="BULK_CH")
        session.add(
            SceneBlueprint(row_id="bp_bulk", scene_id=scene.scene_id, chapter_id=scene.chapter_id, blueprint_json={}, status="accepted")
        )
        StyleJobService(session).create(JOB_KIND_CLASSIFY, book_id=book_b, params={"mode": "retype"})
        session.commit()
    applied = client.post(
        f"{PREFIX}/profiles/{profile_a}/apply",
        json={"scope": "project", "scope_ref_id": "PRJ_BULK"},
        headers=_key("bulk-apply"),
    )
    assert applied.status_code == 200, applied.text
    with SessionLocal() as session:
        session.get(SceneBlueprint, "bp_bulk").status = "accepted"
        session.commit()

    resp = client.post(
        f"{PREFIX}/books/bulk-delete",
        json={"book_ids": [book_a, "sr_book_missing", book_b, book_a]},
        headers=_key("bulk"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["deleted_count"] == 2 and data["failed_count"] == 1
    results = {item["book_id"]: item for item in data["results"]}
    assert list(results) == [book_a, "sr_book_missing", book_b]  # 重复的只删一次
    assert results[book_a]["deleted"] is True and results[book_a]["title"] == "合成书"
    assert [b["scope_ref_id"] for b in results[book_a]["unbound"]] == ["PRJ_BULK"]
    assert results["sr_book_missing"]["deleted"] is False
    assert results["sr_book_missing"]["error"]["code"] == "STYLE_REFERENCE_BOOK_NOT_FOUND"
    with SessionLocal() as session:
        assert session.get(StyleReferenceBook, book_a) is None and session.get(StyleReferenceBook, book_b) is None
        assert session.get(StyleReferenceBook, keep) is not None
        assert session.scalars(select(StyleReferenceInjectionBinding)).all() == []
        # 删掉的书原来用在的范围里,按它做的规划作废
        assert session.get(SceneBlueprint, "bp_bulk").status == "superseded"
    listed = [b["book_id"] for b in client.get(f"{PREFIX}/books").json()["data"]["books"]]
    assert listed == [keep]


def test_bulk_delete_validates_its_body(client: TestClient) -> None:
    for body in ({"book_ids": []}, {"book_ids": ["x"] * 101}, {"book_ids": "x"}):
        assert client.post(f"{PREFIX}/books/bulk-delete", json=body, headers=_key("bulk-bad")).status_code == 422


# ---------------------------------------------------------------------------
# 服务器路径导入(管理面)
# ---------------------------------------------------------------------------


_PATH_TEXT = "\n\n".join(row["text"] for row in synthetic_rows("path_import", chapters=2, per_chapter=12))


def test_import_path_needs_the_admin_token_and_a_configured_root(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "books"
    root.mkdir()
    inside = root / "合成参考.txt"
    inside.write_text(_PATH_TEXT, encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text(_PATH_TEXT, encoding="utf-8")
    monkeypatch.setenv("NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS", str(root))
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    body = {
        "file_path": str(inside),
        "title": "路径导入",
        "cloud_policy": "allow_full_cloud",
        "rights_declaration": {"analysis_rights": True, "send_rights": True},
    }
    with fake_import_llm():
        no_token = client.post(f"{PREFIX}/books/import-path", json=body, headers=_key("path-no-token"))
        assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "ADMIN_TOKEN_REQUIRED"
        wrong = client.post(
            f"{PREFIX}/books/import-path", json=body, headers={**_key("path-wrong"), "X-Admin-Token": "nope"}
        )
        assert wrong.status_code == 403
        forbidden = client.post(
            f"{PREFIX}/books/import-path",
            json={**body, "file_path": str(outside)},
            headers={**_key("path-outside"), "X-Admin-Token": "admin-token"},
        )
        assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_PATH_FORBIDDEN"
        ok_resp = client.post(
            f"{PREFIX}/books/import-path", json=body, headers={**_key("path-ok"), "X-Admin-Token": "admin-token"}
        )
        assert ok_resp.status_code == 200, ok_resp.text
        data = ok_resp.json()["data"]
        assert data["book"]["source_kind"] == "path" and data["job_id"]
        book = wait_book_status(client, data["book"]["book_id"])
    assert book["classification"]["state"] == "succeeded"


def test_import_path_is_disabled_without_configured_roots(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "book.txt"
    path.write_text(_PATH_TEXT, encoding="utf-8")
    monkeypatch.delenv("NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS", raising=False)
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    with fake_import_llm():
        resp = client.post(
            f"{PREFIX}/books/import-path",
            json={"file_path": str(path), "title": "t", "cloud_policy": "allow_full_cloud",
                  "rights_declaration": {"analysis_rights": True, "send_rights": True}},
            headers={**_key("path-disabled"), "X-Admin-Token": "admin-token"},
        )
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "STYLE_REFERENCE_PATH_IMPORT_DISABLED"


# ---------------------------------------------------------------------------
# 删掉的端点
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/runs/sr_run_x"),
        ("get", "/bindings/sr_bind_x/injection-preview"),
        ("get", "/injection/task-defaults"),
        ("get", "/imports/some-key/progress"),
        ("post", "/profiles/sr_profile_x/preview"),
        ("post", "/profiles/sr_profile_x/validate"),
        ("get", "/reports/sr_rep_x"),
        ("get", "/profiles/sr_profile_x/reports"),
    ],
)
def test_removed_endpoints_are_gone(client: TestClient, method: str, path: str) -> None:
    resp = client.request(method, f"{PREFIX}{path}", json={} if method == "post" else None, headers=_key("gone"))
    assert resp.status_code in (404, 405), (path, resp.status_code)
