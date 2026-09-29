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


def test_entity_reference_keeps_the_raw_id_as_a_fallback_name() -> None:
    reference = EntityReference.of("CHAR_LINYUAN", ("林远", "阿远", "林远"))
    assert reference.names == ("林远", "阿远", "char_linyuan")
    assert reference.labels == ("林远", "阿远", "CHAR_LINYUAN")
