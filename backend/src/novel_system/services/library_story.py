"""资料里从故事长出来的那一半：只读，读的时候现推，从不写库。

资料库自己的行（人物 ``StoryCharacter``、世界 ``LibraryEntity``、大事记 ``TimelineEvent``、关系 ``LibraryRelation``）
归作者；这里把别处已经有的东西摊到资料页上，作者不用再抄一遍（2026-10-14：资料页以前只显示作者在资料里亲手写的
字段，构思同步进来的人物只剩一个名字，世界和大事记永远是空的）：

* **人物的构思档案**：雪花 04 角色摘要表 / 06 角色背景故事 / 08 角色全档案**最近一次确认的版本**；构思里有更新的
  版本还没确认时，``pending`` 说出是哪几步。
* **人物写到现在**：工作记忆（正史 + 还没核对的抽取，与起草提示词读的同一份，见 ``narrative.replay``）折叠出的现状
  与得知的事、各章摘要里写下的人物变化、在目录里出场的场次。
* **大事记**：目录里规划过的每一场按目录顺序排开——故事内时间、地点、视角与在场人物；写成了的场带上终稿落下的
  事实。各章的摘要另列在 ``chapters``，附在章上。
* **世界**：场景地点（目录场景卡的「地点」）、物品（工作记忆里的 ``has_item:<物品>``），以及抽取直接记在地点 /
  物品名下的事实。作者登记过的同类条目把名字里含着它的那些收走（取最长的那个名字）：一份「雨城」就收下
  「雨城码头」「雨城旧钟楼顶」……；收走的不再单列，它们的场次 / 持有人挂到作者那一份的 ``story`` 上。

派生条目的 ref 是 ``story:<id>``：不能编辑、不能当关系端点（``LibraryService._validate_ref`` 只认人物与实体）；
作者要改它，就新建一份同名的世界条目——它随即收走这一条。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import LibraryEntity, NarrativeEvent, SnowflakeStepRun, StoryCharacter
from novel_system.services.catalog_reader import CatalogReader
from novel_system.services.character_names import name_variants, normalized_name
from novel_system.services.hash_engine import sha256_text
from novel_system.services.narrative.fact_vocabulary import split_fact_key
from novel_system.services.narrative.replay import NarrativeEventStore, ProjectedFact, ProjectionSnapshot, fold_fact
from novel_system.services.snowflake_step_catalog import CHARACTER_STEPS, step_label
from novel_system.services.story_memory_digest import active_digests, normalize_digest

STORY_REF_PREFIX = "story:"

# 04 角色摘要表：一句话故事线不在这里——它是人物档案头上的那一句（``_character_payload`` 的 summary 兜底）
_SHEET_ROWS: tuple[tuple[str, str], ...] = (
    ("goal", "目标"),
    ("ambition", "抱负"),
    ("values", "价值观"),
    ("conflict", "冲突"),
    ("epiphany", "顿悟"),
    ("one_paragraph_summary", "一段话故事线"),
)
# 06 角色背景故事：构思页把六栏打包成「信念：…」这样的前缀行（``ws-snow-canon.js``），这里拆回去
_SYNOPSIS_PREFIXES = ("信念", "旧伤", "欲望", "恐惧", "关系", "视角故事")
_SYNOPSIS_LINE_RE = re.compile(r"^(" + "|".join(_SYNOPSIS_PREFIXES) + r")[：:]\s*(.*)$")
# 08 角色全档案的四个侧面
_BIBLE_GROUPS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("physical_profile", "外形", (("age", "年龄"), ("height", "身高"), ("appearance", "外貌"), ("style", "衣着"))),
    (
        "personality_profile",
        "性格",
        (("strongest_trait", "最突出的特质"), ("weakest_trait", "最大的弱点"), ("humor", "幽默感"), ("preferences", "喜好")),
    ),
    (
        "environment_profile",
        "处境",
        (
            ("home", "住处"),
            ("family_background", "家庭背景"),
            ("education", "教育"),
            ("work", "工作"),
            ("relationships", "人际关系"),
        ),
    ),
    (
        "psychological_profile",
        "心理",
        (
            ("best_memory", "最好的记忆"),
            ("worst_memory", "最坏的记忆"),
            ("deepest_fear", "最深的恐惧"),
            ("greatest_hope", "最大的希望"),
            ("philosophy", "人生观"),
            ("self_image", "自我认知"),
            ("public_image", "旁人眼中"),
            ("character_arc", "人物弧"),
        ),
    ),
)
_LEARNS_EVENT_TYPE = "character_learns"
_CONFIRMED_STATUSES = frozenset({"approved", "stale"})


def _text(value: Any) -> str:
    """档案里的一格写成一行字：列表用顿号连，字典写成「键：值」。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "、".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, Mapping):
        return "；".join(f"{key}：{text}" for key, text in ((k, _text(v)) for k, v in value.items()) if text)
    if value is None:
        return ""
    return str(value).strip()


