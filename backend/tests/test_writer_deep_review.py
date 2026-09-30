from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from novel_system.db.models import (
    AuthorPreferenceProfile,
    ChapterGoal,
    FinalScene,
    LlmCall,
    PassagePatchCandidate,
    ReviewItem,
    SceneCard,
    SceneRunState,
    StoryProject,
    WriterEvaluation,
)
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.llm_client import LLMResponse, OnlineAccountedExecution
from novel_system.services.writer_deep_review import LITERARY_REVISION_RUBRIC_ID
from novel_system.services.writer_deep_review import WriterDeepReviewService
from novel_system.services.writer_deep_review import _normalize_deep_review_output


import pytest as _pytest_wr
from tests.real_llm_fakes import install_online_writer_pipeline as _install_online_writer_pipeline


@_pytest_wr.fixture(autouse=True)
def _auto_online_writer(monkeypatch):
    """假生成已退役：作家评审/深评/passage-patch 走在线记账替身（按 node_id 派发）。"""
    _install_online_writer_pipeline(monkeypatch)


CHAPTER_ID = "DEEP_CH01"
SCENE_ID = "DEEP_CH01_SC01"
FINAL_ROW_ID = "final_DEEP_CH01_SC01"
PROJECT_ID = "PROJECT_DEEP_CH01"


def test_normalize_deep_review_output_validates_model_lens_evaluations() -> None:
    normalized = _normalize_deep_review_output(
        {
            "overall_score": 0.6,
            "scores": {"choice_pressure": 0.5},
            "findings": [
                {"lens": "story", "dimension": "choice_pressure", "severity": "revision", "issue": "选择没有落成动作。"},
                {"lens": "reader", "dimension": "ending_drive", "severity": "taste", "issue": "结尾可以更硬。"},
            ],
            "revision_brief": [],
            "requires_human_review": False,
            "lens_evaluations": [
                {"lens": " Story ", "overall_score": 1.7, "scores": {"choice_pressure": 0.5, "bogus_dim": 0.1}, "findings": [{"dimension": "choice_pressure", "severity": "nonsense", "issue": "a"}]},
                {"lens": "story", "findings": [{"dimension": "dialogue_subtext", "severity": "taste", "issue": "b"}]},
                {"lens": "camera"},
                "garbage",
            ],
        }
    )
    by_lens = {entry["lens"]: entry for entry in normalized["lens_evaluations"]}
    # 非法镜头名整条丢弃，大小写/空白容错，重复镜头合并，漏掉的镜头从顶层 findings 重建
    assert set(by_lens) == {"story", "reader"}
    # 模板声明 0–1 分：越界的 1.7 丢掉，不夹成满分（与 review_scores.normalize_score 同一口径）
    assert by_lens["story"]["overall_score"] is None
    assert "bogus_dim" not in by_lens["story"]["scores"]
    merged = by_lens["story"]["findings"]
    assert [item["issue"] for item in merged] == ["a", "b"]
    assert all(item["lens"] == "story" for item in merged)
    assert merged[0]["severity"] == "revision"
    assert by_lens["reader"]["findings"][0]["issue"] == "结尾可以更硬。"
    assert len(by_lens["story"]["revision_brief"]) == 2


def test_normalize_deep_review_output_rebuilds_lenses_when_model_omits_grouping() -> None:
    normalized = _normalize_deep_review_output(
        {
            "overall_score": 0.7,
            "scores": {},
            "findings": [
                {"lens": "THEME", "dimension": "theme_pressure", "severity": "revision", "issue": "主题压力未落到选择上。"},
                {"lens": "看不懂", "dimension": "choice_pressure", "severity": "taste", "issue": "非法镜头归入 story。"},
            ],
            "revision_brief": [],
            "requires_human_review": False,
        }
    )
    by_lens = {entry["lens"]: entry for entry in normalized["lens_evaluations"]}
    assert set(by_lens) == {"story", "theme"}
    assert by_lens["theme"]["findings"][0]["issue"] == "主题压力未落到选择上。"
    assert by_lens["story"]["findings"][0]["lens"] == "story"


