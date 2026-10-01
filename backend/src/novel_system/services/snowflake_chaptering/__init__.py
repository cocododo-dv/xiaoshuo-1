"""构思侧分章：09 场景列表 → 章节结构（Phase 2 → 阶段 K / V / W / X / Y）。

历史上「整理成章节结构」没有任何一方真正在分章：09/10 步没有章字段，提示词让
LLM 把 chapter_id 留空说「server assigns」，而服务端的起始值就是 ``{project_id}_CH01``
且把作者传入的 chapter_id 当系统身份丢弃 —— 于是全书落进一章。

这个包把分章变成一次**可预览、可调整、可确认**的显式动作：
- ``preview`` 是只读推演（分章方案不写库），每种策略都确定性可复算；
- ``save`` 把作者在面板里确认的归属落到 ``SnowflakeScenePlan.chapter_plan_id`` / ``chapter_id``；
- 物化读章表分组（``outline_plan``），章标题/章目标来自章表，而不是章 id 字符串。

2026-09-18（阶段 V）起的纪律——对应一次真实的「整理出来乱七八糟」：
- **章是故事序上连续的一段。** 故事序只有一个来源：09 场景列表草稿的行序（``snowflake_scene_order``）；
  ``scene_seq`` 只表示章内位置、只由 ``renumber_scene_seq`` 写。这里读场景永远按故事序，
  ``save`` 不看载荷里的章内先后。
- **章在场景之后**（阶段 K）：``from_scenes`` 策略按场景列表提议章表——三个灾难各自收束一章，每章场数的
  来历写在回包的 ``scale`` 里。它和别的策略一样是预览；确认时 ``save(replace_chapters=true)`` 才建章
  （``new:N`` → 真 row_uid）、软删没列出来的旧章、镜像回 07。``auto`` 由服务端按现状挑策略。
- 灾难标记既认场上的 ``spine`` 列，也认功能标签里的「灾难一 / 二 / 三」（``spine_from_role``）。

2026-09-30 拆成包（B07-01）：门面 ``service``；``derive`` / ``preview`` / ``warnings`` / ``save`` / ``orphans`` /
``llm`` / ``outline_plan`` / ``outline_sync``；纯算法 ``algorithms`` / ``contiguity`` / ``spine`` / ``rhythm``；尺度与
参考书 ``scale``。章表行的读写在包外的叶子 ``snowflake_chapter_table``（目录服务也引它——包里的任何模块一被导入
就会先跑这个文件，而这里引着的服务又经回收站 / 作品服务绕回目录）；AI 调用在包外的 ``snowflake_chapter_llm``。
包内模块彼此按子模块导入，从不经这个文件。这里只把原来从 ``snowflake_chaptering`` 导入的名字原样转出。
"""

from __future__ import annotations

from novel_system.services.snowflake_chapter_table import (
    NEW_CHAPTER_PREFIX,
    SPINE_MARKS,
    chapter_target_id,
    coerce_act as _coerce_act,
    is_auto_chapter_title,
    is_placeholder_chapter,
    mint_chapter_row_uid,
    mirror_chapters_into_long_synopsis,
    parse_outline_chapters,
)
from novel_system.services.snowflake_chaptering.algorithms import (
    STRATEGIES,
    hinge_min_chapters,
    match_chunks_to_chapters,
    propose_chapter_chunks,
)
from novel_system.services.snowflake_chaptering.contiguity import heal_assignment, misplaced_scene_plan_ids
from novel_system.services.snowflake_chaptering.outline_plan import build_chaptered_outline_plan, protagonist_hint
from novel_system.services.snowflake_chaptering.outline_sync import sync_long_synopsis_chapters
from novel_system.services.snowflake_chaptering.rhythm import rhythm_report
from novel_system.services.snowflake_chaptering.scale import (
    reference_chapter_scale_hint as _reference_chapter_scale_hint,
    reference_chapter_titles as _reference_chapter_titles,
)
from novel_system.services.snowflake_chaptering.service import SnowflakeChapteringService
from novel_system.services.snowflake_chaptering.spine import scene_spine, spine_from_role, spine_positions

__all__ = [
    "NEW_CHAPTER_PREFIX",
    "SPINE_MARKS",
    "STRATEGIES",
    "SnowflakeChapteringService",
    "_coerce_act",
    "_reference_chapter_scale_hint",
    "_reference_chapter_titles",
    "build_chaptered_outline_plan",
    "chapter_target_id",
    "heal_assignment",
    "hinge_min_chapters",
    "is_auto_chapter_title",
    "is_placeholder_chapter",
    "match_chunks_to_chapters",
    "mint_chapter_row_uid",
    "mirror_chapters_into_long_synopsis",
    "misplaced_scene_plan_ids",
    "parse_outline_chapters",
    "propose_chapter_chunks",
    "protagonist_hint",
    "rhythm_report",
    "scene_spine",
    "spine_from_role",
    "spine_positions",
    "sync_long_synopsis_chapters",
]
