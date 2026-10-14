"""2026-10-14 评审统一：一份评审模板、校准、代码定放行、成对比较。

背景（实库 10-13）：同一份稿子起草流程里的软 QC 评审打 8.6（16 维一个分都没给），「对照检查」打 2.7——两份提示词
各说各的，流程内的分数还与「每维 ≥7 放行」绑在一起。这里钉住：

- 解析：16 维、按模板声明的刻度、越界丢掉、``applicable=false`` 不作数；
- 校准：作者原文与普通改写拉得开才可用，按每一维的两个锚点折算；评审模型 / 模板 / 画像版本变了就过期；
- 代码定放行：作数的维低于阈值才补（重点维在前、至多 4 维），评审不可用时不补；
- 成对比较：两个次序一致才算数；补丁与定向修改的去留；
- 校准作业端到端、学完自动排校准、路由；软 QC 有绑定时走统一评审、没带上参考时退回润色口径。

全部合成数据；无真实模型、无真实作者原文。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import AttemptTracker, QcReport, StyleReferenceJob, StyleReferenceProfile
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.qc_validator import validate_qc_report
from novel_system.services.style_reference import judge as J
from novel_system.services.style_reference import judge_calibration as JC
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference import style_step as S
from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from novel_system.services.style_reference.jobs import JOB_KIND_CALIBRATE, StyleJobService, run_job_inline
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.style_reference_inject_helpers import seed_reference

THRESHOLDS = J.JudgeThresholds()
PLAIN_MARK = "（平铺直叙）"


def _structured(scores: dict[str, float] | None = None, *, default: float = 8.0, **extra) -> dict:
    dims = {}
    for dim in ALL_DIMENSIONS:
        score = (scores or {}).get(dim, default)
        dims[dim] = {"score": score, "applicable": True, "note": f"{dim} 说明", "fix": f"{dim} 改法"}
    return {"dimensions": dims, "summary": "合成的评语", **extra}


def _scores(scores: dict[str, float] | None = None, *, default: float = 8.0) -> J.JudgeScores:
    parsed = J.parse_judge_output(_structured(scores, default=default))
    assert parsed is not None
    return parsed


def _calibration(*, positive: float = 8.0, negative: float = 4.0, identity: dict | None = None) -> dict:
    return J.compute_calibration(
        [_scores(default=positive)] * 3,
        [_scores(default=negative)] * 3,
        identity=identity or {"provider": "p", "model": "m", "template_version": "v", "profile_version": "1"},
        thresholds=THRESHOLDS,
    )


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_judge_output_is_parsed_by_the_declared_scale_and_drops_out_of_range_or_unknown_dimensions() -> None:
    schema = load_prompt_templates()[J.JUDGE_TEMPLATE].structured_schema
    structured = {
        "dimensions": {
            "scene.dialogue": {"score": 4, "applicable": True, "note": "太正式", "fix": "第二段改成拆台的对白"},
            "theme.values": {"score": 85, "applicable": True, "note": "答错了刻度", "fix": ""},
            "scene.environment": {"score": 0, "applicable": False, "note": "没有环境描写", "fix": "不该出现"},
            "bogus.dimension": {"score": 9, "applicable": True, "note": "", "fix": ""},
        },
        "summary": "差在对白",
    }
    parsed = J.parse_judge_output(structured, schema=schema)
    assert parsed is not None
    assert set(parsed.dimensions) == {"scene.dialogue", "scene.environment"}, "越界与未知维丢掉"
    assert parsed.dimensions["scene.dialogue"].score == 4.0
    assert parsed.dimensions["scene.environment"].applicable is False
    assert parsed.dimensions["scene.environment"].fix == "", "没有证据的维不留改法"
    assert parsed.applicable_scores() == {"scene.dialogue": 4.0}
    assert J.parse_judge_output({"dimensions": {"scene.dialogue": {"score": 0, "applicable": False}}}) is None
    assert J.parse_judge_output({"overall": 8}) is None, "旧形状（没有 dimensions）不算评审"


def test_the_unified_template_requires_all_sixteen_dimensions_and_never_asks_for_an_outcome() -> None:
    template = load_prompt_templates()[J.JUDGE_TEMPLATE]
    schema = template.structured_schema
    assert schema["required"] == ["dimensions", "summary"]
    assert set(schema["properties"]["dimensions"]["required"]) == set(ALL_DIMENSIONS)
    member = schema["properties"]["dimensions"]["properties"]["scene.dialogue"]
    assert member["required"] == ["score", "applicable", "note", "fix"]
    assert "overall" not in schema["properties"] and "resolution_code" not in schema["properties"]
    assert "style_ref_check_judge" not in load_prompt_templates()
    soft = load_prompt_templates()["soft_qc"].structured_schema["properties"]
    assert "dimension_scores" not in soft and "style_score" not in soft, "软 QC 模板只剩润色口径"


# ---------------------------------------------------------------------------
# 校准
# ---------------------------------------------------------------------------


def test_calibration_is_usable_only_when_originals_and_plain_rewrites_separate() -> None:
    usable = _calibration(positive=8.0, negative=4.0)
    assert usable["status"] == J.CALIBRATION_STATUS_USABLE
    assert usable["separation"] == 4.0 and set(usable["discriminating"]) == set(ALL_DIMENSIONS)
    flat = _calibration(positive=8.4, negative=7.8)
    assert flat["status"] == J.CALIBRATION_STATUS_UNUSABLE, "原文 8.4、普通改写 7.8：评审分不清"
    mixed = J.compute_calibration(
        [_scores({"language.punctuation": 7.0}, default=8.0)] * 2,
        [_scores({"language.punctuation": 6.8}, default=3.0)] * 2,
        identity={},
        thresholds=THRESHOLDS,
    )
    assert mixed["status"] == J.CALIBRATION_STATUS_USABLE
    assert "language.punctuation" not in mixed["discriminating"], "差不到 1 分的维不作数"


def test_calibration_state_follows_the_judge_identity() -> None:
    identity = {"provider": "p", "model": "m", "template_version": "v", "profile_version": "1"}
    calibration = _calibration(identity=identity)
    assert J.calibration_state(None, identity) == J.STATE_MISSING
    assert J.calibration_state(calibration, identity) == J.STATE_CALIBRATED
    for key, value in (("model", "another"), ("template_version", "v2"), ("profile_version", "2")):
        assert J.calibration_state(calibration, {**identity, key: value}) == J.STATE_STALE, key
    assert J.calibration_state(_calibration(positive=8, negative=7.5, identity=identity), identity) == J.STATE_UNUSABLE
    assert J.calibration_state(calibration, None) == J.STATE_CALIBRATED, "说不出此刻的评审路由时不判过期"
    assert J.stored_calibration({J.PROFILE_CALIBRATION_KEY: {"version": "old"}}) is None


def test_scores_are_folded_onto_the_two_anchors_per_dimension() -> None:
    calibration = J.compute_calibration(
        [_scores({"language.punctuation": 7.0}, default=8.0)] * 2,
        [_scores({"language.punctuation": 6.8}, default=4.0)] * 2,
        identity={},
        thresholds=THRESHOLDS,
    )
    raw = _scores({"scene.dialogue": 4.0, "theme.values": 6.0, "scene.environment": 9.0, "language.punctuation": 7.0})
    verdict = J.apply_calibration(
        raw,
        calibration=calibration,
        state=J.STATE_CALIBRATED,
        dimension_states={"theme.motifs": "exclude"},
        thresholds=THRESHOLDS,
    )
    dims = verdict.dimensions
    assert dims["scene.dialogue"]["score"] == 0.0, "普通改写的水平 = 0"
    assert dims["theme.values"]["score"] == 5.0, "两点正中 = 5"
    assert dims["scene.environment"]["score"] == 10.0, "高过作者原文的封顶 10"
    assert dims["language.punctuation"]["score"] is None and dims["language.punctuation"]["raw"] == 7.0, "分不清的维不作数"
    assert "theme.motifs" not in dims, "不学的维整维丢掉"
    assert verdict.pass_score == THRESHOLDS.pass_score
    assert verdict.raw_overall is not None and verdict.overall is not None
    uncalibrated = J.apply_calibration(
        raw, calibration=None, state=J.STATE_MISSING, dimension_states=None, thresholds=THRESHOLDS
    )
    assert uncalibrated.dimensions["scene.dialogue"]["score"] == 4.0 and uncalibrated.pass_score == THRESHOLDS.raw_pass_score
    unusable = J.apply_calibration(
        raw, calibration=calibration, state=J.STATE_UNUSABLE, dimension_states=None, thresholds=THRESHOLDS
    )
    assert unusable.counted() == {} and unusable.overall is None


# ---------------------------------------------------------------------------
# 代码定放行
# ---------------------------------------------------------------------------


def test_the_code_decides_pass_patch_or_waive_and_the_payload_is_a_valid_soft_qc_report() -> None:
    labels = {dim: dim for dim in ALL_DIMENSIONS}
    passing = J.apply_calibration(
        _scores(default=8.0), calibration=None, state=J.STATE_MISSING, dimension_states=None, thresholds=THRESHOLDS
    )
    payload = J.soft_qc_payload(passing, dimension_states=None, dimension_labels=labels)
    assert payload["resolution_code"] == "soft_pass" and payload["issues"] == []
    validate_qc_report("soft_qc", payload)

    low = {"scene.dialogue": 3.0, "theme.values": 5.0, "language.rhetoric": 6.0, "narrative.pacing": 6.5, "scene.environment": 6.9}
    verdict = J.apply_calibration(
        _scores(low), calibration=None, state=J.STATE_MISSING, dimension_states=None, thresholds=THRESHOLDS
    )
    states = {"narrative.pacing": "emphasize"}
    failing = J.failing_dimensions(verdict, states)
    assert failing == ["narrative.pacing", "scene.dialogue", "theme.values", "language.rhetoric"], "重点维在前、至多 4 维"
    payload = J.soft_qc_payload(verdict, dimension_states=states, dimension_labels=labels)
    assert payload["resolution_code"] == "soft_patch" and payload["next_action"] == "patch"
    assert [issue["issue_key"] for issue in payload["issues"]][0] == "style_reference.narrative.pacing"
    assert payload["rewrite_brief"][1].startswith("scene.dialogue") and "scene.dialogue 改法" in payload["rewrite_brief"][1]
    assert 0 <= payload["style_score"] <= 1 and all(0 <= v <= 1 for v in payload["dimension_scores"].values())
    validate_qc_report("soft_qc", payload)

    unusable = J.apply_calibration(
        _scores(low), calibration=_calibration(positive=8, negative=7.5), state=J.STATE_UNUSABLE,
        dimension_states=None, thresholds=THRESHOLDS,
    )
    payload = J.soft_qc_payload(unusable, dimension_states=None, dimension_labels=labels)
    assert payload["resolution_code"] == "soft_waive" and payload["rewrite_brief"] == []
    assert "分不清" in payload["carry_note_text"]
    validate_qc_report("soft_qc", payload)


# ---------------------------------------------------------------------------
# 成对比较与去留
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_first", "candidate_first", "verdict"),
    [
        ("A", "B", J.COMPARE_BASE),
        ("B", "A", J.COMPARE_CANDIDATE),
        ("A", "A", J.COMPARE_TIE),
        ("same", "B", J.COMPARE_TIE),
        ("B", None, J.COMPARE_UNAVAILABLE),
    ],
)
def test_pairwise_comparison_counts_only_when_both_orders_agree(base_first, candidate_first, verdict) -> None:
    assert J.compare_verdict(base_first, candidate_first) == verdict


def test_patch_and_revision_keep_follow_the_comparison_then_the_reading() -> None:
    assert S.patch_keep_decision(comparison=J.COMPARE_BASE, before_distance=1.0, after_distance=0.5) == (
        S.PATCH_DECISION_REVERTED,
        S.PATCH_REASON_PAIRWISE_ORIGINAL,
    )
    assert S.patch_keep_decision(comparison=J.COMPARE_CANDIDATE, before_distance=0.5, after_distance=1.5) == (
        S.PATCH_DECISION_KEPT,
        S.PATCH_REASON_PAIRWISE_PATCH,
    )
    assert S.patch_keep_decision(comparison=J.COMPARE_TIE, before_distance=0.5, after_distance=0.6)[1] == S.PATCH_REASON_DISTANCE_WORSE
    assert S.patch_keep_decision(comparison=None, before_distance=0.5, after_distance=0.52)[0] == S.PATCH_DECISION_KEPT
    assert S.patch_keep_decision(comparison=None, before_distance=None, after_distance=None)[1] == S.PATCH_REASON_NO_EVIDENCE
    assert not hasattr(S.FidelityThresholds(), "judge_tolerance")

    first, closer = SimpleNamespace(distance=1.0), SimpleNamespace(distance=0.9)
    common = {"copy_blocked": False, "base_safe": True}
    assert S.revision_keep_decision(first, closer, **common) == (True, S.REASON_CLOSER)
    assert S.revision_keep_decision(first, closer, comparison=J.COMPARE_BASE, **common) == (False, S.REASON_PAIRWISE_FIRST_DRAFT)
    assert S.revision_keep_decision(first, closer, comparison=J.COMPARE_TIE, **common) == (True, S.REASON_CLOSER)
    assert S.revision_needs_comparison(first, closer, **common)
    assert not S.revision_needs_comparison(first, SimpleNamespace(distance=0.99), **common), "读数已经不采用就不比"


# ---------------------------------------------------------------------------
# 校准作业
# ---------------------------------------------------------------------------


class _FakeCalibrationLLM(AccountedGenerateMixin):
    """改写调用：给原文加记号返回；评审调用：带记号（普通改写）打 ``plain`` 分，原文打 ``original`` 分。"""

    def __init__(self, *, original: float = 8.5, plain: float = 3.5) -> None:
        self.original = original
        self.plain = plain
        self.requests: list = []

    def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        user = request.messages[-1]["content"]
        if "## Passage" in user:
            body = user.split("## Passage", 1)[1]
            structured = {"text": PLAIN_MARK + body.strip()}
        else:
            structured = _structured(default=self.plain if PLAIN_MARK in user else self.original)
        return SimpleNamespace(
            structured_output=structured,
            text=json.dumps(structured, ensure_ascii=False),
            usage={},
            finish_reason="stop",
            provider="fake",
            model="fake",
            response_format="json_object",
            request_id=None,
            raw_response={},
        )


@pytest.fixture
def local_routes(monkeypatch):
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda node_id, **_k: True)
    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: True)


def _run_calibration(session, monkeypatch, key: str, fake: _FakeCalibrationLLM) -> tuple[str, StyleReferenceJob]:
    _book, profile_id = seed_reference(session, key, chapters=14, per_chapter=100)
    monkeypatch.setattr(JC, "resolve_calibration_client", lambda: (fake, True))
    job = JC.start_calibration_job(session, profile_id, llm_client=fake)
    session.commit()
    run_job_inline(job.job_id)
    session.expire_all()
    return profile_id, session.get(StyleReferenceJob, job.job_id)


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_calibration_job_scores_originals_against_plain_rewrites_and_stores_the_anchors(session, monkeypatch) -> None:
    fake = _FakeCalibrationLLM(original=8.5, plain=3.5)
    profile_id, job = _run_calibration(session, monkeypatch, "cal_ok", fake)
    assert job.state == "succeeded", job.error_json
    assert job.result_json["status"] == "usable" and job.result_json["separation"] == 5.0
    assert len(fake.requests) == 9, "3 次改写 + 6 次评审"
    nodes = [request.node_id for request in fake.requests]
    assert nodes.count(J.NEUTRALIZE_NODE_ID) == 3 and nodes.count(J.JUDGE_NODE_ID) == 6
    profile = session.get(StyleReferenceProfile, profile_id)
    stored = J.stored_calibration(profile.profile_json)
    assert stored is not None and stored["positive_mean"] == 8.5 and stored["negative_mean"] == 3.5
    assert len(stored["anchors"]) == 3 and stored["identity"]["profile_version"] == str(profile.version_tag or "")
    used = {a["window_no"] for a in stored["anchors"]}
    assert len(used) == 3
    context = JC.judge_context(session, profile_id)
    assert context.state == J.STATE_CALIBRATED
    # 校准之后的一次评审：原文水平 = 10，普通改写水平 = 0
    policy = JC.profile_policy(session, profile_id)
    verdict = JC.verdict_for(session, policy, _scores(default=6.0))
    assert verdict.state == J.STATE_CALIBRATED and verdict.overall == 5.0
    payload = JC.calibration_payload(session, profile)
    assert payload["state"] == J.STATE_CALIBRATED and payload["job"]["kind"] == JOB_KIND_CALIBRATE


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_a_judge_that_cannot_separate_the_author_is_stored_as_unusable(session, monkeypatch) -> None:
    profile_id, job = _run_calibration(session, monkeypatch, "cal_flat", _FakeCalibrationLLM(original=8.6, plain=8.4))
    assert job.state == "succeeded" and job.result_json["status"] == "unusable"
    assert JC.judge_context(session, profile_id).state == J.STATE_UNUSABLE


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_a_second_calibration_on_the_same_book_is_refused_and_cancel_needs_an_active_job(session) -> None:
    _book, profile_id = seed_reference(session, "cal_twice", chapters=14, per_chapter=100)
    fake = _FakeCalibrationLLM()
    first = JC.start_calibration_job(session, profile_id, llm_client=fake)
    session.commit()
    with pytest.raises(Exception) as excinfo:
        JC.start_calibration_job(session, profile_id, llm_client=fake)
    assert getattr(excinfo.value, "code", None) == JC.CALIBRATION_ALREADY_ACTIVE_CODE
    session.rollback()
    cancelled = JC.cancel_calibration(session, profile_id)
    session.commit()
    assert cancelled.job_id == first.job_id and cancelled.state == "cancelled"
    with pytest.raises(Exception) as excinfo:
        JC.cancel_calibration(session, profile_id)
    assert getattr(excinfo.value, "code", None) == JC.CALIBRATION_NOT_ACTIVE_CODE


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_learning_queues_a_calibration_only_when_a_model_is_available(session, monkeypatch) -> None:
    _book, profile_id = seed_reference(session, "cal_after_learn", chapters=14, per_chapter=100)
    monkeypatch.setattr(JC, "resolve_calibration_client", lambda: (None, False))
    assert JC.start_after_learn(session, profile_id) is None
    monkeypatch.setattr(JC, "resolve_calibration_client", lambda: (_FakeCalibrationLLM(), True))
    job_id = JC.start_after_learn(session, profile_id)
    assert job_id is not None
    job = session.get(StyleReferenceJob, job_id)
    assert job.kind == JOB_KIND_CALIBRATE and job.state == "queued", "排进队列，由清扫线程派发"
    assert JC.start_after_learn(session, profile_id) is None, "已有校准在排队：不重复排"
    assert len(StyleJobService(session).active_for_book(job.book_id, kind=JOB_KIND_CALIBRATE)) == 1


@pytest.mark.usefixtures("style_workers", "local_routes")
def test_calibration_routes_start_and_cancel_and_the_profile_carries_the_card(client, session, monkeypatch) -> None:
    _book, profile_id = seed_reference(session, "cal_routes", chapters=14, per_chapter=100)
    session.commit()
    fake = _FakeCalibrationLLM()
    monkeypatch.setattr(
        "novel_system.api.routes.style_reference.profiles.llm_client_and_enabled", lambda: (fake, True)
    )
    monkeypatch.setattr("novel_system.api.routes.style_reference._common.dispatch", lambda job_id: None)
    started = client.post(f"/api/v2/style-reference/profiles/{profile_id}/judge-calibration", json={})
    assert started.status_code == 200, started.text
    job_id = started.json()["data"]["job_id"]
    again = client.post(f"/api/v2/style-reference/profiles/{profile_id}/judge-calibration", json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == JC.CALIBRATION_ALREADY_ACTIVE_CODE
    detail = client.get(f"/api/v2/style-reference/profiles/{profile_id}").json()["data"]["profile"]
    card = detail["judge_calibration"]
    assert card["state"] == J.STATE_MISSING and card["job"]["job_id"] == job_id
    cancelled = client.post(f"/api/v2/style-reference/profiles/{profile_id}/judge-calibration/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["data"]["finished"] is True
    none_left = client.post(f"/api/v2/style-reference/profiles/{profile_id}/judge-calibration/cancel", json={})
    assert none_left.status_code == 409 and none_left.json()["error"]["code"] == JC.CALIBRATION_NOT_ACTIVE_CODE


# ---------------------------------------------------------------------------
# 软 QC：有绑定 → 统一评审；参考没带上 → 润色口径
# ---------------------------------------------------------------------------


class _Runner:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls: list[dict] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(
            llm_call_id=f"llm_call_judge_{len(self.calls)}",
            response=SimpleNamespace(structured_output=json.loads(json.dumps(self.output))),
        )


def _carry_reference(monkeypatch, *, carried: bool = True) -> list[dict]:
    seen: list[dict] = []

    def fake_inject(session_arg, prompt, scene, bundle=None, **kwargs):  # noqa: ANN001, ANN003
        seen.append({"template": prompt.get("template_name"), **kwargs})
        if not carried:
            return {**prompt, "_style_reference_runtime_audit": {"outcome": "degraded"}}
        return {
            **prompt,
            "system_prompt": "[STYLE_REFERENCE]\n[风格样例]合成样例[/风格样例]\n[/STYLE_REFERENCE]\n" + prompt["system_prompt"],
            "_style_reference_runtime_audit": {"outcome": "hit"},
        }

    monkeypatch.setattr("novel_system.services.style_prompt_injection.inject_style_reference_prefix", fake_inject)
    return seen


def _bound_soft_scene(session, key: str):
    from tests.support.style_gate import CLEAN_TEXT, REFERENCE_PARAGRAPH, seed_soft_scene, seed_style_binding

    profile_id = seed_style_binding(project_id=f"proj_{key}", seed=key, paragraphs=[REFERENCE_PARAGRAPH])
    seed_soft_scene(session, project_id=f"proj_{key}", draft_content=CLEAN_TEXT)
    return profile_id, CLEAN_TEXT


def test_bound_soft_qc_uses_the_unified_judge_without_scene_context_and_the_code_decides(session, monkeypatch) -> None:
    from tests.support.style_gate import run_soft_qc

    seen = _carry_reference(monkeypatch)
    _profile_id, text = _bound_soft_scene(session, "judge_soft")
    runner = _Runner(_structured({"scene.dialogue": 4.0}, default=8.0))
    decision = run_soft_qc(session, runner, text)
    session.commit()
    call = runner.calls[0]
    assert call["node_id"] == "soft_qc" and call["prompt"]["template_name"] == J.JUDGE_TEMPLATE
    assert seen[0]["template"] == J.JUDGE_TEMPLATE and seen[0]["node_id"] == "soft_qc"
    assert "## Text Under Review" in call["user_prompt"] and "Scene ID:" not in call["user_prompt"], "评审不看场景上下文"
    assert decision.branch == "patch", "没校准：原始分 4 < 7"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    assert report.resolution_code == "soft_patch"
    briefs = [entry["instruction"] for entry in report.rewrite_brief_json if "instruction" in entry]
    assert len(briefs) == 1 and briefs[0].startswith("scene.dialogue") and "scene.dialogue 改法" in briefs[0]
    judge_entry = next(entry for entry in report.rewrite_brief_json if entry.get("kind") == "reference_judge")
    assert judge_entry["calibration"]["state"] == J.STATE_MISSING
    assert judge_entry["dimensions"]["scene.dialogue"]["raw"] == 4.0 and judge_entry["source"] == "soft_qc"
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "soft_qc")).scalars().one()
    assert attempt.details_json["reference_judge"] == judge_entry


def test_bound_soft_qc_scores_by_the_calibration_and_waives_when_the_judge_is_unusable(session, monkeypatch) -> None:
    from tests.support.style_gate import run_soft_qc

    _carry_reference(monkeypatch)
    profile_id, text = _bound_soft_scene(session, "judge_soft_cal")
    runtime = JC.judge_runtime_or_none()
    profile = session.get(StyleReferenceProfile, profile_id)
    identity = JC.identity_for(profile, runtime)
    profile.profile_json = {**profile.profile_json, J.PROFILE_CALIBRATION_KEY: _calibration(positive=8.0, negative=7.6, identity=identity)}
    session.commit()
    decision = run_soft_qc(session, _Runner(_structured(default=5.0)), text)
    session.commit()
    assert decision.branch == "waive", "评审分不清作者：不据它补丁"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    entry = next(item for item in report.rewrite_brief_json if item.get("kind") == "reference_judge")
    assert entry["calibration"]["state"] == J.STATE_UNUSABLE and entry["overall"] is None


def test_soft_qc_falls_back_to_the_polish_template_when_the_reference_is_not_carried(session, monkeypatch) -> None:
    from tests.support.style_gate import run_soft_qc

    _carry_reference(monkeypatch, carried=False)
    _profile_id, text = _bound_soft_scene(session, "judge_soft_polish")
    polish = {
        "resolution_code": "soft_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
    }
    runner = _Runner(polish)
    decision = run_soft_qc(session, runner, text)
    session.commit()
    assert runner.calls[0]["prompt"]["template_name"] == "soft_qc" and decision.branch == "continue"
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    assert not [entry for entry in report.rewrite_brief_json if entry.get("kind") == "reference_judge"]
