"""硬事实检查器本身：检查表与可查的事实键一一对应；同一句里出现几个错地名时点名最先出现的那个。"""

from __future__ import annotations

from novel_system.services.narrative import hard_facts
from novel_system.services.narrative.hard_facts import EntityReference, check_fact_against_text
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