def _seed_finished_scene(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="Deep review project", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="林岑必须决定是否公开导师留下的失踪案证据。",
            main_plot_push="把旧档案线推进到公开真相的选择。",
            emotional_target="从职业克制转向道德压力。",
            ending_effect="读者知道她已经不能只做修复师。",
            writer_brief_json={
                "chapter_promise": "真相和保护幸存者不能同时满足。",
                "relationship_delta": "林岑开始不再完全信任许望的判断。",
                "ending_question": "她会把证据交给谁？",
            },
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            scene_seq=1,
            scene_goal="林岑发现关键录音，但必须决定公开还是隐藏。",
            beats_json=["修复录音", "听见幸存者编号", "决定暂缓公开"],
            exit_change="她把一半证据藏起来，准备独自核查。",
            hook="录音最后出现她自己的心跳声。",
            writer_brief_json={
                "character_desire": "确认导师留下的证据是否真实。",
                "choice_under_pressure": "公开证据或先保护幸存者。",
                "power_shift": "林岑从被动修复者变成证据持有人。",
                "reader_aftertaste": "她越冷静，越像在越界。",
            },
        )
    )
    session.add(
        SceneRunState(
            scene_id=SCENE_ID,
            scene_status="archived",
            current_final_scene_row_id=FINAL_ROW_ID,
            current_bundle_id="bundle_deep",
            current_bundle_hash="hash_deep",
        )
    )
    session.add(
        FinalScene(
            row_id=FINAL_ROW_ID,
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            content=(
                "林岑的手指在潮湿的档案盒边缘停顿。盐霜泛着幽蓝的冷光。"
                "她低声说证据还不能公开，然后又解释这是为了保护所有人。"
                "许望没有回答，录音里传来三声钟响。林岑的手指再次停顿。"
            ),
            status="approved",
            source_bundle_id="bundle_deep",
            source_bundle_hash="hash_deep",
        )
    )
    session.commit()


def test_scene_deep_review_is_fail_closed_without_a_live_llm(client: TestClient, session) -> None:
    """2026-09-22：深评是拒绝式节点——没有真实模型就 409 + author_action，不再有本地词表兜底，也不动历史。"""

    _seed_finished_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WRITER_DEEP_REVIEW_LLM_REQUIRED"
    session.expire_all()
    assert session.query(WriterEvaluation).filter_by(object_type="scene", object_id=SCENE_ID).count() == 0

    # 服务层：author_action 指向系统配置
    from novel_system.services.errors import DomainError

    try:
        WriterDeepReviewService(session).run_scene_review(SCENE_ID)
    except DomainError as exc:
        assert exc.code == "WRITER_DEEP_REVIEW_LLM_REQUIRED"
        assert exc.details["author_action"]["target_view"] == "config"
        assert exc.details["node_id"] == "writer_deep_review"
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("deep review must refuse without a live LLM")


def test_scene_deep_review_get_returns_the_unified_diagnosis_before_any_ai_run(client: TestClient, session) -> None:
    """GET 是统一诊断载荷：规则 / 节奏发现已经在，AI 部分标 not_run；旧契约的键照旧。"""

    _seed_finished_scene(session)

    response = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["status"] == "not_run"
    assert payload["latest_evaluation"] is None
    assert payload["rubric_id"] == LITERARY_REVISION_RUBRIC_ID
    assert payload["ai"]["status"] == "not_run"
    assert payload["text"]["layer"] == "runtime_final_scene"
    assert payload["findings"], "the rule dimensions already diagnose the final text"
    assert {item["source"] for item in payload["findings"]} <= {"rules", "craft"}
    assert all(item["signal_id"] and item["label"] and item["issue"] for item in payload["findings"])
    assert payload["summary"]["open"] == len(payload["findings"])


def test_scene_deep_review_uses_llm_when_live(client: TestClient, session, monkeypatch) -> None:
    class FakeDeepReviewRunner:
        def __init__(self, db_session, **kwargs) -> None:
            self.session = db_session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            assert kwargs["node_id"] == "writer_deep_review"
            context = kwargs["context"]
            assert context.scope_type == "scene"
            assert context.scope_id == SCENE_ID
            assert context.project_id == PROJECT_ID
            assert context.chapter_id == CHAPTER_ID
            assert context.scene_id == SCENE_ID
            return SimpleNamespace(
                llm_call_id="llm_call_writer_deep_review_test",
                response=SimpleNamespace(
                    structured_output={
                        "overall_score": 0.61,
                        "scores": {"choice_pressure": 0.55, "dialogue_subtext": 0.6},
                        "findings": [
                            {
                                "lens": "story",
                                "dimension": "choice_pressure",
                                "severity": "revision",
                                "classification": "revision",
                                "issue": "The choice is described rather than enacted.",
                                "recommendation": "Rewrite the choice as a visible action.",
                                "evidence_excerpt": "truth mattered",
                                "evidence_location": "scene body",
                                "why_it_matters": "Deep review must use textual evidence.",
                                "scene_form": "plot_scene",
                            }
                        ],
                        "revision_brief": [
                            {
                                "classification": "revision",
                                "dimension": "choice_pressure",
                                "recommendation": "Make the choice visible.",
                            }
                        ],
                        "lens_evaluations": [
                            {
                                "lens": "story",
                                "overall_score": 0.61,
                                "scores": {"choice_pressure": 0.55},
                                "findings": [],
                                "revision_brief": [],
                            }
                        ],
                    }
                ),
            )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", FakeDeepReviewRunner)
    _seed_finished_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    evaluation = response.json()["data"]["latest_evaluation"]
    assert evaluation["evaluator_llm_call_id"] == "llm_call_writer_deep_review_test"
    assert evaluation["overall_score"] == 0.61
    assert evaluation["findings"][0]["issue"] == "The choice is described rather than enacted."


