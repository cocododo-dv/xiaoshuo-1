"""硬事实检查器本身：检查表与可查的事实键一一对应；同一句里出现几个错地名时点名最先出现的那个；
事实键的词表（抽取 / 补录写成硬质检认得的键）与给作者看的「硬质检核对」说明。"""

from __future__ import annotations

import pytest

from novel_system.services.narrative import hard_facts
from novel_system.services.narrative.fact_vocabulary import (
    EXTRACTION_FACT_KINDS,
    FACT_KINDS,
    checkable_form,
    compose_extracted_fact,
    normalize_fact,
)
from novel_system.services.narrative.hard_facts import EntityReference, check_fact_against_text, hard_check_note
from novel_system.services.narrative.taxonomy import CHECKABLE_FACT_KEYS


def test_every_checkable_fact_key_has_exactly_one_checker() -> None:
    assert set(hard_facts._FACT_CHECKERS) == set(CHECKABLE_FACT_KEYS)


def test_unknown_fact_keys_are_not_checked() -> None:
    assert check_fact_against_text("林远说了话", EntityReference.of("林远"), "mood", "sad") is None


def test_wrong_location_names_the_place_that_comes_first_in_the_clause() -> None:
    """以前按集合次序取错地名，同一句正文在不同进程里点名的地方可能不一样。"""
    violation = check_fact_against_text(
        "苏晚还在雨城的钟楼下等消息",
        EntityReference.of("苏晚"),
        "location",
        "北境",
        known_locations={"北境", "钟楼", "雨城"},
    )
    assert violation is not None
    assert violation.actual == "text places 苏晚 at 雨城"


def test_every_name_of_the_expected_place_entity_counts_as_the_right_place() -> None:
    """同一个地点实体的写法（id、显示名、别名）是一组：事实记其中一个、正文写另一个，不是错地方；
    事实值里写着这个实体的名字（「钟楼顶上」）也算它。组外的已知地名照旧算错地方。"""
    tower = frozenset({"ent_bell_tower", "钟楼", "旧钟楼"})
    places = {"known_locations": set(tower) | {"北境"}, "place_groups": (tower,)}
    guzhou = EntityReference.of("CHAR_GUZHOU", ("顾舟",), display="顾舟")

    assert check_fact_against_text("顾舟还在钟楼等消息", guzhou, "location", "旧钟楼", **places) is None
    assert check_fact_against_text("顾舟还在旧钟楼等消息", guzhou, "location", "钟楼顶上", **places) is None
    elsewhere = check_fact_against_text("顾舟还在北境等消息", guzhou, "location", "旧钟楼", **places)
    assert elsewhere is not None and elsewhere.actual == "text places 顾舟 at 北境"
    back_at_tower = check_fact_against_text("顾舟还在旧钟楼等消息", guzhou, "location", "北境", **places)
    assert back_at_tower is not None and back_at_tower.actual == "text places 顾舟 at 旧钟楼"


def test_entity_reference_keeps_the_raw_id_as_a_fallback_name() -> None:
    reference = EntityReference.of("CHAR_LINYUAN", ("林远", "阿远", "林远"), display="林远")
    assert reference.names == ("林远", "阿远", "char_linyuan")
    assert reference.labels == ("林远", "阿远", "CHAR_LINYUAN")
    assert reference.display == "林远"
    assert EntityReference.of("CHAR_X").display == "CHAR_X"


# ---------------------------------------------------------------------------
# 事实键的词表（narrative/fact_vocabulary.py，2026-10-14）：抽取 / 补录的事实写成硬质检认得的键
# ---------------------------------------------------------------------------


def test_extraction_fact_kinds_are_the_checkable_keys_plus_other() -> None:
    assert set(FACT_KINDS) == set(CHECKABLE_FACT_KEYS)
    assert EXTRACTION_FACT_KINDS == (*FACT_KINDS, "other")


