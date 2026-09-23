"""ProfileSynthesizer 单测(PR-4)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.prompt_builder import PromptTemplate, load_prompt_templates
from novel_system.services.style_reference import injection as injection_module
from novel_system.services.style_reference.errors import (
    SYNTHESIZE_REASON_CODES,
    LLMRequiredError,
)
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.narrative_guidance import render_narrative_section
from novel_system.services.style_reference.metrics import (
    METRIC_NAMES,
    PROSE_SHAPE_METRIC_NAMES,
)
from novel_system.services.style_reference.profile_synthesizer import (
    _ANCHOR_QUOTES_PER_DIMENSION,
    _SYNTHESIS_REQUIRED_METRICS,
    ProfileSynthesizer,
    ProfileTextIntegrityError,
    SynthesizeError,
    _build_anchor_quotes_payload,
    _build_sample_quotes_payload,
    _derive_narrative_guidance,
    _estimate_synthesis_input_tokens,
    _fit_synthesis_payload_to_budget,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.untrusted_data import (
    UntrustedPayload,
    render_untrusted_user_prompt,
)
from tests.accounted_llm_fakes import AccountedGenerateMixin

VOICE_SIGNATURE_MODULE = "novel_system.services.style_reference.voice_signature"


SAMPLE_TEXT = """这是一段叙述,介绍清晨场景。

他说:"今天天气真好。"

我心里想着昨日的对话。

记得那年她还在的时候。

