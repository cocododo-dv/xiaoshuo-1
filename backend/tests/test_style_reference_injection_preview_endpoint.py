"""GET /bindings/{id}/injection-preview + POST /profiles/{id}/injection-preview(2026-09-23 风格参考 v3)。

预览 = 起草时同一套选窗、同一个块次序:``prefix`` 是 system 前缀(指路句 + 文风卡 / 卡替身 + 声音 + 红线),
``user_tail`` 是样例 + 收口;``stats`` 的键仍是 ``InjectionPreviewStats``;给了 ``scene_id`` 就是这一场起草时
会拿到的窗(不写冻结行)。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from novel_system.db.models import StyleReferenceSceneWindows
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository
from tests.style_reference_inject_helpers import bind, seed_reference, seed_scene

PREFIX = "/api/v2/style-reference"


def _seed(key: str, **kwargs) -> tuple[str, str]:
    with SessionLocal() as session:
        _book_id, profile_id = seed_reference(session, key, **kwargs)
        binding = bind(session, profile_id, binding_id=f"pv_bind_{key}")
        return binding.binding_id, profile_id


def test_get_binding_preview_200_happy(client: TestClient) -> None:
    binding_id, _ = _seed("gethappy")
    resp = client.get(f"{PREFIX}/bindings/{binding_id}/injection-preview")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["prefix"].startswith("[STYLE_REFERENCE]\n")
    assert "[文风卡]" in data["fragments"]["positive_block"]
    assert data["fragments"]["strategy"] == "mixed" and data["reference_mode"] == "full"
    assert data["user_tail"].lstrip().startswith("[风格样例]")
    assert data["stats"]["few_shot_windows"] == 12 and data["stats"]["total_prefix_chars"] == len(data["prefix"]) + len(data["user_tail"])


def test_get_binding_preview_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/bindings/sr_bind_nonexistent/injection-preview")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BINDING_NOT_FOUND"


def test_dryrun_preview_404_profile(client: TestClient) -> None:
    resp = client.post(f"{PREFIX}/profiles/sr_profile_missing/injection-preview", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_PROFILE_NOT_FOUND"


def test_dryrun_preview_422_invalid_values(client: TestClient) -> None:
    _, profile_id = _seed("invalid")
    for body in ({"intensity": 101}, {"sample_windows": 17}, {"reference_mode": "rag"}, {"dimension_states": {"language.rhetoric": "loud"}}):
        assert client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json=body).status_code == 422


def test_dryrun_reference_modes_and_sample_windows(client: TestClient) -> None:
    _, profile_id = _seed("modes")
    url = f"{PREFIX}/profiles/{profile_id}/injection-preview"
    full = client.post(url, json={}).json()["data"]
    assert full["stats"]["few_shot_windows"] == 12 and full["sample_windows"] == 12
    fewer = client.post(url, json={"sample_windows": 5}).json()["data"]
    assert fewer["stats"]["few_shot_windows"] == 5 and fewer["stats"]["few_shot_k"] == 5
    # 旧滑块仍映射:强度 0 → 3 窗
    legacy = client.post(url, json={"strategy": "mixed", "intensity": 0}).json()["data"]
    assert legacy["stats"]["few_shot_windows"] == 3
    card = client.post(url, json={"reference_mode": "card_only"}).json()["data"]
    assert card["stats"]["few_shot_windows"] == 0 and card["user_tail"] == "" and card["fragments"]["strategy"] == "A"
    samples = client.post(url, json={"reference_mode": "samples_only"}).json()["data"]
    assert samples["fragments"]["positive_block"] == "" and samples["fragments"]["voice_block"] == ""
    assert samples["stats"]["few_shot_windows"] == 12 and samples["fragments"]["anti_plagiarism_block"]
    # 维度状态改变卡的内容
    emphasized = client.post(url, json={"dimension_states": {"theme.emotional_tone": "emphasize", "language.rhetoric": "exclude"}}).json()["data"]
    block = emphasized["fragments"]["positive_block"]
    assert "【重点】情感基调" in block and "修辞手法" not in block


def test_preview_with_scene_id_shows_that_scenes_windows_without_freezing(client: TestClient) -> None:
    _, profile_id = _seed("scene")
    with SessionLocal() as session:
        seed_scene(session, "PV_SC_1", scene_seq=1)
        seed_scene(session, "PV_SC_2")
    url = f"{PREFIX}/profiles/{profile_id}/injection-preview"
    first = client.post(url, json={"scene_id": "PV_SC_1"}).json()["data"]
    again = client.post(url, json={"scene_id": "PV_SC_1"}).json()["data"]
    other = client.post(url, json={"scene_id": "PV_SC_2"}).json()["data"]
    assert len(first["window_refs"]) == 12
    assert {"start", "end", "chapter", "position", "paragraphs", "chars", "window_no", "slot"} <= set(first["window_refs"][0])
    assert first["window_refs"] == again["window_refs"] and first["window_refs"] != other["window_refs"]
    # 章首场：位置配额里的窗在最前、样例行带「章首」，尾块有开章补充
    assert any("·章首" in line or "·整章" in line for line in first["user_tail"].splitlines() if line.startswith("- ("))
    assert "本场是本章的第一场" in first["user_tail"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(StyleReferenceSceneWindows)) == 0


def test_generation_banned_terms_reach_the_preview_red_line(client: TestClient) -> None:
    _, profile_id = _seed("terms", terms=("甲乙社",))
    data = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json={}).json()["data"]
    assert "- 甲乙社" in data["fragments"]["anti_plagiarism_block"]
    with SessionLocal() as session:
        assert StyleReferenceRepository(session).get_profile(profile_id) is not None
