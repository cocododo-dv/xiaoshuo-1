"""2026-09-14 风格保真修补 WP3:全书样例窗口索引与按场景选窗。

覆盖:鲁迅黄金语料按篇切窗(每窗有界、不重叠、位置标签);副文本与太短的「章」不入索引;
选窗的对白配额 / 段型覆盖 / 位置配额 / 跨章分散;渲染走索引路径并记录窗口引用,按场景轮换,
位置提示生效;冻结契约根哈希失配时退回证据引文路径;合成期写入 exemplar_windows;
预览端点按 scene_id 返回 window_refs。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import injection as injection_module
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.exemplar_index import (
    build_exemplar_window_index,
    dominant_types,
    primary_type,
)
from novel_system.services.style_reference.injection import (
    InjectionService,
    _pick_index_windows,
    scene_sampling_hints,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    build_style_runtime_contract,
    extract_style_generation_context,
)
from novel_system.services.style_reference.schemas import InjectionStrategy
from novel_system.services.style_reference.text_utils import normalize_text, split_paragraphs

from tests.test_style_reference_injection_v2 import _bind, _profile_json

PREFIX = "/api/v2/style-reference"
_CORPUS = Path(__file__).resolve().parent / "golden" / "style_reference" / "corpus" / "luxun_short_stories.txt"


@pytest.fixture(autouse=True)
def _reset_caches():
    clear_config_cache()
    injection_module._EXEMPLAR_INDEX_CACHE.clear()
    yield
    clear_config_cache()
    injection_module._EXEMPLAR_INDEX_CACHE.clear()


# ---------------------------------------------------------------------------
# 合成书:12 章——前 6 章各 10 段(约 700 字,单窗),后 6 章各 24 段 × 约 200 字(两窗:开章 + 收章)
# ---------------------------------------------------------------------------

_SHORT_LINES = (
    "“第{c}章第{i}句，你到底去不去？”她把灯芯拨小了些，屋里暗下去一半。",
    "第{c}章第{i}段叙述：他没有回答，先把袖口的水拧了拧，桌上两只碗一只是干的。",
)
_LONG_LINE = (
    "第{c}章第{i}段：外头的雨又密起来了，院子里那株桂树被打得低了头，叶子上滚下的水珠一颗一颗落在石阶上，"
    "声音清脆得近乎不合时宜；他把茶碗端起来又放下，热气扑到脸上，忽然觉得眼睛有些酸，便把头转向窗外，"
    "假装看那株低头的桂树，屋子里没有人说话，只有灶间的水开始在锅里响，那声音把整个屋子都填满了，"
    "他数着檐角的余声，一滴，又一滴，像是替谁在等一个不会来的人。"
)


def _synthetic_rows() -> list[dict]:
    rows: list[dict] = []

    def add(text: str, ptype: str) -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    for chapter in range(1, 13):
        add(f"第{chapter}章 灯下 Under the Lamp", "transition")
        if chapter <= 6:
            # 约 800 字:单窗(whole)
            for i in range(10):
                add(_SHORT_LINES[i % 2].format(c=chapter, i=i) * 2, "dialogue" if i % 2 == 0 else "narration")
        else:
            # 约 6,600 字:两窗(opening + closing)
            for i in range(30):
                if i % 3 == 0:
                    add(_SHORT_LINES[0].format(c=chapter, i=i) * 3, "dialogue")
                else:
                    add(_LONG_LINE.format(c=chapter, i=i) + _LONG_LINE.format(c=chapter, i=i)[:80], "narration")
    return rows


def _seed_synthetic_book(repo: StyleReferenceRepository, seed: str) -> tuple[str, dict[str, list[str]]]:
    rows = _synthetic_rows()
    book_id = f"ei_book_{seed}"
    repo.create_book(
        book_id=book_id,
        title="合成十二章",
        source_kind="upload",
        cloud_policy="segments_only",
        text_checksum=hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        total_chars=sum(len(r["text"]) for r in rows),
        status="ready",
        stats_json={"rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True}},
    )
    repo.create_run(run_id=f"ei_run_{seed}", book_id=book_id, status="done", phase="done")
    for row in rows:
        repo.create_paragraph(
            paragraph_id=f"ei_p_{seed}_{row['paragraph_index']:04d}",
            book_id=book_id,
            paragraph_index=row["paragraph_index"],
            paragraph_type=row["paragraph_type"],
            start_offset=0,
            end_offset=len(row["text"]),
            text=row["text"],
            char_count=len(row["text"]),
            classifier_confidence=0.9,
        )
    # 证据引文只落在第 7–9 章的少数段上(兜底路径的窗口中心)
    samples_index: dict[str, list[str]] = {}
    for row in rows:
        if row["paragraph_type"] == "transition":
            continue
        chapter_text = row["text"][:3]
        if chapter_text in ("第7章", "第8章", "第9章") and row["paragraph_index"] % 5 == 0:
            quote_id = f"ei_q_{seed}_{row['paragraph_index']}"
            repo.create_quote(
                quote_id=quote_id,
                book_id=book_id,
                paragraph_id=f"ei_p_{seed}_{row['paragraph_index']:04d}",
                span_start=0,
                span_end=18,
                quote_text=row["text"][:18],
                illustrates_dims=["language.rhythm"],
                extracted_features={},
            )
            samples_index.setdefault(row["paragraph_type"], []).append(quote_id)
    return book_id, samples_index


def _seed_full(seed: str, *, scope_ref_id: str | None = None) -> tuple[str, str, str | None]:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id, samples_index = _seed_synthetic_book(repo, seed)
        profile_id = f"ei_profile_{seed}"
        repo.create_profile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=f"ei_run_{seed}",
            title="合成风格",
            status="active",
            profile_json=_profile_json(samples_index),
            coverage_json={},
            source_finding_ids_json=[],
        )
        binding_id = None
        if scope_ref_id:
            binding_id = f"ei_bind_{seed}"
            _bind(
                repo,
                binding_id=binding_id,
                profile_id=profile_id,
                scope="project",
                scope_ref_id=scope_ref_id,
                strategy="mixed",
                config_json={"intensity": 100},
            )
        session.commit()
    return book_id, profile_id, binding_id


def _render(profile_id: str, *, seed: str | None, intensity: int = 100, position: str | None = None, context: str | None = None):
    with SessionLocal() as session:
        svc = InjectionService(session)
        svc.few_shot_seed = seed
        svc.scene_position = position
        svc.context_text = context
        profile = StyleReferenceRepository(session).get_profile(profile_id)
        fragments, stats = svc.render_preview(profile, InjectionStrategy.MIXED, {"intensity": intensity})
        return fragments, stats, list(svc.last_few_shot_window_refs)


# ---------------------------------------------------------------------------
# 索引本身
# ---------------------------------------------------------------------------


def test_index_on_luxun_corpus_splits_stories_into_bounded_windows() -> None:
    from novel_system.services.style_reference.segmentation.heuristic import classify_heuristic_sequence

    text = normalize_text(_CORPUS.read_text(encoding="utf-8"))
    spans = split_paragraphs(text)
    classified = classify_heuristic_sequence([body for _s, _e, body in spans])
    rows = [
        {"paragraph_index": i, "text": body, "paragraph_type": ptype}
        for i, ((_s, _e, body), (ptype, _conf)) in enumerate(zip(spans, classified))
    ]
    index = build_exemplar_window_index(rows, window_paragraphs=60, window_max_chars=4000, min_window_chars=600)
    assert index["version"] == "exemplar_windows_v2"
    assert index["chapter_count"] == 11  # 十一篇以《题名》分章
    windows = index["windows"]
    assert index["window_count"] == len(windows) >= 11
    previous_end = -1
    for window in windows:
        assert 600 <= window["chars"] <= 4000 * 1.25
        assert 1 <= window["paragraphs"] <= 60 * 1.25
        assert window["start"] > previous_end and window["end"] >= window["start"]
        previous_end = window["end"]
        assert window["position"] in {"opening", "closing", "middle", "whole"}
        assert 0.0 <= window["dialogue_share"] <= 1.0
        assert isinstance(window["affinity"], float)
    assert {w["position"] for w in windows} >= {"opening", "closing"}
    assert primary_type(windows[0]) in windows[0]["types"]
    assert dominant_types(windows[0]) <= set(windows[0]["types"])


def test_paratext_and_tiny_chapters_never_enter_the_index() -> None:
    rows: list[dict] = []

    def add(text: str, ptype: str = "narration") -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    add("第一章 卷首", "transition")
    for i in range(3):
        add(f"卷首语第{i}句，很短。")
    add("第二章 正文 The Real Thing", "transition")
    for i in range(12):
        if i == 6:
            add("[1] 这是一条脚注，不是作者的文字。")
        add(f"第二章第{i}段：" + "他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯。" * 2)
    index = build_exemplar_window_index(rows, min_window_chars=600)
    assert index["chapter_count"] == 2
    assert index["window_count"] == 1  # 卷首太短不入索引;脚注不计段
    window = index["windows"][0]
    assert window["chapter"] == 2 and window["paragraphs"] == 12 and window["position"] == "whole"
    assert window["start"] == 5 and window["end"] == 17


def test_pick_index_windows_honours_dialogue_type_position_and_chapter_spread() -> None:
    def cand(order: int, chapter: int, ptype: str, *, dialogue: bool, position_match: int = 0, start: int = 0):
        return {
            "window": {"start": start or order * 10, "end": (start or order * 10) + 5, "chapter": chapter},
            "ptype": ptype,
            "dominant": {ptype},
            "has_dialogue": dialogue,
            "position_match": position_match,
            "key": (order,),
        }

    candidates = [
        cand(0, 1, "narration", dialogue=False, position_match=1),
        cand(1, 1, "narration", dialogue=False, position_match=1),
        cand(2, 2, "dialogue", dialogue=True),
        cand(3, 2, "dialogue", dialogue=True),
        cand(4, 3, "psychology", dialogue=False),
        cand(5, 4, "dialogue", dialogue=True),
        cand(6, 5, "narration", dialogue=False),
    ]
    picked = _pick_index_windows(candidates, k=4, dialogue_quota=2, coverage_types={"dialogue", "narration"}, position_quota=1)
    assert len(picked) == 4
    assert sum(1 for c in picked if c["has_dialogue"]) >= 2
    assert sum(1 for c in picked if c["position_match"]) <= 1
    assert {c["ptype"] for c in picked} >= {"dialogue", "narration"}
    chapters = [c["window"]["chapter"] for c in picked]
    assert len(set(chapters)) == len(chapters)  # 够分时不重复选章


def test_scene_sampling_hints_from_the_scene_card() -> None:
    from types import SimpleNamespace

    assert scene_sampling_hints(None) == (None, set())
    # 2026-09-22 风格参考优先:首稿没有正文可分类时,段型提示按场景形态给出(缺省主动场 → 对白 + 叙述)
    assert scene_sampling_hints(SimpleNamespace(scene_seq=1, is_chapter_last=False, writer_brief_json={})) == ("opening", {"dialogue", "narration"})
    assert scene_sampling_hints(SimpleNamespace(scene_seq=3, is_chapter_last=True, writer_brief_json=None)) == ("closing", {"dialogue", "narration"})
    assert scene_sampling_hints(SimpleNamespace(scene_seq=1, is_chapter_last=True, writer_brief_json={})) == ("whole", {"dialogue", "narration"})
    _pos, reactive_hints = scene_sampling_hints(
        SimpleNamespace(scene_seq=2, is_chapter_last=False, writer_brief_json={"scene_form": "reactive"})
    )
    assert reactive_hints == {"psychology", "narration", "dialogue"}
    position, hints = scene_sampling_hints(
        SimpleNamespace(scene_seq=2, is_chapter_last=False, writer_brief_json={"rendering_mode": "summary"})
    )
    assert position is None and hints == {"narration"}


# ---------------------------------------------------------------------------
# 渲染走索引
# ---------------------------------------------------------------------------


def test_render_selects_windows_from_the_whole_book_and_records_refs() -> None:
    _book_id, profile_id, _ = _seed_full("render")
    fragments, stats, refs = _render(profile_id, seed="scene-1")
    assert stats["few_shot_k"] == 12
    assert stats["few_shot_windows"] == len(refs) == 12
    assert "连续" in fragments.few_shot_block and "段窗口" in fragments.few_shot_block
    # 索引窗口跨章分散(6 + 12 = 18 个窗口 / 12 章:每章最多一窗)
    chapters = [ref["chapter"] for ref in refs]
    assert len(set(chapters)) == 12
    for ref in refs:
        assert set(ref) == {"start", "end", "chapter", "position", "paragraph_type", "paragraphs", "chars"}
        assert ref["chars"] >= 600 and ref["end"] >= ref["start"]
    # 按原书顺序呈现
    assert [ref["start"] for ref in refs] == sorted(ref["start"] for ref in refs)
    # 引文兜底路径的短引文标签不再出现
    assert "证据短引文" not in fragments.few_shot_block


def test_render_rotates_by_seed_and_prefers_the_scene_position() -> None:
    _book_id, profile_id, _ = _seed_full("rotate")
    _f1, _s1, refs_a = _render(profile_id, seed="scene-A", intensity=50)
    _f2, _s2, refs_b = _render(profile_id, seed="scene-B", intensity=50)
    _f3, _s3, refs_a2 = _render(profile_id, seed="scene-A", intensity=50)
    assert refs_a == refs_a2
    assert refs_a != refs_b
    _f4, _s4, closing = _render(profile_id, seed="scene-C", position="closing")
    matched = [ref for ref in closing if ref["position"] in {"closing", "whole"}]
    assert matched and len([ref for ref in closing if ref["position"] == "closing"]) >= 1
    # 位置匹配最多一半,其余来自全书别处
    assert len([ref for ref in closing if ref["position"] == "closing"]) <= 6


def test_dialogue_heavy_context_fills_the_dialogue_quota() -> None:
    _book_id, profile_id, _ = _seed_full("dialogue")
    context = "\n".join(["“你去不去？”她问。", "“去。”他说。", "“那就走。”", "“怎么了？”", "“没什么。”他站起来。"])
    _fragments, _stats, refs = _render(profile_id, seed="scene-D", context=context)
    assert sum(1 for ref in refs if ref["paragraph_type"] == "dialogue") >= 6


def test_small_books_fall_back_to_the_evidence_quote_path() -> None:
    from tests.test_style_reference_injection_v2 import _seed_full as _seed_tiny

    _book_id, profile_id = _seed_tiny("ei_tiny")
    fragments, stats, refs = _render(profile_id, seed="scene-T")
    assert stats["few_shot_windows"] >= 1 and fragments.few_shot_block
    assert refs == []  # 24 段的小书凑不出 12 个窗口:证据引文路径,不记窗口引用


def test_frozen_contract_uses_the_index_only_while_the_root_matches() -> None:
    project_id = "ei_proj_frozen"
    book_id, profile_id, _binding_id = _seed_full("frozen", scope_ref_id=project_id)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        binding = repo.get_binding("ei_bind_frozen")
        contract = build_style_runtime_contract(repo, [binding], task_type="scene_generation")
        assert contract is not None
        context = extract_style_generation_context("她在门外停步。", source_kind="generation_source")
        svc = InjectionService(session)
        svc.few_shot_seed = "scene-F"
        intact = svc.fragments_for_contract(contract, project_id=project_id, context=context)
        assert len(svc.last_few_shot_window_refs) == 12
        assert svc.last_runtime_audit is not None
        assert len(svc.last_runtime_audit["few_shot_window_refs"]) == 12
        # 篡改第 12 章的一段:根哈希失配 → 退回证据引文路径(只用冻结的引文父段 ± 相邻段)
        far = repo.get_paragraph("ei_p_frozen_0200")
        far.text = "这一段在冻结之后被人改过了。"
        session.flush()
        tampered_svc = InjectionService(session)
        tampered_svc.few_shot_seed = "scene-F"
        tampered = tampered_svc.fragments_for_contract(contract, project_id=project_id, context=context)
        assert tampered_svc.last_few_shot_window_refs == []
        assert tampered.few_shot_block and "被人改过了" not in tampered.few_shot_block
    assert intact.few_shot_block != tampered.few_shot_block


# ---------------------------------------------------------------------------
# 合成期写入 + 预览端点
# ---------------------------------------------------------------------------


def test_synthesis_writes_the_exemplar_window_index(session) -> None:
    from novel_system.services.style_reference.ingest import IngestService
    from novel_system.services.style_reference.profile_synthesizer import ProfileSynthesizer
    from tests.test_style_reference_structure import _synthesis_fake

    text = "\n\n".join(row["text"] for row in _synthetic_rows())
    result = IngestService(session, llm_enabled=False).ingest_upload(
        raw_bytes=text.encode("utf-8"),
        file_name="ei_synth.txt",
        title="合成十二章",
        author_label="作者",
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )
    book_id = result.book.book_id
    repo = StyleReferenceRepository(session)
    run_id = "ei_syn_run"
    repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
    repo.create_extraction(
        extraction_id="ei_syn_ext", book_id=book_id, run_id=run_id, layer="scene", sub_dimension="scene.dialogue",
        raw_payload_json={}, status="done", validation_errors_json=[], purpose="extract",
    )
    repo.create_finding(
        finding_id="ei_syn_find", book_id=book_id, run_id=run_id, extraction_id="ei_syn_ext",
        sub_dimension="scene.dialogue", finding_kind="observation", statement="对白短促，常以反问收束",
        confidence="high", status="approved",
    )
    session.commit()
    profile = ProfileSynthesizer(session, llm_client=_synthesis_fake(), llm_enabled=True).synthesize(book_id, run_id)
    session.commit()
    index = profile.profile_json["exemplar_windows"]
    assert index["version"] == "exemplar_windows_v2"
    assert index["chapter_count"] == 12 and index["window_count"] >= 12
    assert index["paragraph_count"] == len(repo.list_paragraphs(book_id))


def test_preview_endpoint_accepts_scene_id_and_returns_window_refs(client) -> None:
    _book_id, profile_id, _ = _seed_full("preview")
    body = {"strategy": "mixed", "intensity": 100, "scene_id": "scene-preview-1"}
    resp = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["stats"]["few_shot_windows"] == 12
    assert len(data["window_refs"]) == 12
    assert {"start", "end", "chapter", "position", "paragraph_type", "paragraphs", "chars"} <= set(data["window_refs"][0])
    other = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json={**body, "scene_id": "scene-preview-2"})
    assert other.status_code == 200
    assert other.json()["data"]["window_refs"] != data["window_refs"]
    unseeded = client.post(f"{PREFIX}/profiles/{profile_id}/injection-preview", json={"strategy": "mixed", "intensity": 100})
    assert unseeded.status_code == 200 and len(unseeded.json()["data"]["window_refs"]) == 12