def test_a_lens_the_model_scored_zero_keeps_its_zero(client: TestClient, session, monkeypatch) -> None:
    """模型给一个镜头打 0 分就是 0 分：不当作「没给」、再拿各维分的平均顶上（B05-05：``or`` 把 0.0 当成缺失）。"""

    class ZeroLensRunner:
        def __init__(self, db_session, **kwargs) -> None:
            self.session = db_session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            return SimpleNamespace(
                llm_call_id="llm_call_zero_lens",
                response=SimpleNamespace(
                    structured_output={
                        "overall_score": 0.4,
                        "scores": {"choice_pressure": 0.5},
                        "findings": [],
                        "revision_brief": [],
                        "lens_evaluations": [
                            {"lens": "story", "overall_score": 0.0, "scores": {"choice_pressure": 0.5}, "findings": [], "revision_brief": []}
                        ],
                    }
                ),
            )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", ZeroLensRunner)
    _seed_finished_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    lenses = {item["lens"]: item for item in response.json()["data"]["ai"]["lenses"]}
    assert lenses["story"]["overall_score"] == 0.0


def test_scene_deep_review_prefers_current_author_draft_over_runtime_final(client: TestClient, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    AuthorDraftService(session).save(
        draft["draft_id"],
        {
            "content": "林岑把证据袋压进袖口，没有解释，只问许望：你要我现在开门吗？",
            "base_revision_no": draft["revision_no"],
        },
        actor_ref="writer",
    )
    session.commit()

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    evaluation = response.json()["data"]["latest_evaluation"]
    assert evaluation["source_text_ref"] == f"author_draft:{draft['draft_id']}"


def test_passage_patch_candidate_accepts_without_overwriting_final_and_learns_no_preference(client: TestClient, session) -> None:
    _seed_finished_scene(session)
    original_final = session.get(FinalScene, FINAL_ROW_ID).content

    create_response = client.post(
        "/api/v1/passages/patch-candidates",
        json={
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"final_scene:{FINAL_ROW_ID}",
            "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
            "issue_dimension": "dialogue_subtext",
        },
    )

    assert create_response.status_code == 200
    candidate = create_response.json()["data"]["candidate"]
    assert candidate["status"] == "candidate"
    assert candidate["manual_only"] is True
    assert len(candidate["replacement_options"]) == 3
    assert {option["tone"] for option in candidate["replacement_options"]} == {"shorter", "sharper", "subtler"}

    accept_response = client.post(
        f"/api/v1/passage-patch-candidates/{candidate['patch_id']}/accept",
        json={"selected_option_id": candidate["replacement_options"][1]["option_id"], "note": "更有刺。"},
    )

    assert accept_response.status_code == 200
    accepted = accept_response.json()["data"]["candidate"]
    assert accepted["status"] == "accepted"
    assert accepted["author_decision"] == "accepted"

    session.expire_all()
    assert session.get(FinalScene, FINAL_ROW_ID).content == original_final
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.selected_option_id == candidate["replacement_options"][1]["option_id"]
    assert row.author_decision_note == "更有刺。"

    # 写作偏好学习已退役（批准 #6）：采纳只记在候选行上，不再重建偏好画像、不往待办里塞「写作偏好」卡
    assert session.query(AuthorPreferenceProfile).count() == 0
    assert session.query(ReviewItem).count() == 0
    assert client.get("/api/v1/author-preference-profile").status_code == 404


def test_rejecting_passage_patch_updates_only_the_candidate(client: TestClient, session) -> None:
    _seed_finished_scene(session)

    candidate = client.post(
        "/api/v1/passages/patch-candidates",
        json={
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"final_scene:{FINAL_ROW_ID}",
            "source_excerpt": "林岑的手指再次停顿。",
            "issue_dimension": "repetitive_expression",
        },
    ).json()["data"]["candidate"]

    reject_response = client.post(
        f"/api/v1/passage-patch-candidates/{candidate['patch_id']}/reject",
        json={"note": "这处重复保留为人物习惯。"},
    )

    assert reject_response.status_code == 200
    rejected = reject_response.json()["data"]["candidate"]
    assert rejected["status"] == "rejected"
    assert rejected["author_decision"] == "rejected"
    assert rejected["author_decision_note"] == "这处重复保留为人物习惯。"

    session.expire_all()
    assert session.query(AuthorPreferenceProfile).count() == 0
    assert session.query(ReviewItem).count() == 0


class ScriptedPassagePatchClient(OnlineAccountedExecution):
    def __init__(self) -> None:
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        output = {
            "patches": [
                {
                    "target_text_ref": "author_draft:test",
                    "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
                    "replacement_text": "她把证据袋压在袖口下，只问许望：你也要我现在开门吗？",
                    "patch_type": "replace_excerpt",
                    "changed_dimensions": ["dialogue_subtext", "choice_pressure"],
                    "why_it_helps": "把解释改成动作和反问，关系压力更清楚。",
                },
                {
                    "target_text_ref": "author_draft:test",
                    "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
                    "replacement_text": "证据袋在她袖口里折出硬角。她没有解释，只把门锁重新扣上。",
                    "patch_type": "replace_excerpt",
                    "changed_dimensions": ["information_rhythm", "image_necessity"],
                    "why_it_helps": "用物件和动作承载信息，减少说明。",
                },
            ],
            "rationale": "保留事实，把解释句改成选择压力。",
            "manual_only": True,
        }
        return LLMResponse(
            request_id="patch_req_1",
            provider="fake",
            model=request.model,
            text="{}",
            structured_output=output,
            response_format=request.response_format,
            raw_response={"id": "patch_req_1", "model": request.model},
            usage={"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            finish_reason="stop",
        )

    def generate_accounted(self, request, *, accounting_hook):
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def test_passage_patch_candidate_uses_writer_passage_patch_llm_and_records_provenance(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    llm_client = ScriptedPassagePatchClient()

    result = WriterDeepReviewService(session, llm_client=llm_client).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
            "issue_dimension": "dialogue_subtext",
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]
    assert llm_client.requests[0].node_id == "writer_passage_patch"
    assert candidate["source_draft_id"] == draft["draft_id"]
    assert candidate["generation_llm_call_id"]
    assert candidate["rationale"] == "保留事实，把解释句改成选择压力。"
    assert [option["label"] for option in candidate["replacement_options"]] == ["版本 1", "版本 2"]
    assert candidate["replacement_options"][0]["tone"] == "dialogue_subtext"

    session.expire_all()
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.source_draft_id == draft["draft_id"]
    assert row.generation_llm_call_id == candidate["generation_llm_call_id"]
    assert session.get(LlmCall, candidate["generation_llm_call_id"]).node_id == "writer_passage_patch"
    patch_call = session.get(LlmCall, candidate["generation_llm_call_id"])
    assert patch_call.scope_type == "scene"
    assert patch_call.scope_id == SCENE_ID
    assert patch_call.project_id == PROJECT_ID


def test_passage_patch_candidate_records_category_range_strategy_and_preference_tags(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]

    result = WriterDeepReviewService(session).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "她低声说证据还不能公开，然后又解释这是为了保护所有人。",
            "issue_dimension": "dialogue_subtext",
            "candidate_category": "dialogue_rewrite",
            "target_range": {"start": 8, "end": 32, "unit": "char"},
            "revision_strategy": "反问替代解释",
            "preference_tags": ["少解释", "对白更短", "动作承压"],
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]

    assert candidate["candidate_category"] == "dialogue_rewrite"
    assert candidate["target_range"] == {"start": 8, "end": 32, "unit": "char"}
    assert candidate["revision_strategy"] == "反问替代解释"
    assert candidate["preference_tags"] == ["少解释", "对白更短", "动作承压"]
    assert candidate["inserted_into_author_draft"] is False

    session.expire_all()
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.candidate_category == "dialogue_rewrite"
    assert row.target_range_json == {"start": 8, "end": 32, "unit": "char"}
    assert row.revision_strategy == "反问替代解释"
    assert row.preference_tags_json == ["少解释", "对白更短", "动作承压"]
    assert row.inserted_into_author_draft == 0


def test_passage_patch_candidate_records_quality_signal_id_for_quality_handoff(session) -> None:
    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]

    result = WriterDeepReviewService(session).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "林岑的手指再次停顿。",
            "issue_dimension": "template_action_reuse",
            "quality_signal_id": "quality:scene:DEEP_CH01_SC01:template_action_reuse",
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]

    assert candidate["quality_signal_id"] == "quality:scene:DEEP_CH01_SC01:template_action_reuse"

    session.expire_all()
    row = session.get(PassagePatchCandidate, candidate["patch_id"])
    assert row.quality_signal_id == "quality:scene:DEEP_CH01_SC01:template_action_reuse"


