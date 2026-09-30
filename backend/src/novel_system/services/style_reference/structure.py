"""Style Reference · 结构画像与规划层指引（2026-09-12 结构跟随，Step 2 Track B）。

规划层（雪花场景清单 / 场景规划、章架构、章内场景规划、场景蓝图）此前完全看不到参考
作者的**结构**：章 / 场多长、多密、怎么开、怎么收、对白占几成，全部按系统自己的模板
来。本模块在合成期从段落表**确定性**算出一张结构画像（无 LLM），随
``runtime_contract._FROZEN_PROFILE_JSON_KEYS`` 冻结进契约，规划节点再渲染成中文块：

- ``compute_structure_card(paragraphs, voice_signature=...)`` → ``profile_json["structure_card"]``
  用 ``book_text.is_title_paragraph`` 按标题段切章；每章字数 / 段数 / 对白段
  占比 / 开章段型与首段摘录 / 收章段型与末段摘录；全书章数、章长分位、每章段数、段型比重、
  开章 / 收章段型分布、人称（取自 voice_signature）、章首 / 章尾样例（≤3 × ≤150 字，跨全书
  首 / 中 / 末取样）。无章标记 → 全书一章、``has_chapter_markers=False``。
- ``render_structure_card_parts(profile_json)`` → （``[结构画像]`` 块（≤1,500 字、**带数字**——规划层
  需要尺度）, 「章首样例」「章尾样例」「章题样例」块（原文，过 ``secure_reference_block``））。
- ``derive_planning_guidance(findings, ...)`` → ``profile_json["planning_guidance"]``：
  scene.* / theme.* 的 observation 陈述（≤10 行、跨子维度轮转、原文重合过滤）；
  ``render_planning_guidance(profile_json)`` → ``[场景手法]`` 块。

旧画像没有这两个键时两个渲染器都返回 ``""``，调用方据此不注入（优雅退化）。

（2026-09-30 拆成三块：书的正文规则 ``book_text``、结构画像的计算 ``structure_card``、渲染与规划层指引
``structure_render``；这里照旧转出原来的名字，调用方与测试不用改。）
"""

from __future__ import annotations

from novel_system.services.style_reference.book_text import (
    FRONT_MATTER_TAIL_MAX_CHARS,
    FRONT_MATTER_TAIL_MAX_ROWS,
    BookChapter,
    non_body_kind,
    split_book_chapters,
    title_name_part,
)
from novel_system.services.style_reference.structure_card import (
    REFERENCE_SCENE_CHARS_CEILING,
    REFERENCE_SCENE_CHARS_FLOOR,
    STRUCTURE_CARD_VERSION,
    STRUCTURE_SAMPLE_MAX_CHARS,
    STRUCTURE_SAMPLES_PER_SIDE,
    STRUCTURE_TITLE_MAX_CHARS,
    STRUCTURE_TITLE_SAMPLES,
    chapter_titles_summary,
    compute_structure_card,
    reference_scene_scale,
)
from novel_system.services.style_reference.structure_render import (
    PLANNING_GUIDANCE_HEADER,
    PLANNING_GUIDANCE_MAX_LINES,
    STRUCTURE_CARD_HEADER,
    STRUCTURE_CARD_MAX_CHARS,
    STRUCTURE_SAMPLES_KIND,
    STRUCTURE_SAMPLES_PREAMBLE,
    chapter_boundary_habits,
    derive_planning_guidance,
    render_planning_guidance,
    render_structure_card_parts,
)

__all__ = [
    "BookChapter",
    "FRONT_MATTER_TAIL_MAX_CHARS",
    "FRONT_MATTER_TAIL_MAX_ROWS",
    "PLANNING_GUIDANCE_HEADER",
    "PLANNING_GUIDANCE_MAX_LINES",
    "REFERENCE_SCENE_CHARS_CEILING",
    "REFERENCE_SCENE_CHARS_FLOOR",
    "STRUCTURE_CARD_HEADER",
    "STRUCTURE_CARD_MAX_CHARS",
    "STRUCTURE_CARD_VERSION",
    "STRUCTURE_SAMPLES_KIND",
    "STRUCTURE_SAMPLES_PREAMBLE",
    "STRUCTURE_SAMPLE_MAX_CHARS",
    "STRUCTURE_SAMPLES_PER_SIDE",
    "STRUCTURE_TITLE_MAX_CHARS",
    "STRUCTURE_TITLE_SAMPLES",
    "chapter_boundary_habits",
    "chapter_titles_summary",
    "compute_structure_card",
    "derive_planning_guidance",
    "non_body_kind",
    "reference_scene_scale",
    "render_planning_guidance",
    "render_structure_card_parts",
    "split_book_chapters",
    "title_name_part",
]
