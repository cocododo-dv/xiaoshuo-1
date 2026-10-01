"""本场预览 POST /profiles/{id}/injection-preview(2026-09-23 风格参考 v3;旧 GET /bindings/{id}/injection-preview 已删)。

预览 = 起草时同一套选窗、同一个块次序(``inject.preview.preview_render``:system 前缀是指路句 + 文风卡 + 声音 +
红线,user 尾块是样例 + 收口);给了 ``scene_id`` 就是这一场起草时会拿到的窗(不写冻结行)。端点回界面读的
v3 字段:``windows``(章 / 位置 / 标签 / 梗概)、``blocks``、``sizes``、生效的 ``reference_mode`` 与
``requested_reference_mode``、``notices``;旧的 ``fragments`` / ``prefix`` / ``user_tail`` / ``stats`` /
``window_refs`` 界面一个都不读,2026-09-30 不再回(B10-22)——提示全文的断言对 ``preview_render`` 做。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from novel_system.db.models import StyleReferenceSceneWindows
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.repository import StyleReferenceRepository
from tests.style_reference_inject_helpers import bind, seed_reference, seed_scene

PREFIX = "/api/v2/style-reference"
DROPPED_KEYS = {"fragments", "prefix", "user_tail", "stats", "window_refs"}


def _seed(key: str, **kwargs) -> tuple[str, str]:
    with SessionLocal() as session:
        _book_id, profile_id = seed_reference(session, key, **kwargs)
        binding = bind(session, profile_id, binding_id=f"pv_bind_{key}")
        return binding.binding_id, profile_id


def _render(profile_id: str, config: dict | None = None, *, scene_id: str | None = None) -> dict:
    """端点背后的同一次渲染(提示全文、读数)。"""
    with SessionLocal() as session:
        return preview_render(session, profile_id, dict(config or {}), scene_id=scene_id)


def test_dryrun_preview_v3_fields(client: TestClient) -> None:
    _, profile_id = _seed("v3fields")
    data = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json={}).json()["data"]
    assert not DROPPED_KEYS & set(data)
    rendered = _render(profile_id)
    assert rendered["prefix"].startswith("[STYLE_REFERENCE]\n")
    assert data["reference_mode"] == "full" and data["requested_reference_mode"] == "full"
    assert data["draft_mode"] == "style_first" and data["sample_windows"] == 12
    assert len(data["windows"]) == 12
    window = data["windows"][0]
    assert {"window_no", "chapter", "position", "chars", "paragraphs", "slot", "situations", "moods", "dimensions", "gist", "paragraph_type"} <= set(window)
    assert "devices" not in window
    # 每窗带占比最大的段落类型（合成书只有对话 / 叙述两类正文段）
    assert {w["paragraph_type"] for w in data["windows"]} <= {"dialogue", "narration"}
    assert all(w["paragraph_type"] for w in data["windows"])
    # 窗按原书顺序
    assert [w["start"] for w in data["windows"]] == sorted(w["start"] for w in data["windows"])
    assert "[文风卡]" in data["blocks"]["card"] and data["blocks"]["card"] == rendered["fragments"]["positive_block"]
    assert data["blocks"]["samples"] == rendered["fragments"]["few_shot_block"] and data["blocks"]["red_line"]
    assert set(data["blocks"]) == {"card", "voice", "samples", "red_line"}
    sizes = data["sizes"]
    assert sizes["system_prefix_chars"] == len(rendered["prefix"]) and sizes["user_tail_chars"] == len(rendered["user_tail"])
    assert sizes["sample_windows"] == 12 and sizes["sample_chars"] == rendered["stats"]["few_shot_chars"]
    assert sizes["card_chars"] == len(data["blocks"]["card"]) and data["notices"] == []


def test_dryrun_preview_reports_the_effective_mode_for_segments_only_books(client: TestClient) -> None:
    """「只发短句」的书:起草时只送文风卡——预览如实说生效的是 card_only,样例窗为 0。"""
    _, profile_id = _seed("segonly", cloud_policy="segments_only")
    data = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json={"reference_mode": "full"}).json()["data"]
    assert data["requested_reference_mode"] == "full" and data["reference_mode"] == "card_only"
    assert data["windows"] == [] and data["sizes"]["sample_windows"] == 0 and data["blocks"]["samples"] == ""
    # 样例位置只有一句「本次没有附原文样例」（M5），没有原文
    user_tail = _render(profile_id, {"reference_mode": "full"})["user_tail"]
    assert "\n- (第" not in user_tail and "本次没有附参考作者的原文样例" in user_tail


def test_dryrun_preview_404_profile(client: TestClient) -> None:
    resp = client.post(f"{PREFIX}/profiles/sr_profile_missing/injection-preview", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_PROFILE_NOT_FOUND"


def test_dryrun_preview_422_invalid_values(client: TestClient) -> None:
    _, profile_id = _seed("invalid")
    # 旧 strategy / intensity 不再收（2026-09-24）：未知键一律 422
    for body in ({"intensity": 50}, {"strategy": "A"}, {"sample_windows": 17}, {"reference_mode": "rag"}, {"dimension_states": {"language.rhetoric": "loud"}}):
        assert client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json=body).status_code == 422


def test_dryrun_reference_modes_and_sample_windows(client: TestClient) -> None:
    _, profile_id = _seed("modes")
    url = f"{PREFIX}/profiles/{profile_id}/injection-preview"
    full = client.post(url, json={}).json()["data"]
    assert full["sizes"]["sample_windows"] == 12 and full["sample_windows"] == 12
    fewer = client.post(url, json={"sample_windows": 5}).json()["data"]
    assert fewer["sizes"]["sample_windows"] == 5 and _render(profile_id, {"sample_windows": 5})["stats"]["few_shot_k"] == 5
    card = client.post(url, json={"reference_mode": "card_only"}).json()["data"]
    assert card["sizes"]["sample_windows"] == 0 and card["blocks"]["samples"] == ""
    card_render = _render(profile_id, {"reference_mode": "card_only"})
    assert set(card_render["stats"]) == {"positive_lines", "avoid_lines", "voice_lines", "few_shot_windows", "few_shot_chars", "total_prefix_chars", "card_chars", "few_shot_k"}
    assert card["sizes"]["card_chars"] == len(card["blocks"]["card"]) and card_render["stats"]["avoid_lines"] >= 1
    assert card["sizes"]["card_lines"] == card_render["stats"]["positive_lines"] + card_render["stats"]["avoid_lines"]
    assert "\n- (第" not in card_render["user_tail"] and "本次没有附参考作者的原文样例" in card_render["user_tail"]
    samples = client.post(url, json={"reference_mode": "samples_only"}).json()["data"]
    assert samples["blocks"]["card"] == "" and samples["blocks"]["voice"] == ""
    assert samples["sizes"]["sample_windows"] == 12 and samples["blocks"]["red_line"]
    # 维度状态改变卡的内容
    emphasized = client.post(url, json={"dimension_states": {"theme.emotional_tone": "emphasize", "language.rhetoric": "exclude"}}).json()["data"]
    block = emphasized["blocks"]["card"]
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
    assert len(first["windows"]) == 12
    assert {"start", "end", "chapter", "position", "paragraphs", "chars", "window_no", "slot"} <= set(first["windows"][0])
    assert first["windows"] == again["windows"] and first["windows"] != other["windows"]
    assert first["scene"]["scene_id"] == "PV_SC_1" and first["scene"]["found"] is True
    assert first["scene"]["position"] == "opening"
    rendered = _render(profile_id, scene_id="PV_SC_1")
    assert [w["window_no"] for w in first["windows"]] == [r["window_no"] for r in rendered["window_refs"]]
    # 章首场：位置配额里的窗在最前、样例行带「章首」，尾块有开章补充
    assert any("·章首" in line or "·整章" in line for line in first["blocks"]["samples"].splitlines() if line.startswith("- ("))
    assert "本场是本章的第一场" in rendered["user_tail"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(StyleReferenceSceneWindows)) == 0


def test_generation_banned_terms_reach_the_preview_red_line(client: TestClient) -> None:
    _, profile_id = _seed("terms", terms=("甲乙社",))
    data = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json={}).json()["data"]
    assert "- 甲乙社" in data["blocks"]["red_line"]
    with SessionLocal() as session:
        assert StyleReferenceRepository(session).get_profile(profile_id) is not None


def test_scene_preview_dominant_type_is_stable() -> None:
    """主段落类型取占比最大的一类；并列时按键名取前者，结果稳定；没有分布给空串（选窗冻结与本场预览同一个函数）。"""
    from novel_system.services.style_reference.windows import dominant_type

    assert dominant_type({"narration": 0.3, "dialogue": 0.7}) == "dialogue"
    assert dominant_type({"narration": 0.5, "dialogue": 0.5}) == "dialogue"
    assert dominant_type({}) == "" and dominant_type(None) == ""
    assert dominant_type({"narration": "x", "action": 0.2}) == "action"
