"""Wave 4 — POV 减法投影服务的 golden 用例（设计 §5.6 / §9.1）。

覆盖 6 个知识级别（known / believed_false / suspected / unknown / public /
secret_owner）的投影极性，以及"无显式秘密→等价全量注入"的退化性质。

投影只减**写作提示词**里的非 POV 秘密内容；硬 QC 的全量读取由
test_consistency_validation_realistic.py 的守卫测试锁定。
"""
from __future__ import annotations

from novel_system.db.models import ChapterGoal, ChapterState, SceneCard, StoryProject
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.pov_knowledge_projection import PovKnowledgeProjection

PROJECT = "PROJ_POV"
CHAPTER = "CH_POV01"


def _seed(session, *, onstage: dict[int, list[str]] | None = None) -> None:
    session.add(StoryProject(project_id=PROJECT, title="POV projection", outline_text=""))
    session.add(ChapterGoal(
        chapter_id=CHAPTER,
        project_id=PROJECT,
        planned_scene_count=3,
        chapter_goal="pov",
    ))
    session.add(ChapterState(chapter_id=CHAPTER, current_phase="drafting"))
    onstage = onstage or {}
    for seq in (1, 2, 3):
        session.add(SceneCard(
            scene_id=f"{CHAPTER}_SC0{seq}",
            chapter_id=CHAPTER,
            project_id=PROJECT,
            scene_seq=seq,
            scene_goal=f"scene {seq}",
            onstage_chars_json=onstage.get(seq),
        ))
    session.commit()


def _state(session, seq, entity, key, value, **kw):
    NarrativeEventLog(session).log_event(
        project_id=PROJECT, scene_id=f"{CHAPTER}_SC0{seq}", chapter_id=CHAPTER,
        event_type=kw.pop("event_type", "character_state"),
        entity_type="character", entity_id=entity,
        fact_key=key, fact_value=value,
        authority_status="accepted", source_kind="test_fixture", **kw,
    )


# ---------------------------------------------------------------------------
# format_state_for_prompt — POV 过滤
# ---------------------------------------------------------------------------

def test_projection_suppresses_non_pov_secret_content(session) -> None:
    """secret_owner：X 的秘密对非持有者 POV=Y 不可见（不含秘密正文）。"""
    _seed(session)
    _state(session, 1, "X", "secret_held_by", "X杀了市长")
    _state(session, 1, "X", "location", "码头")
    _state(session, 1, "Y", "location", "码头")
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC02", pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert "X杀了市长" not in out            # 秘密正文不得泄漏
    assert "码头" in out                     # 公共事实照旧注入
    assert "Y" in out and "X" in out


def test_projection_keeps_pov_owned_secret(session) -> None:
    """known：POV 自己持有的秘密要注入（POV 据此行动）。"""
    _seed(session)
    _state(session, 1, "Y", "secret_held_by", "Y藏了钥匙")
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC02", pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert "Y藏了钥匙" in out


def test_projection_keeps_secret_revealed_to_pov(session) -> None:
    """known：秘密已 revealed_to POV → 注入。"""
    _seed(session)
    _state(session, 1, "X", "secret_held_by", "地图在钟楼")
    _state(session, 2, "X", "revealed_to", "Y")           # 秘密向 Y 揭示
    _state(session, 2, "Y", "knows_map_location", "地图在钟楼",
           event_type="character_learns")                 # Y 已获知秘密内容
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC03", pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert "地图在钟楼" in out


def test_projection_pov_false_belief_injected_others_suppressed(session) -> None:
    """believed_false：POV 的错误信念注入；他人的错误信念内容抑制。"""
    _seed(session)
    _state(session, 1, "Y", "believes_false", "Y以为盟友还活着")
    _state(session, 1, "X", "believes_false", "X以为自己没暴露")
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC02", pov_character_id="Y", onstage_character_ids=["X", "Y"],
    )
    assert "Y以为盟友还活着" in out          # POV 自己的错误信念
    assert "X以为自己没暴露" not in out       # 他人错误信念内容抑制


