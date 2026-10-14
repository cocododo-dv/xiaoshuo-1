"""资料页从故事长出来的那一半（``services/library_story.py``）。

2026-10-14 之前资料页只显示作者在资料里亲手写的字段：构思同步进来的人物只剩名字（04 存的是
``one_sentence_summary``，资料只认 ``one_line``），世界与大事记永远是空的。现在 overview 带上：

* 人物：04 / 06 / 08 最近一次确认的版本（构思里有更新的版本没确认时说出来）；工作记忆里的现状与得知的事、
  章摘要里的人物变化、出场场次；
* ``story.timeline``：目录各场按目录顺序，写成了的带上终稿落下的事实；``story.chapters``：章摘要；
* ``story.entities``：场景地点与物品，作者登记的同类条目（名字出现在里面的、最长的那个）把它们收走。
"""
from __future__ import annotations

import hashlib

from novel_system.db.models import (
    ChapterDigest,
    ChapterGoal,
    FinalScene,
    LibraryEntity,
    NarrativeEvent,
    SceneCard,
    SceneRunState,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
)
from novel_system.services.library import LibraryService
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

PROJECT = "PRJ_LIBSTORY"
LZ = f"{PROJECT}_c1"
SW = f"{PROJECT}_c2"


def _chapter(index: int) -> str:
    return f"{PROJECT}_CH{index:02d}"


def _scene(chapter: int, seq: int) -> str:
    return f"{PROJECT}_CH{chapter:02d}_SC{seq:02d}"


def _run(step_key: str, version: int, status: str, characters: list[dict], *, confirmed: bool) -> SnowflakeStepRun:
    return SnowflakeStepRun(
        step_run_id=f"run_{step_key}_v{version}",
        project_id=PROJECT,
        step_key=step_key,
        version=version,
        status=status,
        draft_json={"characters": characters},
        approved_at="2026-10-01T00:00:00+00:00" if confirmed else None,
    )


def _event(event_id: str, scene_id: str, final_row_id: str, *, entity_id: str, event_type: str, key: str, value: str) -> NarrativeEvent:
    """后台抽取落下、还没核对的一条（工作记忆认它，正史不认）。"""
    return NarrativeEvent(
        event_id=event_id,
        project_id=PROJECT,
        scene_id=scene_id,
        chapter_id=_chapter(1),
        scene_seq=1,
        event_type=event_type,
        entity_type="character",
        entity_id=entity_id,
        fact_key=key,
        fact_value=value,
        confidence="extracted",
        authority_status="pending",
        source_kind="prose_extraction",
        final_scene_row_id=final_row_id,
    )


