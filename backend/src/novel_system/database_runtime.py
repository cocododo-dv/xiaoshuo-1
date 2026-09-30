"""兼容转出：数据库引导设置住在 ``env_config``（B09-05）。

还从这里 import 的：``alembic/env.py``、``db/session.py``（P09a 在改）与 ``tests/test_db_session_guard.py``。
它们改成 ``from novel_system.env_config import …`` 之后删掉本文件。
"""

from __future__ import annotations

from novel_system.env_config import BACKEND_ROOT, DEFAULT_DATABASE_PATH, DatabaseRuntime, load_database_runtime

__all__ = ["BACKEND_ROOT", "DEFAULT_DATABASE_PATH", "DatabaseRuntime", "load_database_runtime"]
