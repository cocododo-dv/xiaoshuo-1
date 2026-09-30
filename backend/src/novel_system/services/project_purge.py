"""作品「永久清除」要删的每一行——从 ORM 元数据推出来，不再手工列表（B08-19）。

一部作品拥有：

1. 带 ``PURGE_KEY_COLUMNS`` 里任何一列的表中，值落在这部作品名下的行——作品自己（``project_id``）、
   它的章（``chapter_id``）、它的场景（``scene_id``）、它的作者稿（``draft_id``）、它的人物（``character_id``）。
   新表只要带着其中一列就自动在清单里；过去的手工清单漏了 ``style_reference_scene_windows``（每场冻结的
   选窗，没有外键），永久清除之后留下了孤儿行；
2. 没有这些列、靠别的引用认出来的行（``INDIRECTLY_PURGED_TABLES``）：这部作品的 LLM 调用的逐次尝试
   （``llm_call_id``）、``scope_ref_id`` 指向作品 / 章 / 场景 / 人物的风格绑定与作者偏好档案。

``tests/test_trash_purge_completeness.py`` 的守卫要求：一张表只要带着指向作品对象的列（``*project_id`` /
``*chapter_id`` / ``*scene_id`` / ``*draft_id`` / ``*character_id`` / ``scope_ref_id``）却不在上面两种里，
就得在 ``NOT_PURGED_TABLES`` 里说清为什么不删——不许默默漏掉。操作日志（``operation_logs``）刻意保留：
纯操作审计、没有正文，按 ``object_ref`` 记录，不在守卫的范围里。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import Table, delete, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    AuthorPreferenceProfile,
    ChapterGoal,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    StoryCharacter,
    StyleReferenceInjectionBinding,
)
from novel_system.services.project_ownership import tables_child_first

#: 按这些列认出「这一行属于这部作品」（值 = 作品 id / 它的章、场景、作者稿、人物的 id）
PURGE_KEY_COLUMNS = ("project_id", "chapter_id", "scene_id", "draft_id", "character_id")

#: 没有上面任何一列、却属于作品的表：怎么找到它们的行
INDIRECTLY_PURGED_TABLES = {
    "llm_call_attempts": "llm_call_id 属于这部作品的 LLM 调用（作品 / 章 / 场景名下的 llm_calls）",
    "style_reference_injection_bindings": "scope_ref_id = 作品 / 章 / 场景 / 人物 id（绑定的画像是共享资产，不删）",
    "author_preference_profiles": "scope_ref_id = 作品 / 章 / 场景 / 人物 id",
}

#: 带着指向作品对象的列、却刻意不随作品永久清除的表，以及为什么
NOT_PURGED_TABLES = {
    "relation_profiles": "退役的关系卡（批准 #15）：产品里写不进来，表随后由迁移删除",
}

#: 守卫认为「可能指向作品对象」的列名后缀
OWNERSHIP_REFERENCE_SUFFIXES = ("project_id", "chapter_id", "scene_id", "draft_id", "character_id")


@dataclass(frozen=True)
class ProjectPurgePlan:
    """永久清除之前冻结的所有权：作品名下的章、场景、作者稿、人物。"""

    project_id: str
    chapter_ids: tuple[str, ...]
    scene_ids: tuple[str, ...]
    draft_ids: tuple[str, ...]
    character_ids: tuple[str, ...]

    def ids_for(self, column: str) -> tuple[str, ...]:
        return {
            "project_id": (self.project_id,),
            "chapter_id": self.chapter_ids,
            "scene_id": self.scene_ids,
            "draft_id": self.draft_ids,
            "character_id": self.character_ids,
        }[column]

    @property
    def scope_ref_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((self.project_id, *self.chapter_ids, *self.scene_ids, *self.character_ids))
        )


@lru_cache(maxsize=1)
def purged_tables_child_first() -> tuple[Table, ...]:
    """带 ``PURGE_KEY_COLUMNS`` 的表（作品本表除外，它最后单独删），子表在前：删除顺序不撞外键。

    与作者状态重置同一条元数据遍历（``project_ownership.tables_child_first``）。"""
    return tables_child_first(PURGE_KEY_COLUMNS, exclude=NOT_PURGED_TABLES)


def build_project_purge_plan(session: Session, project_id: str) -> ProjectPurgePlan:
    chapter_ids = tuple(
        session.execute(select(ChapterGoal.chapter_id).where(ChapterGoal.project_id == project_id)).scalars()
    )
    # 场景归属：场景卡自己的 project_id，或者它所在的章属于这部作品（v1 建的旧场景卡没有 project_id）
    scene_filter = SceneCard.project_id == project_id
    if chapter_ids:
        scene_filter = or_(scene_filter, SceneCard.chapter_id.in_(chapter_ids))
    scene_ids = tuple(session.execute(select(SceneCard.scene_id).where(scene_filter)).scalars())
    draft_filter = (AuthorDraft.object_type == "project") & (AuthorDraft.object_id == project_id)
    if scene_ids:
        draft_filter = draft_filter | ((AuthorDraft.object_type == "scene") & AuthorDraft.object_id.in_(scene_ids))
    if chapter_ids:
        draft_filter = draft_filter | ((AuthorDraft.object_type == "chapter") & AuthorDraft.object_id.in_(chapter_ids))
    draft_ids = tuple(session.execute(select(AuthorDraft.draft_id).where(draft_filter)).scalars())
    character_ids = tuple(
        session.execute(select(StoryCharacter.character_id).where(StoryCharacter.project_id == project_id)).scalars()
    )
    return ProjectPurgePlan(
        project_id=project_id,
        chapter_ids=tuple(dict.fromkeys(chapter_ids)),
        scene_ids=tuple(dict.fromkeys(scene_ids)),
        draft_ids=tuple(dict.fromkeys(draft_ids)),
        character_ids=tuple(dict.fromkeys(character_ids)),
    )


def purge_project_rows(session: Session, plan: ProjectPurgePlan) -> dict[str, int]:
    """删掉这部作品名下的每一行（作品本表除外）。返回 ``{表名: 删除行数}``。"""
    counts: dict[str, int] = {}

    def execute(statement, key: str) -> None:
        result = session.execute(statement)
        counts[key] = counts.get(key, 0) + max(int(result.rowcount or 0), 0)

    # LLM 调用的逐次尝试只认 llm_call_id：先按这部作品的调用删，再删调用本身
    call_scope = [LlmCall.project_id == plan.project_id]
    if plan.scene_ids:
        call_scope.append(LlmCall.scene_id.in_(plan.scene_ids))
    if plan.chapter_ids:
        call_scope.append(LlmCall.chapter_id.in_(plan.chapter_ids))
    execute(
        delete(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id.in_(select(LlmCall.llm_call_id).where(or_(*call_scope)))
        ),
        "llm_call_attempts",
    )
    refs = plan.scope_ref_ids
    execute(
        delete(StyleReferenceInjectionBinding).where(StyleReferenceInjectionBinding.scope_ref_id.in_(refs)),
        "style_reference_injection_bindings",
    )
    execute(
        delete(AuthorPreferenceProfile).where(AuthorPreferenceProfile.scope_ref_id.in_(refs)),
        "author_preference_profiles",
    )
    for table in purged_tables_child_first():
        conditions = [
            table.c[column].in_(plan.ids_for(column))
            for column in PURGE_KEY_COLUMNS
            if column in table.c and plan.ids_for(column)
        ]
        if conditions:
            execute(delete(table).where(or_(*conditions)), table.name)
    return counts
