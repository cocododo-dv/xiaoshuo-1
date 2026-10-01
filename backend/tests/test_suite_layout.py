"""测试套件自身的结构守卫（X04-10）。

- 测试文件之间不互相 import：一个 ``test_*.py`` 被别的文件 import，就成了别人的助手库——拆它、改名都会牵连
  别的文件，import 它还会把它整个模块体（连同 autouse 夹具的定义）执行一遍。共用的助手住在 ``tests/support/``。
- ``tests/support`` 下的每个模块都登记了断言改写（``tests/support/__init__.py``），助手里的 ``assert`` 失败时照样
  打印两边的值。
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
SUPPORT_ROOT = TESTS_ROOT / "support"


def _imported_test_modules(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            # 绝对（tests.test_x）与相对（.test_x / from . import test_x）两种写法都算
            parts = (node.module or "").split(".") if node.module else []
            if node.level == 0 and parts and parts[0] == "tests":
                parts = parts[1:]
            elif node.level == 0:
                continue
            dotted = "." * node.level + (node.module or "")
            if parts and parts[0].startswith("test_"):
                found.append(f"{node.lineno}: from {dotted} import …")
            elif not parts:
                found.extend(f"{node.lineno}: from {dotted or 'tests'} import {alias.name}" for alias in node.names if alias.name.startswith("test_"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "tests" and len(parts) > 1 and parts[1].startswith("test_"):
                    found.append(f"{node.lineno}: import {alias.name}")
    return found


def test_no_module_of_the_suite_imports_a_test_module() -> None:
    offenders = [
        f"{path.relative_to(TESTS_ROOT).as_posix()}:{line}"
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        for line in _imported_test_modules(path)
    ]
    assert offenders == [], "测试文件 / 助手 import 了别的 test_*.py——把共用的东西搬进 tests/support/"


def test_every_support_module_is_registered_for_assertion_rewriting() -> None:
    tree = ast.parse((SUPPORT_ROOT / "__init__.py").read_text(encoding="utf-8"))
    registered = {
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "register_assert_rewrite"
        for arg in node.args
        if isinstance(arg, ast.Constant)
    }
    modules = {f"tests.support.{path.stem}" for path in SUPPORT_ROOT.glob("*.py") if path.stem != "__init__"}
    assert registered == modules


def test_support_holds_no_collectable_tests() -> None:
    assert sorted(path.name for path in SUPPORT_ROOT.glob("test_*.py")) == []
