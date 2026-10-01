"""Wave 3（结果闭环治理 §4.6/§5.5/§5.8/§7.12）：场景 token 预算（5× 单发基线）。

- run 启动时确立 `scene_token_budget = 5 × 单发基线`（已设不覆盖、从不收缩）；
- 凡带 scene_id 的 LLM 调用（成功/失败）累计 `scene_tokens_used`；
- 预算按场景生命周期累计，重跑不重置（§7.12：自动流程不得重置）；
- 预算耗尽只拦「可选支出」（补候选/批判/补丁），不撤销已有正文；
- 扩容唯一入口是作者显式 topup（留审计）。
"""

from __future__ import annotations

import json
import inspect
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from novel_system.api.app import create_app
from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    LlmCall,
    OperationLog,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.llm_accounting import LLMCallContext, execute_accounted_call
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_budget import (
    FALLBACK_INPUT_TOKENS,
    FALLBACK_OUTPUT_TOKENS,
    can_spend,
    ensure_scene_budget_initialized,
    estimate_baseline_tokens,
)
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService


from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.support.api_client import AutoKeyTestClient

pytestmark = pytest.mark.usefixtures("online_pipeline")


SCENE_ID = "CH400_SC01"
CHAPTER_ID = "CH400"
PROJECT_ID = "PROJECT_CH400"
CALL_TOKENS = 78


def _response(payload: dict, *, request_id: str) -> LLMResponse:
    usage = {"input_tokens": 60, "output_tokens": 18, "total_tokens": CALL_TOKENS}
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model="fake-model",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={"id": request_id, "model": "fake-model", "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": CALL_TOKENS}, "finish_reason": "stop"},
        usage=usage,
        raw_usage=usage,
        usage_present=True,
        usage_complete=True,
        finish_reason="stop",
    )


class CountingSceneClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return _response(
            {"scene_text": f"Provider-generated draft #{len(self.requests)}.", "continuity_notes": []},
            request_id=f"resp_scene_{len(self.requests):03d}",
        )


class CountingQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return _response(self.payload, request_id=f"resp_qc_{len(self.requests):03d}")


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


def _seed_scene(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="Budget test project", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="Budgeted reunion.",
        )
    )
    session.add(ChapterState(chapter_id=CHAPTER_ID, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Harbor",
            scene_goal="Reveal the letter.",
            beats_json=["arrival", "reveal"],
            must_include_text="",
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()


def _make_orchestrator(session, *, scene_client=None) -> Orchestrator:
    return Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=scene_client or CountingSceneClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=CountingQcClient(_hard_pass())),
        soft_qc_engine=SoftQcEngine(session, llm_client=CountingQcClient(_soft_pass())),
    )


# ---------- 预算确立与累计 ----------

def test_run_establishes_budget_and_accumulates_usage(session) -> None:
    _seed_scene(session)
    result = _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()

    state = session.get(SceneRunState, SCENE_ID)
    assert result["scene_status"] == "archived"
    assert state.scene_token_budget and state.scene_token_budget > 0
    # 5× 单发基线（§4.6）：预算是基线的整 5 倍
    assert state.scene_token_budget % 5 == 0
    assert state.provider_attempt_budget == 32
    assert state.scene_budget_basis_json["provider_attempt_budget"] == {
        "config_key": "retry_budget.provider_attempt_budget",
        "value": 32,
    }
    # 场景内每次 LLM 调用（生成 + QC + near-final）都计入
    assert state.scene_tokens_used > 0
    assert state.scene_tokens_used % CALL_TOKENS == 0
    # 完成门：总消耗不超过 5× 基线
    assert state.scene_tokens_used <= state.scene_token_budget
    calls = session.execute(select(LlmCall)).scalars().all()
    assert calls
    assert {(call.scope_type, call.scope_id) for call in calls} == {("scene", SCENE_ID)}