def _seed(session) -> dict[str, str]:
    session.add(StoryProject(project_id=PROJECT, title="雨城旧信", outline_text=""))
    session.flush()
    for index in (1, 2):
        session.add(ChapterGoal(chapter_id=_chapter(index), project_id=PROJECT, chapter_goal=f"第 {index} 章", display_order=index))
    session.flush()
    scenes = [
        # (章, 场, 地点, 视角, 在场, 故事内时间, 场目标)
        (1, 1, "雨城旧钟楼顶", LZ, [SW], "立冬·夜", "林昭在钟楼顶找到那封旧信"),
        (1, 2, "雨城码头", SW, [], "次日清晨", "苏晚在码头等船"),
        (2, 1, "北境驿站", LZ, [SW], "三日后", "两人在驿站对质"),
        (2, 2, None, None, [], "", None),  # 作者建了还没规划的空场：不进大事记
    ]
    for chapter, seq, location, pov, cast, story_time, goal in scenes:
        session.add(
            SceneCard(
                scene_id=_scene(chapter, seq),
                chapter_id=_chapter(chapter),
                project_id=PROJECT,
                scene_seq=seq,
                location=location,
                pov_character_id=pov,
                onstage_chars_json=cast,
                scene_goal=goal or "",
                writer_brief_json={"story_time": story_time} if story_time else {},
            )
        )
    session.add_all(
        [
            StoryCharacter(character_id=LZ, project_id=PROJECT, display_name="林昭", role="主角",
                           summary_json={"one_sentence_summary": "替亡父送完最后一封信的档案员"}, status="approved"),
            StoryCharacter(character_id=SW, project_id=PROJECT, display_name="苏晚", role="对手", summary_json={}, status="approved"),
            LibraryEntity(entity_id="ENT_YC", project_id=PROJECT, kind="location", name="雨城"),
            LibraryEntity(entity_id="ENT_ZL", project_id=PROJECT, kind="location", name="旧钟楼"),
            LibraryEntity(entity_id="ENT_KEY", project_id=PROJECT, kind="item", name="铜钥匙"),
        ]
    )
    sheet = {
        "character_id": LZ, "display_name": "林昭", "role": "主角", "goal": "查清旧信是谁写的",
        "ambition": "", "values": ["守信", "克制"], "conflict": "信里的人要她闭嘴", "epiphany": "",
        "one_sentence_summary": "替亡父送完最后一封信的档案员", "one_paragraph_summary": "她回到雨城。",
    }
    bible = {
        "character_id": LZ, "display_name": "林昭", "role": "主角",
        "physical_profile": {"age": "29", "height": "", "appearance": "左手腕一道旧疤", "style": ""},
        "psychological_profile": {"deepest_fear": "被人遗忘", "favourite_rain": "冬雨"},
    }
    session.add_all(
        [
            _run("character_sheets", 1, "approved", [sheet], confirmed=True),
            _run("character_sheets", 2, "pending_review", [{**sheet, "goal": "改过的目标"}], confirmed=False),
            _run("character_synopses", 1, "approved",
                 [{"character_id": LZ, "synopsis": "开头一句没有前缀\n信念：信要送到\n旧伤：父亲失踪\n那年下雨"}], confirmed=True),
            _run("character_bibles", 1, "stale", [bible], confirmed=True),
        ]
    )
    session.flush()

    scene_id = _scene(1, 1)
    content = "林昭在钟楼顶找到了那封旧信，铜钥匙还挂在她脖子上。"
    final = FinalScene(
        row_id=f"final_{scene_id}_v1", scene_id=scene_id, chapter_id=_chapter(1), content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(), status="archived",
        source_bundle_id=f"bundle_{scene_id}", source_bundle_hash="bundle-hash",
    )
    session.add(final)
    session.flush()
    session.add(SceneRunState(scene_id=scene_id, scene_status="archived", current_final_scene_row_id=final.row_id))
    session.add_all(
        [
            _event("ev1", scene_id, final.row_id, entity_id=LZ, event_type="character_state", key="has_item:铜钥匙", value="held"),
            _event("ev2", scene_id, final.row_id, entity_id=LZ, event_type="character_state", key="has_item:旧信", value="held"),
            _event("ev3", scene_id, final.row_id, entity_id=LZ, event_type="character_learns",
                   key="learned_letter_writer", value="旧信是父亲失踪前一晚写的"),
        ]
    )
    session.add(
        ChapterDigest(
            row_id="digest_ch1", project_id=PROJECT, chapter_id=_chapter(1),
            digest_json={
                "summary": "林昭找到旧信，苏晚在码头等船。",
                "ending_state": "雨还在下。",
                "character_changes": [
                    {"character": "林昭（档案员）", "change": "决定把信读完"},
                    {"character": "苏晚", "change": "离开雨城"},
                ],
                "threads": [],
            },
            content_hash="hash_ch1", source_hash="seeded",
        )
    )
    session.commit()
    return {"final_row_id": final.row_id}


def _overview(session) -> dict:
    return LibraryService(session).overview(PROJECT)


def _character(data: dict, character_id: str) -> dict:
    return next(item for item in data["characters"] if item["character_id"] == character_id)


