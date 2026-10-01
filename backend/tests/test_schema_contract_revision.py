"""运行时结构契约必须与 Alembic head 同步。

`db/schema_contract.CURRENT_SCHEMA_REVISION` 是 `GET /ready` 判定「库结构就绪」的唯一依据，
所有启动脚本与 React 合约 E2E 都在等 /ready。2026-09-13 的迁移 0084 没有推这个常量，
结果每个升级到新 head 的安装都被判「未就绪」——单元测试全绿，CI 的 E2E 连红两次。
这里把常量钉在迁移脚本目录的 head 上：以后加迁移不推常量，套件当场失败。
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from novel_system.db.schema_contract import (
    CURRENT_SCHEMA_REVISION,
    known_schema_revisions,
    revision_unknown_to_code,
)


def _script_directory() -> ScriptDirectory:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return ScriptDirectory.from_config(config)


def _alembic_heads() -> tuple[str, ...]:
    return tuple(_script_directory().get_heads())


def test_schema_contract_revision_is_the_alembic_head() -> None:
    heads = _alembic_heads()
    assert len(heads) == 1, f"迁移脚本目录出现多个 head：{heads}"
    assert CURRENT_SCHEMA_REVISION == heads[0], (
        f"db/schema_contract.CURRENT_SCHEMA_REVISION={CURRENT_SCHEMA_REVISION!r} 落后于 Alembic head {heads[0]!r}："
        "加了迁移就要推这个常量，否则 GET /ready 永远报 schema_revision_mismatch，启动脚本与 E2E 全部卡住。"
    )


def test_known_schema_revisions_are_exactly_the_alembic_revisions() -> None:
    """``GET /ready`` 与 API 的库结构闸靠这份集合分辨「库落后于代码」与「库比代码新」（复核 P09b-R2）：它只读迁移文件里的
    ``revision = "…"``、不导入迁移模块，所以这里拿 Alembic 自己读出来的版本集合对一遍——写法变了，这里当场失败。"""
    alembic_revisions = {script.revision for script in _script_directory().walk_revisions()}
    assert known_schema_revisions() == alembic_revisions
    assert CURRENT_SCHEMA_REVISION in alembic_revisions
    assert not revision_unknown_to_code("20260716_0072")
    assert revision_unknown_to_code("20991231_0999")
