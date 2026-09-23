"""PR-8 §5.1 — scene_generation._inject_style_reference 与 InjectionService 集成。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from novel_system.db.models import SceneCard, SceneRunState
from novel_system.db.session import SessionLocal
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.style_reference.repository import StyleReferenceRepository


import pytest as _pytest_ap
from tests.real_llm_fakes import install_online_pipeline as _install_online_pipeline


# 风格参考 v3（H1）：云策略按接收提示的节点判——「仅本机」的书遇上说不出接收节点的裸提示（这里的基础 prompt 没有
# template_name）一个字都不送。这些用例测的是注入本身，参考书用可送云的策略 + 严格发送权声明。
_SEND_RIGHTS = {"rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True}}


@_pytest_ap.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    """假生成已退役：给场景管线未显式注入的子服务兜底在线记账替身。"""
    _install_online_pipeline(monkeypatch)


def _seed_style_reference_binding(
    *,
    project_id: str,
    seed: str,
    task_type: str = "scene_generation",
    style_features: list[str] | None = None,
    strategy: str = "A",
    profile_status: str = "active",
) -> None:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = f"sr_book_{seed}"
        run_id = f"sr_run_{seed}"
        profile_id = f"sr_profile_{seed}"
        binding_id = f"sr_bind_{seed}"
        repo.create_book(
            book_id=book_id,
            title="t",
            source_kind="upload",
            cloud_policy="allow_full_cloud",
            text_checksum=f"chk_{seed}",
            total_chars=10,
            status="ready",
            stats_json=dict(_SEND_RIGHTS),
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="t",
            status=profile_status,
            profile_json={
                "narrative_summary": "短句白话",
                "style_features": style_features or ["短句", "克制"],
            },
            coverage_json={},
            source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=binding_id,
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            task_type=task_type,
            strategy=strategy,
            config_json={},
            status="active",
        )
        session.commit()


def _make_scene(project_id: str | None) -> SceneCard:
    return SceneCard(
        scene_id="CH900_SC01",
        chapter_id="CH900",
        project_id=project_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="Café",
        scene_goal="reveal",
        beats_json=["arrival"],
        must_include_text="x",
        target_length_band="short",
        scene_type="reveal",
        is_chapter_last=0,
    )


def _persist_scene(session, scene: SceneCard) -> None:
    session.add(scene)
    session.add(SceneRunState(scene_id=scene.scene_id, scene_status="ready"))
    session.flush()


def _bundle() -> dict[str, object]:
    return {
        "bundle_id": "bundle_CH900_SC01_test",
        "bundle_snapshot_hash": "bundle_hash_continuation_test",
        "snapshot": {"scene_id": "CH900_SC01"},
    }


def test_injection_prepends_style_reference_block(session) -> None:
    _seed_style_reference_binding(project_id="proj_inj1", seed="inj1")
    service = SceneGenerationService(session, llm_client=object())
    scene = _make_scene("proj_inj1")
    base_prompt = {"system_prompt": "BASE_SYSTEM_PROMPT", "user_prompt": "u"}
    out = service._inject_style_reference(
        base_prompt, scene, task_type="scene_generation"
    )
    assert out is not base_prompt
    assert out["system_prompt"].startswith("[STYLE_REFERENCE]\n")
    assert "短句" in out["system_prompt"]
    assert out["system_prompt"].endswith("BASE_SYSTEM_PROMPT")
    # user_prompt 不变
    assert out["user_prompt"] == "u"


def test_injection_noop_when_scene_has_no_project_id(session) -> None:
    service = SceneGenerationService(session, llm_client=object())
    scene = _make_scene(None)
    base = {"system_prompt": "BASE", "user_prompt": "u"}
    out = service._inject_style_reference(base, scene)
    assert out is base  # 完全未修改(直接返回原对象)


def test_injection_noop_when_no_active_binding(session) -> None:
    # 不 seed binding
    service = SceneGenerationService(session, llm_client=object())
    scene = _make_scene("proj_no_binding")
    base = {"system_prompt": "BASE", "user_prompt": "u"}
    out = service._inject_style_reference(base, scene)
    # 无 binding 时 fragments 返 empty → prefix="" → 返回原 prompt
    assert out is base


def test_injection_failure_is_swallowed(session) -> None:
    # v3：没有绑定时根本不渲染（严格 no-op），所以这里要有一条绑定，渲染才会被调用并抛错
    _seed_style_reference_binding(project_id="proj_explode", seed="explode")
    service = SceneGenerationService(session, llm_client=object())
    scene = _make_scene("proj_explode")
    base = {"system_prompt": "BASE", "user_prompt": "u"}
    # v3：渲染在 inject.render.render_style（适配器 style_prompt_injection 调它）
    with patch(
        "novel_system.services.style_prompt_injection.render_style",
        side_effect=RuntimeError("style_reference unreachable"),
    ):
        out = service._inject_style_reference(base, scene)
    # 异常被吞掉，LLM 流程不阻断；降级原因进入审计但不进入 system/user 文本。
    assert out is not base
    assert out["system_prompt"] == base["system_prompt"]
    assert out["user_prompt"] == base["user_prompt"]
    assert out["_style_reference_runtime_audit"]["outcome"] == "degraded"
    assert out["_style_reference_runtime_audit"]["error_code"] == "RuntimeError"


def test_neutral_draft_does_not_receive_style_reference_injection(session) -> None:
    """中性稿只搭事实骨架；参考风格必须留到 style_draft 阶段。"""
    _seed_style_reference_binding(project_id="proj_neutral_plain", seed="neutral_plain")
    scene = _make_scene("proj_neutral_plain")
    _persist_scene(session, scene)
    bundle = _bundle()

    class FakePromptBuilder:
        def build(self, snapshot, template_name):  # noqa: ANN001
            assert snapshot == bundle["snapshot"]
            assert template_name == "neutral_draft"
            return {
                "template_name": template_name,
                "template_version": "test",
                "system_prompt": "NEUTRAL_BASE_SYSTEM",
                "user_prompt": "NEUTRAL_BASE_USER",
                "structured_schema": {
                    "type": "object",
                    "required": ["scene_text"],
                    "properties": {"scene_text": {"type": "string"}},
                },
                "token_budget": {
                    "target_input_tokens": 1000,
                    "estimated_input_tokens": 0,
                    "remaining_input_tokens": 1000,
                },
            }

    class FakeRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run(self, **kwargs):  # noqa: ANN003
            self.calls.append(kwargs)
            return SimpleNamespace(
                llm_call_id="llm_call_neutral_plain",
                response=SimpleNamespace(
                    structured_output={
                        "scene_text": "门外的脚步停住了，他把信封放到桌上，等对面的人先开口。"
                    }
                ),
            )

    runner = FakeRunner()
    service = SceneGenerationService(session, llm_runner=runner)
    service._prompt_builder_instance = FakePromptBuilder()

    with patch.object(
        service, "_inject_style_reference", wraps=service._inject_style_reference
    ) as inject_spy:
        result = service.generate_neutral_draft(scene.scene_id, bundle)

    inject_spy.assert_not_called()
    assert runner.calls[0]["prompt"]["system_prompt"] == "NEUTRAL_BASE_SYSTEM"
    assert "短句白话" not in runner.calls[0]["prompt"]["system_prompt"]
    assert result.content == "门外的脚步停住了，他把信封放到桌上，等对面的人先开口。"


def _seed_character_binding(*, seed: str, character_id: str, feature: str) -> None:
    """PR-14 — 落 character scope binding(scope_ref_id=character_id)。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=f"sr_book_{seed}",
            title="t",
            source_kind="upload",
            cloud_policy="allow_full_cloud",
            text_checksum=f"chk_{seed}",
            total_chars=10,
            status="ready",
            stats_json=dict(_SEND_RIGHTS),
        )
        repo.create_run(
            run_id=f"sr_run_{seed}",
            book_id=f"sr_book_{seed}",
            status="done",
            phase="done",
        )
        repo.create_profile(
            profile_id=f"sr_profile_{seed}",
            book_id=f"sr_book_{seed}",
            run_id=f"sr_run_{seed}",
            title="t",
            status="active",
            profile_json={"narrative_summary": "n", "style_features": [feature]},
            coverage_json={},
            source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=f"sr_bind_{seed}",
            profile_id=f"sr_profile_{seed}",
            scope="character",
            scope_ref_id=character_id,
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        session.commit()


def test_injection_matches_character_binding_via_pov(session) -> None:
    """PR-14 — scene.pov_character_id 命中 character binding 时注入该 profile。"""
    _seed_character_binding(
        seed="povchar", character_id="CHAR_A", feature="角色专属腔调"
    )
    service = SceneGenerationService(session, llm_client=object())
    # scene 的 project 无 binding,但 pov_character_id=CHAR_A 命中 character binding
    scene = _make_scene("proj_no_project_binding")
    base = {"system_prompt": "BASE", "user_prompt": "u"}
    out = service._inject_style_reference(base, scene, task_type="scene_generation")
    assert out is not base
    assert "角色专属腔调" in out["system_prompt"]
    assert out["system_prompt"].endswith("BASE")


def _seed_scene_binding(*, seed: str, scene_id: str, feature: str) -> None:
    """PR-15 — 落 scene scope binding(scope_ref_id=scene_id)。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=f"sr_book_{seed}",
            title="t",
            source_kind="upload",
            cloud_policy="allow_full_cloud",
            text_checksum=f"chk_{seed}",
            total_chars=10,
            status="ready",
            stats_json=dict(_SEND_RIGHTS),
        )
        repo.create_run(
            run_id=f"sr_run_{seed}",
            book_id=f"sr_book_{seed}",
            status="done",
            phase="done",
        )
        repo.create_profile(
            profile_id=f"sr_profile_{seed}",
            book_id=f"sr_book_{seed}",
            run_id=f"sr_run_{seed}",
            title="t",
            status="active",
            profile_json={"narrative_summary": "n", "style_features": [feature]},
            coverage_json={},
            source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=f"sr_bind_{seed}",
            profile_id=f"sr_profile_{seed}",
            scope="scene",
            scope_ref_id=scene_id,
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        session.commit()


def test_injection_matches_scene_binding_via_scene_id(session) -> None:
    """PR-15 — scene.scene_id 命中 scene binding(优先于 character/project)。"""
    # _make_scene scene_id 固定为 CH900_SC01
    _seed_scene_binding(seed="scenebind", scene_id="CH900_SC01", feature="场景专属腔调")
    service = SceneGenerationService(session, llm_client=object())
    scene = _make_scene("proj_no_project_binding")
    base = {"system_prompt": "BASE", "user_prompt": "u"}
    out = service._inject_style_reference(base, scene, task_type="scene_generation")
    assert out is not base
    assert "场景专属腔调" in out["system_prompt"]
    assert out["system_prompt"].endswith("BASE")


def test_injection_matches_onstage_nonpov_character(session) -> None:
    """PR-18 — pov 无 binding,但 onstage 配角有 character binding → 命中配角。"""
    _seed_character_binding(
        seed="onstagechar", character_id="CHAR_B", feature="配角腔调"
    )
    service = SceneGenerationService(session, llm_client=object())
    scene = _make_scene("proj_no_project_binding")
    scene.pov_character_id = "POV_NO_BIND"  # pov 无 binding
    scene.onstage_chars_json = ["POV_NO_BIND", "CHAR_B"]
    base = {"system_prompt": "BASE", "user_prompt": "u"}
    out = service._inject_style_reference(base, scene, task_type="scene_generation")
    assert out is not base
    assert "配角腔调" in out["system_prompt"]


# ---------------------------------------------------------------------------
# 2026-09 风格模仿 v2（W5，规格 §2.W5.5 / §2.W5.6）：notices 与 styled-draft gate
# ---------------------------------------------------------------------------

from sqlalchemy import select as _select  # noqa: E402

from novel_system.db.models import (  # noqa: E402
    AttemptTracker,
    ChapterGoal,
    LlmCall,
    SceneDraft,
    StoryProject,
    StyleReferenceMetricEvent,
)
from novel_system.services.qc_engine import (  # noqa: E402
    _STYLED_GATE_MAX_HITS,
    STYLED_DRAFT_GATE_EVENT_KIND,
    _styled_gate_result,
)
from novel_system.services.scene_generation import (  # noqa: E402
    STYLE_NOTICE_BANNED_TERM_HIT,
    STYLE_NOTICE_CODES,
    STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
    STYLE_NOTICE_GATE_UNAVAILABLE,
    STYLE_NOTICE_INJECTION_DEGRADED,
    STYLE_NOTICE_INJECTION_MISS,
    STYLE_NOTICE_PLAGIARISM_HIT,
    _styled_draft_gate_notices,
    latest_style_notices,
    style_injection_notices,
    style_notice,
)

_REFERENCE_PARAGRAPH = "月光落在青石板上，像一层薄薄的盐，他踩过去时鞋底发出细碎的声响。"
_COPIED_SENTENCE = "月光落在青石板上，像一层薄薄的盐"
_NEUTRAL_TEXT = "门外的脚步停住了，他把信封放到桌上，等对面的人先开口。她没有伸手去接。"


def test_style_notice_codes_are_closed_set() -> None:
    from novel_system.services.scene_generation import (
        STYLE_NOTICE_FIRST_DRAFT,
        STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
        STYLE_NOTICE_PATCH_REVERTED,
        STYLE_NOTICE_REFERENCE_BOOK_CHANGED,
        STYLE_NOTICE_REFERENCE_BOOK_MISSING,
        STYLE_NOTICE_REFERENCE_NO_WINDOWS,
        STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED,
        STYLE_NOTICE_REVISION_REJECTED,
    )

    assert STYLE_NOTICE_CODES == {
        STYLE_NOTICE_FIRST_DRAFT,
        STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
        STYLE_NOTICE_INJECTION_MISS,
        STYLE_NOTICE_INJECTION_DEGRADED,
        STYLE_NOTICE_PLAGIARISM_HIT,
        STYLE_NOTICE_BANNED_TERM_HIT,
        STYLE_NOTICE_GATE_UNAVAILABLE,
        # 2026-09-23 风格参考 v3（P5b）：风格步 / 软补丁的决定，与注入适配器审计转成的提示
        STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
        STYLE_NOTICE_REVISION_REJECTED,
        STYLE_NOTICE_PATCH_REVERTED,
        STYLE_NOTICE_REFERENCE_BOOK_CHANGED,
        STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED,
        STYLE_NOTICE_REFERENCE_BOOK_MISSING,
        STYLE_NOTICE_REFERENCE_NO_WINDOWS,
    }
    assert all(code.startswith("STYLE_") for code in STYLE_NOTICE_CODES)
    notice = style_notice(STYLE_NOTICE_INJECTION_MISS, "m", severity="info", extra=1, dropped=None)
    assert notice == {"code": STYLE_NOTICE_INJECTION_MISS, "message": "m", "severity": "info", "extra": 1}
    with pytest.raises(ValueError):
        style_notice("NOT_A_CODE", "m")
    with pytest.raises(ValueError):
        style_notice(STYLE_NOTICE_INJECTION_MISS, "m", severity="loud")


def test_style_injection_notices_translate_runtime_audit_outcomes() -> None:
    assert style_injection_notices(None) == []
    assert style_injection_notices({"system_prompt": "no audit → no binding → no notice"}) == []
    assert style_injection_notices({"_style_reference_runtime_audit": {"outcome": "hit"}}) == []
    miss = style_injection_notices(
        {"_style_reference_runtime_audit": {"outcome": "miss", "contract_hash": "abc", "profile_ids": ["p1"]}}
    )
    assert [item["code"] for item in miss] == [STYLE_NOTICE_INJECTION_MISS]
    assert miss[0]["severity"] == "warning" and miss[0]["profile_ids"] == ["p1"]
    degraded = style_injection_notices(
        {"_style_reference_runtime_audit": {"outcome": "degraded", "error_code": "RuntimeError"}}
    )
    assert [item["code"] for item in degraded] == [STYLE_NOTICE_INJECTION_DEGRADED]
    assert degraded[0]["severity"] == "error" and degraded[0]["error_code"] == "RuntimeError"
    budget = style_injection_notices(
        {"_style_reference_runtime_audit": {"outcome": "degraded_budget", "budget_fit": {"compacted": True}}}
    )
    assert [item["code"] for item in budget] == [STYLE_NOTICE_INJECTION_DEGRADED]
    assert budget[0]["budget_fit"] == {"compacted": True}


def _seed_style_scene(
    session,
    *,
    project_id: str,
    scene_id: str = "CH901_SC01",
    chapter_id: str = "CH901",
    must_include_text: str = "信封",
) -> SceneCard:
    session.add(StoryProject(project_id=project_id, title="v2 notices", outline_text=""))
    session.add(
        ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="g")
    )
    scene = SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="Café",
        scene_goal="reveal",
        beats_json=["arrival"],
        must_include_text=must_include_text,
        target_length_band="short",
        scene_type="reveal",
        is_chapter_last=0,
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=scene_id, scene_status="hard_qc_passed"))
    session.add(
        SceneDraft(
            row_id=f"draft_neutral_{scene_id}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            stage="neutral_draft",
            content=_NEUTRAL_TEXT,
            source_bundle_id=f"bundle_{scene_id}_v2",
            source_bundle_hash="bundle_hash_v2",
        )
    )
    session.commit()
    return scene


def _style_bundle(scene_id: str = "CH901_SC01", chapter_id: str = "CH901") -> dict:
    return {
        "bundle_id": f"bundle_{scene_id}_v2",
        "bundle_snapshot_hash": "bundle_hash_v2",
        "snapshot": {
            "contract_version": "BSHASH_v1",
            "stage_allowlist_name": "bundle_build_allowlist_v1",
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "inline_digests": {"scene_card": "Reveal the letter without explaining it."},
        },
    }


class _StepRunner:
    """假 LLMNodeRunner：按 step 返回不同 scene_text，记录每次调用。"""

    def __init__(self, outputs: dict[str, str], default: str) -> None:
        self.outputs = outputs
        self.default = default
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        text = self.outputs.get(str(kwargs.get("step")), self.default)
        return SimpleNamespace(
            llm_call_id=f"llm_call_v2_{len(self.calls)}",
            response=SimpleNamespace(structured_output={"scene_text": text}),
        )


def _seed_reference_paragraphs(seed: str, paragraphs: list[str]) -> None:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        offset = 0
        for index, text in enumerate(paragraphs):
            repo.create_paragraph(
                paragraph_id=f"sr_par_{seed}_{index}",
                book_id=f"sr_book_{seed}",
                paragraph_index=index,
                paragraph_type="narration",
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
                char_count=len(text),
            )
            offset += len(text)
        session.commit()


def test_style_draft_fallback_to_neutral_emits_notice_and_is_audited(session) -> None:
    """风格稿丢了必含项 → 回退中性稿：结果与 AttemptTracker 都带 STYLE_DRAFT_FALLBACK_NEUTRAL。"""
    _seed_style_reference_binding(project_id="proj_v2_fallback", seed="v2_fallback")
    scene = _seed_style_scene(session, project_id="proj_v2_fallback")
    styled_without_required = "门外的脚步停住了。他把那个东西放到桌上，等对面的人先开口。她没有伸手去接。"
    runner = _StepRunner(
        outputs={"style_draft": styled_without_required},
        default=_NEUTRAL_TEXT,
    )
    service = SceneGenerationService(session, llm_runner=runner)

    result = service.generate_style_draft(
        scene.scene_id,
        _style_bundle(),
        neutral_draft_row_id=f"draft_neutral_{scene.scene_id}",
        neutral_content=_NEUTRAL_TEXT,
    )
    session.commit()

    codes = [item["code"] for item in result.notices]
    assert STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL in codes
    fallback = next(item for item in result.notices if item["code"] == STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL)
    assert fallback["severity"] == "warning"
    assert "required_facts" in " ".join(fallback["reasons"])
    assert fallback["rejected_candidate_row_id"]
    # 注入命中 → 没有 MISS / DEGRADED
    assert STYLE_NOTICE_INJECTION_MISS not in codes and STYLE_NOTICE_INJECTION_DEGRADED not in codes
    # style_draft 主行承载的是中性稿；provider 原稿留为 rejected 行
    base_row = session.execute(
        _select(SceneDraft).where(SceneDraft.scene_id == scene.scene_id, SceneDraft.stage == "style_draft")
    ).scalars().one()
    assert base_row.content == _NEUTRAL_TEXT
    rejected = session.execute(
        _select(SceneDraft).where(SceneDraft.scene_id == scene.scene_id, SceneDraft.stage == "style_rejected")
    ).scalars().one()
    assert rejected.status == "rejected" and rejected.content == styled_without_required
    attempt = session.execute(
        _select(AttemptTracker).where(
            AttemptTracker.scene_id == scene.scene_id,
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()
    attempt_codes = [item["code"] for item in attempt.details_json["notices"]]
    assert STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL in attempt_codes
    runtime_audit = attempt.details_json["style_reference_runtime"]
    assert runtime_audit["generation_outcome"] == "approved_neutral_fallback"
    assert STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL in runtime_audit["notice_codes"]
    # 回退稿没有过 styled-draft gate（gate 只对通过安全门的风格稿跑）
    assert "styled_draft_gate" not in attempt.details_json
    # API 回读同一份 notices
    assert STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL in [
        item["code"] for item in latest_style_notices(session, scene.scene_id)
    ]
    # style_draft 系统提示确实带了 [STYLE_REFERENCE] 前缀（注入命中）
    assert runner.calls[0]["prompt"]["system_prompt"].startswith("[STYLE_REFERENCE]\n")


def test_styled_draft_gate_plagiarism_hit_is_never_silent(session) -> None:
    """通过安全门的风格稿若复刻了参考原文：STYLE_PLAGIARISM_HIT 进结果、AttemptTracker 与回读。"""
    _seed_style_reference_binding(project_id="proj_v2_plag", seed="v2_plag")
    _seed_reference_paragraphs("v2_plag", [_REFERENCE_PARAGRAPH])
    scene = _seed_style_scene(session, project_id="proj_v2_plag")
    styled_with_copy = f"门外的脚步停住了。{_COPIED_SENTENCE}。他把信封放到桌上，等对面的人先开口。她没有伸手去接。"
    runner = _StepRunner(outputs={}, default=styled_with_copy)
    service = SceneGenerationService(session, llm_runner=runner)

    result = service.generate_style_draft(
        scene.scene_id,
        _style_bundle(),
        neutral_draft_row_id=f"draft_neutral_{scene.scene_id}",
        neutral_content=_NEUTRAL_TEXT,
    )
    session.commit()

    codes = [item["code"] for item in result.notices]
    assert STYLE_NOTICE_PLAGIARISM_HIT in codes
    assert STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL not in codes
    hit = next(item for item in result.notices if item["code"] == STYLE_NOTICE_PLAGIARISM_HIT)
    assert hit["severity"] == "blocking"
    assert hit["hit_count"] >= 1
    assert hit["profile_id"] == "sr_profile_v2_plag"
    # 命中片段（参考原文）不得随 notice 泄出
    assert _COPIED_SENTENCE not in str(result.notices)
    attempt = session.execute(
        _select(AttemptTracker).where(
            AttemptTracker.scene_id == scene.scene_id,
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()
    gate = attempt.details_json["styled_draft_gate"]
    assert gate["stage"] == "style_draft" and gate["verdict"] == "plagiarism"
    assert gate["plagiarism_passed"] is False
    assert STYLE_NOTICE_PLAGIARISM_HIT in attempt.details_json["style_reference_runtime"]["notice_codes"]
    assert STYLE_NOTICE_PLAGIARISM_HIT in [
        item["code"] for item in latest_style_notices(session, scene.scene_id)
    ]


def test_styled_draft_gate_banned_term_hit_emits_notice(session) -> None:
    _seed_style_reference_binding(project_id="proj_v2_term", seed="v2_term")
    with SessionLocal() as other:
        StyleReferenceRepository(other).create_banned_term(
            term_id="sr_term_v2_term", profile_id="sr_profile_v2_term",
            scope="generation", term="美轮美奂", source="manual",
        )
        other.commit()
    scene = _seed_style_scene(session, project_id="proj_v2_term")
    styled = "门外的脚步停住了。他把信封放到桌上，等对面的人先开口。她没有伸手去接，只觉得一切美轮美奂。"
    runner = _StepRunner(outputs={}, default=styled)
    service = SceneGenerationService(session, llm_runner=runner)
    result = service.generate_style_draft(
        scene.scene_id,
        _style_bundle(),
        neutral_draft_row_id=f"draft_neutral_{scene.scene_id}",
        neutral_content=_NEUTRAL_TEXT,
    )
    session.commit()
    hit = next(item for item in result.notices if item["code"] == STYLE_NOTICE_BANNED_TERM_HIT)
    assert hit["severity"] == "error" and hit["terms"] == ["美轮美奂"]
    assert STYLE_NOTICE_PLAGIARISM_HIT not in [item["code"] for item in result.notices]


def test_clean_styled_draft_has_no_notices_and_gate_passes(session) -> None:
    _seed_style_reference_binding(project_id="proj_v2_clean", seed="v2_clean")
    _seed_reference_paragraphs("v2_clean", [_REFERENCE_PARAGRAPH])
    scene = _seed_style_scene(session, project_id="proj_v2_clean")
    styled = "门外的脚步停住。他把信封放到桌上，不说话，等对面先开口。她没有伸手。"
    runner = _StepRunner(outputs={}, default=styled)
    service = SceneGenerationService(session, llm_runner=runner)
    result = service.generate_style_draft(
        scene.scene_id,
        _style_bundle(),
        neutral_draft_row_id=f"draft_neutral_{scene.scene_id}",
        neutral_content=_NEUTRAL_TEXT,
    )
    session.commit()
    assert result.notices == []
    attempt = session.execute(
        _select(AttemptTracker).where(
            AttemptTracker.scene_id == scene.scene_id,
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()
    assert attempt.details_json["notices"] == []
    assert attempt.details_json["styled_draft_gate"]["verdict"] == "pass"
    assert latest_style_notices(session, scene.scene_id) == []


def test_plagiarism_notice_reports_true_hit_count_not_the_truncated_evidence_list() -> None:
    """gate 把证据列表截到 8 条，但 notice 的 hit_count 必须是真实总数（与事件 / issue 一致）。"""
    hits = [
        {"position": index * 40, "matched_length": 14, "matched_text": f"命中片段{index}"}
        for index in range(11)
    ]
    forbidden = [
        {"pattern_statement": f"禁用{index}", "matched_excerpt": f"禁用{index}", "severity": "error"}
        for index in range(9)
    ]
    report = SimpleNamespace(
        plagiarism_json={"passed": False, "hits": hits},
        forbidden_hits_json=forbidden,
        quantitative_json=[],
        verdict="plagiarism",
    )
    gate = _styled_gate_result(
        stage="style_draft", report=report, profile_id="p", binding_id="b",
        runtime_contract_hash="h", runtime_contract_mode="frozen",
    )
    assert gate["plagiarism_hit_count"] == 11
    assert len(gate["plagiarism_hits"]) == _STYLED_GATE_MAX_HITS == 8
    assert gate["forbidden_hit_count"] == 9 and len(gate["forbidden_hits"]) == 8
    notices = _styled_draft_gate_notices(gate)
    plagiarism = next(item for item in notices if item["code"] == STYLE_NOTICE_PLAGIARISM_HIT)
    assert plagiarism["hit_count"] == 11
    banned = next(item for item in notices if item["code"] == STYLE_NOTICE_BANNED_TERM_HIT)
    assert banned["hit_count"] == 9 and len(banned["terms"]) == 8
    # 旧形状的 gate 字典（没有 *_hit_count）退回列表长度
    legacy = {k: v for k, v in gate.items() if k not in {"plagiarism_hit_count", "forbidden_hit_count"}}
    legacy_notices = _styled_draft_gate_notices(legacy)
    assert next(item for item in legacy_notices if item["code"] == STYLE_NOTICE_PLAGIARISM_HIT)["hit_count"] == 8


def test_styled_draft_gate_unavailable_is_never_silent(session) -> None:
    """gate 自身失败 ≠ 无绑定：结果 / AttemptTracker / 运行审计 / 回读都要带 STYLE_GATE_UNAVAILABLE。"""
    _seed_style_reference_binding(project_id="proj_v2_unavail", seed="v2_unavail")
    _seed_reference_paragraphs("v2_unavail", [_REFERENCE_PARAGRAPH])
    scene = _seed_style_scene(session, project_id="proj_v2_unavail")
    styled = "门外的脚步停住。他把信封放到桌上，不说话，等对面先开口。她没有伸手。"
    runner = _StepRunner(outputs={}, default=styled)
    service = SceneGenerationService(session, llm_runner=runner)
    with patch(
        "novel_system.services.reference_copy_gate.check_reference_copy",
        side_effect=RuntimeError("corpus unavailable"),
    ):
        result = service.generate_style_draft(
            scene.scene_id,
            _style_bundle(),
            neutral_draft_row_id=f"draft_neutral_{scene.scene_id}",
            neutral_content=_NEUTRAL_TEXT,
        )
    session.commit()

    assert [item["code"] for item in result.notices] == [STYLE_NOTICE_GATE_UNAVAILABLE]
    notice = result.notices[0]
    assert notice["severity"] == "error"
    assert notice["error"] == "RuntimeError" and notice["stage"] == "style_draft"
    assert result.styled_draft_gate["verdict"] == "unavailable"
    attempt = session.execute(
        _select(AttemptTracker).where(
            AttemptTracker.scene_id == scene.scene_id,
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()
    assert attempt.details_json["styled_draft_gate"]["verdict"] == "unavailable"
    assert attempt.details_json["styled_draft_gate"]["error"] == "RuntimeError"
    assert STYLE_NOTICE_GATE_UNAVAILABLE in attempt.details_json["style_reference_runtime"]["notice_codes"]
    assert STYLE_NOTICE_GATE_UNAVAILABLE in [
        item["code"]
        for item in latest_style_notices(session, scene.scene_id, bundle_id=_style_bundle()["bundle_id"])
    ]
    events = session.execute(
        _select(StyleReferenceMetricEvent).where(
            StyleReferenceMetricEvent.event_kind == STYLED_DRAFT_GATE_EVENT_KIND
        )
    ).scalars().all()
    assert [event.outcome for event in events] == ["error"]


def test_near_final_rewrite_is_gated_and_notices_land_on_the_rewrite_attempt(session) -> None:
    """准终稿重写带同一 [STYLE_REFERENCE] 前缀且输出直接成为终稿：必须过 gate，裁决落在重写尝试上。"""
    _seed_style_reference_binding(project_id="proj_v2_nfr", seed="v2_nfr")
    _seed_reference_paragraphs("v2_nfr", [_REFERENCE_PARAGRAPH])
    scene = _seed_style_scene(session, project_id="proj_v2_nfr")
    bundle = _style_bundle()
    styled_source = "门外的脚步停住。他把信封放到桌上，不说话，等对面先开口。她没有伸手。"
    source_row_id = f"draft_style_{scene.scene_id}"
    session.add(
        SceneDraft(
            row_id=source_row_id,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            stage="style_draft",
            content=styled_source,
            source_bundle_id=bundle["bundle_id"],
            source_bundle_hash=bundle["bundle_snapshot_hash"],
        )
    )
    session.commit()
    rewrite_with_copy = (
        f"门外的脚步停住。{_COPIED_SENTENCE}。他把信封放到桌上，等对面的人先开口。她没有伸手去接。"
    )
    runner = _StepRunner(outputs={"scene_literary_rewrite": rewrite_with_copy}, default=styled_source)
    service = SceneGenerationService(session, llm_runner=runner)

    result = service.generate_near_final_rewrite(
        scene.scene_id,
        bundle,
        source_draft_row_id=source_row_id,
        source_content=styled_source,
        revision_brief=["让选择的代价落在动作上。"],
        source_evaluation_id="eval_nfr_0",
    )
    session.commit()

    assert runner.calls[0]["step"] == "scene_literary_rewrite"
    assert runner.calls[0]["prompt"]["system_prompt"].startswith("[STYLE_REFERENCE]\n")
    assert result.content == rewrite_with_copy
    assert result.styled_draft_gate is not None
    assert result.styled_draft_gate["stage"] == "near_final_rewrite"
    assert result.styled_draft_gate["verdict"] == "plagiarism"
    hit = next(item for item in result.notices if item["code"] == STYLE_NOTICE_PLAGIARISM_HIT)
    assert hit["severity"] == "blocking" and hit["stage"] == "near_final_rewrite"
    assert _COPIED_SENTENCE not in str(result.notices)
    attempt = session.execute(
        _select(AttemptTracker).where(
            AttemptTracker.scene_id == scene.scene_id,
            AttemptTracker.step == "scene_literary_rewrite",
            AttemptTracker.status == "completed",
        )
    ).scalars().one()
    assert attempt.details_json["styled_draft_gate"]["stage"] == "near_final_rewrite"
    assert STYLE_NOTICE_PLAGIARISM_HIT in [item["code"] for item in attempt.details_json["notices"]]
    assert STYLE_NOTICE_PLAGIARISM_HIT in attempt.details_json["style_reference_runtime"]["notice_codes"]
    events = session.execute(
        _select(StyleReferenceMetricEvent).where(
            StyleReferenceMetricEvent.event_kind == STYLED_DRAFT_GATE_EVENT_KIND
        )
    ).scalars().all()
    assert [(event.context_json["stage"], event.outcome) for event in events] == [
        ("near_final_rewrite", "plagiarism")
    ]
    # API 回读（run/full / 工作台）按 bundle 合并 style_draft 与重写尝试的 notices
    readback = latest_style_notices(session, scene.scene_id, bundle_id=bundle["bundle_id"])
    assert [item["code"] for item in readback] == [STYLE_NOTICE_PLAGIARISM_HIT]


def test_api_style_notice_readers_are_scoped_to_the_current_run_bundle(session) -> None:
    """run/full 与工作台只回读本次运行 bundle 的 notices；解析不出 bundle 时不做无范围回读。"""
    from novel_system.api.routes.scenes import _attach_style_notices, _serialize_generation_summary

    scene = _seed_style_scene(session, project_id="proj_v2_scope")
    stale_hit = {"code": STYLE_NOTICE_PLAGIARISM_HIT, "message": "run 1", "severity": "blocking"}
    session.add(
        AttemptTracker(
            scene_id=scene.scene_id, chapter_id=scene.chapter_id, step="style_draft", status="completed",
            source_bundle_id="bundle_v1", details_json={"notices": [stale_hit]},
        )
    )
    # run 2（bundle_v2）：style_draft 没有 completed（hard_qc 挡下 / provider 失败）
    session.add(
        AttemptTracker(
            scene_id=scene.scene_id, chapter_id=scene.chapter_id, step="style_draft", status="failed",
            source_bundle_id="bundle_v2", details_json={"notices": [stale_hit]},
        )
    )
    state = session.get(SceneRunState, scene.scene_id)
    state.current_bundle_id = "bundle_v2"
    session.commit()

    blocked = {"scene_status": "hard_qc_blocked", "current_bundle_id": "bundle_v2"}
    assert _attach_style_notices(session, scene.scene_id, blocked) == blocked
    # 运行在建 bundle 之前早退：结果与状态都没有 bundle → 原样返回，而不是读别的运行
    state.current_bundle_id = None
    session.commit()
    early = {"scene_status": "preflight_blocked"}
    assert _attach_style_notices(session, scene.scene_id, early) == early
    # 拥有这些 notices 的运行照常拿到（结果里的 bundle id，或退而取 SceneRunState 的）
    owner = _attach_style_notices(
        session, scene.scene_id, {"scene_status": "human_review_required", "current_bundle_id": "bundle_v1"}
    )
    assert [item["code"] for item in owner["notices"]] == [STYLE_NOTICE_PLAGIARISM_HIT]
    state.current_bundle_id = "bundle_v1"
    session.commit()
    via_state = _attach_style_notices(session, scene.scene_id, {"scene_status": "human_review_required"})
    assert [item["code"] for item in via_state["notices"]] == [STYLE_NOTICE_PLAGIARISM_HIT]

    # 工作台生成摘要：与 llm_call 同一次运行（bundle_v2 的中性稿）→ 不带 bundle_v1 的旧 notice
    session.add(
        LlmCall(
            llm_call_id="llm_call_scope_v2", step="neutral_draft", scope_type="scene",
            scope_id=scene.scene_id, scene_id=scene.scene_id, chapter_id=scene.chapter_id,
        )
    )
    session.add(
        SceneDraft(
            row_id="draft_neutral_scope_v2", scene_id=scene.scene_id, chapter_id=scene.chapter_id,
            stage="neutral_draft", content="x", source_bundle_id="bundle_v2",
            source_bundle_hash="h_v2", generation_llm_call_id="llm_call_scope_v2",
        )
    )
    state.current_neutral_draft_row_id = "draft_neutral_scope_v2"
    state.current_bundle_id = "bundle_v2"
    session.commit()
    summary = _serialize_generation_summary(session, scene.scene_id, state)
    assert summary["raw_step"] == "neutral_draft"
    assert summary["notices"] == []
    state.current_bundle_id = "bundle_v1"
    session.commit()
    assert [
        item["code"] for item in _serialize_generation_summary(session, scene.scene_id, state)["notices"]
    ] == [STYLE_NOTICE_PLAGIARISM_HIT]


def test_latest_style_notices_reads_most_recent_completed_style_attempt(session) -> None:
    scene = _seed_style_scene(session, project_id="proj_v2_readback")
    older = AttemptTracker(
        scene_id=scene.scene_id, chapter_id=scene.chapter_id, step="style_draft", status="completed",
        source_bundle_id="bundle_old",
        details_json={"notices": [{"code": STYLE_NOTICE_INJECTION_MISS, "message": "old", "severity": "warning"}]},
    )
    newer = AttemptTracker(
        scene_id=scene.scene_id, chapter_id=scene.chapter_id, step="style_draft", status="completed",
        source_bundle_id="bundle_new",
        details_json={
            "notices": [
                {"code": STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL, "message": "new", "severity": "warning"},
                {"not": "a notice"},
                "garbage",
            ]
        },
    )
    failed = AttemptTracker(
        scene_id=scene.scene_id, chapter_id=scene.chapter_id, step="style_draft", status="failed",
        source_bundle_id="bundle_new",
        details_json={"notices": [{"code": STYLE_NOTICE_PLAGIARISM_HIT, "message": "x", "severity": "blocking"}]},
    )
    session.add_all([older, newer, failed])
    session.commit()
    assert [item["code"] for item in latest_style_notices(session, scene.scene_id)] == [
        STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL
    ]
    assert [item["code"] for item in latest_style_notices(session, scene.scene_id, bundle_id="bundle_old")] == [
        STYLE_NOTICE_INJECTION_MISS
    ]
    assert latest_style_notices(session, "CH901_SC99") == []

    # 路由层透传：运行结果 dict 追加本次运行 bundle 的 notices（非 dict 原样返回、无 notice 不加键）
    from novel_system.api.routes.scenes import _attach_style_notices

    attached = _attach_style_notices(
        session, scene.scene_id, {"scene_status": "archived", "current_bundle_id": "bundle_new"}
    )
    assert [item["code"] for item in attached["notices"]] == [STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL]
    assert attached["scene_status"] == "archived"
    assert _attach_style_notices(
        session, "CH901_SC99", {"scene_status": "x", "current_bundle_id": "bundle_new"}
    ) == {"scene_status": "x", "current_bundle_id": "bundle_new"}
    assert _attach_style_notices(session, scene.scene_id, "not-a-dict") == "not-a-dict"