def test_rerun_accumulates_and_never_resets(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(session)
    orchestrator.run_scene(SCENE_ID)
    session.commit()
    state = session.get(SceneRunState, SCENE_ID)
    first_used = state.scene_tokens_used
    first_budget = state.scene_token_budget

    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    session.refresh(state)

    # §7.12：重跑不重置——生命周期累计；预算也不因重跑改变
    assert state.scene_tokens_used > first_used
    assert state.scene_token_budget == first_budget


def test_prepare_state_for_rerun_preserves_all_lifecycle_accounting_counters(session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 12_000
    state.scene_tokens_used = 3_200
    state.scene_tokens_reserved = 17
    state.attempt_budget = 9
    state.total_attempt_count = 4
    state.provider_attempt_budget = 11
    state.provider_attempts_used = 6

    Orchestrator._prepare_state_for_run(state, new_execution=True)

    assert (
        state.scene_token_budget,
        state.scene_tokens_used,
        state.scene_tokens_reserved,
        state.attempt_budget,
        state.total_attempt_count,
        state.provider_attempt_budget,
        state.provider_attempts_used,
    ) == (12_000, 3_200, 17, 9, 4, 11, 6)
    source = inspect.getsource(Orchestrator._prepare_state_for_run)
    for field in (
        "scene_token_budget",
        "scene_tokens_used",
        "scene_tokens_reserved",
        "attempt_budget",
        "total_attempt_count",
        "provider_attempt_budget",
        "provider_attempts_used",
    ):
        assert f"state.{field} =" not in source


def test_existing_budget_is_not_overwritten(session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 99999
    session.commit()

    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    session.refresh(state)
    assert state.scene_token_budget == 99999
    assert state.provider_attempt_budget == 32
    assert state.scene_budget_basis_json == {
        "basis_type": "legacy_existing_scene_token_budget",
        "scene_token_budget": 99999,
        "topup_audit_cutoff_operation_id": 0,
        "token_budget_basis": {
            "reconstructable": False,
            "reason": "legacy_scene_token_budget_without_basis",
        },
            "provider_attempt_budget": {
                "config_key": "retry_budget.provider_attempt_budget",
                "value": 32,
            },
            "attempt_budget": {
                "source": "scene_run_states.initial",
                "value": 4,
            },
        }


# ---------- can_spend 纯函数 ----------

def test_can_spend_semantics(session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 500

    state.scene_tokens_used = 0
    assert can_spend(state, 500) is True
    state.scene_tokens_used = 401
    assert can_spend(state, 100) is False  # 预留不足 → 拒绝
    state.scene_token_budget = None
    assert can_spend(state, 10**9) is True  # 未初始化预算不拦（渐进迁移）


# ---------- 预算耗尽只拦可选支出 ----------

def test_public_scene_budget_initialization_creates_missing_state_once_under_two_sessions(
    session,
) -> None:
    _seed_scene(session)
    expected_baseline = max(
        estimate_baseline_tokens(session, {}),
        FALLBACK_INPUT_TOKENS + FALLBACK_OUTPUT_TOKENS,
    )
    session.delete(session.get(SceneRunState, SCENE_ID))
    session.commit()
    barrier = Barrier(2)

    def initialize() -> tuple[int, dict, int]:
        worker = SessionLocal()
        try:
            barrier.wait(timeout=10)
            state = ensure_scene_budget_initialized(worker, SCENE_ID)
            return (
                state.scene_token_budget,
                dict(state.scene_budget_basis_json),
                state.provider_attempt_budget,
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize), pool.submit(initialize)]
        outcomes = [future.result(timeout=20) for future in futures]

    session.expire_all()
    rows = session.execute(
        select(SceneRunState).where(SceneRunState.scene_id == SCENE_ID)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].scene_token_budget == 5 * expected_baseline
    assert rows[0].provider_attempt_budget == 32
    assert rows[0].scene_budget_basis_json == {
        "baseline_tokens": expected_baseline,
        "budget_multiplier": 5,
        "scene_token_budget": 5 * expected_baseline,
        "scene_budget_armed": True,
        "topup_audit_cutoff_operation_id": 0,
        "provider_attempt_budget": {
            "config_key": "retry_budget.provider_attempt_budget",
            "value": 32,
        },
        "attempt_budget": {
            "source": "scene_run_states.initial",
            "value": 4,
        },
    }
    assert outcomes[0] == outcomes[1]


def test_orchestrator_budget_checkpoint_uses_only_public_canonical_initializer() -> None:
    source = inspect.getsource(Orchestrator._phase_budget)

    assert "ensure_scene_budget_initialized(" in source


def test_independent_scene_nodes_share_one_immutable_public_budget_basis(session) -> None:
    _seed_scene(session)
    client = CountingSceneClient()

    def call(node_id: str, ordinal: int) -> None:
        execute_accounted_call(
            session,
            client,
            LLMRequest(
                model="fake-model",
                messages=[{"role": "user", "content": f"run {node_id}"}],
                temperature=0,
                max_output_tokens=64,
                response_format="json_object",
                provider="fake-provider",
                node_id=node_id,
            ),
            LLMCallContext(
                scope_type="scene",
                scope_id=SCENE_ID,
                node_id=node_id,
                step=node_id,
                project_id=PROJECT_ID,
                chapter_id=CHAPTER_ID,
                scene_id=SCENE_ID,
                execution_id=f"task6-independent-{ordinal}",
                execution_step_key=node_id,
            ),
        )

    call("scene_blueprint", 1)
    state = session.get(SceneRunState, SCENE_ID)
    first_budget = state.scene_token_budget
    first_basis = dict(state.scene_budget_basis_json)
    scene = session.get(SceneCard, SCENE_ID)
    scene.scene_goal = "A later bundle/source mutation must not re-estimate the lifecycle budget."
    session.commit()

    call("scene_auto_rewrite", 2)
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)

    assert len(client.requests) == 2
    assert state.scene_token_budget == first_budget
    assert state.scene_budget_basis_json == first_basis
    assert state.provider_attempts_used == 2


def test_public_scene_budget_initialization_never_overwrites_or_resets_existing_state(
    session,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    basis = {
        "basis_type": "externally_managed",
        "scene_token_budget": 12_345,
        "opaque": {"version": 1},
        "provider_attempt_budget": {
            "config_key": "retry_budget.provider_attempt_budget",
            "value": 7,
        },
        "attempt_budget": {
            "source": "externally_managed",
            "value": 12,
        },
    }
    state.scene_token_budget = 12_345
    state.scene_budget_basis_json = basis
    state.provider_attempt_budget = 7
    state.scene_tokens_used = 321
    state.scene_tokens_reserved = 123
    state.provider_attempts_used = 4
    state.total_attempt_count = 9
    state.attempt_budget = 12
    session.commit()

    returned = ensure_scene_budget_initialized(session, SCENE_ID)

    assert returned.scene_token_budget == 12_345
    assert returned.scene_budget_basis_json == basis
    assert returned.provider_attempt_budget == 7
    assert returned.scene_tokens_used == 321
    assert returned.scene_tokens_reserved == 123
    assert returned.provider_attempts_used == 4
    assert returned.total_attempt_count == 9
    assert returned.attempt_budget == 12


def test_public_scene_budget_initialization_recovers_basis_only_without_reestimating(
    session,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    basis = {
        "baseline_tokens": 2_000,
        "budget_multiplier": 5,
        "scene_token_budget": 10_000,
        "provider_attempt_budget": {
            "config_key": "retry_budget.provider_attempt_budget",
            "value": 7,
        },
    }
    state.scene_token_budget = None
    state.scene_budget_basis_json = basis
    state.provider_attempt_budget = 7
    state.scene_tokens_used = 200
    state.scene_tokens_reserved = 100
    state.provider_attempts_used = 2
    session.commit()

    returned = ensure_scene_budget_initialized(session, SCENE_ID)

    assert returned.scene_token_budget == 10_000
    assert returned.scene_budget_basis_json == basis
    assert returned.provider_attempt_budget == 7
    assert returned.scene_tokens_used == 200
    assert returned.scene_tokens_reserved == 100
    assert returned.provider_attempts_used == 2


def test_public_scene_budget_initialization_preserves_legacy_provider_attempt_limit(
    session,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 12_345
    state.scene_budget_basis_json = None
    state.provider_attempt_budget = 7
    session.commit()

    returned = ensure_scene_budget_initialized(session, SCENE_ID)

    assert returned.scene_token_budget == 12_345
    assert returned.provider_attempt_budget == 7
    assert returned.scene_budget_basis_json["provider_attempt_budget"]["value"] == 7


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("basis_not_dict", lambda state: setattr(state, "scene_budget_basis_json", ["bad"])),
        (
            "basis_budget_mismatch",
            lambda state: setattr(
                state,
                "scene_budget_basis_json",
                {"scene_token_budget": 9_999},
            ),
        ),
        ("negative_used", lambda state: setattr(state, "scene_tokens_used", -1)),
        (
            "used_plus_reserved_over_budget",
            lambda state: (
                setattr(state, "scene_tokens_used", 9_500),
                setattr(state, "scene_tokens_reserved", 600),
            ),
        ),
        (
            "provider_attempts_over_limit",
            lambda state: setattr(state, "provider_attempts_used", 8),
        ),
        (
            "provider_attempt_basis_mismatch",
            lambda state: setattr(
                state,
                "scene_budget_basis_json",
                {
                    "scene_token_budget": 10_000,
                    "provider_attempt_budget": {
                        "config_key": "retry_budget.provider_attempt_budget",
                        "value": 8,
                    },
                },
            ),
        ),
        ("negative_total_attempt_count", lambda state: setattr(state, "total_attempt_count", -1)),
        ("nonpositive_attempt_budget", lambda state: setattr(state, "attempt_budget", 0)),
        ("legacy_attempt_basis_drift", lambda state: setattr(state, "attempt_budget", 999)),
        (
            "business_attempts_over_limit",
            lambda state: (
                setattr(state, "total_attempt_count", 5),
                setattr(state, "attempt_budget", 4),
            ),
        ),
    ],
)
def test_public_scene_budget_initialization_fails_closed_on_corrupt_complete_state(
    session,
    case: str,
    mutate,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 10_000
    state.scene_budget_basis_json = {
        "scene_token_budget": 10_000,
        "provider_attempt_budget": {
            "config_key": "retry_budget.provider_attempt_budget",
            "value": 7,
        },
    }
    state.provider_attempt_budget = 7
    state.scene_tokens_used = 100
    state.scene_tokens_reserved = 50
    state.provider_attempts_used = 2
    mutate(state)
    session.commit()

    with pytest.raises(ValueError, match="scene budget state is corrupt"):
        ensure_scene_budget_initialized(session, SCENE_ID)


def test_public_scene_budget_initialization_rejects_empty_budget_with_historical_usage(
    session,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = None
    state.scene_budget_basis_json = None
    state.scene_tokens_used = 1
    state.scene_tokens_reserved = 0
    state.provider_attempts_used = 0
    session.commit()

    with pytest.raises(ValueError, match="scene budget state is corrupt"):
        ensure_scene_budget_initialized(session, SCENE_ID)

    session.refresh(state)
    assert state.scene_token_budget is None
    assert state.scene_budget_basis_json is None
    assert state.scene_tokens_used == 1


def test_public_scene_budget_initialization_rejects_empty_budget_with_business_attempt_history(
    session,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = None
    state.scene_budget_basis_json = None
    state.total_attempt_count = 1
    session.commit()

    with pytest.raises(ValueError, match="scene budget state is corrupt"):
        ensure_scene_budget_initialized(session, SCENE_ID)

    session.refresh(state)
    assert state.scene_token_budget is None
    assert state.scene_budget_basis_json is None
    assert state.total_attempt_count == 1


def test_public_scene_budget_initialization_validates_negative_reserved_legacy_corruption(
    session,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 10_000
    state.scene_budget_basis_json = {
        "scene_token_budget": 10_000,
        "provider_attempt_budget": {
            "config_key": "retry_budget.provider_attempt_budget",
            "value": 7,
        },
    }
    state.provider_attempt_budget = 7
    session.commit()

    connection = session.connection()
    connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
    connection.exec_driver_sql(
        "UPDATE scene_run_states SET scene_tokens_reserved = -1 WHERE scene_id = ?",
        (SCENE_ID,),
    )
    session.commit()
    session.connection().exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    session.commit()
    session.expire_all()

    with pytest.raises(ValueError, match="scene budget state is corrupt"):
        ensure_scene_budget_initialized(session, SCENE_ID)


def test_exhausted_budget_blocks_new_provider_dispatch_without_leaking_reservation(session) -> None:
    """严格 token 门禁对所有新 provider dispatch 生效，且拒绝后不泄漏预留。"""
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1  # 立即耗尽
    session.commit()

    scene_client = CountingSceneClient()
    from novel_system.services.near_final import NearFinalAcceptanceService

    near_final_fail = {
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": 0.4,
        "scores": {},
        "findings": [],
        "revision_brief": [{"dimension": "story_necessity", "action": "补足抉择", "priority": "high"}],
        "failure_class": "scene_structure_failure",
        "requires_human_review": False,
    }
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=scene_client),
        hard_qc_engine=HardQcEngine(session, llm_client=CountingQcClient(_hard_pass())),
        soft_qc_engine=SoftQcEngine(session, llm_client=CountingQcClient(_soft_pass())),
        near_final_service=NearFinalAcceptanceService(
            session, llm_client=CountingQcClient(near_final_fail)
        ),
    )

    with pytest.raises(Exception) as exc_info:
        orchestrator.run_scene(SCENE_ID)

    assert getattr(exc_info.value, "code", None) == "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert scene_client.requests == []
    assert state.scene_tokens_reserved == 0
    assert state.provider_attempts_used == 0
    assert state.total_attempt_count == 0


@pytest.mark.parametrize(
    ("exhausted_field", "error_code"),
    [
        ("business", "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED"),
        ("provider", "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED"),
    ],
)
def test_rerun_with_exhausted_attempt_budget_makes_zero_provider_calls(
    session,
    exhausted_field: str,
    error_code: str,
) -> None:
    _seed_scene(session)
    state = ensure_scene_budget_initialized(session, SCENE_ID)
    if exhausted_field == "business":
        state.total_attempt_count = state.attempt_budget
    else:
        state.provider_attempts_used = state.provider_attempt_budget
    attempts_before = state.total_attempt_count
    session.commit()
    client = CountingSceneClient()

    with pytest.raises(Exception) as exc_info:
        _make_orchestrator(session, scene_client=client).run_scene(SCENE_ID)

    assert getattr(exc_info.value, "code", None) == error_code
    assert client.requests == []
    assert all(
        call.request_dispatched_at is None
        for call in session.scalars(select(LlmCall)).all()
    )
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert state.scene_tokens_reserved == 0
    assert state.total_attempt_count == attempts_before


# ---------- 单作者默认：解除武装（NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER=0） ----------

def test_disarmed_scene_budget_initializes_finite_sentinels(monkeypatch, session) -> None:
    """multiplier=0：三道额度落到有限哨兵，依据打上 disarmed 标记，且不违反任何不变量。"""
    from novel_system.services.scene_budget import (
        UNARMED_ATTEMPT_BUDGET,
        UNARMED_SCENE_TOKEN_BUDGET,
        is_scene_budget_disarmed,
    )

    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    _seed_scene(session)

    state = ensure_scene_budget_initialized(session, SCENE_ID)
    assert state.scene_token_budget == UNARMED_SCENE_TOKEN_BUDGET
    assert state.provider_attempt_budget == UNARMED_ATTEMPT_BUDGET
    assert state.attempt_budget == UNARMED_ATTEMPT_BUDGET
    assert state.scene_budget_basis_json["budget_multiplier"] == 0
    assert state.scene_budget_basis_json["scene_budget_armed"] is False
    assert is_scene_budget_disarmed(state) is True
    # 再次进入公共校验器不报「预算腐坏」——哨兵额度满足全部有限正值/审计不变量。
    again = ensure_scene_budget_initialized(session, SCENE_ID)
    assert again.scene_token_budget == UNARMED_SCENE_TOKEN_BUDGET


def test_disarmed_initialization_skips_the_baseline_estimate(monkeypatch, session) -> None:
    """B02-10：哨兵预算用不上单发基线——解除武装时不再渲染一遍 style_draft 模板、查一遍路由去估它；依据里记 None，
    单发当量回落到常量（只有预算断点会用它，解除武装的场景不会有预算断点）。"""
    from novel_system.services import scene_budget

    def must_not_estimate(*_args, **_kwargs):
        raise AssertionError("disarmed initialization must not estimate a baseline")

    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    monkeypatch.setattr(scene_budget, "estimate_baseline_tokens", must_not_estimate)
    monkeypatch.setattr(scene_budget, "_canonical_budget_candidate", must_not_estimate)
    _seed_scene(session)

    state = ensure_scene_budget_initialized(session, SCENE_ID)
    assert state.scene_budget_basis_json["baseline_tokens"] is None
    assert state.scene_budget_basis_json["scene_budget_armed"] is False
    assert scene_budget.baseline_tokens(state) is None
    assert scene_budget.budget_unit(state) == FALLBACK_INPUT_TOKENS + FALLBACK_OUTPUT_TOKENS
    # 再进一次公共校验器照旧通过
    assert ensure_scene_budget_initialized(session, SCENE_ID).scene_budget_basis_json["baseline_tokens"] is None


def test_disarmed_scene_lifecycle_budget_never_blocks_the_author(monkeypatch, session) -> None:
    """端到端跑一场：解除武装后不再抛 LLM_SCENE_TOKEN_BUDGET_EXHAUSTED，但记账照旧累计。"""
    from novel_system.services.scene_budget import (
        UNARMED_ATTEMPT_BUDGET,
        UNARMED_SCENE_TOKEN_BUDGET,
        is_scene_budget_disarmed,
    )

    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    _seed_scene(session)

    scene_client = CountingSceneClient()
    result = _make_orchestrator(session, scene_client=scene_client).run_scene(SCENE_ID)

    assert result["scene_status"] == "archived"
    assert scene_client.requests  # provider 真的被调用了（不是被闸门拦在派发前）
    state = session.get(SceneRunState, SCENE_ID)
    assert state.scene_token_budget == UNARMED_SCENE_TOKEN_BUDGET
    assert state.provider_attempt_budget == UNARMED_ATTEMPT_BUDGET
    assert is_scene_budget_disarmed(state) is True
    # 关闸门 ≠ 停记账：真实 usage 仍逐次累计进账本。
    assert state.scene_tokens_used > 0


def test_disarmed_scene_budget_view_reports_unlimited(monkeypatch, session) -> None:
    """成本看板对解除武装的场景显示「不限」（budget=None）而非哨兵天文数字，usage 照常透出。"""
    from novel_system.services.cost_aggregation import _budget_view

    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "0")
    _seed_scene(session)
    state = ensure_scene_budget_initialized(session, SCENE_ID)
    state.scene_tokens_used = 4321
    session.commit()

    view = _budget_view(state)
    assert view["budget"] is None
    assert view["disarmed"] is True
    assert view["over_budget"] is False
    assert view["used"] == 4321


# ---------- 扩容唯一入口：作者显式 topup ----------

def test_author_topup_expands_budget_with_audit(client, session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1000
    state.scene_tokens_used = 900
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 500, "reason": "关键场景需要再补一个候选"},
        headers={"X-Idempotency-Key": "w3-topup-1"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scene_token_budget"] == 1500
    assert data["scene_tokens_used"] == 900
    assert data["scene_tokens_reserved"] == 0
    assert data["attempt_budget"] == 4
    assert data["total_attempt_count"] == 0
    assert data["provider_attempt_budget"] == 32
    assert data["provider_attempts_used"] == 0

    session.refresh(state)
    assert state.scene_token_budget == 1500
    assert state.scene_budget_basis_json["scene_token_budget"] == 1000

    audits = session.execute(select(OperationLog)).scalars().all()
    topups = [row for row in audits if row.event_type == "scene_budget_topup"]
    assert topups and topups[0].payload_json.get("extra_tokens") == 500
    assert topups[0].operation_id > state.scene_budget_basis_json["topup_audit_cutoff_operation_id"]

    # Every later online call re-enters the public canonical validator. A legal,
    # audited topup must therefore remain usable instead of looking like basis drift.
    returned = ensure_scene_budget_initialized(session, SCENE_ID)
    assert returned.scene_token_budget == 1500


def test_apply_topup_is_the_service_entry_for_the_author_topup(session) -> None:
    """B02-10：追加的整笔事务住在 scene_budget.apply_topup（路由只做入参校验与幂等）：确立依据、一条 CAS 追加、写审计；
    超出有符号 64 位上限时 422、什么都不改；审计回放认它写的前缀。"""
    from novel_system.services.errors import DomainError
    from novel_system.services.scene_budget import LIFECYCLE_BUDGET_MAX, apply_topup

    _seed_scene(session)
    before = int(ensure_scene_budget_initialized(session, SCENE_ID).scene_token_budget)
    result = apply_topup(
        session, SCENE_ID, extra_tokens=500, extra_attempts=1, extra_provider_attempts=0, reason="多给一轮", actor_ref="author"
    )
    session.commit()
    assert result["scene_token_budget"] == before + 500
    audit = session.scalars(select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")).one()
    assert audit.object_ref == SCENE_ID
    assert audit.payload_json["reason"] == "多给一轮" and audit.payload_json["actor_ref"] == "author"
    assert audit.payload_json["scene_token_budget"] == before + 500

    with pytest.raises(DomainError) as excinfo:
        apply_topup(
            session,
            SCENE_ID,
            extra_tokens=LIFECYCLE_BUDGET_MAX,
            extra_attempts=0,
            extra_provider_attempts=0,
            reason="",
            actor_ref="author",
        )
    assert excinfo.value.code == "INVALID_BUDGET_TOPUP" and excinfo.value.status_code == 422
    session.rollback()
    state = ensure_scene_budget_initialized(session, SCENE_ID)
    assert int(state.scene_token_budget) == before + 500


def test_workbench_exposes_a_safe_lifecycle_budget_projection_for_author_topup(
    client, session
) -> None:
    _seed_scene(session)
    state = ensure_scene_budget_initialized(session, SCENE_ID)
    baseline = state.scene_budget_basis_json["baseline_tokens"]
    state.scene_tokens_used = baseline + 37
    state.total_attempt_count = 2
    state.provider_attempts_used = 5
    session.commit()

    response = client.get(f"/api/v1/scenes/{SCENE_ID}/workbench")

    assert response.status_code == 200
    budget = response.json()["data"]["scene_run_state"]["lifecycle_budget"]
    assert budget == {
        "scene_token_budget": baseline * 5,
        "scene_tokens_used": baseline + 37,
        "scene_tokens_reserved": 0,
        "scene_tokens_remaining": baseline * 4 - 37,
        "baseline_tokens": baseline,
        "recommended_topup_tokens": baseline,
        "attempt_budget": 4,
        "total_attempt_count": 2,
        "provider_attempt_budget": 32,
        "provider_attempts_used": 5,
    }


# ---------- 单发基线：依据里记的那一份，不是「当前预算 ÷ 5」（B02-09） ----------

def test_single_call_baseline_follows_the_armed_multiplier(monkeypatch, session) -> None:
    """武装倍率不是 5：可选支出按一次生成调用当量算，成本看板的基线与倍数也按依据里的基线算。"""
    from novel_system.services.cost_aggregation import _budget_view
    from novel_system.services.scene_budget import baseline_tokens, budget_unit

    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "10")
    _seed_scene(session)
    state = ensure_scene_budget_initialized(session, SCENE_ID)
    baseline = state.scene_budget_basis_json["baseline_tokens"]
    assert state.scene_budget_basis_json["budget_multiplier"] == 10
    assert state.scene_token_budget == 10 * baseline

    assert baseline_tokens(state) == baseline
    assert budget_unit(state) == baseline
    state.scene_tokens_used = baseline
    view = _budget_view(state)
    assert view["baseline"] == baseline
    assert view["multiplier_used"] == 1.0


def test_single_call_baseline_survives_an_author_topup(client, session) -> None:
    """作者追加只放大预算、不改基线：可选支出的当量、成本看板的基线、工作台的建议追加量都不跟着变大。"""
    from novel_system.services.cost_aggregation import _budget_view
    from novel_system.services.scene_budget import budget_unit

    _seed_scene(session)
    state = ensure_scene_budget_initialized(session, SCENE_ID)
    baseline = state.scene_budget_basis_json["baseline_tokens"]
    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 3 * baseline, "reason": "关键场景需要再补几个候选"},
        headers={"X-Idempotency-Key": "b02-09-topup-baseline"},
    )
    assert response.status_code == 200
    session.refresh(state)
    assert state.scene_token_budget == 8 * baseline

    assert budget_unit(state) == baseline
    assert _budget_view(state)["baseline"] == baseline
    lifecycle = client.get(f"/api/v1/scenes/{SCENE_ID}/workbench").json()["data"]["scene_run_state"]["lifecycle_budget"]
    assert lifecycle["baseline_tokens"] == baseline
    assert lifecycle["recommended_topup_tokens"] == baseline


def test_legacy_budget_without_a_recorded_baseline_divides_its_initial_budget(client, session) -> None:
    """依据里没记基线的旧预算：基线按初始预算 ÷ 历史倍率 5 还原；追加过之后也不变（以前拿当前预算去除）。"""
    from novel_system.services.scene_budget import budget_unit

    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1000
    session.commit()
    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 500, "reason": "旧预算追加"},
        headers={"X-Idempotency-Key": "b02-09-topup-legacy"},
    )
    assert response.status_code == 200
    session.refresh(state)
    assert state.scene_token_budget == 1500
    assert state.scene_budget_basis_json["basis_type"] == "legacy_existing_scene_token_budget"

    assert budget_unit(state) == 200
    lifecycle = client.get(f"/api/v1/scenes/{SCENE_ID}/workbench").json()["data"]["scene_run_state"]["lifecycle_budget"]
    assert lifecycle["baseline_tokens"] == 200


@pytest.mark.parametrize(
    ("topup", "expected"),
    [
        ({"extra_tokens": 0, "extra_attempts": 2}, (1_000, 6, 32)),
        ({"extra_tokens": 0, "extra_provider_attempts": 3}, (1_000, 4, 35)),
        ({"extra_tokens": 25, "extra_attempts": 2, "extra_provider_attempts": 3}, (1_025, 6, 35)),
    ],
)
def test_author_topup_expands_any_lifecycle_budget_combination_without_resetting_usage(
    client,
    session,
    topup,
    expected,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1_000
    state.scene_tokens_used = 321
    state.scene_tokens_reserved = 7
    state.total_attempt_count = 3
    state.provider_attempts_used = 5
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json=topup,
        headers={"X-Idempotency-Key": f"task6-topup-{sorted(topup.items())}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert (
        data["scene_token_budget"],
        data["attempt_budget"],
        data["provider_attempt_budget"],
    ) == expected
    assert (
        data["scene_tokens_used"],
        data["scene_tokens_reserved"],
        data["total_attempt_count"],
        data["provider_attempts_used"],
    ) == (321, 7, 3, 5)
    session.refresh(state)
    assert (
        state.scene_tokens_used,
        state.scene_tokens_reserved,
        state.total_attempt_count,
        state.provider_attempts_used,
    ) == (321, 7, 3, 5)
    audit = session.scalar(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    )
    assert audit.payload_json["extra_tokens"] == topup.get("extra_tokens", 0)
    assert audit.payload_json["extra_attempts"] == topup.get("extra_attempts", 0)
    assert audit.payload_json["extra_provider_attempts"] == topup.get(
        "extra_provider_attempts", 0
    )
    assert audit.payload_json["scene_tokens_reserved"] == 7
    assert audit.payload_json["total_attempt_count"] == 3
    assert audit.payload_json["provider_attempts_used"] == 5

    returned = ensure_scene_budget_initialized(session, SCENE_ID)
    assert (
        returned.scene_token_budget,
        returned.attempt_budget,
        returned.provider_attempt_budget,
    ) == expected


def test_author_topup_is_idempotent_across_all_three_budget_dimensions(client, session) -> None:
    _seed_scene(session)
    key = "task6-three-dimensional-idempotency"
    payload = {"extra_tokens": 50, "extra_attempts": 2, "extra_provider_attempts": 4}

    first = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )
    replay = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json=payload,
        headers={"X-Idempotency-Key": key},
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"] == replay.json()["data"]
    assert replay.headers["X-Idempotency-Status"] == "replayed"
    state = session.get(SceneRunState, SCENE_ID)
    assert state.attempt_budget == 6
    assert state.provider_attempt_budget == 36
    topups = session.scalars(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    ).all()
    assert len(topups) == 1


def test_author_topup_allows_the_next_online_scene_llm_call(client, session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1000
    session.commit()
    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 50_000, "reason": "continue online generation"},
        headers={"X-Idempotency-Key": "w3-topup-online-continuation"},
    )
    assert response.status_code == 200

    scene_client = CountingSceneClient()
    result = _make_orchestrator(session, scene_client=scene_client).run_scene(SCENE_ID)
    assert result["scene_status"] == "archived"
    assert scene_client.requests
    assert session.get(SceneRunState, SCENE_ID).scene_token_budget == 51_000


