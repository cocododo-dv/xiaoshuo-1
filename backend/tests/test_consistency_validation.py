"""Blueprint §17 Action B — Minimum Verifiable Kernel for consistency checking.

"拿一段已知有连续性 bug 的旧稿跑，量两个数——
 召回率（已知的 bug 抓到了几个？）和精确率（报警里有几个是误报/抽取器幻觉？）"

This test constructs a known character-state truth table via NarrativeEventLog,
then feeds passages with *labelled* bugs and clean passages, measuring:
  - Recall:    of the seeded bugs, how many does check_consistency catch?
  - Precision: of the reported violations, how many are genuine (not false alarms)?
  - F1:        harmonic mean of recall and precision

Run standalone:  pytest -m consistency_validation -s
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
from novel_system.services.narrative_event_log import NarrativeEventLog

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_ID = "proj_consistency_test"
OUTLINE_ID = "outline_consistency"
CHAPTER_ID = "ch_consistency_01"

# Scenes: events are seeded at scene_seq=1..4, the tested passage is scene_seq=5.
SETUP_SCENE_IDS = ["sc_setup_01", "sc_setup_02", "sc_setup_03", "sc_setup_04"]
TARGET_SCENE_ID = "sc_target_05"


# ---------------------------------------------------------------------------
# Structured test-case container
# ---------------------------------------------------------------------------
@dataclass
class BuggyPassage:
    """One test passage with its ground-truth label."""

    label: str
    text: str
    expected_bug_count: int  # 0 means clean (should trigger zero violations)
    bug_description: str


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_project(session) -> None:
    """Create the minimal project / outline / chapter / scene scaffolding."""
    session.add(StoryProject(
        project_id=PROJECT_ID,
        title="连续性验证测试项目",
        outline_text="测试用大纲",
    ))
    session.add(OutlinePlan(
        plan_id=OUTLINE_ID,
        project_id=PROJECT_ID,
    ))
    session.add(ChapterGoal(
        chapter_id=CHAPTER_ID,
        project_id=PROJECT_ID,
        outline_plan_id=OUTLINE_ID,
        chapter_goal="验证连续性检查",
    ))
    # Setup scenes (1-4) where character state is established
    for i, sid in enumerate(SETUP_SCENE_IDS, start=1):
        session.add(SceneCard(
            scene_id=sid,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            outline_plan_id=OUTLINE_ID,
            scene_seq=i,
            scene_goal=f"设定场景 {i}",
        ))
    # Target scene where the generated text will be checked
    session.add(SceneCard(
        scene_id=TARGET_SCENE_ID,
        chapter_id=CHAPTER_ID,
        project_id=PROJECT_ID,
        outline_plan_id=OUTLINE_ID,
        scene_seq=5,
        scene_goal="测试生成段落的连续性检查",
    ))
    session.flush()


def _seed_character_state(log: NarrativeEventLog) -> None:
    """Establish the known character/entity truth table via event log.

    After these events, the projected state (up to scene_seq=4) is:

    林远:
      alive        = alive
      location     = 北境
      missing_limb = right_arm      (断右臂)
      has_item     = lost:断剑       (lost the broken sword)

    苏晚:
      alive     = alive
      location  = 沧澜城
      (holds a secret 林远 doesn't know)

    沧澜城城主:
      alive = dead   (died in scene 3)

    Foreshadow FS-007: planted (将军的刀上有毒)
    """
    common = dict(
        project_id=PROJECT_ID,
        chapter_id=CHAPTER_ID,
        authority_status="accepted",
        source_kind="test_fixture",
    )

    # --- Scene 1: introduce characters and locations ---
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[0],
        event_type="character_state", entity_type="character", entity_id="林远",
        fact_key="alive", fact_value="alive",
    )
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[0],
        event_type="location_change", entity_type="character", entity_id="林远",
        fact_key="location", fact_value="北境",
    )
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[0],
        event_type="character_state", entity_type="character", entity_id="苏晚",
        fact_key="alive", fact_value="alive",
    )
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[0],
        event_type="location_change", entity_type="character", entity_id="苏晚",
        fact_key="location", fact_value="沧澜城",
    )
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[0],
        event_type="character_state", entity_type="character", entity_id="沧澜城城主",
        fact_key="alive", fact_value="alive",
    )

    # --- Scene 2: 林远 loses his right arm, loses the broken sword ---
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[1],
        event_type="character_state", entity_type="character", entity_id="林远",
        fact_key="missing_limb", fact_value="right_arm",
    )
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[1],
        event_type="item_change", entity_type="character", entity_id="林远",
        fact_key="has_item", fact_value="lost:断剑",
    )

    # --- Scene 3: 沧澜城城主 dies, 苏晚 acquires a secret ---
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[2],
        event_type="character_state", entity_type="character", entity_id="沧澜城城主",
        fact_key="alive", fact_value="dead",
    )
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[2],
        event_type="character_learns", entity_type="character", entity_id="苏晚",
        fact_key="secret_held_by", fact_value="城主死前告诉苏晚的密道位置",
    )

    # --- Scene 4: foreshadow planted ---
    log.log_event(
        **common, scene_id=SETUP_SCENE_IDS[3],
        event_type="foreshadow_plant", entity_type="foreshadow", entity_id="FS-007",
        fact_key="setup", fact_value="将军的刀上有毒",
    )

    log.session.commit()


# ---------------------------------------------------------------------------
# Test passages — known bugs and clean text
# ---------------------------------------------------------------------------
BUGGY_PASSAGES: list[BuggyPassage] = [
    # ---- BUG 1: missing limb — 林远 uses his right arm normally ----
    BuggyPassage(
        label="BUG-1:断臂角色正常使用右手",
        text=textwrap.dedent("""\
            林远站在北境的城墙上,凛冽的寒风吹过他破旧的斗篷。
            他右手握住剑柄,缓缓拔出腰间的长刀,目光冷峻地望向远方。
            "来吧,"他低声说道,"我已经等了很久。"
        """),
        expected_bug_count=1,
        bug_description="林远的右臂已断,但文中写'右手握住剑柄'",
    ),

    # ---- BUG 2: wrong location — 林远 shown in 沧澜城 instead of 北境 ----
    BuggyPassage(
        label="BUG-2:角色出现在错误地点",
        text=textwrap.dedent("""\
            沧澜城的夜晚总是格外安静。林远仍在沧澜城的集市上游荡,
            他的身影在月光下若隐若现。街边的小贩早已收摊,
            只剩几盏昏黄的灯笼还在风中摇晃。
        """),
        expected_bug_count=1,
        bug_description="林远应在北境,但文中写他'仍在沧澜城'",
    ),

    # ---- BUG 3: dead character acts alive — 沧澜城城主 speaks ----
    BuggyPassage(
        label="BUG-3:已死角色说话",
        text=textwrap.dedent("""\
            大殿之上,气氛凝重。苏晚站在台阶下方,等待宣判。
            沧澜城城主说道:"此事不可轻率,需要从长计议。"
            众人纷纷点头,表示赞同城主的决定。
        """),
        expected_bug_count=1,
        bug_description="沧澜城城主已死,但文中他在说话('说道')",
    ),

    # ---- BUG 4: character uses a lost item ----
    BuggyPassage(
        label="BUG-4:角色使用已丢失物品",
        text=textwrap.dedent("""\
            战场上硝烟弥漫。林远拿出断剑,剑身上的裂纹在火光中格外醒目。
            他将这柄跟随多年的兵器举过头顶,朝着敌阵冲去。
        """),
        expected_bug_count=1,
        bug_description="林远已失去断剑,但文中写'林远拿出断剑'",
    ),
]

CLEAN_PASSAGES: list[BuggyPassage] = [
    # ---- CLEAN 1: consistent text about 林远 in 北境, no right-arm use ----
    BuggyPassage(
        label="CLEAN-1:林远北境独白(无矛盾)",
        text=textwrap.dedent("""\
            北境的冬天来得比往年更早。林远裹紧了斗篷,用仅存的左手拢了拢衣领。
            断臂处的伤口在寒风中隐隐作痛,但他早已习惯了这种感觉。
            远处的狼烟再次升起,他知道,战争从未真正结束。
        """),
        expected_bug_count=0,
        bug_description="文本与角色状态完全一致,不应报警",
    ),

    # ---- CLEAN 2: 苏晚 alone in 沧澜城, no dead character present ----
    BuggyPassage(
        label="CLEAN-2:苏晚沧澜城独白(无矛盾)",
        text=textwrap.dedent("""\
            苏晚推开客栈的木窗,沧澜城的晨雾正缓缓散去。
            她想起城主临终前的嘱托,心中五味杂陈。
            "我一定会找到那条密道的,"她在心里暗暗发誓。
        """),
        expected_bug_count=0,
        bug_description="苏晚确实在沧澜城,提到城主用的是回忆口吻,不应视为城主存活",
    ),

    # ---- CLEAN 3: pure scenery, no character contradictions possible ----
    BuggyPassage(
        label="CLEAN-3:纯景物描写(无矛盾)",
        text=textwrap.dedent("""\
            北境的山脉在暮色中变成一道深紫色的剪影。
            积雪覆盖了每一棵松树,偶尔有几声鸟鸣从林间传出。
            溪流已经结冰,冰面下隐约可见流动的暗水。
        """),
        expected_bug_count=0,
        bug_description="纯景物描写,无角色动作,不应报警",
    ),

    # ---- CLEAN 4: mentions 林远 without triggering limb/location/item checks ----
    BuggyPassage(
        label="CLEAN-4:林远对话不涉及断臂动作(无矛盾)",
        text=textwrap.dedent("""\
            林远沉默了很久。北境的星空明亮得令人窒息。
            "有些事,"他终于开口,"失去之后才知道它的分量。"
            篝火噼啪作响,火星在夜风中飞舞。
        """),
        expected_bug_count=0,
        bug_description="林远说话且在北境,状态一致;提及失去但不涉及具体物品",
    ),
]


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _safe_div(a: float, b: float) -> float:
    return a / b if b > 0 else 0.0


def _f1(precision: float, recall: float) -> float:
    return _safe_div(2.0 * precision * recall, precision + recall)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.consistency_validation
class TestConsistencyValidation:
    """Blueprint §17 Action B — minimum verifiable kernel."""

    # -- helpers --

    @staticmethod
    def _setup(session) -> NarrativeEventLog:
        _seed_project(session)
        log = NarrativeEventLog(session)
        _seed_character_state(log)
        return log

    # -- main kernel test --

    def test_recall_precision_f1(self, session) -> None:
        """Run all buggy + clean passages and compute recall / precision / F1."""
        log = self._setup(session)

        all_passages = BUGGY_PASSAGES + CLEAN_PASSAGES
        total_known_bugs = sum(p.expected_bug_count for p in all_passages)

        true_positives = 0    # real bugs correctly flagged
        false_positives = 0   # clean text wrongly flagged
        false_negatives = 0   # real bugs missed

        results: list[str] = []

        for passage in all_passages:
            report = log.check_consistency(
                generated_text=passage.text,
                project_id=PROJECT_ID,
                scene_id=TARGET_SCENE_ID,
            )
            detected = len(report.violations)

            if passage.expected_bug_count > 0:
                # Buggy passage
                caught = min(detected, passage.expected_bug_count)
                true_positives += caught
                false_negatives += passage.expected_bug_count - caught
                # Any detections beyond the known bugs count as FP
                false_positives += max(0, detected - passage.expected_bug_count)
                status = "CAUGHT" if caught > 0 else "MISSED"
            else:
                # Clean passage
                false_positives += detected
                status = "CLEAN-OK" if detected == 0 else "FALSE-ALARM"

            detail_lines = []
            for v in report.violations:
                detail_lines.append(
                    f"      fact={v.fact_key}  expected={v.expected}  "
                    f"actual={v.actual}  entity={v.entity_id}  evidence={v.evidence}"
                )
            detail_str = "\n".join(detail_lines) if detail_lines else "      (none)"

            results.append(
                f"  [{status}] {passage.label}\n"
                f"    desc: {passage.bug_description}\n"
                f"    violations detected: {detected} (expected bugs: {passage.expected_bug_count})\n"
                f"    facts checked: {report.facts_checked}\n"
                f"    details:\n{detail_str}"
            )

        recall = _safe_div(true_positives, true_positives + false_negatives)
        precision = _safe_div(true_positives, true_positives + false_positives)
        f1 = _f1(precision, recall)

        # ---- Summary ----
        summary = (
            "\n" + "=" * 72 + "\n"
            "  Blueprint §17 Action B — Consistency Validation Report\n"
            "=" * 72 + "\n\n"
            + "\n\n".join(results)
            + "\n\n" + "-" * 72 + "\n"
            f"  Total known bugs:  {total_known_bugs}\n"
            f"  True positives:    {true_positives}\n"
            f"  False positives:   {false_positives}\n"
            f"  False negatives:   {false_negatives}\n"
            f"  Recall:            {recall:.2%}\n"
            f"  Precision:         {precision:.2%}\n"
            f"  F1 Score:          {f1:.2%}\n"
            + "-" * 72 + "\n"
        )
        print(summary)

        # Hard assertions — the minimum-kernel bar.
        # The current rule-based checker must catch at least 3 of 4 known bugs.
        assert recall >= 0.75, f"Recall {recall:.2%} below 75% floor"
        # Precision must be perfect on clean passages (no false alarms).
        assert precision >= 0.75, f"Precision {precision:.2%} below 75% floor"
        assert f1 >= 0.75, f"F1 {f1:.2%} below 75% floor"

    # -- individual bug-class tests for clear diagnostics --

    def test_bug1_missing_limb_usage(self, session) -> None:
        """BUG-1: 断臂角色正常使用右手 should be caught."""
        log = self._setup(session)
        passage = BUGGY_PASSAGES[0]
        report = log.check_consistency(
            generated_text=passage.text,
            project_id=PROJECT_ID,
            scene_id=TARGET_SCENE_ID,
        )
        assert not report.passed, f"BUG-1 was not caught: {passage.bug_description}"
        assert any(
            v.fact_key == "missing_limb" for v in report.violations
        ), "Expected a missing_limb violation"

    def test_bug2_wrong_location(self, session) -> None:
        """BUG-2: 角色出现在错误地点 should be caught."""
        log = self._setup(session)
        passage = BUGGY_PASSAGES[1]
        report = log.check_consistency(
            generated_text=passage.text,
            project_id=PROJECT_ID,
            scene_id=TARGET_SCENE_ID,
        )
        assert not report.passed, f"BUG-2 was not caught: {passage.bug_description}"
        assert any(
            v.fact_key == "location" for v in report.violations
        ), "Expected a location violation"

    def test_bug3_dead_character_speaks(self, session) -> None:
        """BUG-3: 已死角色说话 should be caught."""
        log = self._setup(session)
        passage = BUGGY_PASSAGES[2]
        report = log.check_consistency(
            generated_text=passage.text,
            project_id=PROJECT_ID,
            scene_id=TARGET_SCENE_ID,
        )
        assert not report.passed, f"BUG-3 was not caught: {passage.bug_description}"
        assert any(
            v.fact_key == "alive" for v in report.violations
        ), "Expected an alive violation for dead character"

    def test_bug4_lost_item_usage(self, session) -> None:
        """BUG-4: 角色使用已丢失物品 should be caught."""
        log = self._setup(session)
        passage = BUGGY_PASSAGES[3]
        report = log.check_consistency(
            generated_text=passage.text,
            project_id=PROJECT_ID,
            scene_id=TARGET_SCENE_ID,
        )
        assert not report.passed, f"BUG-4 was not caught: {passage.bug_description}"
        assert any(
            v.fact_key == "has_item" for v in report.violations
        ), "Expected a has_item violation for lost item"

    # -- clean passage tests (false-positive guard) --

    @pytest.mark.parametrize(
        "idx",
        range(len(CLEAN_PASSAGES)),
        ids=[p.label for p in CLEAN_PASSAGES],
    )
    def test_clean_passages_no_false_positives(self, session, idx: int) -> None:
        """Clean passages must produce zero violations (no false positives)."""
        log = self._setup(session)
        passage = CLEAN_PASSAGES[idx]
        report = log.check_consistency(
            generated_text=passage.text,
            project_id=PROJECT_ID,
            scene_id=TARGET_SCENE_ID,
        )
        assert report.passed, (
            f"False positive on {passage.label}: "
            f"{[(v.fact_key, v.expected, v.actual) for v in report.violations]}"
        )

    # -- state projection sanity checks --

    def test_projected_state_is_correct(self, session) -> None:
        """Verify the seeded events produce the expected character states."""
        log = self._setup(session)

        lin_state = log.project_character_state("林远", PROJECT_ID, up_to_scene_seq=4)
        assert lin_state.get("alive") == "alive"
        assert lin_state.get("location") == "北境"
        assert lin_state.get("missing_limb") == "right_arm"
        assert lin_state.get("has_item") == "lost:断剑"

        su_state = log.project_character_state("苏晚", PROJECT_ID, up_to_scene_seq=4)
        assert su_state.get("alive") == "alive"
        assert su_state.get("location") == "沧澜城"

        mayor_state = log.project_character_state("沧澜城城主", PROJECT_ID, up_to_scene_seq=4)
        assert mayor_state.get("alive") == "dead"

    def test_facts_checked_count(self, session) -> None:
        """check_consistency should examine all checkable facts across all characters."""
        log = self._setup(session)
        # Use a minimal clean text
        report = log.check_consistency(
            generated_text="空旷的原野上什么也没有。",
            project_id=PROJECT_ID,
            scene_id=TARGET_SCENE_ID,
        )
        # 林远 has 4 checkable facts: alive, location, missing_limb, has_item
        # 苏晚 has 2 checkable facts: alive, location
        # 沧澜城城主 has 1 checkable fact: alive
        # Total: 7
        assert report.facts_checked == 7, (
            f"Expected 7 facts checked, got {report.facts_checked}"
        )
        assert report.passed, "Minimal text should not trigger any violations"
