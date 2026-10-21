from __future__ import annotations

from sqlalchemy import inspect


def test_schema_creates_required_tables(session) -> None:
    inspector = inspect(session.bind)
    table_names = set(inspector.get_table_names())

    assert "chapter_goals" in table_names
    assert "scene_run_states" in table_names
    assert "idempotency_keys" in table_names
    assert "operation_logs" in table_names
