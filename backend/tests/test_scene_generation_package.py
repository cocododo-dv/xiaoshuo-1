"""scene_generation 包的结构守卫：门面、子模块的依赖方向、叶子模块，以及规则文学分析的记忆（B02-01 / 14 / 19）。"""

from __future__ import annotations

import ast
from pathlib import Path

from novel_system.services import literary_signals
from novel_system.services import scene_generation as sg

SERVICES_ROOT = Path(__file__).resolve().parents[1] / "src" / "novel_system" / "services"
PACKAGE_ROOT = SERVICES_ROOT / "scene_generation"


def _imported_modules(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def test_facade_exports_every_name_it_lists() -> None:
    missing = [name for name in sg.__all__ if not hasattr(sg, name)]
    assert missing == []


def test_submodules_never_import_the_facade() -> None:
    """子模块只从兄弟子模块取东西；import 门面会在服务依赖图里成环（门面 import 了所有子模块）。"""
    offenders = [
        f"{path.name} -> {module}"
        for path in sorted(PACKAGE_ROOT.glob("*.py"))
        if path.name != "__init__.py"
        for module in _imported_modules(path)
        if module == "novel_system.services.scene_generation"
    ]
    assert offenders == []


def test_scene_form_is_a_leaf() -> None:
    imports = _imported_modules(SERVICES_ROOT / "scene_form.py")
    assert [module for module in imports if module.startswith("novel_system")] == []


def test_literary_signals_only_wraps_the_rule_analysis() -> None:
    imports = {module for module in _imported_modules(SERVICES_ROOT / "literary_signals.py") if module.startswith("novel_system")}
    assert imports == {"novel_system.cache_registry", "novel_system.services.literary_quality"}


def test_rule_analysis_runs_once_per_text_across_gate_and_critique(monkeypatch) -> None:
    """B02-19：同一份稿子被去模板门、规则批判、LLM 批判里的规则一遍各分析一次——现在按正文只算一次，
    每个调用方拿到的是独立的拷贝（改了也不污染下一个）。"""
    from novel_system.services.auto_critique import auto_critique, llm_auto_critique

    calls: list[str] = []
    real = literary_signals.analyze_literary_quality

    def counting(text, **kwargs):  # noqa: ANN001, ANN003
        calls.append(text)
        return real(text, **kwargs)

    monkeypatch.setattr(literary_signals, "analyze_literary_quality", counting)
    literary_signals._analysis.cache_clear()
    text = "她突然意识到一切都变了。门开了，他站在雨里，没有说话。" * 6

    gate = sg._anti_template_quality_gate(text, scene_id="SC_MEMO", chapter_id="CH_MEMO")
    first = auto_critique(text)
    second = llm_auto_critique(text, llm_runner=None)

    assert len(calls) == 1
    assert gate["findings"] and first.dimension_scores == second.dimension_scores
    signals, findings = literary_signals.rule_analysis(text)
    expected_findings = len(findings)
    signals.clear()
    findings.clear()
    again_signals, again_findings = literary_signals.rule_analysis(text)
    assert again_signals and len(again_findings) == expected_findings
    assert len(calls) == 1
