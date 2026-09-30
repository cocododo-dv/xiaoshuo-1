"""钉住叙事连续性的提示词摘要与检查结果（B11-06 单趟投影之前的安全网）。

每个摘要逐字节对照 ``tests/golden/narrative/digests.json``：全知 / POV 的状态摘要与信息差摘要、最近已提交的正史
变化、POV 已知 / 被抑制的秘密集合、自动补丁简报脱敏、连续性检查的违例。输出有意变化时（例如摘要改用人物名），
用 ``NARRATIVE_GOLDEN_REGEN=1`` 重新生成，并在提交说明里写清为什么变。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.pov_knowledge_projection import PovKnowledgeProjection
from tests.narrative_fixtures import (
    GUZHOU,
    LINYUAN,
    SUWAN,
    WORLD_PROJECT,
    WORLD_TARGET_SCENE,
    seed_narrative_world,
    world_scene,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "narrative" / "digests.json"
ONSTAGE = [LINYUAN, SUWAN, GUZHOU]


def _consistency(log: NarrativeEventLog, text: str) -> list[dict[str, Any]]:
    report = log.check_consistency(text, WORLD_PROJECT, WORLD_TARGET_SCENE, character_ids=ONSTAGE)
    return [
        {
            "fact_key": violation.fact_key,
            "entity_id": violation.entity_id,
            "entity_name": violation.entity_name,
            "expected": violation.expected,
            "actual": violation.actual,
            "evidence": violation.evidence,
            "source": violation.source,
        }
        for violation in report.violations
    ] + [{"facts_checked": report.facts_checked}]


def _collect(session) -> dict[str, Any]:
    log = NarrativeEventLog(session)
    projection = PovKnowledgeProjection(session, event_log=log)
    canon = CanonContinuityService(session)
    target = WORLD_TARGET_SCENE
    return {
        "state_omniscient_onstage": log.format_state_for_prompt(
            WORLD_PROJECT, scene_id=target, onstage_character_ids=ONSTAGE
        ),
        "state_omniscient_all_characters": log.format_state_for_prompt(
            WORLD_PROJECT, scene_id=target
        ),
        "state_omniscient_chapter_two_opening": log.format_state_for_prompt(
            WORLD_PROJECT, scene_id=world_scene(2, 1)
        ),
        "state_pov_linyuan": log.format_state_for_prompt(
            WORLD_PROJECT, scene_id=target, pov_character_id=LINYUAN, onstage_character_ids=ONSTAGE
        ),
        "state_pov_guzhou": log.format_state_for_prompt(
            WORLD_PROJECT, scene_id=target, pov_character_id=GUZHOU, onstage_character_ids=ONSTAGE
        ),
        "asymmetry_omniscient_suwan_guzhou": log.information_asymmetry_digest(
            WORLD_PROJECT, scene_id=target, onstage_character_ids=[SUWAN, GUZHOU]
        ),
        # 林远对苏晚有两条独有认知：两条的先后必须与进程的字符串哈希种子无关
        "asymmetry_omniscient_onstage": log.information_asymmetry_digest(
            WORLD_PROJECT, scene_id=target, onstage_character_ids=ONSTAGE
        ),
        "asymmetry_pov_linyuan": log.information_asymmetry_digest(
            WORLD_PROJECT, scene_id=target, onstage_character_ids=ONSTAGE, pov_character_id=LINYUAN
        ),
        "asymmetry_pov_suwan": log.information_asymmetry_digest(
            WORLD_PROJECT, scene_id=target, onstage_character_ids=ONSTAGE, pov_character_id=SUWAN
        ),
        "asymmetry_single_onstage": log.information_asymmetry_digest(
            WORLD_PROJECT, scene_id=target, onstage_character_ids=[LINYUAN], pov_character_id=LINYUAN
        ),
        "recent_checkpoint": canon.format_recent_checkpoint_for_prompt(WORLD_PROJECT, target),
        "recent_checkpoint_pov_linyuan": canon.format_recent_checkpoint_for_prompt(
            WORLD_PROJECT, target, pov_character_id=LINYUAN
        ),
        "pov_known_fact_values_linyuan": sorted(
            projection.pov_known_fact_values(WORLD_PROJECT, LINYUAN, scene_id=target)
        ),
        "suppressed_secret_values_linyuan": sorted(
            projection.suppressed_secret_values(WORLD_PROJECT, LINYUAN, ONSTAGE, scene_id=target)
        ),
        "suppressed_secret_values_guzhou": sorted(
            projection.suppressed_secret_values(WORLD_PROJECT, GUZHOU, ONSTAGE, scene_id=target)
        ),
        "redact_brief_linyuan": projection.redact_brief(
            [
                "把苏晚藏着半页旧信写得更隐晦",
                "顾舟以为案卷已经烧毁这一点要埋伏笔",
                "节奏再紧一点",
            ],
            WORLD_PROJECT,
            scene_id=target,
            pov_character_id=LINYUAN,
            onstage_character_ids=ONSTAGE,
        ),
        "consistency_prose_with_names": _consistency(
            log, "林远抬起右手握紧刀柄。苏晚还在雨城等消息。"
        ),
        "consistency_prose_with_ids": _consistency(
            log, "CHAR_LINYUAN抬起右手握紧刀柄。CHAR_SUWAN还在雨城等消息。"
        ),
    }


def test_narrative_digests_match_golden(session) -> None:
    seed_narrative_world(session)
    actual = _collect(session)
    if os.environ.get("NARRATIVE_GOLDEN_REGEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        assert actual[key] == value, key


def test_facts_after_the_boundary_never_reach_a_digest(session) -> None:
    """林远在 CH02_SC03 才到钟楼：CH02_SC02 之前的摘要里他还在雨城。"""
    seed_narrative_world(session)
    actual = _collect(session)
    states = {key: value for key, value in actual.items() if key.startswith("state_")}
    assert all("location: 钟楼" not in value for value in states.values())
    assert "location: 雨城" in states["state_omniscient_onstage"]
