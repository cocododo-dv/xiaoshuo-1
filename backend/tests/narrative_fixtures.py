"""叙事连续性测试的共用夹具：一个小作品世界，事实有两种来路。

* ``commit_scene_canon`` 走产品路径：作者手填候选 → 采纳 → 确认本场正史（正史核对面板做的事）。这样落下的事实
  带着和终稿哈希绑定的提交，运行时重放照产品规则认它们（B11-25：以前的用例直接往表里写事实，
  掩盖了「事实按角色 id 存、正文里写的是名字」这类问题）。
* ``log_fixture_fact`` 直接写一条已认可的非正史管理事件（``source_kind="test_fixture"``），用来覆盖产品路径
  造不出来的形状（例如带 ``knowledge_status`` 的「怀疑」）。

``seed_narrative_world`` 搭一个两章五场、三个角色、一个地点、一个物品的世界，摘要的 golden 用例与连续性检查
用例共用它。名字取自仓库里已有的合成世界（林远 / 苏晚 / 顾舟 / 雨城 / 钟楼 / 旧信 / 案卷）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    LibraryEntity,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    StoryProject,
)
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.narrative_event_log import NarrativeEventLog

WORLD_PROJECT = "PRJ_NARR_WORLD"
LINYUAN = "CHAR_LINYUAN"
SUWAN = "CHAR_SUWAN"
GUZHOU = "CHAR_GUZHOU"
BELL_TOWER = "ENT_BELL_TOWER"
OLD_LETTER = "ENT_OLD_LETTER"


def world_scene(chapter: int, scene: int) -> str:
    return f"{WORLD_PROJECT}_CH{chapter:02d}_SC{scene:02d}"


def world_chapter(chapter: int) -> str:
    return f"{WORLD_PROJECT}_CH{chapter:02d}"


def seed_final_scene(session: Session, *, scene: SceneCard, content: str) -> FinalScene:
    """给场景挂一份已归档的终稿和运行状态（正史核对的前提）。"""
    final = FinalScene(
        row_id=f"final_{scene.scene_id}_v1",
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        status="archived",
        source_bundle_id=f"bundle_{scene.scene_id}",
        source_bundle_hash="bundle-hash",
    )
    session.add(final)
    session.add(
        SceneRunState(
            scene_id=scene.scene_id,
            scene_status="archived",
            current_final_scene_row_id=final.row_id,
        )
    )
    session.flush()
    return final


def commit_scene_canon(
    session: Session,
    *,
    project_id: str,
    scene_id: str,
    facts: list[dict[str, Any]],
    note: str = "作者已核对本场正史",
) -> None:
    """走产品路径把 ``facts`` 定为本场正史：手填候选 → 逐条采纳 → 确认本场。

    每条 ``fact`` 是 ``create_manual_candidate`` 的关键字参数（``event_type`` / ``raw_entity_ref`` / ``fact_key`` /
    ``fact_value`` / ``evidence_text`` [/ ``entity_type``]），证据必须是终稿里的原文。"""
    service = CanonContinuityService(session)
    state = session.get(SceneRunState, scene_id)
    assert state is not None and state.current_final_scene_row_id, scene_id
    final_row_id = state.current_final_scene_row_id
    service.mark_archive_pending(final_row_id)
    for fact in facts:
        candidate = service.create_manual_candidate(project_id, scene_id, **fact)
        service.decide_candidate(
            project_id,
            candidate["candidate_id"],
            action="accept",
            actor_ref="author",
        )
    service.verify_scene_complete(
        project_id,
        scene_id,
        actor_ref="author",
        note=note,
        expected_final_scene_row_id=final_row_id,
    )
    session.flush()


def log_fixture_fact(session: Session, *, scene_id: str, **kwargs: Any):
    """直接写一条已认可、不归正史管理的事件（重放照单全收）。"""
    scene = session.get(SceneCard, scene_id)
    assert scene is not None, scene_id
    kwargs.setdefault("authority_status", "accepted")
    kwargs.setdefault("source_kind", "test_fixture")
    kwargs.setdefault("entity_type", "character")
    return NarrativeEventLog(session).log_event(
        project_id=scene.project_id,
        chapter_id=scene.chapter_id,
        scene_id=scene_id,
        **kwargs,
    )


_WORLD_SCENES: dict[tuple[int, int], dict[str, Any]] = {
    (1, 1): {"onstage": [LINYUAN, SUWAN], "pov": LINYUAN},
    (1, 2): {"onstage": [LINYUAN, GUZHOU], "pov": LINYUAN},
    (1, 3): {"onstage": [SUWAN], "pov": SUWAN},
    (2, 1): {
        "onstage": [LINYUAN, SUWAN, GUZHOU],
        "pov": LINYUAN,
        "final": "林远在钟楼下醒来，右臂受伤，一时抬不起来。苏晚已经动身去北境，只留下半页旧信。",
    },
    (2, 2): {"onstage": [LINYUAN, SUWAN, GUZHOU], "pov": LINYUAN},
    (2, 3): {"onstage": [LINYUAN], "pov": LINYUAN},
}

# 目标场：各摘要都取「第 2 章第 2 场之前」的状态；第 2 章第 3 场的事实在边界之后，不许出现。
WORLD_TARGET_SCENE = world_scene(2, 2)


def seed_narrative_world(session: Session) -> dict[str, Any]:
    session.add(StoryProject(project_id=WORLD_PROJECT, title="叙事连续性夹具", outline_text=""))
    session.flush()
    for chapter in (1, 2):
        session.add(
            ChapterGoal(
                chapter_id=world_chapter(chapter),
                project_id=WORLD_PROJECT,
                chapter_goal=f"第 {chapter} 章",
                planned_scene_count=3,
                display_order=chapter,
            )
        )
    session.flush()
    scenes: dict[tuple[int, int], SceneCard] = {}
    for (chapter, seq), spec in _WORLD_SCENES.items():
        scene = SceneCard(
            scene_id=world_scene(chapter, seq),
            chapter_id=world_chapter(chapter),
            project_id=WORLD_PROJECT,
            scene_seq=seq,
            scene_goal=f"第 {chapter} 章第 {seq} 场",
            pov_character_id=spec["pov"],
            onstage_chars_json=list(spec["onstage"]),
        )
        session.add(scene)
        scenes[(chapter, seq)] = scene
    session.add_all(
        [
            StoryCharacter(
                character_id=LINYUAN,
                project_id=WORLD_PROJECT,
                display_name="林远",
                summary_json={"aliases": ["阿远"]},
                status="active",
            ),
            StoryCharacter(character_id=SUWAN, project_id=WORLD_PROJECT, display_name="苏晚", status="active"),
            StoryCharacter(character_id=GUZHOU, project_id=WORLD_PROJECT, display_name="顾舟", status="active"),
            LibraryEntity(entity_id=BELL_TOWER, project_id=WORLD_PROJECT, kind="location", name="钟楼", aliases_json=["旧钟楼"]),
            LibraryEntity(entity_id=OLD_LETTER, project_id=WORLD_PROJECT, kind="item", name="旧信"),
        ]
    )
    session.flush()

    # 第 1 章：直接写的已认可事实
    log_fixture_fact(
        session, scene_id=world_scene(1, 1), event_type="location_change",
        entity_id=LINYUAN, fact_key="location", fact_value="雨城",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 1), event_type="character_state",
        entity_id=SUWAN, fact_key="secret_held_by", fact_value="苏晚藏着半页旧信",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 1), event_type="character_learns",
        entity_id=LINYUAN, fact_key="knows_letter", fact_value="旧信藏在钟楼",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 2), event_type="character_state",
        entity_id=GUZHOU, fact_key="believes_false", fact_value="顾舟以为案卷已经烧毁",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 2), event_type="character_learns",
        entity_id=LINYUAN, fact_key="suspects_insider", fact_value="城里有人通风报信",
        payload={"knowledge_status": "suspected"},
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 2), event_type="character_state",
        entity_type="location", entity_id=BELL_TOWER, fact_key="bell", fact_value="钟声停了",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 3), event_type="character_learns",
        entity_id=SUWAN, fact_key="knows_archive", fact_value="案卷在北境",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 3), event_type="item_change",
        entity_type="item", entity_id=OLD_LETTER, fact_key="holder", fact_value="苏晚",
    )
    log_fixture_fact(
        session, scene_id=world_scene(1, 3), event_type="character_state",
        entity_id=SUWAN, fact_key="revealed_to", fact_value=GUZHOU,
    )

    # 第 2 章第 1 场：走正史核对的产品路径
    seed_final_scene(session, scene=scenes[(2, 1)], content=_WORLD_SCENES[(2, 1)]["final"])
    commit_scene_canon(
        session,
        project_id=WORLD_PROJECT,
        scene_id=world_scene(2, 1),
        facts=[
            {
                "event_type": "character_state",
                "raw_entity_ref": "林远",
                "fact_key": "missing_limb",
                "fact_value": "右臂",
                "evidence_text": "右臂受伤，一时抬不起来",
            },
            {
                "event_type": "location_change",
                "raw_entity_ref": "苏晚",
                "fact_key": "location",
                "fact_value": "北境",
                "evidence_text": "苏晚已经动身去北境",
            },
        ],
    )

    # 边界之后的事实：任何「第 2 章第 2 场之前」的摘要都不该出现
    log_fixture_fact(
        session, scene_id=world_scene(2, 3), event_type="location_change",
        entity_id=LINYUAN, fact_key="location", fact_value="钟楼",
    )
    session.commit()
    return {"scenes": scenes}
