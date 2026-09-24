"""声音签名基线工具(2026-09-24 从 ``services/style_reference/voice_signature.py`` 的 ``__main__`` 搬来,行为不变)。

``config/style_reference/voice_baseline.yaml`` 是黄金语料(``backend/tests/golden/style_reference/corpus`` 全部公版
文本)按 1500 字块算出的每个测量核特征的 mean / std / p15 / p50 / p85;管线里只剩一处用途——``deliberate_repetition``
(叠词 / 短句连打 ≥ 基线字面 p85)。测量口径变了(``measure.KERNEL_VERSION``、分句、词表)就要重新生成,并在提交里说明。

用法(backend 目录下;在工作树里用 ``PYTHONPATH=<worktree>/backend/src`` 从中性目录跑,避免可编辑安装指向别的检出):
    python -m novel_system.tools.build_voice_baseline build-baseline                       # 写回仓库的 yaml
    python -m novel_system.tools.build_voice_baseline build-baseline --block-chars 1500 --corpus-dir DIR --output FILE
    python -m novel_system.tools.build_voice_baseline inspect PATH                         # 打印一个文本文件的签名与习惯句
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from novel_system.services.style_reference.text_utils import normalize_text, split_paragraphs
from novel_system.services.style_reference.voice_signature import (
    BASELINE_BLOCK_CHARS,
    FEATURE_NAMES,
    KERNEL_VERSION,
    TOP_WORD_GROUPS,
    VOICE_BASELINE_VERSION,
    VOICE_SIGNATURE_VERSION,
    compute_voice_signature,
    compute_voice_signature_for_text,
    quantile,
    render_voice_habits,
    round_stat,
)

COMMAND = "python -m novel_system.tools.build_voice_baseline build-baseline"


def _chunk_paragraphs(paragraphs: Sequence[str], block_chars: int) -> list[list[str]]:
    """按累计字数把段落切成 ≈block_chars 的块;残尾并入最后一块(与 metrics 一致)。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in paragraphs:
        current.append(paragraph)
        current_chars += len(paragraph)
        if current_chars >= block_chars:
            blocks.append(current)
            current = []
            current_chars = 0
    if current:
        if blocks:
            blocks[-1].extend(current)
        else:
            blocks.append(current)
    return blocks


