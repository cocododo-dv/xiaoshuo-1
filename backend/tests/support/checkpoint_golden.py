"""场景运行检查点的 golden：旧代码写下的库状态（读端兼容）与检查点写端的规范化形状（写端不漂移）。

两种 golden，都在 ``tests/golden/scene_run_checkpoint/``：

- **读端（``resume_*.json``）**：重构之前的代码（``refactor/2026-09-29`` @ 60443dc，P01c 开工前）把一场跑到某个
  检查点停下，把整库的行原样倒出来。测试把这些行灌进空库，用现在的代码按同一个执行 id 续跑——这证明现在的读端
  认旧代码写下的检查点（部署时正停在半路的运行接得上）。**它们不许重生成**：重生成就变成「新代码写、新代码读」，
  失去意义。新增一个场景只能从一个已知的旧版本捕获，并在提交里写明版本。
- **写端（``format_*.json``）**：现在的代码把一场跑完，逐次记下每一次检查点保存（节点、子游标、这一步新加的引用
  / 哈希键）与归档后的整份检查点 JSON，规范化后与 golden 比。时间戳、随机 id、哈希值换成按出现顺序编号的占位符，
  结构、键、确定性的值与「哪两处是同一个值」都保留。检查点格式有意变化时用 ``CHECKPOINT_FORMAT_GOLDEN_REGEN=1``
  重生成，并在提交里说明原因。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.base import Base

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "scene_run_checkpoint"


def dump_database(session: Session) -> dict[str, list[dict[str, Any]]]:
    """整库非空表的全部行（按主键排序；列值原样——时间戳在本库里是字符串，JSON 列是对象）。"""
    session.flush()
    rows: dict[str, list[dict[str, Any]]] = {}
    for table in Base.metadata.sorted_tables:
        ordered = list(table.primary_key.columns) or list(table.columns)
        found = session.execute(select(table).order_by(*ordered)).mappings().all()
        if found:
            rows[table.name] = [dict(row) for row in found]
    return rows


def restore_database(session: Session, rows: dict[str, list[dict[str, Any]]]) -> None:
    """把 :func:`dump_database` 倒出的行灌回空库（一个事务；外键检查推迟到提交）。

    只写现在的表结构里还有的表和列：以后的迁移删掉的列 / 表照样读得回来，新加的列取模型默认值。
    """
    for table in Base.metadata.sorted_tables:
        found = rows.get(table.name)
        if not found:
            continue
        columns = {column.name for column in table.columns}
        session.execute(
            table.insert(),
            [{key: value for key, value in row.items() if key in columns} for row in found],
        )
    session.commit()


def write_json(path: Path, payload: Any, *, compact: bool = False) -> None:
    """``compact``：整库转储写成一行（读端 golden 只由机器读、永不重生成）；写端 golden 缩进写，改动时 diff 可读。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if compact
        else json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    )
    path.write_text(text + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# 规范化：随运行而变的值换成占位符（同一个值同一个编号，按排序后的遍历顺序第一次出现编号）。
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"  # ISO（utcnow）
    r"|\d{8}T\d{6,}Z?"  # 紧凑写法（行 id 里嵌的时刻）
)
_HEX_RUN = re.compile(r"(?<![0-9a-f])[0-9a-f]{8,}(?![0-9a-f])")


class Canonicalizer:
    """把一次运行里随机 / 随时间变化的值（时间戳、uuid 片段、哈希）换成稳定的占位符。"""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def _token(self, kind: str, value: str) -> str:
        key = f"{kind}:{value}"
        if key not in self._tokens:
            self._tokens[key] = f"<{kind}#{sum(1 for k in self._tokens if k.startswith(kind + ':')) + 1}>"
        return self._tokens[key]

    def text(self, value: str) -> str:
        value = _TIMESTAMP.sub(lambda match: self._token("ts", match.group(0)), value)
        return _HEX_RUN.sub(
            lambda match: self._token("hex", match.group(0)) if _looks_random(match.group(0)) else match.group(0),
            value,
        )

    def value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(value[key]) for key in sorted(value, key=str)}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _looks_random(token: str) -> bool:
    return any(char.isdigit() for char in token) and any(char.isalpha() for char in token)
