"""风格参考模块加固回归(2026-06 审查修复)。

覆盖六组新行为:
1. cloud_policy 强制执行(local_only 拒绝云端 LLM 操作;严格 LLM 后没有启发式兜底,分类节点须走本机模型)
2. LLMRequiredError / CloudPolicyBlockedError 映射 DomainError 409
3. 反抄袭红线段(anti_plagiarism_block)接线:渲染 / banned_terms 填充 / 免截断
4. 抄袭检测:规范化匹配(防标点空格绕过)+ 全书段落语料
5. (2026-09-23 风格参考 v3 P5b:旧校验层的同步量化裁决 / semantic 降级封顶随校验层删除;
   严格 LLM 与 cloud_policy 在「对照检查」作业的建作业处照样把关)
6. 孤儿 pending report 回收 + 上传体积上限(旧抽取 run 的僵尸回收随 RunOrchestrator 删除;学习作业的续跑见 test_style_reference_learn_job)
"""

from __future__ import annotations

import io
import time

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import (
    CloudPolicyBlockedError,
    LLMRequiredError,
)
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_policy import style_policy_live
from novel_system.services.style_reference.inject.fit import fit_rendered
from novel_system.services.style_reference.inject.render import render_style
from novel_system.services.style_reference.inject.request import StyleRenderRequest
from novel_system.services.style_reference.repository import StyleReferenceRepository
from tests.style_reference_factories import RIGHTS_STATS, make_book, make_profile
from novel_system.services.reference_copy_gate import (
    check_reference_copy,
    reset_reference_copy_gate_cache,
)
from novel_system.services.style_reference.check_job import start_check_job
from novel_system.services.style_reference.validation import check_plagiarism


@pytest.fixture(autouse=True)
def _clear_corpus_cache():
    reset_reference_copy_gate_cache()
    yield
    reset_reference_copy_gate_cache()


class _SentinelLLM:
    """任何调用都视为违规的 LLM client 哨兵。"""

    def __init__(self) -> None:
        self.called = False

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            self.called = True
            raise AssertionError(f"LLM client must not be called (method {name})")

        return _boom


SAMPLE_TEXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

