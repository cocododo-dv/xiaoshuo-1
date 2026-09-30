"""雪花场景计划 → 场景卡的写作简报与节拍（叶子模块：纯函数，不引用任何服务）。

物化（``_build_chaptered_outline_plan``）与回流（``_scene_card_resync_patch``）共用这里的配方——两个写入方
各算一套时，刚物化完的每一场都会被报成「待同步」。这两个函数原先寄住在 v1 规划器里（B06-10）；v1 规划器
退役之后它们是 v2 工作台唯一的家。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.value_coercion import coerce_string_list


def scene_writer_brief(scene_type: str, detail: dict[str, Any]) -> dict[str, Any]:
    common = {
        # 阶段 B：挫折 / 胜利以主角衡量（Ingermanson）；物化时由角色摘要表带入，没有就是 None。
        "protagonist_hint": detail.get("protagonist_hint"),
        "protagonist_character_id": detail.get("protagonist_character_id"),
        # 阶段 C / N：呈现方式（full / summary / skip）——summary 对两种形态都合法，skip 只给反应场。
        "rendering_mode": str(detail.get("rendering_mode") or "full"),
        # 阶段 N：作者的破例理由——结构简报带给起草与 QC，「未规划」的三拍是故意的。
        "exception_reason": detail.get("exception_reason") or "",
        "tension_target": detail.get("tension_target"),
        "function_tag": detail.get("function_tag"),
        "involved_foreshadowing": detail.get("involved_foreshadowing") or detail.get("involved_foreshadowing_json") or [],
        "cost_requirement": detail.get("cost_requirement"),
        # 阶段 J：原著场景表的时间戳——连续性锚，也进结构简报。
        "story_time": detail.get("story_time") or "",
        "causal_prerequisite_scene_id": detail.get("causal_prerequisite_scene_id"),
        "downstream_obligations": detail.get("downstream_obligations") or detail.get("downstream_obligations_json") or [],
    }
    # 阶段 X：一场可以接着另一组三拍（阶段 I / N）。过去物化只写主形态那三个键，后续三拍要等一次回流才
    # 进得了场景卡——刚物化完的场于是没有 ``Follow-up beats``，还被当场报成「待同步」。与回流同一口径：写了才带。
    secondary = ("goal", "conflict", "setback") if scene_type == "reactive" else ("reaction", "dilemma", "decision")
    common.update({key: str(detail.get(key) or "") for key in secondary if str(detail.get(key) or "").strip()})
    if scene_type == "reactive":
        return {
            "source": "snowflake_method",
            "scene_form": "reactive",
            # 2026-09-13 阶段 F：不再用样板句冒充作者没写的坩埚 / 三拍 / 必须隐瞒 / 读者情绪——
            # 结构简报会把空槽位明写成「未规划」，硬 QC 只核验作者真正写下的事实。
            "scene_crucible": detail.get("scene_crucible") or detail.get("crucible") or "",
            "reaction": detail.get("reaction") or "",
            "dilemma": detail.get("dilemma") or "",
            "decision": detail.get("decision") or "",
            "must_reveal": detail.get("decision") or "",
            "must_withhold": detail.get("must_withhold") or "",
            "expected_reader_emotion": detail.get("expected_reader_emotion") or "",
            "timebox": detail.get("target_length_band") or "medium",
            "next_scene_pull": detail.get("hook") or "",
            **common,
        }
    return {
        "source": "snowflake_method",
        "scene_form": "proactive",
        "scene_crucible": detail.get("scene_crucible") or detail.get("crucible") or "",
        "goal": detail.get("goal") or "",
        "conflict": detail.get("conflict") or "",
        "setback": detail.get("setback") or "",
        "must_reveal": detail.get("setback") or "",
        "must_withhold": detail.get("must_withhold") or "",
        "expected_reader_emotion": detail.get("expected_reader_emotion") or "",
        "timebox": detail.get("target_length_band") or "medium",
        "next_scene_pull": detail.get("hook") or "",
        **common,
    }


def beats_from_detail(scene_type: str, detail: dict[str, Any]) -> list[str]:
    # 阶段 I：一场可以接着另一组三拍（原著 Goldilocks 场景 1：目标 / 冲突 / 挫折之后紧接反应 / 两难 / 决定）——
    # 主形态的三拍在前，次要三拍在后，节拍顺序就是它们在页面上发生的顺序。
    primary = ["reaction", "dilemma", "decision"] if scene_type == "reactive" else ["goal", "conflict", "setback"]
    secondary = ["goal", "conflict", "setback"] if scene_type == "reactive" else ["reaction", "dilemma", "decision"]
    return [str(detail.get(key) or "").strip() for key in [*primary, *secondary] if str(detail.get(key) or "").strip()]


#: 构思里的场景题名超过这个长度就当它是摘要（09 没单独起题名时 title 会跟着摘要走）
SCENE_TITLE_SEED_MAX_CHARS = 40


def real_scene_title(title: Any, summary: Any) -> str:
    """构思里**真起过**的场景短题名；空、和摘要一字不差、或长得像一句摘要的都不算。"""
    value = " ".join(str(title or "").split())
    if not value or value == " ".join(str(summary or "").split()) or len(value) > SCENE_TITLE_SEED_MAX_CHARS:
        return ""
    return value


def scene_title_seed(detail: dict[str, Any]) -> dict[str, str]:
    """物化写进场景卡简报的题名键：``title``（目录显示用，作者可在台子上改）+ ``seeded_title``
    （这次播下去的值——之后题名还等于它，就说明作者没改过，回流 / 重新物化可以跟着构思走）。"""
    real = real_scene_title(detail.get("title"), detail.get("summary"))
    # seeded_title 总是写（哪怕是空串）：这个键在，就说明这张卡是阶段 X 之后播的，题名可以参与「待同步」比较
    return {"title": real, "seeded_title": real} if real else {"seeded_title": ""}


def followed_scene_title(previous_brief: dict[str, Any], real: str) -> dict[str, str]:
    """回流 / 重新物化时题名怎么走：作者在台子上改过的（≠ 上次播的）原样保留，否则跟构思。"""
    current = str(previous_brief.get("title") or "").strip()
    seeded = str(previous_brief.get("seeded_title") or "").strip()
    if current and current != seeded:
        return {"title": current, "seeded_title": real}
    return {"title": real, "seeded_title": real} if real else {"seeded_title": ""}


def scene_card_beats(scene_type: str, detail: dict[str, Any]) -> list[str]:
    """``SceneCard.beats_json`` 的唯一配方，物化与 resync 共用。

    规划行自带节拍就用它；否则按场景类型从 goal/conflict/setback（主动）或
    reaction/dilemma/decision（反应）推导（``snowflake_scene_brief.beats_from_detail``）。hook 不进节拍：它已经单独落在 ``SceneCard.hook`` 与 brief 的
    ``next_scene_pull`` 上，resync 曾额外拼进去，正是「刚物化完就待同步」的来源。
    """
    beats = coerce_string_list(detail.get("beats_json"))
    return beats or beats_from_detail(scene_type, detail)
