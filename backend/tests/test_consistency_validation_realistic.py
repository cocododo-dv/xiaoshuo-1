"""Blueprint §17 Action B — REALISTIC consistency validation (the honest test).

The companion file ``test_consistency_validation.py`` proves the checker works on
*textbook-exact* bugs ("右手握", "沧澜城城主说道", "林远拿出断剑"). Those passages
were, in effect, written backwards from the detector's narrow substring patterns,
so a green run there gives **false confidence** — the blueprint asks for "一段你
已知有连续性 bug 的旧稿" (a real draft), not prose tailored to the matcher.

This file is that real draft. Every seeded bug is phrased the way an LLM actually
slips — the contradiction is present but NOT in the detector's literal form:

    断臂角色:  "抬起右手，稳稳握住长刀"     (not "右手握")
              "双手握住长枪"                (the blueprint's 双手握剑 example)
    已死角色:  "缓缓睁开双眼，沙哑地开口"    (not "<全名>说")
    错误地点:  "依旧待在沧澜城的客栈里"      (not "仍在/还在")
    丢失物品:  "握紧了那柄断剑"             (not "拿出/掏出")

It measures recall / precision / F1 for the upgraded clause-level detector AND, on
the *same* passages, for a frozen copy of the original adjacent-substring matcher.
The contrast is the evidence the blueprint wants:

    naive (pre-upgrade) recall on realistic prose  ≈ 0%
    upgraded recall on realistic prose             ≥ 80%   (precision held ~100%)

Run standalone:  pytest tests/test_consistency_validation_realistic.py -s
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass

import pytest

from novel_system.db.models import (
    ChapterGoal,
    OutlinePlan,
    SceneCard,
    StoryProject,
)
from novel_system.services.narrative_event_log import (
    ConsistencyViolation,
    NarrativeEventLog,
)

# ---------------------------------------------------------------------------
PROJECT_ID = "proj_consistency_realistic"
OUTLINE_ID = "outline_consistency_realistic"
CHAPTER_ID = "ch_consistency_realistic_01"
SETUP_SCENE_IDS = ["rc_setup_01", "rc_setup_02", "rc_setup_03", "rc_setup_04"]
TARGET_SCENE_ID = "rc_target_05"


@dataclass
class Passage:
    label: str
    text: str
    expected_bug_count: int
    bug_description: str


# ---------------------------------------------------------------------------
# Seeding — identical authoritative truth table to the textbook test
# ---------------------------------------------------------------------------

def _seed(session) -> NarrativeEventLog:
    session.add(StoryProject(project_id=PROJECT_ID, title="真实连续性验证", outline_text="x"))
    session.add(OutlinePlan(plan_id=OUTLINE_ID, project_id=PROJECT_ID))
    session.add(ChapterGoal(
        chapter_id=CHAPTER_ID, project_id=PROJECT_ID,
        outline_plan_id=OUTLINE_ID, chapter_goal="验证",
    ))
    for i, sid in enumerate(SETUP_SCENE_IDS, start=1):
        session.add(SceneCard(
            scene_id=sid, chapter_id=CHAPTER_ID, project_id=PROJECT_ID,
            outline_plan_id=OUTLINE_ID, scene_seq=i, scene_goal=f"setup {i}",
        ))
    session.add(SceneCard(
        scene_id=TARGET_SCENE_ID, chapter_id=CHAPTER_ID, project_id=PROJECT_ID,
        outline_plan_id=OUTLINE_ID, scene_seq=5, scene_goal="target",
    ))
    session.flush()

    log = NarrativeEventLog(session)
    common = dict(
        project_id=PROJECT_ID,
        chapter_id=CHAPTER_ID,
        authority_status="accepted",
        source_kind="test_fixture",
    )
    # 林远: alive, 北境, 右臂已断, 失去断剑
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[0], event_type="character_state",
                  entity_type="character", entity_id="林远", fact_key="alive", fact_value="alive")
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[0], event_type="location_change",
                  entity_type="character", entity_id="林远", fact_key="location", fact_value="北境")
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[1], event_type="character_state",
                  entity_type="character", entity_id="林远", fact_key="missing_limb", fact_value="right_arm")
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[1], event_type="item_change",
                  entity_type="character", entity_id="林远", fact_key="has_item", fact_value="lost:断剑")
    # 苏晚: alive, 沧澜城
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[0], event_type="character_state",
                  entity_type="character", entity_id="苏晚", fact_key="alive", fact_value="alive")
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[0], event_type="location_change",
                  entity_type="character", entity_id="苏晚", fact_key="location", fact_value="沧澜城")
    # 沧澜城城主: dead
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[0], event_type="character_state",
                  entity_type="character", entity_id="沧澜城城主", fact_key="alive", fact_value="alive")
    log.log_event(**common, scene_id=SETUP_SCENE_IDS[2], event_type="character_state",
                  entity_type="character", entity_id="沧澜城城主", fact_key="alive", fact_value="dead")
    session.commit()
    return log


# ---------------------------------------------------------------------------
# Realistic bugs — present but NOT in the detector's literal substring form
# ---------------------------------------------------------------------------
REALISTIC_BUGS: list[Passage] = [
    Passage(
        label="R-BUG-1:断臂角色抬起右手握刀(非紧贴句式)",
        text="林远抬起右手,稳稳握住了那柄长刀,刀锋在月色下泛着寒光。",
        expected_bug_count=1,
        bug_description="右臂已断,却'抬起右手...握住'——旧匹配只认'右手握'紧贴,漏判",
    ),
    Passage(
        label="R-BUG-2:断臂角色双手持械(蓝图双手握剑范例)",
        text="战鼓擂动,林远双手握住长枪,迎着冲锋而来的骑兵奋力刺出。",
        expected_bug_count=1,
        bug_description="右臂已断却'双手握住'——旧匹配完全不处理'双手',漏判",
    ),
    Passage(
        label="R-BUG-3:已死角色苏醒发声(非'全名+说')",
        text="沧澜城城主缓缓睁开双眼,沙哑地开口,问眼前的人究竟是谁。",
        expected_bug_count=1,
        bug_description="城主已死却'睁开双眼...开口'——旧匹配只认'沧澜城城主说'紧贴,漏判",
    ),
    Passage(
        label="R-BUG-4:角色滞留错误地点(非'仍在/还在')",
        text="夜色深沉,林远依旧待在沧澜城的客栈里,迟迟没有北上。",
        expected_bug_count=1,
        bug_description="林远应在北境却'依旧待在沧澜城'——旧匹配只认'仍在/还在',漏判",
    ),
    Passage(
        label="R-BUG-5:角色动用已失物品(非'拿出/掏出')",
        text="林远握紧了那柄断剑,剑身的裂纹在火光中清晰可见。",
        expected_bug_count=1,
        bug_description="断剑已失却'握紧了那柄断剑'——旧匹配只认'拿出/掏出',漏判",
    ),
]

# Clean near-misses — must NOT fire (these are where naive precision usually dies)
REALISTIC_CLEAN: list[Passage] = [
    Passage(
        label="RC-1:已死角色出现在回忆/墓前(应判净)",
        text="苏晚跪在沧澜城城主的墓前,想起他临终时的叮嘱,泪水无声滑落。",
        expected_bug_count=0,
        bug_description="城主在墓前/回忆框架中被提及,非存活动作",
    ),
    Passage(
        label="RC-2:断臂角色用健全的左手(应判净)",
        text="林远用仅存的左手握住缰绳,空荡荡的右袖在寒风中来回摆动。",
        expected_bug_count=0,
        bug_description="用左手(健全),右袖是空的——不是动用右臂",
    ),
    Passage(
        label="RC-3:角色正确返回北境并握缰绳(应判净)",
        text="林远策马北上,终于回到北境的关隘,握紧了手中的缰绳。",
        expected_bug_count=0,
        bug_description="位置正确(北境),'手中'非具体右手/断剑",
    ),
    Passage(
        label="RC-4:泛指失去与握拳(应判净)",
        text="林远低声叹息,想起自己失去的一切,缓缓握紧了拳头。",
        expected_bug_count=0,
        bug_description="泛指失去,未动用断剑;握拳非右臂特指",
    ),
    Passage(
        label="RC-5:状态一致的独白(应判净)",
        text="北境的风雪从不停歇。林远立于城头,望着苍茫的远方,久久不语。",
        expected_bug_count=0,
        bug_description="林远在北境、存活、无右臂动作——完全一致",
    ),
]


# ---------------------------------------------------------------------------
# Frozen copy of the ORIGINAL adjacent-substring matcher (pre-upgrade baseline).
# This is intentionally a verbatim snapshot so the test quantifies the gap the
# upgrade closed and guards against any regression back to narrow matching.
# ---------------------------------------------------------------------------

def _naive_check(text_lower: str, char_id: str, fact_key: str, fact_value: str) -> bool:
    value_lower = fact_value.lower()
    char_lower = char_id.lower()
    if fact_key == "alive" and value_lower == "dead":
        for verb in ["说", "走", "跑", "笑", "叹", "喊", "回答", "点头", "摇头",
                     "said", "walked", "ran", "smiled", "spoke", "nodded"]:
            if f"{char_lower}{verb}" in text_lower or f"{char_lower} {verb}" in text_lower:
                return True
    if fact_key == "location":
        for phrase in [f"{char_lower} was still at", f"{char_lower} remained at",
                       f"{char_lower}还在", f"{char_lower}仍在"]:
            idx = text_lower.find(phrase)
            if idx >= 0:
                after = text_lower[idx + len(phrase):idx + len(phrase) + 50]
                if value_lower not in after:
                    return True
    if fact_key == "missing_limb" and value_lower:
        limb_actions = {
            "right_arm": ["右手握", "右手举", "右臂挥", "right hand gripped", "raised his right arm"],
            "left_arm": ["左手握", "左手举", "左臂挥", "left hand gripped", "raised his left arm"],
            "right_leg": ["右脚踢", "right leg kicked"],
            "left_leg": ["左脚踢", "left leg kicked"],
        }
        for limb_key, indicators in limb_actions.items():
            if limb_key in value_lower or value_lower in limb_key:
                for indicator in indicators:
                    if indicator.lower() in text_lower:
                        return True
    if fact_key == "has_item" and value_lower.startswith("lost:"):
        lost_item = value_lower.replace("lost:", "").strip()
        for pat in [f"{char_lower}拿出{lost_item}", f"{char_lower}掏出{lost_item}",
                    f"{char_lower} pulled out the {lost_item}"]:
            if pat in text_lower:
                return True
    return False


def _naive_violation_count(log: NarrativeEventLog, text: str) -> int:
    """How many distinct facts the naive matcher would flag for this passage."""
    text_lower = text.lower()
    count = 0
    for char_id in ["林远", "苏晚", "沧澜城城主"]:
        state = log.project_character_state(char_id, PROJECT_ID, up_to_scene_seq=4)
        for fact_key, projected in state.facts.items():
            if fact_key not in {"alive", "location", "missing_limb", "has_item",
                                "physical_state", "appearance", "ability"}:
                continue
            if _naive_check(text_lower, char_id, fact_key, projected.fact_value):
                count += 1
    return count


# ---------------------------------------------------------------------------
def _safe_div(a: float, b: float) -> float:
    return a / b if b > 0 else 0.0


def _f1(p: float, r: float) -> float:
    return _safe_div(2 * p * r, p + r)


def _score(detected_counts: list[tuple[Passage, int]]) -> tuple[float, float, float, dict]:
    tp = fp = fn = 0
    for passage, detected in detected_counts:
        if passage.expected_bug_count > 0:
            caught = min(detected, passage.expected_bug_count)
            tp += caught
            fn += passage.expected_bug_count - caught
            fp += max(0, detected - passage.expected_bug_count)
        else:
            fp += detected
    recall = _safe_div(tp, tp + fn)
    precision = _safe_div(tp, tp + fp)
    return recall, precision, _f1(precision, recall), {"tp": tp, "fp": fp, "fn": fn}


# ---------------------------------------------------------------------------
@pytest.mark.consistency_validation
class TestRealisticConsistency:

    def test_realistic_recall_precision_with_baseline(self, session, capsys) -> None:
        log = _seed(session)
        all_passages = REALISTIC_BUGS + REALISTIC_CLEAN

        upgraded: list[tuple[Passage, int]] = []
        naive: list[tuple[Passage, int]] = []
        rows: list[str] = []
        for p in all_passages:
            report = log.check_consistency(p.text, PROJECT_ID, TARGET_SCENE_ID)
            up_n = len(report.violations)
            nv_n = _naive_violation_count(log, p.text)
            upgraded.append((p, up_n))
            naive.append((p, nv_n))
            tag = "BUG " if p.expected_bug_count else "CLEAN"
            rows.append(
                f"  [{tag}] {p.label}\n"
                f"        upgraded={up_n}  naive={nv_n}  (expected={p.expected_bug_count})"
                + (f"\n        -> " + "; ".join(f"{v.fact_key}:{v.actual}" for v in report.violations)
                   if report.violations else "")
            )

        up_r, up_p, up_f1, up_c = _score(upgraded)
        nv_r, nv_p, nv_f1, nv_c = _score(naive)

        report_txt = (
            "\n" + "=" * 74 + "\n"
            "  Blueprint §17 Action B — REALISTIC Consistency Validation\n"
            "=" * 74 + "\n"
            + "\n".join(rows) + "\n"
            + "-" * 74 + "\n"
            f"  UPGRADED (clause-level)   recall={up_r:.0%}  precision={up_p:.0%}  F1={up_f1:.0%}  {up_c}\n"
            f"  NAIVE    (pre-upgrade)    recall={nv_r:.0%}  precision={nv_p:.0%}  F1={nv_f1:.0%}  {nv_c}\n"
            + "-" * 74 + "\n"
            f"  Recall lift on realistic prose: {nv_r:.0%}  ->  {up_r:.0%}\n"
            + "=" * 74 + "\n"
        )
        with capsys.disabled():
            print(report_txt)

        # The point of the test: realistic prose defeats the old narrow matcher...
        assert nv_r <= 0.20, f"Naive baseline recall {nv_r:.0%} unexpectedly high — set too easy"
        # ...and the upgraded detector recovers recall without sacrificing precision.
        assert up_r >= 0.80, f"Upgraded recall {up_r:.0%} below 80% floor on realistic prose"
        assert up_p >= 0.90, f"Upgraded precision {up_p:.0%} below 90% floor (false alarms)"

    @pytest.mark.parametrize("idx", range(len(REALISTIC_BUGS)),
                             ids=[p.label for p in REALISTIC_BUGS])
    def test_each_realistic_bug_caught(self, session, idx: int) -> None:
        log = _seed(session)
        p = REALISTIC_BUGS[idx]
        report = log.check_consistency(p.text, PROJECT_ID, TARGET_SCENE_ID)
        assert not report.passed, f"MISSED: {p.label} — {p.bug_description}"

    @pytest.mark.parametrize("idx", range(len(REALISTIC_CLEAN)),
                             ids=[p.label for p in REALISTIC_CLEAN])
    def test_each_realistic_clean_no_false_alarm(self, session, idx: int) -> None:
        log = _seed(session)
        p = REALISTIC_CLEAN[idx]
        report = log.check_consistency(p.text, PROJECT_ID, TARGET_SCENE_ID)
        assert report.passed, (
            f"FALSE ALARM on {p.label}: "
            f"{[(v.fact_key, v.actual, v.evidence) for v in report.violations]}"
        )


# ---------------------------------------------------------------------------
# Wave 4（§5.6 / 完成门）：POV 投影只减**写作提示词**，硬 QC 仍读全量权威状态。
# 守卫这条分离不变量——秘密从写作提示词消失，但硬 QC 仍能用全量事实发现矛盾。
# ---------------------------------------------------------------------------
class TestHardQcReadsFullStateDespitePovProjection:

    def test_hard_qc_still_sees_full_state_when_project_has_secrets(self, session) -> None:
        """项目含秘密时，check_consistency 仍读全量 → 死人行动矛盾照旧检出。"""
        from novel_system.services.pov_knowledge_projection import PovKnowledgeProjection

        log = _seed(session)
        # 追加：沧澜城城主持有一个秘密（写作提示词应对非持有者 POV 隐藏）。
        log.log_event(
            project_id=PROJECT_ID, chapter_id=CHAPTER_ID, scene_id=SETUP_SCENE_IDS[2],
            event_type="character_state", entity_type="character", entity_id="沧澜城城主",
            fact_key="secret_held_by", fact_value="城主藏了传国玉玺",
            authority_status="accepted", source_kind="test_fixture",
        )
        session.commit()

        # (1) 硬 QC：城主已死却"睁开双眼开口"——即便项目有秘密，全量读取仍检出。
        report = log.check_consistency(
            "沧澜城城主缓缓睁开双眼,沙哑地开口,问眼前的人究竟是谁。",
            PROJECT_ID, TARGET_SCENE_ID,
        )
        assert not report.passed
        assert any(v.fact_key == "alive" for v in report.violations)

        # (2) 硬 QC 的全量投影仍持有秘密（供确定性校验/人工确认使用）。
        full_state = log.project_character_state("沧澜城城主", PROJECT_ID, up_to_scene_seq=4)
        assert full_state.get("secret_held_by") == "城主藏了传国玉玺"

        # (3) 但**写作提示词**（POV=林远，非秘密持有者）不含该秘密正文。
        writing_prompt = PovKnowledgeProjection(
            session,
            event_log=NarrativeEventLog(session),
        ).format_state_for_prompt(
            PROJECT_ID, scene_seq=5,
            pov_character_id="林远", onstage_character_ids=["林远", "沧澜城城主"],
        )
        assert "城主藏了传国玉玺" not in writing_prompt
