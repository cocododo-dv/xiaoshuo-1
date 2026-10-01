"""运维工具的共用骨架（B12-17）：写库前核对库结构版本、系统配置快照的读改写。

- :func:`open_checked_session`：会写库的工具（``--execute``）先核对 ``alembic_version`` 与代码认的
  ``CURRENT_SCHEMA_REVISION``；对不上（库没升级、或是代码比库旧）就不写，告诉操作者先 ``alembic upgrade head``，
  以退出码 2 拒跑——与 ``GET /ready`` 同一条标准。干跑照常打开（只读）。
- :func:`active_snapshot_payload` / :func:`activate_snapshot_payload`：两个改系统配置快照的工具
  （``sync_prompt_templates``、``raise_llm_output_budget``）共用的「读活动快照 → 改 → 另存为新版本并激活」。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import yaml
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from novel_system.db.session import SessionLocal
from novel_system.services.config_cache import safe_load_yaml
from novel_system.services.system_config import SystemConfigService

# 退出码 2：没碰任何东西就拒跑（与检出守卫、E2E 通道的端口守卫同一个约定）
REFUSED_EXIT_CODE = 2


def schema_revision_problem(session: Session) -> str | None:
    """库的迁移版本与代码不一致时返回说明（中文），一致返回 ``None``。"""

    connection = session.connection()
    if not inspect(connection).has_table("alembic_version"):
        return f"这个库没有迁移版本记录（alembic_version）；代码要的是 {CURRENT_SCHEMA_REVISION}。先运行 alembic upgrade head。"
    revisions = sorted(str(value) for value in connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if revisions != [CURRENT_SCHEMA_REVISION]:
        found = "、".join(revisions) or "（空）"
        return f"库的迁移版本是 {found}，代码要的是 {CURRENT_SCHEMA_REVISION}。先运行 alembic upgrade head（或换回匹配的代码）再写库。"
    return None


@contextmanager
def open_checked_session(tool: str, *, writes: bool) -> Iterator[Session]:
    """打开会话；``writes`` 时先核对库结构版本，对不上就说明原因、以退出码 2 拒跑（什么都没写）。"""

    session = SessionLocal()
    try:
        if writes:
            problem = schema_revision_problem(session)
            session.rollback()
            if problem is not None:
                print(f"{tool}: {problem}", file=sys.stderr)
                raise SystemExit(REFUSED_EXIT_CODE)
        yield session
    finally:
        session.close()


def active_snapshot_payload(service: SystemConfigService, category: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """``(活动快照的元数据, 解析后的配置)``；这一类还没有存过快照时元数据为 ``None``、配置为空字典。"""

    overview = service.overview()["categories"][category]
    snapshot = overview.get("active_snapshot")
    if not snapshot:
        return None, {}
    return snapshot, safe_load_yaml(overview["yaml_raw"]) or {}


def activate_snapshot_payload(service: SystemConfigService, category: str, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """把改好的配置另存为这一类的新快照版本并激活，返回激活后的快照元数据。"""

    created = service.create_draft(
        category=category,
        yaml_raw=yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        secrets=None,
        actor_ref=actor,
    )
    return service.activate(created["snapshot"]["snapshot_id"], actor_ref=actor)["snapshot"]


__all__ = [
    "REFUSED_EXIT_CODE",
    "activate_snapshot_payload",
    "active_snapshot_payload",
    "open_checked_session",
    "schema_revision_problem",
]
