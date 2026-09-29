"""bundle 段落的登记（B03-19）：``source_version_refs`` / ``ordered_injections`` / ``inline_digests`` 三张表按登记
顺序一起写。

BSHASH_v1 的哈希依赖注入段的顺序；存库的冻结快照（JSON）依赖两个字典的键序——登记顺序就是快照的字节顺序。
一个注入段 = 先记它的来源版本引用，再排进注入顺序，最后放正文（``add``）；只进正文、不排注入顺序的段（叙事状态、
信息差、章间过渡、相似场景）与内部信号（以 ``_`` 开头，不渲染成段落）用 ``digest``。

bundle 能写的正文键在这里一处声明：进提示词的业务段必须在 ``context_budget.SECTION_SPECS`` 里有渲染位
（``tests/test_prompt_assembly_e2e.py`` 对着这张表查），没声明的键写不进来。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novel_system.services.scene_design_context import SCENE_DESIGN_SECTION_KEY
from novel_system.services.scene_structure_brief import SCENE_STRUCTURE_SECTION_KEY
from novel_system.services.style_prompt_injection import SCENE_SITUATION_TAGS_KEY
from novel_system.services.style_reference.narrative_guidance import NARRATIVE_GUIDANCE_SECTION_KEY

# 进提示词的业务段（渲染位见 context_budget.SECTION_SPECS）
BUNDLE_SECTION_DIGEST_KEYS: frozenset[str] = frozenset(
    {
        "chapter_goal",
        "scene_card",
        SCENE_STRUCTURE_SECTION_KEY,
        SCENE_DESIGN_SECTION_KEY,
        NARRATIVE_GUIDANCE_SECTION_KEY,
        "author_instruction",
        "chapter_writer_brief",
        "scene_writer_brief",
        "scene_blueprint",
        "character_pressure",
        "chapter_story_architecture",
        "voice_card",
        "relation_card",
        "character_contract",
        "narrative_state",
        "information_asymmetry",
        "chapter_transition_buffer",
        "previous_scene_voice_anchor",
        "similar_scene",
        "scene_memory",
        "literary_freshness_budget",
        "author_preference_profile",
        "scene_summary",
        "chapter_summary",
        "volume_summary",
    }
)
# 内部信号：随 bundle 冻结、不渲染成段落
BUNDLE_SIGNAL_DIGEST_KEYS: frozenset[str] = frozenset(
    {
        "_style_reference_scene_scale",
        "_style_reference_runtime_contract",
        SCENE_SITUATION_TAGS_KEY,
    }
)


class BundleSections:
    """按登记顺序写 bundle 的三张表。"""

    def __init__(self) -> None:
        self.source_version_refs: dict[str, Any] = {}
        self.ordered_injections: list[dict[str, Any]] = []
        self.inline_digests: dict[str, Any] = {}

    def ref(self, key: str, value: Any) -> None:
        """记一条来源版本引用（进哈希；已有的键原地更新，键序不变）。"""
        self.source_version_refs[key] = value

    def add(
        self,
        slot: str,
        *,
        ref_id: Any,
        text: Any,
        digest_key: str | None = None,
        refs: Mapping[str, Any] | None = None,
    ) -> None:
        """一个注入段：来源版本引用 → 注入顺序 → 正文（``digest_key`` 缺省与 ``slot`` 同名）。"""
        key = digest_key or slot
        _require_declared(key)
        for ref_key, value in (refs or {}).items():
            self.source_version_refs[ref_key] = value
        self.ordered_injections.append({"slot": slot, "ref_id": ref_id, "digest_key": key})
        self.inline_digests[key] = text

    def digest(self, key: str, text: Any) -> None:
        """只放正文、不排注入顺序的段与内部信号。"""
        _require_declared(key)
        self.inline_digests[key] = text


def _require_declared(key: str) -> None:
    if key not in BUNDLE_SECTION_DIGEST_KEYS and key not in BUNDLE_SIGNAL_DIGEST_KEYS:
        raise ValueError(f"bundle digest key {key!r} is not declared in bundle_sections")
