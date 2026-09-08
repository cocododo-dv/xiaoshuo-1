"""LanguageExtractor + NarrativeExtractor + SceneExtractor + ThemeExtractor
抽取 happy path 与边界(PR-3 + PR-6)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.extractors import (
    LanguageExtractor,
    NarrativeExtractor,
    SceneExtractor,
    ThemeExtractor,
)
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.repository import StyleReferenceRepository


SAMPLE_TEXT = """第一段是一段比较长的叙述文字,描绘清晨雾气弥漫的街口与零星行人的脚步声,气氛安静而略带寒意。

他说:"今天天气真好,看来要出太阳了。"

我心里想着昨天的事情,觉得一阵难以言说的不安,暗忖该如何回应他。

记得那年她还在的时候,我们一起去过河边,看稻穗在风中起伏,鸭子在水里游动。

天空忽然暗了下来。

他低头看着脚下的路,脸色一阵苍白,眼神疲惫。

她转身,推开木门,跑出院子,沿着小路一路向前。

屋外的山脚下,雪花静静地飘落,覆盖了远处的村庄、树林和河面。
"""


def _ingest_book(book_id_seed: str = "x") -> str:
    """便利函数:用 IngestService 在当前 session 中导入一份 placeholder。"""
    with SessionLocal() as session:
        service = IngestService(session, llm_enabled=False)
        result = service.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name=f"sample_{book_id_seed}.txt",
            title="测试样本",
            author_label="测试",
            cloud_policy="local_only",
        )
        session.commit()
        return result.book.book_id


def _make_run(book_id: str) -> str:
    """便利函数:建一个 run 行供 extractor 关联。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        run_id = "sr_run_test_1"
        repo.create_run(
            run_id=run_id,
            book_id=book_id,
            status="running",
            phase="extract",
            coverage_json={},
        )
        session.commit()
    return run_id


# ---------------------------------------------------------------------------
# LanguageExtractor happy path
# ---------------------------------------------------------------------------


def test_language_extractor_happy_path(fake_extractor_llm) -> None:
    book_id = _ingest_book("lang_happy")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")

    with SessionLocal() as session:
        extractor = LanguageExtractor(
            session, client, run_id=run_id, book_id=book_id
        )
        results = extractor.extract_all_sub_dimensions()
        session.commit()

    assert len(results) == 4, "language 层 4 sub_dim"
    # 每 sub_dim default rule 应产出 3 obs + 1 forbid
    for r in results:
        kinds = [f.finding_kind for f in r.findings]
        observations = [k for k in kinds if k.value == "observation"]
        forbids = [k for k in kinds if k.value == "forbidden_pattern"]
        assert len(observations) == 3, f"{r.sub_dimension.value} obs count {len(observations)}"
        assert len(forbids) == 1, f"{r.sub_dimension.value} forbid count {len(forbids)}"


def test_narrative_extractor_happy_path(fake_extractor_llm) -> None:
    book_id = _ingest_book("narr_happy")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")

    with SessionLocal() as session:
        extractor = NarrativeExtractor(
            session, client, run_id=run_id, book_id=book_id
        )
        results = extractor.extract_all_sub_dimensions()
        session.commit()

    assert len(results) == 4, "narrative 层 4 sub_dim"
    # 验证写入了 findings 表
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        findings = repo.list_findings(book_id=book_id)
        # 4 sub_dim * 4 findings = 16
        assert len(findings) == 16


def test_scene_extractor_happy_path(fake_extractor_llm) -> None:
    """PR-6:SceneExtractor 跑 4 sub_dim 各产出 4 findings。"""
    book_id = _ingest_book("scene_happy")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")

    with SessionLocal() as session:
        extractor = SceneExtractor(session, client, run_id=run_id, book_id=book_id)
        results = extractor.extract_all_sub_dimensions()
        session.commit()

    assert len(results) == 4, "scene 层 4 sub_dim"
    expected_sub_dims = {
        "scene.environment",
        "scene.character_portrayal",
        "scene.dialogue",
        "scene.sensory_priority",
    }
    assert {r.sub_dimension.value for r in results} == expected_sub_dims


