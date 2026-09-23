"""回归守卫：确认「演示数据 / 假生成」退役后不再回潮到产品运行时源码。

范围只覆盖本轮实际删除/中性化的东西——退役的两部演示作品（潮汐档案 / 盐镇来信）
的专有名词、被删的假生成客户端与演示种子模块、以及被下线的演示视图资产。

刻意不覆盖：
- 测试文件（`*.test.jsx` / `*_test.py` / `tests/`）—— 夹具用中性名，允许出现；
- 记账边界的 `offline_deterministic` 执行模式常量与 `OfflineDeterministicExecution`
  ABC —— 休眠的基础设施，不是演示数据。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 产品运行时源码根（不含 eval/qa 数据集与测试）
PRODUCT_ROOTS = (
    REPO_ROOT / "backend" / "src",
    REPO_ROOT / "frontend-react" / "src",
    REPO_ROOT / "frontend" / "src",
)
TEXT_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".css", ".yaml", ".yml", ".json"}


def _is_test_file(path: Path) -> bool:
    name = path.name
    return (
        name.endswith(".test.jsx")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or name.endswith(".spec.jsx")
        or name.startswith("test_")
        or ".test-helpers" in name
        or name == "test-helpers.js"
    )


def _product_files() -> list[Path]:
    out: list[Path] = []
    for root in PRODUCT_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            if "egg-info" in str(path):
                continue
            if _is_test_file(path):
                continue
            out.append(path)
    return out


# 退役的两部演示作品的专有名词（多字，避免与常用字误撞）。
RETIRED_STORY_MARKERS = (
    "潮汐档案",
    "盐镇来信",
    "林岑",
    "周岚",
    "阿恪",
    "苏怀梅",
    "顾老馆长",
    "盐钟残片",
    "周岚的钥匙",
    "三号档案箱",
    "返回的潮声",
    "夜班修复台",
    "回声讲堂",
    "第三潮汐",
    "潮汐城",
)

# 被删的假生成客户端 / 演示种子模块 / 演示视图门控符号。
RETIRED_CODE_SYMBOLS = (
    "seed_fe_demo_works",
    "OfflineNeutralClient",
    "OfflineStyleClient",
    "OfflineHardQcClient",
    "OfflineSoftQcClient",
    "OfflineSceneBlueprintClient",
    "OfflineWriterReviewClient",
    "OfflineWriterDeepReviewClient",
    "OfflinePassagePatchClient",
    "OfflineAuthorProposalClient",
    "OfflineAuthorStructureExtractClient",
    "OfflineNearFinalPlanningClient",
    "OfflineNearFinalAcceptanceClient",
    "offline_client_factory",
    "WsDemoTag",
)

# 本轮删除的运行时资产。
REMOVED_ASSETS = (
    "backend/src/novel_system/tools/seed_demo.py",
    "backend/src/novel_system/tools/seed_fe_demo_works.py",
    "backend/src/novel_system/tools/fe_demo_catalog.json",
    "backend/src/novel_system/tools/fe_demo_library.json",
    "frontend-react/src/ct-app.jsx",
    "frontend-react/src/lf2-data.jsx",
    "frontend-react/src/lf3-data.jsx",
    "frontend-react/src/lf6-app.jsx",
    "frontend-react/src/lf7-bridge.jsx",
    "frontend-react/scripts/export-demo-catalog.mjs",
)


def test_no_retired_story_markers_in_product_runtime() -> None:
    violations: list[str] = []
    for path in _product_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in RETIRED_STORY_MARKERS:
            if marker in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} :: {marker}")
    assert not violations, "退役演示作品的专有名词回潮到产品运行时源码：\n" + "\n".join(sorted(violations))


def test_no_retired_fake_generation_symbols_in_product_runtime() -> None:
    violations: list[str] = []
    for path in _product_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for symbol in RETIRED_CODE_SYMBOLS:
            if symbol in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} :: {symbol}")
    assert not violations, "退役的假生成 / 演示门控符号回潮到产品运行时源码：\n" + "\n".join(sorted(violations))


def test_removed_demo_assets_stay_removed() -> None:
    present = [rel for rel in REMOVED_ASSETS if (REPO_ROOT / rel).exists()]
    assert not present, "已退役的演示资产又出现了：\n" + "\n".join(present)


def test_dev_launcher_does_not_seed_demo() -> None:
    dev_ps1 = (REPO_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    assert "seed_demo" not in dev_ps1, "生产启动器 dev.ps1 不应再注入演示种子。"
    assert "skip-demo-seed" not in dev_ps1, "dev.ps1 不应再引用退役的 seed-skip 标记。"
