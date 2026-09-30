"""分章的节奏体检（P3，纯确定性，不调 LLM）。"""

from __future__ import annotations

from typing import Any

#: 三幕结构里三个灾难该落在哪一幕（Ingermanson：灾一收一幕、灾二是中点、灾三送进三幕）。
#: 这是**建议**不是规则——作者故意把灾二后置是合法选择，所以体检只提示、从不阻断。
SPINE_EXPECTED_ACT = {"灾一": 1, "灾二": 2, "灾三": 2}


def rhythm_report(chapter_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """只报三件能从结构本身看出来、且作者一眼能行动的事：每章场数的分布、三幕的章场
    配比、三个灾难落点是否还在它该在的幕。不做「张力评分」那种需要读正文才谈得上的
    判断——这里还没有正文。
    """
    counts = [item["scene_count"] for item in chapter_payloads]
    # 阶段 C：概述两段的反应场只有一两百字，按半场计入均值——它不该把一章「撑」成长章。
    # 阶段 I：页面上略过的反应场不占篇幅，按 0 计。
    # 阶段 N：作者裁定该重写 / 待删的场（excluded）不物化，同样按 0 计。
    _weight = {"summary": 0.5, "skip": 0.0}
    weighted = [
        sum(
            0.0 if scene.get("excluded") else _weight.get(str(scene.get("rendering_mode") or "full"), 1.0)
            for scene in (item.get("scenes") or [])
        )
        for item in chapter_payloads
    ]
    summary_scene_count = sum(
        1
        for item in chapter_payloads
        for scene in (item.get("scenes") or [])
        if str(scene.get("rendering_mode") or "full") == "summary"
    )
    skipped_scene_count = sum(
        1
        for item in chapter_payloads
        for scene in (item.get("scenes") or [])
        if str(scene.get("rendering_mode") or "full") == "skip"
    )
    non_empty = [count for count in weighted if count]
    mean = (sum(non_empty) / len(non_empty)) if non_empty else 0.0

    acts: dict[int, dict[str, int]] = {}
    for item in chapter_payloads:
        bucket = acts.setdefault(int(item.get("act") or 1), {"chapter_count": 0, "scene_count": 0})
        bucket["chapter_count"] += 1
        bucket["scene_count"] += item["scene_count"]

    spine_placement = []
    for mark, expected_act in SPINE_EXPECTED_ACT.items():
        hit = next((item for item in chapter_payloads if (item.get("spine") or "") == mark), None)
        if hit is None:
            spine_placement.append({"spine": mark, "placed": False, "expected_act": expected_act})
            continue
        act = int(hit.get("act") or 1)
        index = chapter_payloads.index(hit)
        is_last_of_act = not any(
            other for other in chapter_payloads[index + 1 :] if int(other.get("act") or 1) == act
        )
        spine_placement.append(
            {
                "spine": mark,
                "placed": True,
                "chapter_seq": hit["chapter_seq"],
                "chapter_title": hit["title"],
                "act": act,
                "expected_act": expected_act,
                # 灾一/灾三 是幕的收束点，落在本幕最后一章才算「在铰链上」；灾二是中点，只看幕。
                "on_hinge": (act == expected_act) and (mark == "灾二" or is_last_of_act),
            }
        )

    raw_non_empty = [count for count in counts if count]
    return {
        "scene_counts": counts,
        "weighted_scene_counts": weighted,
        "summary_scene_count": summary_scene_count,
        "skipped_scene_count": skipped_scene_count,
        "mean_scenes_per_chapter": round(mean, 2),
        "min_scenes": min(raw_non_empty) if raw_non_empty else 0,
        "max_scenes": max(raw_non_empty) if raw_non_empty else 0,
        "empty_chapter_count": sum(1 for count in counts if not count),
        "acts": [
            {"act": act, **acts[act]}
            for act in sorted(acts)
        ],
        "spine_placement": spine_placement,
    }
