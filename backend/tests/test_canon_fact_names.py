"""正史事实按人物名 / 别名对照正文，提示词摘要印名字（批准 #14，B11-01）。

事实按角色 id 存（作者在正史核对里采纳候选时解析成 ``StoryCharacter.character_id``），正文里写的是名字。
以前连续性检查拿 id 去正文里找，走产品路径定下的正史事实一条矛盾也查不出（硬质检与成稿门的 Q1 形同虚设），
摘要也把 id 当人名印进提示词。
"""

from __future__ import annotations

from novel_system.db.models import StoryCharacter
from novel_system.services.narrative_event_log import NarrativeEventLog
from tests.narrative_fixtures import (
    GUZHOU,
    LINYUAN,
    SUWAN,
    WORLD_PROJECT,
    WORLD_TARGET_SCENE,
    log_fixture_fact,
    seed_narrative_world,
    world_scene,
)

CAST = [LINYUAN, SUWAN, GUZHOU]


def _violations(session, text: str) -> list[tuple[str, str, str]]:
    report = NarrativeEventLog(session).check_consistency(text, WORLD_PROJECT, WORLD_TARGET_SCENE, character_ids=CAST)
    return [(violation.entity_id, violation.entity_name, violation.fact_key) for violation in report.violations]


def test_canon_fact_is_checked_against_the_characters_name_in_prose(session) -> None:
    """林远的断臂是走正史核对定下的：正文写「林远」「阿远」都查得出，写别人查不出。"""
    seed_narrative_world(session)

    assert _violations(session, "林远抬起右手握紧刀柄。") == [(LINYUAN, "林远", "missing_limb")]
    assert _violations(session, "阿远抬起右手握紧刀柄。") == [(LINYUAN, "林远", "missing_limb")]
    assert _violations(session, "顾舟抬起右手握紧刀柄。") == []


def test_location_entity_names_count_as_known_places(session) -> None:
    """苏晚已经去了北境；「还在钟楼」的钟楼是资料库里的地点（账本里按 id 记），也算已知地名。"""
    seed_narrative_world(session)

    report = NarrativeEventLog(session).check_consistency(
        "苏晚还在钟楼等消息。", WORLD_PROJECT, WORLD_TARGET_SCENE, character_ids=CAST
    )

    assert [(v.entity_id, v.fact_key, v.actual) for v in report.violations] == [
        (SUWAN, "location", "text places 苏晚 at 钟楼")
    ]


def test_another_name_of_the_same_place_is_not_a_wrong_place(session) -> None:
    """地点实体的几种写法指同一个地方：顾舟的位置记的是别名「旧钟楼」，正文写显示名「钟楼」不算把人放错了地方
    （地点实体的名字算进已知地名之后，这样写对的正文曾被判成 Q1）；位置记得更细（「钟楼顶上」）也一样。"""
    seed_narrative_world(session)
    log_fixture_fact(
        session, scene_id=world_scene(1, 2), event_type="location_change",
        entity_id=GUZHOU, fact_key="location", fact_value="旧钟楼",
    )
    session.commit()

    def wrong_places(text: str) -> list[tuple[str, str]]:
        report = NarrativeEventLog(session).check_consistency(text, WORLD_PROJECT, WORLD_TARGET_SCENE, character_ids=CAST)
        return [(v.entity_id, v.actual) for v in report.violations]

    assert wrong_places("顾舟还在钟楼等消息。") == []
    assert wrong_places("顾舟还在旧钟楼等消息。") == []
    assert wrong_places("顾舟还在北境等消息。") == [(GUZHOU, "text places 顾舟 at 北境")]

    log_fixture_fact(
        session, scene_id=world_scene(2, 1), event_type="location_change",
        entity_id=GUZHOU, fact_key="location", fact_value="钟楼顶上",
    )
    session.commit()
    assert wrong_places("顾舟还在旧钟楼等消息。") == []
    assert wrong_places("顾舟还在雨城等消息。") == [(GUZHOU, "text places 顾舟 at 雨城")]


def test_single_character_aliases_are_not_used_to_find_a_character(session) -> None:
    """单字别名在中文正文里几乎处处命中：只用两个字以上的名字找人。"""
    seed_narrative_world(session)
    character = session.get(StoryCharacter, LINYUAN)
    character.summary_json = {"aliases": ["阿远", "远"]}
    session.commit()

    assert _violations(session, "远处有人抬起右手握紧刀柄。") == []
    assert _violations(session, "阿远抬起右手握紧刀柄。") == [(LINYUAN, "林远", "missing_limb")]


def test_digests_name_characters_and_places_instead_of_printing_ids(session) -> None:
    seed_narrative_world(session)
    log = NarrativeEventLog(session)

    state = log.format_state_for_prompt(WORLD_PROJECT, scene_id=WORLD_TARGET_SCENE, onstage_character_ids=CAST)
    pov_state = log.format_state_for_prompt(
        WORLD_PROJECT, scene_id=WORLD_TARGET_SCENE, pov_character_id=LINYUAN, onstage_character_ids=CAST
    )
    asymmetry = log.information_asymmetry_digest(
        WORLD_PROJECT, scene_id=WORLD_TARGET_SCENE, onstage_character_ids=CAST, pov_character_id=LINYUAN
    )

    assert "### 林远 (CHAR_LINYUAN)" in state
    assert "### 钟楼 (ENT_BELL_TOWER)" in state
    assert "### CHAR_LINYUAN\n" not in state
    assert "- 角色 苏晚 掌握 林远 未知的信息；勿在 林远 视角泄漏其内容。" in pov_state
    assert "### 林远 独有认知（可据此行动）" in asymmetry


def test_ids_without_a_known_name_are_printed_as_before(session) -> None:
    """没有人物 / 资料库记录的 id（例如直接写进账本的旧数据）照旧原样印出，也照旧能按 id 查。"""
    seed_narrative_world(session)
    log_fixture_fact(
        session, scene_id=world_scene(1, 1), event_type="character_state",
        entity_id="路人甲", fact_key="alive", fact_value="dead",
    )
    session.commit()
    log = NarrativeEventLog(session)

    state = log.format_state_for_prompt(WORLD_PROJECT, scene_id=WORLD_TARGET_SCENE, onstage_character_ids=["路人甲"])
    report = log.check_consistency("路人甲站起身说话。", WORLD_PROJECT, WORLD_TARGET_SCENE, character_ids=["路人甲"])

    assert "### 路人甲\n- alive: dead" in state
    assert [(v.entity_id, v.entity_name, v.fact_key) for v in report.violations] == [("路人甲", "路人甲", "alive")]


def test_name_and_taxonomy_modules_stay_leaves() -> None:
    """人物名与叙事分类表是谁都能引的叶子：只依赖表模型（分类表连表模型都不依赖）。"""
    from tests.support.import_graph import PACKAGE_ROOT, imports_of as _imports, modules_under as _modules_under

    modules = {module: path for path, module in _modules_under(PACKAGE_ROOT).items()}
    allowed = {
        "novel_system.services.character_names": {"novel_system.db.models"},
        "novel_system.services.narrative.taxonomy": set(),
    }
    violations = [
        f"{leaf}:{line} imports {target}"
        for leaf, permitted in allowed.items()
        for target, line in _imports(modules[leaf])
        if target.startswith("novel_system") and target not in permitted
    ]
    assert not violations, violations
