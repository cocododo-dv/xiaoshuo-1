"""迁移 20260722_0074：清掉机器盖上去的 30s LLM 超时上限。

守两条：

1. ``parsed_json`` 与 ``yaml_raw`` 只能一起动。运行时读的是 parsed_json，而 系统配置
   的 YAML 编辑器读的是 yaml_raw、并在作者下一次保存时重新校验写回 parsed_json ——
   只清一半的话，编辑器里 30.0 还在，下一次保存就把它原样送回运行时，没有任何提示。
2. ``downgrade`` 不许给「本来就没有上限」的快照盖上 30.0：升级之后，被这次迁移清过的
   快照和后来作者主动不设上限的快照在库里长得一模一样，无差别回填等于把这个迁移专门
   要消灭的缺陷重新种回去，还种到它从没碰过的行上。
"""

from __future__ import annotations

import json

import sqlalchemy as sa
import yaml
from alembic import command

from tests.support.migrations import alembic_config

_LEGACY_SNAPSHOTS = """
CREATE TABLE system_config_snapshots (
    snapshot_id VARCHAR NOT NULL PRIMARY KEY,
    category VARCHAR NOT NULL,
    version INTEGER NOT NULL,
    yaml_raw TEXT NOT NULL,
    parsed_json JSON,
    validation_json JSON,
    status VARCHAR NOT NULL DEFAULT 'draft',
    active_flag INTEGER NOT NULL DEFAULT 0,
    created_by VARCHAR,
    created_at VARCHAR NOT NULL,
    activated_at VARCHAR
)
"""


def _insert(conn, *, snapshot_id: str, payload: dict, yaml_payload: dict | str) -> None:
    raw = yaml_payload if isinstance(yaml_payload, str) else yaml.safe_dump(
        yaml_payload, allow_unicode=True, sort_keys=False
    )
    conn.execute(
        sa.text(
            "INSERT INTO system_config_snapshots "
            "(snapshot_id, category, version, yaml_raw, parsed_json, validation_json, status, "
            " active_flag, created_at) "
            "VALUES (:pk, 'api', 1, :raw, :parsed, '{}', 'active', 1, '2026-07-01T00:00:00Z')"
        ),
        {"pk": snapshot_id, "raw": raw, "parsed": json.dumps(payload, ensure_ascii=False)},
    )


def _upgrade(tmp_path, monkeypatch, seed) -> dict[str, tuple[dict, dict | None]]:
    """建一个 0073 形态的库、灌数据、跑到 head，回读 (parsed_json, yaml 解析结果)。"""
    from novel_system.db.session import reset_engine

    legacy_db = tmp_path / "legacy-0074.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{legacy_db}")
    reset_engine()

    engine = sa.create_engine(f"sqlite:///{legacy_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_LEGACY_SNAPSHOTS))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('20260716_0073')"))
            seed(conn)

        command.upgrade(alembic_config(), "20260722_0074")

        with engine.begin() as conn:
            result = {}
            for pk, raw, parsed in conn.execute(
                sa.text("SELECT snapshot_id, yaml_raw, parsed_json FROM system_config_snapshots")
            ):
                try:
                    from_yaml = yaml.safe_load(raw)
                except yaml.YAMLError:
                    from_yaml = None
                result[pk] = (json.loads(parsed), from_yaml)
            return result
    finally:
        engine.dispose()
        reset_engine()


def test_the_legacy_ceiling_is_cleared_from_both_columns(tmp_path, monkeypatch) -> None:
    payload = {"llm": {"enabled": True, "provider": "openai", "timeout_seconds": 30.0}}

    def seed(conn):
        _insert(conn, snapshot_id="snap_legacy", payload=payload, yaml_payload=payload)

    rows = _upgrade(tmp_path, monkeypatch, seed)
    parsed, from_yaml = rows["snap_legacy"]
    assert "timeout_seconds" not in parsed["llm"]
    assert "timeout_seconds" not in from_yaml["llm"], \
        "YAML 里 30.0 还在——编辑器会照旧显示它，作者下一次保存就把上限送回运行时"


def test_an_authored_ceiling_is_left_exactly_as_written(tmp_path, monkeypatch) -> None:
    payload = {"llm": {"enabled": True, "timeout_seconds": 180.0}}

    def seed(conn):
        _insert(conn, snapshot_id="snap_authored", payload=payload, yaml_payload=payload)

    parsed, from_yaml = _upgrade(tmp_path, monkeypatch, seed)["snap_authored"]
    assert parsed["llm"]["timeout_seconds"] == 180.0
    assert from_yaml["llm"]["timeout_seconds"] == 180.0


