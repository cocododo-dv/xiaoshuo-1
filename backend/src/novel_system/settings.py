"""运行时设置：环境变量（``env_config.load_env_settings``）叠上库里的活动 api 快照（服务、地址、启用、超时、密钥）。

``system_config`` 读不了本模块（本模块要读它的活动快照），它和记账层、Alembic 引导直接用 ``env_config``。
"""

from __future__ import annotations

from novel_system.env_config import (
    BACKEND_ROOT,
    DEFAULT_DATABASE_PATH,
    Settings,
    load_env_settings,
    warn_retired_env_vars,
)

__all__ = [
    "BACKEND_ROOT",
    "DEFAULT_DATABASE_PATH",
    "Settings",
    "get_settings",
]


def get_settings(*, include_runtime_config: bool = True) -> Settings:
    warn_retired_env_vars()
    settings = load_env_settings()
    if not include_runtime_config:
        return settings

    from novel_system.services.system_config import apply_active_api_config

    return apply_active_api_config(settings)
