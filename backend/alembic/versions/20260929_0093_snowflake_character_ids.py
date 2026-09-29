"""Snowflake character ids carry their work's prefix: hand-made ids (c1 / c2 / …) stop colliding across works.

Revision ID: 20260929_0093
Revises: 20260924_0092
Create Date: 2026-09-29

2026-09-30（B06-01，作者批准 #16c）——只改数据，不改结构。React 的角色表按 c1 / c2 / … 给手加的角色编号，
服务端原样把它写进 ``story_characters.character_id``——那是**全局**主键：第二部作品确认角色表时会把第一部作品
的同号角色改名。代码从这一版起在写入时把雪花角色 id 规范成 ``f"{project_id}_{raw}"``
（``services/snowflake_character_ids.py``）；这个迁移把库里已有的数据一次规范好，否则新写入的规范 id 与旧行对不上。

范围：只动雪花作品（``story_projects.planning_mode = 'snowflake'``）里**出现在雪花数据中**的角色 id——角色计划
（``snowflake_character_plans``）与各步草稿里的 ``characters[].character_id``。资料库 / 章节编排自己铸的
``CHAR_<hex>`` 本来就全局唯一，不在构思里就不动。每个作品：

- ``story_characters``：主键改名（目标 id 已存在时不改名，留一条记录）；``summary_json`` / ``synopsis_json`` /
  ``bible_json`` 里抄的 ``character_id`` 同步改；
- ``snowflake_character_plans``：``character_id`` 与三份 json 里的 ``character_id``（主键 ``character_plan_id`` 不变——
  旧的派生规则 ``snowflake_character_plan_{project}_{raw}`` 恰好等于新规则 ``snowflake_character_plan_{规范 id}``）；
- ``snowflake_scene_plans`` / ``scene_cards``：``pov_character_id``、``onstage_chars_json``（场景卡还有
  ``writer_brief_json.protagonist_character_id``）；
- ``snowflake_step_runs``（全部版本，恢复历史要用）：``draft_json`` 的 ``characters[].character_id``、
  ``protagonist_character_id``、``scenes[].pov_character_id`` / ``onstage_chars_json``——``fe_*`` 写穿键是前端自己的
  数据，一个字不改（服务端交给前端时再把前缀剥掉，前端本机缓存里的 c1 仍然对得上）；
- ``outline_plans.plan_json``（待确认的物化计划确认时要写场景卡）与 ``snowflake_assistant_turns.candidate_patch_json``；
- ``library_relations``：``character:<旧 id>`` 的引用改成新 id。

执行契约（``scene_execution_contracts``）不改：它们按场景卡快照哈希复用，视角 id 变了下一次运行自然重建。
每个改过的作品在 ``operation_logs`` 留一条 ``snowflake_character_ids_canonicalized``（旧 id → 新 id 的对照表）。

幂等：已带本作品前缀的 id 原样保留，重跑什么都不改。降级是空操作：新写入的角色从这一版起就是规范 id，把前缀
剥回去会让不同作品的同号角色重新撞上主键（对照表在操作日志里）。历史迁移是冻结的显式 SQL，不导入应用代码。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

import sqlalchemy as sa
from alembic import op


revision = "20260929_0093"
down_revision = "20260924_0092"
branch_labels = None
depends_on = None


def _load(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _canonical(project_id: str, raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    prefix = f"{project_id}_"
    return text if text.startswith(prefix) else prefix + text


def _map_draft(value: Any, mapper: Callable[[Any], str]) -> Any:
    """与 ``snowflake_character_ids.canonicalize_draft`` 同一形状（冻结在这里）：fe_* 键不碰。"""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    characters = value.get("characters")
    if isinstance(characters, list):
        mapped = []
        for item in characters:
            if isinstance(item, dict) and str(item.get("character_id") or "").strip():
                item = {**item, "character_id": mapper(item.get("character_id"))}
            mapped.append(item)
        result["characters"] = mapped
    if str(value.get("protagonist_character_id") or "").strip():
        result["protagonist_character_id"] = mapper(value.get("protagonist_character_id"))
    scenes = value.get("scenes")
    if isinstance(scenes, list):
        result["scenes"] = [_map_scene(item, mapper) for item in scenes]
    return result


def _map_scene(item: Any, mapper: Callable[[Any], str]) -> Any:
    if not isinstance(item, dict):
        return item
    scene = dict(item)
    if str(item.get("pov_character_id") or "").strip():
        scene["pov_character_id"] = mapper(item.get("pov_character_id"))
    onstage = item.get("onstage_chars_json")
    if isinstance(onstage, list):
        scene["onstage_chars_json"] = [mapper(entry) for entry in onstage if str(entry or "").strip()]
    return scene


def _map_member(value: Any, mapper: Callable[[Any], str]) -> Any:
    if isinstance(value, dict) and str(value.get("character_id") or "").strip():
        return {**value, "character_id": mapper(value.get("character_id"))}
    return value


def _snowflake_character_ids(bind: Any, tables: set[str], project_id: str) -> set[str]:
    """这个作品在雪花数据里出现过的角色 id（角色计划 + 各步草稿的角色表）。"""
    ids: set[str] = set()
    if "snowflake_character_plans" in tables:
        for (character_id,) in bind.execute(
            sa.text("SELECT character_id FROM snowflake_character_plans WHERE project_id = :pid"), {"pid": project_id}
        ):
            if str(character_id or "").strip():
                ids.add(str(character_id).strip())
    if "snowflake_step_runs" in tables:
        for (raw,) in bind.execute(
            sa.text(
                "SELECT draft_json FROM snowflake_step_runs WHERE project_id = :pid "
                "AND step_key IN ('character_sheets', 'character_synopses', 'character_bibles')"
            ),
            {"pid": project_id},
        ):
            draft = _load(raw)
            for item in (draft or {}).get("characters") or [] if isinstance(draft, dict) else []:
                if isinstance(item, dict) and str(item.get("character_id") or "").strip():
                    ids.add(str(item["character_id"]).strip())
    return ids


def _canonicalize_project(bind: Any, tables: set[str], project_id: str, now: str) -> None:
    raw_ids = {value for value in _snowflake_character_ids(bind, tables, project_id) if value != _canonical(project_id, value)}
    if not raw_ids:
        return
    renames = {raw: _canonical(project_id, raw) for raw in raw_ids}

    def mapper(value: Any) -> str:
        text = str(value or "").strip()
        return renames.get(text, text)

    kept: list[str] = []
    if "story_characters" in tables:
        rows = bind.execute(
            sa.text(
                "SELECT character_id, summary_json, synopsis_json, bible_json FROM story_characters WHERE project_id = :pid"
            ),
            {"pid": project_id},
        ).fetchall()
        existing = {str(row[0]) for row in bind.execute(sa.text("SELECT character_id FROM story_characters WHERE character_id IN :ids").bindparams(sa.bindparam("ids", expanding=True)), {"ids": sorted(renames.values())})}
        for character_id, summary, synopsis, bible in rows:
            new_id = renames.get(str(character_id))
            if new_id is None:
                continue
            if new_id in existing:
                kept.append(str(character_id))
                continue
            bind.execute(
                sa.text(
                    "UPDATE story_characters SET character_id = :new, summary_json = :summary, synopsis_json = :synopsis, "
                    "bible_json = :bible WHERE character_id = :old"
                ),
                {
                    "new": new_id,
                    "old": character_id,
                    "summary": _dump(_map_member(_load(summary), mapper)) if _load(summary) is not None else summary,
                    "synopsis": _dump(_map_member(_load(synopsis), mapper)) if _load(synopsis) is not None else synopsis,
                    "bible": _dump(_map_member(_load(bible), mapper)) if _load(bible) is not None else bible,
                },
            )

    if "snowflake_character_plans" in tables:
        for plan_id, character_id, summary, synopsis, bible in bind.execute(
            sa.text(
                "SELECT character_plan_id, character_id, summary_json, synopsis_json, bible_json "
                "FROM snowflake_character_plans WHERE project_id = :pid"
            ),
            {"pid": project_id},
        ).fetchall():
            bind.execute(
                sa.text(
                    "UPDATE snowflake_character_plans SET character_id = :cid, summary_json = :summary, "
                    "synopsis_json = :synopsis, bible_json = :bible WHERE character_plan_id = :plan_id"
                ),
                {
                    "plan_id": plan_id,
                    "cid": mapper(character_id),
                    "summary": _dump(_map_member(_load(summary), mapper)) if _load(summary) is not None else summary,
                    "synopsis": _dump(_map_member(_load(synopsis), mapper)) if _load(synopsis) is not None else synopsis,
                    "bible": _dump(_map_member(_load(bible), mapper)) if _load(bible) is not None else bible,
                },
            )

    for table, key in (("snowflake_scene_plans", "scene_plan_id"), ("scene_cards", "scene_id")):
        if table not in tables:
            continue
        with_brief = table == "scene_cards"
        columns = f"{key}, pov_character_id, onstage_chars_json" + (", writer_brief_json" if with_brief else "")
        for row in bind.execute(sa.text(f"SELECT {columns} FROM {table} WHERE project_id = :pid"), {"pid": project_id}).fetchall():
            row_key, pov, onstage = row[0], row[1], row[2]
            values: dict[str, Any] = {"key": row_key}
            sets: list[str] = []
            if str(pov or "").strip() and mapper(pov) != pov:
                values["pov"] = mapper(pov)
                sets.append("pov_character_id = :pov")
            onstage_list = _load(onstage)
            if isinstance(onstage_list, list):
                mapped = [mapper(entry) for entry in onstage_list if str(entry or "").strip()]
                if mapped != onstage_list:
                    values["onstage"] = _dump(mapped)
                    sets.append("onstage_chars_json = :onstage")
            if with_brief:
                brief = _load(row[3])
                if isinstance(brief, dict) and str(brief.get("protagonist_character_id") or "").strip():
                    new_brief = {**brief, "protagonist_character_id": mapper(brief.get("protagonist_character_id"))}
                    if new_brief != brief:
                        values["brief"] = _dump(new_brief)
                        sets.append("writer_brief_json = :brief")
            if sets:
                bind.execute(sa.text(f"UPDATE {table} SET {', '.join(sets)} WHERE {key} = :key"), values)

    for table, key, column in (
        ("snowflake_step_runs", "step_run_id", "draft_json"),
        ("snowflake_assistant_turns", "turn_id", "candidate_patch_json"),
    ):
        if table not in tables:
            continue
        for row_key, raw in bind.execute(
            sa.text(f"SELECT {key}, {column} FROM {table} WHERE project_id = :pid"), {"pid": project_id}
        ).fetchall():
            value = _load(raw)
            mapped = _map_draft(value, mapper)
            if isinstance(value, dict) and mapped != value:
                bind.execute(sa.text(f"UPDATE {table} SET {column} = :value WHERE {key} = :key"), {"value": _dump(mapped), "key": row_key})

    if "outline_plans" in tables:
        for plan_id, raw in bind.execute(
            sa.text("SELECT plan_id, plan_json FROM outline_plans WHERE project_id = :pid"), {"pid": project_id}
        ).fetchall():
            plan = _load(raw)
            if not isinstance(plan, dict):
                continue
            chapters = []
            for chapter in plan.get("chapters") or []:
                if isinstance(chapter, dict) and isinstance(chapter.get("scenes"), list):
                    scenes = []
                    for scene in chapter["scenes"]:
                        scene = _map_scene(scene, mapper)
                        brief = scene.get("writer_brief_json") if isinstance(scene, dict) else None
                        if isinstance(brief, dict) and str(brief.get("protagonist_character_id") or "").strip():
                            scene = {**scene, "writer_brief_json": {**brief, "protagonist_character_id": mapper(brief["protagonist_character_id"])}}
                        scenes.append(scene)
                    chapter = {**chapter, "scenes": scenes}
                chapters.append(chapter)
            if isinstance(plan.get("chapters"), list) and chapters != plan["chapters"]:
                bind.execute(
                    sa.text("UPDATE outline_plans SET plan_json = :value WHERE plan_id = :plan_id"),
                    {"value": _dump({**plan, "chapters": chapters}), "plan_id": plan_id},
                )

    if "library_relations" in tables:
        for relation_id, from_ref, to_ref in bind.execute(
            sa.text("SELECT relation_id, from_ref, to_ref FROM library_relations WHERE project_id = :pid"), {"pid": project_id}
        ).fetchall():
            new_from = _map_ref(from_ref, mapper)
            new_to = _map_ref(to_ref, mapper)
            if new_from != from_ref or new_to != to_ref:
                bind.execute(
                    sa.text("UPDATE library_relations SET from_ref = :f, to_ref = :t WHERE relation_id = :rid"),
                    {"f": new_from, "t": new_to, "rid": relation_id},
                )

    if "operation_logs" in tables:
        bind.execute(
            sa.text(
                "INSERT INTO operation_logs (event_type, object_type, object_ref, payload_json, created_at) "
                "VALUES ('snowflake_character_ids_canonicalized', 'story_project', :pid, :payload, :now)"
            ),
            {
                "pid": project_id,
                "payload": _dump({"project_id": project_id, "renamed": dict(sorted(renames.items())), "kept_old_rows": sorted(kept)}),
                "now": now,
            },
        )


def _map_ref(ref: Any, mapper: Callable[[Any], str]) -> Any:
    text = str(ref or "")
    if text.startswith("character:"):
        return "character:" + mapper(text.split(":", 1)[1])
    return ref


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "story_projects" not in tables:
        return
    now = datetime.now(timezone.utc).isoformat()
    projects = [
        str(row[0])
        for row in bind.execute(sa.text("SELECT project_id FROM story_projects WHERE planning_mode = 'snowflake'"))
    ]
    for project_id in projects:
        _canonicalize_project(bind, tables, project_id, now)


def downgrade() -> None:
    """空操作：规范 id 从这一版起就是写入口径，剥回前缀会让不同作品的同号角色重新撞上主键（对照表在操作日志里）。"""
