"""本地私有语料验证通道(不入仓库的端到端检验)。

用户想拿自己的书(含受版权保护的当代小说,如《龙族》)验证摄取/统计/
抄袭检测时,把环境变量指到本地 TXT(UTF-8 或 GB18030)后单独跑本文件:

    $env:NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS = "C:\\path\\to\\本地参考书.txt"
    python -m pytest tests/test_style_reference_local_corpus.py -v

未设变量时整文件自动跳过;书的内容只进当次测试的临时库,永不入仓库。
(黄金语料红线见 tests/golden/style_reference/README.md——受版权 IP
不允许提交,本通道就是为它们准备的合规出口。)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.reference_copy_gate import (
    check_reference_copy,
    reset_reference_copy_gate_cache,
)
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.repository import StyleReferenceRepository

LOCAL_CORPUS = os.environ.get("NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS", "").strip()

pytestmark = pytest.mark.skipif(
    not LOCAL_CORPUS or not Path(LOCAL_CORPUS).exists(),
    reason="NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS 未设置或文件不存在(本地私有语料通道,可选)",
)


@pytest.fixture(autouse=True)
def _clear_corpus_cache():
    reset_reference_copy_gate_cache()
    yield
    reset_reference_copy_gate_cache()


@pytest.fixture(scope="function")
def local_book():
    """摄取本地书(启发式分类,无 LLM),返回 (book_id, stats, paragraphs_count)。"""
    with SessionLocal() as session:
        result = IngestService(session, llm_enabled=False).ingest_path(
            Path(LOCAL_CORPUS),
            title="本地验证语料",
            author_label=None,
            cloud_policy="local_only",  # 私有书默认最严策略
        )
        session.commit()
        return result.book.book_id, dict(result.book.stats_json), result.paragraphs_count


def test_local_corpus_ingests_with_full_stats(local_book):
    book_id, stats, paragraphs_count = local_book
    assert paragraphs_count > 0
    assert len(stats["metrics"]) == 21
    for m in stats["metrics"].values():
        assert "mean" in m and "std" in m
    assert set(stats["input_assessment"]) == {"language", "narrative", "scene", "theme"}
    assert stats["paragraph_type_distribution"]
    print(
        f"\n本地语料统计:段落 {paragraphs_count},"
        f" input_assessment={stats['input_assessment']},"
        f" 平均句长 {stats['metrics']['avg_sentence_length']['mean']:.1f}"
    )


def test_local_corpus_self_plagiarism_detected(local_book):
    """从本地书正文中段抄一句 → 唯一抄袭门(全书语料 + 规范化)必须拦下。

    (2026-09-23 风格参考 v3 P5b:旧同步回测删除,改走 reference_copy_gate。)
    """
    book_id, _stats, _count = local_book
    with SessionLocal() as session:
        paragraphs = StyleReferenceRepository(session).list_paragraphs(book_id)
        # 取书中部一个 ≥40 字的段落,截 40 字模拟"微改抄袭"(插空格)
        source = next(
            p.text for p in paragraphs[len(paragraphs) // 2 :] if len(p.text) >= 40
        )
        copied = source[:20] + " " + source[20:40] + "——这后半句是我自己写的全新内容。"
        check = check_reference_copy(session, copied, book_ids=[book_id])
    assert check.blocked, "抄原书原文必须被拦下"
    assert check.hits


def test_local_corpus_original_text_passes(local_book):
    book_id, _stats, _count = local_book
    original = "深夜的服务器机房里只有风扇的嗡鸣，他盯着滚动的日志，手边的咖啡早凉透了。"
    with SessionLocal() as session:
        check = check_reference_copy(session, original, book_ids=[book_id])
    assert not check.hits