def test_theme_extractor_happy_path(fake_extractor_llm) -> None:
    """PR-6:ThemeExtractor 跑 4 sub_dim 各产出 4 findings。"""
    book_id = _ingest_book("theme_happy")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")

    with SessionLocal() as session:
        extractor = ThemeExtractor(session, client, run_id=run_id, book_id=book_id)
        results = extractor.extract_all_sub_dimensions()
        session.commit()

    assert len(results) == 4, "theme 层 4 sub_dim"
    expected_sub_dims = {
        "theme.emotional_tone",
        "theme.values",
        "theme.motifs",
        "theme.narrative_philosophy",
    }
    assert {r.sub_dimension.value for r in results} == expected_sub_dims


# ---------------------------------------------------------------------------
# BannedAdjective:命中即丢弃
# ---------------------------------------------------------------------------


def test_extractor_drops_banned_adjective_findings(fake_extractor_llm) -> None:
    book_id = _ingest_book("banned")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("all_banned")

    with SessionLocal() as session:
        extractor = LanguageExtractor(
            session, client, run_id=run_id, book_id=book_id
        )
        results = extractor.extract_all_sub_dimensions()
        session.commit()

    # 所有 obs 被丢弃 → findings 数 = 0
    for r in results:
        assert len(r.findings) == 0, (
            f"{r.sub_dimension.value} 应因 banned_adjective 全部丢弃,实际 {len(r.findings)}"
        )


# ---------------------------------------------------------------------------
# metrics_anchor 注入(LLM call_log 验证 user_msg 含 metrics_anchor 字段)
# ---------------------------------------------------------------------------


def test_metrics_anchor_injected_into_prompt(fake_extractor_llm, monkeypatch) -> None:
    book_id = _ingest_book("metrics_anchor")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")
    captured: list[str] = []

    original_generate_accounted = client.generate_accounted

    def _spy_generate_accounted(request, *, accounting_hook):  # noqa: ANN001
        captured.append(request.messages[-1]["content"])
        return original_generate_accounted(request, accounting_hook=accounting_hook)

    monkeypatch.setattr(client, "generate_accounted", _spy_generate_accounted)

    with SessionLocal() as session:
        extractor = LanguageExtractor(
            session, client, run_id=run_id, book_id=book_id
        )
        extractor.extract_all_sub_dimensions()
        session.commit()

    assert captured, "应至少有一次 LLM 调用"
    # extract_language 调用的 user_msg 必含 metrics_anchor 字段
    extract_calls = [
        c for c in captured if '"metrics_anchor"' in c
    ]
    assert extract_calls, "user_msg 应注入 metrics_anchor 字段"


def test_normalize_finding_item_tolerates_near_miss_output() -> None:
    """schema 降级模式下模型的近似输出(description / 字符串 span / 多余键)
    必须归一到可过 Pydantic 校验的形状;内容级校验不放松。"""
    from novel_system.services.style_reference.extractors.base import _normalize_finding_item

    item = _normalize_finding_item({
        "description": "以短句推进叙事。",
        "extra_field": "x",
        "evidence": [
            {"paragraph_id": "p1", "span": "他走了。", "quote": "他走了。", "source": "y"},
            {"paragraph_id": "p2", "span": [0, 4], "quote": "雨停了。"},
        ],
    })
    assert item["statement"] == "以短句推进叙事。"
    assert "extra_field" not in item and "description" not in item
    assert item["evidence"][0]["span"] is None
    assert "source" not in item["evidence"][0]
    assert item["evidence"][1]["span"] == [0, 4]

    # statement 已存在时不被别名覆盖
    item2 = _normalize_finding_item({"statement": "原句。", "description": "别名。", "evidence": []})
    assert item2["statement"] == "原句。"


# ---------------------------------------------------------------------------
# v2(风格模仿 v2 · W2):全书锚点 / payload 顺序字段 / 定种 rng / full_retry 失败保留首抽
# ---------------------------------------------------------------------------


import json  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402

