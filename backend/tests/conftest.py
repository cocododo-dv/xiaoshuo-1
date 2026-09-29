from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.accounted_llm_fakes import AccountedGenerateMixin

from novel_system.api.app import create_app
from novel_system.cache_registry import reset_all_caches
from novel_system.db.base import Base
from novel_system.db.session import SessionLocal, reset_engine


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """整套 ORM 表结构每个会话只建一次，每个测试拿一份文件副本。

    逐测试 drop_all + create_all（72 张表、113 个索引、每条 DDL 一次 fsync）约 1 s / 测试，
    占本机全量的一半多；文件副本 1–3 ms。模板用一个不带 WAL / 外键钩子的临时 engine 建，
    不经 db.session.engine()，所以不会把引擎状态带进任何测试；每次会话现建、从不入库，
    表结构漂移守卫照旧拿活的 ORM 与 Alembic 对比。
    """
    path = tmp_path_factory.mktemp("schema_template") / "template.db"
    template_engine = sa.create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(bind=template_engine)
    finally:
        template_engine.dispose()
    return path


@pytest.fixture(autouse=True)
def _hermetic_test_process() -> Generator[None, None, None]:
    """每个用例前后各复位一次登记过的进程级缓存（X04-16；为什么要复位见 ``novel_system.cache_registry``）。"""
    reset_all_caches()
    yield
    reset_all_caches()


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    _schema_template: Path,
) -> Generator[None, None, None]:
    is_chroma_integration = request.node.get_closest_marker("chroma_integration") is not None
    if is_chroma_integration and sys.platform == "win32":
        pytest.skip("Chroma integration tests require Linux/WSL; native Windows Chroma is blocked")

    vector_backend = "chroma" if is_chroma_integration else "memory"
    database_path = tmp_path / "test.db"
    shutil.copyfile(_schema_template, database_path)
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("NOVEL_SYSTEM_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("NOVEL_SYSTEM_VECTOR_BACKEND", vector_backend)
    # Path-import tests are isolated to the per-test temporary directory. In
    # production this capability is disabled unless an operator configures one
    # or more roots explicitly.
    import_roots = [str(tmp_path), str(Path(__file__).resolve().parent)]
    local_corpus = os.environ.get("NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS", "").strip()
    if local_corpus:
        import_roots.append(str(Path(local_corpus).expanduser().resolve().parent))
    monkeypatch.setenv(
        "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS",
        os.pathsep.join(import_roots),
    )
    # 场景 token 生命周期预算现在**产品默认关闭**（单作者不预设硬闸门）。整套测试仍以历史
    # 「武装 5×」为基线运行——绝大多数断言都建立在这道闸门存在之上（5×基线、耗尽码、topup
    # 审计等）。解除武装本身由 test_scene_token_budget.py 里显式设 0 的专门用例覆盖。
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "5")
    # A small set of acceptance tests seed review lifecycle fixtures through a
    # hidden maintenance boundary. Production keeps this disabled by default.
    monkeypatch.setenv("NOVEL_SYSTEM_ENABLE_FIXTURE_IMPORT", "true")

    reset_engine()
    yield
    # 关掉本测试的连接池（Windows 上不关掉删不了 tmp 目录里的库文件）。
    reset_engine()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def build_fake_paragraph_classifier():
    """Deterministic LLMClient mock 的类(不经 fixture 也能用:路由测试的导入助手需要它——
    2026-09-15 严格 LLM 后,产品路由的导入 / 重新分类没有 LLM 就 409)。

    返回一个 FakeLLMClient 类(测试调 `FakeLLMClient(rule="dialogue_heavy")` 实例化)。
    `rule` 控制启发式行为,便于覆盖锚定校准 agreement >= 0.85 与 < 0.85 两条路径。
    """
    import json
    import re

    class _FakeLLMResponse:
        def __init__(self, classifications: list[dict]) -> None:
            self.structured_output = {"classifications": classifications}
            self.text = json.dumps(self.structured_output, ensure_ascii=False)
            self.usage: dict = {}
            self.finish_reason = "stop"
            self.request_id = None
            self.provider = "fake"
            self.model = "fake"
            self.raw_response: dict = {}
            self.response_format = "json_object"

    class FakeLLMClient(AccountedGenerateMixin):
        def __init__(self, rule: str = "default") -> None:
            self.rule = rule
            self.call_count = 0
            self.call_log: list[dict] = []

        def generate(self, request):  # noqa: ANN001
            self.call_count += 1
            self.call_log.append(
                {
                    "node_id": getattr(request, "node_id", None),
                    "model": getattr(request, "model", None),
                }
            )
            user_msg = request.messages[-1]["content"]
            paragraphs: list[dict] = []
            # 精确匹配"包含 paragraphs 字段的 JSON 块":在 user_msg 中找
            # 形如 {"paragraphs": [...]} 的子串。task_prompt 模板里可能含其他 `{`,
            # 所以不能用最长贪婪;改为按 "paragraphs" 关键字定位。
            anchor = '"paragraphs"'
            anchor_pos = user_msg.find(anchor)
            if anchor_pos >= 0:
                # 从 anchor 向左找最近的 {
                start = user_msg.rfind("{", 0, anchor_pos)
                if start >= 0:
                    # 平衡括号扫描
                    depth = 0
                    end = -1
                    for i in range(start, len(user_msg)):
                        ch = user_msg[i]
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                end = i + 1
                                break
                    if end > start:
                        try:
                            data = json.loads(user_msg[start:end])
                            paragraphs = data.get("paragraphs", []) or []
                        except json.JSONDecodeError:
                            paragraphs = []
            classifications = [
                {
                    "paragraph_index": p.get("paragraph_index", i),
                    "paragraph_type": self._classify(p.get("text", ""), request, i),
                    "confidence": "high",
                }
                for i, p in enumerate(paragraphs)
            ]
            return _FakeLLMResponse(classifications)

        def _classify(self, text: str, request, idx: int) -> str:
            # rule="disagree_after_anchor":anchor 节点稳定;bulk 节点强制变型,
            # 用于校验 agreement < 0.85 时 fallback 路径。
            if self.rule == "disagree_after_anchor":
                node_id = getattr(request, "node_id", "") or ""
                if node_id.endswith("_bulk"):
                    return "transition"  # 与 anchor 大量不一致
            if any(q in text for q in ('"', "“", "”", "「", "」")):
                return "dialogue"
            if "记得" in text or "想起" in text:
                return "flashback"
            if "想着" in text or "心里" in text:
                return "psychology"
            if len(text) < 30:
                return "transition"
            return "narration"

    return FakeLLMClient


@pytest.fixture
def fake_paragraph_classifier():
    """见 build_fake_paragraph_classifier;fixture 形式供 segmentation / 路由测试注入。"""
    return build_fake_paragraph_classifier()
