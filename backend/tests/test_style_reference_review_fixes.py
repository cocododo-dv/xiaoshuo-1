"""2026-07 深度评审修复回归 — 每项缺陷一组可证伪锚点。

覆盖(编号对应评审报告):
- D2 输入量门槛接线:skip 层剔除 / 全 skip 409 / force 逃生门 / 无评估不门控
- D4 metrics 字符集勘误:全角 ？/； 计数、重复字符不双计
- D5 补证晋升的原始 evidence 必须过 span 校验(伪造引文不得入库)
- D6 resolve 选取单点过滤非 active profile 的 binding(注入 / qc gate 一致)
- D7 终态(被 reap/取消)的 run 不被后台 worker 复活
- D8 binding 决平时间戳微秒精度
- D9 抽取数量上限 obs ≤6 / fp ≤2
- D10 import-path 后缀白名单(任意服务器文件读取面收窄)
- D1 synthesizer 的 quotes run-scoped(跨 run 引文不混入 profile)

D3(quantitative 排除段型比例指标)在
test_style_reference_validation_quantitative.py 中锚定。
"""

from __future__ import annotations

import json

import pytest

from tests.accounted_llm_fakes import AccountedGenerateMixin
from sqlalchemy import select

from novel_system.db.models import StyleReferenceQuote
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.dimensions import Layer, SubDimension
from novel_system.services.style_reference.ingest import (
    MAX_REFERENCE_BOOK_BYTES,
    IngestService,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.run_orchestrator import RunOrchestrator

SAMPLE_TEXT = """这是一段比较长的叙述文字,用于让 ingest 能切出多个段落来。

他说:"你好啊,今天风很大。"

我心里想着昨天的事,觉得有些不安。

记得那年她还在的时候。

天空忽然变暗了。
"""


def _decode_untrusted_payload(user_msg: str) -> dict:
    """按共享 LLM helper 的显式边界协议解码测试请求。"""

    opening_token = "[UNTRUSTED_REFERENCE_DATA:"
    closing_token = "\n[/UNTRUSTED_REFERENCE_DATA]"
    assert user_msg.count(opening_token) == 1, "请求必须且只能包含一个不可信数据起始边界"
    assert user_msg.count(closing_token) == 1, "请求必须且只能包含一个不可信数据结束边界"

    opening = user_msg.index(opening_token)
    payload_start = user_msg.index("]\n", opening) + 2
    payload_end = user_msg.index(closing_token, payload_start)
    payload = json.loads(user_msg[payload_start:payload_end])
    assert isinstance(payload, dict), "不可信数据边界内必须是 JSON object"
    return payload


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


def _set_assessment(book_id: str, assessment: dict | None) -> None:
    """覆写 book.stats_json.input_assessment(None = 删除该键)。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book = repo.get_book(book_id)
        stats = dict(book.stats_json or {})
        if assessment is None:
            stats.pop("input_assessment", None)
        else:
            stats["input_assessment"] = assessment
        book.stats_json = stats
        session.commit()


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
# D2 — 输入量门槛接线(§6.4)
# ---------------------------------------------------------------------------


def test_skip_layers_filtered_and_recorded(fake_extractor_llm) -> None:
    """混合评估:skip 层被剔除且记入 coverage_json.skipped_layers,其余层照常抽取。"""
    book_id = _ingest("gate_mix")
    _set_assessment(
        book_id,
        {"language": "low", "narrative": "skip", "scene": "medium", "theme": "skip"},
    )
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        result = orch.start_extract_run(book_id)
        session.commit()
    assert result.status == "done"
    assert result.layers == ["language", "scene"]
    assert len(result.sub_dim_results) == 8  # 2 层 × 4 sub_dim
    with SessionLocal() as session:
        run = StyleReferenceRepository(session).get_run(result.run_id)
        assert run.coverage_json.get("skipped_layers") == ["narrative", "theme"]


def test_all_skip_raises_input_too_small(fake_extractor_llm) -> None:
    """全层 skip → 409 STYLE_REFERENCE_INPUT_TOO_SMALL(此前会照跑 16 sub_dim 白烧 LLM)。"""
    book_id = _ingest("gate_all")  # 百余字,ingest 评估天然全 skip
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        with pytest.raises(DomainError) as exc_info:
            orch.start_extract_run(book_id)
        assert exc_info.value.code == "STYLE_REFERENCE_INPUT_TOO_SMALL"
        assert exc_info.value.status_code == 409
        assert exc_info.value.details["input_assessment"]
    # 门槛拒绝不留 run 残行
    with SessionLocal() as session:
        assert StyleReferenceRepository(session).list_runs(book_id=book_id) == []


def test_force_bypasses_input_gate(fake_extractor_llm) -> None:
    book_id = _ingest("gate_force")
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        result = orch.start_extract_run(book_id, force=True)
        session.commit()
    assert result.status == "done"
    assert set(result.layers) == {"language", "narrative", "scene", "theme"}


def test_missing_assessment_skips_gating(fake_extractor_llm) -> None:
    """无 input_assessment(直建 book 的测试/历史数据)→ 不门控,行为向后兼容。"""
    book_id = _ingest("gate_none")
    _set_assessment(book_id, None)
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        result = orch.start_extract_run(book_id, layers=[Layer.LANGUAGE])
        session.commit()
    assert result.status == "done"
    assert result.layers == ["language"]


# ---------------------------------------------------------------------------
# D7 — 终态 run 不复活
# ---------------------------------------------------------------------------


class _ExplodingClient(AccountedGenerateMixin):
    """被调用即失败:用于断言终态 run 不会再触发任何 LLM 抽取。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):  # noqa: ANN001
        self.calls += 1
        raise AssertionError("terminal-state run must not invoke extractors")


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_terminal_run_is_not_resurrected(terminal_status: str) -> None:
    """排队中被 reap(FAILED)/cancel 的 run,worker 接手时必须直接退出:
    不得把终态拉回 RUNNING→DONE(修复前 reap 后 worker 开跑会复活僵尸 run,
    并与同书新 run 并发互撞)。"""
    book_id = _ingest(f"zombie_{terminal_status}")
    run_id = f"sr_run_zombie_{terminal_status}"
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_run(
            run_id=run_id,
            book_id=book_id,
            status=terminal_status,
            phase="extract",
            coverage_json={"failure_reason": "stale_running_reaped"},
        )
        session.commit()

    client = _ExplodingClient()
    with SessionLocal() as session:
        orch = RunOrchestrator(session, llm_client=client, llm_enabled=True)
        result = orch._execute(
            run_id, book_id, [Layer.LANGUAGE], progress_commits=True
        )
    assert result.status == terminal_status
    assert client.calls == 0
    with SessionLocal() as session:
        run = StyleReferenceRepository(session).get_run(run_id)
        assert run.status == terminal_status


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
# D5 — 补证晋升的原始 evidence 必须过 span 校验
# ---------------------------------------------------------------------------


