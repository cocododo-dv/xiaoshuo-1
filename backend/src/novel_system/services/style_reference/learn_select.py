"""风格参考 v3 —「学习文风」的抽取窗口集（纯函数，叶子模块）。

旧抽取每个子维度各自随机抽 ~600 字的碎片（全书 3.3%，16 份互不相同的样本，写出来的陈述互相矛盾）。
v3 所有层读**同一组连续窗口**（约 12 窗 / 4 万字），从全书各处挑，覆盖作者写作的几种典型处境：

1. 分层配额（按顺序挑，每层挑到配额或没有候选为止）：章首 2 / 章末 2 / 对白多 2 / 叙述多 2 /
   心理 1 / 动作 1 / 描写 1，余下名额按典型度补满；
2. 每层里：优先**没用过的章**（一章至多一窗，候选不够才放宽）、优先**没覆盖到的全书分段**（把全书按窗号
   等分成目标窗数那么多段），再按作者自己窗口分布里的**典型度**（``windows.typicality``，越大越典型）排；
   在排名前三里按种子挑一个——同一本书（同一校验和）永远挑出同一组；
3. 字数：累计到目标字数就停，任何一窗都不让总数超过上限；整本书的窗口加起来不超过上限时全要。

结果按原书顺序排列。只读窗口的元数据（章号、位置、字数、对白占比、段型构成、典型度），不读正文。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

TARGET_WINDOWS = 12
TARGET_CHARS = 40_000
MAX_CHARS = 44_000
_TOP_PICK = 3

STRATUM_ALL = "all"
STRATUM_OPENING = "opening"
STRATUM_CLOSING = "closing"
STRATUM_DIALOGUE = "dialogue"
STRATUM_NARRATION = "narration"
STRATUM_PSYCHOLOGY = "psychology"
STRATUM_ACTION = "action"
STRATUM_DESCRIPTION = "description"
STRATUM_TYPICAL = "typical"
STRATUM_LABELS: dict[str, str] = {
    STRATUM_ALL: "全书",
    STRATUM_OPENING: "章首",
    STRATUM_CLOSING: "章末",
    STRATUM_DIALOGUE: "对白多",
    STRATUM_NARRATION: "叙述多",
    STRATUM_PSYCHOLOGY: "心理",
    STRATUM_ACTION: "动作",
    STRATUM_DESCRIPTION: "描写",
    STRATUM_TYPICAL: "典型",
}


@dataclass(frozen=True)
class WindowMeta:
    window_no: int
    start_index: int
    end_index: int
    chapter_no: int
    position: str
    chars: int
    dialogue_share: float
    typicality: float
    type_mix: Mapping[str, float] = field(default_factory=dict)

    def share(self, *types: str) -> float:
        return float(sum(float(self.type_mix.get(t, 0.0) or 0.0) for t in types))


@dataclass(frozen=True)
class Selection:
    windows: tuple[WindowMeta, ...]
    strata: Mapping[int, str]
    chars: int

    @property
    def window_nos(self) -> list[int]:
        return [w.window_no for w in self.windows]

    def to_cursor(self) -> dict[str, Any]:
        return {
            "windows": [
                {
                    "window_no": w.window_no,
                    "start": w.start_index,
                    "end": w.end_index,
                    "chapter": w.chapter_no,
                    "position": w.position,
                    "chars": w.chars,
                    "stratum": self.strata.get(w.window_no, STRATUM_TYPICAL),
                }
                for w in self.windows
            ],
            "chars": self.chars,
        }


def window_meta(row: Any) -> WindowMeta:
    """``StyleReferenceWindow`` 行（或同名键的 dict）→ ``WindowMeta``。"""

    def get(name: str, default: Any = None) -> Any:
        if isinstance(row, Mapping):
            return row.get(name, default)
        return getattr(row, name, default)

    mix = get("type_mix_json") or get("type_mix") or {}
    return WindowMeta(
        window_no=int(get("window_no") or 0),
        start_index=int(get("start_index") or 0),
        end_index=int(get("end_index") or 0),
        chapter_no=int(get("chapter_no") or 0),
        position=str(get("position") or "middle"),
        chars=int(get("chars") or 0),
        dialogue_share=float(get("dialogue_share") or 0.0),
        typicality=float(get("typicality") or 0.0),
        type_mix=dict(mix) if isinstance(mix, Mapping) else {},
    )


def _quantile(values: Sequence[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * ratio
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _strata(windows: Sequence[WindowMeta]) -> list[tuple[str, Callable[[WindowMeta], bool], int]]:
    dialogue = [w.dialogue_share for w in windows]
    q25, q75 = _quantile(dialogue, 0.25), _quantile(dialogue, 0.75)
    psych = _quantile([w.share("psychology") for w in windows], 0.9)
    action = _quantile([w.share("action") for w in windows], 0.9)
    description = _quantile([w.share("description_env", "description_char") for w in windows], 0.9)
    return [
        (STRATUM_OPENING, lambda w: w.position in ("opening", "whole"), 2),
        (STRATUM_CLOSING, lambda w: w.position in ("closing", "whole"), 2),
        (STRATUM_DIALOGUE, lambda w: w.dialogue_share >= q75 and w.dialogue_share > q25, 2),
        (STRATUM_NARRATION, lambda w: w.dialogue_share <= q25 and w.dialogue_share < q75, 2),
        (STRATUM_PSYCHOLOGY, lambda w: w.share("psychology") >= max(0.08, psych) > 0, 1),
        (STRATUM_ACTION, lambda w: w.share("action") >= max(0.08, action) > 0, 1),
        (STRATUM_DESCRIPTION, lambda w: w.share("description_env", "description_char") >= max(0.12, description) > 0, 1),
    ]


def select_extraction_windows(
    rows: Sequence[Any],
    *,
    seed: str,
    target_windows: int = TARGET_WINDOWS,
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
) -> Selection:
    """挑抽取窗口集（确定性：同样的窗口 + 同样的种子 → 同样的结果；见模块文档）。"""
    windows = sorted((w if isinstance(w, WindowMeta) else window_meta(w) for w in rows), key=lambda w: w.window_no)
    windows = [w for w in windows if w.chars > 0]
    if not windows:
        return Selection(windows=(), strata={}, chars=0)
    total = sum(w.chars for w in windows)
    if total <= max_chars:
        return Selection(windows=tuple(windows), strata={w.window_no: STRATUM_ALL for w in windows}, chars=total)

    rng = random.Random(f"style-reference-learn-select:{seed}")
    bands = max(1, int(target_windows))
    band_of = {w.window_no: min(bands - 1, index * bands // len(windows)) for index, w in enumerate(windows)}
    chosen: dict[int, WindowMeta] = {}
    strata: dict[int, str] = {}
    used_chapters: set[int] = set()
    covered_bands: set[int] = set()
    chars = 0

    def full() -> bool:
        return len(chosen) >= target_windows or chars >= target_chars

    def pick(label: str, predicate: Callable[[WindowMeta], bool], quota: int) -> None:
        nonlocal chars
        taken = 0
        while taken < quota and not full():
            candidates = [
                w
                for w in windows
                if w.window_no not in chosen and predicate(w) and chars + w.chars <= max_chars
            ]
            if not candidates:
                return
            fresh = [w for w in candidates if w.chapter_no not in used_chapters]
            pool = fresh or candidates
            pool.sort(
                key=lambda w: (
                    band_of[w.window_no] in covered_bands,  # 没覆盖到的全书分段在前
                    -w.typicality,  # 越典型越前
                    w.window_no,
                )
            )
            choice = pool[rng.randrange(min(_TOP_PICK, len(pool)))]
            chosen[choice.window_no] = choice
            strata[choice.window_no] = label
            used_chapters.add(choice.chapter_no)
            covered_bands.add(band_of[choice.window_no])
            chars += choice.chars
            taken += 1

    for label, predicate, quota in _strata(windows):
        if full():
            break
        pick(label, predicate, quota)
    if not full():
        pick(STRATUM_TYPICAL, lambda _w: True, target_windows)
    ordered = tuple(sorted(chosen.values(), key=lambda w: w.window_no))
    return Selection(windows=ordered, strata=strata, chars=chars)


__all__ = [
    "MAX_CHARS",
    "STRATUM_LABELS",
    "Selection",
    "TARGET_CHARS",
    "TARGET_WINDOWS",
    "WindowMeta",
    "select_extraction_windows",
    "window_meta",
]
