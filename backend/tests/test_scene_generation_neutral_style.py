"""场景生成 · 风格通道（services/scene_generation/neutral_style.py）：风格稿、去模板一遍、基础不安全时回退中性稿重来、
只差长度时按段落局部补丁、极短时有界的中性救稿、该拆场时不起稿（X04-19，自 test_scene_generation 拆出）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.support.scene_generation import ThreeStepSceneClient as FakeSceneClient, seed_generation_scene as _seed_scene


class FakeDeTemplateClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            structured_output = {
                "scene_text": (
                    "她低头看着钥匙，沉默了片刻。"
                    "他低头看着录音，沉默了片刻。"
                    "她低头看着门缝，沉默了片刻。"
                    "她知道真相必须公开。"
                ),
                "style_notes": ["kept an unsafe template"],
            }
            request_id = "resp_fake_style_template"
            model = "fake-style-model"
        else:
            structured_output = {
                "scene_text": "她把钥匙扣进掌心，转身拔掉录音线。门缝里的光灭了，外面的人开始敲门。",
                "style_notes": ["removed repeated action template"],
            }
            request_id = "resp_fake_de_template"
            model = "fake-patch-model"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=__import__("json").dumps(structured_output),
            structured_output=structured_output,
            response_format="json_object",
            raw_response={"id": request_id, "model": model, "usage": {}, "finish_reason": "stop"},
            usage={"input_tokens": 101, "output_tokens": 25, "total_tokens": 126},
            finish_reason="stop",
        )


class FakeRegressiveDeTemplateClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            scene_text = "她走了。"
            request_id = "resp_fake_de_template_regressed"
        else:
            scene_text = (
                "她低头看着红色信封，沉默了片刻。"
                "他低头看着录音，沉默了片刻。"
                "她低头看着门缝，沉默了片刻。"
                "她知道真相必须公开。"
            )
            request_id = "resp_fake_style_with_required_fact"
        payload = {"scene_text": scene_text}
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeMissingSceneTextPatchClient(FakeDeTemplateClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        if request.node_id != "style_patch":
            return super().generate(request)
        self.requests.append(request)
        payload = {"style_notes": ["provider omitted scene_text"]}
        return LLMResponse(
            request_id="resp_fake_patch_missing_scene_text",
            provider="fake-provider",
            model="fake-patch-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": "resp_fake_patch_missing_scene_text",
                "model": "fake-patch-model",
                "usage": {},
            },
            usage={"input_tokens": 80, "output_tokens": 5, "total_tokens": 85},
            finish_reason="stop",
        )


class FakeUnsafeBaseThenSafePatchClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            scene_text = "她接过红色信封，没有拆。门外脚步停住，她把信封压进抽屉，转身关灯。"
            request_id = "resp_safe_style_patch"
        else:
            scene_text = "她低头看着门缝，沉默了片刻。" * 20
            request_id = "resp_unsafe_style_base"
        payload = {"scene_text": scene_text}
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeLengthOnlyLocalPatchClient(AccountedGenerateMixin):
    neutral = "她接过红色信封，站在门边等了片刻。楼梯上传来脚步，她没有拆信，只把它握在手里。"
    removable = (
        "窗外的雨水沿着窗框一遍又一遍地滑落，墙上的影子也一遍又一遍地晃动，"
        "同一阵脚步声被反复描写了许多次，除此之外没有发生任何新的事情。"
    )
    replacement = "窗外雨声未停。"
    ending = "她仍把信封握在手里。"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            payload = {
                "edits": [
                    {
                        "segment_id": "S003",
                        "new_text": self.replacement,
                    }
                ]
            }
            request_id = "resp_local_length_patch"
        else:
            payload = {"scene_text": self.neutral + self.removable + self.ending}
            request_id = "resp_length_only_style_base"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeFactRepairThenLengthPatchClient(AccountedGenerateMixin):
    neutral = "她接过红色信封，站在门边等了片刻。楼梯上传来脚步，她没有拆信。"
    repaired_but_short = "她接过红色信封，没有拆。门外脚步忽然停住。"
    insertion = "雨水沿着门槛漫开，她后退半步，仍盯着楼梯口，直到那阵脚步再次逼近。"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        patch_count = sum(
            prior.node_id == "style_patch" for prior in self.requests
        )
        if request.node_id != "style_patch":
            payload = {"scene_text": "她在门边等。"}
            request_id = "resp_fact_and_length_unsafe_base"
        elif patch_count == 1:
            payload = {"scene_text": self.repaired_but_short}
            request_id = "resp_fact_repaired_length_short"
        else:
            payload = {
                "edits": [
                    {
                        "segment_id": "S001",
                        "new_text": self.insertion,
                    }
                ]
            }
            request_id = "resp_followup_local_length_patch"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeExtremeUnderlengthThenSalvageClient(AccountedGenerateMixin):
    paragraph_one = (
        "她接过红色信封，没有拆，只把封口对着灯光看了一遍。"
        "楼梯上的脚步停住，门外却没有人敲门。"
    )
    paragraph_two = (
        "她后退半步，将信封压在桌角，听见雨水沿着窗棂往下流。"
        "那阵脚步又响了一次，比先前更近。"
    )
    ending = "她仍没有拆信，伸手熄了灯，站在黑暗里等。"
    neutral = "\n\n".join((paragraph_one, paragraph_two, ending))
    extreme_short = "她接过红色信封，没有拆，倚门听着那阵越来越近的脚步。"
    replacement = (
        "红色信封到了她手里，还是一个小小的纸包；她不拆，"
        "只向灯下一照。楼梯上的脚步停了，门也很客气，并不响。"
    )

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            payload = {
                "edits": [
                    {
                        "segment_id": "S001",
                        "new_text": self.replacement,
                    }
                ]
            }
            request_id = "resp_bounded_style_salvage"
        else:
            payload = {"scene_text": self.extreme_short}
            request_id = "resp_extreme_underlength_style"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


def test_generate_style_draft_runs_one_de_template_pass_for_high_risk_anti_template(session) -> None:
    _seed_scene(session, must_include_text=None)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
    }
    fake_client = FakeDeTemplateClient()

    result = SceneGenerationService(session, llm_client=fake_client).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Approved neutral draft.",
    )

    assert len(fake_client.requests) == 2
    assert fake_client.requests[1].node_id == "style_patch"
    assert fake_client.requests[1].temperature == 0.3
    assert "De-template Rewrite Brief" in fake_client.requests[1].messages[1]["content"]
    assert "Deterministic Style Repair Length Guard" in fake_client.requests[1].messages[1]["content"]
    assert "Edit the labeled style draft directly" in fake_client.requests[1].messages[1]["content"]
    assert "Recompose the supplied source draft" not in fake_client.requests[1].messages[1]["content"]
    assert "quality:scene:CH100_SC01:template_action_reuse" in fake_client.requests[1].messages[1]["content"]

    drafts = session.execute(select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())).scalars().all()
    assert [draft.stage for draft in drafts] == ["style_draft", "de_template"]
    assert drafts[0].content.startswith("她低头看着钥匙")
    assert drafts[1].content == result.content
    assert result.row_id == drafts[1].row_id

    llm_calls_by_step = {
        row.step: row for row in session.execute(select(LlmCall).order_by(LlmCall.created_at.asc())).scalars().all()
    }
    assert set(llm_calls_by_step) == {"style_draft", "de_template"}
    assert llm_calls_by_step["de_template"].node_id == "style_patch"

    attempts = {
        row.step: row for row in session.execute(select(AttemptTracker).order_by(AttemptTracker.created_at.asc())).scalars().all()
    }
    assert attempts["de_template"].details_json["quality_gate"]["triggered"] is True
    assert attempts["de_template"].details_json["quality_gate"]["rewrite_pass"] == 1
    assert session.get(SceneRunState, "CH100_SC01").current_style_draft_row_id == drafts[1].row_id


def test_de_template_rewrite_is_audited_but_rejected_when_required_fact_is_lost(session) -> None:
    _seed_scene(session, must_include_text="红色信封")
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    fake_client = FakeRegressiveDeTemplateClient()

    result = SceneGenerationService(
        session,
        llm_client=fake_client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="红色信封被递到她手里。",
    )

    drafts = session.execute(
        select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())
    ).scalars().all()
    base = next(draft for draft in drafts if draft.stage == "style_draft")
    rejected = next(draft for draft in drafts if draft.stage == "de_template")
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "de_template")
    ).scalar_one()

    assert result.row_id == base.row_id
    assert result.content == base.content
    assert rejected.content == "她走了。"
    assert rejected.status == "rejected"
    assert attempt.details_json["acceptance"]["accepted"] is False
    assert "required_facts_regressed" in attempt.details_json["acceptance"]["reasons"]
    assert session.get(SceneRunState, "CH100_SC01").current_style_draft_row_id == base.row_id


def test_de_template_missing_scene_text_is_audited_and_falls_back_to_base(session) -> None:
    _seed_scene(session, must_include_text=None)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeMissingSceneTextPatchClient()

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Approved neutral draft.",
    )

    base = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "style_draft")
    ).scalar_one()
    failed = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.step == "de_template",
            AttemptTracker.status == "failed",
        )
    ).scalar_one()
    assert result.row_id == base.row_id
    assert result.content == base.content
    assert failed.details_json["error_code"] == "SCENE_GENERATION_RESPONSE_INVALID"
    assert failed.details_json["business_attempt_consumed"] is True
    assert session.get(SceneRunState, "CH100_SC01").current_style_draft_row_id == base.row_id


def test_unsafe_base_is_audited_then_retried_from_approved_neutral_fallback(
    session, monkeypatch
) -> None:
    _seed_scene(session, must_include_text="红色信封")
    scene = session.get(SceneCard, "CH100_SC01")
    scene.target_length_band = "30-100 Chinese characters"
    session.commit()
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    neutral = "她接过红色信封，站在门边等了片刻。楼梯上传来脚步，她没有拆信，只把它握在手里。"

    client = FakeUnsafeBaseThenSafePatchClient()
    service = SceneGenerationService(
        session,
        llm_client=client,
    )
    original_inject = service._inject_style_reference
    injection_calls: list[None] = []

    def recording_inject(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        injection_calls.append(None)
        return original_inject(*args, **kwargs)

    monkeypatch.setattr(service, "_inject_style_reference", recording_inject)
    result = service.generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=neutral,
    )

    drafts = session.execute(
        select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())
    ).scalars().all()
    rejected = next(draft for draft in drafts if draft.stage == "style_rejected")
    fallback = next(draft for draft in drafts if draft.stage == "style_draft")
    patched = next(draft for draft in drafts if draft.stage == "de_template")
    base_attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "style_draft")
    ).scalar_one()

    assert rejected.status == "rejected"
    assert len(rejected.content) > 100
    assert fallback.content == neutral
    assert base_attempt.details_json["base_safety"]["accepted"] is False
    assert base_attempt.details_json["rejected_candidate_row_id"] == rejected.row_id
    assert "required_facts_regressed" in base_attempt.details_json["base_safety"]["reasons"]
    assert "target_length_not_met" in base_attempt.details_json["base_safety"]["reasons"]
    assert result.row_id == patched.row_id
    assert result.content == patched.content
    assert "红色信封" in result.content
    repair_prompt = client.requests[1].messages[1]["content"]
    assert "Rejected Style Draft Requiring One Safety Repair" in repair_prompt
    assert "Safety Repair Brief" in repair_prompt
    assert "De-template Rewrite Brief" not in repair_prompt
    assert "她低头看着门缝" in repair_prompt
    assert "Every final required constraint must be explicit." in repair_prompt
    assert "include at least one literal alternative from each group: 红色信封" in repair_prompt
    assert "working window at 40-90" in repair_prompt
    assert "Deterministic Style Repair Length Guard" in repair_prompt
    assert "Absolute final range: 30-100" in repair_prompt
    assert "Expand by at least" not in repair_prompt
    assert "Edit the labeled rejected style draft directly" in repair_prompt
    assert "Recompose the supplied source draft" not in repair_prompt
    assert client.requests[1].temperature == 0.1
    style_prompt = client.requests[0].messages[1]["content"]
    assert "Deterministic Style Rewrite Length Guard" in style_prompt
    # 安全修复编辑的是已经风格化的拒绝稿，不能按拒绝稿的新长度再次重算并叠加
    # 一份冲突的风格量化约束；完整风格注入只发生在首轮生成。
    assert len(injection_calls) == 1
    repair_attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "de_template")
    ).scalar_one()
    assert (
        repair_attempt.details_json["repair_source_style_draft_row_id"]
        == rejected.row_id
    )
    assert (
        repair_attempt.details_json["source_style_draft_row_id"]
        == fallback.row_id
    )


def test_length_only_unsafe_style_uses_exact_local_patch_instead_of_full_rewrite(
    session,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-80 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeLengthOnlyLocalPatchClient()

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 2
    assert result.content == client.neutral + client.replacement + client.ending
    assert 30 <= sum(not char.isspace() for char in result.content) <= 80
    patch_prompt = client.requests[1].messages[1]["content"]
    assert "Deterministic Local Length Patch Contract" in patch_prompt
    assert "Return edits only, never scene_text" in patch_prompt
    assert "Recompose the supplied source draft" not in patch_prompt
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "de_template")
    ).scalar_one()
    assert attempt.details_json["acceptance"]["accepted"] is True
    assert attempt.details_json["length_patch"]["valid"] is True
    assert attempt.details_json["length_patch"]["mode"] == "compress"
    schema = client.requests[1].response_schema["schema"]
    allowed_ids = schema["properties"]["edits"]["items"]["properties"][
        "segment_id"
    ]["enum"]
    assert "S003" in allowed_ids
    assert "S004" not in allowed_ids


def test_fact_repair_can_finish_with_one_local_length_followup(session) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="45-100 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeFactRepairThenLengthPatchClient()

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 3
    assert "红色信封" in result.content
    assert client.insertion in result.content
    assert 45 <= sum(not char.isspace() for char in result.content) <= 100
    attempts = session.execute(
        select(AttemptTracker)
        .where(AttemptTracker.step == "de_template")
        .order_by(AttemptTracker.attempt_id.asc())
    ).scalars().all()
    assert len(attempts) == 2
    assert attempts[0].details_json["acceptance"]["reasons"] == [
        "target_length_not_met"
    ]
    assert attempts[1].details_json["acceptance"]["accepted"] is True
    assert attempts[1].details_json["length_patch"]["valid"] is True
    followup_schema = client.requests[2].response_schema["schema"]["properties"][
        "edits"
    ]
    assert followup_schema["minItems"] == followup_schema["maxItems"] == 1
    new_text_schema = followup_schema["items"]["properties"]["new_text"]
    assert new_text_schema["minLength"] > 1


def test_extreme_underlength_style_uses_bounded_neutral_salvage(
    session,
    monkeypatch,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="100-260 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeExtremeUnderlengthThenSalvageClient()
    # 未绑定的场景没有读数（挽救补丁不可比 → 不采用，见 test_style_salvage_is_never_adopted_without_comparable_readings）；
    # 这里把「改写不退步」定死为可比且没退步，只看补丁本身的接线
    monkeypatch.setattr(
        "novel_system.services.scene_generation.fidelity_probe.rewrite_drift",
        lambda *_args, **_kwargs: {
            "available": True,
            "comparable": True,
            "regressed": False,
        },
    )

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 2
    assert result.content != client.neutral
    assert client.replacement in result.content
    assert client.paragraph_two in result.content
    assert result.content.endswith(client.ending)
    assert 100 <= sum(not char.isspace() for char in result.content) <= 260
    salvage_attempt = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.step == "style_salvage_patch"
        )
    ).scalar_one()
    assert salvage_attempt.details_json["acceptance"]["accepted"] is True
    assert salvage_attempt.details_json["style_salvage"]["valid"] is True
    assert salvage_attempt.details_json["style_salvage"]["segment_id"] == "S001"
    schema = client.requests[1].response_schema["schema"]
    allowed_ids = schema["properties"]["edits"]["items"]["properties"][
        "segment_id"
    ]["enum"]
    assert "S003" not in allowed_ids


def test_style_salvage_is_never_adopted_without_comparable_readings(session) -> None:
    """风格参考 v3 S2（d）：改写不退步只看读数。未绑定的场景没有读数 → 两稿不可比 → 挽救补丁照旧不采用
    （与指标包络时代同一结果：``style_salvage_conformance_unavailable``），终稿回到已批准的中性稿；
    旧的段落形态整理（``paragraph_shape_normalization``）不再写。"""
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="100-260 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeExtremeUnderlengthThenSalvageClient()

    result = SceneGenerationService(session, llm_client=client).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 2
    assert result.content == client.neutral
    salvage_attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "style_salvage_patch")
    ).scalar_one()
    acceptance = salvage_attempt.details_json["acceptance"]
    assert acceptance["accepted"] is False
    assert "style_salvage_conformance_unavailable" in acceptance["reasons"]
    assert "style_salvage_conformance_regressed" not in acceptance["reasons"]
    drift = acceptance["style_conformance"]
    assert drift["version"] == "style_rewrite_drift_v1"
    assert drift["available"] is False and drift["comparable"] is False and drift["regressed"] is False
    assert drift["unavailable_reason"] in {"bundle_has_no_style_profile", "style_policy_unbound"}
    assert acceptance["style_salvage_non_regression_enforced"] is True
    assert "style_salvage_regression_tolerance" not in acceptance
    assert "paragraph_shape_normalization" not in salvage_attempt.details_json
    style_attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "style_draft")
    ).scalar_one()
    assert "paragraph_shape_normalization" not in style_attempt.details_json
    assert "style_step" not in style_attempt.details_json  # 未绑定：没有读数决定


def test_generate_style_draft_blocks_provider_when_scene_must_split(session) -> None:
    _seed_scene(session)
    fake_client = FakeSceneClient()
    service = SceneGenerationService(session, llm_client=fake_client)

    class StubPromptBuilder:
        def build(self, *_args, **_kwargs):
            return {
                "template_name": "style_draft",
                "template_version": "test",
                "system_prompt": "system",
                "user_prompt": "user\n\nReturn JSON that matches the structured schema exactly.",
                "structured_schema": {},
                "prompt_hash": "prompt_hash_style_split",
                "token_budget": {
                    "target_input_tokens": 60,
                    "estimated_input_tokens": 10,
                    "remaining_input_tokens": 50,
                    "included_sections": [],
                    "compressed_sections": [],
                    "omitted_sections": [],
                    "section_status": {},
                    "continuity_policy": [],
                    "split_scene_recommended": False,
                    "stop_reason": None,
                    "continuity_warning": None,
                },
                "continuity_warning": None,
            }

    service._prompt_builder_instance = StubPromptBuilder()
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
    }

    with pytest.raises(DomainError) as exc:
        service.generate_style_draft(
            "CH100_SC01",
            bundle,
            neutral_draft_row_id="draft_neutral_CH100_SC01",
            neutral_content=" ".join(["oversized neutral draft"] * 80),
        )

    assert exc.value.code == "CONTINUITY_BUDGET_EXCEEDED"
    assert fake_client.requests == []

    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "style_draft")).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "style_draft")).scalars().one()

    assert llm_call.error_code == "CONTINUITY_BUDGET_EXCEEDED"
    assert llm_call.request_payload_summary["continuity_warning"]["requires_scene_split"] is True
    assert attempt.status == "failed"