class _FabricatedEvidenceClient(AccountedGenerateMixin):
    """extract 返回 1 条 finding,其唯一原始 evidence 的 quote 为伪造文本
    (不在任何段落里);supplement 返回 2 条真实 evidence。

    修复前:伪造原始条目未经校验即随晋升入库;修复后仅 2 条真实 evidence 入库。
    """

    FABRICATED = "这句话绝对不在原文里九头蛇万岁"

    def __init__(self) -> None:
        self.supplement_calls = 0

    def generate(self, request):  # noqa: ANN001
        user_msg = request.messages[-1]["content"]
        payload = _decode_untrusted_payload(user_msg)
        paras = payload.get("paragraphs") or []
        first = paras[0] if paras else {"paragraph_id": None, "text": ""}

        if request.node_id == "style_ref_supplement_evidence":
            self.supplement_calls += 1
            structured = {
                "additional_evidence": [
                    {
                        "paragraph_id": first.get("paragraph_id"),
                        "span": [0, 5],
                        "quote": (first.get("text") or "")[:5],
                        "illustrates_dims": [],
                        "anchor_kind": "paragraph_quote",
                    },
                    {
                        "paragraph_id": first.get("paragraph_id"),
                        "span": [0, 8],
                        "quote": (first.get("text") or "")[:8],
                        "illustrates_dims": [],
                        "anchor_kind": "paragraph_quote",
                    },
                ]
            }
        else:  # extract 节点
            structured = {
                "observations": [
                    {
                        "statement": "叙述倾向以短句推进并保留留白",
                        "confidence": "medium",
                        "evidence": [
                            {
                                "paragraph_id": first.get("paragraph_id"),
                                "span": [0, 10],
                                "quote": self.FABRICATED,
                                "illustrates_dims": [],
                                "anchor_kind": "paragraph_quote",
                            }
                        ],
                    }
                ],
                "forbidden_patterns": [],
            }

        class _Resp:
            structured_output = structured
            text = json.dumps(structured, ensure_ascii=False)
            usage: dict = {}
            finish_reason = "stop"
            request_id = None
            provider = "fake"
            model = "fake"
            raw_response: dict = {}
            response_format = "json_object"

        return _Resp()


