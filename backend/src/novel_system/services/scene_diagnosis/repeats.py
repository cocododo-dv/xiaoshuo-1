"""跨场重复（2026-10-20）：全书已写的各场正文里，逐字反复出现的写法。

成稿中心「跨章」页签按短语列出（出现在哪几章哪几场），写作台深改把每一处做成一条 ``source = "repeat"`` 的发现
（钉到这一场里的那一段，可按诊断改写、可忽略）。不用模型，按各场现在的正文（写作台上那份，``context.diagnosis_text``）
现算，结果按正文的指纹记在进程里。

怎么算：

- 只看汉字连写的片段（标点、空白、引号都断开），作品人物与资料库条目的名字处也断开——「许望手指慢慢攥得发白」与
  「林昭手指慢慢攥得发白」是同一个写法；
- 两场之间按 5 字种子找最长的共同片段（:data:`SEED_CHARS`），再数每个片段在全书里逐字出现几次、在几场几章；
- 片段两头的半截虚词修掉（开头的「的得地了着」、结尾的「的地」——「的眼神一下子冷了下去」是「眼神一下子冷了下去」）；
- 算作写法重复的：至少两场；5–6 字的要出现 3 次以上，7 字以上两场各一次就算；要带描写性的虚词或补语
  （:data:`DESCRIPTIVE_MARKERS`：的地得着了过般像似如起出住开）——纯名词（道具、地名、文件名）反复出现是故事本身；带年月日、
  钟点、几个小时这类数目的（或连着两个以上数字）不算；被更长的重复片段完全包住、次数又不比它多的短片段不单列；
- 绑定了参考书时数一下这句在参考作者原文里出现几次：每十万字 ≥ :data:`HABIT_PER_100K` 次算「参考作者也常这么写」，
  降为提示。
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from novel_system.cache_registry import register_cache_reset

SEED_CHARS = 5
MIN_PHRASE_CHARS = 5
MAX_PHRASE_CHARS = 40
# 5–6 字的短片段要出现这么多次才算；7 字以上两场各一次就算
SHORT_PHRASE_MAX_CHARS = 6
SHORT_PHRASE_MIN_COUNT = 3
# 共同片段两头常是半截虚词（两处在这儿分岔）：开头去掉这些、结尾去掉那些
_LEADING_PARTICLES = "的得地了着"
_TRAILING_PARTICLES = "的地"
# 人物名常带说明（「林昭（档案修复师）」「许望/老许」）：括号前、分隔符前的那一截才是正文里出现的名字
_NAME_SPLIT_RE = re.compile(r"[（(【\[/／、，,·]")
# 一个 5 字种子在全书里出现太多次（多半是「的时候他」这类虚词串）就不拿它当种子：长的重复片段不会只由它构成
SEED_MAX_OCCURRENCES = 60
# 一份报告最多列这么多条（按「字数 × 次数」排）
MAX_PHRASES = 40
DESCRIPTIVE_MARKERS = frozenset("的地得着了过般像似如起出住开")
_NUMBER_RE = re.compile(
    r"[零〇一二三四五六七八九十百千万两几\d]+\s*(?:年|月|日|号|点|时|分钟|秒|天|夜|个小时|小时|岁|周|次|遍|米|公里)"
    r"|[零〇一二三四五六七八九十百千万两]{2,}|\d"
)
_CJK_RE = re.compile(r"[一-鿿]+")
# 参考作者每十万字用到这么多次以上：他的寻常说法，只作提示
HABIT_PER_100K = 0.5
EXCERPT_CONTEXT_CHARS = 12
REPEATS_CACHE_PROJECTS = 8


@dataclass(frozen=True)
class SceneText:
    """一场参与比对的正文：编辑器段落（与写作台同一套段号）。"""

    chapter_id: str
    chapter_number: int
    scene_id: str
    scene_number: int
    paragraphs: tuple[str, ...]


@dataclass(frozen=True)
class Occurrence:
    chapter_id: str
    chapter_number: int
    scene_id: str
    scene_number: int
    paragraph_index: int
    start: int
    excerpt: str


@dataclass(frozen=True)
class RepeatedPhrase:
    phrase: str
    occurrences: tuple[Occurrence, ...]
    reference_count: int | None = None
    habitual: bool = False

    @property
    def count(self) -> int:
        return len(self.occurrences)

    @property
    def scene_ids(self) -> list[str]:
        return list(dict.fromkeys(item.scene_id for item in self.occurrences))

    @property
    def chapter_ids(self) -> list[str]:
        return list(dict.fromkeys(item.chapter_id for item in self.occurrences))

    @property
    def signal_id(self) -> str:
        return repeat_signal_id(self.phrase)


@dataclass(frozen=True)
class RepeatReport:
    phrases: tuple[RepeatedPhrase, ...]
    scenes_with_text: int
    chapters_with_text: int
    chars: int
    reference_bound: bool = False
    by_scene: dict[str, list[RepeatedPhrase]] = field(default_factory=dict)


def repeat_signal_id(phrase: str) -> str:
    return "repeat:" + hashlib.sha1(phrase.encode("utf-8")).hexdigest()[:10]


def _segments(paragraph: str, names: Sequence[str]) -> list[tuple[int, str]]:
    """一段里的汉字连写片段（起点, 片段）；人物 / 条目名字处断开。"""
    out: list[tuple[int, str]] = []
    for match in _CJK_RE.finditer(paragraph):
        run, base = match.group(0), match.start()
        cuts = [(0, len(run))]
        for name in names:
            next_cuts: list[tuple[int, int]] = []
            for start, end in cuts:
                piece = run[start:end]
                cursor = 0
                while True:
                    at = piece.find(name, cursor)
                    if at < 0:
                        break
                    next_cuts.append((start + cursor, start + at))
                    cursor = at + len(name)
                next_cuts.append((start + cursor, end))
            cuts = next_cuts
        out.extend((base + start, run[start:end]) for start, end in cuts if end - start >= MIN_PHRASE_CHARS)
    return out


def name_forms(names: Iterable[str]) -> list[str]:
    """人物 / 条目名 → 正文里会出现的写法（去掉括号里的说明，按分隔符拆开），长的在前（先断长名）。"""
    forms: set[str] = set()
    for raw in names:
        text = str(raw or "").strip()
        if not text:
            continue
        forms.add(_NAME_SPLIT_RE.split(text, maxsplit=1)[0].strip())
        for part in re.split(r"[/／、，,]", re.sub(r"[（(【\[].*?[）)】\]]", "", text)):
            forms.add(part.strip())
    return sorted((form for form in forms if len(form) >= 2), key=lambda form: (-len(form), form))


def _trim(phrase: str) -> str:
    return phrase.lstrip(_LEADING_PARTICLES).rstrip(_TRAILING_PARTICLES)


def _is_writing(phrase: str) -> bool:
    return any(char in DESCRIPTIVE_MARKERS for char in phrase) and _NUMBER_RE.search(phrase) is None


def _maximal_pair_repeats(segments: Sequence[tuple[str, str]]) -> set[str]:
    """（场, 片段）之间、不在同一场的两处共同的最长片段（至少 :data:`SEED_CHARS` 字）。"""
    index: dict[str, list[tuple[int, int]]] = {}
    for seg_no, (_scene, text) in enumerate(segments):
        for pos in range(len(text) - SEED_CHARS + 1):
            index.setdefault(text[pos : pos + SEED_CHARS], []).append((seg_no, pos))
    found: set[str] = set()
    for spots in index.values():
        if len(spots) < 2 or len(spots) > SEED_MAX_OCCURRENCES:
            continue
        for i, (seg_a, pos_a) in enumerate(spots):
            text_a = segments[seg_a][1]
            for seg_b, pos_b in spots[i + 1 :]:
                if segments[seg_a][0] == segments[seg_b][0]:
                    continue
                text_b = segments[seg_b][1]
                # 只从左边伸不出去的那一对起算：同一个共同片段只算一次
                if pos_a > 0 and pos_b > 0 and text_a[pos_a - 1] == text_b[pos_b - 1]:
                    continue
                length = SEED_CHARS
                while pos_a + length < len(text_a) and pos_b + length < len(text_b) and text_a[pos_a + length] == text_b[pos_b + length]:
                    length += 1
                found.add(text_a[pos_a : pos_a + min(length, MAX_PHRASE_CHARS)])
    return found


def _occurrences(phrase: str, scenes: Sequence[SceneText], names: Sequence[str]) -> list[Occurrence]:
    out: list[Occurrence] = []
    for scene in scenes:
        for paragraph_index, paragraph in enumerate(scene.paragraphs):
            # 在名字断开的片段里找：「林昭手指……」里的「手指……」算一处，跨过名字拼起来的不算
            for base, segment in _segments(paragraph, names):
                cursor = 0
                while True:
                    at = segment.find(phrase, cursor)
                    if at < 0:
                        break
                    start = base + at
                    left = max(0, start - EXCERPT_CONTEXT_CHARS)
                    right = min(len(paragraph), start + len(phrase) + EXCERPT_CONTEXT_CHARS)
                    excerpt = ("…" if left > 0 else "") + paragraph[left:right] + ("…" if right < len(paragraph) else "")
                    out.append(
                        Occurrence(
                            chapter_id=scene.chapter_id,
                            chapter_number=scene.chapter_number,
                            scene_id=scene.scene_id,
                            scene_number=scene.scene_number,
                            paragraph_index=paragraph_index,
                            start=start,
                            excerpt=excerpt,
                        )
                    )
                    cursor = at + len(phrase)
    return out


def find_repeats(
    scenes: Sequence[SceneText],
    *,
    names: Iterable[str] = (),
    reference_counter: Callable[[str], int] | None = None,
    reference_chars: int = 0,
) -> RepeatReport:
    """全书各场的跨场重复（见模块说明）。``reference_counter(phrase)`` = 这句在参考书里出现几次（没绑定 → None）。"""
    name_list = name_forms(names)
    segments: list[tuple[str, str]] = [
        (scene.scene_id, segment)
        for scene in scenes
        for paragraph in scene.paragraphs
        for _base, segment in _segments(paragraph, name_list)
    ]
    candidates = {_trim(phrase) for phrase in _maximal_pair_repeats(segments)}
    kept: list[tuple[str, list[Occurrence]]] = []
    for phrase in sorted(candidates, key=len, reverse=True):
        if len(phrase) < MIN_PHRASE_CHARS or not _is_writing(phrase):
            continue
        occurrences = _occurrences(phrase, scenes, name_list)
        scene_count = len({item.scene_id for item in occurrences})
        if scene_count < 2:
            continue
        if len(phrase) <= SHORT_PHRASE_MAX_CHARS and len(occurrences) < SHORT_PHRASE_MIN_COUNT:
            continue
        # 被已收下的更长片段包住、次数又不比它多：不单列
        if any(phrase in longer and len(occurrences) <= len(longer_occ) for longer, longer_occ in kept):
            continue
        kept.append((phrase, occurrences))
    phrases: list[RepeatedPhrase] = []
    for phrase, occurrences in kept:
        reference_count = reference_counter(phrase) if reference_counter is not None else None
        habitual = bool(
            reference_count is not None and reference_chars > 0 and reference_count * 100_000 / reference_chars >= HABIT_PER_100K
        )
        phrases.append(RepeatedPhrase(phrase=phrase, occurrences=tuple(occurrences), reference_count=reference_count, habitual=habitual))
    phrases.sort(key=lambda item: (item.habitual, -len(item.phrase) * item.count, item.phrase))
    phrases = phrases[:MAX_PHRASES]
    by_scene: dict[str, list[RepeatedPhrase]] = {}
    for item in phrases:
        for scene_id in item.scene_ids:
            by_scene.setdefault(scene_id, []).append(item)
    return RepeatReport(
        phrases=tuple(phrases),
        scenes_with_text=sum(1 for scene in scenes if any(p.strip() for p in scene.paragraphs)),
        chapters_with_text=len({scene.chapter_id for scene in scenes if any(p.strip() for p in scene.paragraphs)}),
        chars=sum(len(p) for scene in scenes for p in scene.paragraphs),
        reference_bound=reference_counter is not None,
        by_scene=by_scene,
    )


class RepeatsCache:
    """全书的跨场重复按（作品, 各场正文指纹, 名字, 参考书版本）记在进程里：同一份正文第二次取就不再算。"""

    def __init__(self, *, maxsize: int = REPEATS_CACHE_PROJECTS) -> None:
        self._maxsize = maxsize
        self._items: OrderedDict[tuple[Any, ...], RepeatReport] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_build(self, key: tuple[Any, ...], build: Callable[[], RepeatReport]) -> RepeatReport:
        with self._lock:
            hit = self._items.get(key)
            if hit is not None:
                self._items.move_to_end(key)
                return hit
        report = build()
        with self._lock:
            self._items[key] = report
            self._items.move_to_end(key)
            while len(self._items) > self._maxsize:
                self._items.popitem(last=False)
        return report

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_REPEATS_CACHE = RepeatsCache()
register_cache_reset("scene_diagnosis.repeats", _REPEATS_CACHE.clear)


def cached_repeats(key: tuple[Any, ...], build: Callable[[], RepeatReport]) -> RepeatReport:
    return _REPEATS_CACHE.get_or_build(key, build)


class _ReferenceTextCache:
    """参考书的全文（数一句在参考作者原文里出现几次用），按（书、版本）只留最近一本：一本书一两百万字。"""

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._text = ""
        self._lock = threading.Lock()

    def get_or_build(self, key: tuple[Any, ...], build: Callable[[], str]) -> str:
        with self._lock:
            if self._key == key:
                return self._text
        text = build()
        with self._lock:
            self._key, self._text = key, text
        return text

    def clear(self) -> None:
        with self._lock:
            self._key, self._text = None, ""


_REFERENCE_TEXT = _ReferenceTextCache()
register_cache_reset("scene_diagnosis.repeats.reference_text", _REFERENCE_TEXT.clear)


def cached_reference_text(key: tuple[Any, ...], build: Callable[[], str]) -> str:
    return _REFERENCE_TEXT.get_or_build(key, build)


__all__ = [
    "DESCRIPTIVE_MARKERS",
    "HABIT_PER_100K",
    "MAX_PHRASES",
    "MIN_PHRASE_CHARS",
    "Occurrence",
    "RepeatReport",
    "RepeatedPhrase",
    "SceneText",
    "cached_reference_text",
    "cached_repeats",
    "find_repeats",
    "name_forms",
    "repeat_signal_id",
]
