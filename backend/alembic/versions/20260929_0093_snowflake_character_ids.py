"""Snowflake character ids carry their work's prefix: hand-made ids (c1 / c2 / …) stop colliding across works.

Revision ID: 20260929_0093
Revises: 20260924_0092
Create Date: 2026-09-29

2026-09-30（B06-01，作者批准 #16c）——只改数据，不改结构。React 的角色表按 c1 / c2 / … 给手加的角色编号，
服务端原样把它写进 ``story_characters.character_id``——那是**全局**主键：第二部作品确认角色表时会把第一部作品
的同号角色改名。代码从这一版起在写入时把雪花角色 id 规范成 ``f"{project_id}_{raw}"``
（``services/snowflake_character_ids.py``）；这个迁移把库里已有的数据一次规范好，否则新写入的规范 id 与旧行对不上。

范围：只动雪花作品（``story_projects.planning_mode = 'snowflake'``）。成员 id 只改**出现在雪花数据中**的——角色计划
（``snowflake_character_plans``）与各步草稿里的 ``characters[].character_id``；资料库 / 章节编排自己铸的
``CHAR_<hex>`` 本来就全局唯一，不在构思里就不动。视角 / 在场 / 全书主角这些**引用**与运行时同一条规则
（``canonical_character_ref``）：指着本作品名册里的角色、或长着前端编号的样子（c1 / c2 / …）才补前缀，手填的姓名
原样——否则部署后前端照常上行一次，已确认的 09 / 10 就因为「视角从 林昭 变成 <作品>_林昭」被打回待审。每个作品：

- ``story_characters``：主键改名（目标 id 已存在时不改名，留一条记录）；``summary_json`` / ``synopsis_json`` /
  ``bible_json`` 里抄的 ``character_id`` 同步改；
- ``snowflake_character_plans``：``character_id`` 与三份 json 里的 ``character_id``（主键 ``character_plan_id`` 不变——
  旧的派生规则 ``snowflake_character_plan_{project}_{raw}`` 恰好等于新规则 ``snowflake_character_plan_{规范 id}``）；
- ``snowflake_scene_plans`` / ``scene_cards``：``pov_character_id``、``onstage_chars_json``（场景卡还有
  ``writer_brief_json.protagonist_character_id``）；
- ``snowflake_step_runs``（全部版本，恢复历史要用）：``draft_json`` 的 ``characters[].character_id``、
  ``protagonist_character_id``、``scenes[].pov_character_id`` / ``onstage_chars_json``——``fe_*`` 写穿键是前端自己的
  数据，一个字不改（服务端交给前端时再把前缀剥掉，前端本机缓存里的 c1 仍然对得上）；
- 下游的审批快照 ``snowflake_step_runs.consumed_input_sigs_json``：它是审批那一刻对上游草稿逐字段算的签名，上游
  草稿里的 id 改了，签名跟着换算（只换「正好等于某一版上游改写前签名」的那些——本来就过期的快照照旧过期）；
  不换的话，部署后第一次重新确认 04（哪怕只改了全书主角）就会把 06 / 08 判成需复核，09 → 10 同理；
- ``outline_plans.plan_json``（待确认的物化计划确认时要写场景卡）与 ``snowflake_assistant_turns.candidate_patch_json``；
- ``library_relations``：``character:<旧 id>`` 的引用改成新 id。

执行契约（``scene_execution_contracts``）不改：它们按场景卡快照哈希复用，视角 id 变了下一次运行自然重建。
每个改过的作品在 ``operation_logs`` 留一条 ``snowflake_character_ids_canonicalized``（旧 id → 新 id 的对照表）。

幂等：已带本作品前缀的 id 原样保留，重跑什么都不改。降级是空操作：新写入的角色从这一版起就是规范 id，把前缀
剥回去会让不同作品的同号角色重新撞上主键（对照表在操作日志里）。历史迁移是冻结的显式 SQL，不导入应用代码
（快照签名的算法也冻结在这里：``snowflake_staleness.semantic_payload`` / ``field_sigs`` 2026-09-30 的样子）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

import sqlalchemy as sa
from alembic import op


revision = "20260929_0093"
down_revision = "20260924_0092"
branch_labels = None
depends_on = None

#: 前端给手加角色的编号（c1 / c2 / …）
_FRONTEND_MINTED = re.compile(r"c\d+")


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


def _map_draft(value: Any, mapper: Callable[[Any], str], ref_mapper: Callable[[Any], str]) -> Any:
    """与 ``snowflake_character_ids.canonicalize_draft`` 同一形状（冻结在这里）：成员 id 走 ``mapper``，
    视角 / 在场 / 全书主角走 ``ref_mapper``；fe_* 键不碰。"""
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
        result["protagonist_character_id"] = ref_mapper(value.get("protagonist_character_id"))
    scenes = value.get("scenes")
    if isinstance(scenes, list):
        result["scenes"] = [_map_scene(item, ref_mapper) for item in scenes]
    return result


def _map_scene(item: Any, ref_mapper: Callable[[Any], str]) -> Any:
    if not isinstance(item, dict):
        return item
    scene = dict(item)
    if str(item.get("pov_character_id") or "").strip():
        scene["pov_character_id"] = ref_mapper(item.get("pov_character_id"))
    onstage = item.get("onstage_chars_json")
    if isinstance(onstage, list):
        scene["onstage_chars_json"] = [ref_mapper(entry) for entry in onstage if str(entry or "").strip()]
    return scene


def _map_member(value: Any, mapper: Callable[[Any], str]) -> Any:
    if isinstance(value, dict) and str(value.get("character_id") or "").strip():
        return {**value, "character_id": mapper(value.get("character_id"))}
    return value


# ---- 冻结的快照签名（``snowflake_staleness`` 2026-09-30）：consumed_input_sigs_json 是这样算的 ----

_SCENE_ROW_PACKAGING_KEYS = frozenset(
    {
        "scene_plan_id", "chapter_plan_id", "chapter_id", "chapter_title", "chapter_goal", "scene_seq",
        "status", "stale_reason", "stale_accepted_at", "stale_accepted_by", "stale_accepted_note", "diagnosis",
    }
)


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False


def _semantic_scene_row(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    mode = str(item.get("rendering_mode") or "").strip().lower()
    if "rendering_mode" in item and mode in {"", "full"}:
        item = {key: value for key, value in item.items() if key != "rendering_mode"}
    return {key: value for key, value in item.items() if key not in _SCENE_ROW_PACKAGING_KEYS}


def _semantic_payload(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    result = {key: value for key, value in data.items() if not str(key).startswith("fe_") and not _is_empty_value(value)}
    scenes = result.get("scenes")
    if isinstance(scenes, list):
        result["scenes"] = [_semantic_scene_row(item) for item in scenes]
    return result


def _field_sigs(payload: Any) -> dict[str, str]:
    def sig(value: Any) -> str:
        text = json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    return {key: sig(value) for key, value in _semantic_payload(payload).items()}


def _move_input_snapshots(bind: Any, project_id: str, rewritten: list[tuple[str, Any, Any]]) -> int:
    """上游草稿改写前后的逐字段签名对照（旧签名 → 新签名，按 (步, 字段)），套到本作品每一版的审批快照上。

    只换正好等于某一版上游改写前签名的快照字段：快照拍的就是那份内容，改写后它等于那一版的新签名；拍的是库里
    已经没有的内容（待审版后来被原位改写）或本来就过期的，签名对不上，原样留着——该过期的照旧过期。"""
    moves: dict[tuple[str, str], dict[str, str]] = {}
    for step_key, before, after in rewritten:
        old_sigs, new_sigs = _field_sigs(before), _field_sigs(after)
        for field, old_sig in old_sigs.items():
            new_sig = new_sigs.get(field)
            if new_sig is not None and new_sig != old_sig:
                moves.setdefault((step_key, field), {})[old_sig] = new_sig
    if not moves:
        return 0
    moved = 0
    for run_id, raw in bind.execute(
        sa.text(
            "SELECT step_run_id, consumed_input_sigs_json FROM snowflake_step_runs "
            "WHERE project_id = :pid AND consumed_input_sigs_json IS NOT NULL"
        ),
        {"pid": project_id},
    ).fetchall():
        snapshot = _load(raw)
        if not isinstance(snapshot, dict):
            continue
        remapped: dict[str, Any] = {}
        for upstream, sigs in snapshot.items():
            if isinstance(sigs, dict):
                sigs = {
                    field: moves.get((upstream, field), {}).get(value, value) if isinstance(value, str) else value
                    for field, value in sigs.items()
                }
            remapped[upstream] = sigs
        if remapped != snapshot:
            bind.execute(
                sa.text("UPDATE snowflake_step_runs SET consumed_input_sigs_json = :value WHERE step_run_id = :key"),
                {"value": _dump(remapped), "key": run_id},
            )
            moved += 1
    return moved


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
    character_ids = _snowflake_character_ids(bind, tables, project_id)
    renames = {raw: _canonical(project_id, raw) for raw in character_ids if raw != _canonical(project_id, raw)}
    # 名册（库里口径）：引用据此认出哪些是角色 id
    roster = {_canonical(project_id, value) for value in character_ids}
    prefix = f"{project_id}_"
    changed_rows = 0

    def mapper(value: Any) -> str:
        """成员 id（与角色行、资料库关系）：按对照表改。"""
        text = str(value or "").strip()
        return renames.get(text, text)

    def ref_mapper(value: Any) -> str:
        """视角 / 在场 / 全书主角：与运行时 ``canonical_character_ref`` 同一条规则。"""
        text = str(value or "").strip()
        if not text or text.startswith(prefix):
            return text
        if _FRONTEND_MINTED.fullmatch(text) or prefix + text in roster:
            return prefix + text
        return text

    kept: list[str] = []
    if renames and "story_characters" in tables:
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
            changed_rows += 1

    if renames and "snowflake_character_plans" in tables:
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
            changed_rows += 1

    for table, key in (("snowflake_scene_plans", "scene_plan_id"), ("scene_cards", "scene_id")):
        if table not in tables:
            continue
        with_brief = table == "scene_cards"
        columns = f"{key}, pov_character_id, onstage_chars_json" + (", writer_brief_json" if with_brief else "")
        for row in bind.execute(sa.text(f"SELECT {columns} FROM {table} WHERE project_id = :pid"), {"pid": project_id}).fetchall():
            row_key, pov, onstage = row[0], row[1], row[2]
            values: dict[str, Any] = {"key": row_key}
            sets: list[str] = []
            if str(pov or "").strip() and ref_mapper(pov) != pov:
                values["pov"] = ref_mapper(pov)
                sets.append("pov_character_id = :pov")
            onstage_list = _load(onstage)
            if isinstance(onstage_list, list):
                mapped = [ref_mapper(entry) for entry in onstage_list if str(entry or "").strip()]
                if mapped != onstage_list:
                    values["onstage"] = _dump(mapped)
                    sets.append("onstage_chars_json = :onstage")
            if with_brief:
                brief = _load(row[3])
                if isinstance(brief, dict) and str(brief.get("protagonist_character_id") or "").strip():
                    new_brief = {**brief, "protagonist_character_id": ref_mapper(brief.get("protagonist_character_id"))}
                    if new_brief != brief:
                        values["brief"] = _dump(new_brief)
                        sets.append("writer_brief_json = :brief")
            if sets:
                bind.execute(sa.text(f"UPDATE {table} SET {', '.join(sets)} WHERE {key} = :key"), values)
                changed_rows += 1

    # 各步草稿：记下改写前后，下游的审批快照要跟着换算
    rewritten: list[tuple[str, Any, Any]] = []
    if "snowflake_step_runs" in tables:
        for run_id, step_key, raw in bind.execute(
            sa.text("SELECT step_run_id, step_key, draft_json FROM snowflake_step_runs WHERE project_id = :pid"),
            {"pid": project_id},
        ).fetchall():
            value = _load(raw)
            mapped = _map_draft(value, mapper, ref_mapper)
            if isinstance(value, dict) and mapped != value:
                bind.execute(
                    sa.text("UPDATE snowflake_step_runs SET draft_json = :value WHERE step_run_id = :key"),
                    {"value": _dump(mapped), "key": run_id},
                )
                rewritten.append((str(step_key), value, mapped))
                changed_rows += 1
    moved_snapshots = _move_input_snapshots(bind, project_id, rewritten) if rewritten else 0

    if "snowflake_assistant_turns" in tables:
        for turn_id, raw in bind.execute(
            sa.text("SELECT turn_id, candidate_patch_json FROM snowflake_assistant_turns WHERE project_id = :pid"),
            {"pid": project_id},
        ).fetchall():
            value = _load(raw)
            mapped = _map_draft(value, mapper, ref_mapper)
            if isinstance(value, dict) and mapped != value:
                bind.execute(
                    sa.text("UPDATE snowflake_assistant_turns SET candidate_patch_json = :value WHERE turn_id = :key"),
                    {"value": _dump(mapped), "key": turn_id},
                )
                changed_rows += 1

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
                        scene = _map_scene(scene, ref_mapper)
                        brief = scene.get("writer_brief_json") if isinstance(scene, dict) else None
                        if isinstance(brief, dict) and str(brief.get("protagonist_character_id") or "").strip():
                            scene = {**scene, "writer_brief_json": {**brief, "protagonist_character_id": ref_mapper(brief["protagonist_character_id"])}}
                        scenes.append(scene)
                    chapter = {**chapter, "scenes": scenes}
                chapters.append(chapter)
            if isinstance(plan.get("chapters"), list) and chapters != plan["chapters"]:
                bind.execute(
                    sa.text("UPDATE outline_plans SET plan_json = :value WHERE plan_id = :plan_id"),
                    {"value": _dump({**plan, "chapters": chapters}), "plan_id": plan_id},
                )
                changed_rows += 1

    if renames and "library_relations" in tables:
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
                changed_rows += 1

    if changed_rows and "operation_logs" in tables:
        bind.execute(
            sa.text(
                "INSERT INTO operation_logs (event_type, object_type, object_ref, payload_json, created_at) "
                "VALUES ('snowflake_character_ids_canonicalized', 'story_project', :pid, :payload, :now)"
            ),
            {
                "pid": project_id,
                "payload": _dump(
                    {
                        "project_id": project_id,
                        "renamed": dict(sorted(renames.items())),
                        "kept_old_rows": sorted(kept),
                        "rewritten_rows": changed_rows,
                        "moved_input_snapshots": moved_snapshots,
                    }
                ),
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