def test_promoted_finding_drops_fabricated_original_evidence() -> None:
    from novel_system.services.style_reference.extractors import LanguageExtractor

    book_id = _ingest("fab_ev")
    run_id = "sr_run_fab_ev"
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_run(
            run_id=run_id, book_id=book_id, status="running", phase="extract",
            coverage_json={},
        )
        session.commit()

    client = _FabricatedEvidenceClient()
    with SessionLocal() as session:
        extractor = LanguageExtractor(session, client, run_id=run_id, book_id=book_id)
        result = extractor._extract_with_retry(SubDimension.LANGUAGE_SENTENCE_STRUCTURE)
        session.commit()

    assert client.supplement_calls >= 1, "应走补证重试路径"
    assert len(result.findings) == 1, "补证后 finding 应晋升"
    with SessionLocal() as session:
        quote_texts = [
            q.quote_text
            for q in session.scalars(
                select(StyleReferenceQuote).where(StyleReferenceQuote.book_id == book_id)
            ).all()
        ]
    assert quote_texts, "真实补证 evidence 应落 quotes 表"
    assert all(_FabricatedEvidenceClient.FABRICATED not in t for t in quote_texts), (
        "伪造的原始 evidence 不得入库(修复前会随晋升写进 quotes → few-shot 注入)"
    )


# ---------------------------------------------------------------------------
# D9 — 抽取数量上限 obs ≤6 / fp ≤2
# ---------------------------------------------------------------------------


class _OverflowClient(AccountedGenerateMixin):
    """extract 返回 10 obs + 5 fp(全部合法 2 evidence),超出 §6.5 上限。"""

    def generate(self, request):  # noqa: ANN001
        user_msg = request.messages[-1]["content"]
        payload = _decode_untrusted_payload(user_msg)
        paras = payload.get("paragraphs") or []
        first = paras[0] if paras else {"paragraph_id": None, "text": ""}

        def _finding(i: int) -> dict:
            return {
                "statement": f"观察陈述编号{i}:句式以短句为主",
                "confidence": "medium",
                "evidence": [
                    {
                        "paragraph_id": first.get("paragraph_id"),
                        "span": [0, 5],
                        "quote": (first.get("text") or "")[:5],
                        "illustrates_dims": [],
                        "anchor_kind": "paragraph_quote",
                    },
                    {
                        "paragraph_id": first.get("paragraph_id"),
                        "span": [0, 8],
                        "quote": (first.get("text") or "")[:8],
                        "illustrates_dims": [],
                        "anchor_kind": "paragraph_quote",
                    },
                ],
            }

        structured = {
            "observations": [_finding(i) for i in range(10)],
            "forbidden_patterns": [_finding(100 + i) for i in range(5)],
        }

        class _Resp:
            structured_output = structured
            text = json.dumps(structured, ensure_ascii=False)
            usage: dict = {}
            finish_reason = "stop"
            request_id = None
            provider = "fake"
            model = "fake"
            raw_response: dict = {}
            response_format = "json_object"

        return _Resp()