def test_topup_audit_replay_rejects_a_forged_non_monotonic_prefix(client, session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1000
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 500, "reason": "audited"},
        headers={"X-Idempotency-Key": "w3-topup-audit-prefix"},
    )
    assert response.status_code == 200
    topup = session.execute(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    ).scalar_one()
    topup.payload_json = {**topup.payload_json, "scene_token_budget": 1499}
    session.commit()

    with pytest.raises(ValueError, match="scene budget state is corrupt"):
        ensure_scene_budget_initialized(session, SCENE_ID)


def test_basis_only_recovery_restores_full_audited_topup_budget(client, session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1000
    session.commit()
    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 500},
        headers={"X-Idempotency-Key": "w3-topup-basis-only-recovery"},
    )
    assert response.status_code == 200
    session.execute(
        SceneRunState.__table__.update()
        .where(SceneRunState.scene_id == SCENE_ID)
        .values(scene_token_budget=None)
    )
    session.commit()

    recovered = ensure_scene_budget_initialized(session, SCENE_ID)
    assert recovered.scene_budget_basis_json["scene_token_budget"] == 1000
    assert recovered.scene_token_budget == 1500


def test_budget_checkpoint_accepts_a_valid_topup_prefix_and_later_full_replay(client, session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1000
    session.commit()
    first = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 500},
        headers={"X-Idempotency-Key": "w3-topup-before-budget-checkpoint"},
    )
    assert first.status_code == 200
    state = ensure_scene_budget_initialized(session, SCENE_ID)

    execution_id = "topup-budget-checkpoint-execution"
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(SCENE_ID, execution_id)
    orchestrator = Orchestrator(session)
    checkpoints.save_checkpoint(
        scene_id=SCENE_ID,
        execution_id=execution_id,
        node_key="budget_ready",
        artifact_refs={"scene_token_budget": state.scene_token_budget},
        artifact_hashes={"budget_basis": orchestrator._json_hash(state.scene_budget_basis_json)},
    )
    session.commit()

    second = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 250},
        headers={"X-Idempotency-Key": "w3-topup-after-budget-checkpoint"},
    )
    assert second.status_code == 200
    orchestrator._execution_id = execution_id
    orchestrator._checkpoint_service = checkpoints
    orchestrator._validate_budget_checkpoint(session.get(SceneRunState, SCENE_ID))
    assert session.get(SceneRunState, SCENE_ID).scene_token_budget == 1750


