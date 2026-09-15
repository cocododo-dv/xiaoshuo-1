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


@pytest.fixture
def fake_extractor_llm():
    """PR-3 extractor 测试 LLMClient mock。

    按 request.node_id 分发:
    - style_ref_extract_language / _narrative → {observations:[], forbidden_patterns:[]}
    - style_ref_supplement_evidence → {additional_evidence:[]}

    rule:
    - default:每次返回 3 obs + 1 forbid,每条带 2 evidence(快路径,首次过)
    - evidence_short:obs 只带 1 evidence,触发重试;supplement 时补 1 条
    - all_banned:obs.statement 全部命中"文笔优美"(BannedAdjective)
    - fail_then_pass:第一次 obs 只带 1 evidence,supplement_evidence 调用返回 1 条
      新 evidence(第一级重试通过)
    - empty_then_default:每 sub_dim 第一次返回合法空数组(弱模型"产出薄"),
      第二次(空结果 full_retry)返回 default 内容
    - always_empty:抽取一律返回空数组(验证空结果只重抽一次、不死循环)
    """
    import json
    import re

    class _FakeLLMResponse:
        def __init__(self, structured: dict) -> None:
            self.structured_output = structured
            self.text = json.dumps(structured, ensure_ascii=False)
            self.usage: dict = {}
            self.finish_reason = "stop"
            self.request_id = None
            self.provider = "fake"
            self.model = "fake"
            self.raw_response: dict = {}
            self.response_format = "json_object"

    def _extract_payload(user_msg: str) -> dict:
        # 共享 helper 约定:payload JSON 位于唯一显式不可信数据边界内。
        opening = re.search(r"\[UNTRUSTED_REFERENCE_DATA:[^\]]+\]\n", user_msg)
        if opening is None:
            return {}
        closing = user_msg.find("\n[/UNTRUSTED_REFERENCE_DATA]", opening.end())
        if closing < 0:
            return {}
        try:
            return json.loads(user_msg[opening.end():closing])
        except json.JSONDecodeError:
            return {}

    def _two_evidence_for(p: dict, *, n: int = 2) -> list[dict]:
        text = p.get("text", "") or "ph_quote"
        if n == 1:
            spans = [(0, min(20, len(text)))]
        else:
            split = max(1, len(text) // 2)
            spans = [(0, split), (split, len(text))]
            spans.extend((0, min(20, len(text))) for _ in range(max(0, n - 2)))
        return [
            {
                "paragraph_id": p.get("paragraph_id"),
                "span": [start, end],
                "quote": text[start:end],
                "illustrates_dims": [],
                "anchor_kind": "paragraph_quote",
            }
            for start, end in spans
            if end > start
        ]

    class FakeExtractorLLM(AccountedGenerateMixin):
        def __init__(self, rule: str = "default") -> None:
            self.rule = rule
            self.call_count = 0
            self.call_log: list[dict] = []
            self._supplement_call = 0
            self._extract_calls: dict = {}

        def generate(self, request):  # noqa: ANN001
            self.call_count += 1
            node_id = getattr(request, "node_id", None) or ""
            self.call_log.append({"node_id": node_id, "model": getattr(request, "model", None)})
            user_msg = request.messages[-1]["content"]
            payload = _extract_payload(user_msg)
            paragraphs = payload.get("paragraphs") or []
            sub_dim = payload.get("sub_dimension", "language.rhetoric")

            if node_id == "style_ref_supplement_evidence":
                self._supplement_call += 1
                # 返回 1 条新 evidence(使 finding 凑齐 ≥2)
                if paragraphs:
                    p = paragraphs[0]
                    return _FakeLLMResponse(
                        {
                            "additional_evidence": [
                                {
                                    "paragraph_id": p.get("paragraph_id"),
                                    "span": [0, min(15, len(p.get("text", "")))],
                                    "quote": (p.get("text", "") or "supp_quote")[:15],
                                    "anchor_kind": "paragraph_quote",
                                    "illustrates_dims": [],
                                }
                            ]
                        }
                    )
                return _FakeLLMResponse({"additional_evidence": []})

            # extract_language / extract_narrative
            if not paragraphs:
                return _FakeLLMResponse({"observations": [], "forbidden_patterns": []})

            if self.rule == "always_empty":
                return _FakeLLMResponse({"observations": [], "forbidden_patterns": []})
            if self.rule == "empty_then_default":
                self._extract_calls[sub_dim] = self._extract_calls.get(sub_dim, 0) + 1
                if self._extract_calls[sub_dim] == 1:
                    return _FakeLLMResponse(
                        {"observations": [], "forbidden_patterns": []}
                    )
                # 第二次(空结果 full_retry)落到 default 分支返回正常内容
            if self.rule == "many_invalid_then_default":
                self._extract_calls[sub_dim] = self._extract_calls.get(sub_dim, 0) + 1
                if self._extract_calls[sub_dim] == 1:
                    return _FakeLLMResponse(
                        {
                            "observations": [
                                {
                                    "statement": f"{sub_dim} invalid batch #{i}",
                                    "confidence": "medium",
                                    "finding_kind": "observation",
                                    "sub_dimension": sub_dim,
                                    "evidence": [
                                        {
                                            "paragraph_id": paragraphs[0].get("paragraph_id"),
                                            "span": [0, 0],
                                            "quote": f"不存在的拼接引文甲{i}",
                                            "anchor_kind": "paragraph_quote",
                                        },
                                        {
                                            "paragraph_id": paragraphs[0].get("paragraph_id"),
                                            "span": [0, 0],
                                            "quote": f"不存在的拼接引文乙{i}",
                                            "anchor_kind": "paragraph_quote",
                                        },
                                    ],
                                }
                                for i in range(3)
                            ],
                            "forbidden_patterns": [],
                        }
                    )
                # 大面积证据失败应整维重抽；第二次落到 default 分支。

            observations: list[dict] = []
            forbidden: list[dict] = []
            if self.rule == "all_banned":
                # statement 全部命中
                observations = [
                    {
                        "statement": "文笔优美,情感真挚",
                        "confidence": "high",
                        "finding_kind": "observation",
                        "sub_dimension": sub_dim,
                        "evidence": _two_evidence_for(paragraphs[0]),
                    }
                ]
            elif self.rule in ("evidence_short", "fail_then_pass"):
                # 仅 1 evidence
                observations = [
                    {
                        "statement": f"{sub_dim} obs short A",
                        "confidence": "medium",
                        "finding_kind": "observation",
                        "sub_dimension": sub_dim,
                        "evidence": _two_evidence_for(paragraphs[0], n=1),
                    }
                ]
            else:
                # default: 3 obs + 1 forbid,各 2 evidence
                for i, p in enumerate(paragraphs[:3]):
                    observations.append(
                        {
                            "statement": f"{sub_dim} observation #{i}",
                            "confidence": "high",
                            "finding_kind": "observation",
                            "sub_dimension": sub_dim,
                            "evidence": _two_evidence_for(p),
                        }
                    )
                if paragraphs:
                    forbidden.append(
                        {
                            "statement": f"{sub_dim} forbidden pattern #0",
                            "confidence": "medium",
                            "finding_kind": "forbidden_pattern",
                            "sub_dimension": sub_dim,
                            "evidence": _two_evidence_for(paragraphs[0]),
                        }
                    )
            return _FakeLLMResponse(
                {"observations": observations, "forbidden_patterns": forbidden}
            )

    return FakeExtractorLLM


@pytest.fixture
def fake_validation_llm():
    """PR-7 ValidationOrchestrator 测试 LLMClient mock。

    按 request.node_id 分发:
    - style_ref_validate_semantic → {dimension_scores: [...]};rule 控制是否含 quote
    - style_ref_validate_forbidden → {triggered, excerpt, reasoning}

    rule:
    - with_quote(默认):semantic explanation 含「...」,forbidden never trigger
    - no_quote:semantic explanation 不含 quote → score 应被截至 4
    - always_trigger:forbidden 永远 triggered=True
    - never_trigger:forbidden 永远 triggered=False
    - fail:LLM 一律 raise(测试降级)
    """
    import json as _json

    class _Resp:
        def __init__(self, structured: dict) -> None:
            self.structured_output = structured
            self.text = _json.dumps(structured, ensure_ascii=False)
            self.usage: dict = {}
            self.finish_reason = "stop"
            self.provider = "fake"
            self.model = "fake"
            self.response_format = "json_object"
            self.request_id = None
            self.raw_response: dict = {}

    class FakeValidationLLM(AccountedGenerateMixin):
        def __init__(self, rule: str = "with_quote") -> None:
            self.rule = rule
            self.call_count = 0
            self.call_log: list[dict] = []

        def generate(self, request):  # noqa: ANN001
            self.call_count += 1
            node_id = getattr(request, "node_id", "") or ""
            self.call_log.append(
                {"node_id": node_id, "model": getattr(request, "model", None)}
            )
            if self.rule == "fail":
                raise RuntimeError("forced LLM failure")

            if node_id == "style_ref_validate_semantic":
                if self.rule == "no_quote":
                    return _Resp(
                        {
                            "dimension_scores": [
                                {
                                    "dimension": "rhythm",
                                    "score": 8.5,
                                    "explanation": "节奏整体连贯流畅(无 quote)",
                                }
                            ]
                        }
                    )
                return _Resp(
                    {
                        "dimension_scores": [
                            {
                                "dimension": "rhythm",
                                "score": 7.5,
                                "explanation": "短句节奏,如「他低头看着脚下」",
                            },
                            {
                                "dimension": "tone",
                                "score": 6.5,
                                "explanation": "克制基调,「雪从天上飘下来」",
                            },
                        ]
                    }
                )
            if node_id == "style_ref_validate_forbidden":
                if self.rule == "always_trigger":
                    return _Resp(
                        {
                            "triggered": True,
                            "excerpt": "命中节选",
                            "reasoning": "match",
                        }
                    )
                return _Resp(
                    {"triggered": False, "excerpt": "", "reasoning": "no match"}
                )
            # 兜底
            return _Resp({})

    return FakeValidationLLM
