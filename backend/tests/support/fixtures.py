"""具名夹具（conftest 用 ``pytest_plugins`` 载入）：测试文件按需启用，例如
``pytestmark = pytest.mark.usefixtures("online_pipeline")``——以前每个文件各写一个一行的 autouse 包装。

- ``online_pipeline``：假生成已退役，场景管线各模块没显式注入 client 的默认 LLMNodeRunner 换成在线记账替身
  （``tests/real_llm_fakes.install_online_pipeline``）。
- ``skeleton_snowflake`` / ``skeleton_snowflake_llm_on``：雪花 ``generate_step`` 换成骨架直通（不调模型、按大纲
  出各步，见 ``tests/snowflake_skeleton.py``）；后者同时打开 ``NOVEL_SYSTEM_LLM_ENABLED`` 过路由闸。
- ``online_author_pipeline``：作者稿 AI 建议 / 结构反提取的默认运行器换成在线记账替身。
- ``online_orchestrator_runner``：只换编排器自己的默认运行器（场景运行检查点、整链冒烟那几组）。
- ``style_workers``：登记风格参考作业的处理器（不经应用直接跑作业的用例）。
"""

from __future__ import annotations

import pytest

from novel_system.services.llm_task_runner import LLMNodeRunner
from tests.real_llm_fakes import (
    ScenePipelineOnlineFake,
    install_online_author_pipeline,
    install_online_pipeline,
    install_skeleton_snowflake,
)


@pytest.fixture
def online_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    install_online_pipeline(monkeypatch)


@pytest.fixture
def skeleton_snowflake(monkeypatch: pytest.MonkeyPatch) -> None:
    install_skeleton_snowflake(monkeypatch)


@pytest.fixture
def skeleton_snowflake_llm_on(monkeypatch: pytest.MonkeyPatch) -> None:
    install_skeleton_snowflake(monkeypatch, llm_enabled=True)


@pytest.fixture
def online_author_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    install_online_author_pipeline(monkeypatch)


@pytest.fixture
def online_orchestrator_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """只换编排器自己按 ``LLMNodeRunner(session)`` 取的默认运行器（其余子服务各自注入替身的用例用它）。"""
    monkeypatch.setattr(
        "novel_system.services.orchestrator.LLMNodeRunner",
        lambda session: LLMNodeRunner(session, llm_client=ScenePipelineOnlineFake()),
    )


@pytest.fixture
def style_workers() -> None:
    """登记风格参考作业的处理器（lifespan 会调 ``install_workers()``；不经应用、直接跑作业的用例自己登记一次）。"""
    from novel_system.services.style_reference.workers import install_workers

    install_workers()
