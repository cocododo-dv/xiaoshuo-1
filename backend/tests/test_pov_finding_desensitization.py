"""Wave 4 — finding 证据脱敏（设计 §5.6 / §7.11 / 不变量 11 / §9.1）。

任何把 QC/批判 finding 回灌进写作或补丁提示词的路径，必须先对 finding 证据做同一
POV 投影脱敏：引用了非 POV 已知秘密的 finding 不得进入自动补丁提示词，改走作者确认。
硬 QC 自身不经此路径（始终读全量）。
"""
from __future__ import annotations

from novel_system.db.models import ChapterGoal, ChapterState, SceneCard, StoryProject
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.pov_knowledge_projection import PovKnowledgeProjection

PROJECT = "PROJ_DESENS"
CHAPTER = "CH_DESENS01"


def _seed(session) -> None:
    session.add(StoryProject(project_id=PROJECT, title="POV desensitization", outline_text=""))
    session.add(ChapterGoal(
        chapter_id=CHAPTER,
        project_id=PROJECT,
        planned_scene_count=2,
        chapter_goal="d",
    ))
    session.add(ChapterState(chapter_id=CHAPTER, current_phase="drafting"))
    for seq in (1, 2):
        session.add(SceneCard(
            scene_id=f"{CHAPTER}_SC0{seq}", chapter_id=CHAPTER, project_id=PROJECT,
            scene_seq=seq, scene_goal=f"s{seq}",
        ))
    log = NarrativeEventLog(session)
    # 非 POV 角色 X 的秘密；POV=Y 不知。
    log.log_event(
        project_id=PROJECT, scene_id=f"{CHAPTER}_SC01", chapter_id=CHAPTER,
        event_type="character_state", entity_type="character", entity_id="X",
        fact_key="secret_held_by", fact_value="X是幕后凶手",
        authority_status="accepted", source_kind="test_fixture",
    )
    # 公共硬事实（供公共 finding 引用）。
    log.log_event(
        project_id=PROJECT, scene_id=f"{CHAPTER}_SC01", chapter_id=CHAPTER,
        event_type="character_state", entity_type="character", entity_id="X",
        fact_key="location", fact_value="书房",
        authority_status="accepted", source_kind="test_fixture",
    )
    session.commit()


def test_finding_referencing_non_pov_secret_excluded_from_auto_patch(session) -> None:
    _seed(session)
    finding_secret = {
        "issue_key": "event_log_consistency_violation",
        "quality_level": "Q1",
        "authority_ref": "event:X.secret_held_by",
        "expected": "X是幕后凶手",
        "actual": "文本暗示X清白",
        "evidence": "X是幕后凶手",
    }
    finding_public = {
        "issue_key": "event_log_consistency_violation",
        "quality_level": "Q1",
        "authority_ref": "event:X.location",
        "expected": "书房",
        "actual": "客厅",
        "evidence": "X在客厅",
    }
    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    safe, redacted = proj.desensitize_findings(
        [finding_secret, finding_public], PROJECT, None, scene_id=f"{CHAPTER}_SC02",
        pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert finding_public in safe
    assert finding_secret not in safe
    assert any(r.get("author_confirmation_only") for r in redacted)
    assert redacted[0].get("desensitized_reason") == "references_non_pov_secret"


def test_finding_on_public_fact_passes_through(session) -> None:
    _seed(session)
    finding_public = {
        "issue_key": "x", "quality_level": "Q1",
        "expected": "书房", "actual": "客厅", "evidence": "X在客厅",
    }
    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    safe, redacted = proj.desensitize_findings(
        [finding_public], PROJECT, None, scene_id=f"{CHAPTER}_SC02",
        pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert safe == [finding_public]
    assert redacted == []


def test_pov_owned_secret_finding_passes_through(session) -> None:
    """POV 自己已知的秘密相关 finding 可进入补丁（POV 本就知道）。"""
    _seed(session)
    finding_pov_secret = {
        "issue_key": "x", "quality_level": "Q1",
        "expected": "X是幕后凶手", "actual": "...", "evidence": "X是幕后凶手",
    }
    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    # POV=X 本人 → 该秘密对 X 不是"非 POV 秘密" → 放行。
    safe, redacted = proj.desensitize_findings(
        [finding_pov_secret], PROJECT, None, scene_id=f"{CHAPTER}_SC02",
        pov_character_id="X", onstage_character_ids=["X", "Y"],
    )
    assert safe == [finding_pov_secret]
    assert redacted == []


def test_redact_brief_drops_secret_lines(session) -> None:
    """补丁 brief（list[str] 指令）中引用非 POV 秘密的条目被剔除。"""
    _seed(session)
    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    brief = [
        "让X的动机更清晰：X是幕后凶手，应在结尾点破。",   # 引用非 POV 秘密 → 剔除
        "加强场景的节奏与钩子。",                          # 纯软性 → 保留
    ]
    kept = proj.redact_brief(
        brief, PROJECT, None, scene_id=f"{CHAPTER}_SC02",
        pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert "加强场景的节奏与钩子。" in kept
    assert all("X是幕后凶手" not in line for line in kept)


def test_desensitize_noop_without_pov(session) -> None:
    """pov=None（全知视角）→ 不脱敏，全部放行。"""
    _seed(session)
    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    findings = [{"expected": "X是幕后凶手"}]
    safe, redacted = proj.desensitize_findings(
        findings, PROJECT, None, scene_id=f"{CHAPTER}_SC02", pov_character_id=None,
    )
    assert safe == findings
    assert redacted == []
