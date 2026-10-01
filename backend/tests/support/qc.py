"""硬质检 / 软质检测试共用：一场待质检的场景、质检回包的拼装、按序回包的质检替身。"""

from __future__ import annotations

import json

import pytest

from novel_system.db.models import ChapterGoal, ChapterState, SceneCard, SceneRunState, StoryProject
from novel_system.services import scene_generation as scene_generation_module
from novel_system.services.llm_client import LLMRequest, LLMResponse
from tests.accounted_llm_fakes import AccountedGenerateMixin


# ---------------------------------------------------------------- 一场待质检的场景（CH100_SC01）、质检回包与按序回包的替身（test_qc_engine 拆出的三个文件共用）


class FakeSoftQcClient(AccountedGenerateMixin):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("unexpected soft_qc request")
        self.requests.append(request)
        payload = self.payloads.pop(0)
        return LLMResponse(
            request_id=f"resp_soft_qc_{len(self.requests):03d}",
            provider="fake-provider",
            model="fake-soft-qc-model",
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": f"resp_soft_qc_{len(self.requests):03d}",
                "model": "fake-soft-qc-model",
                "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            finish_reason="stop",
        )


class FakeQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            request_id="resp_hard_qc_001",
            provider="fake-provider",
            model="fake-hard-qc-model",
            text=json.dumps(self.payload),
            structured_output=self.payload,
            response_format="json_object",
            raw_response={
                "id": "resp_hard_qc_001",
                "model": "fake-hard-qc-model",
                "usage": {"input_tokens": 77, "output_tokens": 21, "total_tokens": 98},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 77, "output_tokens": 21, "total_tokens": 98},
            finish_reason="stop",
        )


def seed_qc_scene(session) -> None:
    session.add(StoryProject(project_id="PROJECT_QC", title="QC", outline_text="QC"))
    session.add(
        ChapterGoal(
            chapter_id="CH100",
            project_id="PROJECT_QC",
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(ChapterState(chapter_id="CH100", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="CH100_SC01",
            project_id="PROJECT_QC",
            chapter_id="CH100",
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text="A red envelope changes hands.",
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id="CH100_SC01", scene_status="ready"))
    session.commit()


def allow_legacy_neutral_required_fact_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a pre-validation neutral draft so Hard-QC remains defense in depth."""

    original = scene_generation_module._assess_neutral_draft

    def assess(scene, content, lengths):  # noqa: ANN001, ANN202
        result = original(scene, content, lengths)
        if set(result.get("reasons") or []) == {"required_facts_missing"}:
            return {**result, "accepted": True, "reasons": []}
        return result

    monkeypatch.setattr(scene_generation_module.text_gates, "_assess_neutral_draft", assess)


def qc_payload(*, resolution_code: str, next_action: str, issues: list[dict] | None = None) -> dict:
    return {
        "resolution_code": resolution_code,
        "pass_flag": resolution_code == "hard_pass",
        "next_action": next_action,
        "issues": issues or [],
        "rewrite_brief": ["Repair the continuity issue before style generation."] if next_action != "pass" else [],
    }


def soft_qc_payload(
    *,
    resolution_code: str,
    next_action: str,
    issues: list[dict] | None = None,
    rewrite_brief: list[str] | None = None,
    carry_forward_note: bool = False,
    note_scope: str | None = None,
    carry_note_text: str | None = None,
    style_score: float | None = None,
    style_dimensions: list[dict] | None = None,
    style_deviations: list[dict] | None = None,
) -> dict:
    payload = {
        "resolution_code": resolution_code,
        "pass_flag": resolution_code in {"soft_pass", "soft_waive"},
        "next_action": next_action,
        "issues": issues or [],
        "rewrite_brief": rewrite_brief or [],
        "carry_forward_note": carry_forward_note,
        "note_scope": note_scope,
        "carry_note_text": carry_note_text,
    }
    if style_score is not None:
        payload["style_score"] = style_score
    if style_dimensions is not None:
        payload["style_dimensions"] = style_dimensions
    if style_deviations is not None:
        payload["style_deviations"] = style_deviations
    return payload
