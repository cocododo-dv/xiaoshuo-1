"""测试用的 LLM 替身：节点运行器与客户端。

记账：替身客户端一律经 ``tests/accounted_llm_fakes.AccountedGenerateMixin`` 走记账钩子（裸 ``generate`` 会绕过账本）。
"""

from __future__ import annotations

from types import SimpleNamespace


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
