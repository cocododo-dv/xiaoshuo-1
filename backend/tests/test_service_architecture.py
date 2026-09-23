"""Architecture guards for service independence and dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
SERVICES_ROOT = SRC_ROOT / "novel_system" / "services"
API_ROOT = SRC_ROOT / "novel_system" / "api"
PACKAGE_ROOT = SRC_ROOT / "novel_system"


def _service_modules() -> dict[Path, str]:
    return _modules_under(SERVICES_ROOT)


def _modules_under(root: Path) -> dict[Path, str]:
    modules: dict[Path, str] = {}
    for path in root.rglob("*.py"):
        parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[path] = ".".join(parts)
    return modules


def _imports(path: Path) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno))
        elif isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
    return imports


def _service_graph() -> dict[str, set[str]]:
    return _module_graph(_service_modules(), prefix="novel_system.services")


def _module_graph(
    modules: dict[Path, str],
    *,
    prefix: str,
) -> dict[str, set[str]]:
    known = set(modules.values())
    graph = {module: set() for module in known}
    for path, module in modules.items():
        for target, _line in _imports(path):
            if not target.startswith(prefix):
                continue
            candidate = target
            while candidate and candidate not in known:
                candidate = candidate.rpartition(".")[0]
            if candidate in known and candidate != module:
                graph[module].add(candidate)
    return graph


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for dependency in graph[node]:
            if dependency not in indexes:
                visit(dependency)
                lowlinks[node] = min(lowlinks[node], lowlinks[dependency])
            elif dependency in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[dependency])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            item = stack.pop()
            on_stack.remove(item)
            component.append(item)
            if item == node:
                break
        if len(component) > 1:
            components.append(sorted(component))

    for module in sorted(graph):
        if module not in indexes:
            visit(module)
    return sorted(components)


def test_service_dependency_graph_has_no_cycles() -> None:
    assert _strongly_connected_components(_service_graph()) == []


def test_full_package_dependency_graph_has_no_cycles() -> None:
    modules = _modules_under(PACKAGE_ROOT)
    graph = _module_graph(modules, prefix="novel_system")
    assert _strongly_connected_components(graph) == []


def test_services_never_depend_on_operator_tools() -> None:
    violations = [
        f"{path.relative_to(SRC_ROOT)}:{line} -> {target}"
        for path in _service_modules()
        for target, line in _imports(path)
        if target.startswith("novel_system.tools")
    ]
    assert violations == []


def test_runtime_api_never_depends_on_operator_tools() -> None:
    violations = [
        f"{path.relative_to(SRC_ROOT)}:{line} -> {target}"
        for path in _modules_under(API_ROOT)
        for target, line in _imports(path)
        if target.startswith("novel_system.tools")
    ]
    assert violations == []


def test_runtime_never_infers_project_ownership_from_structured_ids() -> None:
    """项目归属必须来自关系字段，不能依赖 ``*_CH_*`` 等命名约定。"""

    violations: list[str] = []
    for root in (SERVICES_ROOT, API_ROOT):
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "chapter_id.rsplit(" in source or "chapter_id.split(" in source:
                violations.append(str(path.relative_to(SRC_ROOT)))
    assert violations == []


def test_alembic_bootstrap_does_not_read_database_backed_runtime_config() -> None:
    alembic_env = SRC_ROOT.parent / "alembic" / "env.py"
    source = alembic_env.read_text(encoding="utf-8")
    assert "load_database_runtime().database_url" in source
    assert "novel_system.settings" not in source


# 风格参考 v3（V8）：「有没有风格绑定、要不要让位」只有一个判定来源——StylePolicy。
# 旧的三个判定函数只允许 style_policy（把它们包成策略）与 runtime_contract（定义处）调用；
# 管线节点一律 style_policy_for_bundle / style_policy_for_scene / style_policy_live。
_STYLE_BINDING_PREDICATES = frozenset(
    {"is_style_bound", "effective_draft_mode", "resolve_style_runtime_contract_state"}
)
_STYLE_BINDING_PREDICATE_OWNERS = frozenset(
    {
        "novel_system/services/style_policy.py",
        "novel_system/services/style_reference/runtime_contract.py",
    }
)
# 注入渲染端（P4 的 inject/ 包会取代它，届时从这里删掉）：只准减少，不准增加。
_STYLE_BINDING_PREDICATE_PENDING = frozenset(
    {"novel_system/services/style_prompt_injection.py"}
)


def _style_binding_predicate_uses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    uses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _STYLE_BINDING_PREDICATES:
                    uses.append(f"{node.lineno}:import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in _STYLE_BINDING_PREDICATES:
                uses.append(f"{node.lineno}:call {name}")
    return uses


def test_only_style_policy_resolves_style_binding_state() -> None:
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative in _STYLE_BINDING_PREDICATE_OWNERS or relative in _STYLE_BINDING_PREDICATE_PENDING:
            continue
        violations.extend(f"{relative}:{use}" for use in _style_binding_predicate_uses(path))
    assert violations == [], "绑定 / 让位判定只走 StylePolicy（style_policy_for_bundle / _for_scene / _live）"
    for relative in _STYLE_BINDING_PREDICATE_PENDING:
        path = SRC_ROOT / relative
        assert not path.exists() or _style_binding_predicate_uses(path), (
            f"{relative} 已不再调用旧判定函数：把它从 _STYLE_BINDING_PREDICATE_PENDING 里删掉"
        )
