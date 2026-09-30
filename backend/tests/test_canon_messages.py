"""正史核对与叙事账本的报错给作者看：成稿中心的正史面板把后端的 message 原样显示（B11-23）。

棘轮：这些模块里每个 ``DomainError(code, message)`` 的 message 都得是中文——错误码不变，前端照旧按码分支。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "src" / "novel_system" / "services"
AUTHOR_FACING_MODULES = sorted(
    [
        SERVICES / "canon_continuity.py",
        *(SERVICES / "canon").glob("*.py"),
        *(SERVICES / "narrative").glob("*.py"),
    ]
)
_CJK = re.compile(r"[一-鿿]")


def _domain_error_messages(path: Path) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "DomainError"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant):
            continue
        message = node.args[1]
        if isinstance(message, ast.Constant):
            text = str(message.value)
        elif isinstance(message, ast.JoinedStr):
            text = "".join(str(part.value) for part in message.values if isinstance(part, ast.Constant))
        else:
            continue
        found.append((node.lineno, str(node.args[0].value), text))
    return found


def test_canon_and_narrative_errors_speak_chinese() -> None:
    english = [
        f"{path.name}:{line} {code}: {text}"
        for path in AUTHOR_FACING_MODULES
        if path.exists()
        for line, code, text in _domain_error_messages(path)
        if not _CJK.search(text)
    ]
    assert not english, english
    scanned = sum(len(_domain_error_messages(path)) for path in AUTHOR_FACING_MODULES if path.exists())
    assert scanned >= 30, f"只扫到 {scanned} 个 DomainError（模块搬家了？）"
