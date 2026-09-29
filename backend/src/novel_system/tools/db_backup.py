"""SQLite 一致性备份、校验与停机恢复工具。

备份使用 ``sqlite3.Connection.backup`` 读取包含 WAL 已提交内容在内的一致逻辑快照。
新快照先写入目标目录中的临时文件，通过完整性、外键和校验和检查后才原子发布；失败不会
先删除上一份可用备份。恢复只接受带 sidecar 清单且校验通过的备份，并在替换目标前要求
WAL checkpoint 不繁忙。运维上仍必须先停止服务：SQLite 无法仅凭文件接口证明另一个空闲
进程不会在检查之后重新写入。

本模块不导入 ORM 或应用 settings，可独立用于任意文件型 SQLite 数据库（命令行入口先过只依赖标准库的检出守卫）。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
from typing import Any

from novel_system.tools._checkout_guard import refuse_foreign_checkout

_META_SUFFIX = ".meta.json"
_SIDECAR_SUFFIXES = ("-wal", "-shm")


def resolve_sqlite_path(url_or_path: str) -> str:
    """把 ``sqlite:///rel`` / ``sqlite:////abs`` / 裸路径统一解析为文件路径。"""
    if url_or_path.startswith("sqlite:////"):
        return "/" + url_or_path[len("sqlite:////"):]
    if url_or_path.startswith("sqlite:///"):
        return url_or_path[len("sqlite:///"):]
    if url_or_path.startswith("sqlite://"):
        return url_or_path[len("sqlite://"):]
    return url_or_path


def _file_path(value: str, *, label: str) -> str:
    path = resolve_sqlite_path(value).strip()
    if not path or path == ":memory:" or path.startswith("file::memory:"):
        raise ValueError(f"{label} must be a file-backed SQLite database")
    return os.path.abspath(path)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_checks(path: str) -> dict[str, Any]:
    connection = sqlite3.connect(path)
    try:
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        integrity = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)
        foreign_key_violations = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        revisions: list[str] = []
        has_alembic = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if has_alembic:
            revisions = sorted(str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version"))
        return {
            "integrity": integrity,
            "foreign_key_violations": foreign_key_violations,
            "page_count": page_count,
            "page_size": page_size,
            "alembic_revisions": revisions,
        }
    finally:
        connection.close()


def _fsync_file(path: str) -> None:
    # Windows CRT 对只读 fd 的 fsync 会报 EBADF；快照已经关闭，rb+ 仅为取得
    # 可刷盘句柄，不会改写内容。
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: str) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-meta-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def _build_snapshot(source: str, target: str) -> None:
    source_connection = sqlite3.connect(source)
    try:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _snapshot_metadata(snapshot: str, source: str, *, tool: str) -> dict[str, Any]:
    checks = _database_checks(snapshot)
    if checks["integrity"] != "ok" or checks["foreign_key_violations"]:
        raise ValueError(f"refusing to publish invalid SQLite snapshot: {checks}")
    return {
        "source": source,
        "checksum": _sha256(snapshot),
        **checks,
        "created_at": _utcnow(),
        "tool": tool,
    }


def _assert_no_live_sidecars(path: str, *, operation: str) -> None:
    present = [suffix for suffix in _SIDECAR_SUFFIXES if os.path.exists(path + suffix)]
    if present:
        raise RuntimeError(f"refusing to {operation} while destination sidecars exist: {present}")


