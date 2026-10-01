"""测试库的迁移版本戳：测试用 create_all 建库，没有 ``alembic_version``；要走「先核对库结构版本」的代码
（运维工具的 ``--execute``、API 的库结构闸）时给它盖上代码认的版本。"""

from __future__ import annotations

from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from novel_system.db.session import engine


def stamp_schema_revision(revision: str = CURRENT_SCHEMA_REVISION) -> None:
    with engine().begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql("DELETE FROM alembic_version")
        connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))


__all__ = ["stamp_schema_revision"]
