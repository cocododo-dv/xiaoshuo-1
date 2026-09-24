# -*- coding: utf-8 -*-
"""重新生成黄金语料 ingest 期望 JSON。

在隔离临时 SQLite 上跑真实 IngestService(启发式分类,无 LLM,结果确定),
把 stats_json 的关键面落盘到 expected/。corpus、分段规则或启发式分类**有意**
变更后运行本脚本并连同 expected 一起提交(2026-09-24 起不再有 ``metrics`` 块:
v2 指标包络随风格参考 v3 S2 删除)。

用法(backend 目录下):
    python tests/golden/style_reference/regen_expected.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent
BACKEND_DIR = GOLDEN_DIR.parents[2]
sys.path.insert(0, str(BACKEND_DIR / "src"))

CORPUS = {
    "luxun": GOLDEN_DIR / "corpus" / "luxun_short_stories.txt",
    "zhuziqing": GOLDEN_DIR / "corpus" / "zhuziqing_essays.txt",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["NOVEL_SYSTEM_DATABASE_URL"] = f"sqlite:///{tmp}/golden_regen.db"
        os.environ["NOVEL_SYSTEM_VECTOR_BACKEND"] = "memory"
        # ingest_path 只接受配置过的导入根目录;把黄金语料目录加进去(不覆盖调用方已设值)
        os.environ.setdefault(
            "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS", str(GOLDEN_DIR / "corpus")
        )

        from novel_system.db.models import Base
        from novel_system.db.session import SessionLocal, engine, reset_engine
        from novel_system.services.style_reference.ingest import IngestService

        reset_engine()
        Base.metadata.create_all(bind=engine())

        for name, path in CORPUS.items():
            with SessionLocal() as session:
                result = IngestService(session, llm_enabled=False).ingest_path(
                    path,
                    title=f"golden_{name}",
                    author_label=name,
                    cloud_policy="segments_only",
                    rights_declaration={
                        "analysis_rights": True,
                        "send_rights": True,
                    },
                )
                session.commit()
                stats = result.book.stats_json
                expected = {
                    "total_chars": result.book.total_chars,
                    "paragraphs_count": result.paragraphs_count,
                    "input_assessment": stats["input_assessment"],
                    "paragraph_type_distribution": stats["paragraph_type_distribution"],
                }
            out = GOLDEN_DIR / "expected" / f"{name}_ingest_expected.json"
            out.write_text(
                json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"=> {out.name}: total_chars={expected['total_chars']} "
                  f"paragraphs={expected['paragraphs_count']} "
                  f"assessment={expected['input_assessment']}")

        reset_engine()


if __name__ == "__main__":
    main()
