"""进程级缓存的复位登记处（``novel_system.cache_registry``）与 conftest 的逐用例复位（X04-16）。

每个用例一个新库、却反复用同样的字面 id：按库内容缓存的东西必须在用例之间清空。缓存在定义处登记复位函数，
``tests/conftest.py`` 的 ``_hermetic_test_process`` 在每个用例前后调 ``reset_all_caches()``。
"""

from __future__ import annotations

import importlib

import pytest

from novel_system import cache_registry
from novel_system.cache_registry import register_cache_reset, registered_cache_names, reset_all_caches

# 已知的进程级缓存：模块 → 它在定义处登记的名字。新加一处按库内容缓存的地方，就在这里加一行。
KNOWN_CACHES = {
    "novel_system.services.llm_degrade": ("llm_degrade.connectivity_caps",),
    "novel_system.services.pricing": ("pricing.price_book",),
    "novel_system.services.reference_copy_gate": ("reference_copy_gate",),
    "novel_system.services.scene_diagnosis": ("scene_diagnosis.findings", "scene_diagnosis.reference_craft"),
    "novel_system.services.scene_run_jobs": ("scene_run_jobs.cancelled_hints",),
    "novel_system.services.style_policy": ("style_policy",),
    "novel_system.services.style_reference.config_loader": ("style_reference.config_loader",),
    "novel_system.services.style_reference.fidelity": ("style_reference.fidelity.reference_distribution",),
    "novel_system.services.style_reference.inject.render": ("style_reference.inject.render",),
    "novel_system.services.style_reference.measure": ("style_reference.measure.kernel",),
    "novel_system.services.style_reference.planning_context": ("style_reference.planning_context.chapter_titles",),
    "novel_system.services.style_reference.runtime_contract": ("style_reference.runtime_contract.validated",),
}


@pytest.mark.parametrize("module_name", sorted(KNOWN_CACHES))
def test_each_known_cache_registers_its_reset_where_it_is_defined(module_name: str) -> None:
    importlib.import_module(module_name)
    names = registered_cache_names()
    for name in KNOWN_CACHES[module_name]:
        assert name in names, f"{module_name} 的缓存 {name} 没有登记复位函数"


def test_registry_replaces_by_name_and_resets_everything(monkeypatch) -> None:
    monkeypatch.setattr(cache_registry, "_RESETS", {})
    calls: list[str] = []
    assert register_cache_reset("probe.a", lambda: calls.append("a-old")) is not None
    register_cache_reset("probe.a", lambda: calls.append("a"))  # 模块重新导入：同名覆盖
    register_cache_reset("probe.b", lambda: calls.append("b"))
    assert registered_cache_names() == ("probe.a", "probe.b")
    reset_all_caches()
    assert calls == ["a", "b"]
    with pytest.raises(ValueError):
        register_cache_reset("  ", lambda: None)
    with pytest.raises(TypeError):
        register_cache_reset("probe.c", "not callable")  # type: ignore[arg-type]


def _fill_every_known_cache() -> None:
    from novel_system.services import pricing, reference_copy_gate, scene_diagnosis, scene_run_jobs, style_policy
    from novel_system.services.style_reference import config_loader, fidelity, measure, planning_context, runtime_contract
    from novel_system.services.style_reference.inject import render

    pricing.load_price_book()
    reference_copy_gate._RESULT_CACHE[("probe",)] = object()
    scene_diagnosis._FINDINGS_CACHE[("probe",)] = {"findings": [], "waived": []}
    scene_diagnosis._REFERENCE_CRAFT_CACHE[("probe", 1, "")] = {}
    scene_run_jobs.remember_committed_cancellation("probe-job")
    style_policy._CACHE["probe"] = style_policy.UNBOUND
    config_loader.load_yaml_config("input_thresholds")
    fidelity._DIST_CACHE[("probe", "probe", "probe", "probe")] = object()
    render._CACHE["probe"] = object()
    measure.load_kernel_lexicon()
    planning_context._CHAPTER_TITLES_CACHE[("probe", 1)] = {}
    runtime_contract._VALIDATED["probe"] = "{}"


def test_caches_filled_by_one_test_part_1_fill() -> None:
    """与下一条成对：这一条把每个已知缓存都填上，下一条看它们在用例之间被 conftest 清空了。"""
    _fill_every_known_cache()


def test_caches_filled_by_one_test_part_2_are_empty_in_the_next() -> None:
    from novel_system.services import pricing, reference_copy_gate, scene_diagnosis, scene_run_jobs, style_policy
    from novel_system.services.style_reference import config_loader, fidelity, measure, planning_context, runtime_contract
    from novel_system.services.style_reference.inject import render

    assert pricing._CACHE is None
    assert not reference_copy_gate._RESULT_CACHE and not reference_copy_gate._INDEX_CACHE
    assert not scene_diagnosis._FINDINGS_CACHE and not scene_diagnosis._REFERENCE_CRAFT_CACHE
    assert scene_run_jobs.is_cancellation_cached("probe-job") is False
    assert not style_policy._CACHE
    assert config_loader._load_yaml.cache_info().currsize == 0
    assert not fidelity._DIST_CACHE
    assert not render._CACHE
    assert measure._kernel_lexicon.cache_info().currsize == 0
    assert not planning_context._CHAPTER_TITLES_CACHE
    assert not runtime_contract._VALIDATED
