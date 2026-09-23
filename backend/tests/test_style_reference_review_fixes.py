"""2026-07 深度评审修复回归 — 每项缺陷一组可证伪锚点。

覆盖(编号对应评审报告):
- D4 metrics 字符集勘误:全角 ？/； 计数、重复字符不双计
- D6 resolve 选取单点过滤非 active profile 的 binding(注入 / qc gate 一致)
- D8 binding 决平时间戳微秒精度
- D10 import-path 后缀白名单(任意服务器文件读取面收窄)

D1 / D2 / D5 / D7 / D9 随旧学习链路(RunOrchestrator / 抽取器 / 同步合成)删除;对应语义在学习文风作业里的锚点见
test_style_reference_learn_job.py(输入量门槛 / 伪造引文丢弃 / 数量上限 / 本轮发现不混入旧轮 / 续跑)。

D3(quantitative 排除段型比例指标)在
test_style_reference_validation_quantitative.py 中锚定。
"""

from __future__ import annotations

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.ingest import (
    MAX_REFERENCE_BOOK_BYTES,
    IngestService,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

SAMPLE_TEXT = """这是一段比较长的叙述文字,用于让 ingest 能切出多个段落来。

他说:"你好啊,今天风很大。"

我心里想着昨天的事,觉得有些不安。

记得那年她还在的时候。

天空忽然变暗了。
"""


def _ingest(seed: str, *, cloud_policy: str = "segments_only") -> str:
    with SessionLocal() as session:
        result = IngestService(session, llm_enabled=False).ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name=f"rf_{seed}.txt",
            title=f"评审修复{seed}",
            author_label=None,
            cloud_policy=cloud_policy,
            rights_declaration=(
                {"analysis_rights": True, "send_rights": True}
                if cloud_policy != "local_only"
                else None
            ),
        )
        session.commit()
        return result.book.book_id


# ---------------------------------------------------------------------------
# D4 — metrics 字符集勘误
# ---------------------------------------------------------------------------


def test_fullwidth_question_and_semicolon_are_counted() -> None:
    """全角 ？/； 必须计入密度(修复前两指标对中文文本恒 ≈0)。"""
    from novel_system.services.style_reference.metrics import (
        MetricsEngine,
        ParagraphRecord,
    )

    engine = MetricsEngine()
    paras = [ParagraphRecord(text="你要去哪里？我不知道；他也不知道。", paragraph_type="narration")]
    out = engine.compute_all(paras)
    assert out["question_density_per_1k"] > 0, "全角？未被统计"
    assert out["semicolon_density_per_1k"] > 0, "全角；未被统计"


def test_ascii_question_not_double_counted() -> None:
    """修复前 "??"(两个 ASCII ?)使每个 ASCII 问号被双计。3 个可见字含 1 个 ? → 1000/3 每千字
    (2026-09-23 起「每千字」按可见字算,不含标点)。"""
    from novel_system.services.style_reference.metrics import (
        MetricsEngine,
        ParagraphRecord,
    )

    engine = MetricsEngine()
    out = engine.compute_all([ParagraphRecord(text="abc?", paragraph_type="narration")])
    assert out["question_density_per_1k"] == pytest.approx(1000.0 / 3)


def test_punct_chars_have_no_duplicates_and_include_fullwidth_colon() -> None:
    from novel_system.services.style_reference.metrics import _PUNCT_CHARS

    assert len(set(_PUNCT_CHARS)) == len(_PUNCT_CHARS), "标点字符集含重复字符(会双计)"
    assert "：" in _PUNCT_CHARS


# ---------------------------------------------------------------------------
# D6 — resolve 单点过滤非 active profile
# ---------------------------------------------------------------------------


