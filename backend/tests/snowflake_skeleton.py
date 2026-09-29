"""测试用的雪花「骨架」生成器：按大纲逐行拼出十步的规范草稿（不调模型）。

假生成已退役：产品里每个生成节点都 fail-closed。只回归物化 / 失效 / 回流 / 收口链路、不关心生成质量的
雪花用例用 ``install_skeleton_snowflake`` 把 ``SnowflakeWorkspaceLLMService.generate_step`` 换成这里的骨架直通。
这套拼句原先是 v1 规划器 ``SnowflakePlannerService._build_artifact_json``（在生产路由上可达的罐头生成，违反
fail-closed）；v1 规划器与它的路由在 2026-09-30 退役（R9），骨架只作为测试夹具留在这里。

夹具形状要求（回流 / 目录用例依赖）：每章一场主动加一场反应——需要一个反应场样本。
"""

from __future__ import annotations

from typing import Any

from novel_system.db.models import StoryProject
from novel_system.services.errors import DomainError


def build_skeleton_draft(project: Any, step_key: str, latest_by_step: dict[str, Any]) -> dict[str, Any]:
    """一步的骨架草稿（``latest_by_step`` 是工作台的 step_run 映射）。"""
    return _build_outline_based_artifact(project, step_key, dict(latest_by_step), _outline_lines(project.outline_text))


def install_skeleton_snowflake(monkeypatch, *, llm_enabled: bool = False) -> None:
    """把雪花 ``generate_step`` 换成骨架直通。

    ``llm_enabled=False``（默认）：LLM 关闭的用例走骨架；显式开了 LLM 的「live」用例（自带 LLM 替身）
    委托真实 ``generate_step``——同文件里的 fail-closed 用例照旧走真实路径。
    ``llm_enabled=True``：开 ``NOVEL_SYSTEM_LLM_ENABLED`` 过路由闸，所有整步生成一律走骨架。
    """
    from novel_system.services.hash_engine import normalize
    from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService, WorkspaceLLMResult
    from novel_system.settings import get_settings

    if llm_enabled:
        monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    original_generate_step = SnowflakeWorkspaceLLMService.generate_step

    def fake_generate_step(self, *, project, step_key, latest_by_step, **kwargs):
        if not llm_enabled and get_settings().llm_enabled:
            return original_generate_step(
                self, project=project, step_key=step_key, latest_by_step=latest_by_step, **kwargs
            )
        payload = build_skeleton_draft(project, step_key, latest_by_step)
        return WorkspaceLLMResult(source="llm", llm_call_id=None, payload=normalize(payload))

    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "generate_step", fake_generate_step)


