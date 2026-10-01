"""QC 分级与可靠成稿模式的场景脚手架：一场普通（未绑参考书）的场景，替身起草 / 质检，组装编排器。"""

from __future__ import annotations

import json

from novel_system.db.models import ChapterGoal, ChapterState, SceneCard, SceneRunState, StoryProject
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.near_final import NearFinalAcceptanceService
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.support.scene_generation import PATCHED_SCENE_TEXT, STYLE_SCENE_TEXT


# ---------------------------------------------------------------- 可靠成稿模式：一场普通场景，替身起草 / 质检，组装编排器（test_qc_grading_reliable_mode）


PROJECT_ID = "PROJECT200"
SCENE_ID = "CH200_SC01"
CHAPTER_ID = "CH200"


def response(payload: dict, *, request_id: str, model: str) -> LLMResponse:
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
            payload = {"scene_text": STYLE_SCENE_TEXT, "style_notes": []}
        else:
            payload = {"scene_text": PATCHED_SCENE_TEXT, "style_notes": []}
        return response(payload, request_id=f"resp_scene_{index:03d}", model="fake-scene-model")


class FakeQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        return response(self.payload, request_id="resp_qc_001", model="fake-qc-model")


class FakeSequenceQcClient(AccountedGenerateMixin):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("unexpected qc request")
        self.requests.append(request)
        return response(self.payloads.pop(0), request_id=f"resp_qc_{len(self.requests):03d}", model="fake-qc-model")


def hard_pass() -> dict:
    return {"resolution_code": "hard_pass", "pass_flag": True, "next_action": "pass", "issues": [], "rewrite_brief": []}


def soft_pass() -> dict:
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


def seed_scene(session, *, must_include: str = "A red envelope changes hands.") -> None:
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
    session.commit()


def make_orchestrator(
    session,
    *,
    hard_qc_client=None,
    soft_qc_client=None,
    near_final_client=None,
) -> Orchestrator:
    kwargs: dict = {
        "scene_generation_service": SceneGenerationService(session, llm_client=FakeSceneClient()),
        "hard_qc_engine": HardQcEngine(session, llm_client=hard_qc_client or FakeQcClient(hard_pass())),
        "soft_qc_engine": SoftQcEngine(session, llm_client=soft_qc_client or FakeSequenceQcClient([soft_pass()])),
    }
    if near_final_client is not None:
        kwargs["near_final_service"] = NearFinalAcceptanceService(session, llm_client=near_final_client)
    return Orchestrator(session, **kwargs)


def near_final_fail(reason: str = "结构不足") -> dict:
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
