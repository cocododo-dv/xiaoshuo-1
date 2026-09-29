"""雪花各步的规则诊断：完整度、步骤压力、逐场三拍的缺失 / 占位判定与建议。

规则层只认缺失与占位（阶段 B / H）；关键词与短语表只给建议，不改状态、不扣分——质量判断交给 LLM 分诊与作者。
2026-09-30 从 ``snowflake_steps.py`` 拆出（B06-09），``snowflake_steps`` 仍然转出这里的每一个名字。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.value_coercion import coerce_string_list, has_value
from novel_system.services.snowflake_step_catalog import get_step_definition
from novel_system.services.snowflake_step_guidance import _FIELD_HELP
from novel_system.services.snowflake_step_drafts import derive_three_act


def step_completeness(step_key: str, draft: dict[str, Any] | None) -> dict[str, Any]:
    payload = draft if isinstance(draft, dict) else {}
    missing_fields = _missing_fields_for_step(step_key, payload)
    total_count = _total_fields_for_step(step_key, payload)
    filled_count = max(0, total_count - len(missing_fields))
    return {
        "filled_count": filled_count,
        "total_count": total_count,
        "missing_fields": missing_fields,
    }


def diagnose_step_pressure(step_key: str, draft: dict[str, Any] | None) -> dict[str, Any]:
    payload = draft if isinstance(draft, dict) else {}
    if step_key == "scene_details":
        return _diagnose_scene_step_pressure(step_key, payload)

    completeness = step_completeness(step_key, payload)
    missing_fields = completeness.get("missing_fields") or []
    flags = [f"missing_{_flag_key(field)}" for field in missing_fields]
    strengths: list[str] = []
    fix_steps: list[str] = []

    # 2026-09-13 阶段 H（雪花评估第二轮）：关键词与短语表只能给**建议**，不再改状态、不再扣分——
    # 「但 / 却 / cost」这类标记验证不了灾难链，泛泛短语表也判不了一句话是否有压力；把它们当旗标，
    # 合规的产出会被判「需修补」再回灌给模型去「修」。规则层只认缺失（完整度）与数量契约。
    if step_key == "book_brief":
        target_reader = _text(payload.get("target_reader"))
        story_kind = _text(payload.get("story_kind"))
        delight_reason = _text(payload.get("delight_reason"))
        genre_promise = _text(payload.get("genre_promise"))
        expected_emotion = _text(payload.get("expected_reader_emotion"))
        if target_reader and _looks_generic(target_reader, min_chars=12):
            fix_steps.append("建议：把目标读者收窄成可感知的读者承诺，不要只写宽泛类型。")
        elif target_reader:
            strengths.append("目标读者已经能作为可用读者承诺")
        if any(
            text and _looks_generic(text, min_chars=12)
            for text in (story_kind, delight_reason, genre_promise)
        ):
            fix_steps.append("建议：把故事类型、爽点和类型承诺落到具体压力、阻力与代价上。")
        elif story_kind and delight_reason and genre_promise:
            strengths.append("故事压力和类型承诺已经连上")
        if expected_emotion and _looks_generic(expected_emotion, min_chars=8):
            fix_steps.append("建议：写清楚读者在压力升级中持续感到的情绪。")
    elif step_key == "one_sentence_summary":
        # 阶段 B：一句话的契约是「主角必须目标，但阻力」——看要素，不看字数。提示词要求 40 字以内，
        # 旧规则却把 28 字以下一律判空泛，合规的 logline 必被标弱、再被回灌给模型去「修」。
        summary = _text(payload.get("summary"))
        if summary and (_looks_generic(summary, min_chars=10) or not _has_pressure_turn(summary)):
            fix_steps.append("建议：把主角、目标、阻力和代价压缩进一句因果句。")
        elif summary:
            strengths.append("一句话已经带出可用的压力转折")
    elif step_key == "one_paragraph_summary":
        sentences = [_text(item) for item in payload.get("sentences") or [] if _text(item)]
        if len(sentences) < 5:
            flags.append("five_sentence_spine_incomplete")
            fix_steps.append("补齐五句话：开局、三次灾难和结局方向。")
        disaster_text = " ".join(str(item or "") for item in derive_three_act(payload).values())
        if sentences and (_looks_generic(disaster_text, min_chars=12) or not _has_pressure_turn(disaster_text)):
            fix_steps.append("建议：让每次灾难都迫使承诺、价值转变或不可逆升级。")
        elif sentences:
            strengths.append("三幕灾难链已经有可见压力")
    elif step_key in {"character_sheets", "character_synopses", "character_bibles"}:
        characters = [item for item in payload.get("characters") or [] if isinstance(item, dict)]
        if not characters:
            flags.append("character_pressure_missing")
            fix_steps.append("至少补入主角、对手/阻力，以及一个能承载压力的盟友或映照角色。")
        # 阶段 H：留白即合法——原著的角色表满是「尚未定义」，配角甚至只有一行定位。只有主角 / 对手
        # 空着才提醒；写了但泛泛只给建议；角色全档案的压力文本读嵌套的心理 / 性格档，不再读被归一化
        # 搬走的顶层键（那个错位让每个全档案角色永远「压力不足」）。
        soft: list[str] = []
        for index, character in enumerate(characters, start=1):
            label = _text(character.get("display_name") or character.get("name") or f"character_{index}")
            pressure_text = _character_pressure_text(character)
            if not pressure_text:
                if is_lead_role(character.get("role")):
                    soft.append(label)
                continue
            if _looks_generic(pressure_text, min_chars=12) or not _has_pressure_turn(pressure_text):
                soft.append(label)
            else:
                strengths.append(f"{label} 已经有目标、冲突和变化压力")
        if soft:
            fix_steps.append("建议：把 " + "、".join(soft[:4]) + " 的具体目标、阻挡力量、价值冲突和变化再压实；配角可以留白。")
        if step_key == "character_sheets":
            # 阶段 D：书里的角色表还有一句话/一段话故事线，价值观要「没有什么比___更重要」写 2–3 条且互相有张力。
            # 这些是建议，不是旗标——缺了不降状态，只提醒。
            thin = [
                _text(character.get("display_name") or character.get("name") or f"character_{index}")
                for index, character in enumerate(characters[:4], start=1)
                if len([item for item in coerce_string_list(character.get("values")) if _text(item)]) < 2
                or not _text(character.get("one_sentence_summary"))
            ]
            if thin:
                fix_steps.append(
                    "建议：给 " + "、".join(thin) + " 补上一句话故事线，并把价值观写成至少两条互相有张力的「没有什么比___更重要」。"
                )
    elif step_key in {"short_synopsis", "long_synopsis"}:
        paragraphs = [_text(item) for item in payload.get("paragraphs") or [] if _text(item)]
        if not paragraphs:
            flags.append("synopsis_missing")
            fix_steps.append("把上一层扩成因果相连的压力节点。")
        elif not _has_pressure_turn(" ".join(paragraphs)):
            fix_steps.append("建议：加入可见反转、上升代价，以及会改变下一段目标的转向。")
        else:
            strengths.append("梗概已经包含压力升级")
    elif step_key == "scene_list":
        scenes = [item for item in payload.get("scenes") or [] if isinstance(item, dict)]
        if not scenes:
            flags.append("scene_list_missing")
            fix_steps.append("按顺序列出具体场景，并让每场都有视角压力和结果/变化。")
        # chapter_role 本来就是「起疑 / 取证 / 灾难一」这样的短标签，只要求非空、不是泛泛短语。
        weak_scenes = [
            str(scene.get("scene_id") or index)
            for index, scene in enumerate(scenes, start=1)
            if _looks_generic(_text(scene.get("summary")), min_chars=6)
            or _looks_generic(_text(scene.get("chapter_role")), min_chars=2)
        ]
        if weak_scenes:
            fix_steps.append("建议：给每个场景明确职责——什么改变、谁在阻挡、为什么下一场必须发生（" + "、".join(weak_scenes[:5]) + "）。")
        if scenes and not weak_scenes:
            strengths.append("场景列表已经有可用职责")
        fix_steps.extend(_repeated_crucible_advice(scenes))

    for missing_field in missing_fields[:3]:
        fix_steps.append(f"补齐必填字段：{_field_display_label(missing_field)}。")

    return _pressure_result(step_key, flags=flags, fix_steps=fix_steps, strengths=strengths)


def _missing_fields_for_step(step_key: str, draft: dict[str, Any]) -> list[str]:
    if step_key == "scene_details":
        scenes = draft.get("scenes") if isinstance(draft.get("scenes"), list) else []
        if not scenes:
            return ["scenes"]
        missing: list[str] = []
        for index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                missing.append(f"scenes[{index}]")
                continue
            scene_missing = _missing_scene_detail_fields(scene)
            missing.extend(f"{scene.get('scene_id') or index}.{field}" for field in scene_missing)
        return missing

    fields = get_step_definition(step_key).get("editor", {}).get("fields") or []
    missing = []
    for field in fields:
        key = str(field.get("key") or "")
        if field.get("optional"):
            continue  # 可选字段空着不算缺失（01 叙述人称、07 章表）
        if key and not has_value(draft.get(key)):
            missing.append(key)
    return missing


def _total_fields_for_step(step_key: str, draft: dict[str, Any]) -> int:
    if step_key == "scene_details":
        scenes = draft.get("scenes") if isinstance(draft.get("scenes"), list) else []
        if not scenes:
            return 1
        total = 0
        for scene in scenes:
            if not isinstance(scene, dict):
                total += 1
                continue
            scene_type = str(scene.get("primary_form") or scene.get("scene_type") or "proactive").strip().lower()
            total += 4 if scene_type in {"proactive", "reactive"} else 1
        return total
    return len(get_step_definition(step_key).get("editor", {}).get("fields") or [])


def _diagnose_scene_step_pressure(step_key: str, draft: dict[str, Any]) -> dict[str, Any]:
    scenes = [scene for scene in draft.get("scenes") or [] if isinstance(scene, dict)]
    if not scenes:
        return _pressure_result(
            step_key,
            flags=["missing_scenes"],
            fix_steps=["物化前先补出场景规划，让每场都有压力和可见转折。"],
            strengths=[],
        )
    diagnoses = [diagnose_scene_detail(scene, index=index) for index, scene in enumerate(scenes, start=1)]
    flags = _unique(flag for diagnosis in diagnoses for flag in diagnosis.get("pressure_flags") or [])
    fix_steps = _unique(step for diagnosis in diagnoses for step in diagnosis.get("fix_steps") or [])
    fix_steps.extend(_repeated_crucible_advice(scenes))
    strengths = []
    pass_count = sum(1 for diagnosis in diagnoses if diagnosis.get("recommended_status") == "pass")
    if pass_count:
        strengths.append(f"{pass_count} 场已经具备完整压力结构")
    score = round(sum(int(diagnosis.get("score") or 0) for diagnosis in diagnoses) / len(diagnoses))
    status = "rewrite" if any(diagnosis.get("recommended_status") == "rewrite" for diagnosis in diagnoses) else "maybe" if flags else "pass"
    return {
        "step_key": step_key,
        "pressure_score": max(0, min(100, score)),
        "pressure_status": status,
        "pressure_flags": flags,
        "fix_steps": fix_steps,
        "strengths": strengths,
        "score": max(0, min(100, score)),
        "status": status,
        "gaps": flags,
        "next_actions": fix_steps,
        "hard_blockers": _hard_blockers_from_flags(flags),
    }


def _missing_scene_detail_fields(scene: dict[str, Any]) -> list[str]:
    if _text(scene.get("exception_reason")):
        # 阶段 N：作者写了破例理由——原著「不过关也可以放行，但我要知道理由」；缺的三拍 / 坩埚不再算缺失，
        # 理由本身进结构简报，分诊与作者仍可判断它成不成立。
        return []
    scene_type = str(scene.get("primary_form") or scene.get("scene_type") or "proactive").strip().lower()
    required = ["reaction", "dilemma", "decision"] if scene_type == "reactive" else ["goal", "conflict", "setback"]
    missing = []
    if not has_value(scene.get("scene_crucible") or scene.get("crucible")):
        missing.append("crucible")
    missing.extend(key for key in required if not has_value(scene.get(key)))
    return missing


def diagnose_scene_detail(scene: dict[str, Any], *, index: int = 1) -> dict[str, Any]:
    payload = scene if isinstance(scene, dict) else {}
    scene_type = str(payload.get("primary_form") or payload.get("scene_type") or "proactive").strip().lower()
    if scene_type not in {"proactive", "reactive"}:
        scene_type = "proactive"
    required = ["reaction", "dilemma", "decision"] if scene_type == "reactive" else ["goal", "conflict", "setback"]
    missing_fields = _missing_scene_detail_fields(payload)
    total_fields = len(required) + 1
    filled_fields = max(0, total_fields - len(missing_fields))
    pressure_flags = [f"missing_{field}" for field in missing_fields]
    scene_core_empty = not has_value(payload.get("title")) and not has_value(payload.get("summary"))
    if scene_core_empty and all(field in missing_fields for field in required):
        pressure_flags.append("scene_core_empty")
    weak_flags, advice = _weak_scene_pressure_flags(payload, scene_type)
    pressure_flags.extend(flag for flag in weak_flags if flag not in pressure_flags)
    exception_reason = _text(payload.get("exception_reason"))
    if exception_reason:
        advice.insert(0, f"作者破例：{exception_reason}——缺的三拍 / 坩埚不计入缺失；分诊时仍请判断这个理由成不成立。")

    score = round((filled_fields / total_fields) * 100) if total_fields else 0
    if scene_core_empty:
        score = max(0, score - 10)
    if weak_flags:
        score = max(0, score - min(45, len(weak_flags) * 14))

    if "scene_core_empty" in pressure_flags or score < 40:
        recommended_status = "rewrite"
    elif missing_fields or weak_flags:
        recommended_status = "maybe"
    else:
        recommended_status = "pass"

    return {
        "scene_id": str(payload.get("scene_id") or f"scene_{index:02d}"),
        "primary_form": scene_type,
        "scene_type": scene_type,
        "recommended_status": recommended_status,
        "score": score,
        "missing_fields": missing_fields,
        "pressure_flags": pressure_flags,
        "exception_reason": exception_reason,
        # 建议只是建议：不扣分、不改状态，作者与 LLM 分诊才判质量。
        "advice": advice,
        "fix_steps": _diagnostic_fix_steps(
            scene_type, missing_fields, recommended_status, pressure_flags=pressure_flags, advice=advice
        ),
    }


# 2026-09-13 阶段 B（雪花评估 B4）：规则层只认两类弱点——字段还是**占位**（空、等于编辑器提示语 /
# 占位例句 / 修复例句、含「待补」、或命中泛泛短语表），以及**缺代价**。「冲突是否升级、两难是否真两难、
# 决定是否引出下一目标」这类质量判断只给建议，不扣分、不改状态：它们靠长度阈值与关键词猜，
# 把 Ingermanson 自己书里的场景计划（目标只有一句「拿到时间戳」、两难是「跑不掉、打不过、没处躲」）
# 判成 55 分「需修补」。质量判断交给 LLM 分诊与作者，并且永远可覆盖。
_PLACEHOLDER_MARKERS = ("待补", "TODO", "todo", "TBD", "tbd", "占位")


def _weak_scene_pressure_flags(scene: dict[str, Any], scene_type: str) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    advice: list[str] = []
    crucible = _text(scene.get("scene_crucible") or scene.get("crucible"))
    if crucible and _scene_field_placeholder_like("crucible", crucible):
        flags.append("placeholder_crucible")

    # 阶段 H：「代价」是本项目对原著的强化，不是原著的三拍——按原著五分钟写法规划的场不该因此
    # 拿不到「通过」。缺代价只提醒（Blueprint §4 的道理仍在：免费选择 = 注水）。
    if not has_value(scene.get("cost_requirement")):
        advice.append("建议：写出角色为这个选择付出了什么——什么信任被消耗、什么可能性被关闭、什么代价不可逆；免费选择 = 注水。")

    beats = ("reaction", "dilemma", "decision") if scene_type == "reactive" else ("goal", "conflict", "setback")
    generic_beats: list[str] = []
    for key in beats:
        value = _text(scene.get(key))
        if not value:
            continue
        if _scene_field_placeholder_like(key, value):
            flags.append(f"placeholder_{key}")
        elif _looks_generic(value, min_chars=0):
            generic_beats.append(_field_display_label(key))
    if crucible and not _scene_field_placeholder_like("crucible", crucible) and _looks_generic(crucible, min_chars=0):
        generic_beats.insert(0, _field_display_label("crucible"))
    if generic_beats:
        # 短语表只能猜「泛泛」，猜错就把原著级的短句判成占位——所以只提醒，不改状态。
        advice.append("建议：" + "、".join(generic_beats) + " 还是泛泛短语，写成这一场里具体的人、物、动作。")

    if scene_type == "reactive":
        dilemma = _text(scene.get("dilemma"))
        decision = _text(scene.get("decision"))
        if dilemma and not _has_true_choice_cost(dilemma):
            advice.append("建议：两难要写出两个都要付代价的选项——只有一个真选项就不是两难。")
        if decision and not _points_to_next_goal(decision):
            advice.append("建议：决定应直接变成下一场的具体目标。")
        return flags, advice

    conflict = _text(scene.get("conflict"))
    setback = _text(scene.get("setback"))
    if conflict and not _has_escalating_conflict(conflict):
        advice.append("建议：冲突写成多轮尝试→受阻并逐级升级（至少两轮，关键场可以更多），不只是一次拒绝。")
    if setback and not _has_cost_or_reversal(setback):
        advice.append("建议：让挫折比开场更糟，或让胜利带上代价——以主角衡量。")
    return flags, advice


def _scene_field_placeholder_like(field_key: str, value: str) -> bool:
    """字段内容是否仍是占位：空、等于编辑器提示语 / 占位例句 / 修复例句、或含「待补」。不看长度。
    阶段 H：泛泛短语表不再算占位（只给建议）——它猜错就把原著级的短句判成占位。"""
    text = _text(value)
    if not text:
        return True
    if _normalize_placeholder_text(text) in _SCENE_PLACEHOLDER_TEXTS.get(field_key, frozenset()):
        return True
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def _normalize_placeholder_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    for prefix in ("例：", "例:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.rstrip("。.；;，, ").strip()


def _collect_scene_placeholder_texts() -> dict[str, frozenset[str]]:
    """编辑器提示语、占位例句、修复例句——作者把它们原样留在字段里就等于没写。"""
    collected: dict[str, set[str]] = {}

    def add(field_key: str, *texts: Any) -> None:
        key = "crucible" if field_key == "scene_crucible" else field_key
        bucket = collected.setdefault(key, set())
        for text in texts:
            normalized = _normalize_placeholder_text(str(text or ""))
            if normalized:
                bucket.add(normalized)
            # 多行占位例句（「① … ② … ③ …」）也按整段登记；单独一行不算占位——作者可能真写了一轮。

    for field in get_step_definition("scene_details")["editor"]["fields"]:
        for mode in field.get("scene_modes") or []:
            for mode_field in mode.get("fields") or []:
                add(str(mode_field.get("key") or ""), mode_field.get("hint"), mode_field.get("placeholder"))
    for field_key, help_text in _FIELD_HELP.items():
        if field_key in {"crucible", "scene_crucible", "goal", "conflict", "setback", "reaction", "dilemma", "decision", "cost_requirement"}:
            add(field_key, help_text.get("hint"), help_text.get("placeholder"))
    for field_key, example in SCENE_FIELD_EXAMPLES.items():
        add(field_key, example)
    return {key: frozenset(values) for key, values in collected.items()}


def _diagnostic_fix_steps(
    scene_type: str,
    missing_fields: list[str],
    recommended_status: str,
    *,
    pressure_flags: list[str] | None = None,
    advice: list[str] | None = None,
) -> list[str]:
    if recommended_status == "pass":
        return list(advice or [])
    if recommended_status == "rewrite":
        return [
            "围绕具体坩埚重建这一场：谁被困住、被什么压力困住、结尾发生什么变化。",
            "开写前先选一种结构：主动场景用目标/冲突/挫折，反应场景用反应/困境/决定。",
        ]
    if scene_type == "reactive":
        labels = {
            "crucible": "写清楚角色为什么躲不开这个困境。",
            "reaction": "先补身体或情绪反应，再进入理性分析。",
            "dilemma": "把选择改成真正的两难，两边都有代价。",
            "decision": "用一个能制造下一场目标的决定收尾。",
        }
    else:
        labels = {
            "crucible": "写清楚什么力量把角色困在这个压力里。",
            "goal": "让场景目标具体、可见、可判断是否达成。",
            "conflict": "补出多轮升级的尝试与受阻，而不是一次拒绝。",
            "setback": "结尾要比开场更糟，或让胜利带上具体代价。",
        }
    steps = [labels[field] for field in missing_fields if field in labels]
    weak_labels = {
        "placeholder_crucible": "坩埚还是占位或泛泛之词：写出困住角色的具体陷阱、倒计时、社会代价或不可逆损失。",
        "placeholder_goal": "目标还是占位或泛泛之词：写成页面上可见、可检验的具体目标。",
        "placeholder_conflict": "冲突还是占位或泛泛之词：写出这一场里具体的尝试与受阻。",
        "placeholder_setback": "挫折还是占位或泛泛之词：写出结尾具体怎么更糟，或胜利付了什么代价。",
        "placeholder_reaction": "反应还是占位或泛泛之词：用身体反应、行为或迟来的意识写出来。",
        "placeholder_dilemma": "两难还是占位或泛泛之词：写出两个都有代价的具体选项。",
        "placeholder_decision": "决定还是占位或泛泛之词：写出角色接下来具体要去做什么。",
        # 旧标记名保留给历史分诊行（库里存过的 pressure_flags_json）。
        "weak_crucible_pressure": "把坩埚具体化：写出困住角色的陷阱、倒计时、社会代价或不可逆损失。",
        "weak_goal_specificity": "让场景目标在页面上可见、可检验。",
        "weak_conflict_escalation": "把冲突改成多轮尝试和更强阻力。",
        "weak_setback_cost": "让结尾变糟，或让表面胜利带上具体代价。",
        "weak_reaction_specificity": "用身体反应、行为或迟来的意识替代直接说情绪。",
        "fake_dilemma": "重写困境，让两个选项都有真实且明确的代价。",
        "weak_decision_next_goal": "让决定触发下一场的具体目标。",
        "missing_cost_requirement": "写出角色为这个选择付出了什么——什么信任被消耗、什么可能性被关闭、什么代价不可逆。免费选择 = 注水。",
    }
    steps.extend(weak_labels[flag] for flag in pressure_flags or [] if flag in weak_labels)
    steps.extend(advice or [])
    return _unique(steps)


def _repeated_crucible_advice(scenes: list[dict[str, Any]]) -> list[str]:
    """阶段 O：场景坩埚每场都要新（原著：新旧坩埚可以部分相同，但至少一部分被打破、一部分不同）。
    只认相邻两场坩埚一字不差——短语相似度判不准，猜错就冤枉作者；所以只提醒，不改状态。"""
    advice: list[str] = []
    previous_label = ""
    previous_crucible = ""
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        label = _text(scene.get("title")) or _text(scene.get("scene_id")) or f"第 {index} 场"
        crucible = " ".join(_text(scene.get("scene_crucible") or scene.get("crucible")).split())
        if crucible and previous_crucible and crucible == previous_crucible:
            advice.append(
                f"建议：{previous_label} 与 {label} 的坩埚一字不差——场景坩埚每场都要新，至少一部分被打破、一部分不同。"
            )
        previous_label = label
        previous_crucible = crucible
    return advice


def _pressure_result(step_key: str, *, flags: list[str], fix_steps: list[str], strengths: list[str]) -> dict[str, Any]:
    unique_flags = _unique(flags)
    unique_fix_steps = _unique(fix_steps)
    unique_strengths = _unique(strengths)
    score = max(0, min(100, 100 - len(unique_flags) * 12))
    critical_flags = {"missing_scenes", "scene_core_empty", "character_pressure_missing", "synopsis_missing", "scene_list_missing"}
    if any(flag in critical_flags for flag in unique_flags) and score > 45:
        score = 44
    status = "rewrite" if score < 45 else "maybe" if unique_flags else "pass"
    return {
        "step_key": step_key,
        "pressure_score": score,
        "pressure_status": status,
        "pressure_flags": unique_flags,
        "fix_steps": unique_fix_steps,
        "strengths": unique_strengths,
        "score": score,
        "status": status,
        "gaps": unique_flags,
        "next_actions": unique_fix_steps,
        "hard_blockers": _hard_blockers_from_flags(unique_flags),
    }


def _hard_blockers_from_flags(flags: list[str]) -> list[str]:
    critical_flags = {"missing_scenes", "scene_core_empty", "scene_list_missing"}
    blockers = [
        flag
        for flag in flags
        if flag in critical_flags or flag.startswith("missing_")
    ]
    return _unique(blockers)


_FIELD_DISPLAY_LABELS = {
    "category": "类型",
    "target_reader": "目标读者",
    "story_kind": "故事类型",
    "delight_reason": "读者沉迷原因",
    "genre_promise": "类型承诺",
    "expected_reader_emotion": "期待读者情绪",
    "narrative_stance": "叙述人称与时态",
    "story_time": "故事时间",
    "summary": "概括",
    "sentences": "五句骨架",
    "three_act_check": "三幕校验",
    "moral_premise": "主题前提",
    "paragraphs": "段落梗概",
    "characters": "角色",
    "scenes": "场景",
    "crucible": "坩埚",
    "scene_crucible": "坩埚",
    "goal": "目标",
    "conflict": "冲突",
    "setback": "挫折",
    "reaction": "反应",
    "dilemma": "困境",
    "decision": "决定",
    "exit_change": "离场变化",
    "hook": "钩子",
    "target_length_band": "目标篇幅",
    "rendering_mode": "呈现方式",
    "exception_reason": "破例理由",
}


def _field_display_label(key: str) -> str:
    return _FIELD_DISPLAY_LABELS.get(str(key or ""), str(key or "字段"))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _flag_key(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in str(value or "")).strip("_") or "field"


_GENERIC_FRAGMENTS = (
    "a mystery",
    "a story",
    "they argue",
    "she decides",
    "he decides",
    "she is upset",
    "feels bad",
    "stay or leave",
    "like mysteries",
    "likes mysteries",
    "喜欢 mysteries",
    "喜欢故事",
    "一段故事",
    "喜欢悬疑的读者",
    "发生了一些事",
    "一些事情发生",
)


_LEAD_ROLE_MARKERS = ("主角", "主人公", "对手", "反派", "protagonist", "antagonist", "hero", "heroine", "villain", "lead")


def is_lead_role(role: Any) -> bool:
    """主角 / 对手一类的定位——原著只对他们要求完整的角色表（诊断提醒与生成后的修复重试共用这一条）。"""
    lowered = _text(role).lower()
    return any(marker in lowered for marker in _LEAD_ROLE_MARKERS)


def _character_pressure_text(character: dict[str, Any]) -> str:
    """角色三步共用的「压力文本」：摘要表的目标 / 抱负 / 冲突 / 顿悟，背景的 synopsis，
    全档案嵌套的心理 / 性格档（归一化把旧的顶层 deepest_fear / how_character_changes 搬进了这里）。"""
    parts = [
        _text(character.get(key))
        for key in ("goal", "ambition", "conflict", "epiphany", "synopsis", "deepest_fear", "how_character_changes")
    ]
    for profile_key, keys in (
        ("psychological_profile", ("deepest_fear", "greatest_hope", "character_arc", "philosophy", "worst_memory")),
        ("personality_profile", ("strongest_trait", "weakest_trait")),
    ):
        profile = character.get(profile_key)
        if isinstance(profile, dict):
            parts.extend(_text(profile.get(key)) for key in keys)
    return " ".join(part for part in parts if part)


def _looks_generic(value: str, *, min_chars: int = 8) -> bool:
    """空、短于本字段的最小长度、或命中泛泛短语表。

    阶段 B（雪花评估）：旧版对所有字段统一用 28 字阈值，结果一句话概括的合规输出（提示词要求 40 字以内）、
    场景目标「拿到昨天各事件的时间戳」、章内职能「承压」全部被判空泛。最小长度改由调用方按字段传入，
    场景三拍不看长度（见 ``_scene_field_placeholder_like``）。
    """
    text = _text(value)
    if not text:
        return True
    if len(text) < min_chars:
        return True
    lowered = text.lower()
    return any(fragment in lowered for fragment in _GENERIC_FRAGMENTS)


def _has_pressure_turn(value: str) -> bool:
    lowered = _text(value).lower()
    markers = [
        "but",
        "yet",
        "cost",
        "risk",
        "lose",
        "force",
        "阻",
        "却",
        "但",
        "代价",
        "失去",
        "逼",
        "风险",
        "冲突",
        "挫折",
        "灾难",
    ]
    return any(marker in lowered for marker in markers)


def _has_escalating_conflict(value: str) -> bool:
    text = _text(value)
    lowered = text.lower()
    if len(text) >= 60:
        return True
    markers = ["→", ";", "；", "first", "then", "again", "each", "stronger", "tries", "attempt", "resist", "阻", "更", "升级", "尝试"]
    return sum(1 for marker in markers if marker in lowered) >= 2


def _has_cost_or_reversal(value: str) -> bool:
    lowered = _text(value).lower()
    cost_markers = [
        "but",
        "yet",
        "cost",
        "lose",
        "worse",
        "expose",
        "implicate",
        "risk",
        "debt",
        "却",
        "但是",
        "代价",
        "失去",
        "暴露",
        "牵连",
        "更糟",
        "风险",
    ]
    return any(marker in lowered for marker in cost_markers)


def _has_true_choice_cost(value: str) -> bool:
    lowered = _text(value).lower()
    has_choice = any(
        marker in lowered
        for marker in [" or ", "或", "选a", "选b", "choice", "choose", "one choice", "the other", "一边", "另一边", "要么"]
    )
    has_cost = any(
        marker in lowered
        for marker in ["cost", "means", "lose", "burn", "harm", "expose", "代价", "意味着", "失去", "伤害", "暴露", "牺牲"]
    )
    return has_choice and has_cost


def _points_to_next_goal(value: str) -> bool:
    lowered = _text(value).lower()
    action_markers = [
        "go",
        "send",
        "call",
        "meet",
        "find",
        "ask",
        "steal",
        "confront",
        "next",
        "去",
        "发",
        "见",
        "找",
        "问",
        "偷",
        "对峙",
        "下一",
    ]
    return any(marker in lowered for marker in action_markers)


def _unique(values: Any) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        marker = str(value)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


# 场景三拍的修复例句：旧版 LLM 关闭时「应用修复补丁」写进字段的就是它们（AI 分诊 2026-09-30 起 fail-closed，
# 不再递这些例句），库里旧数据可能还留着——它们同时登记为占位文本，例句留在字段里就等于没写。
SCENE_FIELD_EXAMPLES: dict[str, str] = {
    "crucible": "一个具体压力把视角角色困在这里；离开会让损失永久化。",
    "goal": "拿到某个具体证据、许可或让步——读者能看出「赢」长什么样。",
    "conflict": "角色先直接索取，再尝试策略绕路，最后冒险揭露；每一轮都遇到更强阻力。",
    "setback": "角色拿到线索，但代价指向一个他无法失去的人。",
    "reaction": "角色先出现身体和情绪反应，然后才开始分析损害。",
    "dilemma": "一个选择保护关系却埋掉真相，另一个选择暴露真相却烧掉保护。",
    "decision": "角色选择代价更高的路径，并制造下一场的具体目标。",
    "cost_requirement": "拿到线索的同时，永久失去了这个线人的信任。",
}


_SCENE_PLACEHOLDER_TEXTS: dict[str, frozenset[str]] = _collect_scene_placeholder_texts()
