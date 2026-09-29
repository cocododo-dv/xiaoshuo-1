"""token / 金额聚合——场景 / 章节 / 项目三级 + 成本看板（结果闭环治理 §5.8/§10）。

以 token 为主（批准#4）：占比、构成、排序都按 token；金额只算价书里写了单价的模型，其余「未定价」
（``cost`` 为 ``None``）。跨服务的 token 分列；三口径（估算 / 实际 / 计费）；额外成本只算失败的物理尝试
（重评 R2 第 5 项删了「重复质检」「补候选」两项）。
"""
from __future__ import annotations

import textwrap

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services import cost_aggregation as ca
from novel_system.services import pricing


@pytest.fixture
def priced_gpt5(tmp_path, monkeypatch):
    """价书只给 openai_compatible/gpt-5 定价：输入 1、输出 4（每千 token）。"""
    path = tmp_path / "pricing.yaml"
    path.write_text(
        textwrap.dedent(
            """
            currency: USD
            prices:
              - {provider: openai_compatible, model: gpt-5, input_per_1k: 1.0, output_per_1k: 4.0}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pricing, "_price_book_path", lambda: path)
    pricing.reset_price_book_cache()
    return path


def _gpt5_cost(tokens: int) -> float:
    """``_call`` 把 tokens 拆成 2/3 输入 + 1/3 输出。"""
    return int(tokens * 2 // 3) / 1000 * 1.0 + int(tokens // 3) / 1000 * 4.0


def _scene(session, scene_id, chapter_id="CH1", project_id="proj1", seq=None):
    if project_id and session.get(StoryProject, project_id) is None:
        session.add(
            StoryProject(
                project_id=project_id,
                title=project_id,
                outline_text="test outline",
            )
        )
        session.flush()
    if session.get(ChapterGoal, chapter_id) is None:
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                chapter_goal=f"goal {chapter_id}",
            )
        )
        session.flush()
    if seq is None:
        seq = int(
            session.scalar(
                select(func.coalesce(func.max(SceneCard.scene_seq), 0)).where(
                    SceneCard.chapter_id == chapter_id,
                    SceneCard.trashed_flag == 0,
                )
            )
            or 0
        ) + 1
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=seq,
            scene_goal="g",
        )
    )
    session.flush()


def _runstate(session, scene_id, *, budget=None, used=0, criticality=None, policy=None):
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_token_budget=budget,
            scene_tokens_used=used,
            criticality_level=criticality,
            run_policy=policy,
        )
    )
    session.flush()


def _call(
    session,
    scene_id,
    *,
    node_id,
    tokens=150,
    provider="openai_compatible",
    model="gpt-5",
    chapter_id="CH1",
    project_id="proj1",
    error_code=None,
    created_at=None,
):
    idx = _call.counter = getattr(_call, "counter", 0) + 1
    session.add(
        LlmCall(
            llm_call_id=f"llm_{idx:04d}",
            scope_type="scene" if scene_id else "project" if project_id else "system",
            scope_id=scene_id or project_id or node_id,
            provider=provider,
            model=model,
            node_id=node_id,
            step=node_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            prompt_tokens=int(tokens * 2 // 3),
            completion_tokens=int(tokens // 3),
            total_tokens=tokens,
            error_code=error_code,
            created_at=created_at or f"2026-07-12T00:00:{idx:02d}Z",
        )
    )
    session.flush()


def _accounted_retry_call(session, scene_id: str) -> str:
    """Insert one logical parent with two physical attempts and exact aggregates."""
    call_id = f"accounted_retry_{scene_id}"
    session.add(
        LlmCall(
            llm_call_id=call_id,
            scope_type="scene",
            scope_id=scene_id,
            provider="openai_compatible",
            model="gpt-5",
            node_id="style_draft",
            step="style_draft",
            scene_id=scene_id,
            chapter_id="CH1",
            project_id="proj1",
            prompt_tokens=80,
            completion_tokens=20,
            total_tokens=100,
            estimated_tokens=120,
            reserved_tokens=150,
            budget_charged_tokens=100,
            usage_is_estimate=True,
            accounting_status="settled",
            request_dispatched_at="2026-07-12T00:00:01Z",
        )
    )
    session.add_all(
        [
            LlmCallAttempt(
                attempt_id=f"{call_id}:0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                prompt_tokens=30,
                completion_tokens=10,
                total_tokens=40,
                estimated_tokens=40,
                reserved_tokens=50,
                budget_charged_tokens=40,
                usage_is_estimate=True,
                accounting_status="failed",
                request_dispatched_at="2026-07-12T00:00:01Z",
                error_code="LLM_TIMEOUT",
            ),
            LlmCallAttempt(
                attempt_id=f"{call_id}:1",
                llm_call_id=call_id,
                provider_attempt_no=1,
                dispatch_kind="missing_text_degrade",
                prompt_tokens=50,
                completion_tokens=10,
                total_tokens=60,
                estimated_tokens=80,
                reserved_tokens=100,
                budget_charged_tokens=60,
                usage_is_estimate=False,
                accounting_status="settled",
                request_dispatched_at="2026-07-12T00:00:02Z",
            ),
        ]
    )
    session.flush()
    return call_id


def _archived(session, scene_id, chapter_id="CH1"):
    session.add(
        FinalScene(
            row_id=f"fs_{scene_id}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            content="正文",
            status="archived",
            source_bundle_id="b",
            source_bundle_hash="h",
        )
    )
    session.flush()


# ---- classify_phase ----------------------------------------------------------

def test_classify_phase_maps_known_nodes():
    assert ca.classify_phase("style_draft", "style_draft") == "candidate_generation"
    assert ca.classify_phase("neutral_draft", "neutral_draft") == "candidate_generation"
    assert ca.classify_phase("hard_qc", "hard_qc") == "quality_check"
    assert ca.classify_phase("near_final_acceptance_review", "x") == "quality_check"
    assert ca.classify_phase("style_patch", "style_patch") == "revision"
    assert ca.classify_phase("scene_auto_rewrite", "x") == "revision"
    assert ca.classify_phase("writer_deep_review", "x") == "review"
    assert ca.classify_phase("style_profile_extract", "x") == "other"
    # 构思/案头生成类也是候选生成——真实项目的成本大头不落「其他」
    assert ca.classify_phase("snowflake_step_generate", "x") == "candidate_generation"
    assert ca.classify_phase("snowflake_step_candidates", "x") == "candidate_generation"
    assert ca.classify_phase("snowflake_workspace_assistant", "x") == "candidate_generation"
    assert ca.classify_phase("author_proposal_generate", "x") == "candidate_generation"
    assert ca.classify_phase("project_outline_plan", "x") == "candidate_generation"
    assert ca.classify_phase("scene_generation", "x") == "candidate_generation"
    assert ca.classify_phase("snowflake_scene_triage_suggest", "x") == "review"
    # 风格参考验证/评审仍先被 QC/review 关键词抓走，不受生成词干扰
    assert ca.classify_phase("style_ref_validate_semantic", "x") == "quality_check"
    assert ca.classify_phase("chapter_near_final_review", "x") == "quality_check"


# ---- scene_cost --------------------------------------------------------------

def test_scene_cost_phase_shares_sum_to_one(session):
    _scene(session, "S1")
    _runstate(session, "S1")
    _call(session, "S1", node_id="style_draft", tokens=300)
    _call(session, "S1", node_id="hard_qc", tokens=100)
    _call(session, "S1", node_id="writer_deep_review", tokens=100)
    result = ca.scene_cost(session, "S1")
    shares = sum(p["share"] for p in result["phase_breakdown"].values())
    assert abs(shares - 1.0) < 1e-6
    # 占比按 token：模型未定价时各阶段照样有占比
    assert result["phase_breakdown"]["candidate_generation"]["share"] == pytest.approx(0.6)
    assert result["phase_breakdown"]["candidate_generation"]["call_count"] == 1
    assert result["total_tokens"] == 500
    assert result["call_count"] == 3


def test_unpriced_models_show_tokens_and_no_invented_money(session):
    """默认价书没有任何单价：金额全是 None（「未定价」），不再按占位估算价编一个美元数。"""
    _scene(session, "S1u")
    _runstate(session, "S1u")
    _call(session, "S1u", node_id="style_draft", tokens=300, model="relay-sonnet")
    _call(session, "S1u", node_id="hard_qc", tokens=100, model="relay-flash")
    result = ca.scene_cost(session, "S1u")
    assert result["total_tokens"] == 400
    assert result["total_cost"] is None
    assert result["currency"] is None
    assert result["cost_by_provider"] == {"openai_compatible": None}
    assert all(phase["cost"] is None for phase in result["phase_breakdown"].values())
    assert result["pricing"] == {
        "priced_call_count": 0,
        "unpriced_call_count": 2,
        "priced_tokens": 0,
        "unpriced_tokens": 400,
        "complete": False,
        "unpriced_models": [
            {"provider": "openai_compatible", "model": "relay-sonnet", "tokens": 300, "call_count": 1},
            {"provider": "openai_compatible", "model": "relay-flash", "tokens": 100, "call_count": 1},
        ],
    }


def test_money_covers_only_priced_models(session, priced_gpt5):
    _scene(session, "S1p")
    _runstate(session, "S1p")
    _call(session, "S1p", node_id="style_draft", tokens=300)
    _call(session, "S1p", node_id="hard_qc", tokens=150, model="relay-flash")
    result = ca.scene_cost(session, "S1p")
    assert result["total_cost"] == pytest.approx(_gpt5_cost(300))
    assert result["currency"] == "USD"
    assert result["phase_breakdown"]["candidate_generation"]["cost"] == pytest.approx(_gpt5_cost(300))
    assert result["phase_breakdown"]["quality_check"]["cost"] is None
    assert result["pricing"]["priced_call_count"] == 1
    assert result["pricing"]["priced_tokens"] == 300
    assert result["pricing"]["unpriced_tokens"] == 150
    assert result["pricing"]["complete"] is False
    assert [m["model"] for m in result["pricing"]["unpriced_models"]] == ["relay-flash"]


def test_scene_cost_cross_provider_tokens_not_summed(session):
    _scene(session, "S2")
    _runstate(session, "S2")
    _call(session, "S2", node_id="style_draft", provider="openai_compatible", model="gpt-5", tokens=200)
    _call(session, "S2", node_id="hard_qc", provider="anthropic", model="claude", tokens=100)
    result = ca.scene_cost(session, "S2")
    assert result["cross_provider"] is True
    assert set(result["tokens_by_provider"]) == {"openai_compatible", "anthropic"}
    assert result["tokens_by_provider"]["openai_compatible"] == 200
    assert result["tokens_by_provider"]["anthropic"] == 100


def test_scene_cost_budget_over_and_under(session):
    _scene(session, "S3")
    _runstate(session, "S3", budget=1000, used=1200, policy="strict")
    _call(session, "S3", node_id="style_draft", tokens=150)
    over = ca.scene_cost(session, "S3")
    assert over["budget"]["over_budget"] is True
    assert over["budget"]["usage_ratio"] > 1.0
    assert over["budget"]["run_policy"] == "strict"

    _scene(session, "S3b")
    _runstate(session, "S3b", budget=1000, used=200)
    _call(session, "S3b", node_id="style_draft", tokens=150)
    under = ca.scene_cost(session, "S3b")
    assert under["budget"]["over_budget"] is False


def test_scene_cost_three_calibers(session):
    _scene(session, "S4")
    _runstate(session, "S4", budget=1000, used=150)
    _call(session, "S4", node_id="style_draft", tokens=150)
    result = ca.scene_cost(session, "S4")
    cal = result["calibers"]
    assert set(cal) == {"estimate", "provider_actual", "budget_charged"}
    assert cal["estimate"]["tokens"] == 150
    assert cal["provider_actual"]["tokens"] == 0
    assert cal["budget_charged"]["tokens"] == 0


def test_scene_cost_uses_parent_once_and_attempts_only_for_calibers_and_observability(session):
    _scene(session, "S4-accounted")
    _runstate(session, "S4-accounted", budget=1_000, used=100)
    _accounted_retry_call(session, "S4-accounted")

    result = ca.scene_cost(session, "S4-accounted")

    assert result["call_count"] == 1
    assert result["is_estimate"] is True
    assert result["total_tokens"] == 100
    assert result["phase_breakdown"]["candidate_generation"]["tokens"] == 100
    assert result["calibers"] == {
        "estimate": {"tokens": 120, "source": "llm_calls.estimated_tokens"},
        "provider_actual": {
            "tokens": 60,
            "source": "llm_call_attempts.total_tokens_with_provider_usage",
        },
        "budget_charged": {
            "tokens": 100,
            "source": "llm_calls.budget_charged_tokens",
        },
    }
    assert result["attempt_observability"] == {
        "attempt_row_count": 2,
        "physical_attempt_count": 2,
        "pre_dispatch_attempt_count": 0,
        "usage_estimate_count": 1,
        "exception_count": 1,
        "retry_attempt_count": 1,
        "transport_retry_attempt_count": 0,
        "response_parse_retry_attempt_count": 0,
        "degrade_attempt_count": 1,
        "legacy_parent_without_attempt_count": 0,
        "legacy_unreconstructable_tokens": 0,
    }
    # 失败重试 = 发出去却失败的那次物理尝试（40 token），不是整个父调用
    assert result["extra_cost"] == {
        "failed_tokens": 40,
        "failed_attempt_count": 1,
        "failed_cost": None,
        "failed_share": 0.4,
    }


@pytest.mark.parametrize(
    ("dispatch_kind", "transport_count", "parse_count", "degrade_count"),
    [
        ("transport_retry", 1, 0, 0),
        ("response_parse_retry", 0, 1, 0),
        ("api_mode_degrade", 0, 0, 1),
        ("structured_output_degrade", 0, 0, 1),
    ],
)
def test_retry_and_degrade_subtypes_are_durably_distinguishable(
    session,
    dispatch_kind: str,
    transport_count: int,
    parse_count: int,
    degrade_count: int,
):
    scene_id = f"S4-{dispatch_kind}"
    _scene(session, scene_id)
    _runstate(session, scene_id, budget=1_000, used=100)
    call_id = _accounted_retry_call(session, scene_id)
    retry = session.query(LlmCallAttempt).filter_by(
        llm_call_id=call_id,
        provider_attempt_no=1,
    ).one()
    retry.dispatch_kind = dispatch_kind
    session.flush()

    observed = ca.scene_cost(session, scene_id)["attempt_observability"]

    assert observed["retry_attempt_count"] == 1
    assert observed["transport_retry_attempt_count"] == transport_count
    assert observed["response_parse_retry_attempt_count"] == parse_count
    assert observed["degrade_attempt_count"] == degrade_count


def test_undispatched_attempt_row_is_not_reported_as_a_physical_provider_attempt(session):
    _scene(session, "S4-undispatched")
    _runstate(session, "S4-undispatched", budget=1_000, used=100)
    call_id = _accounted_retry_call(session, "S4-undispatched")
    rejected = session.query(LlmCallAttempt).filter_by(
        llm_call_id=call_id,
        provider_attempt_no=0,
    ).one()
    rejected.request_dispatched_at = None
    session.flush()

    result = ca.scene_cost(session, "S4-undispatched")

    assert result["attempt_observability"]["attempt_row_count"] == 2
    assert result["attempt_observability"]["physical_attempt_count"] == 1
    assert result["attempt_observability"]["pre_dispatch_attempt_count"] == 1
    assert result["calibers"]["provider_actual"]["tokens"] == 60


def test_scene_cost_extra_cost_is_failed_attempts_only(session, priced_gpt5):
    _scene(session, "S5")
    _runstate(session, "S5", criticality="standard")
    _call(session, "S5", node_id="style_draft", tokens=300, created_at="2026-07-12T00:00:01Z")
    _call(session, "S5", node_id="hard_qc", tokens=60, created_at="2026-07-12T00:00:02Z")
    # 1 个失败的老式调用（没有物理尝试行）→ 失败重试
    _call(session, "S5", node_id="style_patch", tokens=40, error_code="LLM_TIMEOUT", created_at="2026-07-12T00:00:03Z")
    extra = ca.scene_cost(session, "S5")["extra_cost"]
    assert extra == {
        "failed_tokens": 40,
        "failed_attempt_count": 1,
        "failed_cost": pytest.approx(_gpt5_cost(40)),
        "failed_share": 0.1,
    }


def _live_shaped_critical_scene_run(session, scene_id):
    """真实库里一场关键场景的一轮起草：蓝图 + 章节架构 + 人物压力 + 两次风格稿，三道不同的质检各一次。"""
    _scene(session, scene_id)
    _runstate(session, scene_id, criticality="critical")
    for second, node_id in enumerate(
        (
            "scene_blueprint",
            "chapter_story_architecture",
            "character_pressure_blueprint",
            "style_draft",
            "style_draft",
            "hard_qc",
            "soft_qc",
            "near_final_acceptance_review",
        ),
        start=1,
    ):
        _call(session, scene_id, node_id=node_id, tokens=90, created_at=f"2026-07-12T00:00:{second:02d}Z")


def test_single_candidate_critical_scene_shows_no_candidate_top_up(session, priced_gpt5):
    """重评 R2 第 5 项：只有一份候选的关键场景没有「补候选」——蓝图 / 架构 / 人物压力 / 风格稿都不是补写。"""
    _live_shaped_critical_scene_run(session, "S5-critical")
    extra = ca.scene_cost(session, "S5-critical")["extra_cost"]
    assert "low_dispersion_topup_cost" not in extra
    assert extra["failed_tokens"] == 0


def test_ordinary_quality_checks_are_not_repeated_checks(session, priced_gpt5):
    """一轮起草依次跑硬质检 / 软质检 / 准定稿评审——三道不同的质检，不是「重复质检」。"""
    _live_shaped_critical_scene_run(session, "S5-qc")
    extra = ca.scene_cost(session, "S5-qc")["extra_cost"]
    assert "repeat_qc_cost" not in extra
    assert extra == {"failed_tokens": 0, "failed_attempt_count": 0, "failed_cost": None, "failed_share": 0.0}


def test_scene_cost_empty_no_calls(session):
    _scene(session, "S6")
    _runstate(session, "S6")
    result = ca.scene_cost(session, "S6")
    assert result["total_tokens"] == 0
    assert result["total_cost"] is None
    assert result["call_count"] == 0
    assert result["pricing"]["complete"] is True


# ---- chapter / project rollup ------------------------------------------------

def test_chapter_cost_archived_metrics(session):
    _scene(session, "C1S1", chapter_id="CHX")
    _scene(session, "C1S2", chapter_id="CHX")
    _call(session, "C1S1", node_id="style_draft", tokens=200, chapter_id="CHX")
    _call(session, "C1S2", node_id="style_draft", tokens=100, chapter_id="CHX")
    _archived(session, "C1S1", chapter_id="CHX")
    _archived(session, "C1S2", chapter_id="CHX")
    result = ca.chapter_cost(session, "CHX")
    assert result["archived_scene_count"] == 2
    assert result["total_tokens"] == 300
    assert result["calibers"]["estimate"]["tokens"] == 300
    assert result["calibers"]["provider_actual"]["tokens"] == 0
    assert result["calibers"]["budget_charged"]["tokens"] == 0
    assert result["tokens_per_archived_scene"] == 150


def test_project_cost_rollup(session):
    _scene(session, "P1S1", chapter_id="PCH1", project_id="P1")
    _scene(session, "P1S2", chapter_id="PCH2", project_id="P1")
    _call(session, "P1S1", node_id="style_draft", tokens=200, chapter_id="PCH1", project_id="P1")
    _call(session, "P1S2", node_id="hard_qc", tokens=100, chapter_id="PCH2", project_id="P1")
    _archived(session, "P1S1", chapter_id="PCH1")
    result = ca.project_cost(session, "P1")
    assert result["total_tokens"] == 300
    assert result["total_cost"] is None
    assert result["calibers"]["estimate"]["tokens"] == 300
    assert result["calibers"]["provider_actual"]["tokens"] == 0
    assert result["calibers"]["budget_charged"]["tokens"] == 0
    assert result["chapter_count"] == 2
    assert result["scene_count"] == 2
    assert result["archived_scene_count"] == 1
    assert result["archived_chapter_count"] == 1
    assert result["tokens_per_archived_scene"] == 300
    assert result["cost_per_archived_chapter"] is None


def test_project_cost_counts_archived_chapters_in_one_query_each(session, priced_gpt5):
    """归档场景 / 归档章节各一次聚合查询（以前逐章查一次），口径不变：只数本项目的场景与章节。"""
    for index in range(1, 4):
        _scene(session, f"P2S{index}", chapter_id=f"P2CH{index}", project_id="P2")
        _call(session, f"P2S{index}", node_id="style_draft", tokens=300, chapter_id=f"P2CH{index}", project_id="P2")
    _archived(session, "P2S1", chapter_id="P2CH1")
    _archived(session, "P2S2", chapter_id="P2CH2")
    _scene(session, "OTHER1", chapter_id="OCH1", project_id="OTHER")
    _archived(session, "OTHER1", chapter_id="OCH1")

    statements: list[str] = []
    from sqlalchemy import event

    listener = lambda *args: statements.append(args[2])  # noqa: E731 — (conn, cursor, statement, ...)
    event.listen(session.get_bind(), "before_cursor_execute", listener)
    try:
        result = ca.project_cost(session, "P2")
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", listener)
    assert result["archived_scene_count"] == 2
    assert result["archived_chapter_count"] == 2
    assert result["cost_per_archived_chapter"] == pytest.approx(round(3 * _gpt5_cost(300) / 2, 6))
    assert sum("final_scenes" in statement for statement in statements) == 2
    # 调用与物理尝试各查一次，只取要用的列（请求摘要只对没有尝试行的老记录单独读一次）
    call_queries = [s for s in statements if "llm_calls.total_tokens" in s]
    assert len(call_queries) == 1
    assert "request_payload_summary" not in call_queries[0]
    assert sum("llm_call_attempts.total_tokens" in s for s in statements) == 1


# ---------------------------------------------------------------------------
# project_cost_dashboard：趋势 / 模型 / 节点 / 章节构成 + Top 调用
# ---------------------------------------------------------------------------

def _today_iso(offset_days=0, seq=0):
    from datetime import UTC, datetime, timedelta

    at = datetime.now(UTC) - timedelta(days=offset_days)
    return at.strftime("%Y-%m-%d") + f"T08:00:{seq:02d}Z"


def _dash_seed(session):
    _scene(session, "D1S1", chapter_id="DCH1", project_id="DP")
    _scene(session, "D1S2", chapter_id="DCH2", project_id="DP", seq=2)
    # 今天：草稿 300 tok；昨天：QC 100 tok（另一 provider/model）；8 天前：草稿 200 tok
    _call(session, "D1S1", node_id="style_draft", tokens=300, chapter_id="DCH1",
          project_id="DP", created_at=_today_iso(0, 1))
    _call(session, "D1S2", node_id="hard_qc", tokens=100, chapter_id="DCH2",
          project_id="DP", provider="anthropic", model="claude-x",
          created_at=_today_iso(1, 2))
    _call(session, "D1S1", node_id="style_draft", tokens=200, chapter_id="DCH1",
          project_id="DP", created_at=_today_iso(8, 3))
    session.flush()


def test_dashboard_trend_dense_window_and_bucketing(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", days=7)
    trend = dash["trend"]
    assert trend["days"] == 7
    assert len(trend["series"]) == 7
    # 稠密补零：窗口内无调用的天也在
    assert all("date" in item for item in trend["series"])
    # 8 天前的调用不进 7 天窗口
    assert trend["window_tokens"] == 400
    assert trend["window_call_count"] == 2
    today_bucket = trend["series"][-1]
    assert today_bucket["tokens"] == 300
    assert today_bucket["call_count"] == 1
    assert today_bucket["cost"] is None  # 默认价书没有单价
    assert trend["window_cost"] is None
    yesterday_bucket = trend["series"][-2]
    assert yesterday_bucket["tokens"] == 100


def test_dashboard_summary_covers_all_calls_regardless_of_window(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", days=7)
    assert dash["summary"]["total_tokens"] == 600
    assert dash["summary"]["call_count"] == 3
    # summary 与 project_cost 同口径
    assert dash["summary"] == ca.project_cost(session, "DP")


def test_dashboard_by_model_sorted_by_tokens(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP")
    by_model = dash["by_model"]
    assert len(by_model) == 2
    assert [m["tokens"] for m in by_model] == [500, 100]
    assert [m["priced"] for m in by_model] == [False, False]
    assert [m["cost"] for m in by_model] == [None, None]
    assert {(m["provider"], m["model"]) for m in by_model} == {
        ("openai_compatible", "gpt-5"),
        ("anthropic", "claude-x"),
    }
    gpt = next(m for m in by_model if m["model"] == "gpt-5")
    assert gpt["tokens"] == 500
    assert gpt["call_count"] == 2
    assert gpt["is_estimate"] is True


def test_dashboard_by_node_top_and_remainder(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", node_limit=1)
    by_node = dash["by_node"]
    assert len(by_node["top"]) == 1
    assert by_node["top"][0]["node_id"] == "style_draft"
    assert by_node["top"][0]["phase"] == "candidate_generation"
    assert by_node["top"][0]["tokens"] == 500
    assert by_node["remainder"]["node_count"] == 1
    assert by_node["remainder"]["tokens"] == 100


def test_dashboard_by_chapter_rollup(session):
    _dash_seed(session)
    # 未关联章节的项目级调用排最后
    _call(session, None, node_id="outline_expand", tokens=50, chapter_id=None, project_id="DP")
    session.flush()
    dash = ca.project_cost_dashboard(session, "DP")
    by_chapter = dash["by_chapter"]
    assert by_chapter[0]["chapter_id"] == "DCH1"
    assert by_chapter[0]["tokens"] == 500
    assert by_chapter[0]["scene_count"] == 1
    assert by_chapter[-1]["chapter_id"] is None
    assert by_chapter[-1]["tokens"] == 50


def test_dashboard_top_calls_ordered_and_limited(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", call_limit=2)
    top = dash["top_calls"]
    assert len(top) == 2
    assert [row["total_tokens"] for row in top] == [300, 200]
    for row in top:
        assert row["phase"] in {"candidate_generation", "quality_check"}
        assert row["accounting_status"]
        assert (row["priced"], row["cost"], row["currency"]) == (False, None, None)


def test_dashboard_money_for_priced_models_only(session, priced_gpt5):
    """gpt-5 有单价、claude-x 没有：金额只覆盖 gpt-5，claude-x 那一格是「未定价」，排序照样按 token。"""
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", days=7)
    gpt, claude = dash["by_model"]
    assert (gpt["model"], gpt["priced"], gpt["cost"]) == ("gpt-5", True, pytest.approx(_gpt5_cost(300) + _gpt5_cost(200)))
    assert (claude["model"], claude["priced"], claude["cost"]) == ("claude-x", False, None)
    summary = dash["summary"]
    assert summary["total_cost"] == pytest.approx(_gpt5_cost(300) + _gpt5_cost(200))
    assert summary["pricing"]["unpriced_models"] == [
        {"provider": "anthropic", "model": "claude-x", "tokens": 100, "call_count": 1}
    ]
    trend = dash["trend"]
    assert trend["series"][-1]["cost"] == pytest.approx(_gpt5_cost(300))
    assert trend["series"][-2]["cost"] is None  # 昨天只有未定价的 claude-x
    assert trend["window_cost"] == pytest.approx(_gpt5_cost(300))
    top = dash["top_calls"]
    assert top[0]["priced"] is True and top[0]["currency"] == "USD"
    nodes = dash["by_node"]["top"]
    assert [(n["node_id"], n["cost"] is None) for n in nodes] == [("style_draft", False), ("hard_qc", True)]


def test_dashboard_days_clamped_and_bad_input_safe(session):
    _dash_seed(session)
    assert ca.project_cost_dashboard(session, "DP", days=0)["trend"]["days"] == 1
    assert ca.project_cost_dashboard(session, "DP", days=9999)["trend"]["days"] == ca.DASHBOARD_MAX_DAYS
    assert ca.project_cost_dashboard(session, "DP", days="oops")["trend"]["days"] == ca.DASHBOARD_DEFAULT_DAYS


def test_dashboard_empty_project_returns_empty_shapes(session):
    dash = ca.project_cost_dashboard(session, "NOPE")
    assert dash["summary"]["call_count"] == 0
    assert dash["by_model"] == []
    assert dash["by_node"] == {"top": [], "remainder": None}
    assert dash["by_chapter"] == []
    assert dash["top_calls"] == []
    assert len(dash["trend"]["series"]) == ca.DASHBOARD_DEFAULT_DAYS
    assert dash["trend"]["window_tokens"] == 0
    assert dash["trend"]["window_cost"] is None
