"""LLM 记账测试共用：记账模块、标准请求、作品级 / 场景级调用上下文、场景归属链、已确立的场景预算、退役额度变量表。"""

from __future__ import annotations

import importlib
from typing import Any

from sqlalchemy import func, select

from novel_system.db.models import ChapterGoal, SceneCard, SceneRunState, StoryProject
from novel_system.env_config import DEFAULT_PROVIDER_ATTEMPT_BUDGET, PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY
from novel_system.services.llm_client import LLMRequest


# ---------------------------------------------------------------- 一次记账调用的请求 / 上下文与场景归属链（test_llm_accounting 拆出的四个文件共用）


def accounting_module():
    return importlib.import_module("novel_system.services.llm_accounting")


def accounting_request(*, max_output_tokens: int = 64) -> LLMRequest:
    return LLMRequest(
        model="test-model",
        messages=[
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "写一个短场景。"},
        ],
        temperature=0,
        max_output_tokens=max_output_tokens,
        response_format="json_object",
        provider="openai_compatible",
        node_id="neutral_draft",
    )


def project_call_context(accounting):
    return accounting.LLMCallContext(
        scope_type="project",
        scope_id="project-1",
        node_id="neutral_draft",
        step="draft",
        project_id="project-1",
        execution_id="execution-1",
        execution_step_key="neutral_draft",
    )


def scene_call_context(accounting, scene_id: str):
    return accounting.LLMCallContext(
        scope_type="scene",
        scope_id=scene_id,
        node_id="neutral_draft",
        step="draft",
        project_id="project-1",
        chapter_id="chapter-1",
        scene_id=scene_id,
        execution_id="execution-1",
        execution_step_key="neutral_draft",
    )


def seed_scene_parent(session, scene_id: str) -> None:
    """Seed the project/chapter/scene authority chain for scene accounting."""
    if session.get(StoryProject, "project-1") is None:
        session.add(
            StoryProject(
                project_id="project-1",
                title="LLM accounting integration",
                outline_text="Test-owned outline",
            )
        )
        session.flush()
    if session.get(ChapterGoal, "chapter-1") is None:
        session.add(
            ChapterGoal(
                chapter_id="chapter-1",
                project_id="project-1",
                planned_scene_count=1,
                chapter_goal="Exercise scene accounting",
            )
        )
        session.flush()
    if session.get(SceneCard, scene_id) is None:
        next_scene_seq = int(
            session.scalar(
                select(func.max(SceneCard.scene_seq)).where(
                    SceneCard.chapter_id == "chapter-1"
                )
            )
            or 0
        ) + 1
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id="chapter-1",
                project_id="project-1",
                scene_seq=next_scene_seq,
                scene_goal="Exercise one accounted provider call",
                onstage_chars_json=[],
                beats_json=[],
            )
        )
        session.flush()


def armed_scene_budget(
    scene_token_budget: int,
    *,
    attempt_budget: int = 4,
    provider_attempt_budget: int = DEFAULT_PROVIDER_ATTEMPT_BUDGET,
) -> dict[str, Any]:
    """一场已经确立（武装）预算的场景的四个预算列：三道额度 + 不可变依据，依据与
    ``scene_budget.ensure_scene_budget_initialized`` 武装时写的同形（单发基线按倍率 5 记成预算 ÷ 5）。

    「有预算、没依据」是依据出现之前的旧形状，2026-10-04 起按坏状态 fail-closed（以前就地补写一份 legacy
    依据）——夹具要一场有预算的场景就用这一份，不再只设 ``scene_token_budget``。
    """
    return {
        "scene_token_budget": scene_token_budget,
        "attempt_budget": attempt_budget,
        "provider_attempt_budget": provider_attempt_budget,
        "scene_budget_basis_json": {
            "baseline_tokens": max(1, scene_token_budget // 5),
            "budget_multiplier": 5,
            "scene_token_budget": scene_token_budget,
            "scene_budget_armed": True,
            "topup_audit_cutoff_operation_id": 0,
            "provider_attempt_budget": {
                "config_key": PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY,
                "value": provider_attempt_budget,
            },
            "attempt_budget": {"source": "scene_run_states.initial", "value": attempt_budget},
        },
    }


def arm_scene_budget(state: SceneRunState, scene_token_budget: int, **dimensions: int) -> None:
    """把一行还没确立预算的 ``SceneRunState`` 设成 :func:`armed_scene_budget` 的样子。"""
    for column, value in armed_scene_budget(scene_token_budget, **dimensions).items():
        setattr(state, column, value)


def scene_run_state(session, *, scene_id: str, **kwargs) -> SceneRunState:
    """场景归属链 + 一行 ``SceneRunState``。给了 ``scene_token_budget`` 而没给依据时，按
    :func:`armed_scene_budget` 补上同额度的不可变依据（``attempt_budget`` / ``provider_attempt_budget`` 照传入的记）。"""
    seed_scene_parent(session, scene_id)
    if kwargs.get("scene_token_budget") is not None and "scene_budget_basis_json" not in kwargs:
        dimensions = {
            key: kwargs[key] for key in ("attempt_budget", "provider_attempt_budget") if key in kwargs
        }
        kwargs = {**kwargs, **armed_scene_budget(kwargs["scene_token_budget"], **dimensions)}
    return SceneRunState(scene_id=scene_id, **kwargs)


# 2026-09-30 重评 R3(批准#3a):六道只能靠环境变量打开的全局额度闸与环境变量成本单价删了。这些变量还设着的
# 安装:什么都不拦、启动时记一条警告;成本看板照旧有「全局用量」读数(没有上限可比)。
RETIRED_QUOTA_ENV = {
    "NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT": "1",
    "NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT": "1",
    "NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT": "1",
    "NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT": "1",
    "NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS": "1",
    # 以前只设金额上限不设单价会让 get_settings() 直接报错、后端起不来
    "NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD": "0.000001",
    "NOVEL_SYSTEM_LLM_INPUT_COST_PER_MILLION_USD": "not-a-number",
    "NOVEL_SYSTEM_LLM_OUTPUT_COST_PER_MILLION_USD": "-5",
}
