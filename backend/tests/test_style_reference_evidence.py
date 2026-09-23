from __future__ import annotations

from pathlib import Path

from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.style_reference.evidence import (
    align_evidence_to_paragraph_lookup,
    align_paragraph_evidence,
)
from novel_system.services.style_reference.schemas import ExtractionEvidenceInput
from novel_system.services.style_reference.text_utils import compact_ws


def _evidence(quote: str, span: tuple[int, int] | None) -> ExtractionEvidenceInput:
    return ExtractionEvidenceInput(
        paragraph_id="p1",
        span=span,
        quote=quote,
        anchor_kind="paragraph_quote",
    )


def test_compacted_whitespace_quote_is_mapped_back_to_raw_text() -> None:
    raw = "\n  夜色  很深。\n河水\t发亮。  "
    quote = "夜色 很深。 河水 发亮。"

    aligned = align_paragraph_evidence(_evidence(quote, (0, len(quote))), raw)

    assert aligned is not None
    assert compact_ws(aligned.quote) == quote
    assert aligned.span is not None
    assert raw[slice(*aligned.span)] == aligned.quote


def test_unique_quote_with_invalid_zero_span_is_safely_realigned() -> None:
    raw = "甲走了。乙留下。"

    aligned = align_paragraph_evidence(_evidence("乙留下", (0, 0)), raw)

    assert aligned is not None
    assert aligned.span == (raw.index("乙留下"), raw.index("乙留下") + len("乙留下"))
    assert aligned.quote == "乙留下"


def test_non_contiguous_ellipsis_summary_is_rejected() -> None:
    raw = "甲走了。风吹过院子。乙留下。"

    assert align_paragraph_evidence(_evidence("甲走了……乙留下", (0, 0)), raw) is None


def test_ambiguous_short_quote_requires_a_matching_span() -> None:
    raw = "他点头。他又点头。"

    assert align_paragraph_evidence(_evidence("点头", (0, 0)), raw) is None

    second = raw.rindex("点头")
    aligned = align_paragraph_evidence(
        _evidence("点头", (second, second + len("点头"))),
        raw,
    )
    assert aligned is not None
    assert aligned.span == (second, second + len("点头"))


def test_aligned_quotes_are_canonical_raw_slices_even_past_the_old_600_char_view() -> None:
    """学习作业送整段:对齐器按调用方给的可见长度核对,引文坐标是库里原文的坐标。"""
    raw = "  一盏灯\n慢慢地亮。" + "雨下个不停。" * 120 + "另一扇门\t悄悄合上。"
    lookup = {"p1": raw}
    first = align_evidence_to_paragraph_lookup(_evidence("一盏灯 慢慢地亮", (0, 0)), lookup, prompt_char_limit=100_000)
    late = align_evidence_to_paragraph_lookup(_evidence("另一扇门 悄悄合上", (0, 0)), lookup, prompt_char_limit=100_000)
    assert first is not None and late is not None
    for item in (first, late):
        assert item.span is not None and raw[slice(*item.span)] == item.quote
    assert compact_ws(late.quote) == "另一扇门 悄悄合上"
    # 旧的 600 字可见窗口外的引文对不上(模型没看到那一段)
    assert align_evidence_to_paragraph_lookup(_evidence("另一扇门 悄悄合上", (0, 0)), lookup) is None


def test_missing_paragraph_id_is_recovered_only_from_a_unique_paragraph() -> None:
    evidence = _evidence("风把门推开", (0, 0)).model_copy(
        update={"paragraph_id": None}
    )

    aligned = align_evidence_to_paragraph_lookup(
        evidence,
        {"p1": "屋里没有人。", "p2": "风把门推开。灯灭了。"},
    )

    assert aligned is not None
    assert aligned.paragraph_id == "p2"
    assert aligned.quote == "风把门推开"


def test_missing_paragraph_id_stays_rejected_when_quote_matches_multiple_paragraphs() -> None:
    evidence = _evidence("他点头", (0, 0)).model_copy(update={"paragraph_id": None})

    assert (
        align_evidence_to_paragraph_lookup(
            evidence,
            {"p1": "他点头。", "p2": "门边，他点头。"},
        )
        is None
    )


def test_wrong_paragraph_id_is_repaired_only_when_quote_is_unique_across_lookup() -> None:
    evidence = _evidence("孔乙己低声说道", (0, 0)).model_copy(
        update={"paragraph_id": "wrong"}
    )

    aligned = align_evidence_to_paragraph_lookup(
        evidence,
        {"wrong": "别人在说话。", "right": "孔乙己低声说道：跌断，跌，跌。"},
    )

    assert aligned is not None
    assert aligned.paragraph_id == "right"


def test_quote_mark_variants_are_canonicalized_to_the_raw_paragraph() -> None:
    raw = "他只答了一句：“是的。”便不再开口。"
    evidence = _evidence("'是的。'", (0, 0))

    aligned = align_paragraph_evidence(evidence, raw)

    assert aligned is not None
    assert aligned.quote == "“是的。”"
    assert aligned.span is not None
    assert raw[slice(*aligned.span)] == aligned.quote


def test_all_extraction_prompts_require_contiguous_verbatim_evidence() -> None:
    config_path = Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"
    templates = load_prompt_templates(config_path)

    for name in (
        "style_ref_extract_language",
        "style_ref_extract_narrative",
        "style_ref_extract_scene",
        "style_ref_extract_theme",
    ):
        prompt = templates[name].system_prompt
        assert "连续、逐字一致" in prompt
        assert "【p】" in prompt and "不拼接" in prompt and "不加省略号" in prompt
    assert "style_ref_supplement_evidence" not in templates
