"""测试用的 LLM 替身：节点运行器与客户端。

记账：替身客户端一律经 ``tests/accounted_llm_fakes.AccountedGenerateMixin`` 走记账钩子（裸 ``generate`` 会绕过账本）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.accounted_llm_fakes import AccountedGenerateMixin


# ---------------------------------------------------------------- 按步回正文的节点运行器（test_style_first_draft）


class ScriptedStepRunner:
    """LLMNodeRunner 替身：按 ``step`` 回 ``{"scene_text": …}``（没配的步回 ``default``），记下每次调用的参数。"""

    def __init__(self, outputs: dict[str, str], default: str) -> None:
        self.outputs = outputs
        self.default = default
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        text = self.outputs.get(str(kwargs.get("step")), self.default)
        return SimpleNamespace(
            llm_call_id=f"llm_call_sfd_{len(self.calls)}",
            response=SimpleNamespace(structured_output={"scene_text": text}),
        )


# ---------------------------------------------------------------- 风格参考的段落分类替身（conftest 的 fake_paragraph_classifier 夹具）


class _FakeClassifierResponse:
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


class FakeParagraphClassifier(AccountedGenerateMixin):
    """确定性的段落分类替身：读请求里 ``{"paragraphs": [...]}`` 那段 JSON，按启发式给每段一个类型。

    ``rule`` 控制启发式行为，便于覆盖锚定校准 agreement >= 0.85 与 < 0.85 两条路径。
    """

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
        return _FakeClassifierResponse(classifications)

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


def build_fake_paragraph_classifier() -> type[FakeParagraphClassifier]:
    """段落分类替身的类（不经夹具也能用：路由测试的导入助手需要它——2026-09-15 严格 LLM 后，产品路由的导入 /
    重新分类没有 LLM 就 409）。测试调 ``build_fake_paragraph_classifier()(rule="dialogue_heavy")`` 实例化。

    每次给一个新的子类（与搬家前每次现建一个类一样），测试往类上打的补丁不会漏给别的用例。
    """
    return type("FakeLLMClient", (FakeParagraphClassifier,), {})