def test_extraction_output_caps_enforced() -> None:
    from novel_system.services.style_reference.extractors import LanguageExtractor
    from novel_system.services.style_reference.schemas import FindingKind

    book_id = _ingest("caps")
    run_id = "sr_run_caps"
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_run(
            run_id=run_id, book_id=book_id, status="running", phase="extract",
            coverage_json={},
        )
        session.commit()

    with SessionLocal() as session:
        extractor = LanguageExtractor(
            session, _OverflowClient(), run_id=run_id, book_id=book_id
        )
        result = extractor._extract_with_retry(SubDimension.LANGUAGE_VOCABULARY)
        session.commit()

    obs = [f for f in result.findings if f.finding_kind == FindingKind.OBSERVATION]
    fp = [f for f in result.findings if f.finding_kind == FindingKind.FORBIDDEN_PATTERN]
    assert len(obs) == 6, f"observations 上限 6,实际 {len(obs)}"
    assert len(fp) == 2, f"forbidden_patterns 上限 2,实际 {len(fp)}"


# ---------------------------------------------------------------------------
# D8 — binding 决平时间戳精度
# ---------------------------------------------------------------------------


def test_ts_to_int_distinguishes_microseconds() -> None:
    from novel_system.services.style_reference.inject.bindings import ts_to_int as _ts_to_int

    older = _ts_to_int("2026-07-06T01:02:03.000001+00:00")
    newer = _ts_to_int("2026-07-06T01:02:03.000002+00:00")
    assert newer > older, "同秒不同微秒的时间戳必须可区分(修复前截到秒退化为插入序)"
    # 无微秒的时间串仍可比较且不大于同秒任何带微秒值以外的次序错乱
    assert _ts_to_int("2026-07-06T01:02:04") > newer


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


# ---------------------------------------------------------------------------
# D1 — synthesizer quotes run-scoped
# ---------------------------------------------------------------------------


def _fake_synth_client():
    structured = {
        "profile_title": "t",
        "narrative_summary": "s",
        "style_features": ["f"],
        "narrative_patterns": ["p"],
        "banned_replication_rules": ["b"],
    }

    class _Resp:
        structured_output = structured
        text = json.dumps(structured, ensure_ascii=False)
        usage: dict = {}
        finish_reason = "stop"
        request_id = None
        provider = "fake"
        model = "fake"
        raw_response: dict = {}
        response_format = "json_object"

    class _Client(AccountedGenerateMixin):
        def generate(self, request):  # noqa: ANN001
            return _Resp()

    return _Client()


