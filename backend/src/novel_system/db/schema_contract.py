"""Runtime-owned database schema contract shared by API and operator tools."""

from __future__ import annotations


# 每加一个 Alembic 迁移都要把这里推到新的 head：`GET /ready` 用它判定库结构是否就绪，
# 每个启动脚本（start-dev.cmd / start-all-linux.sh / React 合约 E2E）都在等 /ready。
# 2026-09-13 阶段 C 加了 0084 却没有推它，于是所有迁移到新 head 的安装都「永远没就绪」，
# CI 的 E2E 连红两次。tests/test_schema_contract_revision.py 现在把它钉在 Alembic head 上。
CURRENT_SCHEMA_REVISION = "20260920_0089"