def build_voice_baseline(
    corpus_dir: str | Path,
    *,
    block_chars: int = BASELINE_BLOCK_CHARS,
) -> dict[str, Any]:
    """用 corpus_dir 下全部 ``*.txt`` 按 block_chars 字块计算每个特征的 mean/std/p15/p50/p85。"""
    directory = Path(corpus_dir)
    files = sorted(path for path in directory.glob("*.txt") if path.is_file())
    if not files:
        raise FileNotFoundError(f"no *.txt corpus files under {directory}")
    block_features: list[Mapping[str, float]] = []
    all_paragraphs: list[str] = []
    corpus_records: list[dict[str, Any]] = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        normalized = normalize_text(raw)
        paragraphs = [body for _start, _end, body in split_paragraphs(normalized)]
        blocks = _chunk_paragraphs(paragraphs, block_chars)
        for block in blocks:
            block_features.append(compute_voice_signature(block, baseline={})["features"])
        all_paragraphs.extend(paragraphs)
        corpus_records.append(
            {
                "file": path.name,
                "chars": len(normalized),
                "paragraphs": len(paragraphs),
                "blocks": len(blocks),
                "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            }
        )
    corpus_signature = compute_voice_signature(all_paragraphs, baseline={})
    stats: dict[str, dict[str, float]] = {}
    for name in FEATURE_NAMES:
        values = [float(block.get(name, 0.0)) for block in block_features]
        stats[name] = {
            "mean": round_stat(statistics.fmean(values)),
            "std": round_stat(statistics.pstdev(values) if len(values) > 1 else 0.0),
            "p15": round_stat(quantile(values, 0.15)),
            "p50": round_stat(quantile(values, 0.50)),
            "p85": round_stat(quantile(values, 0.85)),
        }
    return {
        "version": VOICE_BASELINE_VERSION,
        "signature_version": VOICE_SIGNATURE_VERSION,
        "kernel_version": KERNEL_VERSION,
        "block_chars": int(block_chars),
        "block_count": len(block_features),
        "corpus": corpus_records,
        "features": stats,
        "top_words": corpus_signature["top_words"],
    }


def render_voice_baseline_yaml(baseline: Mapping[str, Any], *, command: str, generated_at: str) -> str:
    """把基线写成带来源注释的 YAML(手工排版,保证稳定与可读)。"""
    lines = [
        "# 声音签名基线(voice_signature.py 消费,勿手改数值)。",
        "# 用 backend/tests/golden/style_reference/corpus 全部公版文本(鲁迅短篇 + 朱自清散文;",
        "# luxun_kongyiji / zhuziqing_essays 与主集有重叠,照收)按测量核口径计算:",
        f"# 按 {baseline['block_chars']} 字块切分,对每个特征取块间 mean / std / p15 / p50 / p85。",
        "# 用途只剩两处:deliberate_repetition(叠词 / 短句连打 ≥ 字面 p85)与旧 z 值接口;",
        "# 习惯句不再与它比较,「像不像作者」看作者自己的窗口分布(fidelity.py)。",
        "# 测量口径(measure.KERNEL_VERSION)变了就重新生成(backend 目录下):",
        f"#   {command}",
        f"# generated_at: {generated_at}",
        f"version: {baseline['version']}",
        f"signature_version: {baseline['signature_version']}",
        f"kernel_version: {baseline.get('kernel_version', KERNEL_VERSION)}",
        f"block_chars: {baseline['block_chars']}",
        f"block_count: {baseline['block_count']}",
        "corpus:",
    ]
    for record in baseline["corpus"]:
        lines.append(
            f"  - {{file: {json.dumps(record['file'], ensure_ascii=False)}, chars: {record['chars']}, "
            f"paragraphs: {record['paragraphs']}, blocks: {record['blocks']}, "
            f"normalized_sha256: {record['normalized_sha256']}}}"
        )
    lines.append("features:")
    for name in FEATURE_NAMES:
        entry = baseline["features"][name]
        lines.append(
            f"  {name}: {{mean: {entry['mean']}, std: {entry['std']}, "
            f"p15: {entry['p15']}, p50: {entry['p50']}, p85: {entry['p85']}}}"
        )
    lines.append("top_words:")
    for group in TOP_WORD_GROUPS:
        entries = baseline["top_words"].get(group) or []
        rendered = ", ".join(
            f"[{json.dumps(str(word), ensure_ascii=False)}, {round_stat(share)}]" for word, share in entries
        )
        lines.append(f"  {group}: [{rendered}]")
    return "\n".join(lines) + "\n"


def default_corpus_dir() -> Path:
    """``backend/tests/golden/style_reference/corpus``(本文件在 ``backend/src/novel_system/tools/``)。"""
    return Path(__file__).resolve().parents[3] / "tests" / "golden" / "style_reference" / "corpus"


def default_baseline_path() -> Path:
    """仓库根 ``config/style_reference/voice_baseline.yaml``。"""
    return Path(__file__).resolve().parents[4] / "config" / "style_reference" / "voice_baseline.yaml"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m novel_system.tools.build_voice_baseline",
        description="声音签名工具:生成基线 / 检视单个文本的签名与习惯句。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-baseline", help="用黄金语料生成 voice_baseline.yaml")
    build.add_argument("--corpus-dir", default=str(default_corpus_dir()))
    build.add_argument("--output", default=str(default_baseline_path()))
    build.add_argument("--block-chars", type=int, default=BASELINE_BLOCK_CHARS)
    inspect = sub.add_parser("inspect", help="打印一个文本文件的签名与习惯句")
    inspect.add_argument("path")
    args = parser.parse_args(argv)

    if args.command == "build-baseline":
        import datetime

        baseline = build_voice_baseline(args.corpus_dir, block_chars=args.block_chars)
        command = f"{COMMAND} --block-chars {args.block_chars}"
        generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        output = Path(args.output)
        output.write_text(
            render_voice_baseline_yaml(baseline, command=command, generated_at=generated_at),
            encoding="utf-8",
        )
        sys.stdout.write(f"=> {output} ({baseline['block_count']} blocks, {len(FEATURE_NAMES)} features)\n")
        return 0
    if args.command == "inspect":
        text = Path(args.path).read_text(encoding="utf-8")
        signature = compute_voice_signature_for_text(text)
        sys.stdout.write(json.dumps(signature, ensure_ascii=False, indent=2) + "\n")
        for line in render_voice_habits(signature):
            sys.stdout.write(f"- {line}\n")
        return 0
    return 2


__all__ = [
    "COMMAND",
    "build_voice_baseline",
    "default_baseline_path",
    "default_corpus_dir",
    "main",
    "render_voice_baseline_yaml",
]


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