def test_passage_patch_prompt_carries_no_author_preference_section(session) -> None:
    """写作偏好学习已退役（批准 #6）：库里即使留着一份「已批准」的偏好画像（旧数据），局部改写的提示词里也没有偏好段。"""

    _seed_finished_scene(session)
    draft = AuthorDraftService(session).ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    session.add(
        AuthorPreferenceProfile(
            profile_id="author_pref_draft_ignored",
            scope_type="global",
            scope_ref_id="global",
            status="draft",
            runtime_eligible=0,
            summary_json={"preferred_revision_moves": ["草稿偏好不应进入提示词"]},
            source_patch_ids_json=[],
        )
    )
    session.add(
        AuthorPreferenceProfile(
            profile_id="author_pref_approved_runtime",
            scope_type="global",
            scope_ref_id="global",
            status="approved",
            runtime_eligible=1,
            summary_json={"preferred_revision_moves": ["更锋利的反问"], "rejected_revision_moves": ["解释性对白"]},
            source_patch_ids_json=[],
        )
    )
    session.flush()
    llm_client = ScriptedPassagePatchClient()

    WriterDeepReviewService(session, llm_client=llm_client).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "target_text_ref": f"author_draft:{draft['draft_id']}",
            "source_draft_id": draft["draft_id"],
            "source_excerpt": "林岑的手指再次停顿。",
            "issue_dimension": "repetitive_expression",
        },
        actor_ref="writer",
    )

    user_prompt = llm_client.requests[0].messages[1]["content"]
    assert "Author Preference" not in user_prompt
    assert "更锋利的反问" not in user_prompt and "解释性对白" not in user_prompt
    assert "草稿偏好不应进入提示词" not in user_prompt


