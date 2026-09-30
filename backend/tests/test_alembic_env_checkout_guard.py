"""检出守卫（B12-08）：Alembic 与运维工具只跑命令所在检出的代码。

各 git worktree 共用一个 venv，venv 里可编辑安装的 ``.pth`` 指向作者的主检出；``backend/novel_system`` 那个
``extend_path`` 导入垫片删掉之前，在 worktree 的 ``backend/`` 里直接 ``python -m alembic upgrade head`` 会读本检出的迁移、
却导入主检出的 ``database_runtime``——默认库就成了作者的实库。现在 ``alembic/env.py`` 与每个 ``tools/*`` 命令行入口
在碰任何库之前先核对，不一致就拒跑。

子进程用例从不导入作者的检出：「另一个检出」是临时目录里的假包，它的 ``database_runtime`` 一被导入就留下记号文件。
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import novel_system
from novel_system.tools import _checkout_guard

BACKEND_DIR = Path(__file__).resolve().parents[1]
SRC_PACKAGE = (BACKEND_DIR / "src" / "novel_system").resolve()
TOOLS_DIR = SRC_PACKAGE / "tools"


def _other_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """另一个检出的 ``backend/``：``src/novel_system`` 是个假包，导入它的 ``database_runtime`` 会写下记号文件。"""

    backend = tmp_path / "other_checkout" / "backend"
    package = backend / "src" / "novel_system"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    canary = tmp_path / "foreign_code_ran.txt"
    (package / "database_runtime.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(canary)!r}).write_text('imported', encoding='utf-8')\n"
        "raise RuntimeError('foreign database_runtime imported')\n",
        encoding="utf-8",
    )
    return backend, canary


def _subprocess_env(*, pythonpath: Path, database: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    env["PYTHONPATH"] = str(pythonpath)
    env["NOVEL_SYSTEM_DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    return env


# ---------------------------------------------------------------------------
# 垫片
# ---------------------------------------------------------------------------


def test_the_backend_import_shim_is_gone() -> None:
    assert not (BACKEND_DIR / "novel_system" / "__init__.py").exists()
    assert Path(novel_system.__file__).resolve() == SRC_PACKAGE / "__init__.py"


# ---------------------------------------------------------------------------
# Alembic
# ---------------------------------------------------------------------------


def test_alembic_refuses_novel_system_from_another_checkout(tmp_path: Path) -> None:
    other_backend, canary = _other_checkout(tmp_path)
    database = tmp_path / "must-not-exist.db"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_DIR / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=_subprocess_env(pythonpath=other_backend / "src", database=database),
        capture_output=True,
        text=True,
        timeout=300,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "checkout mismatch" in output, output
    assert str(other_backend / "src" / "novel_system") in output, output
    assert f"PYTHONPATH={BACKEND_DIR / 'src'}" in output, output
    # 在导入外来的 database_runtime 之前就拒绝了：外来代码一行没跑，库文件也没建
    assert not canary.exists()
    assert not database.exists()


def test_a_checkout_whose_src_is_a_symlink_is_not_refused(tmp_path: Path) -> None:
    """``backend/src`` 是符号链接（Windows 上是目录联接）的检出照样能跑：两边都按解析后的真实路径比。"""

    backend = tmp_path / "linked_checkout" / "backend"
    backend.mkdir(parents=True)
    shutil.copytree(BACKEND_DIR / "alembic", backend / "alembic", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(BACKEND_DIR / "alembic.ini", backend / "alembic.ini")
    try:
        (backend / "src").symlink_to(BACKEND_DIR / "src", target_is_directory=True)
    except OSError as exc:  # 没有建符号链接的权限（Windows 非管理员）
        pytest.skip(f"cannot create a directory symlink here: {exc}")
    database = tmp_path / "linked.db"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=backend,
        env=_subprocess_env(pythonpath=backend / "src", database=database),
        capture_output=True,
        text=True,
        timeout=300,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "checkout mismatch" not in output, output
    # 运维工具的守卫同一个口径
    assert _checkout_guard.checkout_mismatch(backend) is None


# ---------------------------------------------------------------------------
# 运维工具
# ---------------------------------------------------------------------------


def test_this_checkout_and_neutral_directories_pass(tmp_path: Path) -> None:
    assert _checkout_guard.invoking_checkout_package(BACKEND_DIR) == SRC_PACKAGE
    assert _checkout_guard.invoking_checkout_package(BACKEND_DIR.parent) == SRC_PACKAGE
    assert _checkout_guard.invoking_checkout_package(TOOLS_DIR) == SRC_PACKAGE
    assert _checkout_guard.checkout_mismatch(BACKEND_DIR) is None
    assert _checkout_guard.checkout_mismatch(BACKEND_DIR.parent / "frontend-react") is None
    # 不在任何检出里：代码、默认库与仓库文件都属于代码所在的检出，不拦
    assert _checkout_guard.invoking_checkout_package(tmp_path) is None
    assert _checkout_guard.checkout_mismatch(tmp_path) is None


def test_a_command_run_inside_another_checkout_is_refused(tmp_path: Path, capsys) -> None:
    other_backend, _canary = _other_checkout(tmp_path)

    problem = _checkout_guard.checkout_mismatch(other_backend)
    assert problem is not None and "checkout mismatch" in problem
    assert f"PYTHONPATH={other_backend.resolve() / 'src'}" in problem

    with pytest.raises(SystemExit) as refused:
        _checkout_guard.refuse_foreign_checkout("reset_author_state", start=other_backend / "src")
    assert refused.value.code == _checkout_guard.REFUSED_EXIT_CODE == 2
    assert "reset_author_state: checkout mismatch" in capsys.readouterr().err


def test_reset_tool_run_from_another_checkout_touches_no_database(tmp_path: Path) -> None:
    """真进程：本检出的代码、命令却在另一个检出里跑——``--execute --yes`` 也在建引擎之前退出 2。"""

    other_backend, canary = _other_checkout(tmp_path)
    database = tmp_path / "reset-target.db"

    result = subprocess.run(
        [sys.executable, "-m", "novel_system.tools.reset_author_state", "--execute", "--yes"],
        cwd=other_backend,
        env=_subprocess_env(pythonpath=BACKEND_DIR / "src", database=database),
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "reset_author_state: checkout mismatch" in result.stderr
    assert not database.exists()
    assert not canary.exists()


def _cli_entry_points() -> dict[str, ast.FunctionDef]:
    entries: dict[str, ast.FunctionDef] = {}
    for path in sorted(TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"main", "_main"}:
                entries[path.stem] = node
    return entries


def test_every_tool_entry_point_checks_the_checkout_first() -> None:
    """每个 ``tools/*`` 命令行入口的第一条语句就是检出守卫（新加的工具也逃不掉）。"""

    entries = _cli_entry_points()
    tool_modules = {path.stem for path in TOOLS_DIR.glob("*.py") if not path.name.startswith("_")}
    assert set(entries) == tool_modules, f"没有 main / _main 的工具模块：{sorted(tool_modules - set(entries))}"
    offenders: list[str] = []
    for module, function in entries.items():
        body = list(function.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # docstring
        first = body[0] if body else None
        call = first.value if isinstance(first, ast.Expr) else None
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "refuse_foreign_checkout"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == module
        ):
            offenders.append(f"{module}.{function.name}")
    assert offenders == [], f"这些入口没有先调 refuse_foreign_checkout('<模块名>')：{offenders}"
