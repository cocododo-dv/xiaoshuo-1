"""Wave 7（结果闭环治理 §8 Wave7 项 5）：数据库备份 / WAL 一致性 / 恢复演练。

SQLite 在线备份 API 产出一致性单文件快照（含未 checkpoint 的 WAL 写入）；恢复原子替换；
verify 用 PRAGMA integrity_check + 元数据 checksum 双校验。恢复不回滚已归档正文之外的
任何东西——纯运维工具。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from novel_system.tools import db_backup


def _make_db(path, *, rows=3, wal=True):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (id, v) VALUES (?, ?)", [(i, f"v{i}") for i in range(rows)])
    conn.commit()
    return conn


def _read_all(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    finally:
        conn.close()


def test_resolve_sqlite_path_from_url():
    assert db_backup.resolve_sqlite_path("sqlite:///./x.db") == "./x.db"
    assert db_backup.resolve_sqlite_path("sqlite:////abs/x.db") == "/abs/x.db"
    assert db_backup.resolve_sqlite_path("/plain/path.db") == "/plain/path.db"


def test_backup_produces_verifiable_snapshot(tmp_path):
    src = str(tmp_path / "src.db")
    conn = _make_db(src, rows=5)
    conn.close()
    dst = str(tmp_path / "backup.db")
    meta = db_backup.backup_database(src, dst)
    assert (tmp_path / "backup.db").exists()
    assert (tmp_path / "backup.db.meta.json").exists()
    assert meta["checksum"]
    assert meta["page_count"] > 0
    # 清单随备份入库（公开仓库）：只记库文件名，不记本机绝对路径（B12-22）
    assert meta["source"] == "src.db"
    v = db_backup.verify_backup(dst)
    assert v["ok"] is True
    assert v["integrity"] == "ok"
    assert v["checksum_ok"] is True
    assert v["foreign_key_violations"] == 0
    assert v["metadata_ok"] is True


def test_backup_captures_uncheckpointed_wal_writes(tmp_path):
    src = str(tmp_path / "src.db")
    conn = _make_db(src, rows=2)
    # 追加写并提交，但不手动 checkpoint —— 数据仍在 WAL 里
    conn.execute("INSERT INTO t (id, v) VALUES (99, 'wal_only')")
    conn.commit()
    dst = str(tmp_path / "backup.db")
    db_backup.backup_database(src, dst)
    conn.close()
    # 备份必须含 WAL 中的写入（一致性快照）
    rows = _read_all(dst)
    assert (99, "wal_only") in rows


def test_restore_reproduces_data(tmp_path):
    src = str(tmp_path / "src.db")
    conn = _make_db(src, rows=4)
    conn.close()
    original = _read_all(src)
    backup = str(tmp_path / "b.db")
    db_backup.backup_database(src, backup)
    # 破坏现库
    with open(src, "wb") as f:
        f.write(b"\x00" * 64)
    # 恢复
    db_backup.restore_database(backup, src)
    assert _read_all(src) == original


def test_verify_rejects_corrupt_file(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database at all")
    v = db_backup.verify_backup(str(bad))
    assert v["ok"] is False


def test_restore_refuses_corrupt_backup(tmp_path):
    src = str(tmp_path / "live.db")
    _make_db(src, rows=1).close()
    live_before = _read_all(src)
    bad = tmp_path / "bad_backup.db"
    bad.write_bytes(b"garbage")
    with pytest.raises(Exception):
        db_backup.restore_database(str(bad), src)
    # 拒绝损坏备份后现库不被破坏
    assert _read_all(src) == live_before


def test_verify_and_restore_require_a_sidecar_manifest(tmp_path):
    source = str(tmp_path / "source.db")
    destination = str(tmp_path / "destination.db")
    _make_db(source, rows=2).close()
    _make_db(destination, rows=1).close()

    verification = db_backup.verify_backup(source)
    assert verification["ok"] is False
    assert verification["error"] == "metadata_missing"
    with pytest.raises(ValueError, match="metadata_missing"):
        db_backup.restore_database(source, destination)
    assert _read_all(destination) == [(0, "v0")]


def test_backup_rejects_same_source_and_destination_without_touching_source(tmp_path):
    source = str(tmp_path / "source.db")
    _make_db(source, rows=2).close()
    before = (tmp_path / "source.db").read_bytes()

    with pytest.raises(ValueError, match="different files"):
        db_backup.backup_database(source, source)

    assert (tmp_path / "source.db").read_bytes() == before
    assert _read_all(source) == [(0, "v0"), (1, "v1")]


def test_failed_snapshot_build_preserves_previous_backup(tmp_path, monkeypatch):
    source = str(tmp_path / "source.db")
    previous_source = str(tmp_path / "previous.db")
    destination = str(tmp_path / "backup.db")
    _make_db(source, rows=3).close()
    _make_db(previous_source, rows=1).close()
    db_backup.backup_database(previous_source, destination)
    before_database = (tmp_path / "backup.db").read_bytes()
    before_manifest = (tmp_path / "backup.db.meta.json").read_bytes()

    def fail_build(_source, _target):
        raise sqlite3.OperationalError("injected backup failure")

    monkeypatch.setattr(db_backup, "_build_snapshot", fail_build)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        db_backup.backup_database(source, destination)

    assert (tmp_path / "backup.db").read_bytes() == before_database
    assert (tmp_path / "backup.db.meta.json").read_bytes() == before_manifest


def test_restore_refuses_busy_destination_and_preserves_uncommitted_wal(tmp_path):
    source = str(tmp_path / "source.db")
    destination = str(tmp_path / "destination.db")
    backup = str(tmp_path / "backup.db")
    _make_db(source, rows=3).close()
    db_backup.backup_database(source, backup)
    writer = _make_db(destination, rows=1)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE t SET v = 'uncommitted' WHERE id = 0")
    try:
        with pytest.raises(RuntimeError, match="stop all database users|WAL is busy"):
            db_backup.restore_database(backup, destination)
        assert writer.execute("SELECT v FROM t WHERE id = 0").fetchone()[0] == "uncommitted"
    finally:
        writer.rollback()
        writer.close()
    assert _read_all(destination) == [(0, "v0")]


def test_manifest_tampering_fails_closed(tmp_path):
    source = str(tmp_path / "source.db")
    backup = str(tmp_path / "backup.db")
    _make_db(source, rows=2).close()
    db_backup.backup_database(source, backup)
    manifest_path = tmp_path / "backup.db.meta.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["page_count"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verification = db_backup.verify_backup(backup)
    assert verification["ok"] is False
    assert verification["page_count_ok"] is False