def _rows(item: Mapping[str, Any], fields: Iterable[tuple[str, str]], *, extra: bool = False) -> list[dict[str, str]]:
    """按 ``fields`` 的次序取有字的格；``extra`` = 词表外的键也带上（键名当标签，模型偶尔多给一格）。"""
    known = dict(fields)
    rows = [{"label": label, "value": _text(item.get(key))} for key, label in known.items()]
    if extra:
        rows.extend(
            {"label": str(key), "value": _text(value)}
            for key, value in item.items()
            if key not in known and key not in {"character_id", "display_name", "role"}
        )
    return [row for row in rows if row["value"]]


def _synopsis_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    loose: list[str] = []
    for line in str(text or "").splitlines():
        match = _SYNOPSIS_LINE_RE.match(line.strip())
        if match:
            rows.append({"label": match.group(1), "value": match.group(2).strip()})
        elif rows:
            rows[-1]["value"] = f"{rows[-1]['value']}\n{line}".strip()
        elif line.strip():
            loose.append(line.strip())
    if loose:
        rows.insert(0, {"label": "背景故事", "value": "\n".join(loose)})
    return [row for row in rows if row["value"]]


def _profile_sections(step_key: str, item: Mapping[str, Any]) -> list[dict[str, Any]]:
    label = step_label(step_key)
    if step_key == "character_sheets":
        sections = [(label, _rows(item, _SHEET_ROWS))]
    elif step_key == "character_synopses":
        sections = [(label, _synopsis_rows(item.get("synopsis")))]
    else:
        sections = [
            (title, _rows(item.get(key) if isinstance(item.get(key), Mapping) else {}, fields, extra=True))
            for key, title, fields in _BIBLE_GROUPS
        ]
    return [
        {"step_key": step_key, "step_label": label, "title": title, "rows": rows}
        for title, rows in sections
        if rows
    ]


def _story_id(kind: str, key: str) -> str:
    return f"{kind}-{sha256_text(key)[:12]}"


def _fact(fact: ProjectedFact) -> dict[str, str]:
    return {"key": fact.fact_key, "value": fact.fact_value, "scene_id": fact.scene_id}


