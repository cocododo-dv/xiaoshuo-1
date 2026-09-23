"""Wave 2（结果闭环治理 §5.4 + Wave 2 条目）：QC 分级和可靠成稿模式。

完成门可复算证明（G-03 核心回归）：
- 旧三章阻断形状（软 QC LLM 要求人工审阅、硬 QC LLM 要求重写、near-final 二评仍 fail、
  QC 执行失败）在无确定性 Q0/Q1 证据时**必须交付可编辑正文并归档**；
- 只有确定性复核过的 Q0/Q1 才能阻断归档（管线内 + adopt-current 双向）。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    QcReport,
    RelationProfile,
    SceneCard,
    SceneRunState,
    StoryProject,
    VoiceProfile,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.near_final import NearFinalAcceptanceService
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services import scene_generation as scene_generation_module
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import install_online_pipeline


@pytest.fixture(autouse=True)
def _online_pipeline(monkeypatch) -> None:
    """假生成已退役：给未显式注入的蓝图/规划/近终审子服务兜底在线记账替身。"""
    install_online_pipeline(monkeypatch)

PROJECT_ID = "PROJECT200"
SCENE_ID = "CH200_SC01"
CHAPTER_ID = "CH200"


def _response(payload: dict, *, request_id: str, model: str) -> LLMResponse:
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model=model,
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": model,
            "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            "finish_reason": "stop",
        },
        usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
        finish_reason="stop",
    )


class FakeSceneClient(AccountedGenerateMixin):
    """草稿生成序列：neutral → style → 后续均为 patch/rewrite。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests)
        if index == 1:
            payload = {"scene_text": "Provider-generated neutral scene text.", "continuity_notes": []}
        elif index == 2:
            payload = {"scene_text": "Provider-generated style scene text.", "style_notes": []}
        else:
            payload = {"scene_text": "Provider-generated patched scene text.", "style_notes": []}
        return _response(payload, request_id=f"resp_scene_{index:03d}", model="fake-scene-model")


class FakeQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        return _response(self.payload, request_id="resp_qc_001", model="fake-qc-model")


class FakeSequenceQcClient(AccountedGenerateMixin):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("unexpected qc request")
        self.requests.append(request)
        return _response(self.payloads.pop(0), request_id=f"resp_qc_{len(self.requests):03d}", model="fake-qc-model")


class FakeRuntimeFailureClient(AccountedGenerateMixin):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("qc transport timed out before a response was returned")


def _hard_pass() -> dict:
    return {"resolution_code": "hard_pass", "pass_flag": True, "next_action": "pass", "issues": [], "rewrite_brief": []}


def _soft_pass() -> dict:
    return {
        "resolution_code": "soft_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
        "carry_forward_note": False,
        "note_scope": None,
        "carry_note_text": None,
    }


