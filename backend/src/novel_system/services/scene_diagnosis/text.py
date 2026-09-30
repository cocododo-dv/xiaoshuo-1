"""诊断看的正文与定位：``DiagnosisText``（写作台看到的那一份，按编辑器的 ``p, blockquote`` 拆段）、把一句话钉到
段落上、局部深评看的范围（叶子：只依赖文本助手）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from novel_system.services.hash_engine import sha256_text
from novel_system.services.style_reference.text_utils import compact_ws

# 局部深评：整场不超过这个字数就全文给模型（焦点段与前后段标出），再长的远段只留开头
PASSAGE_SCENE_FULL_CHARS = 12000
PASSAGE_FAR_PARAGRAPH_HEAD = 40
_WS_RE = re.compile(r"\s+")


@dataclass
class DiagnosisText:
    """一场的诊断正文。建好之后不再改（``paragraphs`` 不追加、不替换），所以可见文字与哈希只算一次。

    ``layer``：``author_draft`` / ``runtime_final_scene``，或 ``none``——没有可见文字（空白作者稿也是，见
    ``context.diagnosis_text``）；各处「有没有正文」都只看 ``layer == "none"`` 这一个口径。"""

    layer: str
    ref: str | None
    content: str
    paragraphs: list[str] = field(default_factory=list)
    updated_at: str | None = None

    @cached_property
    def plain(self) -> str:
        return " ".join(paragraph for paragraph in self.paragraphs if paragraph.strip())

    @cached_property
    def sha256(self) -> str:
        # 章级通读把各场的这个哈希记进评审行（contract_field_refs_json.scenes），口径不能变
        return sha256_text(self.plain)

    @cached_property
    def paragraphs_sha256(self) -> str:
        """分段的指纹：可见文字相同、段落分界不同（段号会变）时不同。"""
        return sha256_text("\x1e".join(self.paragraphs))

    @property
    def chars(self) -> int:
        return len(_WS_RE.sub("", self.plain))

    def compact(self) -> str:
        return compact_ws(self.plain)


def locate_in_paragraphs(paragraphs: list[str], needle: str) -> dict[str, Any] | None:
    """在段落列表里钉住 ``needle``：先原样找（偏移可用），再按压缩空白找（只到段）。"""

    exact = str(needle or "").strip()
    if not exact:
        return None
    for index, paragraph in enumerate(paragraphs):
        at = paragraph.find(exact)
        if at >= 0:
            return {"excerpt": exact, "paragraph_index": index, "start": at, "end": at + len(exact)}
    compact_needle = compact_ws(exact)
    if not compact_needle:
        return None
    for index, paragraph in enumerate(paragraphs):
        if compact_needle in compact_ws(paragraph):
            return {"excerpt": exact, "paragraph_index": index, "start": None, "end": None}
    return None


def _fragments(evidence: str) -> list[str]:
    raw = str(evidence or "").strip()
    if not raw:
        return []
    parts = [raw, *[part.strip() for part in raw.split(" / ") if part.strip()]]
    fragments: list[str] = []
    for part in parts:
        for candidate in (part, part.strip(" .。!?！？,，;；")):
            if candidate and candidate not in fragments:
                fragments.append(candidate)
    return sorted(fragments, key=len, reverse=True)


def locate(paragraphs: list[str], *, needle: str = "", excerpt: str = "", anchor: str = "text") -> dict[str, Any] | None:
    if anchor == "scene":
        return None
    if anchor == "ending" and paragraphs:
        last = len(paragraphs) - 1
        hit = locate_in_paragraphs([paragraphs[last]], needle) if needle else None
        if hit:
            hit["paragraph_index"] = last
            return hit
        return {"excerpt": paragraphs[last][-60:], "paragraph_index": last, "start": None, "end": None}
    hit = locate_in_paragraphs(paragraphs, needle) if needle else None
    if hit:
        return hit
    for fragment in _fragments(excerpt):
        hit = locate_in_paragraphs(paragraphs, fragment)
        if hit:
            return hit
        # 证据窗口跨了段：取它前 24 个字再钉一次
        head = fragment[:24]
        if len(head) >= 8:
            hit = locate_in_paragraphs(paragraphs, head)
            if hit:
                return hit
    return None


def passage_scope(
    paragraphs: list[str],
    focus: list[int],
    *,
    context: int = 1,
    full_chars: int = PASSAGE_SCENE_FULL_CHARS,
) -> dict[str, Any]:
    """局部深评看的范围：焦点段（一段、一段范围或几条发现所在的段）标 【焦点段 N】，前后各 ``context``
    段标 【上下文 N】，其余段按 【第 N 段】 全文给出——整场不超过 ``full_chars`` 字时模型看得到全场，
    跨段的矛盾就能对上；再长的远段只留开头（【第 N 段·略】），至少知道前后发生了什么。"""

    total = len(paragraphs)
    focus_set = sorted({int(index) for index in focus if 0 <= int(index) < total})
    near = {
        neighbour
        for index in focus_set
        for neighbour in range(index - context, index + context + 1)
        if 0 <= neighbour < total and neighbour not in focus_set
    }
    whole_scene = sum(len(paragraph) for paragraph in paragraphs) <= full_chars
    lines: list[str] = []
    abbreviated = 0
    for index, paragraph in enumerate(paragraphs):
        if index in focus_set:
            lines.append(f"【焦点段 {index + 1}】{paragraph}")
        elif index in near:
            lines.append(f"【上下文 {index + 1}】{paragraph}")
        elif whole_scene:
            lines.append(f"【第 {index + 1} 段】{paragraph}")
        else:
            head = paragraph[:PASSAGE_FAR_PARAGRAPH_HEAD]
            abbreviated += 1
            lines.append(f"【第 {index + 1} 段·略】{head}{'……' if len(paragraph) > len(head) else ''}")
    shown = sorted(set(focus_set) | near)
    return {
        "focus": focus_set,
        "start": shown[0] if shown else 0,
        "end": shown[-1] if shown else 0,
        "whole_scene": whole_scene,
        "abbreviated": abbreviated,
        "focus_text": "\n\n".join(paragraphs[index] for index in focus_set),
        "text": "\n\n".join(lines),
    }
