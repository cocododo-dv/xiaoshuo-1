"""Style Reference (v1.1) dimensions module 单元测试。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

from novel_system.services.style_reference.dimensions import (
    LAYER_TO_SUB_DIMS,
    Layer,
    SubDimension,
)


def test_layer_count_is_four() -> None:
    assert len(list(Layer)) == 4


def test_sub_dimension_count_is_sixteen() -> None:
    assert len(list(SubDimension)) == 16


def test_layer_to_sub_dims_strict_four_each() -> None:
    assert set(LAYER_TO_SUB_DIMS.keys()) == set(Layer)
    for layer, subs in LAYER_TO_SUB_DIMS.items():
        assert len(subs) == 4, f"layer {layer.value} 不是严格 4 条 sub_dim"
        for sub in subs:
            assert sub.value.startswith(f"{layer.value}.")


def test_sub_dimension_values_match_design_doc() -> None:
    # 与《v1.1 手册》§3 dimensions.py 与 §6.1 task_name 命名严格对账
    expected = {
        "language.sentence_structure",
        "language.vocabulary",
        "language.rhetoric",
        "language.punctuation",
        "narrative.perspective",
        "narrative.pacing",
        "narrative.time_handling",
        "narrative.information_density",
        "scene.environment",
        "scene.character_portrayal",
        "scene.dialogue",
        "scene.sensory_priority",
        "theme.emotional_tone",
        "theme.values",
        "theme.motifs",
        "theme.narrative_philosophy",
    }
    assert {sub.value for sub in SubDimension} == expected
