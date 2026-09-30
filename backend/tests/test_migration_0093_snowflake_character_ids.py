"""迁移 20260929_0093：雪花作品里手加角色的 id 补上作品前缀（B06-01，作者批准 #16c）——只改数据、幂等。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_system.services.snowflake_staleness import field_sigs, recompute_stale
from novel_system.services.snowflake_step_catalog import STEP_ORDER
from tests.test_migration_0084_scene_plan_rendering_mode import _insert_minimal_row, _migrate

PREVIOUS_HEAD = "20260924_0092"
CURRENT_HEAD = "20260929_0093"


def _project(connection: sqlite3.Connection, project_id: str, mode: str = "snowflake") -> None:
    _insert_minimal_row(connection, "story_projects", {"project_id": project_id, "title": project_id, "planning_mode": mode})


def _seed_work(connection: sqlite3.Connection, project_id: str, names: dict[str, str]) -> None:
    _project(connection, project_id)
    for raw, name in names.items():
        member = {"character_id": raw, "display_name": name, "role": "主角"}
        _insert_minimal_row(
            connection,
            "story_characters",
            {"character_id": f"{raw}" if project_id == "PA" else f"{raw}-{project_id}", "project_id": project_id,
             "display_name": name, "summary_json": json.dumps(member, ensure_ascii=False), "bible_json": "{}", "synopsis_json": "{}"},
        )
        _insert_minimal_row(
            connection,
            "snowflake_character_plans",
            {"character_plan_id": f"snowflake_character_plan_{project_id}_{raw}", "project_id": project_id, "character_id": raw,
             "display_name": name, "summary_json": json.dumps(member, ensure_ascii=False)},
        )
    characters = [{"character_id": raw, "display_name": name} for raw, name in names.items()]
    first = next(iter(names))
    _insert_minimal_row(
        connection,
        "snowflake_step_runs",
        {"step_run_id": f"run-{project_id}-04", "project_id": project_id, "step_key": "character_sheets", "version": 1, "status": "approved",
         "draft_json": json.dumps({"characters": characters, "protagonist_character_id": first,
                                   "fe_scaffold": {"sel": first, "chars": {raw: {"name": name} for raw, name in names.items()}}},
                                  ensure_ascii=False)},
    )
    _insert_minimal_row(
        connection,
        "snowflake_step_runs",
        {"step_run_id": f"run-{project_id}-09", "project_id": project_id, "step_key": "scene_list", "version": 1, "status": "approved",
         "draft_json": json.dumps({"scenes": [{"row_uid": "r1", "pov_character_id": first, "onstage_chars_json": list(names)}],
                                   "fe_scaffold": {"list": [{"id": "r1", "pov": first}]}}, ensure_ascii=False)},
    )
    _insert_minimal_row(
        connection,
        "snowflake_scene_plans",
        {"scene_plan_id": f"sp-{project_id}", "project_id": project_id, "row_uid": "r1", "scene_id": f"{project_id}_SC_r1",
         "chapter_id": f"{project_id}_CH01", "scene_seq": 1, "pov_character_id": first, "onstage_chars_json": json.dumps(list(names))},
    )
    _insert_minimal_row(connection, "chapter_goals", {"chapter_id": f"{project_id}_CH01", "project_id": project_id, "chapter_goal": "x"})
    _insert_minimal_row(
        connection,
        "scene_cards",
        {"scene_id": f"{project_id}_SC_r1", "project_id": project_id, "chapter_id": f"{project_id}_CH01", "scene_seq": 1,
         "scene_goal": "x", "pov_character_id": first, "onstage_chars_json": json.dumps(list(names)),
         "writer_brief_json": json.dumps({"protagonist_character_id": first, "source": "snowflake_method"})},
    )
    _insert_minimal_row(
        connection,
        "outline_plans",
        {"plan_id": f"plan-{project_id}", "project_id": project_id, "version": 1, "status": "pending_review",
         "plan_json": json.dumps({"chapters": [{"scenes": [{"scene_id": "s", "pov_character_id": first, "onstage_chars_json": [first],
                                                              "writer_brief_json": {"protagonist_character_id": first}}]}]})},
    )


def _json(connection: sqlite3.Connection, sql: str, *params) -> object:
    return json.loads(connection.execute(sql, params).fetchone()[0])


def test_0093_prefixes_snowflake_character_ids_everywhere_and_leaves_the_frontend_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "character-ids-0093.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        _seed_work(connection, "PA", {"c1": "林昭", "c2": "程远"})
        # 大纲驱动作品与资料库铸的 CHAR_ id 不在构思里：不动
        _project(connection, "PO", mode="outline_driven")
        _insert_minimal_row(connection, "story_characters", {"character_id": "CHAR_ABC", "project_id": "PO", "display_name": "路人"})
        _insert_minimal_row(connection, "story_characters", {"character_id": "CHAR_LIB", "project_id": "PA", "display_name": "资料库里的人"})
        _insert_minimal_row(connection, "library_relations", {"relation_id": "rel-1", "project_id": "PA", "from_ref": "character:c1", "to_ref": "character:CHAR_LIB"})
        connection.commit()

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        characters = dict(connection.execute("SELECT character_id, project_id FROM story_characters").fetchall())
        assert characters == {"PA_c1": "PA", "PA_c2": "PA", "CHAR_ABC": "PO", "CHAR_LIB": "PA"}
        assert _json(connection, "SELECT summary_json FROM story_characters WHERE character_id = 'PA_c1'")["character_id"] == "PA_c1"
        plans = dict(connection.execute("SELECT character_plan_id, character_id FROM snowflake_character_plans").fetchall())
        assert plans == {"snowflake_character_plan_PA_c1": "PA_c1", "snowflake_character_plan_PA_c2": "PA_c2"}

        sheets = _json(connection, "SELECT draft_json FROM snowflake_step_runs WHERE step_run_id = 'run-PA-04'")
        assert [item["character_id"] for item in sheets["characters"]] == ["PA_c1", "PA_c2"]
        assert sheets["protagonist_character_id"] == "PA_c1"
        assert sheets["fe_scaffold"] == {"sel": "c1", "chars": {"c1": {"name": "林昭"}, "c2": {"name": "程远"}}}
        scene_list = _json(connection, "SELECT draft_json FROM snowflake_step_runs WHERE step_run_id = 'run-PA-09'")
        assert scene_list["scenes"][0]["pov_character_id"] == "PA_c1"
        assert scene_list["scenes"][0]["onstage_chars_json"] == ["PA_c1", "PA_c2"]
        assert scene_list["fe_scaffold"] == {"list": [{"id": "r1", "pov": "c1"}]}

        assert connection.execute("SELECT pov_character_id FROM snowflake_scene_plans").fetchone() == ("PA_c1",)
        assert _json(connection, "SELECT onstage_chars_json FROM snowflake_scene_plans") == ["PA_c1", "PA_c2"]
        assert connection.execute("SELECT pov_character_id FROM scene_cards").fetchone() == ("PA_c1",)
        assert _json(connection, "SELECT writer_brief_json FROM scene_cards")["protagonist_character_id"] == "PA_c1"
        outline_scene = _json(connection, "SELECT plan_json FROM outline_plans")["chapters"][0]["scenes"][0]
        assert outline_scene["pov_character_id"] == "PA_c1" and outline_scene["writer_brief_json"]["protagonist_character_id"] == "PA_c1"
        assert connection.execute("SELECT from_ref, to_ref FROM library_relations").fetchone() == ("character:PA_c1", "character:CHAR_LIB")
        log = _json(
            connection,
            "SELECT payload_json FROM operation_logs WHERE event_type = 'snowflake_character_ids_canonicalized' AND object_ref = 'PA'",
        )
        assert log["renamed"] == {"c1": "PA_c1", "c2": "PA_c2"}

    # 幂等：已经带前缀的 id 原样保留，再跑一遍什么都不改（降级是空操作，重新升级就是重跑）
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path, down=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (PREVIOUS_HEAD,)
        assert "PA_c1" in dict(connection.execute("SELECT character_id, project_id FROM story_characters").fetchall())
    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        assert dict(connection.execute("SELECT character_id, project_id FROM story_characters").fetchall()) == {
            "PA_c1": "PA", "PA_c2": "PA", "CHAR_ABC": "PO", "CHAR_LIB": "PA",
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM operation_logs WHERE event_type = 'snowflake_character_ids_canonicalized'"
        ).fetchone() == (1,)


def test_0093_separates_two_works_that_share_hand_made_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "character-ids-0093-two-works.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        _seed_work(connection, "PA", {"c1": "林昭"})
        _seed_work(connection, "PB", {"c1": "苏晴"})
        connection.commit()
    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        plans = dict(connection.execute("SELECT character_plan_id, character_id FROM snowflake_character_plans").fetchall())
        assert plans == {"snowflake_character_plan_PA_c1": "PA_c1", "snowflake_character_plan_PB_c1": "PB_c1"}
        assert connection.execute("SELECT pov_character_id FROM scene_cards WHERE project_id = 'PB'").fetchone() == ("PB_c1",)
        # 乙作品的角色行在种子里不叫 c1（全局主键已被甲占了）——它不在构思数据里，原样保留
        characters = dict(connection.execute("SELECT character_id, project_id FROM story_characters").fetchall())
        assert characters == {"PA_c1": "PA", "c1-PB": "PB"}


def test_0093_leaves_typed_names_alone_and_prefixes_frontend_numbered_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """引用与运行时同一条规则：指着名册里的角色、或长着前端编号样子（c1 / c2 / …）才补前缀，手填的姓名原样——
    否则部署后前端照常上行一次，运行时认出的口径与迁移写下的对不上，已确认的一步就被打回待审。"""
    path = tmp_path / "character-ids-0093-refs.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        _seed_work(connection, "PA", {"c1": "林昭"})
        scenes = [{"row_uid": "r2", "pov_character_id": "林昭", "onstage_chars_json": ["程远", "c9", "c1", "CHAR_LIB"]}]
        _insert_minimal_row(
            connection,
            "snowflake_step_runs",
            {"step_run_id": "run-PA-10", "project_id": "PA", "step_key": "scene_details", "version": 1, "status": "approved",
             "draft_json": json.dumps({"scenes": scenes}, ensure_ascii=False)},
        )
        _insert_minimal_row(
            connection,
            "snowflake_scene_plans",
            {"scene_plan_id": "sp-PA-typed", "project_id": "PA", "row_uid": "r2", "scene_id": "PA_SC_r2", "chapter_id": "PA_CH01",
             "scene_seq": 2, "pov_character_id": "林昭", "onstage_chars_json": json.dumps(["程远", "c9"], ensure_ascii=False)},
        )
        _insert_minimal_row(
            connection,
            "scene_cards",
            {"scene_id": "PA_SC_r2", "project_id": "PA", "chapter_id": "PA_CH01", "scene_seq": 2, "scene_goal": "x",
             "pov_character_id": "林昭", "onstage_chars_json": json.dumps(["程远"], ensure_ascii=False),
             "writer_brief_json": json.dumps({"protagonist_character_id": "林昭"}, ensure_ascii=False)},
        )
        connection.commit()

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        details = _json(connection, "SELECT draft_json FROM snowflake_step_runs WHERE step_run_id = 'run-PA-10'")
        assert details["scenes"][0]["pov_character_id"] == "林昭"
        assert details["scenes"][0]["onstage_chars_json"] == ["程远", "PA_c9", "PA_c1", "CHAR_LIB"]
        plan = connection.execute("SELECT pov_character_id, onstage_chars_json FROM snowflake_scene_plans WHERE scene_plan_id = 'sp-PA-typed'").fetchone()
        assert plan[0] == "林昭" and json.loads(plan[1]) == ["程远", "PA_c9"]
        card = connection.execute("SELECT pov_character_id, writer_brief_json FROM scene_cards WHERE scene_id = 'PA_SC_r2'").fetchone()
        assert card[0] == "林昭" and json.loads(card[1])["protagonist_character_id"] == "林昭"
        # 名册里的人照旧改
        assert connection.execute("SELECT pov_character_id FROM snowflake_scene_plans WHERE scene_plan_id = 'sp-PA'").fetchone() == ("PA_c1",)


def _run(connection: sqlite3.Connection, run_id: str, step_key: str, version: int, status: str, draft: dict, sigs: dict | None = None) -> None:
    values = {"step_run_id": run_id, "project_id": "PS", "step_key": step_key, "version": version, "status": status,
              "draft_json": json.dumps(draft, ensure_ascii=False)}
    if sigs is not None:
        values["consumed_input_sigs_json"] = json.dumps(sigs, ensure_ascii=False)
    _insert_minimal_row(connection, "snowflake_step_runs", values)


def test_0093_moves_downstream_input_snapshots_with_the_rewritten_drafts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下游的审批快照是对上游草稿逐字段算的签名：上游草稿里的 id 改了，快照跟着换算。不换的话，部署后第一次
    重新确认 04（哪怕只换了全书主角）就会把没受影响的 06 判成需复核；本来就过期的快照照旧过期。"""
    older_sheets = {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角"}]}
    sheets = {
        "characters": [
            {"character_id": "c1", "display_name": "林昭", "role": "主角"},
            {"character_id": "c2", "display_name": "程远", "role": "对手"},
        ],
        "protagonist_character_id": "c1",
        "fe_scaffold": {"chars": {"c1": {"name": "林昭"}, "c2": {"name": "程远"}}},
    }
    synopses = {"characters": [{"character_id": "c1", "display_name": "林昭", "synopsis": "视角故事：她回到雨城"}]}
    path = tmp_path / "character-ids-0093-snapshots.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        _project(connection, "PS")
        for raw, name in (("c1", "林昭"), ("c2", "程远")):
            _insert_minimal_row(
                connection,
                "snowflake_character_plans",
                {"character_plan_id": f"snowflake_character_plan_PS_{raw}", "project_id": "PS", "character_id": raw, "display_name": name},
            )
        _run(connection, "run-PS-04-v1", "character_sheets", 1, "superseded", older_sheets)
        _run(connection, "run-PS-04-v2", "character_sheets", 2, "approved", sheets)
        # 06 在 04 第二版之后确认：快照拍的就是现在的 04；08 的快照停在第一版（本来就过期）
        _run(connection, "run-PS-06", "character_synopses", 1, "approved", synopses, {"character_sheets": field_sigs(sheets)})
        _run(connection, "run-PS-08", "character_bibles", 1, "approved", synopses, {"character_sheets": field_sigs(older_sheets), "character_synopses": field_sigs(synopses)})
        connection.commit()

    _migrate(path, CURRENT_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        migrated_sheets = _json(connection, "SELECT draft_json FROM snowflake_step_runs WHERE step_run_id = 'run-PS-04-v2'")
        migrated_older = _json(connection, "SELECT draft_json FROM snowflake_step_runs WHERE step_run_id = 'run-PS-04-v1'")
        migrated_synopses = _json(connection, "SELECT draft_json FROM snowflake_step_runs WHERE step_run_id = 'run-PS-06'")
        snapshot_06 = _json(connection, "SELECT consumed_input_sigs_json FROM snowflake_step_runs WHERE step_run_id = 'run-PS-06'")
        snapshot_08 = _json(connection, "SELECT consumed_input_sigs_json FROM snowflake_step_runs WHERE step_run_id = 'run-PS-08'")
        log = _json(connection, "SELECT payload_json FROM operation_logs WHERE event_type = 'snowflake_character_ids_canonicalized' AND object_ref = 'PS'")
    assert migrated_sheets["protagonist_character_id"] == "PS_c1"
    assert snapshot_06 == {"character_sheets": field_sigs(migrated_sheets)}
    assert snapshot_08 == {"character_sheets": field_sigs(migrated_older), "character_synopses": field_sigs(migrated_synopses)}
    assert log["moved_input_snapshots"] == 2

    # 部署后只换全书主角、重新确认 04：06 只读 04 的角色表，没受影响；08 的快照本来就停在旧版，照旧需复核
    current = field_sigs({**migrated_sheets, "protagonist_character_id": "PS_c2"})
    rows = [
        SimpleNamespace(step_key="character_synopses", consumed_input_sigs_json=snapshot_06),
        SimpleNamespace(step_key="character_bibles", consumed_input_sigs_json=snapshot_08),
    ]
    hits = recompute_stale(changed_step_key="character_sheets", current_field_sigs=current, candidate_rows=rows, step_order=STEP_ORDER)
    assert [hit.step_key for hit in hits] == ["character_bibles"]