def _build_outline_based_artifact(
    project: StoryProject,
    step_key: str,
    latest_by_step: dict[str, Any],
    outline_lines: list[str],
) -> dict[str, Any]:
    lines = _ensure_outline_lines(project, outline_lines)
    zh = _looks_chinese(" ".join([project.title or "", project.genre or "", *lines]))
    lead = "主角" if zh else "the protagonist"
    final_pressure = lines[-1]
    title = str(project.title or ("未命名小说" if zh else "Untitled Novel")).strip()
    genre = str(project.genre or ("长篇小说" if zh else "Novel")).strip()

    if step_key == "book_brief":
        if zh:
            return {
                "category": genre,
                "target_reader": f"喜欢{genre}、具体压力、人物选择和代价递增的读者。",
                "story_kind": f"围绕《{title}》展开的故事：{lines[0]}，但{final_pressure}",
                "delight_reason": "读者会持续追问下一步行动，因为每个线索都带来新的阻力、代价或关系变化。",
                "genre_promise": f"以{genre}的阅读快感推进，保留项目大纲中的事实和语言。",
                "expected_reader_emotion": "紧张、好奇、担心人物付出代价，并愿意继续翻页。",
                "safety_rules": [
                    "参考材料只抽象方法、节奏和结构。",
                    "不复制参考文本的人物、设定、桥段或标志性表达。",
                ],
            }
        return {
            "category": genre,
            "target_reader": f"Readers who want {genre}, concrete pressure, costly choices, and escalating reveals.",
            "story_kind": f"A {genre} shaped by this outline: {lines[0]}, but {final_pressure}.",
            "delight_reason": "Each clue, choice, or reversal should create a new cost or relationship change.",
            "genre_promise": f"Deliver the {genre} pleasure while preserving the project's own facts and language.",
            "expected_reader_emotion": "Curiosity, pressure, concern for the cost, and a strong need to keep reading.",
            "safety_rules": [
                "Use reference materials only for abstract craft, rhythm, and structure.",
                "Do not copy source characters, settings, plot beats, or signature phrasing.",
            ],
        }

    if step_key == "one_sentence_summary":
        if zh:
            return {"summary": f"{lead}必须处理“{lines[0]}”，但“{final_pressure}”让每一步都付出更高代价。"}
        return {"summary": f"In {title}, {lead} must face {lines[0]}, but {final_pressure}."}

    if step_key == "one_paragraph_summary":
        spine = _five_spine(lines)
        if zh:
            sentences = [
                f"{spine[0]}迫使{lead}进入无法回避的处境。",
                f"{spine[1]}制造第一次不可逆转的代价。",
                f"{spine[2]}改变{lead}对局面的判断。",
                f"{spine[3]}把所有人推向更公开、更危险的冲突。",
                f"{spine[4]}让结局必须在真相、关系和代价之间完成选择。",
            ]
            return {
                # 三幕灾难不入库——读时由 derive_three_act(sentences) 派生（P1-2）。
                "sentences": sentences,
                "moral_premise": "逃避代价只会扩大伤害；承担选择才可能结束伤害。",
            }
        sentences = [
            f"{spine[0]} forces {lead} into a problem that cannot be ignored.",
            f"{spine[1]} creates the first irreversible cost.",
            f"{spine[2]} changes what {lead} believes about the situation.",
            f"{spine[3]} pushes the conflict into a more public and dangerous stage.",
            f"{spine[4]} forces the ending to choose between truth, relationship, and cost.",
        ]
        return {
            # Three-act disasters are not persisted — derived on read from sentences (P1-2).
            "sentences": sentences,
            "moral_premise": "Avoiding cost preserves harm; choosing with cost creates change.",
        }

    if step_key == "character_sheets":
        return {"characters": _outline_characters(project, lines, zh=zh)}

    if step_key == "short_synopsis":
        return {"paragraphs": _synopsis_paragraphs(lines, zh=zh, count=5)}

    if step_key == "character_synopses":
        return {
            "characters": [
                {
                    **character,
                    "synopsis": (
                        f"{character['display_name']} is pressured by {lines[0]} and must act while {final_pressure} changes the cost."
                        if not zh
                        else f"{character['display_name']}被“{lines[0]}”卷入压力，并在“{final_pressure}”带来的代价中做选择。"
                    ),
                }
                for character in _outline_characters(project, lines, zh=zh)
            ]
        }

    if step_key == "long_synopsis":
        short = latest_by_step.get("short_synopsis")
        paragraphs = list(((short.draft_json or {}) if short else {}).get("paragraphs") or _synopsis_paragraphs(lines, zh=zh, count=5))
        result = {"paragraphs": paragraphs + _synopsis_paragraphs(lines[::-1], zh=zh, count=max(0, 4 - len(paragraphs)))}
        return result

    if step_key == "character_bibles":
        return {"characters": [_outline_character_bible(character, lines, zh=zh) for character in _outline_characters(project, lines, zh=zh)]}

    if step_key == "scene_list":
        return {"scenes": _outline_scene_list(project, lines, zh=zh)}

    if step_key == "scene_details":
        scene_list = latest_by_step.get("scene_list")
        scenes = list(((scene_list.draft_json or {}) if scene_list else {}).get("scenes") or _outline_scene_list(project, lines, zh=zh))
        return {"scenes": [_outline_scene_detail(scene, index, lines, zh=zh) for index, scene in enumerate(scenes, start=1)]}

    raise DomainError("SNOWFLAKE_STEP_NOT_FOUND", "unknown snowflake step", status_code=404)


def _ensure_outline_lines(project: StoryProject, outline_lines: list[str]) -> list[str]:
    lines = [str(line or "").strip() for line in outline_lines if str(line or "").strip()]
    if lines:
        return lines
    title = str(project.title or "").strip()
    return [title or "The protagonist faces a costly change."]


def _looks_chinese(text: str) -> bool:
    cjk_count = sum(1 for char in str(text or "") if "\u4e00" <= char <= "\u9fff")
    return cjk_count >= 4


def _five_spine(lines: list[str]) -> list[str]:
    spine = [str(line or "").strip() for line in lines if str(line or "").strip()]
    while len(spine) < 5:
        spine.append(spine[-1] if spine else "A costly pressure turn")
    return spine[:5]


