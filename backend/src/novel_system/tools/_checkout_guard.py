"""运维工具的检出守卫（B12-08）：导入的 ``novel_system`` 必须来自命令所在的检出。

各 git worktree 共用一个 venv，venv 里可编辑安装的 ``.pth`` 指向作者的主检出。在某个 worktree 里直接
``python -m novel_system.tools.<工具>``（不带 ``PYTHONPATH=src``），导入的是主检出的代码——默认库也就跟着成了
主检出的 ``backend/novel_system.db``，也就是作者的实库。每个工具在碰任何库 / 仓库文件之前先调
``refuse_foreign_checkout``：从当前目录往上找到的检出（``backend/src/novel_system``）与导入的包不是同一个，就说清楚
原因、以退出码 2 拒跑。

不在任何检出里运行（从中性目录、``PYTHONPATH`` 指向某个检出）不受限制：那时代码、默认库与仓库文件都属于同一个检出。
Alembic 的 ``env.py`` 有自己的一份同样的检查（它不能依赖被检查的包）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import novel_system

# 退出码 2：没碰任何东西就拒跑（与 E2E 通道的端口守卫同一个约定）
REFUSED_EXIT_CODE = 2


def invoking_checkout_package(start: Path | None = None) -> Path | None:
    """``start``（默认当前目录）所在检出的 ``backend/src/novel_system``；不在任何检出里 → ``None``。"""

    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        for package in (directory / "src" / "novel_system", directory / "backend" / "src" / "novel_system"):
            if (package / "__init__.py").is_file():
                return package.resolve()
    return None


def loaded_package_dirs() -> set[Path]:
    """这个进程导入的 ``novel_system`` 包目录。"""

    dirs = {Path(entry).resolve() for entry in getattr(novel_system, "__path__", ())}
    origin = getattr(novel_system, "__file__", None)
    if origin:
        dirs.add(Path(origin).resolve().parent)
    return dirs


def checkout_mismatch(start: Path | None = None) -> str | None:
    """命令所在的检出与导入的代码不一致时返回说明文字，一致（或不在检出里）返回 ``None``。"""

    checkout = invoking_checkout_package(start)
    if checkout is None:
        return None
    loaded = loaded_package_dirs()
    if loaded == {checkout}:
        return None
    backend_dir = checkout.parents[1]
    loaded_text = "、".join(sorted(str(path) for path in loaded)) or "<未知>"
    return (
        f"checkout mismatch：命令在检出 {backend_dir} 里运行，导入的 novel_system 却来自 {loaded_text}"
        f"（默认库会是那个检出的 novel_system.db）。请带上 PYTHONPATH={backend_dir / 'src'} 再运行。"
    )


def refuse_foreign_checkout(tool: str, *, start: Path | None = None) -> None:
    """代码不来自命令所在的检出就拒跑（``SystemExit(2)``）；在碰任何库之前调用。"""

    problem = checkout_mismatch(start)
    if problem is None:
        return
    print(f"{tool}: {problem}", file=sys.stderr)
    raise SystemExit(REFUSED_EXIT_CODE)


__all__ = [
    "REFUSED_EXIT_CODE",
    "checkout_mismatch",
    "invoking_checkout_package",
    "loaded_package_dirs",
    "refuse_foreign_checkout",
]