from novel_system.services.style_reference.dimensions import SubDimension  # noqa: E402
from novel_system.services.style_reference.extractors import ExtractionRetryPolicy  # noqa: E402
from novel_system.services.style_reference.extractors.base import (  # noqa: E402
    _ExtractLLMError,
    _PartialResult,
    _build_short_finding,
)
from novel_system.services.style_reference.sampling import derive_extraction_rng  # noqa: E402
from novel_system.services.style_reference.schemas import (  # noqa: E402
    ExtractionPurpose,
    FindingKind,
)

_ANCHOR_SUBSET = (
    "avg_sentence_length",
    "sentence_length_std",
    "short_sentence_ratio",
    "long_sentence_ratio",
)


def _payload_of(user_msg: str) -> dict:
    opening = re.search(r"\[UNTRUSTED_REFERENCE_DATA:[^\]]+\]\n", user_msg)
    assert opening is not None
    closing = user_msg.find("\n[/UNTRUSTED_REFERENCE_DATA]", opening.end())
    assert closing > 0
    return json.loads(user_msg[opening.end():closing])


def test_metrics_anchor_equals_book_level_stats(fake_extractor_llm) -> None:
    """锚点读 book.stats_json.metrics(全书真值),与送入的样本子集无关。"""
    book_id = _ingest_book("anchor_book")
    run_id = _make_run(book_id)
    with SessionLocal() as session:
        book = StyleReferenceRepository(session).get_book(book_id)
        extractor = LanguageExtractor(
            session, fake_extractor_llm("default"), run_id=run_id, book_id=book_id
        )
        paragraphs = extractor._sample_paragraphs(SubDimension.LANGUAGE_SENTENCE_STRUCTURE)
        anchor = extractor._build_metrics_anchor(
            paragraphs[:2], SubDimension.LANGUAGE_SENTENCE_STRUCTURE
        )
    assert anchor["anchor_scope"] == "book"
    assert set(anchor) == set(_ANCHOR_SUBSET) | {"anchor_scope"}
    for name in _ANCHOR_SUBSET:
        assert anchor[name]["mean"] == pytest.approx(book.stats_json["metrics"][name]["mean"])
        assert anchor[name]["std"] == pytest.approx(book.stats_json["metrics"][name]["std"])


def test_metrics_anchor_falls_back_to_sample_when_book_stats_missing(fake_extractor_llm) -> None:
    book_id = _ingest_book("anchor_sample")
    run_id = _make_run(book_id)
    with SessionLocal() as session:
        book = StyleReferenceRepository(session).get_book(book_id)
        book.stats_json = {
            k: v for k, v in (book.stats_json or {}).items()
            if k not in ("metrics", "prose_shape_metrics")
        }
        session.flush()
        extractor = LanguageExtractor(
            session, fake_extractor_llm("default"), run_id=run_id, book_id=book_id
        )
        paragraphs = extractor._sample_paragraphs(SubDimension.LANGUAGE_SENTENCE_STRUCTURE)
        anchor = extractor._build_metrics_anchor(
            paragraphs, SubDimension.LANGUAGE_SENTENCE_STRUCTURE
        )
    assert anchor["anchor_scope"] == "sample"
    assert set(anchor) == set(_ANCHOR_SUBSET) | {"anchor_scope"}


