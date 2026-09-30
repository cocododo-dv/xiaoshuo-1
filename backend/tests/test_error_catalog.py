"""接口错误的中文说明（B12-04，批准 #27）：错误码 → 作者看的中文，外加一道只收紧的棘轮。

界面按错误码分支、把信封里的 ``message`` 直接给作者看。这里守三件事：
1. 源码里每个带英文（或运行时才知道的）说明的 ``DomainError`` 错误码都在 ``api/error_catalog.ERROR_MESSAGES`` 里——
   新写一个英文说明的错误就会在这里变红；
2. 表里的说明都是中文，只用 ``{message}`` 一个占位；表里不留源码里已经没有的错误码；
3. 异常处理器真的按表换：英文换中文、原本就是中文的不动、被换掉的原文只在 ``expose_error_detail`` 打开时进
   ``details.debug_message``。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fastapi.testclient import TestClient

from novel_system.api.app import create_app
from novel_system.api.error_catalog import ERROR_MESSAGES, localized_message
from novel_system.services.errors import DomainError

SRC = Path(__file__).resolve().parents[1] / "src" / "novel_system"
CJK = re.compile(r"[一-鿿]")
DYNAMIC = object()

# 外壳自己发的错误码（不经 DomainError）：校验、数据库、未处理异常、访问边界、请求体上限
SHELL_CODES = {
    "REQUEST_VALIDATION_FAILED",
    "DATABASE_BUSY",
    "DATABASE_OPERATION_FAILED",
    "INTERNAL_ERROR",
    "REMOTE_ACCESS_DISABLED",
    "REMOTE_ACCESS_TOKEN_REQUIRED",
    "REQUEST_BODY_TOO_LARGE",
    "INVALID_CONTENT_LENGTH",
}


def _source_files() -> list[Path]:
    # 运维工具在终端里说话，不经 API 信封
    return [path for path in sorted(SRC.rglob("*.py")) if "tools" not in path.relative_to(SRC).parts]


def _string_constants(files: list[Path]) -> dict[str, str]:
    """模块顶层的 ``NAME = "字面量"``（错误码常量、说明常量）；同名不同值的不收，免得解析错。"""
    seen: dict[str, set[str]] = {}
    for path in files:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                seen.setdefault(node.targets[0].id, set()).add(node.value.value)
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def _text(node: ast.AST | None, constants: dict[str, str]) -> object:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.JoinedStr):
        literal = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
        # 带汉字的 f-string 是写给作者的；只剩英文骨架的按英文算
        return literal if CJK.search(literal) else DYNAMIC
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # 拼接出来的说明：与 f-string 同一条规则，写死的部分带汉字就是写给作者的
        parts = (_text(node.left, constants), _text(node.right, constants))
        literal = "".join(part for part in parts if isinstance(part, str))
        return literal if CJK.search(literal) else DYNAMIC
    if isinstance(node, ast.IfExp):
        # 两种说明二选一：两个分支都是中文才算中文，否则按说英文的那一支算
        branches = (_text(node.body, constants), _text(node.orelse, constants))
        if any(branch is DYNAMIC for branch in branches):
            return DYNAMIC
        return next((branch for branch in branches if not CJK.search(branch)), branches[0])
    return DYNAMIC


def _raised_codes() -> dict[str, list[object]]:
    """错误码 → 各处抛它时的说明（字面量 / DYNAMIC）。"""
    files = _source_files()
    constants = _string_constants(files)
    codes: dict[str, list[object]] = {}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name == "DomainError":
                code = _text(node.args[0] if node.args else None, constants)
                if isinstance(code, str) and code.isupper():
                    message = _text(node.args[1] if len(node.args) > 1 else None, constants)
                    codes.setdefault(code, []).append(message)
            elif name == "raise_llm_domain_error":
                # 需要模型的节点：没配好模型 / 调用失败时的两个码由调用方给，说明是英文骨架
                for keyword in node.keywords:
                    if keyword.arg in {"capability_code", "failure_code"}:
                        code = _text(keyword.value, constants)
                        if isinstance(code, str):
                            codes.setdefault(code, []).append(DYNAMIC)
            for keyword in node.keywords:
                if keyword.arg == "error_prefix":
                    prefix = _text(keyword.value, constants)
                    if isinstance(prefix, str):
                        for suffix in ("_LLM_CALL_FAILED", "_LLM_RESPONSE_INVALID_SCHEMA"):
                            codes.setdefault(prefix + suffix, []).append(DYNAMIC)
                if keyword.arg == "invalid_code":
                    code = _text(keyword.value, constants)
                    if isinstance(code, str):
                        codes.setdefault(code, []).append(DYNAMIC)
    return codes


def _speaks_english(messages: list[object]) -> bool:
    return any(message is DYNAMIC or not CJK.search(str(message)) for message in messages)


def test_every_english_domain_error_code_has_a_chinese_message() -> None:
    codes = _raised_codes()
    assert len(codes) > 250  # 不是空跑
    missing = sorted(code for code, messages in codes.items() if _speaks_english(messages) and code not in ERROR_MESSAGES)
    assert missing == [], "这些错误码的说明是英文，给它们在 api/error_catalog.py 里配一句中文：" + ", ".join(missing)


def test_catalog_messages_are_chinese_and_use_only_the_message_placeholder() -> None:
    not_chinese = sorted(code for code, text in ERROR_MESSAGES.items() if not CJK.search(text))
    assert not_chinese == []
    bad_placeholders = sorted(
        code for code, text in ERROR_MESSAGES.items() if set(re.findall(r"\{[^}]*\}", text)) - {"{message}"}
    )
    assert bad_placeholders == []


def test_catalog_lists_no_code_the_backend_no_longer_uses() -> None:
    literals = {
        node.value
        for path in _source_files()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    raised = set(_raised_codes())
    stale = sorted(code for code in ERROR_MESSAGES if code not in raised and code not in SHELL_CODES and code not in literals)
    assert stale == []


def test_localized_message_keeps_chinese_and_carries_english_detail_in_the_placeholder() -> None:
    assert localized_message("SELECTION_LOCKED", "terminal selection is locked") == (ERROR_MESSAGES["SELECTION_LOCKED"], True)
    assert localized_message("SELECTION_LOCKED", "终选已经锁定。") == ("终选已经锁定。", False)
    assert localized_message("NOT_IN_THE_CATALOG", "english text") == ("english text", False)
    text, replaced = localized_message("CONFIG_ROUTE_MODEL_MISSING", "model m1 is not listed by provider p1")
    assert replaced and text == "模型分工用了服务商没有列出的模型（model m1 is not listed by provider p1）。"


def _app_raising(exc: Exception):
    app = create_app()

    @app.get("/api/v2/error-catalog-probe")
    def probe():
        raise exc

    return app


def test_domain_error_envelope_speaks_chinese_and_hides_the_english_original() -> None:
    app = _app_raising(DomainError("HARD_BLOCKED", "verified Q0/Q1 findings block adoption", status_code=409, details={"scene_id": "s1"}))
    with TestClient(app) as client:
        response = client.get("/api/v2/error-catalog-probe")

    error = response.json()["error"]
    assert response.status_code == 409
    assert error == {"code": "HARD_BLOCKED", "message": ERROR_MESSAGES["HARD_BLOCKED"], "details": {"scene_id": "s1"}}


def test_debug_message_carries_the_original_only_when_detail_is_exposed(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL", "true")
    app = _app_raising(DomainError("HARD_BLOCKED", "verified Q0/Q1 findings block adoption", status_code=409))
    with TestClient(app) as client:
        error = client.get("/api/v2/error-catalog-probe").json()["error"]

    assert error["message"] == ERROR_MESSAGES["HARD_BLOCKED"]
    assert error["details"]["debug_message"] == "verified Q0/Q1 findings block adoption"


def test_a_chinese_domain_message_reaches_the_author_unchanged() -> None:
    app = _app_raising(DomainError("HARD_BLOCKED", "这一场有核实过的硬问题：先改稿。", status_code=409))
    with TestClient(app) as client:
        error = client.get("/api/v2/error-catalog-probe").json()["error"]

    assert error["message"] == "这一场有核实过的硬问题：先改稿。"
    assert "debug_message" not in error["details"]


def test_validation_errors_speak_chinese_without_echoing_input(client) -> None:
    response = client.post("/api/v2/projects", json={"title": "雨城", "outline_text": "旧信", "unexpected": "机密原文"})

    error = response.json()["error"]
    assert response.status_code == 422
    assert error["code"] == "REQUEST_VALIDATION_FAILED"
    assert error["message"] == ERROR_MESSAGES["REQUEST_VALIDATION_FAILED"]
    assert error["details"]["issues"] == [{"field": "body.unexpected", "type": "extra_forbidden", "message": "不认识的字段"}]
    assert "机密原文" not in response.text