def backup_database(src: str, dst: str) -> dict[str, Any]:
    """创建已校验快照；构建失败时保留 ``dst`` 中上一份备份。"""
    source = _file_path(src, label="source")
    destination = _file_path(dst, label="destination")
    if _same_path(source, destination):
        raise ValueError("source and destination must be different files")
    if not os.path.isfile(source):
        raise FileNotFoundError(source)

    destination_directory = os.path.dirname(destination) or "."
    os.makedirs(destination_directory, exist_ok=True)
    _assert_no_live_sidecars(destination, operation="replace a backup")
    descriptor, temporary = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=".tmp", dir=destination_directory)
    os.close(descriptor)
    os.remove(temporary)
    try:
        _build_snapshot(source, temporary)
        metadata = _snapshot_metadata(temporary, source, tool="db_backup.v2")
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination_directory)
        _write_json_atomic(destination + _META_SUFFIX, metadata)
        return metadata
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def verify_backup(path: str, *, require_metadata: bool = True) -> dict[str, Any]:
    """校验数据库完整性、外键和 sidecar 清单；恢复路径强制要求清单。"""
    try:
        file_path = _file_path(path, label="backup")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    result: dict[str, Any] = {
        "ok": False,
        "integrity": None,
        "foreign_key_violations": None,
        "checksum_ok": None,
        "metadata_ok": None,
    }
    if not os.path.isfile(file_path):
        result["error"] = "missing"
        return result
    try:
        checks = _database_checks(file_path)
        result.update(checks)
    except (OSError, sqlite3.DatabaseError) as exc:
        result["error"] = str(exc)
        return result

    metadata_path = file_path + _META_SUFFIX
    if not os.path.isfile(metadata_path):
        if require_metadata:
            result["metadata_ok"] = False
            result["error"] = "metadata_missing"
    else:
        try:
            with open(metadata_path, encoding="utf-8") as handle:
                metadata = json.load(handle)
            checksum = str(metadata.get("checksum") or "")
            result["checksum_ok"] = bool(checksum) and hmac.compare_digest(_sha256(file_path), checksum)
            declared_page_count = metadata.get("page_count")
            result["page_count_ok"] = declared_page_count is None or declared_page_count == checks["page_count"]
            declared_page_size = metadata.get("page_size")
            result["page_size_ok"] = declared_page_size is None or declared_page_size == checks["page_size"]
            declared_revisions = metadata.get("alembic_revisions")
            result["alembic_revisions_ok"] = (
                declared_revisions is None or sorted(map(str, declared_revisions)) == checks["alembic_revisions"]
            )
            result["metadata_ok"] = all(
                result[key]
                for key in ("checksum_ok", "page_count_ok", "page_size_ok", "alembic_revisions_ok")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            result["metadata_ok"] = False
            result["error"] = f"metadata_invalid: {exc}"

    result["ok"] = (
        result["integrity"] == "ok"
        and result["foreign_key_violations"] == 0
        and result["metadata_ok"] is not False
    )
    return result


def _checkpoint_destination(path: str) -> None:
    """在替换前清空目标 WAL；有活动事务或无法证明安全时拒绝。"""
    sidecars = [path + suffix for suffix in _SIDECAR_SUFFIXES if os.path.exists(path + suffix)]
    try:
        connection = sqlite3.connect(path, timeout=0.2, isolation_level=None)
        try:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        if sidecars:
            raise RuntimeError("cannot checkpoint destination; stop all database users before restore") from exc
        return  # 损坏的无 WAL 目标仍可由已验证备份替换。
    if row and int(row[0]) != 0:
        raise RuntimeError("destination WAL is busy; stop all database users before restore")
    for sidecar in sidecars:
        if os.path.exists(sidecar):
            os.remove(sidecar)


def restore_database(backup_path: str, dst: str) -> dict[str, Any]:
    """从带校验清单的备份恢复；目标 WAL 忙时拒绝且不替换现库。"""
    backup_file = _file_path(backup_path, label="backup")
    destination = _file_path(dst, label="destination")
    if _same_path(backup_file, destination):
        raise ValueError("backup and destination must be different files")
    verification = verify_backup(backup_file, require_metadata=True)
    if not verification.get("ok"):
        raise ValueError(f"refusing to restore from unverified backup: {verification}")

    destination_directory = os.path.dirname(destination) or "."
    os.makedirs(destination_directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".sqlite-restore-", suffix=".tmp", dir=destination_directory)
    os.close(descriptor)
    os.remove(temporary)
    try:
        _build_snapshot(backup_file, temporary)
        restored_metadata = _snapshot_metadata(temporary, backup_file, tool="db_backup.restore.v2")
        restored_metadata["restored_at"] = _utcnow()
        _fsync_file(temporary)
        if os.path.exists(destination):
            _checkpoint_destination(destination)
        else:
            _assert_no_live_sidecars(destination, operation="restore")
        os.replace(temporary, destination)
        _fsync_directory(destination_directory)
        _write_json_atomic(destination + _META_SUFFIX, restored_metadata)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)

    post_restore = verify_backup(destination, require_metadata=True)
    if not post_restore.get("ok"):
        raise RuntimeError(f"restored database failed post-restore verification: {post_restore}")
    return {
        "restored": destination,
        "from": backup_file,
        "integrity": post_restore["integrity"],
        "foreign_key_violations": post_restore["foreign_key_violations"],
        "checksum": restored_metadata["checksum"],
    }


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _main(argv: list[str] | None = None) -> int:
    refuse_foreign_checkout("db_backup")
    parser = argparse.ArgumentParser(description="SQLite 一致性备份 / 校验 / 停机恢复")
    subcommands = parser.add_mutually_exclusive_group(required=True)
    subcommands.add_argument("--backup", nargs=2, metavar=("SRC", "DST"))
    subcommands.add_argument("--restore", nargs=2, metavar=("BACKUP", "DST"))
    subcommands.add_argument("--verify", metavar="PATH")
    args = parser.parse_args(argv)
    try:
        if args.backup:
            result = backup_database(*args.backup)
        elif args.restore:
            result = restore_database(*args.restore)
        else:
            result = verify_backup(args.verify)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI 边界统一转退出码
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