雪花从天空缓缓飘落,落在他的肩头,他没有拂去。
"""


def _seed_book(
    seed: str,
    *,
    cloud_policy: str = "local_only",
    with_paragraphs: bool = True,
) -> str:
    with SessionLocal() as session:
        book_id = make_book(
            session,
            f"sr_book_hd_{seed}",
            cloud_policy=cloud_policy,
            text_checksum=f"chk_hd_{seed}",
            total_chars=len(SAMPLE_TEXT),
            stats=RIGHTS_STATS if cloud_policy != "local_only" else {},
            paragraphs=[p.strip() for p in SAMPLE_TEXT.split("\n\n") if p.strip()] if with_paragraphs else [],
            paragraph_id="sr_para_hd_" + seed + "_{index:02d}",
        )
        session.commit()
    return book_id


def _seed_profile_for_book(seed: str, book_id: str, *, profile_json: dict | None = None) -> str:
    with SessionLocal() as session:
        profile_id = make_profile(
            session,
            book_id,
            profile_id=f"sr_profile_hd_{seed}",
            run_id=f"sr_run_hd_{seed}",
            profile_json=profile_json or {"narrative_summary": "短句白描"},
        )
        session.commit()
    return profile_id


# ---------------------------------------------------------------------------
# 1/2. cloud_policy 强制执行 + 错误映射
# ---------------------------------------------------------------------------


def test_cloud_policy_blocked_error_is_domain_error_409():
    err = CloudPolicyBlockedError(book_id="b1", operation="op")
    assert isinstance(err, DomainError)
    assert err.status_code == 409
    assert err.code == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert err.details["author_action"]


def test_llm_required_error_is_domain_error_409():
    err = LLMRequiredError(operation="op")
    assert isinstance(err, DomainError)
    assert err.status_code == 409
    assert err.code == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err.details["author_action"]


def test_reclassify_blocked_for_local_only_book():
    book_id = _seed_book("reclass_block", cloud_policy="local_only")
    with SessionLocal() as session:
        service = IngestService(session, llm_client=object(), llm_enabled=True)
        for mode in ("reclassify", "retype"):
            with pytest.raises(CloudPolicyBlockedError):
                service.start_reclassify(book_id, mode=mode)
        with pytest.raises(CloudPolicyBlockedError):
            service.resume(book_id)


def test_ingest_local_only_with_a_cloud_llm_is_refused_not_heuristic(monkeypatch):
    """2026-09-15 严格 LLM:local_only 的段落不送云,也没有启发式兜底——分类节点走云端接入时直接拒绝,
    LLM client 一次都不许被调用;分类节点走本机模型(打桩 node_route_is_local)才建分类作业、由模型分类。"""
    from novel_system.services.style_reference import import_job
    from novel_system.services.style_reference import policy as policy_module
    from novel_system.services.style_reference.jobs import run_job_inline

    sentinel = _SentinelLLM()
    with SessionLocal() as session:
        service = IngestService(session, llm_client=sentinel, llm_enabled=True)
        with pytest.raises(CloudPolicyBlockedError) as caught:
            service.ingest_upload(
                raw_bytes=SAMPLE_TEXT.encode("utf-8"),
                file_name="local_only_book.txt",
                title="本地书",
                author_label=None,
                cloud_policy="local_only",
            )
    assert caught.value.details["author_action"]["view"] == "systemConfig"
    assert caught.value.details["node_id"] in (
        "style_ref_paragraph_classify_anchor",
        "style_ref_paragraph_classify_bulk",
    )
    assert "仅本机" in caught.value.message
    assert not sentinel.called

    from tests.conftest import build_fake_paragraph_classifier

    fake = build_fake_paragraph_classifier()(rule="default")
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: True)
    monkeypatch.setattr(import_job, "resolve_classification_client", lambda: (fake, True))
    with SessionLocal() as session:
        service = IngestService(session, llm_client=fake, llm_enabled=True)
        result = service.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name="local_only_book_local_llm.txt",
            title="本地书",
            author_label=None,
            cloud_policy="local_only",
        )
        session.commit()
        book_id, job_id = result.book.book_id, result.job.job_id
    run_job_inline(job_id)
    assert result.paragraphs_count > 0
    assert fake.call_count >= 1
    with SessionLocal() as session:
        book = StyleReferenceRepository(session).get_book(book_id)
        assert book.status == "ready"
        assert book.stats_json["classifier_calibration"]["fallback_to_heuristic"] is False
        assert book.stats_json["classification_provenance"]["source"] == "llm"


def test_style_check_for_local_only_book_is_refused_without_a_local_llm():
    """2026-09-15 严格 LLM:local_only 不出云——对照检查(取代旧回测的全量三路)用云端模型直接 409,
    建作业时就拒绝,模型一次都不调。"""
    book_id = _seed_book("async_local", cloud_policy="local_only")
    profile_id = _seed_profile_for_book("async_local", book_id)
    sentinel = _SentinelLLM()
    with SessionLocal() as session:
        with pytest.raises(CloudPolicyBlockedError):
            start_check_job(
                session,
                text="一段完全原创的全新文本表达" * 20,
                profile_id=profile_id,
                llm_client=sentinel,
                llm_enabled=True,
            )
    assert not sentinel.called


def test_style_check_without_llm_is_refused():
    """对照检查必须有 LLM:未启用 → 409 STYLE_REFERENCE_LLM_REQUIRED,不静默降级成只有读数的检查。"""
    book_id = _seed_book("async_no_llm", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("async_no_llm", book_id)
    with SessionLocal() as session:
        with pytest.raises(LLMRequiredError):
            start_check_job(
                session,
                text="一段完全原创的全新文本表达" * 20,
                profile_id=profile_id,
                llm_client=None,
                llm_enabled=False,
            )


# ---------------------------------------------------------------------------
# 3. 反抄袭红线段接线
# ---------------------------------------------------------------------------


def _seed_injection(seed: str, *, banned_terms: list[str] | None = None) -> str:
    """book + profile + project binding(strategy A)。返回 project_id。"""
    project_id = f"proj_hd_{seed}"
    book_id = _seed_book(seed, cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book(
        seed,
        book_id,
        profile_json={
            "narrative_summary": "短句白描",
            "style_features": ["短句", "白描"],
        },
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id=f"sr_bind_hd_{seed}",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        for i, term in enumerate(banned_terms or []):
            repo.create_banned_term(
                term_id=f"sr_term_hd_{seed}_{i}",
                profile_id=profile_id,
                term=term,
                replacement_hint=None,
                source="user",
                scope="generation",
            )
        session.commit()
    return project_id


def _render_live(session, project_id: str):
    """v3:按项目的活动绑定现解析契约并渲染(旧 fragments_for 的替代)。"""
    from types import SimpleNamespace

    scope = SimpleNamespace(project_id=project_id, scene_id=None, pov_character_id=None, onstage_chars_json=[])
    return render_style(session, style_policy_live(session, scope), StyleRenderRequest(scene_id="hd_scene"), use_cache=False)


def test_anti_plagiarism_block_present_and_in_prefix():
    project_id = _seed_injection("antiplag")
    with SessionLocal() as session:
        rendered = _render_live(session, project_id)
    prefix = rendered.system_prefix
    assert "严格禁止" in prefix
    # 红线段排最后(在 [/STYLE_REFERENCE] 之前)
    assert prefix.rindex("严格禁止") > prefix.rindex("白描")
    assert prefix.rstrip().endswith("[/STYLE_REFERENCE]")


def test_anti_plagiarism_block_includes_generation_banned_terms():
    project_id = _seed_injection("antiterm", banned_terms=["龙傲天", "玛丽苏镇"])
    with SessionLocal() as session:
        prefix = _render_live(session, project_id).system_prefix
    assert "- 龙傲天" in prefix
    assert "- 玛丽苏镇" in prefix


def test_anti_plagiarism_block_absent_when_fragments_empty():
    with SessionLocal() as session:
        rendered = _render_live(session, "proj_nonexistent")
    assert rendered.system_prefix == "" and rendered.user_tail == ""


def test_anti_plagiarism_block_survives_budget_truncation():
    """预算压缩(贪心去卡句 / 声音)不得动红线段。"""
    project_id = "proj_hd_budget"
    book_id = _seed_book("budget", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book(
        "budget",
        book_id,
        profile_json={
            "qualitative_summary": "概述" * 50,
            "style_features": [f"要点{'甲乙丙丁戊己庚辛壬癸'[i % 10]}{'子丑寅卯'[i // 10]}" * 12 for i in range(30)],
        },
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id="sr_bind_hd_budget",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="B",
            config_json={},
            status="active",
        )
        session.commit()
        rendered = _render_live(session, project_id)
    red_line = rendered.system_prefix[rendered.system_prefix.index("## 严格禁止") :]
    fitted, audit = fit_rendered(rendered, base_system_prompt="", user_prompt="u", target_input_tokens=len(red_line) + 300)
    # 整张卡替身有总预算(整行截断);再压预算时按整行去,红线段完整保留
    assert len(rendered.system_prefix) < 2600 + len(red_line) + 400
    assert audit["compacted"] and audit["dropped_card_units"] >= 1
    assert len(fitted.system_prefix) < len(rendered.system_prefix)
    assert fitted.system_prefix.endswith(red_line) and "抄的是句子" in fitted.system_prefix


# ---------------------------------------------------------------------------
# 4. 抄袭检测:规范化 + 全书语料
# ---------------------------------------------------------------------------


def test_plagiarism_detects_punctuation_and_space_bypass():
    source = "雪花从天空缓缓飘落,落在他的肩头,他没有拂去。"
    # 改标点 + 插空格的微改抄袭
    bypass = "雪花从天空缓缓飘落、落在 他的肩头;他没有拂去!"
    report = check_plagiarism(bypass, [source])
    assert not report.passed
    assert report.hits


def test_plagiarism_position_maps_back_to_original_text():
    source = "雪花从天空缓缓飘落,落在他的肩头,他没有拂去。"
    generated = "开头几个字。雪花从天空缓缓飘落,落在他的肩头,他没有拂去。结尾。"
    report = check_plagiarism(generated, [source])
    assert not report.passed
    hit = report.hits[0]
    assert generated[hit.position : hit.position + len(hit.matched_text)] == hit.matched_text
    assert "雪花" in hit.matched_text


def test_plagiarism_short_common_phrases_pass():
    report = check_plagiarism("今天天气不错。", ["他说:今天天气不错。然后他走了,再没有回头看一眼。"])
    # 规范化后重叠仅 6 字(今天天气不错),低于 12 字阈值
    assert report.passed


def test_copy_gate_uses_full_book_corpus_not_only_quotes():
    """抄全书中未被引用为 quote 的段落,也必须被唯一抄袭门拦下(取代旧同步回测的同名用例)。"""
    book_id = _seed_book("fullcorpus", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("fullcorpus", book_id)
    copied = "雪花从天空缓缓飘落,落在他的肩头,他没有拂去。"  # 段落原文,但没有任何 quote 行
    with SessionLocal() as session:
        check = check_reference_copy(session, copied, book_ids=[book_id], profile_ids=[profile_id])
    assert check.blocked and check.hits


# ---------------------------------------------------------------------------
# 7. 路由层:上传上限 / 孤儿报告回收 / LLMRequired 409
# ---------------------------------------------------------------------------

PREFIX = "/api/v2/style-reference"


def _client():
    from fastapi.testclient import TestClient

    from novel_system.api.app import create_app

    return TestClient(create_app())


def test_upload_exceeding_size_limit_returns_413(monkeypatch: pytest.MonkeyPatch):
    from novel_system.api.routes.style_reference import books as routes_mod

    monkeypatch.setattr(routes_mod, "MAX_UPLOAD_BYTES", 64)
    with _client() as client:
        files = {"file": ("big.txt", io.BytesIO("超限内容".encode("utf-8") * 100), "text/plain")}
        resp = client.post(
            f"{PREFIX}/books/import-upload",
            files=files,
            data={"title": "大文件", "cloud_policy": "segments_only"},
            headers={"X-Idempotency-Key": "imp_big_1"},
        )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_UPLOAD_TOO_LARGE"


def test_llm_required_maps_to_409_with_author_action():
    """LLM 未启用时 reclassify 应返回 409 + author_action(而非通用 500)。"""
    book_id = _seed_book("llm409", cloud_policy="segments_only")
    with _client() as client:
        resp = client.post(
            f"{PREFIX}/books/{book_id}/reclassify",
            headers={"X-Idempotency-Key": "rc_409_1"},
        )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]


# ---------------------------------------------------------------------------
# 8. 后台 run + 进度 / apply 注入配置 / binding 唯一约束 / few-shot
# ---------------------------------------------------------------------------


def _seed_ingested_book(seed: str) -> str:
    with SessionLocal() as session:
        result = IngestService(session, llm_enabled=False).ingest_upload(
            raw_bytes=(SAMPLE_TEXT * 3).encode("utf-8"),
            file_name=f"hd_{seed}.txt",
            title=f"硬化{seed}",
            author_label=None,
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        session.commit()
        return result.book.book_id


def _seed_project(project_id: str) -> str:
    from novel_system.db.models import StoryProject

    with SessionLocal() as session:
        if session.get(StoryProject, project_id) is None:
            session.add(StoryProject(project_id=project_id, title="合成作品", outline_text=""))
            session.commit()
    return project_id


def test_apply_profile_persists_injection_config():
    """v3 直接绑定:配置落成四键;同一画像再用于同一作品只改给出的键(维度状态按维合并),strategy 恒 mixed。"""
    from novel_system.services.style_reference.binding_apply import apply_style_profile

    book_id = _seed_book("applycfg", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("applycfg", book_id)
    project_id = _seed_project("proj_applycfg")
    with SessionLocal() as session:
        first = apply_style_profile(
            session,
            profile_id,
            scope="project",
            scope_ref_id=project_id,
            config={"sample_windows": 5, "dimension_states": {"language.rhetoric": "emphasize"}},
        )
        session.commit()
        binding = StyleReferenceRepository(session).get_binding(first.binding.binding_id)
        assert binding.strategy == "mixed" and first.created is True
        assert binding.config_json["sample_windows"] == 5
        assert binding.config_json["reference_mode"] == "full" and binding.config_json["draft_mode"] == "style_first"
        assert binding.config_json["dimension_states"]["language.rhetoric"] == "emphasize"
        # 重复用于同一作品:复用同一行,只改给出的键
        second = apply_style_profile(
            session,
            profile_id,
            scope="project",
            scope_ref_id=project_id,
            config={"reference_mode": "card_only", "dimension_states": {"scene.dialogue": "exclude"}},
        )
        session.commit()
        assert second.binding.binding_id == first.binding.binding_id and second.created is False
        binding = StyleReferenceRepository(session).get_binding(first.binding.binding_id)
        assert binding.config_json["reference_mode"] == "card_only"
        assert binding.config_json["sample_windows"] == 5
        assert binding.config_json["dimension_states"]["language.rhetoric"] == "emphasize"
        assert binding.config_json["dimension_states"]["scene.dialogue"] == "exclude"


def test_binding_unique_constraint_blocks_duplicates():
    from sqlalchemy.exc import IntegrityError

    book_id = _seed_book("uniq", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("uniq", book_id)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id="sr_bind_hd_uniq_1",
            profile_id=profile_id,
            scope="project",
            scope_ref_id="proj_uniq",
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        session.commit()
        with pytest.raises(IntegrityError):
            repo.create_binding(
                binding_id="sr_bind_hd_uniq_2",
                profile_id=profile_id,
                scope="project",
                scope_ref_id="proj_uniq",
                task_type="scene_generation",
                strategy="B",
                config_json={},
                status="active",
            )
        session.rollback()


def _seed_windowed_binding(seed: str, *, strategy: str, cloud_policy: str = "allow_full_cloud") -> str:
    """一本够切窗的书(一章约 3,000 字)+ 旧画像 + project 绑定。返回 project_id。"""
    book_id = _seed_book(seed, cloud_policy=cloud_policy, with_paragraphs=False)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        for idx in range(40):
            body = f"第{idx}段：他把伞收了，站在檐下看雨，院子里的水一直漫到台阶下面。"
            repo.create_paragraph(
                paragraph_id=f"sr_para_hd_{seed}_{idx:02d}",
                book_id=book_id,
                paragraph_index=idx,
                paragraph_type="narration",
                start_offset=0,
                end_offset=len(body),
                text=body,
                char_count=len(body),
                classifier_confidence=0.9,
            )
        session.commit()
    profile_id = _seed_profile_for_book(seed, book_id, profile_json={"qualitative_summary": "短句白描", "style_features": ["短句"]})
    project_id = f"proj_{seed}"
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_binding(
            binding_id=f"sr_bind_hd_{seed}",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy=strategy,
            config_json={},
            status="active",
        )
        session.commit()
    return project_id


def test_legacy_strategy_b_renders_windows_from_the_book_index():
    """v3:样例只来自全书窗口索引(旧的证据引文路径已删);旧策略 B / mixed 映射为全面模仿。"""
    for strategy in ("B", "mixed"):
        project_id = _seed_windowed_binding(f"fewshot_{strategy}", strategy=strategy)
        with SessionLocal() as session:
            rendered = _render_live(session, project_id)
        assert rendered.stats["few_shot_windows"] == 1
        assert "[风格样例]" in rendered.system_prefix and "他把伞收了" in rendered.system_prefix
        # 样例引用原文 → 红线段必须在场,且在前缀里
        assert "严格禁止" in rendered.system_prefix


def test_legacy_strategy_a_and_segments_only_books_send_no_windows():
    project_id = _seed_windowed_binding("nofs", strategy="A")
    segments = _seed_windowed_binding("nofs_seg", strategy="mixed", cloud_policy="segments_only")
    with SessionLocal() as session:
        for pid in (project_id, segments):
            rendered = _render_live(session, pid)
            assert rendered.stats["few_shot_windows"] == 0 and "[风格样例]" not in rendered.system_prefix
            assert "短句" in rendered.system_prefix and "严格禁止" in rendered.system_prefix


def test_failed_idempotent_action_does_not_half_commit():
    """幂等层:action 中途失败时,已 flush 的半成品写入必须随回滚消失。"""
    from novel_system.services.idempotency import execute_with_idempotency

    book_id = _seed_book("halfcommit", cloud_policy="segments_only", with_paragraphs=False)

    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)

        def _action() -> dict:
            repo.create_run(
                run_id="sr_run_hd_half",
                book_id=book_id,
                status="running",
                phase="extract",
            )
            raise DomainError("BOOM", "deliberate failure", status_code=400)

        with pytest.raises(DomainError):
            execute_with_idempotency(
                session,
                idempotency_key="idem_half_1",
                method="POST",
                path_template="/test/half-commit",
                payload={},
                action=_action,
            )

    with SessionLocal() as session:
        assert StyleReferenceRepository(session).get_run("sr_run_hd_half") is None, (
            "失败 action 的半成品 run 行不应被提交"
        )


# ---------------------------------------------------------------------------
# 9. bind_style_profile 决策卡 effect 转发注入配置(apply 决策卡 → 批准 → 真 bind)
# ---------------------------------------------------------------------------


def test_bind_style_profile_effect_forwards_injection_config():
    """待办里还没处理的旧「应用画像」卡:批准时走 v3 直接绑定,卡上的旧键映射成 v3 配置
    (强度 35 → round(3 + 9·0.35) = 6 窗;mixed → 全面模仿;旧 sub_dimensions 不再有「只学几维」的语义)。"""
    from novel_system.services.review_effects import run_effect

    book_id = _seed_book("effectcfg", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("effectcfg", book_id)
    project_id = _seed_project("proj_effectcfg")
    with SessionLocal() as session:
        result = run_effect(
            session,
            project_id,
            {
                "type": "bind_style_profile",
                "profile_id": profile_id,
                "scope": "project",
                "strategy": "mixed",
                "intensity": 35,
                "sub_dimensions": ["language.rhetoric", "scene.dialogue"],
                "include_metric": True,
            },
        )
        session.commit()
        binding = StyleReferenceRepository(session).get_binding(result["binding_id"])
        assert binding.scope == "project"
        assert binding.scope_ref_id == project_id
        assert binding.strategy == "mixed"
        assert binding.config_json["sample_windows"] == 6
        assert binding.config_json["reference_mode"] == "full"
        assert set(binding.config_json["dimension_states"].values()) == {"normal"}
        assert "intensity" not in binding.config_json and "sub_dimensions" not in binding.config_json


def test_bind_style_profile_effect_scene_and_character_scope():
    """立项 A — 旧决策卡 scope=scene/character + scope_ref_id 落成对应 scope 的真 binding,
    且 resolve_active_binding(scene_id=...) 命中场景级绑定(scene > character > project 优先级);
    旧卡上的策略 A 映射成「只用文风卡」。"""
    from novel_system.services.review_effects import run_effect
    from novel_system.services.style_reference.injection import InjectionService
    from tests.style_reference_inject_helpers import seed_scene

    book_id = _seed_book("scoperef", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("scoperef", book_id)
    with SessionLocal() as session:
        scene = seed_scene(session, "SCOPEREF_SC01", project_id="proj_scoperef", chapter_id="SCOPEREF_CH01")
        scene_res = run_effect(session, "proj_scoperef", {
            "type": "bind_style_profile", "profile_id": profile_id,
            "scope": "scene", "scope_ref_id": scene.scene_id,
            "task_type": "scene_generation", "strategy": "A",
        })
        char_res = run_effect(session, "proj_scoperef", {
            "type": "bind_style_profile", "profile_id": profile_id,
            "scope": "character", "scope_ref_id": "scoperef_CHAR01",
            "task_type": "scene_generation", "strategy": "B",
        })
        session.commit()
        repo = StyleReferenceRepository(session)
        sb = repo.get_binding(scene_res["binding_id"])
        cb = repo.get_binding(char_res["binding_id"])
        assert sb.scope == "scene" and sb.scope_ref_id == scene.scene_id
        assert sb.config_json["reference_mode"] == "card_only" and sb.strategy == "mixed"
        assert cb.scope == "character" and cb.scope_ref_id == "scoperef_CHAR01"
        assert cb.config_json["reference_mode"] == "full"
        # 注入选取:scene_id 命中场景级绑定(优先级最高)
        picked = InjectionService(session).resolve_active_binding(
            "proj_scoperef", "scene_generation",
            character_ids=["scoperef_CHAR01"], scene_id=scene.scene_id,
        )
        assert picked is not None
        assert picked.scope == "scene" and picked.scope_ref_id == scene.scene_id
        # 角色级单独命中:scene 不匹配时,character_ids 命中角色级绑定
        picked_char = InjectionService(session).resolve_active_binding(
            "proj_scoperef", "scene_generation",
            character_ids=["scoperef_CHAR01"], scene_id="other_scene",
        )
        assert picked_char is not None
        assert picked_char.scope == "character" and picked_char.scope_ref_id == "scoperef_CHAR01"


def test_bind_style_profile_effect_scene_requires_scope_ref_id():
    """立项 A — scene/character 级绑定缺 scope_ref_id 应拒绝(防静默回退 project_id 成脏数据)。"""
    from novel_system.services.review_effects import run_effect

    book_id = _seed_book("scoperefreq", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("scoperefreq", book_id)
    with SessionLocal() as session:
        with pytest.raises(DomainError) as exc:
            run_effect(session, "proj_scoperefreq", {
                "type": "bind_style_profile", "profile_id": profile_id, "scope": "scene",
            })
        assert exc.value.status_code == 400


def test_bind_style_profile_effect_without_config_gets_v3_defaults():
    """无配置的旧卡:绑定落 v3 默认(全面模仿 · 12 窗 · 作者手笔直起 · 各维正常),目标默认取卡的作品。"""
    from novel_system.services.review_effects import run_effect

    book_id = _seed_book("effectplain", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("effectplain", book_id)
    project_id = _seed_project("proj_effectplain")
    with SessionLocal() as session:
        result = run_effect(
            session,
            project_id,
            {"type": "bind_style_profile", "profile_id": profile_id},
        )
        session.commit()
        binding = StyleReferenceRepository(session).get_binding(result["binding_id"])
        assert binding.config_json["reference_mode"] == "full"
        assert binding.config_json["sample_windows"] == 12
        assert binding.config_json["draft_mode"] == "style_first"
        assert binding.strategy == "mixed"
        assert binding.scope_ref_id == project_id


# ---------------------------------------------------------------------------
# 10. GET /books/{id}/runs（矩阵深层页定位最新 run）
# ---------------------------------------------------------------------------


def test_list_book_runs_newest_first_and_status_filter():
    book_id = _seed_book("listruns", cloud_policy="segments_only", with_paragraphs=False)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(run_id="sr_run_lr_1", book_id=book_id, status="done", phase="done")
        repo.create_run(run_id="sr_run_lr_2", book_id=book_id, status="running", phase="extract")
        session.commit()
    with _client() as client:
        runs = client.get(f"{PREFIX}/books/{book_id}/runs").json()["data"]["runs"]
        assert [r["run_id"] for r in runs] == ["sr_run_lr_2", "sr_run_lr_1"], "应按 created_at 倒序"
        done = client.get(f"{PREFIX}/books/{book_id}/runs?status=done").json()["data"]["runs"]
        assert [r["run_id"] for r in done] == ["sr_run_lr_1"]