def test_topup_rejects_non_positive(client, session) -> None:
    _seed_scene(session)
    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 0},
        headers={"X-Idempotency-Key": "w3-topup-bad"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"extra_tokens": 0, "extra_attempts": 0, "extra_provider_attempts": 0},
        {"extra_attempts": -1},
        {"extra_provider_attempts": -1},
        {"extra_attempts": True},
        {"extra_provider_attempts": 1.5},
        {"extra_attempts": "1"},
    ],
)
def test_topup_rejects_invalid_three_dimensional_requests_without_mutation_or_audit(
    client,
    session,
    payload,
) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    before = (state.scene_token_budget, state.attempt_budget, state.provider_attempt_budget)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json=payload,
        headers={"X-Idempotency-Key": f"task6-invalid-{repr(payload)}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_BUDGET_TOPUP"
    session.refresh(state)
    assert (state.scene_token_budget, state.attempt_budget, state.provider_attempt_budget) == before
    assert session.scalar(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    ) is None


@pytest.mark.parametrize("extra_tokens", [True, False, 1.0, 1.5, "1", "500"])
def test_topup_rejects_coercible_non_integer_types(client, session, extra_tokens) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    original_budget = state.scene_token_budget

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": extra_tokens},
        headers={"X-Idempotency-Key": f"w3-topup-strict-type-{type(extra_tokens).__name__}-{extra_tokens}"},
    )

    assert response.status_code == 422
    session.refresh(state)
    assert state.scene_token_budget == original_budget
    assert session.scalar(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    ) is None


