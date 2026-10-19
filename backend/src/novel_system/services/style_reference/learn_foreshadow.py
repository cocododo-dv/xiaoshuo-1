"""风格参考 —「学习文风 · 通读全书」的伏笔与回收（2026-10-19 第四轮，学习作业的 ``foreshadow`` 步）。

通读时叙事层逐批记下这几章**埋下**的东西（物件、台词、人物细节、悬念提问、预言暗示……，``learn_structure``）与
**回收**前文的地方（揭晓真相、反转、物件再现、台词呼应……），各带一句情节梗概。一批只看得到自己的几章，跨批的
「这一处收的是哪一处埋」要读完全书再配：

1. **配对**（模板 ``style_ref_foreshadow_match``，走文风卡合成节点的路由）：全书的埋 P1…与收 R1…交给模型，每处收
   至多配一处埋、埋不能晚于收（:func:`parse_matches` 核对）；装不下预算就把「收」分几次送（埋每次都全送）；
2. **统计**（:func:`foreshadow_stats`，确定性）：埋了多少、几成回收、从埋到收隔几章（中位与分位、同一章就收的、
   隔十章以上的占比）、埋与收各是哪几类、每章末同时悬着几处——写进结构画像 ``foreshadowing``，渲染进 ``[结构画像]``；
3. **手法**（模板 ``style_ref_foreshadow_craft``）：按配好的对子写 ≤4 句「这位作者怎么埋、怎么收」（只写手法，不写人名
   情节，在上一版上修订），定稿时过文风卡同一道过滤，以「伏笔：」开头进规划手法（``planning_guidance``，至多 3 句，
   给它们让出位置），原句另存 ``foreshadow_lines`` 供下次修订。

情节梗概只在载荷里给模型配对 / 归纳用，从不进画像与写作提示。快速学习不做这一步。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from novel_system.services.style_reference.card import CARD_LINE_MAX_CHARS, line_id_for
from novel_system.services.style_reference.learn_card import has_statistics
from novel_system.services.style_reference.learn_structure import PAYOFF_STYLES, PLANT_KINDS
from novel_system.services.style_reference.text_utils import compact_ws
from novel_system.services.value_coercion import quantile

FORESHADOW_VERSION = "foreshadowing_v1"
FORESHADOW_LINES_KEY = "foreshadow_lines"
PLANNING_PREFIX = "伏笔："
MAX_CRAFT_LINES = 4
MAX_PLANNING_LINES = 3
MAX_PAIRS_SENT = 60
LONG_RANGE_CHAPTERS = 10
_BOUNDARY = "。；！？!?;"


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------


def match_payload(plants: Sequence[Mapping[str, Any]], payoffs: Sequence[Mapping[str, Any]], *, book_title: str) -> dict[str, Any]:
    return {
        "book_title": book_title,
        "plants": [{"id": p["id"], "chapter": p["chapter"], "kind": p["kind"], "gist": p["gist"]} for p in plants],
        "payoffs": [
            {"id": r["id"], "chapter": r["chapter"], "style": r["style"], "gist": r["gist"], "setup": r.get("setup") or ""}
            for r in payoffs
        ],
    }


def match_chunks(
    plants: Sequence[Mapping[str, Any]],
    payoffs: Sequence[Mapping[str, Any]],
    fits: Callable[[dict[str, Any]], bool],
    *,
    book_title: str,
) -> list[list[Mapping[str, Any]]]:
    """「收」按原书顺序分几组，每组连同全部「埋」装得进节点预算（一组至少一处收）。"""
    chunks: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for payoff in payoffs:
        candidate = [*current, payoff]
        if current and not fits(match_payload(plants, candidate, book_title=book_title)):
            chunks.append(current)
            current = [payoff]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def parse_matches(
    structured: Any,
    plants: Sequence[Mapping[str, Any]],
    payoffs: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """``{收 id: 埋 id}``：编号必须存在、埋的章不晚于收的章、每处收只认第一次配对。"""
    plant_chapter = {str(p["id"]): int(p.get("chapter") or 0) for p in plants}
    payoff_chapter = {str(r["id"]): int(r.get("chapter") or 0) for r in payoffs}
    out: dict[str, str] = {}
    items = structured.get("matches") if isinstance(structured, Mapping) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        payoff = str(item.get("payoff") or "").strip().upper()
        plant = str(item.get("plant") or "").strip().upper()
        if payoff not in payoff_chapter or plant not in plant_chapter or payoff in out:
            continue
        if plant_chapter[plant] > payoff_chapter[payoff]:
            continue
        out[payoff] = plant
    return out


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def _spread(values: Sequence[float]) -> dict[str, int]:
    if not values:
        return {"median": 0, "p10": 0, "p90": 0}
    return {
        "median": int(round(quantile(values, 0.5))),
        "p10": int(round(quantile(values, 0.1))),
        "p90": int(round(quantile(values, 0.9))),
    }


def _shares(values: Sequence[str], vocabulary: Sequence[str]) -> dict[str, float]:
    counts = Counter(v for v in values if v in vocabulary)
    total = sum(counts.values())
    return {
        name: round(n / total, 3)
        for name, n in sorted(counts.items(), key=lambda item: (-item[1], vocabulary.index(item[0])))
    } if total else {}


def foreshadow_stats(
    plants: Sequence[Mapping[str, Any]],
    payoffs: Sequence[Mapping[str, Any]],
    matches: Mapping[str, str],
    *,
    chapters_read: Sequence[int],
) -> dict[str, Any] | None:
    """伏笔统计（见模块文档）。一处埋都没有 → None。"""
    if not plants:
        return None
    by_plant = {str(p["id"]): p for p in plants}
    by_payoff = {str(r["id"]): r for r in payoffs}
    paid_at: dict[str, int] = {}
    distances: list[int] = []
    styles: list[str] = []
    for payoff_id, plant_id in matches.items():
        plant, payoff = by_plant.get(plant_id), by_payoff.get(payoff_id)
        if plant is None or payoff is None:
            continue
        distance = int(payoff["chapter"]) - int(plant["chapter"])
        distances.append(distance)
        styles.append(str(payoff.get("style") or ""))
        paid_at[plant_id] = min(paid_at.get(plant_id, 10**9), int(payoff["chapter"]))
    open_counts: list[int] = []
    for chapter in sorted(set(int(c) for c in chapters_read)):
        open_counts.append(
            sum(
                1
                for plant_id, plant in by_plant.items()
                if int(plant["chapter"]) <= chapter and paid_at.get(plant_id, 10**9) > chapter
            )
        )
    pairs = len(distances)
    return {
        "version": FORESHADOW_VERSION,
        "chapters_read": len(set(int(c) for c in chapters_read)),
        "planted": len(plants),
        "payoffs": len(payoffs),
        "pairs": pairs,
        "paid_share": round(len(paid_at) / len(plants), 3),
        "distance": _spread(distances),
        "same_chapter_share": round(sum(1 for d in distances if d == 0) / pairs, 3) if pairs else 0.0,
        "long_range_share": round(sum(1 for d in distances if d >= LONG_RANGE_CHAPTERS) / pairs, 3) if pairs else 0.0,
        "plant_kinds": _shares([str(p.get("kind") or "") for p in plants], PLANT_KINDS),
        "payoff_styles": _shares(styles, PAYOFF_STYLES),
        "open_per_chapter": _spread(open_counts),
    }


# ---------------------------------------------------------------------------
# 手法
# ---------------------------------------------------------------------------


def craft_pairs(
    plants: Sequence[Mapping[str, Any]],
    payoffs: Sequence[Mapping[str, Any]],
    matches: Mapping[str, str],
    *,
    limit: int = MAX_PAIRS_SENT,
) -> list[dict[str, Any]]:
    """配好的对子（编号 t1…），按隔的章数排后均匀取 ≤``limit`` 对——近收、远收都有。"""
    by_plant = {str(p["id"]): p for p in plants}
    by_payoff = {str(r["id"]): r for r in payoffs}
    rows = []
    for payoff_id, plant_id in matches.items():
        plant, payoff = by_plant.get(plant_id), by_payoff.get(payoff_id)
        if plant is None or payoff is None:
            continue
        rows.append((int(payoff["chapter"]) - int(plant["chapter"]), plant, payoff))
    rows.sort(key=lambda row: (row[0], str(row[2]["id"])))
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return [
        {
            "id": f"t{number}",
            "distance": distance,
            "plant_kind": plant["kind"],
            "plant": plant["gist"],
            "payoff_style": payoff["style"],
            "payoff": payoff["gist"],
        }
        for number, (distance, plant, payoff) in enumerate(rows, start=1)
    ]


def craft_payload(
    stats: Mapping[str, Any],
    pairs: Sequence[Mapping[str, Any]],
    *,
    book_title: str,
    previous: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "book_title": book_title,
        "stats": {
            key: stats.get(key)
            for key in ("planted", "paid_share", "distance", "same_chapter_share", "long_range_share", "plant_kinds", "payoff_styles", "open_per_chapter")
        },
        "pairs": list(pairs),
        "previous_lines": [{"id": line_id, "text": text} for line_id, text in (previous or {}).items()],
    }


def _fit(text: str) -> str | None:
    if len(text) <= CARD_LINE_MAX_CHARS:
        return text
    cut = [i + 1 for i, ch in enumerate(text[:CARD_LINE_MAX_CHARS]) if ch in _BOUNDARY]
    return text[: cut[-1]] if cut and cut[-1] >= CARD_LINE_MAX_CHARS // 2 else None


def parse_craft_lines(
    structured: Any,
    pair_ids: Sequence[str],
    *,
    previous: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """手法句：refs 必须是送去的对子、不带统计数字、超长按句读截；``keep`` 用上一版原句与原 id。"""
    previous = dict(previous or {})
    known = {str(p).lower() for p in pair_ids}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    items = structured.get("lines") if isinstance(structured, Mapping) else None
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        keep = str(raw.get("keep") or "").strip()
        kept = keep in previous
        text = previous[keep] if kept else compact_ws(raw.get("text")).lstrip("-•· ")
        if text.startswith(PLANNING_PREFIX):
            text = text[len(PLANNING_PREFIX) :].strip()
        text = _fit(text) if text else None
        if not text or has_statistics(text):
            continue
        refs = [r for r in dict.fromkeys(str(r or "").strip().lower() for r in raw.get("refs") or []) if r in known]
        if not refs:
            continue
        line_id = keep if kept else line_id_for("foreshadow", text)
        if line_id in seen:
            continue
        seen.add(line_id)
        out.append({"line_id": line_id, "text": text, "pairs": refs, "source": "kept" if kept else "synthesis"})
        if len(out) >= MAX_CRAFT_LINES:
            break
    return out


def previous_foreshadow_lines(profile_json: Mapping[str, Any] | None) -> dict[str, str]:
    """上一版的伏笔手法句：``{line_id: 句子}``（重新学习时在上一版上修订）。"""
    raw = profile_json.get(FORESHADOW_LINES_KEY) if isinstance(profile_json, Mapping) else None
    if not isinstance(raw, list):
        return {}
    return {
        str(item["line_id"]): str(item["text"])
        for item in raw
        if isinstance(item, Mapping) and item.get("line_id") and item.get("text")
    }


def merge_planning(planning: Sequence[str], lines: Sequence[Mapping[str, Any]], *, cap: int) -> list[str]:
    """规划手法 + 至多 :data:`MAX_PLANNING_LINES` 句「伏笔：…」：总数不超过 ``cap``，给伏笔句让位（合成的句从后往前让）。"""
    extra = [f"{PLANNING_PREFIX}{line['text']}" for line in lines[:MAX_PLANNING_LINES]]
    base = [line for line in planning if not str(line).startswith(PLANNING_PREFIX)]
    return [*base[: max(0, cap - len(extra))], *extra]


def chapters_read_from(plan_batches: Sequence[Mapping[str, Any]]) -> list[int]:
    """切批里读过的章（按原书顺序、去重）。"""
    return list(
        dict.fromkeys(
            int(segment.get("chapter") or 0) for batch in plan_batches for segment in batch.get("segments") or []
        )
    )


__all__ = [
    "FORESHADOW_LINES_KEY",
    "FORESHADOW_VERSION",
    "MAX_PLANNING_LINES",
    "PLANNING_PREFIX",
    "chapters_read_from",
    "craft_pairs",
    "craft_payload",
    "foreshadow_stats",
    "match_chunks",
    "match_payload",
    "merge_planning",
    "parse_craft_lines",
    "parse_matches",
    "previous_foreshadow_lines",
]
