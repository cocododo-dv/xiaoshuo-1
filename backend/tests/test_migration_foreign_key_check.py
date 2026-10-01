"""迁移后的外键检查（B12-14）：迁移连接关着外键跑，一次 ``alembic upgrade`` 留下的孤儿行由 env.py 在最后一条
迁移之后用 ``PRAGMA foreign_key_check`` 抓出来，命令以非零退出（启动脚本随之停下）；库在这次运行之前就有的
违例不是迁移造成的，只记一条警告、不拦——否则作者会被一个本来能用的安装挡在门外。

这次运行一条迁移都不会跑（启动脚本在最新库上的 ``upgrade head``、``alembic current``）就不扫：几百 MB 的库冷扫
一遍要好几秒，每次启动都白付。失败也就只拦一次——修好或还原之前再跑一遍无事可做、不会再拦，报错原文得说清楚。

「这次运行制造了孤儿」用一条只在测试里挂上的探针迁移模拟：它删掉一张父表的行，子表的行就成了孤儿；另一条
什么都不做的探针迁移代表「有迁移要跑、但没造孤儿」。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from tests.test_migration_0084_scene_plan_rendering_mode import _insert_minimal_row

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROBE_REVISION = "zz_test_orphans_parent_rows"
HARMLESS_PROBE_REVISION = "zz_test_changes_nothing"


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_test_process: None,
) -> Path:
    """覆盖父 conftest 的同名夹具：库只由 Alembic 建，不走 ``create_all``。"""
    from novel_system.db.session import reset_engine

    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    reset_engine()
    yield db_path
    reset_engine()


def _config(*extra_version_dirs: Path) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    if extra_version_dirs:
        config.set_main_option("path_separator", "os")
        config.set_main_option(
            "version_locations",
            os.pathsep.join([str(BACKEND_DIR / "alembic" / "versions"), *map(str, extra_version_dirs)]),
        )
    return config


def _write_probe_revision(tmp_path: Path, revision: str, upgrade_body: str) -> Path:
    versions = tmp_path / f"probe_versions_{revision}"
    versions.mkdir()
    (versions / f"{revision}.py").write_text(
        "from alembic import op\n\n"
        f"revision = {revision!r}\n"
        f"down_revision = {CURRENT_SCHEMA_REVISION!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n"
        f"    {upgrade_body}\n\n\n"
        "def downgrade() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    return versions


def _probe_revision_dir(tmp_path: Path) -> Path:
    """head 之后的一条探针迁移：删掉全部章节行，挂在它们下面的质检报告就成了孤儿。"""
    return _write_probe_revision(tmp_path, PROBE_REVISION, "op.execute('DELETE FROM chapter_goals')")


def _harmless_probe_revision_dir(tmp_path: Path) -> Path:
    """head 之后的一条什么都不做的探针迁移：有迁移要跑，但一行数据都不动。"""
    return _write_probe_revision(tmp_path, HARMLESS_PROBE_REVISION, "pass")


def _spy_on_foreign_key_scans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """env.py 发出的每一条 ``PRAGMA foreign_key_check``（它走 ``Connection.exec_driver_sql``）。"""
    issued: list[str] = []
    original = Connection.exec_driver_sql

    def spy(self, statement, *args, **kwargs):
        if "foreign_key_check" in str(statement):
            issued.append(str(statement))
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", spy)
    return issued


def _seed_chapter_with_qc_report(path: Path, *, chapter_id: str = "CH_FK_CHECK") -> None:
    """原生 sqlite3 连接不开外键：父子两行都按迁移建出来的表结构补齐必填列。"""
    with sqlite3.connect(path) as connection:
        _insert_minimal_row(connection, "chapter_goals", {"chapter_id": chapter_id})
        _insert_minimal_row(
            connection,
            "qc_reports",
            {"qc_report_id": f"QC_{chapter_id}", "chapter_id": chapter_id, "scene_id": None},
        )


def _violations(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def test_a_migration_run_that_orphans_rows_fails_after_the_last_revision(isolated_database: Path, tmp_path: Path) -> None:
    command.upgrade(_config(), "head")
    _seed_chapter_with_qc_report(isolated_database)
    assert _violations(isolated_database) == []
    probe = _config(_probe_revision_dir(tmp_path))

    with pytest.raises(RuntimeError, match=r"sqlite_foreign_key_check_failed: 1 row\(s\).*qc_reports -> chapter_goals x1") as failed:
        command.upgrade(probe, PROBE_REVISION)

    # 迁移本身已经提交（SQLite 每条迁移各自提交），命令失败只是拦住后面的启动
    with sqlite3.connect(isolated_database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [(PROBE_REVISION,)]
    # 报错原文说清楚：迁移已经落库、再跑一遍不会再拦，启动后端之前先还原备份或修好这些行
    message = str(failed.value)
    assert "already applied" in message
    assert "will not stop the backend" in message
    assert "novel_system.tools.db_backup --restore" in message
    assert "repair the listed rows" in message

    # 原文说的就是现状：第二遍无事可做，不扫、不拦，孤儿还在
    command.upgrade(probe, PROBE_REVISION)
    assert [(table, parent) for table, _rowid, parent, _fkid in _violations(isolated_database)] == [
        ("qc_reports", "chapter_goals")
    ]


def test_violations_the_database_already_had_are_reported_but_do_not_fail(
    isolated_database: Path,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(_config(), "head")
    with sqlite3.connect(isolated_database) as connection:
        _insert_minimal_row(
            connection,
            "qc_reports",
            {"qc_report_id": "QC_ALREADY_ORPHANED", "chapter_id": "CH_GONE_LONG_AGO", "scene_id": None},
        )
    assert len(_violations(isolated_database)) == 1
    capfd.readouterr()

    command.upgrade(_config(_harmless_probe_revision_dir(tmp_path)), HARMLESS_PROBE_REVISION)

    # env.py 的 fileConfig 换掉了根 logger 的处理器（caplog 也在其中），警告走 alembic.ini 的控制台处理器
    err = capfd.readouterr().err
    assert "already violated foreign keys before this migration run" in err
    assert "qc_reports -> chapter_goals x1" in err
    assert len(_violations(isolated_database)) == 1


def test_the_foreign_key_scan_only_runs_when_a_revision_can_run(
    isolated_database: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次启动的 ``upgrade head`` 多半无事可做，``alembic current`` 从不改库：两者都不该冷扫整个库（R2）。"""

    command.upgrade(_config(), "head")
    scans = _spy_on_foreign_key_scans(monkeypatch)

    # 启动脚本在最新库上的 upgrade head、只读的 current
    command.upgrade(_config(), "head")
    command.current(_config())
    command.upgrade(_config(), CURRENT_SCHEMA_REVISION)
    assert scans == []

    # 真有迁移要跑（升级、降级）：前后各扫一遍，照旧
    harmless = _config(_harmless_probe_revision_dir(tmp_path))
    command.upgrade(harmless, "head")
    assert len(scans) == 2
    command.upgrade(harmless, "head")
    command.current(harmless)
    assert len(scans) == 2
    command.downgrade(harmless, "-1")
    assert len(scans) == 4
    with sqlite3.connect(isolated_database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [(CURRENT_SCHEMA_REVISION,)]


def test_a_clean_upgrade_leaves_no_violations(isolated_database: Path) -> None:
    command.upgrade(_config(), "head")
    assert _violations(isolated_database) == []