def test_character_shows_the_confirmed_snowflake_profile_and_what_is_still_unconfirmed(session) -> None:
    _seed(session)
    data = _overview(session)
    lz = _character(data, LZ)

    assert lz["summary"] == "替亡父送完最后一封信的档案员", "04 的一句话故事线就是档案头上那一句"
    sections = {section["title"]: section for section in lz["profile"]["sections"]}
    assert list(sections) == ["角色摘要表", "角色背景故事", "外形", "心理"]
    sheet_rows = {row["label"]: row["value"] for row in sections["角色摘要表"]["rows"]}
    assert sheet_rows == {"目标": "查清旧信是谁写的", "价值观": "守信、克制", "冲突": "信里的人要她闭嘴", "一段话故事线": "她回到雨城。"}, (
        "确认过的那一版（不是待复核的 v2），空格不列"
    )
    assert sections["角色背景故事"]["rows"] == [
        {"label": "背景故事", "value": "开头一句没有前缀"},
        {"label": "信念", "value": "信要送到"},
        {"label": "旧伤", "value": "父亲失踪\n那年下雨"},
    ]
    assert sections["外形"]["rows"] == [{"label": "年龄", "value": "29"}, {"label": "外貌", "value": "左手腕一道旧疤"}]
    assert sections["心理"]["rows"] == [
        {"label": "最深的恐惧", "value": "被人遗忘"},
        {"label": "favourite_rain", "value": "冬雨"},
    ], "词表外的键也照样显示，不吞掉作者 / 模型写下的内容"
    assert {section["step_key"] for section in lz["profile"]["sections"]} == {
        "character_sheets", "character_synopses", "character_bibles",
    }
    assert _character(data, SW)["profile"]["sections"] == []

    pending = {item["step_key"]: item for item in data["story"]["pending_profile_steps"]}
    assert set(pending) == {"character_sheets", "character_bibles"}
    assert pending["character_sheets"]["status"] == "pending_review"
    assert pending["character_sheets"]["approved_version"] == 1
    assert pending["character_bibles"]["status"] == "stale", "确认过、但上游改了等复核：内容照显示，同时提醒"


def test_character_story_comes_from_working_memory_digests_and_the_catalog(session) -> None:
    _seed(session)
    data = _overview(session)
    lz = _character(data, LZ)["story"]
    assert [(fact["key"], fact["value"]) for fact in lz["state"]] == [("has_item:铜钥匙", "held"), ("has_item:旧信", "held")]
    assert [(fact["key"], fact["value"]) for fact in lz["learned"]] == [("learned_letter_writer", "旧信是父亲失踪前一晚写的")]
    assert lz["changes"] == [{"chapter_id": _chapter(1), "text": "决定把信读完"}], "章摘要里带括号注解的名字也认得"
    assert lz["pov_scene_ids"] == [_scene(1, 1), _scene(2, 1)]
    assert lz["onstage_scene_ids"] == []

    sw = _character(data, SW)["story"]
    assert sw["state"] == [] and sw["learned"] == []
    assert sw["changes"] == [{"chapter_id": _chapter(1), "text": "离开雨城"}]
    assert sw["pov_scene_ids"] == [_scene(1, 2)]
    assert sw["onstage_scene_ids"] == [_scene(1, 1), _scene(2, 1)]


def test_timeline_lists_the_planned_scenes_in_catalog_order_with_what_the_written_ones_established(session) -> None:
    _seed(session)
    story = _overview(session)["story"]
    timeline = story["timeline"]
    assert [event["scene_id"] for event in timeline] == [_scene(1, 1), _scene(1, 2), _scene(2, 1)], "没规划的空场不进大事记"

    first, second, third = timeline
    assert first["origin"] == "story" and first["ref"] == f"story:scene-{_scene(1, 1)}"
    assert first["time_label"] == "立冬·夜"
    assert first["note"] == "林昭在钟楼顶找到那封旧信"
    assert first["chapter_ref"] == _chapter(1)
    assert first["realization_status"] == "realized"
    assert first["roles"] == {f"character:{LZ}": "视角", f"character:{SW}": "在场", "entity:ENT_ZL": "地点"}
    assert first["entity_refs"] == list(first["roles"])
    assert [(fact["entity_id"], fact["key"]) for fact in first["facts"]] == [
        (LZ, "has_item:铜钥匙"), (LZ, "has_item:旧信"), (LZ, "learned_letter_writer"),
    ]
    assert second["realization_status"] == "planned" and second["facts"] == []
    assert second["roles"] == {f"character:{SW}": "视角", "entity:ENT_YC": "地点"}
    place_ref = next(ref for ref, role in third["roles"].items() if role == "地点")
    assert place_ref.startswith("story:place-")

    assert story["chapters"] == [{"chapter_id": _chapter(1), "summary": "林昭找到旧信，苏晚在码头等船。", "ending_state": "雨还在下。"}]


