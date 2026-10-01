"""定向长度补丁 / 有界风格挽救的合同提示要列出必写组（S2 3）。

必写内容按组查（批准#11）：``qc_constraints.required_groups`` 把 ``must_include_text`` 分成 ≥2 字的组，一整段分不出
这样的组时整段算一组——起草的确定性门、硬质检、分类器复核与成稿门都按这条查。补丁合同却用 ``constraint_terms``
列组，它把一个字的整段必写（「信」）丢掉了：合同里一个必写组都不提，补丁可以把那一个字改没，后面的门再拦下来。
"""

from __future__ import annotations

from types import SimpleNamespace

from novel_system.services.scene_generation.length_policy import LengthPolicy
from novel_system.services.scene_generation.segment_patch import (
    _style_length_patch_instruction,
    _style_salvage_instruction,
)

SOURCE = "林昭在码头拆开旧信。\n\n送信人不肯交出原件。\n\n雨下大了，她把信塞进怀里。"


def _length_rule(must_include_text: str | None) -> str:
    scene = SimpleNamespace(must_include_text=must_include_text)
    return _style_length_patch_instruction(
        scene,
        lengths=LengthPolicy(band="30-80"),
        source_length=120,
        editable_segment_ids=["S001"],
    )


def _salvage_rule(must_include_text: str | None) -> str:
    scene = SimpleNamespace(must_include_text=must_include_text)
    return _style_salvage_instruction(scene, source_content=SOURCE, editable_segment_ids=["S001"])


def test_a_one_character_must_include_is_a_required_group_in_both_patch_contracts() -> None:
    assert "Do not alter or remove any required constraint group: 信。" in _length_rule("信")
    assert "Preserve every required constraint group wherever it appears: 信。" in _salvage_rule("信")


def test_groups_split_like_the_gates_and_no_groups_means_no_rule() -> None:
    # ≥2 字的组照旧逐组列出（与以前的字节相同）
    assert "required constraint group: 旧信；码头。" in _length_rule("旧信，码头")
    assert "required constraint group wherever it appears: 旧信；码头。" in _salvage_rule("旧信、码头")
    # 分不出 ≥2 字的组：整段算一组（与 qc_constraints.required_groups 同一口径）
    assert "required constraint group: 信，伞。" in _length_rule("信，伞")
    # 没有必写内容：合同里没有这一句
    for empty in (None, "", "   "):
        assert "required constraint group" not in _length_rule(empty)
        assert "required constraint group" not in _salvage_rule(empty)
