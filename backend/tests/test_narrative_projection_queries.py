"""叙事摘要、POV 投影与连续性检查的查询数不随角色 / 实体 / 前文场数增长（B11-06）。

以前每个在场角色、每个地点 / 物品、每条秘密的知情判定各查一次库，「最近已提交的正史变化」从这一场往前逐场
各查三次；一次起草光这几段就有上百条语句。现在每个入口取一份快照（一趟按位置排序的查询）。
只数 SELECT：事务开头的 PRAGMA 由数据库会话层决定，不归这里管。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import event

from novel_system.db.models import SceneCard, StoryCharacter
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.pov_knowledge_projection import PovKnowledgeProjection
from tests.narrative_fixtures import (
    GUZHOU,
    LINYUAN,
    SUWAN,
    WORLD_PROJECT,
    WORLD_TARGET_SCENE,
    log_fixture_fact,
    seed_narrative_world,
    world_chapter,
    world_scene,
)

BASE_CAST = [LINYUAN, SUWAN, GUZHOU]


def _selects(session, action: Callable[[], object]) -> int:
    engine = session.get_bind()
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    session.expire_all()
    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def _grow_cast(session, extra: int) -> list[str]:
    """再加 ``extra`` 个角色：每人一个位置、一条获知、一个秘密，外加同样多的地点。"""
    cast = list(BASE_CAST)
    for index in range(extra):
        character_id = f"CHAR_EXTRA_{index:02d}"
        session.add(
            StoryCharacter(
                character_id=character_id,
                project_id=WORLD_PROJECT,
                display_name=f"路人{index}",
                status="active",
            )
        )
        session.flush()
        scene_id = world_scene(1, 1 + index % 3)
        log_fixture_fact(
            session, scene_id=scene_id, event_type="location_change",
            entity_id=character_id, fact_key="location", fact_value="雨城",
        )
        log_fixture_fact(
            session, scene_id=scene_id, event_type="character_learns",
            entity_id=character_id, fact_key=f"knows_{index}", fact_value=f"第{index}条消息",
        )
        log_fixture_fact(
            session, scene_id=scene_id, event_type="character_state",
            entity_id=character_id, fact_key="secret_held_by", fact_value=f"路人{index}的秘密",
        )
        log_fixture_fact(
            session, scene_id=scene_id, event_type="character_state",
            entity_type="location", entity_id=f"ENT_PLACE_{index:02d}", fact_key="weather", fact_value="雨",
        )
        cast.append(character_id)
    session.commit()
    return cast


def _digest_calls(session, cast: list[str]) -> dict[str, Callable[[], object]]:
    log = NarrativeEventLog(session)
    projection = PovKnowledgeProjection(session, event_log=log)
    target = WORLD_TARGET_SCENE
    return {
        "state_pov": lambda: log.format_state_for_prompt(
            WORLD_PROJECT, None, scene_id=target, pov_character_id=LINYUAN, onstage_character_ids=cast
        ),
        "state_omniscient": lambda: log.format_state_for_prompt(
            WORLD_PROJECT, None, scene_id=target, onstage_character_ids=cast
        ),
        "asymmetry_pov": lambda: log.information_asymmetry_digest(
            WORLD_PROJECT, None, cast, scene_id=target, pov_character_id=LINYUAN
        ),
        "asymmetry_omniscient": lambda: log.information_asymmetry_digest(
            WORLD_PROJECT, None, cast, scene_id=target
        ),
        "redact_brief": lambda: projection.redact_brief(
            ["节奏再紧一点", "把秘密写得更隐晦"],
            WORLD_PROJECT,
            None,
            scene_id=target,
            pov_character_id=LINYUAN,
            onstage_character_ids=cast,
        ),
        "check_consistency": lambda: log.check_consistency(
            "林远抬起右手握紧刀柄。", WORLD_PROJECT, target, character_ids=cast
        ),
    }


def test_digest_statement_count_does_not_grow_with_the_cast(session) -> None:
    seed_narrative_world(session)
    small = {name: _selects(session, call) for name, call in _digest_calls(session, BASE_CAST).items()}
    big_cast = _grow_cast(session, 8)
    big = {name: _selects(session, call) for name, call in _digest_calls(session, big_cast).items()}

    assert big == small, {name: (small[name], big[name]) for name in small}
    assert max(small.values()) <= 8, small


def test_recent_checkpoint_statement_count_does_not_grow_with_earlier_scenes(session) -> None:
    seed_narrative_world(session)
    canon = CanonContinuityService(session)

    def checkpoint() -> object:
        return canon.format_recent_checkpoint_for_prompt(WORLD_PROJECT, WORLD_TARGET_SCENE)

    before = _selects(session, checkpoint)
    for seq in range(4, 16):
        session.add(
            SceneCard(
                scene_id=world_scene(1, seq),
                chapter_id=world_chapter(1),
                project_id=WORLD_PROJECT,
                scene_seq=seq,
                scene_goal=f"CH01 加场 {seq}",
            )
        )
    session.commit()
    after = _selects(session, checkpoint)

    assert after == before, (before, after)
    assert before <= 12, before
