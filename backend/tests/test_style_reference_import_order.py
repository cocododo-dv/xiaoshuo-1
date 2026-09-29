"""风格参考 v3（M1）：每个入口模块都能在一个新解释器里第一个被导入。

``style_policy`` → ``style_reference.binding_config`` 会先跑包的 ``__init__``；包的 ``__init__`` 曾经重导出
``binding_apply``，而 ``binding_apply`` 回头导入 ``style_policy.style_policy_live``——此时 ``style_policy`` 还没初始化
完，新解释器里 ``import novel_system.services.style_policy`` 直接 ImportError。测试套件里别的模块先把包导好了，这种
环在套件里看不见（``test_service_architecture`` 的静态图也不把「包的 ``__init__``」当成依赖），只有进程第一次导入
它的时候才炸。

所以这里查的是静态的「导入时」依赖图：导入 ``a.b.c`` 先执行 ``a/__init__``、``a/b/__init__``，每条导入语句都连到
目标路径上的每一层包；只算导入时真正执行的语句（模块体、类体、``if`` / ``try`` / ``with`` 块；函数体里的延迟导入和
``if TYPE_CHECKING:`` 不算）。图里没有环，任何模块第一个被导入都撞不上半初始化的模块——对整个 ``novel_system`` 成立，
不只风格参考。原先每个入口各起一个解释器（20 个），现在只留三个真进程冒烟，确认模型和真实导入一致。

盲区：模块体里**调用**一个做延迟导入的函数、PEP 562 ``__getattr__`` 外观、``importlib.import_module`` 在导入时
执行的导入，静态图看不见——这类写法别往包的 ``__init__`` 里加；真要加，同时把它补进下面的真进程冒烟。
"""

from __future__ import annotations

import ast
import functools
import graphlib
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# 真进程冒烟：当年那个环的两端
SMOKE_FIRST_IMPORTS = (
    "novel_system.services.style_policy",
    "novel_system.services.style_reference.binding_apply",
)
# 包的 ``__init__`` 只留说明（2026-09-24 清理 S4）：导入包本身不能拉起任何子模块，也没有环
BARE_PACKAGES = (
    "novel_system.services.style_reference",
    "novel_system.services.style_reference.inject",
)


def _import_time_imports(tree: ast.Module):
    """导入模块时真正执行的 import 语句：模块体、类体、if / try / with 块；不进函数体和 ``if TYPE_CHECKING:``。"""

    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            continue
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            pending.extend(node.orelse)
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            pending.extend(getattr(node, field, None) or [])