def _seed_scene(session, *, must_include: str = "A red envelope changes hands.") -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="QC grading", outline_text=""))
    session.add(ChapterGoal(chapter_id=CHAPTER_ID, project_id=PROJECT_ID, planned_scene_count=1, chapter_goal="A reunion turns dangerous."))
    session.add(ChapterState(chapter_id=CHAPTER_ID, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            project_id=PROJECT_ID,
            chapter_id=CHAPTER_ID,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text=must_include,
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.add(
        VoiceProfile(
            row_id="voice_profile_VOICE_CHAR_A_v1",
            voice_profile_id="VOICE_CHAR_A",
            version=1,
            character_id="CHAR_A",
            content="tight internal narration",
            active_flag=1,
        )
    )
    session.add(
        RelationProfile(
            row_id="relation_profile_REL_CHAR_A_CHAR_B_v1",
            relation_profile_id="REL_CHAR_A_CHAR_B",
            left_character_id="CHAR_A",
            right_character_id="CHAR_B",
            version=1,
            content="they mistrust each other but still care",
            active_flag=1,
        )
    )
    session.commit()


def _make_orchestrator(
    session,
    *,
    hard_qc_client=None,
    soft_qc_client=None,
    near_final_client=None,
) -> Orchestrator:
    kwargs: dict = {
        "scene_generation_service": SceneGenerationService(session, llm_client=FakeSceneClient()),
        "hard_qc_engine": HardQcEngine(session, llm_client=hard_qc_client or FakeQcClient(_hard_pass())),
        "soft_qc_engine": SoftQcEngine(session, llm_client=soft_qc_client or FakeSequenceQcClient([_soft_pass()])),
    }
    if near_final_client is not None:
        kwargs["near_final_service"] = NearFinalAcceptanceService(session, llm_client=near_final_client)
    return Orchestrator(session, **kwargs)


def _allow_legacy_neutral_required_fact_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a pre-validation neutral draft for downstream Hard-QC coverage."""

    original = scene_generation_module._assess_neutral_draft

    def assess(scene, content):  # noqa: ANN001, ANN202
        result = original(scene, content)
        if set(result.get("reasons") or []) == {"required_facts_missing"}:
            return {**result, "accepted": True, "reasons": []}
        return result

    monkeypatch.setattr(scene_generation_module, "_assess_neutral_draft", assess)


# ---------- G-03 核心：软性意见不再断头 ----------

def test_soft_qc_llm_human_review_without_hard_evidence_still_archives(session) -> None:
    """旧三章形状：软 QC LLM 要求人工审阅（issues 均无确定性佐证）→ 正文照常归档。"""
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(
        session,
        soft_qc_client=FakeSequenceQcClient(
            [
                {
                    "resolution_code": "soft_block_human",
                    "pass_flag": False,
                    "next_action": "human_review_required",
                    "issues": [
                        {"issue_key": "scene_conflict_missing", "message": "冲突推进不足"},
                        {"issue_key": "instruction_residue", "message": "疑似残留指令"},
                    ],
                    "rewrite_brief": ["加强冲突"],
                    "carry_forward_note": False,
                    "note_scope": None,
                    "carry_note_text": None,
                }
            ]
        ),
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    assert result["current_final_scene_row_id"]
    final = session.get(FinalScene, result["current_final_scene_row_id"])
    assert final is not None and final.status == "archived"

    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    graded = {issue["issue_key"]: issue for issue in report.issues_json}
    assert graded["scene_conflict_missing"]["quality_level"] == "Q2"
    assert graded["scene_conflict_missing"]["blocking"] is False
    assert graded["instruction_residue"]["quality_level"] == "Q2"
    # 降级必须留痕：LLM 词表键无确定性佐证
    assert report.resolution_code == "soft_waive"


def test_hard_qc_llm_rewrite_without_hard_evidence_continues_to_archive(session) -> None:
    """旧三章形状：硬 QC LLM 要求整场重写（无确定性佐证）→ 意见降 Q2，管线继续归档。"""
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(
        session,
        hard_qc_client=FakeQcClient(
            {
                "resolution_code": "hard_fail_full",
                "pass_flag": False,
                "next_action": "full_rewrite",
                "issues": [{"issue_key": "timeline_contradiction", "message": "时间线疑似矛盾"}],
                "rewrite_brief": ["重写时间线"],
            }
        ),
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    assert report.next_action == "pass"
    issue = report.issues_json[0]
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["verified_by"] is None


# ---------- 只有真实 Q0/Q1 能阻断 ----------

def test_verified_missing_required_text_still_blocks_and_keeps_draft(
    session,
    monkeypatch,
) -> None:
    """确定性复核成立的 Q1（必备元素确实缺失）仍阻断，且正文保留、早退契约齐备。"""
    _seed_scene(session)  # must_include 未出现在 provider 草稿中 → 确定性缺失
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_client=FakeQcClient(
            {
                "resolution_code": "hard_fail_partial",
                "pass_flag": False,
                "next_action": "partial_rewrite",
                "issues": [
                    {"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}
                ],
                "rewrite_brief": ["补出红包交接"],
            }
        ),
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "hard_qc_partial_rewrite_required"
    assert result["current_final_scene_row_id"] is None
    # Wave 2 项 5：早退结果必须携带 author_state 契约
    assert result["author_state"] == "hard_blocked"
    assert result["latest_valid_draft_row_id"]
    assert result["can_archive"] is False
    assert result["blocking_findings"]

    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = report.issues_json[0]
    assert issue["quality_level"] == "Q1"
    assert issue["blocking"] is True
    assert issue["verified_by"] == "scene_card_required_text"


def test_deterministic_pronoun_drift_still_blocks(session) -> None:
    _seed_scene(session, must_include="")
    scene = session.get(SceneCard, SCENE_ID)
    scene.pov_character_id = "LIN_CEN"
    scene.onstage_chars_json = ["LIN_CEN"]
    voice = session.get(VoiceProfile, "voice_profile_VOICE_CHAR_A_v1")
    voice.voice_profile_id = "VOICE_LIN_CEN"
    voice.character_id = "LIN_CEN"
    voice.content = "角色名：林岑\n代词：她\n角色职责：档案修复师"
    session.commit()

    class DriftSceneClient(FakeSceneClient):
        def generate(self, request: LLMRequest) -> LLMResponse:
            self.requests.append(request)
            payload = {"scene_text": "林岑把盐钟残片放在灯下。他确认刻痕被人改过，声音仍然很稳。", "continuity_notes": []}
            return _response(payload, request_id=f"resp_scene_{len(self.requests):03d}", model="fake-scene-model")

    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=DriftSceneClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=FakeQcClient(_hard_pass())),
        soft_qc_engine=SoftQcEngine(session, llm_client=FakeSequenceQcClient([_soft_pass()])),
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "hard_qc_partial_rewrite_required"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = report.issues_json[0]
    assert issue["issue_key"] == "character_pronoun_drift"
    assert issue["quality_level"] == "Q1"
    assert issue["verified_by"]


# ---------- QC 执行失败不撤销正文（§5.4/§7.7） ----------

def test_hard_qc_execution_failure_degrades_to_continue(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(session, hard_qc_client=FakeRuntimeFailureClient())

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    keys = {issue["issue_key"] for issue in report.issues_json}
    assert "hard_qc_execution_failed" in keys
    graded = next(issue for issue in report.issues_json if issue["issue_key"] == "hard_qc_execution_failed")
    assert graded["quality_level"] == "Q2"
    assert graded["blocking"] is False


def test_soft_qc_execution_failure_degrades_to_waive(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(session, soft_qc_client=FakeRuntimeFailureClient())

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    keys = {issue["issue_key"] for issue in report.issues_json}
    assert "soft_qc_execution_failed" in keys


# ---------- near-final 达修订上限交付最佳稿（自动修订 ≤2） ----------

def _near_final_fail(reason: str = "结构不足") -> dict:
    return {
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": 0.4,
        "scores": {},
        "findings": [
            {
                "dimension": "story_necessity",
                "severity": "revision",
                "issue": reason,
                "recommendation": "补足抉择与代价",
                "evidence_excerpt": "",
                "evidence_location": "scene body",
                "why_it_matters": "结构完整性",
            }
        ],
        "revision_brief": [{"dimension": "story_necessity", "action": "补足抉择与代价", "priority": "high"}],
        "failure_class": "scene_structure_failure",
        "requires_human_review": False,
    }


def test_near_final_two_fails_delivers_best_draft_with_warnings(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(
        session,
        near_final_client=FakeSequenceQcClient([_near_final_fail(), _near_final_fail("重写后仍结构不足")]),
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    # 达自动修订上限（soft patch 0 + near-final rewrite 1 ≤ 2）后交付最佳稿，不再断头
    assert result["scene_status"] == "archived"
    assert result["current_final_scene_row_id"]
    assert result["near_final"]["pass_flag"] is False
    assert result["near_final"]["rewrite_count"] == 1
    # 作者行动建议随结果携带
    assert result["quality_warnings"]
    assert result["recommended_actions"]


def test_near_final_llm_requires_human_review_does_not_block(session) -> None:
    """near-final 的 requires_human_review 只是 LLM 提案——不得产生 human_review_required 断头。"""
    _seed_scene(session, must_include="")
    fail = _near_final_fail()
    fail["requires_human_review"] = True
    fail["near_final_status"] = "human_review_required"
    fail2 = dict(fail)
    orchestrator = _make_orchestrator(session, near_final_client=FakeSequenceQcClient([fail, fail2]))

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"


def test_near_final_execution_failure_degrades_not_blocks(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(session, near_final_client=FakeRuntimeFailureClient())

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"


# ---------- 严格模式：Q2 需作者显式接受 ----------

def _soft_waive_with_warning() -> dict:
    return {
        "resolution_code": "soft_waive",
        "pass_flag": True,
        "next_action": "pass_with_notes",
        "issues": [{"issue_key": "pacing_flat", "message": "节奏偏平"}],
        "rewrite_brief": [],
        "carry_forward_note": True,
        "note_scope": "scene_memory",
        "carry_note_text": "节奏偏平，下一场注意起伏。",
    }


def test_strict_mode_stops_on_q2_warnings_and_adopt_accepts(client, session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(
        session, soft_qc_client=FakeSequenceQcClient([_soft_waive_with_warning()])
    )

    result = orchestrator.run_scene(SCENE_ID, run_policy="strict")
    session.commit()

    # 严格模式：带 Q2 警告不自动归档，停在可归档的 quality_warning
    assert result["scene_status"] == "quality_warning_pending_acceptance"
    assert result["author_state"] == "quality_warning"
    assert result["can_archive"] is True
    assert result["current_final_scene_row_id"] is None
    assert result["latest_valid_draft_row_id"]

    # 作者显式接受：adopt-current 归档并留下接受审计
    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "strict-adopt-1"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scene_status"] == "archived"
    final = session.get(FinalScene, data["final_scene_row_id"])
    assert final is not None and final.status == "archived"
    from novel_system.db.models import SceneMemory

    memory = session.get(SceneMemory, data["scene_memory_row_id"])
    acceptance = [note for note in (memory.carry_notes_json or []) if note.get("kind") == "quality_warning_acceptance"]
    assert acceptance, "严格模式归档必须留下显式接受 Q2 的审计痕迹"


def test_reliable_mode_archives_with_same_q2_warnings(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(
        session, soft_qc_client=FakeSequenceQcClient([_soft_waive_with_warning()])
    )

    result = orchestrator.run_scene(SCENE_ID, run_policy="reliable")
    session.commit()

    assert result["scene_status"] == "archived"
    assert result["quality_warnings"]


def test_strict_mode_without_warnings_archives(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(session)

    result = orchestrator.run_scene(SCENE_ID, run_policy="strict")
    session.commit()

    assert result["scene_status"] == "archived"


# ---------- style gate：只有确定性抄袭命中保留阻断权 ----------

def test_style_gate_pass_adds_no_issue(session) -> None:
    """中性步位的门只给 pass / plagiarism（风格参考 v3 删掉了 fail / partial 的死分支）：pass 不挂任何 issue。"""
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(session)
    with patch.object(HardQcEngine, "_apply_style_validation_gate", return_value="pass"):
        result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    assert not any(str(issue.get("issue_key") or "").startswith("style_validation") for issue in report.issues_json)


def test_style_gate_plagiarism_still_blocks(session) -> None:
    """确定性 n-gram 抄袭命中 = Q0 来源安全——保留阻断权。"""
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(session)
    with patch.object(HardQcEngine, "_apply_style_validation_gate", return_value="plagiarism"):
        result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "human_review_required"
    assert result["author_state"] == "hard_blocked"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = next(issue for issue in report.issues_json if issue["issue_key"] == "style_plagiarism")
    assert issue["quality_level"] == "Q0"
    assert issue["blocking"] is True
    assert issue["verified_by"] == "style_plagiarism_ngram"


# ---------- adopt-current：只有真实 Q0/Q1 拒绝归档 ----------

def test_adopt_rejects_verified_hard_block(client, session, monkeypatch) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_client=FakeQcClient(
            {
                "resolution_code": "hard_fail_partial",
                "pass_flag": False,
                "next_action": "partial_rewrite",
                "issues": [
                    {"issue_key": "missing_required_text", "message": "缺少必备元素：A red envelope changes hands."}
                ],
                "rewrite_brief": ["补出 red envelope 交接"],
            }
        ),
    )
    orchestrator.run_scene(SCENE_ID)
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "hard-block-adopt-1"},
    )
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "HARD_BLOCKED"

    # 红线 4：拒绝归档不得删除已有草稿
    state = session.get(SceneRunState, SCENE_ID)
    assert state.latest_valid_draft_row_id


def test_run_full_route_accepts_run_policy(client, session) -> None:
    """run/full 接受 run_policy（请求级，Wave 3 才落列）；非法值 422。"""
    _seed_scene(session, must_include="")

    bad = client.post(
        f"/api/v1/scenes/{SCENE_ID}/run/full",
        json={"run_policy": "yolo"},
        headers={"X-Idempotency-Key": "run-policy-bad"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "INVALID_RUN_POLICY"

    # 离线客户端全 pass → strict 无警告也归档
    good = client.post(
        f"/api/v1/scenes/{SCENE_ID}/run/full",
        json={"run_policy": "strict"},
        headers={"X-Idempotency-Key": "run-policy-good"},
    )
    assert good.status_code == 200
    assert good.json()["data"]["scene_status"] == "archived"


# ---------- §6.1 落库契约 ----------

def test_qc_report_issues_carry_grading_contract(session) -> None:
    _seed_scene(session, must_include="")
    orchestrator = _make_orchestrator(
        session,
        soft_qc_client=FakeSequenceQcClient(
            [
                {
                    "resolution_code": "soft_waive",
                    "pass_flag": True,
                    "next_action": "pass_with_notes",
                    "issues": [{"issue_key": "cadence_flat", "message": "开场节奏平"}],
                    "rewrite_brief": [],
                    "carry_forward_note": True,
                    "note_scope": "scene_memory",
                    "carry_note_text": "开场节奏平。",
                }
            ]
        ),
    )
    orchestrator.run_scene(SCENE_ID)
    session.commit()

    for report in session.execute(select(QcReport)).scalars().all():
        for issue in report.issues_json or []:
            for field in ("quality_level", "blocking", "source", "verified_by", "recommended_action"):
                assert field in issue, (report.qc_type, issue.get("issue_key"), field)
            if issue["quality_level"] in ("Q0", "Q1"):
                assert issue["verified_by"], issue
