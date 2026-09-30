from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

from novel_system.env_config import DEFAULT_PROVIDER_ATTEMPT_BUDGET
from novel_system.db import models  # noqa: F401 - register every mapped table
from novel_system.db.base import Base


ACCOUNTING_STATUSES = {
    "reserved",
    "settled",
    "failed",
    "released",
    "rejected",
    "usage_exceeds_reservation",
}
DISPATCH_KINDS = {
    "initial",
    "transport_retry",
    "response_parse_retry",
    "api_mode_degrade",
    "structured_output_degrade",
    "missing_text_degrade",
    "system_probe",
}


def _check_sql(table_name: str) -> dict[str, str]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _indexes(table_name: str) -> dict[str, Index]:
    return {index.name: index for index in Base.metadata.tables[table_name].indexes}


def test_scene_run_state_has_durable_budget_and_checkpoint_columns() -> None:
    table = Base.metadata.tables["scene_run_states"]

    expected = {
        "scene_tokens_reserved": (False, "0"),
        "scene_budget_basis_json": (True, None),
        "provider_attempts_used": (False, "0"),
        "provider_attempt_budget": (False, str(DEFAULT_PROVIDER_ATTEMPT_BUDGET)),
        "active_execution_id": (True, None),
        "run_execution_status": (True, None),
        "run_checkpoint": (True, None),
        "run_checkpoint_json": (True, None),
        "active_run_job_id": (True, None),
    }
    for name, (nullable, server_default) in expected.items():
        column = table.c[name]
        assert column.nullable is nullable
        actual_default = None if column.server_default is None else str(column.server_default.arg)
        assert actual_default == server_default

    assert table.c.provider_attempt_budget.default.arg == DEFAULT_PROVIDER_ATTEMPT_BUDGET
    checks = _check_sql("scene_run_states")
    assert checks["ck_scene_run_states_tokens_reserved_nonnegative"] == "scene_tokens_reserved >= 0"
    assert checks["ck_scene_run_states_provider_attempts_used_nonnegative"] == "provider_attempts_used >= 0"
    assert checks["ck_scene_run_states_provider_attempt_budget_nonnegative"] == "provider_attempt_budget >= 0"


def test_llm_call_has_durable_scope_budget_and_accounting_columns() -> None:
    table = Base.metadata.tables["llm_calls"]

    for name in ("scope_type", "scope_id", "accounting_status"):
        assert table.c[name].nullable is False
    for name in ("estimated_tokens", "reserved_tokens", "budget_charged_tokens"):
        column = table.c[name]
        assert column.nullable is False
        assert str(column.server_default.arg) == "0"
    assert table.c.usage_is_estimate.nullable is False
    assert str(table.c.usage_is_estimate.server_default.arg).lower() in {"1", "true"}

    checks = _check_sql("llm_calls")
    for name in ("estimated_tokens", "reserved_tokens", "budget_charged_tokens"):
        assert checks[f"ck_llm_calls_{name}_nonnegative"] == f"{name} >= 0"
    assert (
        checks["ck_llm_calls_budget_charged_within_reservation"]
        == "budget_charged_tokens <= reserved_tokens"
    )
    for status in ACCOUNTING_STATUSES:
        assert repr(status) in checks["ck_llm_calls_accounting_status"]

    indexes = _indexes("llm_calls")
    assert tuple(indexes["ix_llm_calls_scope_created"].columns.keys()) == (
        "scope_type",
        "scope_id",
        "created_at",
    )
    assert tuple(indexes["ix_llm_calls_run_job"].columns.keys()) == ("run_job_id",)
    assert tuple(indexes["ix_llm_calls_execution_step"].columns.keys()) == (
        "execution_id",
        "execution_step_key",
    )
    assert tuple(indexes["ix_llm_calls_accounting_status"].columns.keys()) == (
        "accounting_status",
    )


def test_llm_call_attempt_is_an_independent_non_cascading_audit_row() -> None:
    table = Base.metadata.tables["llm_call_attempts"]
    expected_columns = {
        "attempt_id",
        "llm_call_id",
        "provider_attempt_no",
        "dispatch_kind",
        "request_max_output_tokens",
        "provider_request_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "usage_is_estimate",
        "accounting_status",
        "request_dispatched_at",
        "settled_at",
        "latency_ms",
        "error_code",
        "error_text",
        "created_at",
    }
    assert expected_columns == set(table.c.keys())
    assert table.c.attempt_id.primary_key is True
    assert table.c.llm_call_id.nullable is False
    assert table.c.provider_attempt_no.nullable is False

    foreign_key = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert tuple(foreign_key.column_keys) == ("llm_call_id",)
    assert foreign_key.elements[0].target_fullname == "llm_calls.llm_call_id"
    assert foreign_key.elements[0].ondelete is None

    unique = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_llm_call_attempts_call_ordinal"
    )
    assert tuple(unique.columns.keys()) == ("llm_call_id", "provider_attempt_no")

    checks = _check_sql("llm_call_attempts")
    for name in (
        "provider_attempt_no",
        "request_max_output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "latency_ms",
    ):
        assert checks[f"ck_llm_call_attempts_{name}_nonnegative"] == f"{name} >= 0"
    assert (
        checks["ck_llm_call_attempts_budget_charged_within_reservation"]
        == "budget_charged_tokens <= reserved_tokens"
    )
    for status in ACCOUNTING_STATUSES:
        assert repr(status) in checks["ck_llm_call_attempts_accounting_status"]
    for dispatch_kind in DISPATCH_KINDS:
        assert repr(dispatch_kind) in checks["ck_llm_call_attempts_dispatch_kind"]

    indexes = _indexes("llm_call_attempts")
    assert tuple(indexes["ix_llm_call_attempts_call_status"].columns.keys()) == (
        "llm_call_id",
        "accounting_status",
    )


def test_chapter_run_job_has_authoritative_scene_history_index() -> None:
    table = Base.metadata.tables["chapter_run_jobs"]

    assert table.c.scene_id.nullable is True
    indexes = _indexes("chapter_run_jobs")
    assert tuple(indexes["ix_chapter_run_jobs_scene_created"].columns.keys()) == (
        "scene_id",
        "created_at",
    )


def test_all_production_llm_call_constructors_declare_queryable_scope() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "novel_system"
    missing: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if function_name != "LlmCall":
                continue
            keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
            absent = {"scope_type", "scope_id"} - keywords
            if absent:
                relative = path.relative_to(source_root.parent.parent)
                missing.append(f"{relative}:{node.lineno}:{','.join(sorted(absent))}")

    assert missing == []