def test_extract_payload_carries_paragraph_index_and_window_fields(
    fake_extractor_llm, monkeypatch
) -> None:
    """language 层每段带 paragraph_index 且按序;narrative 层(窗口层)再带
    window_id / window_position(first / middle / last)。"""
    book_id = _ingest_book("payload_fields")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")
    captured: dict[str, dict] = {}
    original = client.generate_accounted

    def _spy(request, *, accounting_hook):  # noqa: ANN001
        node_id = getattr(request, "node_id", "")
        if node_id not in captured and "extract" in node_id:
            captured[node_id] = _payload_of(request.messages[-1]["content"])
        return original(request, accounting_hook=accounting_hook)

    monkeypatch.setattr(client, "generate_accounted", _spy)
    with SessionLocal() as session:
        LanguageExtractor(session, client, run_id=run_id, book_id=book_id).extract_all_sub_dimensions()
        NarrativeExtractor(session, client, run_id=run_id, book_id=book_id).extract_all_sub_dimensions()
        session.commit()

    lang = captured[LanguageExtractor.extract_node_id]
    assert lang["metrics_anchor"]["anchor_scope"] == "book"
    assert lang["paragraphs"]
    assert all("paragraph_index" in p and "window_id" not in p for p in lang["paragraphs"])
    indices = [p["paragraph_index"] for p in lang["paragraphs"]]
    assert indices == sorted(indices)

    narr = captured[NarrativeExtractor.extract_node_id]
    assert len(narr["paragraphs"]) >= 3
    # 8 段小样本整本进样本 → 一个连续窗口
    assert {p["window_id"] for p in narr["paragraphs"]} == {"w01"}
    positions = [p["window_position"] for p in narr["paragraphs"]]
    assert positions[0] == "first" and positions[-1] == "last"
    assert set(positions[1:-1]) == {"middle"}
    narr_indices = [p["paragraph_index"] for p in narr["paragraphs"]]
    assert narr_indices == list(range(narr_indices[0], narr_indices[0] + len(narr_indices)))


def test_extractor_rng_is_seeded_from_checksum_and_run_id() -> None:
    book_id = _ingest_book("rng_seed")
    run_id = _make_run(book_id)
    with SessionLocal() as session:
        book = StyleReferenceRepository(session).get_book(book_id)
        first = LanguageExtractor(session, None, run_id=run_id, book_id=book_id)
        second = LanguageExtractor(session, None, run_id=run_id, book_id=book_id)
        expected = derive_extraction_rng(book.text_checksum, run_id)
        assert first.rng.random() == second.rng.random() == expected.random()
        other_run = LanguageExtractor(session, None, run_id="sr_run_other", book_id=book_id)
        assert other_run.rng.random() != derive_extraction_rng(book.text_checksum, run_id).random()
        injected = random.Random(5)
        assert LanguageExtractor(
            session, None, run_id=run_id, book_id=book_id, rng=injected
        ).rng is injected


def test_full_retry_llm_failure_keeps_validated_first_pass_findings(
    fake_extractor_llm, monkeypatch
) -> None:
    """Step 3:整维重抽的 LLM 调用失败时,保留首抽已通过校验的 findings,不抛、不整 run FAILED。"""
    book_id = _ingest_book("full_retry_fail")
    run_id = _make_run(book_id)
    client = fake_extractor_llm("default")
    calls: dict[str, int] = {}
    with SessionLocal() as session:
        extractor = LanguageExtractor(
            session,
            client,
            run_id=run_id,
            book_id=book_id,
            retry_policy=ExtractionRetryPolicy(max_targeted_retries=0, max_full_retries=1),
        )
        original = extractor._extract_once

        def _scripted(sub_dim, *args, purpose, **kwargs):  # noqa: ANN001, ANN202
            calls[sub_dim.value] = calls.get(sub_dim.value, 0) + 1
            if purpose == ExtractionPurpose.EXTRACT:
                findings = original(sub_dim, *args, purpose=purpose, **kwargs)
                short = _build_short_finding(
                    {"statement": "证据不足的观察", "confidence": "medium", "evidence": []},
                    sub_dim,
                    FindingKind.OBSERVATION,
                )
                raise _PartialResult(findings=findings, failed=[short])
            raise _ExtractLLMError("provider truncated the retry")

        monkeypatch.setattr(extractor, "_extract_once", _scripted)
        results = extractor.extract_all_sub_dimensions()
        session.commit()

    assert len(results) == 4
    assert all(len(r.findings) == 4 for r in results), "首抽 3 obs + 1 forbid 全部保留"
    assert set(calls.values()) == {2}, "首抽 + 一次 full_retry"
    with SessionLocal() as session:
        rows = StyleReferenceRepository(session).list_extractions(run_id=run_id)
        assert len(rows) == 4
        assert {row.purpose for row in rows} == {"extract"}
        assert len(StyleReferenceRepository(session).list_findings(run_id=run_id)) == 16
