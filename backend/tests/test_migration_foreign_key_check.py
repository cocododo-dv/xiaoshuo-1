"""迁移后的外键检查（B12-14）：迁移连接关着外键跑，一次 ``alembic upgrade`` 留下的孤儿行由 env.py 在最后一条
迁移之后用 ``PRAGMA foreign_key_check`` 抓出来，命令以非零退出（启动脚本随之停下）；库在这次运行之前就有的
违例不是迁移造成的，只记一条警告、不拦——否则作者会被一个本来能用的安装挡在门外。

「这次运行制造了孤儿」用一条只在测试里挂上的探针迁移模拟：它删掉一张父表的行，子表的行就成了孤儿。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from tests.test_migration_0084_scene_plan_rendering_mode import _insert_minimal_row

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROBE_REVISION = "zz_test_orphans_parent_rows"


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
    monkeypatch.setenv("NOVEL_SYSTEM_VECTOR_BACKEND", "memory")
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


def _probe_revision_dir(tmp_path: Path) -> Path:
    """head 之后的一条探针迁移：删掉全部章节行，挂在它们下面的质检报告就成了孤儿。"""
    versions = tmp_path / "probe_versions"
    versions.mkdir()
    (versions / f"{PROBE_REVISION}.py").write_text(
        "from alembic import op\n\n"
        f"revision = {PROBE_REVISION!r}\n"
        f"down_revision = {CURRENT_SCHEMA_REVISION!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n"
        "    op.execute('DELETE FROM chapter_goals')\n\n\n"
        "def downgrade() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    return versions


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

    with pytest.raises(RuntimeError, match=r"sqlite_foreign_key_check_failed: 1 row\(s\).*qc_reports -> chapter_goals x1"):
        command.upgrade(_config(_probe_revision_dir(tmp_path)), PROBE_REVISION)

    # 迁移本身已经提交（SQLite 每条迁移各自提交），命令失败只是拦住后面的启动
    with sqlite3.connect(isolated_database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [(PROBE_REVISION,)]


def test_violations_the_database_already_had_are_reported_but_do_not_fail(
    isolated_database: Path,
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

    command.upgrade(_config(), "head")

    # env.py 的 fileConfig 换掉了根 logger 的处理器（caplog 也在其中），警告走 alembic.ini 的控制台处理器
    err = capfd.readouterr().err
    assert "already violated foreign keys before this migration run" in err
    assert "qc_reports -> chapter_goals x1" in err
    assert len(_violations(isolated_database)) == 1


def test_a_clean_upgrade_leaves_no_violations(isolated_database: Path) -> None:
    command.upgrade(_config(), "head")
    assert _violations(isolated_database) == []
