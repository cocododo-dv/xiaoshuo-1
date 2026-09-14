"""2026-09-14:refresh_style_reference_books——对已导入的书就地剥离副文本、重算统计、记符号场界。"""

from __future__ import annotations

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.tools.refresh_style_reference_books import apply_book_refresh, main, plan_book_refresh

_PROSE = "他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯。"


def _seed_book_with_legacy_rows(session) -> str:
    text = "\n\n".join(["第一章 灯下", *[f"{_PROSE}{i}" for i in range(12)]])
    result = IngestService(session, llm_enabled=False).ingest_upload(
        raw_bytes=text.encode("utf-8"),
        file_name="legacy.txt",
        title="旧书",
        author_label="作者",
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )
    book_id = result.book.book_id
    repo = StyleReferenceRepository(session)
    # 模拟修补前导入留下的行:一条脚注、一条站点声明、一条纯符号分隔行,以及引用脚注的引文
    for index, (ptype, body) in enumerate(
        [
            ("narration", "[1] 这是一条脚注，不是作者的文字。"),
            ("narration", "声明：本书为某某电子书的用户上传至本站，本站只提供电子书存储服务以及免费下载。"),
            ("transition", "***"),
        ],
        start=100,
    ):
        repo.create_paragraph(
            paragraph_id=f"legacy_p_{index}",
            book_id=book_id,
            paragraph_index=index,
            paragraph_type=ptype,
            start_offset=0,
            end_offset=len(body),
            text=body,
            char_count=len(body),
            classifier_confidence=0.5,
        )
    repo.create_run(run_id="legacy_run", book_id=book_id, status="done", phase="done")
    repo.create_quote(
        quote_id="legacy_quote",
        book_id=book_id,
        paragraph_id="legacy_p_100",
        span_start=0,
        span_end=6,
        quote_text="[1] 这是",
        illustrates_dims=["language.rhythm"],
        extracted_features={},
    )
    session.commit()
    return book_id


def test_plan_and_apply_strip_paratext_and_recompute_stats(session) -> None:
    book_id = _seed_book_with_legacy_rows(session)
    plan = plan_book_refresh(session, book_id)
    assert plan is not None
    assert [p.paragraph_id for p in plan["paratext"]] == ["legacy_p_100", "legacy_p_101"]
    assert plan["scene_breaks"] == [102]
    assert [q.quote_id for q in plan["quotes_to_detach"]] == ["legacy_quote"]
    assert plan["kept_count"] == plan["paragraph_count"] - 2
    # 干跑不写库
    repo = StyleReferenceRepository(session)
    assert len(repo.list_paragraphs(book_id)) == plan["paragraph_count"]

    apply_book_refresh(session, plan)
    session.commit()
    remaining = {p.paragraph_id for p in repo.list_paragraphs(book_id)}
    assert "legacy_p_100" not in remaining and "legacy_p_101" not in remaining
    assert "legacy_p_102" in remaining
    quote = repo.list_quotes_by_ids(["legacy_quote"])[0]
    assert quote.paragraph_id is None
    stats = repo.get_book(book_id).stats_json
    assert stats["scene_breaks"] == [102]
    assert stats["paratext_dropped"] == 2
    assert stats["refresh"]["paragraphs_removed"] == 2 and stats["refresh"]["quotes_detached"] == 1
    assert stats["metrics"]["classical_word_ratio"]["sample_count"] == plan["kept_count"]
    assert stats["voice_signature"]["features"]["person_third_share"] >= 0.0


def test_cli_dry_run_then_execute(session, capsys) -> None:
    book_id = _seed_book_with_legacy_rows(session)
    assert main(["--book", book_id]) == 0
    out = capsys.readouterr().out
    assert "干跑" in out and "副文本 2 段" in out
    with SessionLocal() as other:
        assert len(StyleReferenceRepository(other).list_paragraphs(book_id)) == 16
    assert main(["--book", book_id, "--execute"]) == 0
    assert "已刷新 1 本书" in capsys.readouterr().out
    with SessionLocal() as other:
        assert len(StyleReferenceRepository(other).list_paragraphs(book_id)) == 14
    assert main(["--book", "sr_book_missing"]) == 0
    assert "跳过" in capsys.readouterr().out