def _import_targets(node: ast.Import | ast.ImportFrom, *, package: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if node.level:
        parts = package.split(".")
        module = ".".join(parts[: len(parts) - node.level + 1] + ([node.module] if node.module else []))
    # ``from a.b import c``：c 可能是子模块
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def import_time_graph(src_root: Path, package: str) -> dict[str, set[str]]:
    """模块 → 导入它时会执行的包内模块（目标本身 + 目标路径上的每一层包 ``__init__``）。

    模块自己的上层包在它的模块体开始执行前就已经在 ``sys.modules`` 里了，不算边——否则每个重导出子模块的包都成了环。
    """

    modules: dict[str, tuple[Path, bool]] = {}
    for path in sorted((src_root / package.replace(".", "/")).rglob("*.py")):
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        modules[".".join(parts)] = (path, is_package)
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, (path, is_package) in modules.items():
        own_package = name if is_package else name.rpartition(".")[0]
        own_ancestors = {name.rsplit(".", depth)[0] for depth in range(1, name.count(".") + 1)}
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _import_time_imports(tree):
            for target in _import_targets(node, package=own_package):
                if target != package and not target.startswith(package + "."):
                    continue
                target_parts = target.split(".")
                for index in range(1, len(target_parts) + 1):
                    candidate = ".".join(target_parts[:index])
                    if candidate in graph and candidate != name and candidate not in own_ancestors:
                        graph[name].add(candidate)
    return graph


def _cycle(graph: dict[str, set[str]]) -> list[str] | None:
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        return list(exc.args[1])
    return None


@functools.cache
def _package_graph() -> dict[str, set[str]]:
    return import_time_graph(SRC_ROOT, "novel_system")


def _fresh_interpreter_env() -> dict[str, str]:
    env = dict(os.environ)
    # 这个 worktree 的源码排在最前（venv 里的可编辑安装可能指向别的检出）
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def test_import_time_graph_has_no_cycle_through_package_inits() -> None:
    cycle = _cycle(_package_graph())
    assert cycle is None, "导入时依赖成环（第一个被导入的模块会撞上半初始化的模块）：" + " → ".join(cycle or [])


def test_static_model_catches_a_cycle_through_a_package_init(tmp_path: Path, monkeypatch) -> None:
    """当年那个环的缩样：只有把「包的 ``__init__``」算成依赖才看得见，真导入确实会炸；去掉重导出就没有环。"""

    files = {
        "cyclepkg/__init__.py": "",
        "cyclepkg/policy.py": "from cyclepkg.style.binding_config import MODE\n\n\ndef live():\n    return MODE\n",
        "cyclepkg/style/__init__.py": "from cyclepkg.style.binding_apply import apply\n",
        "cyclepkg/style/binding_config.py": "MODE = 'full'\n",
        "cyclepkg/style/binding_apply.py": "from cyclepkg.policy import live\n\n\ndef apply():\n    return live()\n",
    }
    for relative, source in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(source, encoding="utf-8")

    def first_import_policy() -> None:
        for name in [name for name in sys.modules if name == "cyclepkg" or name.startswith("cyclepkg.")]:
            del sys.modules[name]
        importlib.invalidate_caches()
        importlib.import_module("cyclepkg.policy")

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # 改写 __init__ 后不能读到旧的 .pyc
    try:
        cycle = _cycle(import_time_graph(tmp_path, "cyclepkg"))
        assert cycle is not None
        assert set(cycle) == {"cyclepkg.policy", "cyclepkg.style", "cyclepkg.style.binding_apply"}
        with pytest.raises(ImportError):
            first_import_policy()

        (tmp_path / "cyclepkg/style/__init__.py").write_text('"""只留说明。"""\n', encoding="utf-8")
        assert _cycle(import_time_graph(tmp_path, "cyclepkg")) is None
        first_import_policy()
    finally:
        for name in [name for name in sys.modules if name == "cyclepkg" or name.startswith("cyclepkg.")]:
            del sys.modules[name]


@pytest.mark.parametrize("package", BARE_PACKAGES)
def test_package_init_imports_no_submodule(package: str) -> None:
    graph = _package_graph()
    reached: set[str] = set()
    # 导入包先执行它路径上的每一层 ``__init__``
    pending = [package.rsplit(".", depth)[0] for depth in range(package.count(".") + 1)]
    while pending:
        module = pending.pop()
        if module not in reached:
            reached.add(module)
            pending.extend(graph[module])
    assert sorted(name for name in reached if name.startswith(package + ".")) == []


@pytest.mark.parametrize("module", SMOKE_FIRST_IMPORTS)
def test_module_imports_first_in_a_fresh_interpreter(module: str, tmp_path: Path) -> None:
    code = (
        "import importlib, pathlib, sys\n"
        f"importlib.import_module({module!r})\n"
        "import novel_system\n"
        f"assert pathlib.Path(novel_system.__file__).resolve().is_relative_to(pathlib.Path({str(SRC_ROOT)!r}).resolve())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_fresh_interpreter_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"import {module} failed in a fresh interpreter:\n{result.stderr[-2000:]}"


@pytest.mark.parametrize("package", BARE_PACKAGES[:1])
def test_package_import_pulls_no_submodule(package: str, tmp_path: Path) -> None:
    code = (
        "import importlib, sys\n"
        f"importlib.import_module({package!r})\n"
        f"loaded = sorted(name for name in sys.modules if name.startswith({package + '.'!r}))\n"
        "print('\\n'.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=_fresh_interpreter_env(), capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = [line for line in result.stdout.splitlines() if line.strip()]
    assert loaded == [], f"importing {package} pulled submodules: {loaded}"
