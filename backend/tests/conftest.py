from __future__ import annotations

import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.accounted_llm_fakes import AccountedGenerateMixin

from novel_system.api.app import create_app
from novel_system.db.base import Base
from novel_system.db.session import SessionLocal, engine


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    is_chroma_integration = request.node.get_closest_marker("chroma_integration") is not None
    if is_chroma_integration and sys.platform == "win32":
        pytest.skip("Chroma integration tests require Linux/WSL; native Windows Chroma is blocked")

    vector_backend = "chroma" if is_chroma_integration else "memory"
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
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
    from novel_system.db.session import reset_engine

    reset_engine()
    Base.metadata.drop_all(bind=engine())
    Base.metadata.create_all(bind=engine())
    yield
    Base.metadata.drop_all(bind=engine())


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