def test_a_row_whose_yaml_cannot_be_cleaned_is_skipped_whole(tmp_path, monkeypatch) -> None:
    """两列对不上时整行不动——绝不写出「parsed 干净、YAML 还脏」的半吊子状态。

    回归：旧实现在 YAML 剥不掉时回退成原文，照样写 UPDATE，于是两列分家。运行时读
    parsed_json 以为上限没了，编辑器读 yaml_raw 显示 30.0，作者一保存就复活，无任何提示。
    """
    parsed_payload = {"llm": {"enabled": True, "timeout_seconds": 30.0}}

    def seed(conn):
        # YAML 手工编辑坏了（tab 缩进），解析不出来
        _insert(conn, snapshot_id="snap_broken_yaml", payload=parsed_payload,
                yaml_payload="llm:\n\tenabled: true\n\ttimeout_seconds: 30.0\n")
        # YAML 能解析，但它声明的上限是作者写的 180（与 parsed_json 分家已久）
        _insert(conn, snapshot_id="snap_divergent", payload=parsed_payload,
                yaml_payload={"llm": {"enabled": True, "timeout_seconds": 180.0}})

    rows = _upgrade(tmp_path, monkeypatch, seed)

    broken_parsed, broken_yaml = rows["snap_broken_yaml"]
    assert broken_parsed["llm"]["timeout_seconds"] == 30.0, "parsed 被单独清了，YAML 却没动"
    assert broken_yaml is None  # 仍然是那份坏 YAML，原样保留

    divergent_parsed, divergent_yaml = rows["snap_divergent"]
    assert divergent_parsed["llm"]["timeout_seconds"] == 30.0
    assert divergent_yaml["llm"]["timeout_seconds"] == 180.0


def test_non_api_snapshots_are_not_touched(tmp_path, monkeypatch) -> None:
    payload = {"llm": {"timeout_seconds": 30.0}}

    def seed(conn):
        conn.execute(
            sa.text(
                "INSERT INTO system_config_snapshots "
                "(snapshot_id, category, version, yaml_raw, parsed_json, validation_json, status, "
                " active_flag, created_at) "
                "VALUES ('snap_models', 'models', 1, :raw, :parsed, '{}', 'active', 1, "
                " '2026-07-01T00:00:00Z')"
            ),
            {"raw": yaml.safe_dump(payload), "parsed": json.dumps(payload)},
        )

    parsed, _ = _upgrade(tmp_path, monkeypatch, seed)["snap_models"]
    assert parsed["llm"]["timeout_seconds"] == 30.0


def test_downgrade_never_stamps_the_ceiling_onto_a_snapshot_that_lacked_it(tmp_path, monkeypatch) -> None:
    """回归：旧 downgrade 给**每一个**没有 timeout_seconds 的 api 快照盖上 30.0。

    升级之后，被这次迁移清过的快照和作者后来主动不设上限的快照在库里完全无法区分，
    所以无差别回填一定会波及后者 —— 把这个迁移专门要消灭的机器上限重新种回去。
    """
    from novel_system.db.session import reset_engine

    legacy_db = tmp_path / "legacy-0074-down.db"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{legacy_db}")
    reset_engine()

    engine = sa.create_engine(f"sqlite:///{legacy_db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_LEGACY_SNAPSHOTS))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('20260722_0074')"))
            # 作者主动配的「不限时」——这次迁移从来没碰过它
            _insert(conn, snapshot_id="snap_no_ceiling",
                    payload={"llm": {"enabled": True, "provider": "deepseek"}},
                    yaml_payload={"llm": {"enabled": True, "provider": "deepseek"}})

        command.downgrade(alembic_config(), "20260716_0073")

        with engine.begin() as conn:
            raw, parsed = conn.execute(
                sa.text("SELECT yaml_raw, parsed_json FROM system_config_snapshots "
                        "WHERE snapshot_id = 'snap_no_ceiling'")
            ).first()
        assert "timeout_seconds" not in json.loads(parsed)["llm"], \
            "降级给一个作者主动不设上限的快照盖上了 30.0"
        assert "timeout_seconds" not in yaml.safe_load(raw)["llm"]
    finally:
        engine.dispose()
        reset_engine()
