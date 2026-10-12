"""场景运行检查点的写端 golden（``tests/golden/scene_run_checkpoint/format_*.json``）。

现在的代码把一场跑完，逐次记下每一次检查点保存（节点、子游标、这一步新加的引用 / 哈希键）与归档后的整份检查点
JSON，规范化后与 golden 比。时间戳、随机 id、哈希值换成按出现顺序编号的占位符，结构、键、确定性的值与「哪两处是
同一个值」都保留。检查点格式有意变化时用 ``CHECKPOINT_FORMAT_GOLDEN_REGEN=1`` 重生成，并在提交里说明原因。

以前这里还有读端的整库转储（``resume_*.json``，重构前的代码 @60443dc 写下、灌回空库续跑）与它的
``dump_database`` / ``restore_database``：格式 v2 的「双读一版」已过，随旧格式读取器一起删了。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "scene_run_checkpoint"


def write_json(path: Path, payload: Any) -> None:
    """缩进写（改动时 diff 可读）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


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
        # 随机十六进制（uuid 片段、截断哈希）可能恰好全是数字，所以不看像不像随机：够长就换
        return _HEX_RUN.sub(lambda match: self._token("hex", match.group(0)), value)

    def value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(value[key]) for key in sorted(value, key=str)}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value