def _seed_binding(seed: str, *, profile_status: str, project_id: str) -> None:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=f"sr_book_{seed}", title="t", source_kind="upload",
            cloud_policy="segments_only", text_checksum=f"chk_{seed}",
            total_chars=10, status="ready",
            stats_json={"rights_declaration": {
                "declared": True, "analysis_rights": True, "send_rights": True,
            }},
        )
        repo.create_run(
            run_id=f"sr_run_{seed}", book_id=f"sr_book_{seed}", status="done", phase="done"
        )
        repo.create_profile(
            profile_id=f"sr_profile_{seed}", book_id=f"sr_book_{seed}",
            run_id=f"sr_run_{seed}", title="t", status=profile_status,
            profile_json={"narrative_summary": "n", "style_features": ["x"]},
            coverage_json={}, source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=f"sr_bind_{seed}", profile_id=f"sr_profile_{seed}",
            scope="project", scope_ref_id=project_id,
            task_type="scene_generation", strategy="A",
            config_json={}, status="active",
        )
        session.commit()


def test_resolve_skips_binding_of_inactive_profile() -> None:
    """draft/archived profile 的 binding 不再被 resolve 选中(与注入渲染一致)。"""
    from novel_system.services.style_reference.injection import InjectionService

    _seed_binding("res_draft", profile_status="draft", project_id="proj_res_draft")
    with SessionLocal() as session:
        svc = InjectionService(session)
        assert svc.resolve_active_binding("proj_res_draft", "scene_generation") is None
        assert svc.resolve_binding_layers("proj_res_draft", "scene_generation") == []

    # 对照:profile 激活后即可被选中(证明过滤条件就是 profile 状态本身)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.update_profile("sr_profile_res_draft", status="active")
        session.commit()
    with SessionLocal() as session:
        picked = InjectionService(session).resolve_active_binding(
            "proj_res_draft", "scene_generation"
        )
        assert picked is not None and picked.binding_id == "sr_bind_res_draft"


def test_qc_gate_returns_none_for_inactive_profile(session) -> None:
    """qc gate 不得以从未注入的 draft profile 做风格裁决(修复前 banned_term 会拦下场景)。"""
    from novel_system.db.models import SceneCard
    from novel_system.services.qc_engine import HardQcEngine

    _seed_binding("gate_draft", profile_status="draft", project_id="proj_gate_draft")
    with SessionLocal() as s2:
        StyleReferenceRepository(s2).create_banned_term(
            term_id="sr_term_gate_draft", profile_id="sr_profile_gate_draft",
            scope="generation", term="美轮美奂", source="manual",
        )
        s2.commit()

    scene = SceneCard(
        scene_id="CH900_SC1", chapter_id="CH900", project_id="proj_gate_draft",
        scene_seq=1, pov_character_id="A", onstage_chars_json=["A"], location="x",
        scene_goal="g", beats_json=["b"], must_include_text="m",
        target_length_band="short", scene_type="t", is_chapter_last=0,
    )
    engine = HardQcEngine(session, llm_client=object())
    verdict = engine._apply_style_validation_gate(scene, "这景色真是美轮美奂极了。")
    assert verdict is None


# ---------------------------------------------------------------------------
# D8 — binding 决平时间戳精度
# ---------------------------------------------------------------------------


def test_ts_to_int_distinguishes_microseconds() -> None:
    from novel_system.services.style_reference.injection import _ts_to_int

    older = _ts_to_int("2026-07-06T01:02:03.000001+00:00")
    newer = _ts_to_int("2026-07-06T01:02:03.000002+00:00")
    assert newer > older, "同秒不同微秒的时间戳必须可区分(修复前截到秒退化为插入序)"
    # 无微秒的时间串仍可比较且不大于同秒任何带微秒值以外的次序错乱
    assert _ts_to_int("2026-07-06T01:02:04") > newer