def test_projection_suspected_marked_not_as_fact(session) -> None:
    """suspected：POV 的怀疑以"尚未确证"措辞注入，不表述为既定事实。"""
    _seed(session)
    _state(session, 1, "Y", "knows_x_lied", "true",
           event_type="character_learns", confidence="low",
           payload={"knowledge_status": "suspected"})
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC02", pov_character_id="Y", onstage_character_ids=["Y"],
    )
    assert "怀疑" in out or "尚未确证" in out or "suspect" in out.lower()


def test_projection_onstage_derivation_feeds_pov_known(session) -> None:
    """回填启发式：POV 在场场景断言的公共事实计入 POV 已知，不饿死上下文。"""
    _seed(session, onstage={1: ["Y", "Z"]})
    _state(session, 1, "Z", "location", "旧仓库")     # Y 在场，可观察 Z 的位置
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    known = proj.pov_known_fact_values(PROJECT, "Y", scene_id=f"{CHAPTER}_SC02")
    assert "旧仓库" in known


def test_projection_no_secrets_public_facts_identical_to_full(session) -> None:
    """退化性质：无任何信息不对称事实时，POV 投影的角色公共状态与全量注入等价。"""
    _seed(session)
    _state(session, 1, "A", "location", "北境")
    _state(session, 1, "A", "physical_state", "healthy")
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    log = NarrativeEventLog(session)
    pov_out = proj.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC02", pov_character_id="A", onstage_character_ids=["A"],
    )
    full_out = log.format_state_for_prompt(
        PROJECT, scene_id=f"{CHAPTER}_SC02", onstage_character_ids=["A"],   # pov=None → 全量
    )
    # 公共事实逐条保留（无秘密可减 → 角色状态段等价）
    assert "location: 北境" in pov_out
    assert "physical_state: healthy" in pov_out
    for line in ("location: 北境", "physical_state: healthy"):
        assert line in full_out and line in pov_out


# ---------------------------------------------------------------------------
# information_asymmetry_digest — POV 过滤
# ---------------------------------------------------------------------------

def test_asymmetry_digest_pov_hides_other_secret(session) -> None:
    """POV 信息不对称摘要不得打印他人秘密正文。"""
    _seed(session)
    _state(session, 1, "X", "secret_held_by", "X毒了酒")
    _state(session, 1, "Y", "location", "宴厅")
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.information_asymmetry_digest(
        PROJECT, scene_id=f"{CHAPTER}_SC02", onstage_character_ids=["X", "Y"], pov_character_id="Y",
    )
    assert "X毒了酒" not in out
    assert "Secrets held by X" not in out


def test_asymmetry_digest_pov_shows_own_exclusive_knowledge(session) -> None:
    """POV 独有的非秘密认知可展示（可据此行动）；他人独有内容只给盲区提示。"""
    _seed(session)
    NarrativeEventLog(session).log_event(
        project_id=PROJECT, scene_id=f"{CHAPTER}_SC01", chapter_id=CHAPTER,
        event_type="character_learns", entity_type="character", entity_id="Y",
        fact_key="knows_route", fact_value="密道在西墙",
        authority_status="accepted", source_kind="test_fixture",
    )
    NarrativeEventLog(session).log_event(
        project_id=PROJECT, scene_id=f"{CHAPTER}_SC01", chapter_id=CHAPTER,
        event_type="character_learns", entity_type="character", entity_id="X",
        fact_key="knows_traitor", fact_value="内奸是Y",
        authority_status="accepted", source_kind="test_fixture",
    )
    session.commit()

    proj = PovKnowledgeProjection(session, event_log=NarrativeEventLog(session))
    out = proj.information_asymmetry_digest(
        PROJECT, scene_id=f"{CHAPTER}_SC02", onstage_character_ids=["X", "Y"], pov_character_id="Y",
    )
    assert "密道在西墙" in out          # POV 独有认知可见
    assert "内奸是Y" not in out         # 他人独有认知（POV 未知）内容不可见