def test_world_grows_places_and_items_and_author_entries_absorb_the_ones_named_inside(session) -> None:
    _seed(session)
    data = _overview(session)
    derived = {entry["name"]: entry for entry in data["story"]["entities"]}
    assert set(derived) == {"北境驿站", "旧信"}, "钟楼顶 / 码头被作者的「旧钟楼」「雨城」收走，铜钥匙被作者的物品收走"
    station = derived["北境驿站"]
    assert station["kind"] == "location" and station["origin"] == "story"
    assert station["ref"] == f"story:{station['entity_id']}"
    assert station["story"] == {"spots": ["北境驿站"], "scene_ids": [_scene(2, 1)], "character_ids": [LZ, SW]}
    assert derived["旧信"]["kind"] == "item"
    assert derived["旧信"]["story"]["holders"] == [{"character_id": LZ, "value": "held", "scene_id": _scene(1, 1)}]
    third_place = next(ref for ref, role in data["story"]["timeline"][2]["roles"].items() if role == "地点")
    assert third_place == station["ref"], "大事记指向的就是世界里那一条"

    authored = {entry["entity_id"]: entry for entry in data["entities"]}
    assert authored["ENT_ZL"]["story"]["spots"] == ["雨城旧钟楼顶"], "「雨城旧钟楼顶」里「旧钟楼」比「雨城」长：归旧钟楼"
    assert authored["ENT_YC"]["story"] == {"spots": ["雨城码头"], "scene_ids": [_scene(1, 2)], "character_ids": [SW]}
    assert authored["ENT_KEY"]["story"]["holders"] == [{"character_id": LZ, "value": "held", "scene_id": _scene(1, 1)}]


def test_overview_route_carries_the_story_half_and_story_refs_are_not_relation_endpoints(client, session) -> None:
    _seed(session)
    response = client.get(f"/api/v2/projects/{PROJECT}/library")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {"entities", "timeline", "chapters", "pending_profile_steps"} <= set(data["story"])
    assert data["relations"] == [] and data["timeline"] == [], "作者自己的大事记 / 关系照旧只有作者写的"
    story_ref = data["story"]["entities"][0]["ref"]

    rejected = client.post(
        f"/api/v2/projects/{PROJECT}/library/relations",
        json={"from_ref": f"character:{LZ}", "to_ref": story_ref, "kind": "related"},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "LIBRARY_RELATION_REF_INVALID"


def test_reconfirming_character_sheets_keeps_what_the_author_wrote_in_the_library(session) -> None:
    _seed(session)
    row = session.get(StoryCharacter, LZ)
    row.summary_json = {**row.summary_json, "one_line": "资料里写的一句话", "fe_details": {"blurb": "作者的简述", "pinned": True}}
    session.commit()

    SnowflakeWorkspaceService(session)._sync_story_character(
        PROJECT, LZ, "林昭", {"character_id": LZ, "goal": "新的目标", "one_sentence_summary": "新的一句话"}, "character_sheets"
    )
    session.commit()
    session.expire_all()
    summary = session.get(StoryCharacter, LZ).summary_json
    assert summary["goal"] == "新的目标" and summary["one_sentence_summary"] == "新的一句话"
    assert summary["one_line"] == "资料里写的一句话"
    assert summary["fe_details"] == {"blurb": "作者的简述", "pinned": True}, "以前再确认一次 04 就把资料里写的简述、置顶冲掉"
