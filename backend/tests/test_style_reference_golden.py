"""黄金语料端到端回归(《手册》§9.2,2026-06 落地)。

语料:公版鲁迅短篇(主力 ~66k 字)+ 朱自清散文(对照 ~8k 字)+
单篇孔乙己(下限,全层 skip)。expected/ 由
`tests/golden/style_reference/regen_expected.py` 在真实 ingest 管线上生成,
本文件断言管线输出与 expected 完全一致——metrics / 分段 / 分类启发式的
任何无意变更都会在这里现形。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.reference_copy_gate import (
    check_reference_copy,
    reset_reference_copy_gate_cache,
)
from novel_system.services.style_reference.fidelity import (
    DEFAULT_MAX_PERCENTILE,
    clear_reference_cache,
    read_fidelity,
    reference_distribution_for_book,
    within_author_range,
)

GOLDEN = Path(__file__).resolve().parent / "golden" / "style_reference"
CORPUS = GOLDEN / "corpus"
EXPECTED = GOLDEN / "expected"


@pytest.fixture(autouse=True)
def _clear_corpus_cache():
    reset_reference_copy_gate_cache()
    clear_reference_cache()
    yield
    reset_reference_copy_gate_cache()
    clear_reference_cache()


def _ingest(path: Path, title: str) -> tuple[str, dict, int]:
    with SessionLocal() as session:
        result = IngestService(session, llm_enabled=False).ingest_path(
            path,
            title=title,
            author_label=None,
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        session.commit()
        return result.book.book_id, dict(result.book.stats_json), result.paragraphs_count


# ---------------------------------------------------------------------------
# 语料完整性 + IP 关键词守卫
# ---------------------------------------------------------------------------


def test_corpus_files_present_and_sized():
    luxun = (CORPUS / "luxun_short_stories.txt").read_text(encoding="utf-8")
    zhu = (CORPUS / "zhuziqing_essays.txt").read_text(encoding="utf-8")
    kong = (CORPUS / "luxun_kongyiji.txt").read_text(encoding="utf-8")
    assert len(luxun) >= 60000, "主力集应 ≥6 万字(language/scene 层 high)"
    assert len(zhu) >= 6000
    assert len(kong) < 10000, "下限集必须低于最低 skip 门槛"
    for piece in ("《狂人日记》", "《阿Q正传》", "《祝福》"):
        assert piece in luxun
    assert "《背影》" in zhu


def test_golden_dir_has_no_contemporary_ip_keywords():
    """§9.2 红线:黄金目录严禁当代受版权 IP 关键词(高特异性词表;
    「江南」为通用地名,在公版文本中合法出现,不入守卫)。"""
    banned = ("龙族", "路明非", "路鸣泽", "楚子航")
    for path in GOLDEN.rglob("*"):
        if not path.is_file() or path.suffix not in (".txt", ".json", ".md", ".py"):
            continue
        text = path.read_text(encoding="utf-8")
        for kw in banned:
            assert kw not in text, f"{path.name} 含禁用 IP 关键词 {kw!r}"


# ---------------------------------------------------------------------------
# ingest 输出回归(与 expected 严格对齐)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,fname", [
    ("luxun", "luxun_short_stories.txt"),
    ("zhuziqing", "zhuziqing_essays.txt"),
])
def test_ingest_matches_expected(name: str, fname: str):
    expected = json.loads((EXPECTED / f"{name}_ingest_expected.json").read_text(encoding="utf-8"))
    _book_id, stats, paragraphs_count = _ingest(CORPUS / fname, f"golden_{name}")

    assert paragraphs_count == expected["paragraphs_count"]
    assert stats["input_assessment"] == expected["input_assessment"]
    assert stats["paragraph_type_distribution"] == pytest.approx(
        expected["paragraph_type_distribution"], rel=1e-9
    )
    assert set(stats["metrics"]) == set(expected["metrics"])
    for metric, exp in expected["metrics"].items():
        got = stats["metrics"][metric]
        assert got["mean"] == pytest.approx(exp["mean"], rel=1e-9), metric
        assert got["std"] == pytest.approx(exp["std"], rel=1e-9), metric
        assert got["sample_count"] == exp["sample_count"]


def test_kongyiji_assesses_all_skip():
    _book_id, stats, _count = _ingest(CORPUS / "luxun_kongyiji.txt", "golden_kongyiji")
    assert stats["input_assessment"] == {
        "language": "skip", "narrative": "skip", "scene": "skip", "theme": "skip",
    }


# ---------------------------------------------------------------------------
# 抄袭门 + 读数闭环(2026-09-23 风格参考 v3 P5b:旧校验层的同步裁决 / 量化回测删除,
# 抄袭检出走唯一抄袭门 reference_copy_gate,「像不像」走读数 fidelity)
# ---------------------------------------------------------------------------


def test_copying_corpus_passage_is_blocked_by_the_copy_gate():
    """从原书任意段抄 ≥12 字(含标点微改)必须被拦——全书语料检测网。"""
    book_id, _stats, _count = _ingest(CORPUS / "luxun_short_stories.txt", "golden_plag")
    # 《故乡》名句,刻意改标点 + 插空格模拟微改抄袭
    copied = "其实地上本没有路，走的人多了、也便 成了路。这是我自己续写的一句。"
    with SessionLocal() as session:
        check = check_reference_copy(session, copied, book_ids=[book_id])
    assert check.blocked and check.hits


def test_original_text_passes_the_copy_gate():
    book_id, _stats, _count = _ingest(CORPUS / "zhuziqing_essays.txt", "golden_orig")
    original = "码头上的起重机缓缓转动，集装箱在暮色里排成沉默的方阵，无人机的航灯一闪一闪。"
    with SessionLocal() as session:
        check = check_reference_copy(session, original, book_ids=[book_id])
    assert not check.blocked and not check.hits


FLOWERY_SAMPLE = (
    "那一袭流光溢彩宛若九天银河倾泻而下的月色温柔地漫过雕梁画栋的飞檐翘角并悄然浸润了"
    "庭院深处每一寸被岁月摩挲得温润如玉的青石板让整个世界都沉醉在这无边无际的瑰丽与"
    "缱绻交织而成的梦幻里仿佛连时间也为之屏住了呼吸。"
    "而她那双仿佛盛满了星辰大海与万千风华的眼眸在这流转的光影中漾起了一圈又一圈令人"
    "心醉神迷难以自拔的涟漪仿佛要将所有凝望过它的灵魂都温柔地溺毙在这片深不见底的"
    "璀璨与温柔交相辉映的湖泊之中再也不愿醒来。"
)


def test_flowery_sample_reads_far_outside_the_reference():
    """伪华丽腔(堆砌长句)对鲁迅全书窗口分布的读数:句式维越界(句子 / 分句偏长),不在作者范围内,
    且比鲁迅自己的一段原文离作者远得多。"""
    corpus = (CORPUS / "luxun_short_stories.txt").read_text(encoding="utf-8")
    book_id, _stats, _count = _ingest(CORPUS / "luxun_short_stories.txt", "golden_flowery")
    lines = [line for line in corpus.splitlines() if line.strip()]
    middle = len(lines) // 2
    own_passage = "\n".join(lines[middle : middle + 40])[:1500]
    with SessionLocal() as session:
        dist = reference_distribution_for_book(session, book_id)
    assert dist is not None and dist.window_count >= 8
    flowery = read_fidelity(FLOWERY_SAMPLE, dist)
    own = read_fidelity(own_passage, dist)
    too_long = {
        item["feature"]
        for item in flowery.out_of_band
        if item["dimension"] == "language.sentence_structure" and item["direction"] == "high"
    }
    assert too_long & {"sent_len_mean", "clause_len_mean"}, flowery.out_of_band
    assert flowery.percentile > DEFAULT_MAX_PERCENTILE
    assert not within_author_range(flowery)
    assert flowery.distance > own.distance * 1.5, (flowery.distance, own.distance)


def test_chunk_variance_tightens_std_vs_paragraph_level():
    """块间 std 修正(2026-06):证明退化方差被收紧——
    真实鲁迅语料上 dialogue_ratio 的块间 std 显著小于逐段 std(后者因逐段 0/1
    取值退化到 ~0.5,使 tolerance 宽到几乎不拦截);同时 mean 不变(== compute_all)。"""
    import statistics

    from novel_system.services.style_reference.metrics import MetricsEngine, ParagraphRecord

    book_id, _stats, _count = _ingest(CORPUS / "luxun_short_stories.txt", "var_lu")
    with SessionLocal() as session:
        paras = [
            ParagraphRecord(text=p.text, paragraph_type=p.paragraph_type)
            for p in StyleReferenceRepository(session).list_paragraphs(book_id)
        ]
    engine = MetricsEngine()
    chunk_mean, chunk_std = engine.compute_with_variance(paras)["dialogue_ratio"]
    para_vals = [engine._per_paragraph("dialogue_ratio", p) for p in paras]
    para_std = statistics.pstdev(para_vals)

    # mean 不随分块改变(仍是全文逐段均值)
    assert chunk_mean == pytest.approx(engine.compute_all(paras)["dialogue_ratio"], rel=1e-9)
    # 块间 std 严格小于逐段 std,且脱离退化区(~0.5)
    assert chunk_std < para_std, f"块间 std {chunk_std:.3f} 应小于逐段 std {para_std:.3f}"
    assert chunk_std < 0.4, f"块间 std 应脱离逐段 0/1 的退化方差区,实际 {chunk_std:.3f}"
