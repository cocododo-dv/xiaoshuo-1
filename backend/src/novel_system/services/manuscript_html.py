from __future__ import annotations

import re
from html import escape, unescape
from html.parser import HTMLParser


ALLOWED_MANUSCRIPT_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "strike",
        "blockquote",
        "ul",
        "ol",
        "li",
        "pre",
        "code",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "mark",
        "sub",
        "sup",
    }
)

_VOID_TAGS = frozenset({"br"})
_DROP_WITH_CONTENT = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "template",
        "noscript",
    }
)
_DROP_EMPTY = frozenset({"img", "audio", "video", "source", "track", "link", "meta", "base", "input"})


class _ManuscriptSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if self.suppressed:
            if normalized in _DROP_WITH_CONTENT:
                self.suppressed.append(normalized)
            return
        if normalized in _DROP_WITH_CONTENT:
            self.suppressed.append(normalized)
            return
        if normalized in _DROP_EMPTY:
            return
        if normalized in ALLOWED_MANUSCRIPT_TAGS:
            # The manuscript format does not need user-controlled attributes.
            # Removing all of them drops on* handlers, style URLs, ids and data-*.
            self.output.append(f"<{normalized}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.suppressed:
            return
        normalized = tag.lower()
        if normalized in ALLOWED_MANUSCRIPT_TAGS:
            self.output.append(f"<{normalized}>")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self.suppressed:
            if normalized == self.suppressed[-1]:
                self.suppressed.pop()
            return
        if normalized in ALLOWED_MANUSCRIPT_TAGS and normalized not in _VOID_TAGS:
            self.output.append(f"</{normalized}>")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.output.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.suppressed:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.suppressed:
            self.output.append(f"&#{name};")


def sanitize_manuscript_html(content: str | None) -> str:
    """Remove executable/remote-content markup from manuscript rich text."""

    if not content:
        return ""
    text = str(content)
    if "<" not in text:
        return text
    parser = _ManuscriptSanitizer()
    parser.feed(text)
    parser.close()
    return "".join(parser.output)


_PARAGRAPH_BLOCK_TAGS = frozenset({"p", "blockquote"})
_PARAGRAPH_LINE_BREAK_TAGS = frozenset({"br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre"})


class _ParagraphCollector(HTMLParser):
    """按写作台的 ``querySelectorAll("p, blockquote")`` 收集段落文字（含嵌套，文档序）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._open: list[list[str]] = []
        self._order: list[list[str]] = []
        self._loose: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in _PARAGRAPH_BLOCK_TAGS:
            buffer: list[str] = []
            self._open.append(buffer)
            self._order.append(buffer)
        elif name in _PARAGRAPH_LINE_BREAK_TAGS and not self._open:
            self._loose.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _PARAGRAPH_BLOCK_TAGS and self._open:
            self._open.pop()
        elif name in _PARAGRAPH_LINE_BREAK_TAGS and not self._open:
            self._loose.append("\n")

    def handle_data(self, data: str) -> None:
        if self._open:
            for buffer in self._open:
                buffer.append(data)
        else:
            self._loose.append(data)

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self.blocks = ["".join(buffer) for buffer in self._order]
        self.loose_text = "".join(self._loose)


def manuscript_paragraphs(content: str | None) -> list[str]:
    """作者稿 HTML → 段落文字列表，序号与写作台编辑器里的 ``p, blockquote`` 一一对应。

    纯文本（没有 ``<``）按空行 / 换行拆；HTML 里没有一个 p / blockquote 时按换行标签拆。
    """

    text = str(content or "")
    if not text.strip():
        return []
    if "<" not in text:
        return [line.strip() for line in re.split(r"\n+", text) if line.strip()]
    parser = _ParagraphCollector()
    parser.feed(text)
    parser.close()
    if parser.blocks:
        return parser.blocks
    loose = unescape(parser.loose_text)
    return [line.strip() for line in re.split(r"\n+", loose) if line.strip()]


def plain_manuscript_text(content: str | None) -> str:
    """作者稿（HTML 或纯文本）→ 一行可分析的可见文字：段落之间一个空格，没有标签。

    文学质量的 21 维规则、成稿门与场景诊断都按这份字算——过去规则直接吃作者稿的 HTML，
    「第一句」里带着 ``<p>``，同一条发现在两个页面里算出两个不同的 id。
    """

    return " ".join(part for part in manuscript_paragraphs(content) if part.strip())
