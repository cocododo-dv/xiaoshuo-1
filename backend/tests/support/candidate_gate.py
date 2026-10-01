"""作者终选门的场景脚手架：一场绑定作者手笔直起（style_first）的关键场景，替身起草 / 质检，组装编排器。

用到它的文件各自用 autouse 夹具决定多稿份数与读数（见 test_candidate_selection_gate）。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from novel_system.db.models import ChapterGoal, ChapterState, HumanReviewEvent, SceneCard, SceneRunState, StoryProject
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.near_final import NearFinalAcceptanceService, NearFinalPlanningService
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import ScenePipelineOnlineFake
from tests.support.style_first_fixtures import bind_style_first


# ---------------------------------------------------------------- 关键场景终选门：一场绑定作者手笔直起的关键场景（test_candidate_selection_gate）


PROJECT_ID = "PROJECT300"
SCENE_ID = "CH300_SC01"
CHAPTER_ID = "CH300"

# 产品里一场的运行总是经幂等路由（或场景作业）发起；选后续跑只认这两种来源的产物归属
#（scene_run_checkpoint._checkpoint_execution_owner_matches），所以首跑也用一个幂等执行 id。
ORIGIN_EXECUTION_ID = "idempotency:w3-origin-run"


def response(payload: dict, *, request_id: str) -> LLMResponse:
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model="fake-model",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": "fake-model",
            "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            "finish_reason": "stop",
        },
        usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
        finish_reason="stop",
    )


class FakeSceneClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests)
        payload = {
            "scene_text": f"Provider-generated draft #{index} for terminal selection.",
            "continuity_notes": [],
        }
        return response(payload, request_id=f"resp_scene_{index:03d}")


class FakePassQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        return response(self.payload, request_id="resp_qc_001")


def hard_pass() -> dict:
    return {
        "resolution_code": "hard_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
    }


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


def seed_scene(session, *, constraint_intensity: float | None = 0.9) -> None:
    session.add(
        StoryProject(project_id=PROJECT_ID, title="Selection gate", outline_text="")
    )
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
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
            must_include_text="",
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
            constraint_intensity=constraint_intensity,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()
    bind_style_first(session, "gate300", project_id=PROJECT_ID)


def make_orchestrator(session, *, scene_client: FakeSceneClient | None = None) -> Orchestrator:
    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session, llm_client=scene_client or FakeSceneClient()
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=FakePassQcClient(hard_pass())),
        soft_qc_engine=SoftQcEngine(session, llm_client=FakePassQcClient(soft_pass())),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(
        session, llm_client=support
    )
    return orchestrator


def selection_gate(session) -> HumanReviewEvent:
    events = (
        session.execute(
            select(HumanReviewEvent).order_by(HumanReviewEvent.created_at.desc())
        )
        .scalars()
        .all()
    )
    for event in events:
        if (event.details_json or {}).get("gate_type") == "style_candidate_selection":
            return event
    raise AssertionError("no style_candidate_selection gate event found")
