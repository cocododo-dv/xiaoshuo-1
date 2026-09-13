"""文档失链守卫：根 README、文档导航、操作手册与雪花方法契约里的相对 .md 链接必须指向存在的文件。

2026-09-13 之前 README 与操作手册各有两条链接指向已删除的文档（长篇运行时契约、系统整改记录），
文档导航还把已实施的分章设计标成「待实施」。这里把它们钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "docs/README.md", "docs/operator-manual.md", "docs/snowflake-method-contract.md"]
_LINK = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


@pytest.mark.parametrize("doc", DOCS)
def test_relative_markdown_links_resolve(doc: str) -> None:
    path = REPO / doc
    missing = []
    for match in _LINK.finditer(path.read_text(encoding="utf-8")):
        target = match.group(1)
        if target.startswith("http"):
            continue
        if not (path.parent / target).resolve().exists():
            missing.append(target)
    assert not missing, f"{doc} 指向不存在的文档：{missing}"


def test_docs_index_and_manual_point_at_the_method_contract() -> None:
    index = (REPO / "docs/README.md").read_text(encoding="utf-8")
    assert "snowflake-method-contract.md" in index
    assert "待实施" not in index, "分章设计早已实施，导航不能再说待实施"
    manual = (REPO / "docs/operator-manual.md").read_text(encoding="utf-8")
    assert "snowflake-method-contract.md" in manual
    contract = (REPO / "docs/snowflake-method-contract.md").read_text(encoding="utf-8")
    assert "Ingermanson" in contract and "指南而非规则" in contract
    for step_key in ("book_brief", "one_sentence_summary", "one_paragraph_summary", "character_sheets", "short_synopsis",
                     "character_synopses", "long_synopsis", "character_bibles", "scene_list", "scene_details"):
        assert step_key in contract, f"契约文档漏了步骤 {step_key}"
