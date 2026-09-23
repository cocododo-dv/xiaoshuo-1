"""purge_style_reference_books:删参考书与一切派生数据(2026-09-23 风格参考 v3 台账 L6 / I15)。

同时钉住 ``cleanup.purge_derived_data`` 现在也清这本书的作业行与窗口索引(两张表都有书的外键)。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    StyleReferenceWindow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.jobs import StyleJobService
from novel_system.tools.purge_style_reference_books import main, purge_book, select_books


def _seed_book(session, seed: str, *, book_id: str | None = None) -> str:
    text = "\n\n".join(f"{seed}的第{i}段,灯下的人把信折好又打开,终于没有寄出去。" for i in range(4))
    result = IngestService(session, llm_enabled=False).ingest_upload(
        raw_bytes=text.encode("utf-8"),
        file_name=f"{seed}.txt",
        title=seed,
        author_label=None,
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )
    session.flush()
    original = result.book.book_id
    if book_id is not None and book_id != original:
        # 夹具书的 id 形如 v2_book_*:把导入得到的书整本改名(段落行跟着改)
        session.execute(
            StyleReferenceParagraph.__table__.update()
            .where(StyleReferenceParagraph.book_id == original)
            .values(book_id=book_id)
        )
        session.execute(
            StyleReferenceBook.__table__.update().where(StyleReferenceBook.book_id == original).values(book_id=book_id)
        )
        original = book_id
    session.add(StyleReferenceRun(run_id=f"sr_run_{seed}", book_id=original, status="done", phase="done"))
    session.add(
        StyleReferenceProfile(
            profile_id=f"sr_profile_{seed}",
            book_id=original,
            run_id=f"sr_run_{seed}",
            title=seed,
            status="active",
            profile_json={},
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id=f"sr_bind_{seed}",
            profile_id=f"sr_profile_{seed}",
            scope="project",
            scope_ref_id=f"proj_{seed}",
            task_type="scene_generation",
            strategy="mixed",
            config_json={},
            status="active",
        )
    )
    session.add(
        StyleReferenceWindow(
            window_id=f"sr_win_{seed}",
            book_id=original,
            index_version="windows_v1",
            root_sha256="root",
            window_no=0,
            start_index=0,
            end_index=3,
        )
    )
    StyleJobService(session).create("classify", book_id=original, params={"mode": "retype"})
    session.commit()
    return original


def _count(session, model, column, value) -> int:
    return int(session.scalar(select(func.count()).select_from(model).where(column == value)) or 0)


def test_purge_book_removes_the_book_and_everything_derived(session) -> None:
    book_id = _seed_book(session, "单本")
    counts = purge_book(session, book_id)
    session.commit()
    assert counts["books"] == 1 and counts["paragraphs"] == 4
    assert counts["jobs"] == 1 and counts["windows"] == 1
    assert counts["profiles"] == 1 and counts["bindings"] == 1 and counts["runs"] == 1
    for model, column in (
        (StyleReferenceBook, StyleReferenceBook.book_id),
        (StyleReferenceParagraph, StyleReferenceParagraph.book_id),
        (StyleReferenceJob, StyleReferenceJob.book_id),
        (StyleReferenceWindow, StyleReferenceWindow.book_id),
        (StyleReferenceRun, StyleReferenceRun.book_id),
        (StyleReferenceProfile, StyleReferenceProfile.book_id),
    ):
        assert _count(session, model, column, book_id) == 0, model.__tablename__


def test_cli_selects_by_prefix_or_id_dry_runs_by_default_and_executes(session, capsys) -> None:
    kept = _seed_book(session, "作者的书")
    first = _seed_book(session, "夹具甲", book_id="v2_book_int_A")
    second = _seed_book(session, "夹具乙", book_id="v2_book_excl")
    assert [book.book_id for book in select_books(session, prefixes=["v2_book_"])] == [first, second]

    assert main(["--id-prefix", "v2_book_"]) == 0
    out = capsys.readouterr().out
    assert "干跑" in out and "v2_book_int_A" in out and "有 1 条生效绑定" in out
    with SessionLocal() as other:
        assert other.get(StyleReferenceBook, first) is not None  # 干跑不删

    assert main(["--id-prefix", "v2_book_", "--execute"]) == 0
    assert "已删除 2 本书" in capsys.readouterr().out
    with SessionLocal() as other:
        assert other.get(StyleReferenceBook, first) is None
        assert other.get(StyleReferenceBook, second) is None
        assert other.get(StyleReferenceBook, kept) is not None
        assert _count(other, StyleReferenceParagraph, StyleReferenceParagraph.book_id, kept) == 4

    assert main(["--book", kept, "--execute"]) == 0
    with SessionLocal() as other:
        assert other.get(StyleReferenceBook, kept) is None


@pytest.mark.parametrize("argv", [[], ["--execute"], ["--id-prefix", "v2"]])
def test_cli_refuses_an_implicit_or_too_broad_selection(argv, capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == 2


def test_purge_book_supersedes_planning_in_the_scope_of_its_active_binding(session, monkeypatch) -> None:
    """与书库「删除」同一个函数:删绑定之前,绑定范围内按这本参考做的规划产物作废。"""
    from novel_system.services import scene_planning_staleness

    calls: list[tuple[str, str, str]] = []

    def record(session_, *, scope, scope_ref_id, reason="style_binding_changed"):
        calls.append((scope, scope_ref_id, reason))
        return {}

    monkeypatch.setattr(scene_planning_staleness, "supersede_for_binding_scope", record)
    book_id = _seed_book(session, "作废")
    purge_book(session, book_id)
    session.commit()
    assert calls == [("project", "proj_作废", f"style_reference_book_deleted:{book_id}")]
