"""Runtime-owned database schema contract shared by API and operator tools."""

from __future__ import annotations

import re
from functools import lru_cache

from novel_system.env_config import BACKEND_ROOT


# 每加一个 Alembic 迁移都要把这里推到新的 head：`GET /ready` 用它判定库结构是否就绪，
# 每个启动脚本（start-dev.cmd / start-all-linux.sh / React 合约 E2E）都在等 /ready。
# 2026-09-13 阶段 C 加了 0084 却没有推它，于是所有迁移到新 head 的安装都「永远没就绪」，
# CI 的 E2E 连红两次。tests/test_schema_contract_revision.py 现在把它钉在 Alembic head 上。
CURRENT_SCHEMA_REVISION = "20260929_0098"

MIGRATION_VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"
# 每个迁移脚本顶上的 ``revision = "…"``；tests/test_schema_contract_revision.py 拿 Alembic 自己读出来的版本集合对它
_REVISION_LINE = re.compile(r"""^revision\s*(?::\s*str\s*)?=\s*["']([^"']+)["']""", re.MULTILINE)


@lru_cache(maxsize=1)
def known_schema_revisions() -> frozenset[str]:
    """这份代码的迁移目录里全部的迁移版本（只读文件、不导入迁移模块）；目录读不到时为空集。"""

    try:
        paths = sorted(MIGRATION_VERSIONS_DIR.glob("*.py"))
    except OSError:
        return frozenset()
    revisions: set[str] = set()
    for path in paths:
        try:
            match = _REVISION_LINE.search(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if match:
            revisions.add(match.group(1))
    return frozenset(revisions)


def revision_unknown_to_code(revision: str) -> bool:
    """库里记的迁移版本不在这份代码的迁移里：库比代码新（代码回退了，或者库来自别的分支）。

    读不到迁移目录（或目录里连代码自己的版本都没有）时说不准，按「不是」处理——调用方照「库落后于代码」报。"""

    known = known_schema_revisions()
    if CURRENT_SCHEMA_REVISION not in known:
        return False
    return revision not in known
