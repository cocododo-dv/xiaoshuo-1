"""文档守卫：入库的每一份 Markdown 的相对链接都指向存在的文件或目录；CLAUDE.md 与几份现行入口文档里用反引号
写出的文件路径都真的存在；CLAUDE.md 不再长回去（≤ 32 KB）。

2026-09-13 之前 README 与操作手册各有两条链接指向已删除的文档（长篇运行时契约、系统整改记录），文档导航还把已实施的
分章设计标成「待实施」。2026-09-29 的文档审计（X05-14）又在 CLAUDE.md 里查到引用了不存在的文件（一个早已搬走的命令行
入口、一个从没有过的服务模块）——那时这个守卫只看四份文档的链接，CLAUDE.md 有 158 KB，一半是日期化的工程日志。
CLAUDE.md 瘦身之后，日期化的内容在 docs/history/ 里原样存档；这里把链接、引用路径与体量三件事钉住。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# 没有 git 时（解包的源码）按这些模式找文档；有 git 时以 ``git ls-files`` 为准。
_FALLBACK_DOC_GLOBS = ("*.md", "docs/**/*.md", "frontend-react/*.md", "backend/tests/golden/**/*.md", ".github/*.md")


def _tracked_markdown() -> list[str]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"], cwd=REPO, capture_output=True, check=True, timeout=60
        ).stdout.decode("utf-8")
        names = sorted(name for name in listed.split("\0") if name)
    except (OSError, subprocess.SubprocessError):
        names = []
    if not names:
        found: set[str] = set()
        for pattern in _FALLBACK_DOC_GLOBS:
            found.update(path.relative_to(REPO).as_posix() for path in REPO.glob(pattern))
        names = sorted(found)
    return [name for name in names if (REPO / name).is_file()]


DOCS = _tracked_markdown()

# 链接目标：去掉 ``#锚点`` 之后按文档所在目录解析；外链、纯锚点与 mailto 不查。围栏代码块里的不是链接。
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_FENCE = re.compile(r"```.*?```", re.S)


def test_the_markdown_list_covers_the_entry_docs() -> None:
    for required in ("README.md", "CLAUDE.md", "docs/README.md", "docs/operator-manual.md", "frontend-react/README.md"):
        assert required in DOCS, f"文档清单漏了 {required}"


@pytest.mark.parametrize("doc", DOCS)
def test_relative_markdown_links_resolve(doc: str) -> None:
    path = REPO / doc
    missing = []
    for match in _LINK.finditer(_FENCE.sub("", path.read_text(encoding="utf-8"))):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        relative = target.split("#", 1)[0]
        if relative and not (path.parent / relative).resolve().exists():
            missing.append(target)
    assert not missing, f"{doc} 指向不存在的文件：{missing}"


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


# ---------------------------------------------------------------- 反引号里的文件路径

# 只查现行入口文档（日期化的契约与 docs/history/ 写的是当时的代码，本来就会提到后来删掉的文件）。
PATH_CHECKED_DOCS = (
    "CLAUDE.md",
    "README.md",
    "frontend-react/README.md",
    "docs/migrations.md",
    "docs/release-checklist.md",
)
# 写成相对路径的引用可以相对这些目录（CLAUDE.md 写 ``services/…``、前端 README 写 ``src/…``）。
_PATH_ROOTS = (
    "",
    "backend",
    "backend/src/novel_system",
    "backend/src/novel_system/services",
    "backend/src/novel_system/api",
    "backend/tests",
    "frontend-react",
    "frontend-react/src",
    "frontend-react/scripts",
    "config",
    "docs",
)
# 只写文件名（不带目录）的引用：在这些目录里有同名文件就算数。
_NAME_ROOTS = (
    "backend/src",
    "backend/tests",
    "backend/alembic",
    "backend/scripts",
    "frontend-react",
    "scripts",
    "config",
    "docs",
    ".github",
)
_PRUNED_DIRS = {"node_modules", "dist", "__pycache__", ".venv", ".venv-wsl", ".git", ".pytest_cache", ".test-results"}
_FILE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".css", ".json", ".yaml", ".yml", ".toml", ".lock", ".md", ".sh",
    ".ps1", ".cmd", ".txt", ".ini", ".html",
)
# 运行时才有、被 .gitignore 挡在仓库外的位置（启动脚本的 pid / 日志 / 密钥、E2E 的临时库、分片的 JUnit）。
_RUNTIME_ONLY_PREFIXES = (".codex-run", "backend/.test-results")
_BRACES = re.compile(r"([^\s{}]*)\{([^{}]*)\}([^\s{}]*)")
_SPLIT = re.compile(r"[\s,()|→·=]+")


def _expand_braces(text: str) -> str:
    """``bundle_{a,b}.py`` → ``bundle_a.py bundle_b.py``（只展开一层，够用）。"""
    while True:
        match = _BRACES.search(text)
        if match is None:
            return text
        prefix, alternatives, suffix = match.groups()
        expanded = " ".join(f"{prefix}{alt.strip()}{suffix}" for alt in alternatives.split(","))
        text = text[: match.start()] + expanded + text[match.end():]


def cited_paths(markdown: str) -> list[str]:
    """反引号里写出来的文件 / 目录路径（带已知后缀的，或以 ``/`` 结尾的）。占位写法（``<…>``、通配、省略号）不算。"""
    found: list[str] = []
    for span in re.findall(r"`([^`\n]+)`", _FENCE.sub("", markdown)):
        for token in _SPLIT.split(_expand_braces(span)):
            token = token.strip().strip("'\"").rstrip(".;").replace("\\", "/")
            if token.startswith("./"):
                token = token[2:]
            token = token.split(":", 1)[0] if ".py:" in token or ".js:" in token else token
            if not token or token.startswith(("http", "-", "$", "/", "#")):
                continue
            if any(mark in token for mark in "<>*?…{}~"):
                continue
            if token in _FILE_SUFFIXES:  # 「每一份 `.md`」这类说法，不是路径
                continue
            if token.endswith("/") or token.endswith(_FILE_SUFFIXES):
                found.append(token)
    return found


def _known_file_names() -> set[str]:
    names: set[str] = set()
    for root in _NAME_ROOTS:
        for directory, subdirs, files in os.walk(REPO / root):
            subdirs[:] = [name for name in subdirs if name not in _PRUNED_DIRS]
            names.update(files)
    names.update(path.name for path in REPO.iterdir() if path.is_file())
    return names


def _path_exists(token: str, file_names: set[str]) -> bool:
    if token.startswith(_RUNTIME_ONLY_PREFIXES):
        return True
    bare = token.rstrip("/")
    if "/" not in bare:
        return bare in file_names or any((REPO / root / bare).exists() for root in _PATH_ROOTS)
    return any((REPO / root / bare).exists() for root in _PATH_ROOTS)


def test_cited_path_extraction_finds_paths_and_skips_placeholders() -> None:
    sample = (
        "Run `scripts/start-all-linux.sh`, edit `snowflake_llm_{schema,sanitize}.py`, `.\\start-dev.cmd`, "
        "`db/schema_contract.py:CURRENT_SCHEMA_REVISION`, never `tests/<file>.py` or `.md` or `src/**/*.test.js`."
    )
    assert cited_paths(sample) == [
        "scripts/start-all-linux.sh",
        "snowflake_llm_schema.py",
        "snowflake_llm_sanitize.py",
        "start-dev.cmd",
        "db/schema_contract.py",
    ]


@pytest.mark.parametrize("doc", PATH_CHECKED_DOCS)
def test_paths_cited_in_current_entry_docs_exist(doc: str) -> None:
    file_names = _known_file_names()
    cited = cited_paths((REPO / doc).read_text(encoding="utf-8"))
    assert cited, f"{doc} 一个路径都没认出来：提取规则坏了"
    missing = sorted({token for token in cited if not _path_exists(token, file_names)})
    assert not missing, f"{doc} 引用了不存在的文件或目录（改了名就改文档，删了就删引用）：{missing}"


# ---------------------------------------------------------------- CLAUDE.md 的体量

# 只降不升。CLAUDE.md 每次会话都整份进上下文：只放现行的约定、命令、守卫与坑；
# 日期化的工程记录写进 docs/（现行契约）或 docs/history/（原样存档）。
CLAUDE_MD_MAX_BYTES = 32 * 1024


def test_claude_md_stays_within_its_size_budget() -> None:
    size = len((REPO / "CLAUDE.md").read_bytes())
    assert size <= CLAUDE_MD_MAX_BYTES, (
        f"CLAUDE.md 有 {size} 字节，超过 {CLAUDE_MD_MAX_BYTES}：把日期化的叙述挪进 docs/ 或 docs/history/，"
        "这里只留现行约定"
    )