雪从天上飘下来。
"""


def test_synthesis_prompt_does_not_anchor_every_profile_to_one_mood() -> None:
    template = load_prompt_templates()["style_ref_synthesize_profile"]

    assert template.version == "2026-09-05.v8"
    assert "冷峻克制的市井白描" not in template.system_prompt
    assert "只有在 finding_summaries 中存在直接对应的 theme finding" in template.system_prompt
    assert "不得扩张成“大量、密集、高频、总是、连续”" in template.system_prompt
    assert "只聚合 payload 实际存在的子维度" in template.task_prompt
    # W1:锚引文只作机制锚点、声音习惯需一致且不得抄写——两条约束都要在 task_prompt 里点名。
    assert "anchor_quotes 只作机制锚点,不得复述进任何输出字段" in template.task_prompt
    assert "voice_habits 是确定性统计得出的语言习惯" in template.task_prompt
    assert "style_features 应与之一致、不得抄写" in template.task_prompt


def _ingest_with_finding(book_seed: str) -> tuple[str, str]:
    """建一本书 + 一个 run + 若干 finding(模拟 PR-3 抽取后的状态)。"""
    with SessionLocal() as session:
        ingest = IngestService(session, llm_enabled=False)
        result = ingest.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name=f"book_{book_seed}.txt",
            title="测试书",
            author_label="作者",
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        book_id = result.book.book_id
        repo = StyleReferenceRepository(session)
        run_id = f"sr_run_{book_seed}"
        repo.create_run(
            run_id=run_id, book_id=book_id, status="done", phase="done"
        )
        # 各 sub_dim 加 1 obs + 1 forbid
        extraction_id = f"sr_ext_{book_seed}"
        repo.create_extraction(
            extraction_id=extraction_id,
            book_id=book_id,
            run_id=run_id,
            layer="language",
            sub_dimension="language.rhetoric",
            raw_payload_json={},
            status="done",
            validation_errors_json=[],
            purpose="extract",
        )
        for kind in ("observation", "forbidden_pattern"):
            repo.create_finding(
                finding_id=f"sr_find_{book_seed}_{kind}",
                book_id=book_id,
                run_id=run_id,
                extraction_id=extraction_id,
                sub_dimension="language.rhetoric",
                finding_kind=kind,
                statement=f"测试 {kind} 描述",
                confidence="high",
                status="pending",
            )
        # 1 个 quote(经 evidence 挂到 observation finding 上——2026-07 起
        # synthesizer 的 quotes 为 run-scoped,未关联 evidence 的引文不进聚合)
        repo.create_quote(
            quote_id=f"sr_quote_{book_seed}",
            book_id=book_id,
            paragraph_id=None,
            span_start=0,
            span_end=20,
            quote_text="他低头看着脚下的路",
            illustrates_dims=["language.rhetoric"],
            extracted_features={"paragraph_type": "narration"},
        )
        repo.create_evidence(
            evidence_id=f"sr_ev_{book_seed}",
            finding_id=f"sr_find_{book_seed}_observation",
            quote_id=f"sr_quote_{book_seed}",
            anchor_kind="paragraph_quote",
            is_synthetic=0,
        )
        session.commit()
        return book_id, run_id


def _fake_llm_with_response(response_dict: dict):
    class _Resp:
        structured_output = response_dict
        text = json.dumps(response_dict, ensure_ascii=False)
        usage = {}
        finish_reason = "stop"
        provider = "fake"
        model = "fake"
        response_format = "json_object"
        request_id = None
        raw_response = {}

    class _Client(AccountedGenerateMixin):
        def generate(self, request):  # noqa: ANN001
            return _Resp()

    return _Client()


def _fake_llm_with_responses(response_dicts: list[dict]):
    class _Client(AccountedGenerateMixin):
        def __init__(self) -> None:
            self.responses = list(response_dicts)
            self.requests = []

        def generate(self, request):  # noqa: ANN001
            self.requests.append(request)
            response_dict = self.responses.pop(0)
            return SimpleNamespace(
                structured_output=response_dict,
                text=json.dumps(response_dict, ensure_ascii=False),
                usage={},
                finish_reason="stop",
                provider="fake",
                model="fake",
                response_format="json_object",
                request_id=None,
                raw_response={},
            )

    return _Client()


def _payload_from_prompt(content: str) -> dict:
    """从 render_untrusted_user_prompt 渲染出的 user prompt 里解析回 payload JSON。"""
    start_marker = "[UNTRUSTED_REFERENCE_DATA:style_ref_synthesize_profile]\n"
    end_marker = "\n[/UNTRUSTED_REFERENCE_DATA]"
    start = content.index(start_marker) + len(start_marker)
    end = content.rindex(end_marker)
    return json.loads(content[start:end])


def _capturing_llm(response_dict: dict):
    """返回 (client, captured_user_prompts)。"""
    captured: list[str] = []

    class _Client(AccountedGenerateMixin):
        def generate(self, request):  # noqa: ANN001
            captured.append(request.messages[-1]["content"])
            return SimpleNamespace(
                structured_output=response_dict,
                text=json.dumps(response_dict, ensure_ascii=False),
                usage={},
                finish_reason="stop",
                provider="fake",
                model="fake",
                response_format="json_object",
                request_id=None,
                raw_response={},
            )

    return _Client(), captured


def _install_fake_voice_signature(monkeypatch, *, habits=None, raise_error: Exception | None = None):
    """把 W3 的 voice_signature 模块替换成可控假件(W3 落地前后都可用)。"""
    module = types.ModuleType(VOICE_SIGNATURE_MODULE)

    def compute_voice_signature(texts):  # noqa: ANN001
        if raise_error is not None:
            raise raise_error
        return {
            "version": "voice_signature_v1",
            "features": {"comma_per_sentence": 1.5, "text_count": float(len(texts))},
            "deliberate_repetition": False,
        }

    def render_voice_habits(features, baseline=None):  # noqa: ANN001
        return list(habits if habits is not None else ["常用连接词:却、便、又;少用:然而、于是"])

    module.compute_voice_signature = compute_voice_signature
    module.render_voice_habits = render_voice_habits
    monkeypatch.setitem(sys.modules, VOICE_SIGNATURE_MODULE, module)
    return module


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_sample_quote_is_selected_from_the_findings_own_evidence() -> None:
    """同一维度有多条引文时，不能把别的 finding 的证据错配过来。"""
    finding = SimpleNamespace(
        finding_id="finding_target",
        sub_dimension="language.rhetoric",
        finding_kind="observation",
        statement="目标观察",
    )
    wrong_quote = SimpleNamespace(
        quote_id="quote_wrong",
        quote_text="同维度但属于另一条观察的引文",
        illustrates_dims=["language.rhetoric"],
    )
    right_quote = SimpleNamespace(
        quote_id="quote_right",
        quote_text="目标观察自己的证据引文",
        illustrates_dims=["language.rhetoric"],
    )
    evidence = SimpleNamespace(
        evidence_id="evidence_target",
        finding_id="finding_target",
        quote_id="quote_right",
        anchor_kind="paragraph_quote",
        is_synthetic=0,
        created_at="2026-01-01T00:00:00Z",
    )

    payload = _build_sample_quotes_payload(
        [finding],
        [wrong_quote, right_quote],
        [evidence],
    )

    assert payload[0]["representative_quote"] == "目标观察自己的证据引文"


def test_synthesize_llm_required_when_disabled() -> None:
    book_id, run_id = _ingest_with_finding("disabled")
    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=None, llm_enabled=False)
        with pytest.raises(LLMRequiredError):
            synth.synthesize(book_id, run_id)


def test_synthesize_happy_path() -> None:
    book_id, run_id = _ingest_with_finding("happy")
    client = _fake_llm_with_response(
        {
            "profile_title": "清晨样本风格 v1",
            "narrative_summary": "短句+反讽+冷静叙述,克制情感",
            "style_features": ["善用短句", "反讽点缀", "白描+留白"],
            "narrative_patterns": ["人物对话引出冲突", "环境暗示情绪"],
            "banned_replication_rules": ["禁止堆砌华丽形容词"],
            "calibration_guidance": ["每场景至少一处白描"],
        }
    )
    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=client, llm_enabled=True)
        profile = synth.synthesize(book_id, run_id)
        session.commit()

    assert profile.title == "清晨样本风格 v1"
    assert profile.status == "draft"
    pj = profile.profile_json
    # 画像来自任意用户参考语料，不依赖固定作者枚举。
    assert pj["reference_basis"]["mode"] == "reference_derived"
    assert pj["reference_basis"]["fixed_author_allowlist"] is False
    assert pj["reference_basis"]["book_id"] == book_id
    assert pj["reference_basis"]["text_checksum"]
    # profile_json 4 类应用建议 + narrative_summary + metrics_baseline + scene_samples_index + sub_dimensions
    assert pj["narrative_summary"].startswith("量化基线")
    assert pj["qualitative_summary"] == "短句+反讽+冷静叙述,克制情感"
    assert "metrics_baseline" in pj
    assert "avg_sentence_length" in pj["metrics_baseline"]
    assert "paragraph_mean_chars" in pj["metrics_baseline"]
    assert "paragraphs_per_1k" in pj["metrics_baseline"]
    assert "scene_samples_index" in pj
    assert "sub_dimensions" in pj
    assert pj["style_features"] == ["善用短句", "反讽点缀", "白描+留白"]
    assert pj["banned_replication_rules"] == ["禁止堆砌华丽形容词"]
    # source_finding_ids_json 含所有 finding
    assert len(profile.source_finding_ids_json) == 2
    # W1 新键:叙事指引确定性派生(无 narrative.* forbidden 时等于 narrative_patterns),
    # 锚引文审计计数 = fixture 里唯一那条真实证据引文;满载之前的小 payload 不触发降级。
    assert pj["narrative_guidance"] == ["人物对话引出冲突", "环境暗示情绪"]
    assert pj["anchor_quotes_used"] == 1
    assert pj["synthesis_input_budget"]["degradation_stage"] == "full"
    assert pj["synthesis_input_budget"]["anchor_quote_count_after"] == 1


def test_synthesize_filters_reference_prose_from_generation_profile_fields() -> None:
    book_id, run_id = _ingest_with_finding("source_filter")
    copied = "这是一段叙述,介绍清晨场景。"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        forbidden = repo.get_finding("sr_find_source_filter_forbidden_pattern")
        forbidden.statement = copied
        session.commit()

    client = _fake_llm_with_response(
        {
            "profile_title": "安全画像",
            "narrative_summary": copied,
            "style_features": [copied, "用动作承担解释，减少抽象判断"],
            "narrative_patterns": [copied, "转折前先释放可见线索"],
            "banned_replication_rules": [copied, "不复用专名和独特意象"],
            "calibration_guidance": [copied, "若解释过多就改回动作"],
        }
    )
    with SessionLocal() as session:
        profile = ProfileSynthesizer(
            session,
            llm_client=client,
            llm_enabled=True,
        ).synthesize(book_id, run_id)
        session.commit()

    payload = profile.profile_json
    serialized_generation_fields = json.dumps(
        {
            "summary": payload["narrative_summary"],
            "features": payload["style_features"],
            "patterns": payload["narrative_patterns"],
            "rules": payload["banned_replication_rules"],
            "calibration": payload["calibration_guidance"],
            "forbidden": payload["generation_safe_forbidden_findings"],
        },
        ensure_ascii=False,
    )
    assert copied not in serialized_generation_fields
    assert payload["style_features"] == ["用动作承担解释，减少抽象判断"]
    assert payload["narrative_patterns"] == ["转折前先释放可见线索"]
    assert payload["source_overlap_filter"]["summary_replaced"] is True
    assert payload["source_overlap_filter"]["dropped_forbidden_finding_count"] == 1


def test_synthesize_pydantic_validation_failure() -> None:
    book_id, run_id = _ingest_with_finding("pyderr")
    # LLM 返回缺 narrative_summary,Pydantic min_length=1 不通过
    client = _fake_llm_with_response(
        {
            "profile_title": "x",
            "narrative_summary": "",  # 违反 min_length=1
            "style_features": [],
            "narrative_patterns": [],
            "banned_replication_rules": [],
        }
    )
    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=client, llm_enabled=True)
        with pytest.raises(SynthesizeError) as info:
            synth.synthesize(book_id, run_id)
    # 两次都无效 → 对外 reason_code=empty_profile,且是 409 DomainError(路由零改动即可透传)。
    assert isinstance(info.value, DomainError)
    assert info.value.status_code == 409
    assert info.value.code == "STYLE_REFERENCE_SYNTHESIZE_FAILED"
    assert info.value.details["reason_code"] == "empty_profile"
    assert info.value.details["author_action"]["action"] == "retry_synthesize"
    assert info.value.details["first_failure"]["reason_code"] == "invalid_or_empty_profile"


def test_synthesize_retries_one_invalid_empty_profile_then_succeeds() -> None:
    book_id, run_id = _ingest_with_finding("retryempty")
    client = _fake_llm_with_responses(
        [
            {
                "profile_title": "",
                "narrative_summary": "",
                "style_features": [],
                "narrative_patterns": [],
                "banned_replication_rules": [],
                "calibration_guidance": [],
            },
            {
                "profile_title": "克制叙事",
                "narrative_summary": "以动作和节奏推进信息，减少直接判断。",
                "style_features": ["用短句承接动作变化"],
                "narrative_patterns": ["转折前先释放可见线索"],
                "banned_replication_rules": [],
                "calibration_guidance": ["若解释过多，改回人物动作"],
            },
        ]
    )

    with SessionLocal() as session:
        profile = ProfileSynthesizer(
            session,
            llm_client=client,
            llm_enabled=True,
        ).synthesize(book_id, run_id)
        session.commit()

    assert len(client.requests) == 2
    assert "validation_retry" in client.requests[1].messages[-1]["content"]
    assert profile.profile_json["synthesis_attempts"]["attempt_count"] == 2
    assert profile.profile_json["synthesis_attempts"]["retried"] is True
    assert (
        profile.profile_json["synthesis_attempts"]["first_failure"]["reason_code"]
        == "invalid_or_empty_profile"
    )


def test_synthesize_retries_profile_with_broken_unicode_then_succeeds() -> None:
    book_id, run_id = _ingest_with_finding("retryunicode")
    client = _fake_llm_with_responses(
        [
            {
                "profile_title": "损坏画像",
                "narrative_summary": "减少文言词造成的阅读隔�0。",
                "style_features": ["使用具体动作"],
                "narrative_patterns": ["转折前先释放线索"],
                "banned_replication_rules": [],
                "calibration_guidance": [],
            },
            {
                "profile_title": "有效画像",
                "narrative_summary": "用具体动作推进信息，语域保持清楚。",
                "style_features": ["使用具体动作"],
                "narrative_patterns": ["转折前先释放线索"],
                "banned_replication_rules": [],
                "calibration_guidance": ["偏离时减少解释"],
            },
        ]
    )

    with SessionLocal() as session:
        profile = ProfileSynthesizer(
            session,
            llm_client=client,
            llm_enabled=True,
        ).synthesize(book_id, run_id)
        session.commit()

    attempts = profile.profile_json["synthesis_attempts"]
    assert len(client.requests) == 2
    assert attempts["attempt_count"] == 2
    assert attempts["first_failure"]["reason_code"] == "profile_text_integrity_invalid"
    assert attempts["first_failure"]["violations"] == [
        "narrative_summary:replacement_character"
    ]
    assert "�" not in json.dumps(profile.profile_json, ensure_ascii=False)


def test_synthesis_payload_budget_keeps_sub_dimension_coverage() -> None:
    dimensions = [f"layer.dimension_{index:02d}" for index in range(16)]
    rows = [
        {
            "sub_dimension": dimension,
            "finding_kind": kind,
            "statement": f"{dimension} 的可执行风格机制" + "具体动作节奏" * 12,
            "confidence": "high" if item_index == 0 else "medium",
            "status": "approved" if item_index == 0 else "pending",
            "evidence_count": 2,
        }
        for dimension in dimensions
        for kind in ("observation", "forbidden_pattern")
        for item_index in range(2)
    ]
    metrics = {
        f"metric_{index:02d}": {"mean": float(index), "std": 0.1}
        for index in range(40)
    }
    template = PromptTemplate(
        name="style_ref_synthesize_profile",
        version="test",
        input_token_budget=5000,
        system_prompt="聚合经过验证的抽象风格机制。",
        task_prompt="覆盖各个子维度后输出结构化画像。",
        structured_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["profile_title"],
            "properties": {"profile_title": {"type": "string"}},
        },
    )
    payload = {
        "book_title": "测试书",
        "sub_dimensions": {dimension: {} for dimension in dimensions},
        "metrics_baseline": metrics,
        "finding_summaries": rows,
    }

    fitted, audit = _fit_synthesis_payload_to_budget(payload, template)

    assert _estimate_synthesis_input_tokens(template, fitted) <= 5000
    assert audit["estimated_after"] <= audit["target_input_tokens"]
    assert audit["finding_count_after"] < audit["finding_count_before"]
    assert set(audit["covered_sub_dimensions"]) == set(dimensions)
    assert all(audit["selected_by_dimension"][dimension] >= 1 for dimension in dimensions)
    assert all(
        {"confidence", "status", "evidence_count"}.issubset(row)
        for row in fitted["finding_summaries"]
    )
    selected_counts = list(audit["selected_by_dimension"].values())
    assert max(selected_counts) - min(selected_counts) <= 1


def test_synthesize_rejects_empty_style_features() -> None:
    """style_features / narrative_patterns 是注入素材,为空的 Profile 必须硬失败。

    banned_replication_rules / calibration_guidance 保持宽松(合法可为空)。
    """
    book_id, run_id = _ingest_with_finding("emptyfeat")
    client = _fake_llm_with_response(
        {
            "profile_title": "t",
            "narrative_summary": "其余字段全部合法的画像简述",
            "style_features": [],  # 违反 min_length=1
            "narrative_patterns": ["p"],
            "banned_replication_rules": [],
            "calibration_guidance": [],
        }
    )
    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=client, llm_enabled=True)
        with pytest.raises(SynthesizeError):
            synth.synthesize(book_id, run_id)


def test_synthesize_excludes_rejected_findings() -> None:
    """PR-23 — rejected finding 不进 LLM 聚合 payload,也不进 source_finding_ids_json。"""
    book_id, run_id = _ingest_with_finding("rej")
    rejected_statement = "被驳回的观察描述不应出现在聚合里"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_finding(
            finding_id="sr_find_rej_rejected",
            book_id=book_id,
            run_id=run_id,
            extraction_id="sr_ext_rej",
            sub_dimension="language.rhetoric",
            finding_kind="observation",
            statement=rejected_statement,
            confidence="high",
            status="rejected",
        )
        session.commit()

    captured: list[str] = []
    response_dict = {
        "profile_title": "t",
        "narrative_summary": "s",
        "style_features": ["f"],
        "narrative_patterns": ["p"],
        "banned_replication_rules": ["b"],
    }

    class _Resp:
        structured_output = response_dict
        text = json.dumps(response_dict, ensure_ascii=False)
        usage = {}
        finish_reason = "stop"
        provider = "fake"
        model = "fake"
        response_format = "json_object"
        request_id = None
        raw_response = {}

    class _CapturingClient(AccountedGenerateMixin):
        def generate(self, request):  # noqa: ANN001
            captured.append(request.messages[-1]["content"])
            return _Resp()

    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=_CapturingClient(), llm_enabled=True)
        profile = synth.synthesize(book_id, run_id)
        session.commit()

    # rejected statement 不进 finding_summaries;证据原文只允许以 anchor_quotes 的
    # 机制锚点身份出现(2026-09 v2),绝不混进 finding_summaries 的 statement。
    assert captured and rejected_statement not in captured[0]
    sent = _payload_from_prompt(captured[0])
    assert [item["quote"] for item in sent["anchor_quotes"]] == ["他低头看着脚下的路"]
    assert all(
        "他低头看着脚下的路" not in row["statement"] for row in sent["finding_summaries"]
    )
    # finding_id 不在 source_finding_ids_json;原 2 条 pending finding 保留
    assert "sr_find_rej_rejected" not in profile.source_finding_ids_json
    assert len(profile.source_finding_ids_json) == 2


def test_synthesize_scene_samples_index_buckets_by_paragraph_type() -> None:
    """scene_samples_index 按段落表实测类型分桶;合成反例/负空间/无段落锚点不入索引。

    2026-07 起 quotes 为 run-scoped(仅本 run findings 经 evidence 关联的引文),
    故本用例把每条 quote 经 evidence 挂到 run 的 finding 上——与真实抽取管线一致
    (extractor 落库时 quote 总是伴随 evidence 行)。
    """
    book_id, run_id = _ingest_with_finding("buckets")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        paras = repo.list_paragraphs(book_id)
        assert len(paras) >= 2
        p_dlg, p_env = paras[0], paras[1]
        p_dlg.paragraph_type = "dialogue"
        p_env.paragraph_type = "description_env"
        obs_finding = f"sr_find_buckets_observation"
        fp_finding = f"sr_find_buckets_forbidden_pattern"
        # 真实原文引文(带 anchor_kind)→ 按段落表类型分桶
        repo.create_quote(
            quote_id="sr_quote_dlg_buckets",
            book_id=book_id,
            paragraph_id=p_dlg.paragraph_id,
            span_start=0,
            span_end=10,
            quote_text="对话引文",
            illustrates_dims=[],
            extracted_features={"anchor_kind": "paragraph_quote"},
        )
        repo.create_evidence(
            evidence_id="sr_ev_dlg_buckets",
            finding_id=obs_finding,
            quote_id="sr_quote_dlg_buckets",
            anchor_kind="paragraph_quote",
            is_synthetic=0,
        )
        # legacy 行(无 anchor_kind)→ 默认按 paragraph_quote 处理,仍按段落表分桶
        repo.create_quote(
            quote_id="sr_quote_env_buckets",
            book_id=book_id,
            paragraph_id=p_env.paragraph_id,
            span_start=0,
            span_end=10,
            quote_text="环境引文",
            illustrates_dims=[],
            extracted_features={},
        )
        repo.create_evidence(
            evidence_id="sr_ev_env_buckets",
            finding_id=obs_finding,
            quote_id="sr_quote_env_buckets",
            anchor_kind="paragraph_quote",
            is_synthetic=0,
        )
        # 合成反例:与原作风格相悖,不能作为风格样例进 few-shot 索引
        repo.create_quote(
            quote_id="sr_quote_syn_buckets",
            book_id=book_id,
            paragraph_id=None,
            span_start=0,
            span_end=10,
            quote_text="(反例)天是那样蓝",
            illustrates_dims=[],
            extracted_features={"anchor_kind": "counter_example"},
        )
        repo.create_evidence(
            evidence_id="sr_ev_syn_buckets",
            finding_id=fp_finding,
            quote_id="sr_quote_syn_buckets",
            anchor_kind="counter_example",
            is_synthetic=1,
        )
        session.commit()

    client = _fake_llm_with_response(
        {
            "profile_title": "t",
            "narrative_summary": "s",
            "style_features": ["f"],
            "narrative_patterns": ["p"],
            "banned_replication_rules": ["b"],
        }
    )
    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=client, llm_enabled=True)
        profile = synth.synthesize(book_id, run_id)
        session.commit()
    index = profile.profile_json["scene_samples_index"]
    assert "sr_quote_dlg_buckets" in index.get("dialogue", [])
    assert "sr_quote_env_buckets" in index.get("description_env", [])
    flat = [qid for ids in index.values() for qid in ids]
    assert "sr_quote_syn_buckets" not in flat
    # helper 里 paragraph_id=None 的旧式 quote 同样不入索引
    assert "sr_quote_buckets" not in flat


# ---------------------------------------------------------------------------
# 2026-09 风格模仿 v2 · W1:完整抽取进得去、出得来
# ---------------------------------------------------------------------------


_FULL_LOAD_DIMENSIONS = [
    f"{layer}.dim_{index:02d}"
    for layer in ("language", "narrative", "scene", "theme")
    for index in range(4)
]


def _full_load_payload(
    *,
    observations_per_dimension: int = 6,
    forbidden_per_dimension: int = 2,
    statement_chars: int = 120,
) -> dict:
    """16 子维 × (6 obs + 2 fp) × 120 字 + 31 项 metrics + 锚引文 2×60 字/维 + 12 行声音习惯。"""
    rows = [
        {
            "sub_dimension": dimension,
            "finding_kind": kind,
            "statement": (f"{dimension}{kind}{index}" + "机制陈述具体动作节奏" * 20)[
                :statement_chars
            ],
            "confidence": "high" if index == 0 else ("medium" if index % 2 else "low"),
            "status": "approved" if index == 0 else "pending",
            "evidence_count": 4 - min(index, 2),
        }
        for dimension in _FULL_LOAD_DIMENSIONS
        for kind, count in (
            ("observation", observations_per_dimension),
            ("forbidden_pattern", forbidden_per_dimension),
        )
        for index in range(count)
    ]
    return {
        "book_title": "测试书名",
        "sub_dimensions": {
            dimension: {
                "observation_count": observations_per_dimension,
                "forbidden_pattern_count": forbidden_per_dimension,
                "quote_count": 4,
                "confidence": "high",
            }
            for dimension in _FULL_LOAD_DIMENSIONS
        },
        "metrics_baseline": {
            name: {"mean": 12.3456, "std": 1.2345}
            for name in (*METRIC_NAMES, *PROSE_SHAPE_METRIC_NAMES)
        },
        "voice_habits": [
            (f"习惯{index}:常用连接词却便又;少用然而于是" * 3)[:40] for index in range(12)
        ],
        "anchor_quotes": [
            {"sub_dimension": dimension, "quote": (f"{dimension}引文锚点样例文字" * 10)[:60]}
            for dimension in _FULL_LOAD_DIMENSIONS
            for _ in range(2)
        ],
        "finding_summaries": rows,
    }


def _template_with_budget(budget: int) -> PromptTemplate:
    real = load_prompt_templates()["style_ref_synthesize_profile"]
    return PromptTemplate(
        name=real.name,
        version=real.version,
        input_token_budget=budget,
        system_prompt=real.system_prompt,
        task_prompt=real.task_prompt,
        structured_schema=real.structured_schema,
    )


def _kinds_per_dimension(rows: list[dict]) -> dict[str, set[str]]:
    kinds: dict[str, set[str]] = {}
    for row in rows:
        kinds.setdefault(row["sub_dimension"], set()).add(row["finding_kind"])
    return kinds


def test_full_extraction_payload_fits_the_real_template_without_degradation() -> None:
    """§3 验收:16 子维 × 6+2 条 × 120 字 payload 用真实模板不失败,且一条不丢、一字不压。"""
    template = load_prompt_templates()["style_ref_synthesize_profile"]
    payload = _full_load_payload()

    fitted, audit = _fit_synthesis_payload_to_budget(payload, template)

    assert audit["applied"] is False
    assert audit["degradation_stage"] == "full"
    assert audit["statement_max_chars"] == 120
    assert audit["estimated_after"] <= audit["target_input_tokens"] == template.input_token_budget
    # 预算留有 ≥20% 余量(模板注释的测量依据)。
    assert audit["estimated_after"] * 1.2 <= template.input_token_budget
    assert audit["finding_count_after"] == audit["finding_count_before"] == 16 * 8
    assert audit["metric_count_after"] == audit["metric_count_before"] == len(METRIC_NAMES) + len(PROSE_SHAPE_METRIC_NAMES)
    assert audit["anchor_quote_count_after"] == audit["anchor_quote_count_before"] == 32
    assert audit["protected_finding_count"] == 32
    assert set(audit["covered_sub_dimensions"]) == set(_FULL_LOAD_DIMENSIONS)
    assert all(count == 8 for count in audit["selected_by_dimension"].values())
    assert all(len(row["statement"]) == 120 for row in fitted["finding_summaries"])
    assert fitted["voice_habits"] == payload["voice_habits"]
    assert fitted["anchor_quotes"] == payload["anchor_quotes"]
    # 送模型的顺序:按子维分组、组内 approved/high 在前。
    sent_dimensions = [row["sub_dimension"] for row in fitted["finding_summaries"]]
    assert sent_dimensions == sorted(sent_dimensions)
    first_rows = [row for row in fitted["finding_summaries"] if row["status"] == "approved"]
    assert len(first_rows) == 32
    assert _estimate_synthesis_input_tokens(template, fitted) == audit["estimated_after"]


def test_budget_ladder_degrades_globally_and_protects_every_dimension() -> None:
    """分级降级:② 非必需指标 → ③ 全体 statement 80/56 → ④ 锚引文 2→1→0 → ⑤ 轮转丢弃。"""
    payload = _full_load_payload()
    generous = _template_with_budget(10**6)
    estimate = lambda candidate: _estimate_synthesis_input_tokens(generous, candidate)  # noqa: E731
    required_only = {
        name: value
        for name, value in payload["metrics_baseline"].items()
        if name in _SYNTHESIS_REQUIRED_METRICS
    }
    assert 0 < len(required_only) < len(payload["metrics_baseline"])

    def reduced(*, metrics=None, statement_chars=120, anchors=None, rows=None) -> dict:
        return {
            **payload,
            "metrics_baseline": payload["metrics_baseline"] if metrics is None else metrics,
            "anchor_quotes": payload["anchor_quotes"] if anchors is None else anchors,
            "finding_summaries": [
                {**row, "statement": row["statement"][:statement_chars]}
                for row in (payload["finding_summaries"] if rows is None else rows)
            ],
        }

    full_estimate = estimate(payload)
    metrics_floor = estimate(reduced(metrics=required_only))
    statement_floor = estimate(reduced(metrics=required_only, statement_chars=56))
    anchor_floor = estimate(reduced(metrics=required_only, statement_chars=56, anchors=[]))
    protected_rows = [
        row
        for row in payload["finding_summaries"]
        if row["status"] == "approved"  # 每子维 index==0 的 obs 与 fp
    ]
    assert len(protected_rows) == 32
    finding_floor = estimate(
        reduced(metrics=required_only, statement_chars=56, anchors=[], rows=protected_rows)
    )
    assert full_estimate > metrics_floor > statement_floor > anchor_floor > finding_floor

    # ② 只差一点:只丢最少的非必需指标,必需项与全部 finding 原样保留。
    fitted, audit = _fit_synthesis_payload_to_budget(
        payload, _template_with_budget(full_estimate - 1)
    )
    assert audit["degradation_stage"] == "drop_optional_metrics"
    assert set(_SYNTHESIS_REQUIRED_METRICS) <= set(fitted["metrics_baseline"])
    assert audit["metric_count_after"] < audit["metric_count_before"]
    assert audit["finding_count_after"] == 128 and audit["statement_max_chars"] == 120
    assert audit["anchor_quote_count_after"] == 32

    # ③ 丢光非必需指标仍不够:全体 statement 统一压缩,finding 与锚引文都不丢。
    fitted, audit = _fit_synthesis_payload_to_budget(
        payload, _template_with_budget(metrics_floor - 1)
    )
    assert audit["degradation_stage"] == "compress_statements"
    assert audit["statement_max_chars"] in {80, 56}
    assert set(fitted["metrics_baseline"]) == set(required_only)
    assert audit["finding_count_after"] == 128
    assert all(len(row["statement"]) <= 80 for row in fitted["finding_summaries"])
    assert audit["anchor_quote_count_after"] == 32

    # ④ 压到 56 字仍不够:锚引文每子维 2→1→0,finding 仍一条不丢。
    fitted, audit = _fit_synthesis_payload_to_budget(
        payload, _template_with_budget(statement_floor - 1)
    )
    assert audit["degradation_stage"] == "trim_anchor_quotes"
    assert audit["anchor_quote_count_after"] < 32
    assert audit["finding_count_after"] == 128
    assert audit["statement_max_chars"] == 56

    # ⑤ 锚引文清空仍不够:轮转丢弃,但每子维 ≥1 obs 且 ≥1 fp,且子维间保留数最多差 1。
    fitted, audit = _fit_synthesis_payload_to_budget(
        payload, _template_with_budget(anchor_floor - 1)
    )
    assert audit["degradation_stage"] == "drop_findings"
    assert audit["anchor_quote_count_after"] == 0
    assert 32 <= audit["finding_count_after"] < 128
    kinds = _kinds_per_dimension(fitted["finding_summaries"])
    assert set(kinds) == set(_FULL_LOAD_DIMENSIONS)
    assert all({"observation", "forbidden_pattern"} <= kinds[d] for d in _FULL_LOAD_DIMENSIONS)
    counts = list(audit["selected_by_dimension"].values())
    assert max(counts) - min(counts) <= 1
    # 被保护的 approved/high 条目一条都不能丢。
    kept_statements = {row["statement"] for row in fitted["finding_summaries"]}
    assert all(row["statement"][:56] in kept_statements for row in protected_rows)
    assert audit["estimated_after"] <= audit["target_input_tokens"]

    # 连"每子维 1 obs + 1 fp"的地板都装不下 → 明确失败,reason_code=budget_unfit。
    with pytest.raises(SynthesizeError) as info:
        _fit_synthesis_payload_to_budget(payload, _template_with_budget(finding_floor - 1))
    assert info.value.details["reason_code"] == "budget_unfit"
    assert info.value.status_code == 409
    assert info.value.details["author_action"]["action"] == (
        "review_dimension_matrix_then_synthesize"
    )
    assert info.value.details["protected_finding_count"] == 32


def test_budget_ladder_never_drops_required_metrics_even_when_dropping_findings() -> None:
    payload = _full_load_payload()
    generous = _template_with_budget(10**6)
    protected_rows = [row for row in payload["finding_summaries"] if row["status"] == "approved"]
    floor_payload = {
        **payload,
        "metrics_baseline": {
            name: value
            for name, value in payload["metrics_baseline"].items()
            if name in _SYNTHESIS_REQUIRED_METRICS
        },
        "anchor_quotes": [],
        "finding_summaries": [{**row, "statement": row["statement"][:56]} for row in protected_rows],
    }
    floor = _estimate_synthesis_input_tokens(generous, floor_payload)

    fitted, audit = _fit_synthesis_payload_to_budget(payload, _template_with_budget(floor))

    assert audit["degradation_stage"] == "drop_findings"
    assert set(fitted["metrics_baseline"]) == set(_SYNTHESIS_REQUIRED_METRICS)
    assert audit["finding_count_after"] == 32
    assert audit["estimated_after"] <= floor


def test_required_metrics_align_with_injection_metric_anchor_order() -> None:
    """合成侧"必需指标"必须与注入侧量化锚(injection._METRIC_REQUIRED_ORDER)保持同一集合。"""
    injection_order = getattr(injection_module, "_METRIC_REQUIRED_ORDER", None)
    if injection_order is None:
        pytest.skip("injection._METRIC_REQUIRED_ORDER not present in this build")
    assert set(_SYNTHESIS_REQUIRED_METRICS) == set(injection_order)


def test_anchor_quotes_payload_limits_trims_and_excludes_synthetic_evidence() -> None:
    dimension = "scene.dialogue"
    findings = [
        SimpleNamespace(
            finding_id="f_weak",
            sub_dimension=dimension,
            finding_kind="observation",
            statement="弱",
            confidence="low",
            status="pending",
        ),
        SimpleNamespace(
            finding_id="f_strong",
            sub_dimension=dimension,
            finding_kind="observation",
            statement="强",
            confidence="high",
            status="approved",
        ),
        SimpleNamespace(
            finding_id="f_mid",
            sub_dimension=dimension,
            finding_kind="forbidden_pattern",
            statement="中",
            confidence="medium",
            status="pending",
        ),
        SimpleNamespace(
            finding_id="f_other",
            sub_dimension="language.rhythm",
            finding_kind="observation",
            statement="它",
            confidence="high",
            status="approved",
        ),
    ]
    long_quote = (
        "第一句说到窄巷尽头的灰墙便停住了。第二句继续往下说了很多很多的话一直没有停下来,"
        "像檐水一样滴到天亮。第三句又开始了新的内容并且很长,一直拖到了段落末尾。"
    )
    assert len(long_quote) > 60
    quotes = [
        SimpleNamespace(quote_id="q_long", quote_text=long_quote, extracted_features={}),
        SimpleNamespace(quote_id="q_mid", quote_text="中等长度的真实引文", extracted_features={}),
        SimpleNamespace(quote_id="q_weak", quote_text="最弱 finding 的引文", extracted_features={}),
        SimpleNamespace(
            quote_id="q_counter",
            quote_text="(反例)天是那样蓝",
            extracted_features={"anchor_kind": "counter_example"},
        ),
        SimpleNamespace(quote_id="q_other", quote_text="另一维度的引文", extracted_features={}),
    ]
    evidences = [
        SimpleNamespace(evidence_id="e1", finding_id="f_strong", quote_id="q_long", anchor_kind="paragraph_quote", is_synthetic=0, created_at="1"),
        SimpleNamespace(evidence_id="e2", finding_id="f_strong", quote_id="q_mid", anchor_kind="paragraph_quote", is_synthetic=0, created_at="2"),
        SimpleNamespace(evidence_id="e3", finding_id="f_mid", quote_id="q_counter", anchor_kind="counter_example", is_synthetic=1, created_at="1"),
        SimpleNamespace(evidence_id="e4", finding_id="f_mid", quote_id="q_mid", anchor_kind="paragraph_quote", is_synthetic=0, created_at="2"),
        SimpleNamespace(evidence_id="e5", finding_id="f_weak", quote_id="q_weak", anchor_kind="paragraph_quote", is_synthetic=0, created_at="1"),
        SimpleNamespace(evidence_id="e6", finding_id="f_other", quote_id="q_other", anchor_kind="paragraph_quote", is_synthetic=0, created_at="1"),
    ]

    anchors = _build_anchor_quotes_payload(findings, quotes, evidences)

    by_dimension: dict[str, list[str]] = {}
    for item in anchors:
        by_dimension.setdefault(item["sub_dimension"], []).append(item["quote"])
    assert set(by_dimension) == {dimension, "language.rhythm"}
    assert len(by_dimension[dimension]) == _ANCHOR_QUOTES_PER_DIMENSION == 2
    # 最强 finding 先出 1 条(长引文截到 ≤60 字且落在句末标点),再轮到下一个 finding。
    first, second = by_dimension[dimension]
    assert len(first) <= 60 and first.endswith("。") and long_quote.startswith(first)
    assert second == "中等长度的真实引文"
    # 合成反例、第三个 finding 的引文都不进锚点;每条 ≤60 字。
    flat = [item["quote"] for item in anchors]
    assert "(反例)天是那样蓝" not in flat and "最弱 finding 的引文" not in flat
    assert all(len(quote) <= 60 for quote in flat)
    assert by_dimension["language.rhythm"] == ["另一维度的引文"]


def test_narrative_guidance_derivation_dedupes_filters_and_caps() -> None:
    corpus = ["这是一段叙述,介绍清晨场景。他说今天天气真好。"]
    patterns = [
        "关键信息放段首一次给出,之后不回头解释",
        "  关键信息放段首一次给出,之后不回头解释 ",  # 空白差异 → 去重
        "转折前先释放可见线索",
    ]
    forbidden = [
        {"sub_dimension": "narrative.pacing", "statement": "不用连续回忆段拖慢当下动作"},
        {"sub_dimension": "narrative.pacing", "statement": "不用连续回忆段拖慢当下动作。"},  # 标点差异 → 去重
        {"sub_dimension": "narrative.perspective", "statement": "这是一段叙述,介绍清晨场景。"},  # 原文重合 → 过滤
        {"sub_dimension": "language.rhetoric", "statement": "不堆叠三个以上排比"},  # 非 narrative.* → 不收
        *[
            {"sub_dimension": "narrative.time_handling", "statement": f"时间处理机制第{index}条"}
            for index in range(10)
        ],
    ]

    guidance = _derive_narrative_guidance(patterns, forbidden, corpus)

    assert guidance[:3] == [
        "关键信息放段首一次给出,之后不回头解释",
        "转折前先释放可见线索",
        "不用连续回忆段拖慢当下动作",
    ]
    assert "这是一段叙述,介绍清晨场景。" not in guidance
    assert "不堆叠三个以上排比" not in guidance
    # 正面措辞的 forbidden 陈述带「避免：」极性标记进入指引
    assert guidance[3:] == [f"避免：时间处理机制第{index}条" for index in range(5)]
    assert len(guidance) == 8


def test_narrative_guidance_keeps_forbidden_polarity_with_avoid_marker() -> None:
    """C17:narrative.* forbidden_pattern 的陈述命名的是作者明确不用的模式;平铺进
    narrative_guidance 时必须带「避免：」,否则 neutral_draft / scene_blueprint 会把它
    当成要采用的机制。已否定的陈述不叠加;原文重合过滤按陈述原文判,标记救不回来。"""
    corpus = ["这是一段叙述,介绍清晨场景。他说今天天气真好。"]
    patterns = ["关键信息放段首一次给出,之后不回头解释"]
    forbidden = [
        {"sub_dimension": "narrative.perspective", "statement": "以全知旁白直接解释人物动机"},
        {"sub_dimension": "narrative.perspective", "statement": " 以全知旁白直接解释人物动机。"},  # 标点差异 → 去重
        {"sub_dimension": "narrative.pacing", "statement": "不用连续回忆段拖慢当下动作"},  # 已否定 → 原样
        {"sub_dimension": "narrative.perspective", "statement": "这是一段叙述,介绍清晨场景。"},  # 原文重合 → 过滤
        {"sub_dimension": "language.rhetoric", "statement": "堆叠三个以上排比"},  # 非 narrative.* → 不收
    ]

    guidance = _derive_narrative_guidance(patterns, forbidden, corpus)

    assert guidance == [
        "关键信息放段首一次给出,之后不回头解释",
        "避免：以全知旁白直接解释人物动机",
        "不用连续回忆段拖慢当下动作",
    ]
    assert not any(line.startswith("以全知旁白") for line in guidance)
    assert not any("介绍清晨场景" in line for line in guidance)
    assert not any("排比" in line for line in guidance)
    # 渲染到 "Narrative Mechanisms" 后每行自带方向
    rendered = render_narrative_section(guidance).split("\n")
    assert "- 避免：以全知旁白直接解释人物动机" in rendered
    assert "- 以全知旁白直接解释人物动机" not in rendered


def test_synthesize_writes_narrative_guidance_from_patterns_and_narrative_forbidden() -> None:
    book_id, run_id = _ingest_with_finding("narrguide")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_finding(
            finding_id="sr_find_narrguide_narr_fp",
            book_id=book_id,
            run_id=run_id,
            extraction_id="sr_ext_narrguide",
            sub_dimension="narrative.pacing",
            finding_kind="forbidden_pattern",
            statement="不用连续回忆段拖慢当下动作",
            confidence="high",
            status="approved",
        )
        # 正面措辞的 forbidden(命名模式本身)→ 入画像时带「避免：」极性标记
        repo.create_finding(
            finding_id="sr_find_narrguide_narr_fp_positive",
            book_id=book_id,
            run_id=run_id,
            extraction_id="sr_ext_narrguide",
            sub_dimension="narrative.perspective",
            finding_kind="forbidden_pattern",
            statement="以全知旁白直接解释人物动机",
            confidence="high",
            status="approved",
        )
        session.commit()
    client, captured = _capturing_llm(
        {
            "profile_title": "克制叙事",
            "narrative_summary": "以动作和节奏推进信息,减少直接判断。",
            "style_features": ["用短句承接动作变化"],
            "narrative_patterns": ["转折前先释放可见线索", "不用连续回忆段拖慢当下动作"],
            "banned_replication_rules": [],
            "calibration_guidance": ["若解释过多,改回人物动作"],
        }
    )
    with SessionLocal() as session:
        profile = ProfileSynthesizer(session, llm_client=client, llm_enabled=True).synthesize(
            book_id, run_id
        )
        session.commit()

    pj = profile.profile_json
    # narrative_patterns ∪ narrative.* forbidden(与 pattern 重复的只留一条;正面措辞的
    # forbidden 带「避免：」极性标记);language.* 的 fp 不收。
    assert pj["narrative_guidance"] == [
        "转折前先释放可见线索",
        "不用连续回忆段拖慢当下动作",
        "避免：以全知旁白直接解释人物动机",
    ]
    assert "以全知旁白直接解释人物动机" not in pj["narrative_guidance"]
    assert "测试 forbidden_pattern 描述" not in pj["narrative_guidance"]
    assert len(pj["narrative_guidance"]) <= 8
    sent = _payload_from_prompt(captured[0])
    assert {row["sub_dimension"] for row in sent["finding_summaries"]} == {
        "language.rhetoric",
        "narrative.pacing",
        "narrative.perspective",
    }


def test_synthesize_writes_voice_signature_and_sends_voice_habits(monkeypatch) -> None:
    _install_fake_voice_signature(
        monkeypatch,
        habits=[f"习惯{index}" for index in range(15)],  # 15 行 → 截到 ≤12
    )
    book_id, run_id = _ingest_with_finding("voicesig")
    client, captured = _capturing_llm(
        {
            "profile_title": "t",
            "narrative_summary": "s",
            "style_features": ["f"],
            "narrative_patterns": ["p"],
            "banned_replication_rules": [],
            "calibration_guidance": [],
        }
    )
    with SessionLocal() as session:
        profile = ProfileSynthesizer(session, llm_client=client, llm_enabled=True).synthesize(
            book_id, run_id
        )
        session.commit()

    signature = profile.profile_json["voice_signature"]
    assert signature["version"] == "voice_signature_v1"
    assert signature["deliberate_repetition"] is False
    assert signature["features"]["comma_per_sentence"] == 1.5
    assert signature["features"]["text_count"] > 0
    assert signature["habits"] == [f"习惯{index}" for index in range(12)]
    sent = _payload_from_prompt(captured[0])
    assert sent["voice_habits"] == signature["habits"]


def test_synthesize_omits_voice_signature_when_module_missing(monkeypatch) -> None:
    # sys.modules[name] = None 让 import 抛 ImportError,无论 W3 是否已落地。
    monkeypatch.setitem(sys.modules, VOICE_SIGNATURE_MODULE, None)
    book_id, run_id = _ingest_with_finding("voicemissing")
    client, captured = _capturing_llm(
        {
            "profile_title": "t",
            "narrative_summary": "s",
            "style_features": ["f"],
            "narrative_patterns": ["p"],
            "banned_replication_rules": [],
        }
    )
    with SessionLocal() as session:
        profile = ProfileSynthesizer(session, llm_client=client, llm_enabled=True).synthesize(
            book_id, run_id
        )
        session.commit()

    assert "voice_signature" not in profile.profile_json
    assert _payload_from_prompt(captured[0])["voice_habits"] == []


def test_synthesize_survives_voice_signature_runtime_failure(monkeypatch, caplog) -> None:
    _install_fake_voice_signature(monkeypatch, raise_error=RuntimeError("boom"))
    book_id, run_id = _ingest_with_finding("voiceboom")
    client, _ = _capturing_llm(
        {
            "profile_title": "t",
            "narrative_summary": "s",
            "style_features": ["f"],
            "narrative_patterns": ["p"],
            "banned_replication_rules": [],
        }
    )
    with SessionLocal() as session, caplog.at_level("WARNING"):
        profile = ProfileSynthesizer(session, llm_client=client, llm_enabled=True).synthesize(
            book_id, run_id
        )
        session.commit()

    assert profile.status == "draft"
    assert "voice_signature" not in profile.profile_json
    assert any("voice signature" in record.getMessage() for record in caplog.records)


def test_synthesize_error_is_a_409_domain_error_with_closed_reason_codes() -> None:
    assert SYNTHESIZE_REASON_CODES == {
        "budget_unfit",
        "empty_profile",
        "source_overlap",
        "text_integrity",
        "llm_failed",
    }
    for reason_code in sorted(SYNTHESIZE_REASON_CODES):
        exc = SynthesizeError("x", reason_code=reason_code, details={"extra": 1})
        assert isinstance(exc, DomainError)
        assert exc.status_code == 409
        assert exc.code == "STYLE_REFERENCE_SYNTHESIZE_FAILED"
        assert exc.reason_code == reason_code
        assert exc.details["reason_code"] == reason_code
        assert exc.details["extra"] == 1
        assert exc.details["author_action"]["view"] == "styleref"
        assert exc.details["author_action"]["label"]
    # 默认 llm_failed;非法 reason_code 立即报错而不是静默进信封。
    assert SynthesizeError("llm").reason_code == "llm_failed"
    with pytest.raises(ValueError):
        SynthesizeError("bad", reason_code="unknown")
    # 子类沿用同一映射。
    integrity = ProfileTextIntegrityError(["narrative_summary:replacement_character"])
    assert isinstance(integrity, SynthesizeError)
    assert integrity.details["reason_code"] == "text_integrity"
    assert integrity.details["violations"] == ["narrative_summary:replacement_character"]


def test_synthesize_source_overlap_failure_maps_to_source_overlap_reason() -> None:
    book_id, run_id = _ingest_with_finding("overlapreason")
    copied = "这是一段叙述,介绍清晨场景。"
    response = {
        "profile_title": "画像",
        "narrative_summary": "简述",
        "style_features": [copied],
        "narrative_patterns": [copied],
        "banned_replication_rules": [],
        "calibration_guidance": [],
    }
    client = _fake_llm_with_responses([response, response])
    with SessionLocal() as session:
        with pytest.raises(SynthesizeError) as info:
            ProfileSynthesizer(session, llm_client=client, llm_enabled=True).synthesize(
                book_id, run_id
            )
    assert info.value.details["reason_code"] == "source_overlap"
    assert info.value.details["first_failure"]["reason_code"] == (
        "source_overlap_removed_required_content"
    )


def test_synthesize_llm_node_failure_maps_to_llm_failed_reason() -> None:
    book_id, run_id = _ingest_with_finding("llmfail")

    class _Broken(AccountedGenerateMixin):
        def generate(self, request):  # noqa: ANN001
            raise RuntimeError("provider exploded")

    with SessionLocal() as session:
        with pytest.raises(SynthesizeError) as info:
            ProfileSynthesizer(session, llm_client=_Broken(), llm_enabled=True).synthesize(
                book_id, run_id
            )
    assert info.value.details["reason_code"] == "llm_failed"
    assert info.value.details["author_action"]["action"] == "retry_synthesize"


def test_synthesize_missing_book_is_a_404_not_a_synthesis_failure() -> None:
    with SessionLocal() as session:
        synth = ProfileSynthesizer(session, llm_client=object(), llm_enabled=True)
        with pytest.raises(DomainError) as info:
            synth.synthesize("sr_book_missing", "sr_run_missing")
    assert not isinstance(info.value, SynthesizeError)
    assert info.value.status_code == 404
    assert info.value.code == "STYLE_REFERENCE_BOOK_NOT_FOUND"


def test_rendered_prompt_never_leaks_anchor_quotes_into_generation_fields() -> None:
    """锚引文进 payload 只是给模型看;模型若复述回来,输出侧过滤照旧生效。"""
    book_id, run_id = _ingest_with_finding("anchorleak")
    anchor = "他低头看着脚下的路"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        paragraphs = repo.list_paragraphs(book_id)
        # 把锚引文写进段落语料,使其成为"原文";模型把它抄进 style_features 时必须被删。
        paragraphs[0].text = paragraphs[0].text + anchor
        session.commit()
    client, captured = _capturing_llm(
        {
            "profile_title": "画像",
            "narrative_summary": "简述",
            "style_features": [anchor, "用动作承担解释"],
            "narrative_patterns": ["转折前先释放可见线索"],
            "banned_replication_rules": [],
            "calibration_guidance": [],
        }
    )
    with SessionLocal() as session:
        profile = ProfileSynthesizer(session, llm_client=client, llm_enabled=True).synthesize(
            book_id, run_id
        )
        session.commit()

    sent = _payload_from_prompt(captured[0])
    assert anchor in [item["quote"] for item in sent["anchor_quotes"]]
    template = load_prompt_templates()["style_ref_synthesize_profile"]
    rendered = render_untrusted_user_prompt(
        template.task_prompt,
        UntrustedPayload(sent),
        kind="style_ref_synthesize_profile",
    )
    assert "anchor_quotes 只作机制锚点" in rendered
    assert profile.profile_json["style_features"] == ["用动作承担解释"]
    assert profile.profile_json["anchor_quotes_used"] == 1