def test_end_to_end_quotes_bucket_by_real_type_and_the_render_carries_the_red_line(fake_extractor_llm) -> None:
    """D1 端到端收口:抽取(真实段落引文)→ 合成(段型分桶)→ 应用 → 渲染。

    合成期的段型索引覆盖 ≥2 种真实段型(修复前所有引文落 narration 桶)。2026-09-23 风格参考 v3:样例不再
    从引文里挑(改由全书窗口索引按本场设计挑),旧策略 B 映射为全面模仿;这本小书切不出 600 字以上的窗,
    所以只有卡替身 + 红线——提示里不出现英文段型标签。
    """
    from types import SimpleNamespace

    from novel_system.services.style_policy import style_policy_live
    from novel_system.services.style_reference.extractors import LanguageExtractor
    from novel_system.services.style_reference.inject.render import render_style
    from novel_system.services.style_reference.inject.request import StyleRenderRequest
    from novel_system.services.style_reference.materialization import (
        MaterializationService,
    )
    from novel_system.services.style_reference.profile_synthesizer import (
        ProfileSynthesizer,
    )
    from novel_system.services.style_reference.schemas import (
        BindingScope,
        InjectionStrategy,
        TaskType,
    )

    book_id = _ingest("e2e_fs", cloud_policy="allow_full_cloud")
    run_id = "sr_run_e2e_fs"
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_run(
            run_id=run_id, book_id=book_id, status="running", phase="extract",
            coverage_json={},
        )
        session.commit()
    with SessionLocal() as session:
        LanguageExtractor(
            session, fake_extractor_llm("default"), run_id=run_id, book_id=book_id
        ).extract_all_sub_dimensions()
        session.commit()

    with SessionLocal() as session:
        profile = ProfileSynthesizer(
            session, llm_client=_fake_synth_client(), llm_enabled=True
        ).synthesize(book_id, run_id)
        profile_id = profile.profile_id
        index = dict(profile.profile_json["scene_samples_index"])
        session.commit()
    assert len(index) >= 2, f"段型索引应覆盖多种真实段型,实际 {sorted(index)}"

    with SessionLocal() as session:
        MaterializationService(session).apply_profile(
            profile_id,
            scope=BindingScope.PROJECT,
            scope_ref_id="proj_e2e_fs",
            task_type=TaskType.SCENE_GENERATION,
            strategy=InjectionStrategy.B,
        )
        session.commit()

    with SessionLocal() as session:
        scope = SimpleNamespace(project_id="proj_e2e_fs", scene_id=None, pov_character_id=None, onstage_chars_json=[])
        policy = style_policy_live(session, scope)
        rendered = render_style(session, policy, StyleRenderRequest(), use_cache=False)
    assert policy.bound and policy.reference_mode == "full"
    assert rendered.system_prefix.startswith("[STYLE_REFERENCE]")
    assert "严格禁止" in rendered.system_prefix
    assert not any(f"({pt}" in rendered.system_prefix for pt in ("dialogue", "psychology", "narration", "flashback"))


def test_synthesize_quotes_are_run_scoped() -> None:
    """同书两次抽取:合成 run2 的 profile 不得混入 run1 的引文。"""
    from novel_system.services.style_reference.profile_synthesizer import (
        ProfileSynthesizer,
    )

    book_id = _ingest("runscope")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        para = repo.list_paragraphs(book_id)[0]
        for run_seed in ("r1", "r2"):
            run_id = f"sr_run_scope_{run_seed}"
            repo.create_run(
                run_id=run_id, book_id=book_id, status="done", phase="done"
            )
            repo.create_extraction(
                extraction_id=f"sr_ext_scope_{run_seed}", book_id=book_id,
                run_id=run_id, layer="language", sub_dimension="language.rhetoric",
                raw_payload_json={}, status="done", validation_errors_json=[],
                purpose="extract",
            )
            repo.create_finding(
                finding_id=f"sr_find_scope_{run_seed}", book_id=book_id,
                run_id=run_id, extraction_id=f"sr_ext_scope_{run_seed}",
                sub_dimension="language.rhetoric", finding_kind="observation",
                statement=f"陈述{run_seed}", confidence="high", status="pending",
            )
            repo.create_quote(
                quote_id=f"sr_quote_scope_{run_seed}", book_id=book_id,
                paragraph_id=para.paragraph_id, span_start=0, span_end=5,
                quote_text=f"引文{run_seed}", illustrates_dims=["language.rhetoric"],
                extracted_features={"anchor_kind": "paragraph_quote"},
            )
            repo.create_evidence(
                evidence_id=f"sr_ev_scope_{run_seed}",
                finding_id=f"sr_find_scope_{run_seed}",
                quote_id=f"sr_quote_scope_{run_seed}",
                anchor_kind="paragraph_quote", is_synthetic=0,
            )
        session.commit()

    with SessionLocal() as session:
        synth = ProfileSynthesizer(
            session, llm_client=_fake_synth_client(), llm_enabled=True
        )
        profile = synth.synthesize(book_id, "sr_run_scope_r2")
        session.commit()

    flat = [
        qid
        for ids in profile.profile_json["scene_samples_index"].values()
        for qid in ids
    ]
    assert "sr_quote_scope_r2" in flat, "本 run 的引文应入索引"
    assert "sr_quote_scope_r1" not in flat, "旧 run 的引文不得混入(修复前全书引文均入)"
    assert profile.coverage_json["quotes_count"] == 1