# ---------------------------------------------------------------------------
# 三个 LLM 流程共用一次节点调用：失败只翻译一次；没有正文不调模型（审计 B05-06 / B05-10、B09-24）
# ---------------------------------------------------------------------------


def _failing_runner(error_code: str, calls: list):
    from novel_system.services.llm_task_runner import LLMNodeExecutionError

    class _Runner:
        def __init__(self, db_session, **kwargs) -> None:
            self.session = db_session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            calls.append(kwargs)
            raise LLMNodeExecutionError(
                llm_call_id="llm_call_failed",
                error_code=error_code,
                message=f"{error_code} from the fake runner",
                request_summary={},
                response_summary={"error_code": error_code},
            )

    return _Runner


def _patch_request(**overrides):
    return {
        "object_type": "scene",
        "object_id": SCENE_ID,
        "chapter_id": CHAPTER_ID,
        "scene_id": SCENE_ID,
        "target_text_ref": f"final_scene:{FINAL_ROW_ID}",
        "source_excerpt": "林岑的手指再次停顿。",
        "issue_dimension": "repetitive_expression",
        **overrides,
    }


def test_a_provider_failure_is_a_502_and_a_missing_route_a_409_on_every_writer_node(client: TestClient, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    _seed_finished_scene(session)
    calls: list = []

    # 上游模型失败（超时 / 服务报错）：502 + 节点自己的 failure 码（以前深评一律 409，局部改写整个漏成 500 INTERNAL_ERROR）
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _failing_runner("LLM_PROVIDER_TIMEOUT", calls))
    deep = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")
    assert deep.status_code == 502 and deep.json()["error"]["code"] == "WRITER_DEEP_REVIEW_LLM_FAILED"
    passage = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_index": 0})
    assert passage.status_code == 502 and passage.json()["error"]["code"] == "WRITER_DEEP_REVIEW_LLM_FAILED"
    patch = client.post("/api/v1/passages/patch-candidates", json=_patch_request())
    assert patch.status_code == 502 and patch.json()["error"]["code"] == "WRITER_PASSAGE_PATCH_LLM_FAILED"
    assert [call["node_id"] for call in calls] == ["writer_deep_review", "writer_deep_review", "writer_passage_patch"]
    assert [call["step"] for call in calls] == ["writer_deep_review", "writer_passage_review", "writer_passage_patch"]

    # 节点没有路由（作者能处理的配置问题）：409 + 能力码，next_action 指向系统设置
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _failing_runner("LLM_ROUTE_NOT_CONFIGURED", calls))
    patch = client.post("/api/v1/passages/patch-candidates", json=_patch_request())
    assert patch.status_code == 409
    error = patch.json()["error"]
    assert error["code"] == "WRITER_PASSAGE_PATCH_LLM_REQUIRED"
    assert error["details"]["next_action"].startswith("configure_") and error["details"]["node_id"] == "writer_passage_patch"
    session.expire_all()
    assert session.query(WriterEvaluation).count() == 0 and session.query(PassagePatchCandidate).count() == 0


