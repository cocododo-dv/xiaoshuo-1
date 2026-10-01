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


def test_scene_form_is_the_one_reader_of_the_scene_form() -> None:
    """B02-14：结构简报、执行契约、关键度、设计上下文与长度策略都从 scene_form 取形态 / 呈现方式 / 文本值。"""
    readers = (
        SERVICES_ROOT / "scene_structure_brief.py",
        SERVICES_ROOT / "scene_execution.py",
        SERVICES_ROOT / "scene_criticality.py",
        SERVICES_ROOT / "scene_design_context.py",
        PACKAGE_ROOT / "length_policy.py",
    )
    assert [path.name for path in readers if "novel_system.services.scene_form" not in _imported_modules(path)] == []


def test_token_estimate_is_a_leaf_that_context_budget_reexports() -> None:
    """B02-21：估算器住在叶子 token_estimate；context_budget 的老导入路径是同一个对象。"""
    from novel_system.services import context_budget, token_estimate

    imports = {module for module in _imported_modules(SERVICES_ROOT / "token_estimate.py") if module.startswith("novel_system")}
    assert imports == {"novel_system.services.hash_engine"}
    assert context_budget.estimate_tokens is token_estimate.estimate_tokens
    assert context_budget.TOKEN_ESTIMATOR_VERSION == token_estimate.TOKEN_ESTIMATOR_VERSION


def test_scene_sections_is_the_blueprints_way_to_the_two_design_sections() -> None:
    """B02-13：结构简报与设计上下文怎么挂进来源快照只写一次（scene_sections）；蓝图不再自己挂。"""
    imports = {module for module in _imported_modules(SERVICES_ROOT / "scene_sections.py") if module.startswith("novel_system")}
    assert imports == {
        "novel_system.db.models",
        "novel_system.services.scene_design_context",
        "novel_system.services.scene_structure_brief",
    }
    blueprint = set(_imported_modules(SERVICES_ROOT / "scene_blueprint.py"))
    assert "novel_system.services.scene_sections" in blueprint
    assert not blueprint & {"novel_system.services.scene_design_context", "novel_system.services.scene_structure_brief"}


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


def test_the_anti_template_gate_score_is_the_shared_weighted_score() -> None:
    """B04-16：去模板门的分数走 ``literary_quality.weighted_score(..., normalize=True)``——与以前自己写的那一份
    （这组维度的 Σ 分数 × 权重 ÷ 权重和，四舍五入到 4 位）逐位相同。"""
    from novel_system.services.literary_quality import DIMENSION_WEIGHTS

    dims = sg.text_gates.ANTI_TEMPLATE_GATE_DIMENSIONS
    texts = (
        "她突然意识到，自己一直在等这一刻。月光照着月光下的院子。",
        "The witness held the key. The lead had to choose the archive or the child.",
        "他转身，转身，又转身。最后，一切都永远改变了。",
        "林昭把旧信折好，放回案卷里，雨停了。",
    )
    for text in texts:
        signals, _findings = literary_signals.rule_analysis(text)
        before = round(
            sum(signals[dim]["score"] * DIMENSION_WEIGHTS[dim] for dim in dims) / sum(DIMENSION_WEIGHTS[dim] for dim in dims),
            4,
        )
        assert sg.text_gates._anti_template_quality_gate(text, scene_id="S", chapter_id="C")["score"] == before


def test_the_drafting_gates_read_required_groups_like_qc() -> None:
    """批准#11：起草的确定性验收与硬质检、成稿门同一处按组读必写（``qc_constraints.required_groups_missing``）——
    一整段分不出 ≥2 字的组时整段算一组（以前起草这边分不出组就当没有必写）。"""
    from types import SimpleNamespace

    card = SimpleNamespace(must_include_text="主角交出钥匙，门外传来警笛", forbidden_text="黑伞|雨伞、钥匙链")
    snapshot = sg.text_gates.ConstraintSnapshot.read(card, "他犹豫很久，最后主角交出钥匙。她撑开雨伞。", None)
    assert snapshot.missing_required == ["门外传来警笛"]
    assert snapshot.forbidden_hits == ["黑伞|雨伞"]

    single = SimpleNamespace(must_include_text="钟", forbidden_text="")
    assert sg.text_gates.ConstraintSnapshot.read(single, "雨停了。", None).missing_required == ["钟"]
    assert sg.text_gates.ConstraintSnapshot.read(single, "钟响了。", None).missing_required == []