def test_merged_positive_block_keeps_single_header() -> None:
    """多层叠加的 positive 块只保留一个 [正向风格特征] 标题(块头重复干扰 LLM 解析)。"""
    from novel_system.services.style_reference.injection import _merge_fragments
    from novel_system.services.style_reference.schemas import SystemPromptFragments

    base = SystemPromptFragments(positive_block="[正向风格特征]\n概述:基底层\n- 短句")
    scene = SystemPromptFragments(positive_block="[正向风格特征]\n概述:场景层\n- 白描")
    merged = _merge_fragments([base, scene])
    assert merged.positive_block.count("[正向风格特征]") == 1
    assert "基底层" in merged.positive_block and "白描" in merged.positive_block


# ---------------------------------------------------------------------------
# D10 — import-path 后缀白名单
# ---------------------------------------------------------------------------


def test_import_path_rejects_non_text_files(tmp_path) -> None:
    with SessionLocal() as session:
        service = IngestService(session, llm_enabled=False)
        for bad in ("/etc/passwd", str(tmp_path / "novel.db"), str(tmp_path / ".env")):
            with pytest.raises(DomainError) as exc_info:
                service.ingest_path(
                    bad, title="t", author_label=None, cloud_policy="segments_only"
                )
            assert exc_info.value.code == "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED", bad
            assert exc_info.value.status_code == 400


def test_import_path_accepts_txt(tmp_path) -> None:
    p = tmp_path / "ok.txt"
    p.write_text(SAMPLE_TEXT, encoding="utf-8")
    with SessionLocal() as session:
        result = IngestService(session, llm_enabled=False).ingest_path(
            str(p),
            title="路径导入",
            author_label=None,
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        session.commit()
    assert result.paragraphs_count >= 2


def test_import_path_is_disabled_without_configured_roots(tmp_path, monkeypatch) -> None:
    p = tmp_path / "disabled.txt"
    p.write_text(SAMPLE_TEXT, encoding="utf-8")
    monkeypatch.delenv("NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS", raising=False)

    with SessionLocal() as session, pytest.raises(DomainError) as exc_info:
        IngestService(session, llm_enabled=False).ingest_path(
            p,
            title="disabled",
            author_label=None,
            cloud_policy="local_only",
        )

    assert exc_info.value.code == "STYLE_REFERENCE_PATH_IMPORT_DISABLED"
    assert exc_info.value.status_code == 403


def test_import_path_rejects_file_outside_configured_root(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    p = outside / "outside.txt"
    p.write_text(SAMPLE_TEXT, encoding="utf-8")
    monkeypatch.setenv("NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS", str(allowed))

    with SessionLocal() as session, pytest.raises(DomainError) as exc_info:
        IngestService(session, llm_enabled=False).ingest_path(
            p,
            title="outside",
            author_label=None,
            cloud_policy="local_only",
        )

    assert exc_info.value.code == "STYLE_REFERENCE_BOOK_PATH_FORBIDDEN"
    assert exc_info.value.status_code == 403


def test_import_path_rejects_oversized_text(tmp_path) -> None:
    p = tmp_path / "too-large.txt"
    p.write_bytes(b"x" * (MAX_REFERENCE_BOOK_BYTES + 1))

    with SessionLocal() as session, pytest.raises(DomainError) as exc_info:
        IngestService(session, llm_enabled=False).ingest_path(
            p,
            title="too large",
            author_label=None,
            cloud_policy="local_only",
        )

    assert exc_info.value.code == "STYLE_REFERENCE_UPLOAD_TOO_LARGE"
    assert exc_info.value.status_code == 413


def test_import_path_rejects_symbolic_links(tmp_path) -> None:
    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    target.write_text(SAMPLE_TEXT, encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this platform does not permit creating a test symlink")

    with SessionLocal() as session, pytest.raises(DomainError) as exc_info:
        IngestService(session, llm_enabled=False).ingest_path(
            link,
            title="link",
            author_label=None,
            cloud_policy="local_only",
        )

    assert exc_info.value.code == "STYLE_REFERENCE_BOOK_PATH_LINK_FORBIDDEN"


