"""按源码静态读 ``novel_system`` 的模块与 import（不执行任何模块）：架构守卫与叶子模块检查共用。"""

from __future__ import annotations

import ast
from pathlib import Path


# ---------------------------------------------------------------- 源码树与静态导入表（test_service_architecture）


SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT = SRC_ROOT / "novel_system"


def modules_under(root: Path) -> dict[Path, str]:
    modules: dict[Path, str] = {}
    for path in root.rglob("*.py"):
        parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[path] = ".".join(parts)
    return modules


def imports_of(path: Path) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
    return imports
