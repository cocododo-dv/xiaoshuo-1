"""LLM 记账：每次调用与每次尝试（预留 → 派发 → 结算）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(
            "estimated_tokens >= 0",
            name="ck_llm_calls_estimated_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_tokens >= 0",
            name="ck_llm_calls_reserved_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens >= 0",
            name="ck_llm_calls_budget_charged_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens <= reserved_tokens",
            name="ck_llm_calls_budget_charged_within_reservation",
        ),
        CheckConstraint(
            "accounting_status IN ('reserved','settled','failed','released','rejected','usage_exceeds_reservation')",
            name="ck_llm_calls_accounting_status",
        ),
        Index("ix_llm_calls_scene_created", "scene_id", "created_at"),
        Index("ix_llm_calls_scope_created", "scope_type", "scope_id", "created_at"),
        Index("ix_llm_calls_run_job", "run_job_id"),
        Index("ix_llm_calls_execution_step", "execution_id", "execution_step_key"),
        Index(
            "uq_llm_calls_execution_step_claim",
            "execution_id",
            "execution_step_key",
            unique=True,
            sqlite_where=text(
                "execution_id IS NOT NULL AND execution_step_key IS NOT NULL "
                "AND NOT (request_dispatched_at IS NULL "
                "AND accounting_status IN ('released','rejected'))"
            ),
            postgresql_where=text(
                "execution_id IS NOT NULL AND execution_step_key IS NOT NULL "
                "AND NOT (request_dispatched_at IS NULL "
                "AND accounting_status IN ('released','rejected'))"
            ),
        ),
        Index("ix_llm_calls_accounting_status", "accounting_status"),
        # 迁移 20260929_0096：成本看板按作品 / 章节过滤
        Index("ix_llm_calls_project_created", "project_id", "created_at"),
        Index("ix_llm_calls_chapter", "chapter_id"),
    )

    llm_call_id: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning_level: Mapped[str | None] = mapped_column(String, nullable=True)
    native_reasoning_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    credential_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    step: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_payload_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_payload_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    scope_type: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String)
    run_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_step_key: Mapped[str | None] = mapped_column(String, nullable=True)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    budget_charged_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    usage_is_estimate: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    accounting_status: Mapped[str] = mapped_column(
        String,
        default="reserved",
        server_default="reserved",
    )
    request_dispatched_at: Mapped[str | None] = mapped_column(String, nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class LlmCallAttempt(Base):
    __tablename__ = "llm_call_attempts"
    __table_args__ = (
        UniqueConstraint(
            "llm_call_id",
            "provider_attempt_no",
            name="uq_llm_call_attempts_call_ordinal",
        ),
        CheckConstraint(
            "provider_attempt_no >= 0",
            name="ck_llm_call_attempts_provider_attempt_no_nonnegative",
        ),
        CheckConstraint(
            "request_max_output_tokens >= 0",
            name="ck_llm_call_attempts_request_max_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "prompt_tokens >= 0",
            name="ck_llm_call_attempts_prompt_tokens_nonnegative",
        ),
        CheckConstraint(
            "completion_tokens >= 0",
            name="ck_llm_call_attempts_completion_tokens_nonnegative",
        ),
        CheckConstraint(
            "total_tokens >= 0",
            name="ck_llm_call_attempts_total_tokens_nonnegative",
        ),
        CheckConstraint(
            "estimated_tokens >= 0",
            name="ck_llm_call_attempts_estimated_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_tokens >= 0",
            name="ck_llm_call_attempts_reserved_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens >= 0",
            name="ck_llm_call_attempts_budget_charged_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens <= reserved_tokens",
            name="ck_llm_call_attempts_budget_charged_within_reservation",
        ),
        CheckConstraint(
            "latency_ms >= 0",
            name="ck_llm_call_attempts_latency_ms_nonnegative",
        ),
        CheckConstraint(
            "accounting_status IN ('reserved','settled','failed','released','rejected','usage_exceeds_reservation')",
            name="ck_llm_call_attempts_accounting_status",
        ),
        CheckConstraint(
            "dispatch_kind IN ('initial','transport_retry','response_parse_retry','api_mode_degrade','structured_output_degrade','missing_text_degrade','system_probe')",
            name="ck_llm_call_attempts_dispatch_kind",
        ),
        Index("ix_llm_call_attempts_call_status", "llm_call_id", "accounting_status"),
        # 迁移 20260929_0096：今日 / 本月用量读数按尝试时间过滤
        Index("ix_llm_call_attempts_created", "created_at"),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    llm_call_id: Mapped[str] = mapped_column(ForeignKey("llm_calls.llm_call_id"))
    provider_attempt_no: Mapped[int] = mapped_column(Integer)
    dispatch_kind: Mapped[str] = mapped_column(String)
    request_max_output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    budget_charged_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    usage_is_estimate: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    accounting_status: Mapped[str] = mapped_column(String)
    request_dispatched_at: Mapped[str | None] = mapped_column(String, nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


__all__ = [
    "LlmCall",
    "LlmCallAttempt",
]