def test_topup_accepts_the_exact_signed_int64_budget_boundary(client, session) -> None:
    _seed_scene(session)
    int64_max = (1 << 63) - 1
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = int64_max - 1
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 1},
        headers={"X-Idempotency-Key": "w3-topup-int64-boundary"},
    )

    assert response.status_code == 200
    session.refresh(state)
    assert type(state.scene_token_budget) is int
    assert state.scene_token_budget == int64_max
    audit = session.scalar(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    )
    assert audit.payload_json["scene_token_budget"] == int64_max


def test_topup_rejects_signed_int64_overflow_without_mutation_or_audit(client, session) -> None:
    _seed_scene(session)
    int64_max = (1 << 63) - 1
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = int64_max
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json={"extra_tokens": 1},
        headers={"X-Idempotency-Key": "w3-topup-int64-overflow"},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_BUDGET_TOPUP"
    assert error["details"] == {
        "extra_tokens": 1,
        "scene_token_budget": int64_max,
        "max_scene_token_budget": int64_max,
    }
    session.refresh(state)
    assert type(state.scene_token_budget) is int
    assert state.scene_token_budget == int64_max
    assert session.scalar(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    ) is None


@pytest.mark.parametrize(
    ("overflow_field", "payload"),
    [
        ("attempt_budget", {"extra_tokens": 10, "extra_attempts": 1}),
        (
            "provider_attempt_budget",
            {"extra_tokens": 10, "extra_provider_attempts": 1},
        ),
    ],
)
def test_three_dimensional_topup_overflow_is_atomic(
    client,
    session,
    overflow_field: str,
    payload: dict,
) -> None:
    _seed_scene(session)
    int64_max = (1 << 63) - 1
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1_000
    setattr(state, overflow_field, int64_max)
    session.commit()
    before = (
        state.scene_token_budget,
        state.attempt_budget,
        state.provider_attempt_budget,
    )

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/budget/topup",
        json=payload,
        headers={"X-Idempotency-Key": f"task6-overflow-{overflow_field}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_BUDGET_TOPUP"
    session.refresh(state)
    assert (
        state.scene_token_budget,
        state.attempt_budget,
        state.provider_attempt_budget,
    ) == before
    assert session.scalar(
        select(OperationLog).where(OperationLog.event_type == "scene_budget_topup")
    ) is None


def test_concurrent_three_dimensional_topups_are_atomic_and_each_audited_once(session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 1_000
    session.commit()
    barrier = Barrier(2)

    def topup(ordinal: int) -> tuple[int, dict]:
        worker_client = worker_clients[ordinal - 1]
        barrier.wait(timeout=10)
        response = worker_client.post(
            f"/api/v1/scenes/{SCENE_ID}/budget/topup",
            json={
                "extra_tokens": 100,
                "extra_attempts": 2,
                "extra_provider_attempts": 3,
            },
            headers={"X-Idempotency-Key": f"task6-concurrent-topup-{ordinal}"},
        )
        return response.status_code, response.json()

    # Build the two ASGI applications serially: this test exercises concurrent
    # budget transactions, not concurrent mutation of FastAPI's shared router
    # metadata during application assembly.  Creating both apps inside worker
    # threads races FastAPI/Pydantic field cloning and emits a spurious
    # UnsupportedFieldAttributeWarning unrelated to the budget contract.
    with AutoKeyTestClient(create_app()) as first_client, AutoKeyTestClient(create_app()) as second_client:
        worker_clients = (first_client, second_client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(topup, (1, 2)))

    assert [status for status, _ in outcomes] == [200, 200]
    session.expire_all()
    state = session.get(SceneRunState, SCENE_ID)
    assert (
        state.scene_token_budget,
        state.attempt_budget,
        state.provider_attempt_budget,
    ) == (1_200, 8, 38)
    audits = session.scalars(
        select(OperationLog)
        .where(OperationLog.event_type == "scene_budget_topup")
        .order_by(OperationLog.operation_id)
    ).all()
    assert len(audits) == 2
    assert [row.payload_json["scene_token_budget"] for row in audits] == [1_100, 1_200]
    assert [row.payload_json["attempt_budget"] for row in audits] == [6, 8]
    assert [row.payload_json["provider_attempt_budget"] for row in audits] == [35, 38]
    ensure_scene_budget_initialized(session, SCENE_ID)