def _synopsis_paragraphs(lines: list[str], *, zh: bool, count: int) -> list[str]:
    spine = _five_spine(lines)
    paragraphs = []
    for index in range(count):
        point = spine[index % len(spine)]
        next_point = spine[(index + 1) % len(spine)]
        if zh:
            paragraphs.append(
                f"{point}。这一段必须把人物行动、阻力和代价写清楚，并让“{next_point}”成为下一段不得不发生的压力。"
            )
        else:
            paragraphs.append(
                f"{point}. This section turns the outline point into action, opposition, and cost, making {next_point} the next necessary pressure."
            )
    return paragraphs


def _outline_characters(project: StoryProject, lines: list[str], *, zh: bool) -> list[dict[str, Any]]:
    names = [("主角", "主角"), ("盟友", "盟友"), ("对手", "对手")] if zh else [
        ("Lead", "lead"),
        ("Ally", "ally"),
        ("Opposition", "opposition"),
    ]
    goal = lines[0]
    pressure = lines[1] if len(lines) > 1 else lines[0]
    cost = lines[-1]
    result = []
    for index, (display_name, role) in enumerate(names, start=1):
        character_id = f"{project.project_id}_CHAR{index:02d}"
        if zh:
            item = {
                "character_id": character_id,
                "display_name": display_name,
                "role": role,
                "goal": f"围绕“{goal}”采取行动",
                "ambition": f"在《{project.title}》中守住最重要的关系或信念",
                "values": ["真相比安全更重要", "保护也必须承担代价"],
                "conflict": f"“{pressure}”让目标和关系发生冲突",
                "epiphany": f"必须在“{cost}”面前做出有代价的选择",
                "one_sentence_summary": f"{display_name}必须处理“{goal}”，但“{cost}”改变了代价。",
                "one_paragraph_summary": f"{display_name}从“{goal}”进入故事，被“{pressure}”持续阻挡，最终必须面对“{cost}”。",
            }
        else:
            item = {
                "character_id": character_id,
                "display_name": display_name,
                "role": role,
                "goal": f"Act on {goal}",
                "ambition": f"Protect the central relationship or belief inside {project.title}",
                "values": ["truth over safety", "protection must carry a cost"],
                "conflict": f"{pressure} puts the goal and relationship at odds",
                "epiphany": f"The character must choose under the cost of {cost}",
                "one_sentence_summary": f"{display_name} must act on {goal}, but {cost} changes the price.",
                "one_paragraph_summary": f"{display_name} enters through {goal}, is blocked by {pressure}, and must finally face {cost}.",
            }
        result.append(item)
    return result


def _outline_character_bible(character: dict[str, Any], lines: list[str], *, zh: bool) -> dict[str, Any]:
    cost = lines[-1]
    home = lines[0]
    if zh:
        return {
            **character,
            "physical_profile": {"age": "", "height": "", "appearance": f"外貌应服务于“{home}”带来的生活压力", "style": ""},
            "personality_profile": {
                "strongest_trait": "能在压力下行动",
                "weakest_trait": "容易把沉默误认为保护",
                "humor": "",
                "preferences": [],
            },
            "environment_profile": {"home": home, "family_background": "", "education": "", "work": "", "relationships": ""},
            "psychological_profile": {
                "best_memory": "",
                "worst_memory": "",
                "deepest_fear": f"{cost}会摧毁现有关系",
                "greatest_hope": "真相和关系可以同时被修复",
                "philosophy": "选择必须承担代价",
                "self_image": "",
                "public_image": "",
                "character_arc": f"从回避代价变成愿意面对“{cost}”。",
            },
        }
    return {
        **character,
        "physical_profile": {"age": "", "height": "", "appearance": f"Appearance should reflect the pressure of {home}", "style": ""},
        "personality_profile": {
            "strongest_trait": "Acts under pressure",
            "weakest_trait": "Mistakes silence for protection",
            "humor": "",
            "preferences": [],
        },
        "environment_profile": {"home": home, "family_background": "", "education": "", "work": "", "relationships": ""},
        "psychological_profile": {
            "best_memory": "",
            "worst_memory": "",
            "deepest_fear": f"{cost} will destroy the current relationships",
            "greatest_hope": "Truth and relationship can both be repaired",
            "philosophy": "Every meaningful choice carries a cost",
            "self_image": "",
            "public_image": "",
            "character_arc": f"Moves from avoiding cost to facing {cost}.",
        },
    }


