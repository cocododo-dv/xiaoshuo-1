"""测试进程不得连仓库的实库 backend/novel_system.db(2026-09-23 风格参考 v3 台账 L6)。

2026-09-06 一次测试运行在环境变量还没指向临时库时建了引擎,把 22 本夹具参考书写进了作者的实库。
``db.session.engine()`` 在 pytest 进程里遇到指向仓库 ``backend/novel_system.db`` 的 SQLite URL 直接拒绝
(绝对路径、相对路径都算),不建引擎、不碰文件。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_system.database_runtime import DEFAULT_DATABASE_PATH
from novel_system.db import session as db_session


def _restore(monkeypatch, original_url: str) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", original_url)
    db_session.reset_engine()


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_engine_refuses_the_repository_database_under_pytest(monkeypatch, relative: bool) -> None:
    original = os.environ["NOVEL_SYSTEM_DATABASE_URL"]
    existed_before = DEFAULT_DATABASE_PATH.exists()
    if relative:
        monkeypatch.chdir(DEFAULT_DATABASE_PATH.parent)
        url = "sqlite:///./novel_system.db"
    else:
        url = f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", url)
    db_session.reset_engine()
    try:
        with pytest.raises(RuntimeError, match="repository database"):
            db_session.engine()
        assert db_session._ENGINE is None
    finally:
        _restore(monkeypatch, original)
    # 拒绝发生在建引擎之前:不会顺手建出一个空的实库文件
    assert DEFAULT_DATABASE_PATH.exists() == existed_before


def test_engine_refuses_another_checkouts_live_database(monkeypatch, tmp_path) -> None:
    """git worktree 里跑测试、URL 指到主检出的实库(同名 novel_system.db、不在临时目录):同样拒绝。"""
    elsewhere = Path.home() / "some-other-checkout" / "backend" / "novel_system.db"
    with pytest.raises(RuntimeError, match="repository database"):
        db_session.refuse_repository_database_under_pytest(f"sqlite:///{elsewhere.as_posix()}")
    assert not elsewhere.exists()
    # 临时目录里同名的库(测试自己建的)不拦
    db_session.refuse_repository_database_under_pytest(f"sqlite:///{(tmp_path / 'novel_system.db').as_posix()}")


def test_other_databases_are_fine_under_pytest(tmp_path) -> None:
    db_session.refuse_repository_database_under_pytest(f"sqlite:///{tmp_path / 'other.db'}")
    db_session.refuse_repository_database_under_pytest("sqlite://")
    db_session.refuse_repository_database_under_pytest("sqlite:///:memory:")
    db_session.refuse_repository_database_under_pytest("postgresql://user@localhost/db")


def test_the_guard_is_inactive_outside_pytest(monkeypatch) -> None:
    monkeypatch.setattr(db_session, "_running_under_pytest", lambda: False)
    db_session.refuse_repository_database_under_pytest(f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}")