def test_deep_review_without_any_text_refuses_before_calling_the_model(client: TestClient, session, monkeypatch) -> None:
    """没有正文不调模型（审计 B05-06）：整场深评与整章通读都 409 WRITER_DEEP_REVIEW_NO_TEXT，不花一次调用。"""

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    calls: list = []
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _failing_runner("LLM_PROVIDER_TIMEOUT", calls))
    _seed_finished_scene(session)
    final = session.get(FinalScene, FINAL_ROW_ID)
    final.content = "   "
    session.commit()

    scene = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")
    assert scene.status_code == 409 and scene.json()["error"]["code"] == "WRITER_DEEP_REVIEW_NO_TEXT"
    chapter = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review")
    assert chapter.status_code == 409 and chapter.json()["error"]["code"] == "WRITER_DEEP_REVIEW_NO_TEXT"
    assert calls == []

    # 没有模型时照旧先说「要模型」（与局部深评同一个先后）
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    denied = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "WRITER_DEEP_REVIEW_LLM_REQUIRED"


# ---------------------------------------------------------------------------
# 局部改写 v4（重评 R12）与深评不编分数（审计 B05-04 / B05-05）
# ---------------------------------------------------------------------------


class _PatchRecordingClient(OnlineAccountedExecution):
    """按脚本回 writer_passage_patch 的在线记账替身，记下发出去的每个请求（含 response schema）。"""

    def __init__(self, patches: list[dict]) -> None:
        self.patches = patches
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        output = {"patches": self.patches, "rationale": "按段换回。", "manual_only": True}
        return LLMResponse(
            request_id=f"patch_v4_{len(self.requests)}",
            provider="fake",
            model=request.model,
            text="{}",
            structured_output=output,
            response_format=request.response_format,
            raw_response={"id": "patch_v4", "model": request.model},
            usage={"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            finish_reason="stop",
        )

    def generate_accounted(self, request, *, accounting_hook):
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def _seed_multi_paragraph_draft(session) -> dict:
    _seed_finished_scene(session)
    service = AuthorDraftService(session)
    draft = service.ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    html = (
        "<p>门外很安静，潮水退得很远。</p>"
        "<p>林岑把录音机按停。“你听见了吗？”</p>"
        "<p>“听见了。”许望说。</p>"
        "<p>钟响了三声，她没有回头。</p>"
    )
    service.save(draft["draft_id"], {"content": html, "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    return service.ensure("scene", SCENE_ID, actor_ref="writer")["draft"]


def _patch_body(draft: dict, excerpt: str) -> dict:
    return {
        "object_type": "scene",
        "object_id": SCENE_ID,
        "chapter_id": CHAPTER_ID,
        "scene_id": SCENE_ID,
        "target_text_ref": f"author_draft:{draft['draft_id']}",
        "source_draft_id": draft["draft_id"],
        "source_excerpt": excerpt,
        "issue_dimension": "author_instruction",
        "instruction": "对话化",
    }


def test_cross_paragraph_rewrites_come_back_as_paragraphs_with_both_seams_in_the_prompt(session) -> None:
    draft = _seed_multi_paragraph_draft(session)
    excerpt = "把录音机按停。“你听见了吗？”\n“听见了。”许望说。"
    client = _PatchRecordingClient(
        [
            {"tone": "sharper", "paragraphs": ["她按停录音机。“听见了？”", "“嗯。”许望没有抬头。"], "patch_type": "replace_excerpt", "changed_dimensions": ["dialogue_subtext"], "why_it_helps": "短。"},
            {"tone": "subtler", "paragraphs": ["她按停录音机，只问了一句。", "“听见了。”"], "patch_type": "replace_excerpt", "changed_dimensions": ["dialogue_subtext"], "why_it_helps": "留白。"},
            # 把两段对白挤成了一段：不要（不替作者拼进正文）
            {"tone": "shorter", "paragraphs": ["她按停录音机。“听见了？”“嗯。”"], "patch_type": "replace_excerpt", "changed_dimensions": ["information_rhythm"], "why_it_helps": "更短。"},
        ]
    )

    candidate = WriterDeepReviewService(session, llm_client=client).create_patch_candidate(_patch_body(draft, excerpt), actor_ref="writer")["candidate"]

    options = candidate["replacement_options"]
    assert [option["paragraphs"] for option in options] == [["她按停录音机。“听见了？”", "“嗯。”许望没有抬头。"], ["她按停录音机，只问了一句。", "“听见了。”"]]
    assert options[0]["replacement_text"] == "她按停录音机。“听见了？”\n“嗯。”许望没有抬头。"
    request = client.requests[-1]
    user_prompt = request.messages[-1]["content"]
    # 原文一行一段地给，两头的接缝都在（第一段里选区之前的字、上一段；最后一段里选区之后的字、下一段）
    assert "Source Paragraphs: 2" in user_prompt and "Source Excerpt:\n把录音机按停。“你听见了吗？”\n“听见了。”许望说。" in user_prompt
    assert "Paragraph Before: 门外很安静，潮水退得很远。" in user_prompt
    assert "Same Paragraph, Before The Passage: 林岑" in user_prompt
    assert "Paragraph After: 钟响了三声，她没有回头。" in user_prompt
    assert "<p>" not in user_prompt, "接缝是可见文字，不是作者稿的 HTML"
    assert user_prompt.count("你听见了吗") == 1, "原文不再在摘要里重复一份"
    schema = request.response_schema["schema"]
    assert schema["properties"]["patches"]["maxItems"] == 3
    assert schema["properties"]["patches"]["items"]["required"] == ["paragraphs", "patch_type", "changed_dimensions", "why_it_helps"]
    assert "source_excerpt" not in schema["properties"]["patches"]["items"]["properties"]


def test_long_passages_are_capped_at_two_options_and_the_server_refuses_over_the_limit(session, monkeypatch) -> None:
    draft = _seed_multi_paragraph_draft(session)
    long_excerpt = "她把录音机按停。" * 130  # 1,040 字：超过 1,000 字只要两个选项
    client = _PatchRecordingClient(
        [
            {"paragraphs": ["甲版。"], "patch_type": "replace_excerpt", "changed_dimensions": ["x"], "why_it_helps": "a"},
            {"paragraphs": ["乙版。"], "patch_type": "replace_excerpt", "changed_dimensions": ["x"], "why_it_helps": "b"},
        ]
    )
    candidate = WriterDeepReviewService(session, llm_client=client).create_patch_candidate(_patch_body(draft, long_excerpt), actor_ref="writer")["candidate"]
    assert len(candidate["replacement_options"]) == 2
    patches_schema = client.requests[-1].response_schema["schema"]["properties"]["patches"]
    assert patches_schema["maxItems"] == 2 and patches_schema["minItems"] == 2

    # 与写作台同一个上限（2,000 字，按码点数）：超了不截短、不发请求
    from novel_system.services.errors import DomainError

    try:
        WriterDeepReviewService(session, llm_client=client).create_patch_candidate(_patch_body(draft, "字" * 2001), actor_ref="writer")
    except DomainError as exc:
        assert exc.code == "PASSAGE_PATCH_TOO_LONG" and exc.status_code == 400
        assert exc.details == {"length": 2001, "limit": 2000} and "请分段改写" in exc.message
    else:  # pragma: no cover
        raise AssertionError("an over-long selection must be refused")
    assert len(client.requests) == 1


def test_a_patch_with_no_usable_option_is_a_502_without_author_action(client: TestClient, session, monkeypatch) -> None:
    """拒绝式：模型给的改写全不能用（空的，或者都把几段挤成一段）→ 502 WRITER_PASSAGE_PATCH_EMPTY，不带 author_action
    （这不是配置问题），不留候选行；以前拿写死的演示句子凑成三个选项。"""

    draft = _seed_multi_paragraph_draft(session)
    collapsing = _PatchRecordingClient([{"paragraphs": ["她按停录音机。“听见了？”“嗯。”"], "patch_type": "replace_excerpt", "changed_dimensions": ["x"], "why_it_helps": "a"}])
    monkeypatch.setattr(
        "novel_system.services.writer_deep_review.LLMNodeRunner",
        lambda db_session, **kwargs: __import__("novel_system.services.llm_task_runner", fromlist=["LLMNodeRunner"]).LLMNodeRunner(db_session, llm_client=collapsing),
    )
    response = client.post("/api/v1/passages/patch-candidates", json=_patch_body(draft, "把录音机按停。“你听见了吗？”\n“听见了。”许望说。"))
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "WRITER_PASSAGE_PATCH_EMPTY" and "author_action" not in error.get("details", {})
    assert "给出可用的改写" in error["message"]
    session.expire_all()
    assert session.query(PassagePatchCandidate).count() == 0


def test_accepting_a_cross_paragraph_rewrite_records_the_reading(session, monkeypatch) -> None:
    """采纳跨段的改写之后照样记一条「像不像」读数：作者稿的可见文字段落之间只隔一个空格，原句与改写之间是换行，
    以前原样比对永远对不上，读数悄悄不记（重评 R12 复核补充 7）。"""

    draft = _seed_multi_paragraph_draft(session)
    excerpt = "把录音机按停。“你听见了吗？”\n“听见了。”许望说。"
    session.add(
        PassagePatchCandidate(
            patch_id="patch_cross_paragraph",
            object_type="scene",
            object_id=SCENE_ID,
            scene_id=SCENE_ID,
            source_draft_id=draft["draft_id"],
            source_excerpt=excerpt,
            issue_dimension="author_instruction",
            replacement_options_json=[{"option_id": "option_llm_1", "paragraphs": ["按停了。", "“嗯。”"], "replacement_text": "按停了。\n“嗯。”"}],
        )
    )
    session.commit()
    readings: list[dict] = []
    monkeypatch.setattr(
        "novel_system.services.writer_deep_review_patches.record_author_draft_reading",
        lambda db_session, **kwargs: readings.append(kwargs),
    )

    WriterDeepReviewService(session).accept_patch_candidate("patch_cross_paragraph", {"selected_option_id": "option_llm_1"})

    assert len(readings) == 1
    assert readings[0]["draft_ref"] == "passage_patch:patch_cross_paragraph:option_llm_1"
    assert "林岑按停了。 “嗯。” 钟响了三声" in readings[0]["text"]


def test_deep_review_invents_no_scores(client: TestClient, session, monkeypatch) -> None:
    """模型没给的分不编（审计 B05-05）：没给的维度就没有分（以前一律 0.78），没给总分就是空（面板显示「—」，
    以前拿掺着 0.78 的平均凑一个），按发现重建的镜头没有分（以前按严重度编 0.42 / 0.58 / 0.72），没给维度的
    发现是「评审意见」（以前冒充「抉择压力」）。"""

    normalized = _normalize_deep_review_output(
        {
            "scores": {"choice_pressure": 0.5, "voice_distinction": 7.5},
            "findings": [
                {"lens": "prose", "severity": "revision", "issue": "叙述声音偏平。", "recommendation": "放慢。"},
                {"lens": "story", "dimension": "choice_pressure", "severity": "blocking", "issue": "选择没落地。", "recommendation": "落成动作。"},
            ],
            "revision_brief": [],
        }
    )
    assert normalized["scores"] == {"choice_pressure": 0.5}, "越界的 7.5 丢掉，其余维度不补"
    assert normalized["overall_score"] is None
    by_lens = {entry["lens"]: entry for entry in normalized["lens_evaluations"]}
    assert set(by_lens) == {"story", "prose"}
    assert all(entry["scores"] == {} and "overall_score" not in entry for entry in by_lens.values())
    assert [finding["dimension"] for finding in normalized["findings"]] == ["review_note", "choice_pressure"]
    assert all("scene_form" not in finding for finding in normalized["findings"])

    class NoTotalRunner:
        def __init__(self, db_session, **kwargs) -> None:
            self.session = db_session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            return SimpleNamespace(
                llm_call_id="llm_call_no_total",
                response=SimpleNamespace(structured_output={"scores": {"choice_pressure": 0.4}, "findings": [], "revision_brief": []}),
            )

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", NoTotalRunner)
    _seed_finished_scene(session)
    payload = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert payload["ai"]["status"] == "current" and payload["ai"]["overall_score"] is None
