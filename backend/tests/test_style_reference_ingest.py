"""IngestService 单测:path/upload + checksum 去重 + assess_input_size + 落表。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import (
    DuplicateBookError,
    EmptyBookError,
)
from novel_system.services.style_reference.ingest import (
    IngestService,
    assess_input_size,
)


SAMPLE_TEXT = """第一段是一段普通的叙述文字,介绍一个清晨的雾气与街上行人,描绘出一种安静而略带寒意的氛围,时间仿佛停滞。

他说:“今天天气不错。”

我心里想着昨天的事情,觉得有些不安,暗忖该如何回应。

记得那年她还在的时候,我们一起去过田野,看到稻穗在风中起伏。

几日后。
"""


# ---------------------------------------------------------------------------
# assess_input_size 边界
# ---------------------------------------------------------------------------


def test_assess_input_size_skip_when_below_threshold() -> None:
    result = assess_input_size(5000)
    assert result["language"] == "skip"
    assert result["narrative"] == "skip"
    assert result["scene"] == "skip"
    assert result["theme"] == "skip"


def test_assess_input_size_low_for_language() -> None:
    # language: skip=10000, low=30000
    result = assess_input_size(15000)
    assert result["language"] == "low"
    assert result["narrative"] == "skip"  # narrative skip=20000


def test_assess_input_size_high_for_language() -> None:
    result = assess_input_size(60000)
    assert result["language"] == "high"


def test_assess_input_size_full_high() -> None:
    result = assess_input_size(200000)
    assert all(level == "high" for level in result.values())


# ---------------------------------------------------------------------------
# IngestService path / upload happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def ingest_service():
    with SessionLocal() as session:
        yield IngestService(session, llm_enabled=False)


def test_ingest_upload_happy_path(ingest_service: IngestService) -> None:
    result = ingest_service.ingest_upload(
        raw_bytes=SAMPLE_TEXT.encode("utf-8"),
        file_name="sample.txt",
        title="测试样本",
        author_label="作者A",
        cloud_policy="local_only",
    )
    book = result.book
    assert book.book_id.startswith("sr_book_")
    assert book.status == "ready"
    assert book.total_chars == len(SAMPLE_TEXT.strip())
    assert result.paragraphs_count == 5
    stats = book.stats_json
    assert set(stats.keys()) >= {
        "metrics",
        "prose_shape_metrics",
        "input_assessment",
        "classifier_calibration",
        "paragraph_type_distribution",
        "safety",
    }
    assert len(stats["metrics"]) == 21
    assert len(stats["prose_shape_metrics"]) == 5
    # 离线 fallback 标记
    assert stats["classifier_calibration"]["fallback_to_heuristic"] is True
    # 段落分布非空
    assert sum(stats["paragraph_type_distribution"].values()) == pytest.approx(1.0, rel=0.05)


def test_ingest_path_happy_path(ingest_service: IngestService, tmp_path: Path) -> None:
    p = tmp_path / "book.txt"
    p.write_text(SAMPLE_TEXT, encoding="utf-8")
    result = ingest_service.ingest_path(
        p,
        title="路径样本",
        author_label="作者B",
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )
    assert result.book.source_kind == "path"
    assert result.book.source_path == str(p)
    assert result.book.cloud_policy == "segments_only"


def test_ingest_path_not_found(ingest_service: IngestService) -> None:
    with pytest.raises(DomainError) as exc_info:
        ingest_service.ingest_path(
            "/non_existent/path.txt",
            title="x",
            author_label="x",
            cloud_policy="local_only",
        )
    assert exc_info.value.code == "STYLE_REFERENCE_BOOK_PATH_NOT_FOUND"


def test_ingest_upload_rejects_non_text_format(ingest_service: IngestService) -> None:
    with pytest.raises(DomainError) as exc_info:
        ingest_service.ingest_upload(
            raw_bytes=b"binary data",
            file_name="image.png",
            title="x",
            author_label="x",
            cloud_policy="local_only",
        )
    assert exc_info.value.code == "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED"


def test_ingest_empty_text_raises(ingest_service: IngestService) -> None:
    with pytest.raises(EmptyBookError):
        ingest_service.ingest_upload(
            raw_bytes=b"   \n\n   ",
            file_name="empty.txt",
            title="x",
            author_label="x",
            cloud_policy="local_only",
        )


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


def test_ingest_duplicate_raises(ingest_service: IngestService) -> None:
    ingest_service.ingest_upload(
        raw_bytes=SAMPLE_TEXT.encode("utf-8"),
        file_name="sample.txt",
        title="第一次",
        author_label="A",
        cloud_policy="local_only",
    )
    ingest_service.session.commit()
    with pytest.raises(DuplicateBookError) as exc_info:
        ingest_service.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name="sample.txt",
            title="第二次",
            author_label="A",
            cloud_policy="local_only",
        )
    assert exc_info.value.book_id.startswith("sr_book_")


# ---------------------------------------------------------------------------
# 黄金 corpus placeholder 跑通
# ---------------------------------------------------------------------------


def test_ingest_luxun_placeholder(ingest_service: IngestService) -> None:
    """端到端冒烟:用 placeholder corpus 跑全流程,断言所有 stats 字段非空。"""
    # 相对本测试文件解析,CWD 无关(repo 根 / backend 目录跑都能过)
    path = Path(__file__).resolve().parent / "golden/style_reference/corpus/luxun_short_stories.txt"
    result = ingest_service.ingest_path(
        path,
        title="鲁迅 placeholder",
        author_label="鲁迅",
        cloud_policy="local_only",
    )
    book = result.book
    assert book.status == "ready"
    assert result.paragraphs_count > 0
    stats = book.stats_json
    # 4 个核心 key 必存
    assert "metrics" in stats
    assert "input_assessment" in stats
    assert "classifier_calibration" in stats
    assert "paragraph_type_distribution" in stats
    # metrics 21 项(2026-09-23 测量核:感官词表指标删除)
    assert len(stats["metrics"]) == 21
    # 段落形态另存，不改变冻结的 21 项 QC 分母
    assert len(stats["prose_shape_metrics"]) == 5
    assert "paragraph_mean_chars" in stats["prose_shape_metrics"]
    # 每个 metric 都含 mean / std / sample_count
    for name, m in {
        **stats["metrics"],
        **stats["prose_shape_metrics"],
    }.items():
        assert "mean" in m
        assert "std" in m
        assert "sample_count" in m
    # v2 W3:全书声音签名随 ingest 落库
    assert stats["voice_signature"]["version"] == "voice_signature_v2"
    assert stats["voice_signature"]["stats"]["char_count"] > 0


# ---------------------------------------------------------------------------
# Paragraphs 表落地
# ---------------------------------------------------------------------------


def test_ingest_persists_paragraphs(ingest_service: IngestService) -> None:
    result = ingest_service.ingest_upload(
        raw_bytes=SAMPLE_TEXT.encode("utf-8"),
        file_name="sample.txt",
        title="持久化测试",
        author_label="作者",
        cloud_policy="local_only",
    )
    ingest_service.session.commit()
    paragraphs = ingest_service.repo.list_paragraphs(result.book.book_id)
    assert len(paragraphs) == 5
    # paragraph_index 必须有序
    assert [p.paragraph_index for p in paragraphs] == [0, 1, 2, 3, 4]
    # 第一段应是 narration
    assert paragraphs[0].paragraph_type == "narration"
    # 第二段是 dialogue(含中文引号)
    assert paragraphs[1].paragraph_type == "dialogue"
    # 全部段都有非零 classifier_confidence
    assert all(p.classifier_confidence > 0 for p in paragraphs)


def test_ingest_cloud_policy_pydantic_validation(ingest_service: IngestService) -> None:
    """无效 cloud_policy 应触发 Pydantic Enum ValueError。"""
    with pytest.raises(ValueError):
        ingest_service.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name="sample.txt",
            title="x",
            author_label="x",
            cloud_policy="invalid_policy_xyz",
        )


def test_ingest_blank_title_falls_back_to_filename(ingest_service: IngestService, tmp_path: Path) -> None:
    """标题留空时从文件名推导（路径导入 = stem；上传 = 文件名 stem）。"""
    book_path = tmp_path / "reference-learning.md"
    book_path.write_text(SAMPLE_TEXT, encoding="utf-8")
    result = ingest_service.ingest_path(
        file_path=book_path,
        title="  ",
        author_label=None,
        cloud_policy="local_only",
    )
    assert result.book.title == "reference-learning"

    upload = ingest_service.ingest_upload(
        raw_bytes=(SAMPLE_TEXT + "（上传变体）").encode("utf-8"),
        file_name="风格样本.txt",
        title="",
        author_label=None,
        cloud_policy="local_only",
    )
    assert upload.book.title == "风格样本"


# ---------------------------------------------------------------------------
# v2 W3:stats_json.voice_signature
# ---------------------------------------------------------------------------


def test_ingest_writes_voice_signature(ingest_service: IngestService) -> None:
    from novel_system.services.style_reference.voice_signature import (
        FEATURE_NAMES,
        VOICE_SIGNATURE_VERSION,
    )

    result = ingest_service.ingest_upload(
        raw_bytes=SAMPLE_TEXT.encode("utf-8"),
        file_name="sample.txt",
        title="声音签名",
        author_label="作者",
        cloud_policy="local_only",
    )
    signature = result.book.stats_json["voice_signature"]
    assert signature["version"] == VOICE_SIGNATURE_VERSION == "voice_signature_v2"
    assert tuple(signature["features"]) == FEATURE_NAMES
    assert all(isinstance(value, float) for value in signature["features"].values())
    assert signature["stats"]["paragraph_count"] == 5
    assert signature["stats"]["quote_count"] == 1
    assert isinstance(signature["deliberate_repetition"], bool)
