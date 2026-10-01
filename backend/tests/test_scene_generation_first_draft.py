"""场景生成 · 首稿（services/scene_generation/first_draft.py）：中性起草、缺必写 / 长度带时带原因修一次、修完仍不合格
就 fail-closed、作者指示冻进包里、起草失败记一次尝试（X04-19，自 test_scene_generation 拆出）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.support.scene_generation import ThreeStepSceneClient as FakeSceneClient, seed_generation_scene as _seed_scene


class FakeNeutralLengthRepairClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        scene_text = (
            "红色信封。" * 60
            if len(self.requests) == 1
            else "她接过红色信封，退到门边。脚步停在楼梯口，她没有拆信，只把信封压进掌心。"
        )
        payload = {"scene_text": scene_text}
        request_id = f"resp_neutral_length_{len(self.requests)}"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-neutral-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": "fake-neutral-model",
                "usage": {},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeNeutralRequiredFactRepairClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        scene_text = (
            "季青看见了那张还款记录，却没有追问。"
            if len(self.requests) == 1
            else "季青看见了那张还款记录，终于明白旧债是周伯代还的，却没有追问。"
        )
        payload = {"scene_text": scene_text}
        request_id = f"resp_neutral_fact_{len(self.requests)}"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-neutral-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": "fake-neutral-model",
                "usage": {},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeNeutralInvalidRepairClient(FakeNeutralLengthRepairClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        response = super().generate(request)
        if len(self.requests) == 2:
            scene_text = "红色信封。" * 2
            payload = {"scene_text": scene_text}
            response = replace(
                response,
                text=__import__("json").dumps(payload, ensure_ascii=False),
                structured_output=payload,
            )
        return response


class FakeFailingClient(AccountedGenerateMixin):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise ValueError("malformed provider payload")


def test_scene_generation_rejects_required_scene_text_when_provider_omits_it(session) -> None:
    _seed_scene(session)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Force both characters to reveal what they know."},
        },
    }
    service = SceneGenerationService(session, llm_client=FakeSceneClient())

    with pytest.raises(DomainError) as exc:
        service.generate_neutral_draft("CH100_SC01", bundle)

    assert exc.value.code == "NEUTRAL_DRAFT_REPAIR_INVALID"
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().all() == []


def test_neutral_draft_retries_once_when_numeric_length_band_is_missed(
    session,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "红色信封必须交到她手中。"},
        },
    }
    client = FakeNeutralLengthRepairClient()

    result = SceneGenerationService(
        session, llm_client=client
    ).generate_neutral_draft("CH100_SC01", bundle)

    drafts = session.execute(
        select(SceneDraft).order_by(SceneDraft.created_at, SceneDraft.row_id)
    ).scalars().all()
    rejected = next(draft for draft in drafts if draft.stage == "neutral_rejected")
    active = next(draft for draft in drafts if draft.stage == "neutral_draft")
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")
    ).scalar_one()
    calls = session.execute(
        select(LlmCall).order_by(LlmCall.created_at)
    ).scalars().all()

    assert len(client.requests) == 2
    assert [call.step for call in calls] == [
        "neutral_draft",
        "neutral_draft_repair",
    ]
    assert "Absolute final range: 30-100" in client.requests[0].messages[1]["content"]
    assert "previous attempt" in client.requests[1].messages[1]["content"]
    assert "Rejected Neutral Draft Requiring One Deterministic Repair" in client.requests[1].messages[1]["content"]
    assert "Deterministic Neutral Repair Brief" in client.requests[1].messages[1]["content"]
    assert "红色信封。" * 3 in client.requests[1].messages[1]["content"]
    assert "Remove at least 210 visible characters" in client.requests[1].messages[1]["content"]
    assert client.requests[1].temperature == 0.1
    assert rejected.status == "rejected"
    assert rejected.content == "红色信封。" * 60
    assert active.content == result.content
    assert result.content.startswith("她接过红色信封")
    assert attempt.details_json["repair"]["accepted"] is True
    assert attempt.details_json["validation"]["accepted"] is True
    assert session.get(SceneRunState, "CH100_SC01").total_attempt_count == 1


def test_neutral_draft_repairs_missing_alternative_even_without_length_band(session) -> None:
    _seed_scene(
        session,
        must_include_text="季青；代还|还清",
        target_length_band="",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "季青发现旧债由周伯代还。"},
        },
    }
    client = FakeNeutralRequiredFactRepairClient()

    result = SceneGenerationService(
        session, llm_client=client
    ).generate_neutral_draft("CH100_SC01", bundle)

    repair_prompt = client.requests[1].messages[1]["content"]
    assert len(client.requests) == 2
    assert "代还|还清" in repair_prompt
    assert "vertical bar means alternatives" in repair_prompt
    assert "代还" in result.content
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")
    ).scalar_one()
    assert attempt.details_json["repair"]["accepted"] is True
    assert attempt.details_json["validation"]["accepted"] is True


def test_neutral_draft_fails_closed_when_the_only_repair_is_still_invalid(
    session,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "红色信封必须交到她手中。"},
        },
    }

    with pytest.raises(DomainError) as exc:
        SceneGenerationService(
            session,
            llm_client=FakeNeutralInvalidRepairClient(),
        ).generate_neutral_draft("CH100_SC01", bundle)

    assert exc.value.code == "NEUTRAL_DRAFT_REPAIR_INVALID"
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().all() == []
    rejected = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_rejected")
    ).scalar_one()
    assert rejected.status == "rejected"
    attempt = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.step == "neutral_draft",
            AttemptTracker.status == "failed",
        )
    ).scalar_one()
    assert attempt.details_json["error_code"] == "NEUTRAL_DRAFT_REPAIR_INVALID"
    assert attempt.details_json["validation"]["accepted"] is False
    assert session.get(SceneRunState, "CH100_SC01").current_neutral_draft_row_id is None


def test_author_instruction_is_frozen_into_bundle_and_reaches_neutral_prompt(session) -> None:
    _seed_scene(session, must_include_text=None)
    note = "把选择提前到第一段，结尾不要解释。"
    bundle = BundleBuilder(session).build("CH100_SC01", author_note=note)

    snapshot = bundle["snapshot"]
    assert snapshot["inline_digests"]["author_instruction"] == note
    assert snapshot["source_version_refs"]["author_instruction_hash"]
    assert any(
        item["slot"] == "author_instruction"
        for item in snapshot["ordered_injections"]
    )

    fake_client = FakeSceneClient()
    SceneGenerationService(session, llm_client=fake_client).generate_neutral_draft(
        "CH100_SC01",
        bundle,
        author_note=note,
    )
    prompt_text = "\n".join(message["content"] for message in fake_client.requests[0].messages)
    assert note in prompt_text


def test_generate_neutral_draft_records_failed_attempt_and_bumps_counter(session) -> None:
    _seed_scene(session)
    bundle = {"bundle_id": "bundle_CH100_SC01", "bundle_snapshot_hash": "bundle_hash_demo", "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Force both characters to reveal what they know."}}}
    service = SceneGenerationService(session, llm_client=FakeFailingClient())

    state = session.get(SceneRunState, "CH100_SC01")
    state.current_bundle_id = bundle["bundle_id"]
    state.current_bundle_hash = bundle["bundle_snapshot_hash"]
    session.commit()

    try:
        service.generate_neutral_draft("CH100_SC01", bundle)
    except ValueError as exc:
        assert str(exc) == "malformed provider payload"
    else:
        raise AssertionError("expected generation failure")

    session.commit()

    llm_call = session.execute(select(LlmCall)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")

    assert llm_call.error_code == "ValueError"
    assert attempt.status == "failed"
    assert attempt.source_bundle_id == bundle["bundle_id"]
    assert attempt.details_json["error_code"] == "ValueError"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert state.total_attempt_count == 1
    assert state.current_bundle_id == bundle["bundle_id"]
    assert state.current_bundle_hash == bundle["bundle_snapshot_hash"]
    assert state.current_neutral_draft_row_id is None
