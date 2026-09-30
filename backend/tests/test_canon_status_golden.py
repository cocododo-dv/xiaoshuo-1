"""钉住正史核对的读模型：一章里各种状态的场，``scene_status`` / ``chapter_status`` 的完整载荷（B11-12 / B11-10 的安全网）。

成稿中心的正史面板与章节详情直接读这些载荷。改读取方式（批量查、不为重建章快照去序列化候选）或拆包之前先把它钉住：
随机 id 与时间戳按出现次序换成占位符，其余逐字节对照 ``tests/golden/narrative/canon_status.json``；
输出有意改变时用 ``NARRATIVE_GOLDEN_REGEN=1`` 重生成并写明原因。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from novel_system.db.models import ChapterGoal, SceneCard, StoryCharacter, StoryProject, TimelineEvent
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.narrative_event_log import NarrativeEventLog
from tests.narrative_fixtures import commit_scene_canon, seed_final_scene

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "narrative" / "canon_status.json"
PROJECT = "PRJ_CANON_STATUS"
CHAPTER = f"{PROJECT}_CH01"
_VOLATILE_ID = re.compile(r"(factcand|nevt)_[0-9a-f]{16,20}")
_TIMESTAMP_KEYS = {"created_at", "decided_at", "updated_at"}


def _scene(seq: int) -> str:
    return f"{CHAPTER}_SC{seq:02d}"


def _normalize(payload: Any) -> Any:
    mapping: dict[str, str] = {}

    def replace_ids(text: str) -> str:
        def placeholder(match: re.Match[str]) -> str:
            token = match.group(0)
            if token not in mapping:
                mapping[token] = f"<{match.group(1)}#{len(mapping) + 1}>"
            return mapping[token]

        return _VOLATILE_ID.sub(placeholder, text)

    def walk(value: Any, key: str | None = None) -> Any:
        if key in _TIMESTAMP_KEYS and value:
            return "<ts>"
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return replace_ids(value)
        return value

    return walk(payload)


def _seed(session) -> CanonContinuityService:
    session.add(StoryProject(project_id=PROJECT, title="正史读模型", outline_text=""))
    session.flush()
    session.add(ChapterGoal(chapter_id=CHAPTER, project_id=PROJECT, chapter_goal="读模型", planned_scene_count=6, display_order=1))
    session.flush()
    scenes = {}
    for seq in range(1, 8):
        scene = SceneCard(
            scene_id=_scene(seq),
            chapter_id=CHAPTER,
            project_id=PROJECT,
            scene_seq=seq,
            scene_goal=f"第 {seq} 场",
            trashed_flag=1 if seq == 7 else 0,
        )
        session.add(scene)
        scenes[seq] = scene
    session.add_all(
        [
            StoryCharacter(character_id="CHAR_LINYUAN", project_id=PROJECT, display_name="林远", summary_json={"aliases": ["阿远"]}, status="active"),
            StoryCharacter(character_id="CHAR_LINNING", project_id=PROJECT, display_name="林宁", summary_json={"aliases": ["阿远"]}, status="active"),
            StoryCharacter(character_id="CHAR_SUWAN", project_id=PROJECT, display_name="苏晚", status="active"),
            TimelineEvent(event_id="timeline_letter", project_id=PROJECT, label="旧信交到苏晚手里", event_mode="planned", realization_status="planned"),
        ]
    )
    session.flush()
    finals = {
        1: "林远在钟楼醒来，右臂已经折断。",
        2: "阿远把旧信塞进怀里，苏晚接过旧信。",
        3: "雨城一整夜都在下雨。",
        4: "苏晚在北境的驿站等了三天。",
        6: "这一场只有风声。",
        7: "回收站里的场。",
    }
    for seq, content in finals.items():
        seed_final_scene(session, scene=scenes[seq], content=content)
    service = CanonContinuityService(session)

    # 第 1 场：已核对（走产品路径）
    commit_scene_canon(
        session,
        project_id=PROJECT,
        scene_id=_scene(1),
        facts=[{
            "event_type": "character_state",
            "raw_entity_ref": "林远",
            "fact_key": "missing_limb",
            "fact_value": "右臂",
            "evidence_text": "右臂已经折断",
        }],
    )
    # 第 2 场：待核对——一条别名撞名的抽取候选（要作者选人），一条带计划时间线的手填候选，一条已拒绝
    final_2 = f"final_{_scene(2)}_v1"
    service.mark_archive_pending(final_2)
    log = NarrativeEventLog(session)
    ambiguous = log.log_event(
        project_id=PROJECT, chapter_id=CHAPTER, scene_id=_scene(2),
        event_type="character_state", entity_type="character", entity_id="阿远",
        fact_key="has_item", fact_value="旧信", confidence="extracted",
        source_text_excerpt="阿远把旧信塞进怀里", payload={"source": "prose"},
        authority_status="pending", source_kind="prose_extraction", final_scene_row_id=final_2,
    )
    rejected = log.log_event(
        project_id=PROJECT, chapter_id=CHAPTER, scene_id=_scene(2),
        event_type="character_state", entity_type="character", entity_id="苏晚",
        fact_key="mood", fact_value="紧张", confidence="extracted",
        source_text_excerpt="苏晚接过旧信", payload={"source": "prose"},
        authority_status="pending", source_kind="prose_extraction", final_scene_row_id=final_2,
    )
    staged = service.stage_extraction(final_2, outcome="completed_events", event_ids=[ambiguous.event_id, rejected.event_id])
    service.decide_candidate(PROJECT, staged["candidate_ids"][1], action="reject", actor_ref="author", note="情绪不是持久事实")
    service.create_manual_candidate(
        PROJECT, _scene(2),
        event_type="item_change", raw_entity_ref="苏晚", fact_key="holder", fact_value="旧信",
        evidence_text="苏晚接过旧信", entity_type="character", planned_timeline_event_id="timeline_letter",
    )
    # 第 3 场：归档后还没抽取
    service.mark_archive_pending(f"final_{_scene(3)}_v1")
    # 第 4 场：抽取调用失败（降级）
    service.mark_archive_pending(f"final_{_scene(4)}_v1")
    service.stage_extraction(
        f"final_{_scene(4)}_v1", outcome="provider_failed", event_ids=[],
        reason="provider_call_failed", error_code="LLM_PROVIDER_TIMEOUT",
    )
    # 第 5 场：没有终稿；第 6 场：抽取完成、一条也没有（要作者确认「本场没有持久事实」）
    service.mark_archive_pending(f"final_{_scene(6)}_v1")
    service.stage_extraction(f"final_{_scene(6)}_v1", outcome="completed_empty", event_ids=[])
    session.commit()
    return service


def test_canon_status_payloads_match_golden(session) -> None:
    service = _seed(session)
    actual = _normalize(
        {
            "chapter": service.chapter_status(PROJECT, CHAPTER),
            "scenes": {seq: service.scene_status(PROJECT, _scene(seq)) for seq in range(1, 7)},
        }
    )
    actual = json.loads(json.dumps(actual, ensure_ascii=False))
    if os.environ.get("NARRATIVE_GOLDEN_REGEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual["chapter"] == expected["chapter"]
    for seq, payload in expected["scenes"].items():
        assert actual["scenes"][seq] == payload, seq
    assert actual == expected


def _selects(session, action) -> int:
    from sqlalchemy import event

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


def test_chapter_status_statement_count_does_not_grow_with_scenes_or_candidates(session) -> None:
    """成稿中心的章列表每一行都带整章的正史状态：以前每场各查几次、每条候选再各查几次（B11-12）。"""
    service = _seed(session)
    before = _selects(session, lambda: service.chapter_status(PROJECT, CHAPTER))

    log = NarrativeEventLog(session)
    for seq in range(8, 14):
        scene = SceneCard(scene_id=_scene(seq), chapter_id=CHAPTER, project_id=PROJECT, scene_seq=seq, scene_goal="加场")
        session.add(scene)
        session.flush()
        seed_final_scene(session, scene=scene, content="阿远把旧信塞进怀里。")
        final_id = f"final_{_scene(seq)}_v1"
        service.mark_archive_pending(final_id)
        event_row = log.log_event(
            project_id=PROJECT, chapter_id=CHAPTER, scene_id=_scene(seq),
            event_type="character_state", entity_type="character", entity_id="阿远",
            fact_key="has_item", fact_value="旧信", confidence="extracted",
            source_text_excerpt="阿远把旧信塞进怀里", payload={"source": "prose"},
            authority_status="pending", source_kind="prose_extraction", final_scene_row_id=final_id,
        )
        service.stage_extraction(final_id, outcome="completed_events", event_ids=[event_row.event_id])
    session.commit()
    after = _selects(session, lambda: service.chapter_status(PROJECT, CHAPTER))

    assert after == before, (before, after)
    assert before <= 12, before
