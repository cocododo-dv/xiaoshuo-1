"""雪花构思：步骤版本（含 v1 规划器留下的旧表）、教练回合、意图要点、人物 / 章 / 场景规划、分诊、修订链。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class SnowflakeArtifact(Base):
    __tablename__ = "snowflake_artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending_review")
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # P0-3: per-upstream content signatures captured at approval ("what I consumed,
    # at what version"). Powers dependency/diff-aware staleness instead of marking
    # every downstream step stale on any upstream change.
    consumed_input_sigs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeStepRun(Base):
    __tablename__ = "snowflake_step_runs"

    step_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending_review")
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    health_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    input_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # P0-3: per-upstream content signatures captured at approval — see SnowflakeArtifact.
    consumed_input_sigs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale_accepted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeAssistantTurn(Base):
    __tablename__ = "snowflake_assistant_turns"

    turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    focus_scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_message: Mapped[str] = mapped_column(Text)
    reply: Mapped[str] = mapped_column(Text)
    suggestions_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_label: Mapped[str | None] = mapped_column(String, nullable=True)
    candidate_patch_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    source: Mapped[str] = mapped_column(String, default="fallback")
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 2026-09-17 阶段 U（迁移 20260917_0088）：教练日志里有两种回合——chat（问答）与 candidates（「先看 3 个方向」）。
    # candidates_json = {"items": [{label, tag, text, notes}], "target_chars": n}；brief_delta_json 是这一轮对
    # 作者意图要点的差异（+ / 改 / 撤），落表后日志自己会说话；adoption_json 记这一回合被哪一版生成采纳过
    # （{step_run_id, candidate_index, adopted_at}）——「已按此生成」的徽章与教练看到的「作者选了哪个方向」都靠它。
    turn_kind: Mapped[str] = mapped_column(String, default="chat")
    candidates_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    brief_delta_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    adoption_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SnowflakeDirectionBrief(Base):
    """2026-09-16 作者意图要点：驻场教练对话蒸馏出的、作者可编辑的本步意图。

    一步一行。``lines_json`` 是带 ``line_id`` 的条目列表（决定 / 否决 / 约束 / 待定，本步 / 全书），
    含已撤条目以便恢复。教练只能改写或撤下自己提出的条目，作者改过的条目归作者。
    生成 / 候选 / 分诊把当前活动条目（加上游各步的全书级条目）作为受保护的提示键读入，
    生成出的版本在 ``health_json.direction_brief`` 记录消费了哪一版。
    """

    __tablename__ = "snowflake_direction_briefs"
    __table_args__ = (
        Index("ix_snowflake_direction_briefs_step", "project_id", "step_key", unique=True),
    )

    brief_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    lines_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    inherit_upstream: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_turn_ids_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=list)
    author_edited_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeCharacterPlan(Base):
    __tablename__ = "snowflake_character_plans"

    character_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    character_id: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    synopsis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    bible_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    source_step_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeChapterPlan(Base):
    """构思侧的「章」（P2）。

    在此之前章在整条雪花管线里没有归属者：09/10 步没有章字段，提示词让 LLM 把
    chapter_id 留空说「server assigns」，而服务端的起始值就是 ``{project_id}_CH01``
    并丢弃作者输入 —— 全书落进一章。唯一编了章的 07 长篇大纲只是四段自由文本，
    物化时根本不读。这张表把章变成有稳定身份、可编辑标题与章目标的一等规划行。
    """

    __tablename__ = "snowflake_chapter_plans"
    __table_args__ = (
        # 与场景计划同一条纪律（P1-1）：作者可改的序号/标题不能当身份，row_uid 才是。
        Index("ix_snowflake_chapter_plans_row_uid", "project_id", "row_uid", unique=True),
        Index("ix_snowflake_chapter_plans_seq", "project_id", "chapter_seq"),
    )

    chapter_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    row_uid: Mapped[str] = mapped_column(String)
    chapter_seq: Mapped[int] = mapped_column(Integer, default=1)
    act: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 灾一 / 灾二 / 灾三 —— 三幕结构的铰链，分章时与同标记的场互相锚定
    spine: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    source_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 阶段 Y：这一章在目录里的 id（ChapterGoal.chapter_id）。一经铸出永不随章序改变——章的身份跟着
    # row_uid 走，不跟着位置走；没物化过、也还没保存过分章的章是 NULL。
    catalog_chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeScenePlan(Base):
    __tablename__ = "snowflake_scene_plans"
    __table_args__ = (
        # P1-1: immutable, system-minted row identity. Scene identity is no longer
        # derived from the author-editable ``scene_id`` — ``row_uid`` is the stable
        # anchor the staleness diff (P0-3) relies on.
        Index("ix_snowflake_scene_plans_row_uid", "project_id", "row_uid", unique=True),
        # P1-2: scene_id 是这一行对外的物化目标身份 —— SceneCard 直接拿它当主键。
        # 历史铸造规则是 f"{chapter_id}_SC{scene_seq:02d}"，创建时铸死而 scene_seq
        # 每次保存都按传入列表重算，于是「删一场再加一场」必然撞号；撞号后
        # _build_outline_plan 的 detail_by_id 与 approve_outline_plan 的
        # session.get(SceneCard, scene_id) 会双重覆盖，静默丢掉一场。铸造规则已改成
        # row_uid 基（见 snowflake_workspace._mint_scene_id），这条唯一索引是结构兜底：
        # 万一还有别的路径铸出重复 id，宁可硬报错也不要静默丢场。
        Index("ix_snowflake_scene_plans_scene_id", "project_id", "scene_id", unique=True),
        Index("ix_snowflake_scene_plans_chapter_plan_id", "chapter_plan_id"),
    )

    scene_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    row_uid: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str] = mapped_column(String)
    # P2：章归属。chapter_id 从「创建时铸死的系统身份」降级为由分章结果推导的
    # 物化目标 id；真正的归属锚是 chapter_plan_id（NULL = 还没分章）。
    chapter_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "snowflake_chapter_plans.chapter_plan_id",
            name="fk_snowflake_scene_plans_chapter_plan_id",
        ),
        nullable=True,
    )
    chapter_id: Mapped[str] = mapped_column(String)
    # 作者在 09 场景列表上标的灾一/灾二/灾三。历史上前端从不上行、水合还硬写回 ""，
    # 标记每次刷新就丢；脊柱锚点分章要靠它，所以现在往返保真。
    spine: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_title: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene_seq: Mapped[int] = mapped_column(Integer, default=1)
    pov_character_id: Mapped[str | None] = mapped_column(String, nullable=True)
    onstage_chars_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene_type: Mapped[str] = mapped_column(String, default="proactive")
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_crucible: Mapped[str | None] = mapped_column(Text, nullable=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict: Mapped[str | None] = mapped_column(Text, nullable=True)
    setback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reaction: Mapped[str | None] = mapped_column(Text, nullable=True)
    dilemma: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    beats_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    must_include_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    tension_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    function_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    causal_prerequisite_scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_length_band: Mapped[str | None] = mapped_column(String, nullable=True)
    # 2026-09-13 阶段 C：反应场的呈现方式——full（整场戏剧化）/ summary（两段概述，200–500 字）。
    # Ingermanson：反应场可以整场写、缩成两段概述、或干脆略过；第一版只做前两档，主动场恒为 full。
    # 迁移 20260913_0084 加列，server_default="full"。
    rendering_mode: Mapped[str] = mapped_column(String, default="full")
    # 2026-09-14 阶段 J：原著场景表的两栏——这一场想让读者经历什么（分诊第 5 步）、故事时间戳。
    # 迁移 20260914_0085 加列，可空。
    expected_reader_emotion: Mapped[str | None] = mapped_column(Text, nullable=True)
    story_time: Mapped[str | None] = mapped_column(String, nullable=True)
    # 2026-09-15 阶段 N：作者写下的破例理由（原著：「冲突：无」的收尾课、没有三拍的叙述收尾场——
    # 不过关也可以放行，但要知道理由）。有理由时规则层不再把缺三拍 / 缺坩埚记成缺失。
    # 迁移 20260915_0086 加列，可空。
    exception_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    source_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale_accepted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    # P1-3 收口：作者在场景列表里删掉的场。历史上 _sync_scene_plans 只增不删，
    # 被删的场永远留在库里、拿不到第 10 步细化、被诊断成 rewrite，于是用一个
    # 作者根本看不见的场把物化闸门永久堵死。软删（不物删）保留可恢复与可审计。
    removed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # 构思侧已删、但目录侧已经落库成 SceneCard（可能已有正文）的场：不能静默删，
    # 标记出来交作者裁决（Phase 2 的分章预览面板会把它列进告警区）。
    orphaned_flag: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeSceneTriageItem(Base):
    __tablename__ = "snowflake_scene_triage_items"

    triage_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    scene_plan_id: Mapped[str] = mapped_column(ForeignKey("snowflake_scene_plans.scene_plan_id"))
    scene_id: Mapped[str] = mapped_column(String)
    recommended_status: Mapped[str] = mapped_column(String, default="")
    manual_status: Mapped[str] = mapped_column(String, default="")
    effective_status: Mapped[str] = mapped_column(String, default="")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    fix_steps_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    repair_patch_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pressure_flags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocking: Mapped[int] = mapped_column(Integer, default=0)
    manual_override: Mapped[int] = mapped_column(Integer, default=0)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeRevisionLink(Base):
    __tablename__ = "snowflake_revision_links"

    revision_link_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    source_step_key: Mapped[str] = mapped_column(String)
    source_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    affected_kind: Mapped[str] = mapped_column(String)
    affected_id: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="open")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


__all__ = [
    "SnowflakeArtifact",
    "SnowflakeAssistantTurn",
    "SnowflakeChapterPlan",
    "SnowflakeCharacterPlan",
    "SnowflakeDirectionBrief",
    "SnowflakeRevisionLink",
    "SnowflakeScenePlan",
    "SnowflakeSceneTriageItem",
    "SnowflakeStepRun",
]
