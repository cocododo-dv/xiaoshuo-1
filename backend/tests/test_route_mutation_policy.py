from __future__ import annotations

import ast
from pathlib import Path


ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "novel_system" / "api" / "routes"
MUTATION_METHODS = {"post", "put", "patch", "delete"}
# Route handlers must go through the one shared wrapper in api/mutations.py:
# - mutate: X-Idempotency-Key required (400 IDEMPOTENCY_KEY_REQUIRED when missing); method and path
#   template come from the matched route, never from a hand-copied literal.
# The optional-key shim (optional_idempotent_response / execute_with_optional_idempotency) was deleted
# (B12-05 / B09-26): the React client and every smoke send a key on each mutation. Raw
# execute_with_idempotency calls and per-file copies (_with_idem, _mutation_response aliases) must not
# reappear in route files.
IDEMPOTENCY_BOUNDARIES = {"mutate"}
READ_ONLY_POST_EXEMPTIONS = {
    ("style_reference/profiles.py", "dryrun_injection_preview"),
}


def _route_files() -> list[Path]:
    """路由目录下全部模块,含按领域拆成包的子模块(style_reference/…)。"""
    return sorted(ROUTES_DIR.rglob("*.py"))


def _route_file_key(path: Path) -> str:
    return path.relative_to(ROUTES_DIR).as_posix()


def _route_methods(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    methods: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in MUTATION_METHODS | {"get"}:
            methods.add(target.attr)
    return methods


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def test_every_http_mutation_has_an_idempotency_boundary_or_a_reviewed_read_only_exemption() -> None:
    uncovered: list[str] = []
    observed_exemptions: set[tuple[str, str]] = set()

    for path in _route_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods = _route_methods(node)
            if not methods.intersection(MUTATION_METHODS):
                continue
            route_key = (_route_file_key(path), node.name)
            if route_key in READ_ONLY_POST_EXEMPTIONS:
                segment = ast.get_source_segment(source, node) or ""
                assert "idempotency-exempt: deterministic read-only preview" in segment
                observed_exemptions.add(route_key)
                continue
            if not _called_names(node).intersection(IDEMPOTENCY_BOUNDARIES):
                uncovered.append(f"{_route_file_key(path)}:{node.lineno}:{node.name}")

    assert observed_exemptions == READ_ONLY_POST_EXEMPTIONS
    assert uncovered == []


def test_route_handlers_never_own_transactions_directly() -> None:
    offenders: list[str] = []
    for path in _route_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _route_methods(node):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "commit":
                    offenders.append(f"{_route_file_key(path)}:{node.lineno}:{node.name}")
                    break

    assert offenders == []


def test_route_files_do_not_hand_copy_method_or_path_template() -> None:
    """``mutate`` takes both from ``request.scope["route"]``; a hand-copied literal can only drift."""
    offenders: list[str] = []
    for path in _route_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(kw.arg in {"path_template", "method"} for kw in node.keywords):
                offenders.append(f"{_route_file_key(path)}:{node.lineno}")
    assert offenders == []
