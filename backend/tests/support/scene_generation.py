"""场景生成测试共用的脚手架（X04-19：test_scene_generation 按 services/scene_generation/ 的子模块拆开时搬出来的，
几份拆出来的测试文件都要）。

- ``seed_generation_scene``：一场待起草的场景（必含「A red envelope changes hands.」，默认短篇幅）；
- ``ThreeStepSceneClient``：按调用次序回中性稿 → 风格稿 → 补丁稿的记账替身；
- ``STYLE_SCENE_TEXT`` / ``PATCHED_SCENE_TEXT``：过得了准终稿房风场景机制门的合成正文（``strict_qc`` 的替身也用它们）。
"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, ChapterState, SceneCard, SceneRunState, StoryProject
from novel_system.services.llm_client import LLMRequest, LLMResponse
from tests.accounted_llm_fakes import AccountedGenerateMixin


# 合成场景：有「选」、有代价、结尾有动作——过得了准终稿的房风场景机制门（B03-16b：以前产品代码里有一条认
# 「Provider-generated」字头的旁路替测试跳过这道门，现在删了）
STYLE_SCENE_TEXT = "Provider-generated style scene text. She has to choose, and the cost is the ledger. She turns and leaves."
PATCHED_SCENE_TEXT = "Provider-generated patched scene text. She has to choose, and the cost is the ledger. She turns and leaves."


class ThreeStepSceneClient(AccountedGenerateMixin):
    """按调用次序回中性稿 → 风格稿 → 补丁稿（第三次起都是补丁稿），每一步有自己的回包 id、模型与用量。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            structured_output = {
                "scene_text": "Provider-generated neutral scene text.",
                "continuity_notes": ["kept the reunion tense"],
            }
            request_id = "resp_fake_neutral_001"
            model = "fake-neutral-model"
            usage = {"input_tokens": 111, "output_tokens": 29, "total_tokens": 140}
        elif len(self.requests) == 2:
            structured_output = {
                "scene_text": STYLE_SCENE_TEXT,
                "style_notes": ["leaned harder into rhythm and inner tension"],
            }
            request_id = "resp_fake_style_001"
            model = "fake-style-model"
            usage = {"input_tokens": 121, "output_tokens": 33, "total_tokens": 154}
        else:
            structured_output = {
                "scene_text": PATCHED_SCENE_TEXT,
                "style_notes": ["applied one controlled patch pass"],
            }
            request_id = "resp_fake_patch_001"
            model = "fake-patch-model"
            usage = {"input_tokens": 131, "output_tokens": 37, "total_tokens": 168}
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=__import__("json").dumps(structured_output),
            structured_output=structured_output,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": model,
                "usage": usage,
                "finish_reason": "stop",
            },
            usage=usage,
            finish_reason="stop",
        )


def seed_generation_scene(
    session,
    *,
    must_include_text: str | None = "A red envelope changes hands.",
    target_length_band: str = "short",
) -> None:
    """一部作品、一章、一场待起草的场景 ``CH100_SC01``（视角 CHAR_A、在场 A / B、钟楼屋顶）。"""
    session.add(StoryProject(project_id="PROJECT100", title="Scene generation", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="CH100",
            project_id="PROJECT100",
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(ChapterState(chapter_id="CH100", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="CH100_SC01",
            project_id="PROJECT100",
            chapter_id="CH100",
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text=must_include_text,
            target_length_band=target_length_band,
            scene_type="reunion",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id="CH100_SC01", scene_status="ready"))
    session.commit()
