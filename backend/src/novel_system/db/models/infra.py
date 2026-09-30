"""基础设施：幂等记录、操作日志、系统配置快照、加密密钥。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="started")
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class OperationLog(Base):
    __tablename__ = "operation_logs"

    operation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_ref: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SystemConfigSnapshot(Base):
    __tablename__ = "system_config_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    category: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    yaml_raw: Mapped[str] = mapped_column(Text)
    parsed_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="draft")
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    activated_at: Mapped[str | None] = mapped_column(String, nullable=True)


class SystemSecret(Base):
    __tablename__ = "system_secrets"

    secret_id: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    value_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    secret_type: Mapped[str] = mapped_column(String, default="generic")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


__all__ = [
    "IdempotencyKey",
    "OperationLog",
    "SystemConfigSnapshot",
    "SystemSecret",
]