class LibraryStory:
    """一部作品资料页的派生部分（构造时读齐，之后只在内存里拼）。"""

    def __init__(
        self,
        session: Session,
        project_id: str,
        *,
        characters: list[StoryCharacter],
        entities: list[LibraryEntity],
    ) -> None:
        self.session = session
        self.project_id = project_id
        self.characters = {row.character_id: row for row in characters}
        self.entities = entities
        self.scenes = self._catalog_scenes()
        self.snapshot = ProjectionSnapshot(NarrativeEventStore(session), project_id, working=True)
        self.profiles, self.pending = self._profiles()
        self.digests = self._digests()
        self.places, self.place_owner = self._places()
        self.items, self.item_owner = self._items()
        self.world_facts = self._world_facts()

    # ---------------------------------------------------------------- 读：目录、构思、工作记忆、章摘要

    def _catalog_scenes(self) -> list[dict[str, Any]]:
        """目录顺序的各场（与写作台 / 章节编排读的同一份目录载荷）。没规划、没写的空场（「新场景」）不算。"""
        scenes: list[dict[str, Any]] = []
        for chapter in CatalogReader(self.session).catalog(self.project_id)["chapters"]:
            for scene in chapter["scenes"]:
                design = scene.get("design") or {}
                written = bool((scene.get("work") or {}).get("has_final"))
                if not (scene.get("summary") or design.get("location") or written):
                    continue
                cast = [
                    str(member.get("character_id") or "")
                    for member in design.get("cast") or []
                    if str(member.get("character_id") or "")
                ]
                pov = str(scene.get("pov_character_id") or "")
                scenes.append(
                    {
                        "scene_id": scene["scene_id"],
                        "chapter_id": scene["chapter_id"],
                        "title": str(scene.get("title") or ""),
                        "summary": str(scene.get("summary") or ""),
                        "location": str(design.get("location") or "").strip(),
                        "story_time": str(design.get("story_time") or "").strip(),
                        "pov": pov,
                        "cast": [cid for cid in cast if cid != pov],
                        "written": written,
                    }
                )
        return scenes

    def _profiles(self) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        """各角色在三个角色步最近一次**确认**的版本里的那一份；以及构思里还有更新版本没确认的步。"""
        runs = list(
            self.session.execute(
                select(SnowflakeStepRun)
                .where(
                    SnowflakeStepRun.project_id == self.project_id,
                    SnowflakeStepRun.step_key.in_(CHARACTER_STEPS),
                )
                .order_by(SnowflakeStepRun.version.desc())
            ).scalars()
        )
        profiles: dict[str, list[dict[str, Any]]] = {}
        pending: list[dict[str, Any]] = []
        for step_key in CHARACTER_STEPS:
            step_runs = [run for run in runs if run.step_key == step_key]
            if not step_runs:
                continue
            # 确认过的那一版：approved，或确认之后上游改了、等复核的 stale（内容仍是作者确认的那一份）
            approved = next(
                (run for run in step_runs if run.status in _CONFIRMED_STATUSES and run.approved_at),
                None,
            )
            latest = step_runs[0]
            if approved is None or latest.version > approved.version or approved.status != "approved":
                pending.append(
                    {
                        "step_key": step_key,
                        "step_label": step_label(step_key),
                        "status": latest.status,
                        "approved_version": approved.version if approved is not None else None,
                    }
                )
            if approved is None:
                continue
            for item in (approved.draft_json or {}).get("characters") or []:
                if not isinstance(item, Mapping):
                    continue
                character_id = str(item.get("character_id") or "").strip()
                if character_id:
                    profiles.setdefault(character_id, []).extend(_profile_sections(step_key, item))
        return profiles, pending

    def _digests(self) -> list[tuple[str, dict[str, Any]]]:
        chapter_ids = list(dict.fromkeys(scene["chapter_id"] for scene in self.scenes))
        rows = active_digests(self.session, chapter_ids)
        return [(chapter_id, normalize_digest(rows[chapter_id].digest_json)) for chapter_id in chapter_ids if chapter_id in rows]

    # ---------------------------------------------------------------- 世界：地点与物品

    def _owner(self, key: str, kind: str) -> LibraryEntity | None:
        """作者登记的同类条目里，名字（或别名）出现在 ``key`` 里的那一份；几份都出现取名字最长的。"""
        best: tuple[int, LibraryEntity] | None = None
        for entity in self.entities:
            if entity.kind != kind:
                continue
            names = [*name_variants(entity.name), *(variant for alias in entity.aliases_json or [] for variant in name_variants(alias))]
            for name in names:
                needle = normalized_name(name)
                if needle and needle in key and (best is None or len(needle) > best[0]):
                    best = (len(needle), entity)
        return best[1] if best else None

    def _places(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        places: dict[str, dict[str, Any]] = {}
        owner_of: dict[str, str] = {}
        for scene in self.scenes:
            location = scene["location"]
            key = normalized_name(location)
            if not key:
                continue
            owner = self._owner(key, "location")
            place_id = owner.entity_id if owner is not None else _story_id("place", key)
            if owner is not None:
                owner_of[place_id] = owner.entity_id
            place = places.setdefault(place_id, {"name": location, "spots": [], "scenes": [], "characters": []})
            if location not in place["spots"]:
                place["spots"].append(location)
            place["scenes"].append(scene["scene_id"])
            for character_id in (scene["pov"], *scene["cast"]):
                if character_id and character_id not in place["characters"]:
                    place["characters"].append(character_id)
        return places, owner_of

    def _items(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """工作记忆里各人名下的 ``has_item:<物品>``：每个持有人取最新的一条（拿着 / 丢了）。"""
        items: dict[str, dict[str, Any]] = {}
        owner_of: dict[str, str] = {}
        latest: dict[tuple[str, str], NarrativeEvent] = {}
        for event in self.snapshot.events:
            kind, slot = split_fact_key(event.fact_key)
            if kind != "has_item" or not slot:
                continue
            latest[(event.entity_id, event.fact_key)] = event
        for (holder, _fact_key), event in latest.items():
            slot = split_fact_key(event.fact_key)[1]
            key = normalized_name(slot)
            owner = self._owner(key, "item")
            item_id = owner.entity_id if owner is not None else _story_id("item", key)
            if owner is not None:
                owner_of[item_id] = owner.entity_id
            item = items.setdefault(item_id, {"name": slot, "holders": []})
            item["holders"].append({"character_id": holder, "value": event.fact_value, "scene_id": event.scene_id})
        return items, owner_of

    def _world_facts(self) -> dict[str, dict[str, Any]]:
        """抽取直接记在地点 / 物品名下的事实（实体 id 是作者条目的就挂到那一份上，否则按名字单列）。"""
        known = {entity.entity_id for entity in self.entities}
        result: dict[str, dict[str, Any]] = {}
        for entity_type in ("location", "item"):
            for entity_id in self.snapshot.entities_of_type(entity_type):
                state = self.snapshot.entity_state(entity_type, entity_id)
                if not state.facts:
                    continue
                if entity_id in known:
                    target = entity_id
                else:
                    key = normalized_name(entity_id)
                    owner = self._owner(key, entity_type)
                    target = owner.entity_id if owner is not None else _story_id("place" if entity_type == "location" else "item", key)
                bucket = result.setdefault(target, {"kind": entity_type, "name": entity_id, "facts": []})
                bucket["facts"].extend(_fact(fact) for fact in state.facts.values())
        return result

    # ---------------------------------------------------------------- 给 overview 的几块

    def character_extras(self, character: StoryCharacter) -> dict[str, Any]:
        character_id = character.character_id
        state: dict[str, ProjectedFact] = {}
        learned: dict[str, ProjectedFact] = {}
        for event in self.snapshot.entity_events(character_id):
            fold_fact(learned if event.event_type == _LEARNS_EVENT_TYPE else state, event)
        names = {normalized_name(variant) for variant in name_variants(character.display_name)} | {
            normalized_name(character_id)
        }
        changes = [
            {"chapter_id": chapter_id, "text": change["change"]}
            for chapter_id, digest in self.digests
            for change in digest["character_changes"]
            if normalized_name(change["character"]) in names
            or any(normalized_name(variant) in names for variant in name_variants(change["character"]))
        ]
        pov = [scene["scene_id"] for scene in self.scenes if scene["pov"] == character_id]
        onstage = [scene["scene_id"] for scene in self.scenes if character_id in scene["cast"]]
        return {
            "profile": {"sections": self.profiles.get(character_id, [])},
            "story": {
                "state": [_fact(fact) for fact in state.values()],
                "learned": [_fact(fact) for fact in learned.values()],
                "changes": changes,
                "pov_scene_ids": pov,
                "onstage_scene_ids": onstage,
            },
        }

    def entity_extras(self, entity: LibraryEntity) -> dict[str, Any]:
        """作者条目收下的地点场次 / 物品持有人 / 记在它名下的事实；什么都没收下就不带 ``story``。"""
        story = self._world_story(entity.entity_id)
        return {"story": story} if story else {}

    def _world_story(self, entity_id: str) -> dict[str, Any]:
        story: dict[str, Any] = {}
        place = self.places.get(entity_id)
        if place:
            story.update(spots=place["spots"], scene_ids=place["scenes"], character_ids=place["characters"])
        item = self.items.get(entity_id)
        if item:
            story["holders"] = item["holders"]
        facts = self.world_facts.get(entity_id)
        if facts:
            story["facts"] = facts["facts"]
        return story

    def _derived_entities(self) -> list[dict[str, Any]]:
        owned = {*self.place_owner, *self.item_owner, *(entity.entity_id for entity in self.entities)}
        entries: dict[str, dict[str, Any]] = {}
        for entity_id, place in self.places.items():
            if entity_id not in owned:
                entries[entity_id] = {"kind": "location", "name": place["name"]}
        for entity_id, item in self.items.items():
            if entity_id not in owned:
                entries.setdefault(entity_id, {"kind": "item", "name": item["name"]})
        for entity_id, bucket in self.world_facts.items():
            if entity_id not in owned:
                entries.setdefault(entity_id, {"kind": bucket["kind"], "name": bucket["name"]})
        return [
            {
                "entity_id": entity_id,
                "project_id": self.project_id,
                "kind": entry["kind"],
                "name": entry["name"],
                "origin": "story",
                "ref": f"{STORY_REF_PREFIX}{entity_id}",
                "story": self._world_story(entity_id),
            }
            for entity_id, entry in entries.items()
        ]

    def _place_ref(self, location: str) -> str | None:
        key = normalized_name(location)
        if not key:
            return None
        owner = self._owner(key, "location")
        return f"entity:{owner.entity_id}" if owner is not None else f"{STORY_REF_PREFIX}{_story_id('place', key)}"

    def _timeline(self) -> list[dict[str, Any]]:
        facts_by_scene: dict[str, list[dict[str, str]]] = {}
        for event in self.snapshot.events:
            facts_by_scene.setdefault(event.scene_id, []).append(
                {
                    "entity_id": event.entity_id,
                    "event_type": event.event_type,
                    "key": event.fact_key,
                    "value": event.fact_value,
                }
            )
        timeline: list[dict[str, Any]] = []
        for scene in self.scenes:
            roles: dict[str, str] = {}
            if scene["pov"] in self.characters:
                roles[f"character:{scene['pov']}"] = "视角"
            for character_id in scene["cast"]:
                if character_id in self.characters:
                    roles.setdefault(f"character:{character_id}", "在场")
            place_ref = self._place_ref(scene["location"])
            if place_ref:
                roles[place_ref] = "地点"
            scene_id = scene["scene_id"]
            timeline.append(
                {
                    "event_id": f"scene-{scene_id}",
                    "project_id": self.project_id,
                    "origin": "story",
                    "ref": f"{STORY_REF_PREFIX}scene-{scene_id}",
                    "scene_id": scene_id,
                    "label": scene["title"] or scene["summary"][:24],
                    "note": scene["summary"],
                    "time_label": scene["story_time"],
                    "location": scene["location"],
                    "chapter_ref": scene["chapter_id"],
                    "entity_refs": list(roles),
                    "roles": roles,
                    "realization_status": "realized" if scene["written"] else "planned",
                    "facts": facts_by_scene.get(scene_id, []) if scene["written"] else [],
                }
            )
        return timeline

    def payload(self) -> dict[str, Any]:
        return {
            "entities": self._derived_entities(),
            "timeline": self._timeline(),
            "chapters": [
                {"chapter_id": chapter_id, "summary": digest["summary"], "ending_state": digest["ending_state"]}
                for chapter_id, digest in self.digests
                if digest["summary"]
            ],
            "pending_profile_steps": self.pending,
        }
