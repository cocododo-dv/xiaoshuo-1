"""运维工具的共用骨架（tools/_cli.py，B12-17）：库结构版本对不上时 ``--execute`` 拒写，干跑照常。

以前写库的工具不看 ``alembic_version``：代码比库新（或库比代码新）时照样写，写进的是一个与代码对不上的库。
"""

from __future__ import annotations

import pytest
import yaml

from novel_system.db.models import StyleReferenceBook
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from novel_system.db.session import SessionLocal
from novel_system.services.system_config import SystemConfigService
from novel_system.tools import purge_style_reference_books, raise_llm_output_budget, reset_author_state
from novel_system.tools._cli import REFUSED_EXIT_CODE, schema_revision_problem
from tests.style_reference_factories import make_book
from tests.support.schema import stamp_schema_revision


def _book(book_id: str) -> None:
    with SessionLocal() as session:
        make_book(session, book_id, paragraphs=["雨城的旧信。"], title="旧信")
        session.commit()


def test_execute_refuses_to_write_when_the_database_is_behind_the_code(capsys) -> None:
    stamp_schema_revision("20260716_0072")
    _book("v2_book_behind")

    with pytest.raises(SystemExit) as refused:
        purge_style_reference_books.main(["--id-prefix", "v2_book_", "--execute"])

    assert refused.value.code == REFUSED_EXIT_CODE
    assert "20260716_0072" in capsys.readouterr().err
    with SessionLocal() as session:
        assert session.get(StyleReferenceBook, "v2_book_behind") is not None


def test_dry_run_still_reads_a_database_that_is_behind(capsys) -> None:
    stamp_schema_revision("20260716_0072")
    _book("v2_book_dry")

    assert purge_style_reference_books.main(["--id-prefix", "v2_book_"]) == 0
    assert "v2_book_dry" in capsys.readouterr().out


def test_a_database_without_a_revision_is_refused_too(capsys) -> None:
    with pytest.raises(SystemExit) as refused:
        reset_author_state.main(["--execute", "--yes"])

    assert refused.value.code == REFUSED_EXIT_CODE
    assert "alembic upgrade head" in capsys.readouterr().err


def test_matching_revision_passes_the_check() -> None:
    stamp_schema_revision()
    with SessionLocal() as session:
        assert schema_revision_problem(session) is None
    with SessionLocal() as session:
        stamp_schema_revision("20260716_0072")
        problem = schema_revision_problem(session)
    assert problem is not None and CURRENT_SCHEMA_REVISION in problem


def test_snapshot_tools_refuse_before_touching_the_active_snapshot(capsys) -> None:
    stamp_schema_revision()
    with SessionLocal() as session:
        service = SystemConfigService(session)
        payload = {
            "model_profiles": {"quality_strong": {"description": "d", "provider": "openai_compatible", "model": "m"}},
            "task_routing": {"snowflake_step_generate": {"provider": "openai_compatible", "model": "m", "max_output_tokens": 100}},
        }
        created = service.create_draft(category="models", yaml_raw=yaml.safe_dump(payload), secrets=None, actor_ref="test")
        service.activate(created["snapshot"]["snapshot_id"], actor_ref="test")
        session.commit()
        before = service.overview()["categories"]["models"]["active_snapshot"]["snapshot_id"]
    stamp_schema_revision("20260716_0072")

    with pytest.raises(SystemExit):
        raise_llm_output_budget.main(["--execute"])

    with SessionLocal() as session:
        after = SystemConfigService(session).overview()["categories"]["models"]["active_snapshot"]["snapshot_id"]
    assert after == before