def _outline_scene_list(project: StoryProject, lines: list[str], *, zh: bool) -> list[dict[str, Any]]:
    chapter_count = max(1, min(project.target_chapter_count or len(lines) or 2, 4))
    spine = list(lines)
    while len(spine) < chapter_count:
        spine.append(spine[-1])
    scenes: list[dict[str, Any]] = []
    for chapter_index in range(1, chapter_count + 1):
        chapter_id = f"{project.project_id}_CH{chapter_index:02d}"
        chapter_point = spine[chapter_index - 1]
        for scene_index in range(1, 3):
            # v1 规划器只为测试保留（批次二减法候选）：每章一场主动加一场反应是**测试夹具的形状**——
            # 回流 / 目录测试要有一个反应场样本。产品路径（v2）默认主动、不按奇偶交替。
            primary_form = "proactive" if scene_index == 1 else "reactive"
            summary = (
                f"{'行动' if scene_index == 1 else '反应'}：{chapter_point}"
                if zh
                else f"{'Action' if scene_index == 1 else 'Reaction'}: {chapter_point}"
            )
            scenes.append(
                {
                    "scene_id": f"{chapter_id}_SC{scene_index:02d}",
                    "chapter_id": chapter_id,
                    "chapter_title": f"{'第' + str(chapter_index) + '章' if zh else 'Chapter ' + str(chapter_index)}: {chapter_point[:36]}",
                    "chapter_goal": f"{'推进' if zh else 'Advance'}: {chapter_point}",
                    "scene_seq": scene_index,
                    "pov_character_id": f"{project.project_id}_CHAR01",
                    "onstage_chars_json": [f"{project.project_id}_CHAR01", f"{project.project_id}_CHAR02"],
                    "summary": summary,
                    "primary_form": primary_form,
                    "scene_type": primary_form,
                    "chapter_role": "承压" if scene_index == 1 and zh else "转向" if zh else ("pressure" if scene_index == 1 else "turn"),
                    "location": chapter_point,
                    "crucible": f"{chapter_point} cannot be avoided without locking in a bigger loss.",
                }
            )
    return scenes


def _outline_scene_detail(scene: dict[str, Any], index: int, lines: list[str], *, zh: bool) -> dict[str, Any]:
    # 阶段 S：默认主动，不按行号奇偶交替（原著：反应场是少数）。
    primary_form = str(scene.get("primary_form") or scene.get("scene_type") or "proactive").strip().lower()
    if primary_form not in {"proactive", "reactive"}:
        primary_form = "proactive"
    summary = str(scene.get("summary") or lines[(index - 1) % len(lines)]).strip()
    base = {
        **scene,
        "primary_form": primary_form,
        "scene_type": primary_form,
        "location": scene.get("location") or summary,
        "target_length_band": scene.get("target_length_band") or "medium",
        "must_include_text": scene.get("must_include_text") or summary,
        "exit_change": scene.get("exit_change") or (f"{summary} leaves a new cost, clue, or relationship shift."),
        "hook": scene.get("hook") or (f"The result of {summary} forces the next move."),
        "triage_status": str(scene.get("triage_status") or "").strip(),
        "triage_notes": str(scene.get("triage_notes") or "").strip(),
    }
    if primary_form == "reactive":
        base.update(
            {
                "scene_crucible": scene.get("scene_crucible") or scene.get("crucible") or f"Retreating after {summary} would make the previous loss permanent.",
                "crucible": scene.get("crucible") or scene.get("scene_crucible") or f"Retreating after {summary} would make the previous loss permanent.",
                "reaction": scene.get("reaction") or f"The POV character absorbs the emotional damage from {summary} before analysis.",
                "dilemma": scene.get("dilemma") or f"One choice protects a relationship but buries the truth; the other exposes truth but costs safety.",
                "decision": scene.get("decision") or f"The POV character chooses the costly next action that follows from {summary}.",
                "beats_json": scene.get("beats_json") or ["reaction", "dilemma", "decision"],
            }
        )
    else:
        base.update(
            {
                "scene_crucible": scene.get("scene_crucible") or scene.get("crucible") or f"The goal inside {summary} is trapped by a clock, relationship cost, or irreversible loss.",
                "crucible": scene.get("crucible") or scene.get("scene_crucible") or f"The goal inside {summary} is trapped by a clock, relationship cost, or irreversible loss.",
                "goal": scene.get("goal") or f"Secure a concrete result from {summary} before the chance closes.",
                "conflict": scene.get("conflict") or f"The character tries a direct move, a workaround, and a risky reveal; each meets stronger resistance.",
                "setback": scene.get("setback") or f"The character gains something from {summary}, but the cost creates a worse and more personal problem.",
                "beats_json": scene.get("beats_json") or ["goal", "conflict", "setback"],
            }
        )
    return base


def _outline_lines(outline_text: str) -> list[str]:
    lines = [line.strip(" -\t\r\n.。") for line in str(outline_text or "").splitlines() if line.strip()]
    return lines or ["旧信把主角带回雨城", "旧案牵出家族秘密", "主角公开真相"]