@pytest.mark.parametrize(
    ("event_type", "key", "value", "expected"),
    [
        # 位置变化一律记成 location
        ("location_change", "moved_to", "北境", ("location", "北境")),
        ("character_state", "位置", "钟楼", ("location", "钟楼")),
        # 早先的 has_item = lost:X / 物品名 → 每件一格
        ("character_state", "has_item", "lost:断剑", ("has_item:断剑", "lost")),
        ("character_state", "has_item", "旧信", ("has_item:旧信", "held")),
        ("character_state", "item_gained", "获得了铜钥匙", ("has_item:铜钥匙", "held")),
        ("character_state", "item_lost", "盐钟", ("has_item:盐钟", "lost")),
        ("character_state", "has_item:旧信", "交给苏晚保管", ("has_item:旧信", "lost")),
        ("character_state", "has_item:旧信", "又找回了", ("has_item:旧信", "held")),
        # 读不出干净物品名的句子原样留着
        ("character_state", "item_gained", "从钟楼废墟里捡回一枚生锈的铜铃当作念想", (
            "item_gained", "从钟楼废墟里捡回一枚生锈的铜铃当作念想",
        )),
        # 生死：认得的值才改写；假死不算死
        ("character_state", "生死", "遇害身亡", ("alive", "dead")),
        ("character_state", "alive", "假死脱身", ("alive", "假死脱身")),
        ("character_state", "death", "下落不明", ("death", "下落不明")),
        ("character_state", "ability:游泳", "再也不能下水", ("ability:游泳", "cannot")),
        ("character_state", "appearance:头发", "白色", ("appearance:hair", "白色")),
        ("character_state", "身体状况", "昏迷", ("physical_state", "昏迷")),
        # 「得知」「关系变化」用了可查键名就让开，不然会被当成这个人自己的状态
        ("character_learns", "location", "苏晚在码头", ("learned_location", "苏晚在码头")),
        ("relation_change", "has_item:旧信", "怀疑", ("relation_has_item:旧信", "怀疑")),
        ("character_learns", "learned_killer", "凶手是顾舟", ("learned_killer", "凶手是顾舟")),
        ("character_state", "oath", "立誓守钟", ("oath", "立誓守钟")),
    ],
)
def test_normalize_fact_rewrites_only_unambiguous_facts(event_type, key, value, expected) -> None:
    assert normalize_fact(event_type, key, value) == expected


@pytest.mark.parametrize(
    ("event_type", "kind", "key", "value", "expected"),
    [
        ("character_state", "has_item", "铜钥匙", "held", ("has_item:铜钥匙", "held")),
        ("character_state", "has_item", "has_item:铜钥匙", "lost", ("has_item:铜钥匙", "lost")),
        ("character_state", "location", "anything", "钟楼", ("location", "钟楼")),
        ("character_state", "alive", "alive", "dead", ("alive", "dead")),
        ("character_state", "appearance", "瞳色", "绿色", ("appearance:eyes", "绿色")),
        ("character_state", "other", "injury_left_leg", "擦伤", ("injury_left_leg", "擦伤")),
        # 表外的种类按 other；得知的事永远不是可查种类
        ("character_state", "mood", "injury", "右臂骨折", ("injury", "右臂骨折")),
        ("character_learns", "has_item", "铜钥匙", "held", ("铜钥匙", "held")),
        ("character_learns", "location", "location", "码头", ("learned_location", "码头")),
    ],
)
def test_compose_extracted_fact_builds_the_canonical_key(event_type, kind, key, value, expected) -> None:
    assert compose_extracted_fact(event_type, kind, key, value) == expected


def test_checkable_form_folds_slots_back_into_the_checker_contracts() -> None:
    assert checkable_form("has_item:断剑", "lost") == ("has_item", "lost:断剑")
    assert checkable_form("has_item:断剑", "held") == ("has_item", "held")
    assert checkable_form("ability:游泳", "cannot") == ("ability", "cannot:游泳")
    assert checkable_form("appearance:发色", "黑色") == ("appearance", "hair:黑色")
    assert checkable_form("appearance:hair", "光头") == ("appearance", "光头")
    assert checkable_form("Location", "钟楼") == ("location", "钟楼")
    assert checkable_form("missing_log_pages", "被裁掉") is None


@pytest.mark.parametrize(
    ("key", "value", "note"),
    [
        ("alive", "dead", "已死，后文再写到说话、行动（回忆、遗体之类除外）会被拦下"),
        ("location", "钟楼", "在「钟楼」，后文若写成「还在」别的已知地点会被拦下"),
        ("has_item:断剑", "lost", "已失去「断剑」，后文再拿出来用会被拦下"),
        ("has_item", "lost:断剑", "已失去「断剑」，后文再拿出来用会被拦下"),
        ("missing_limb", "右臂", "失去右臂，后文再用它做事会被拦下"),
        ("physical_state", "昏迷", "昏迷，后文写到与之不符的动作会被拦下"),
        ("appearance:发色", "黑色", "发色黑色，后文写成别的颜色会被拦下"),
        ("ability:游泳", "cannot", "不能游泳，后文写到做成会被拦下"),
        ("has_item:断剑", "held", None),
        ("physical_state", "疲惫", None),
        ("physical_state", "左眼失明", None),
        ("alive", "alive", None),
        ("oath", "立誓守钟", None),
    ],
)
def test_hard_check_note_mirrors_what_the_checkers_enforce(key, value, note) -> None:
    assert hard_check_note(key, value) == note
