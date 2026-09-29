"""正文与叙事事件账本的硬事实矛盾检查（蓝图 §13 第 6 步 / §17 Action B）。

只查硬事实（存活、位置、断肢、遗失物品、稳定的身体状况、结构化的外貌与能力），按分句 + 邻近 + 同义词匹配，
记忆 / 闪回 / 失去之类的框定一律放过。命中是 Q1（阻断）；软事实（语气、关系的分寸）不在范围内。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from novel_system.db.models import NarrativeEvent
from novel_system.services.character_names import display_name_of
from novel_system.services.narrative.replay import (
    NarrativeEventStore,
    runtime_authority_clause,
    snapshot_before,
)
from novel_system.services.narrative.taxonomy import CHECKABLE_FACT_KEYS


@dataclass(slots=True)
class ConsistencyViolation:
    fact_key: str
    expected: str
    actual: str
    entity_id: str
    evidence: str
    # §15: "keyword" = high-confidence deterministic match (blocking) — the only producer
    # since the advisory LLM flag layer (check_consistency_llm) was removed.
    source: str = "keyword"
    # 给作者看的名字（人物显示名；没有就是 id）——entity_id 仍是账本里的 id
    entity_name: str = ""


@dataclass(slots=True)
class ConsistencyReport:
    passed: bool
    violations: list[ConsistencyViolation] = field(default_factory=list)
    facts_checked: int = 0


def check_consistency(
    store: NarrativeEventStore,
    generated_text: str,
    project_id: str,
    scene_id: str,
    *,
    character_ids: list[str] | None = None,
) -> ConsistencyReport:
    """正文与这一场之前的权威状态做硬事实矛盾检查（蓝图 §17 Action B 的「一次增量一致性检查」）。"""
    snapshot = snapshot_before(store, project_id, scene_id)
    chars = character_ids or snapshot.characters()
    names = snapshot.names()

    violations: list[ConsistencyViolation] = []
    facts_checked = 0
    text_lower = generated_text.lower()
    # Known location names in this project — used by the location detector to
    # distinguish "character is at the WRONG named place" from generic prose.
    # Locations live both as location entities AND as `location` facts asserted
    # on characters (location_change events), so gather both.
    # 地点实体按 id 记账，正文里写的是它的名字 / 别名：两样都算已知地名（B11-01）。
    known_locations = set()
    for location_id in snapshot.entities_of_type("location"):
        if not location_id:
            continue
        known_locations.add(location_id.lower())
        known = names.get(location_id)
        if known is not None:
            known_locations |= {name.lower() for name in known.match_names()}
    loc_values = store.session.execute(
        select(NarrativeEvent.fact_value).where(
            NarrativeEvent.project_id == project_id,
            NarrativeEvent.fact_key == "location",
            runtime_authority_clause(),
        ).distinct()
    ).scalars().all()
    known_locations |= {v.lower() for v in loc_values if v}

    for char_id in chars:
        state = snapshot.character_state(char_id)
        known = names.get(char_id)
        # 正史事实按角色 id 存，正文写的是名字：按显示名 + 别名找，id 本身兜底（B11-01）
        reference = EntityReference.of(
            char_id,
            known.match_names() if known is not None else (),
            display=display_name_of(names, char_id),
        )
        for fact_key, projected in state.facts.items():
            if fact_key not in CHECKABLE_FACT_KEYS:
                continue
            facts_checked += 1
            violation = check_fact_against_text(
                text_lower,
                reference,
                fact_key,
                projected.fact_value,
                known_locations=known_locations,
            )
            if violation is not None:
                violations.append(violation)

    return ConsistencyReport(
        passed=len(violations) == 0,
        violations=violations,
        facts_checked=facts_checked,
    )


# ---------------------------------------------------------------------------
# Hard-fact contradiction detection (blueprint §13 Step 6 / §17 Action B)
#
# Works at *clause* granularity with proximity + synonym matching instead of
# brittle adjacent-substring matching. This is what makes recall survive
# realistic prose ("他抬起右手攥紧剑柄") rather than only textbook-exact phrasings
# ("右手握"). Precision is held up by negative guards (memory / loss framing)
# and by requiring an action signal near the entity reference.
# ---------------------------------------------------------------------------

_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?；;：:\n\r，,、（）()「」『』“”\"'…—　]+")


def _split_clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT_RE.split(text) if c.strip()]


def _clause_refers_to_entity(clauses: list[str], index: int, entity_lower: str) -> bool:
    clause = clauses[index]
    if entity_lower in clause:
        return True
    # Permit a tightly adjacent pronoun continuation ("林远……。他右手……")
    # without attributing a different named character's action to this entity.
    starts_with_pronoun = re.match(
        r"^\s*[‘’“”\"']*(?:他|她|其|he\b|she\b|his\b|her\b)", clause
    ) is not None
    if not starts_with_pronoun or index <= 0:
        return False
    if entity_lower in clauses[index - 1]:
        return True
    if index >= 2 and entity_lower in clauses[index - 2]:
        bridge = clauses[index - 1]
        return any(pronoun in bridge for pronoun in ("他", "她", "其", " he ", " she ", " his ", " her "))
    return False


def _all_idx(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = haystack.find(needle)
    while i >= 0:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


def _min_gap(clause: str, tokens_a: tuple[str, ...], tokens_b: tuple[str, ...]) -> int | None:
    """Smallest char distance between any A-token and any B-token within a clause."""
    pa = [i for t in tokens_a for i in _all_idx(clause, t)]
    pb = [i for t in tokens_b for i in _all_idx(clause, t)]
    if not pa or not pb:
        return None
    return min(abs(a - b) for a in pa for b in pb)


def _verb_after(clause: str, anchor: str, verbs: tuple[str, ...], window: int) -> str | None:
    """First verb appearing within *window* chars AFTER *anchor* (i.e. anchor is subject)."""
    for start in _all_idx(clause, anchor):
        seg = clause[start + len(anchor): start + len(anchor) + window]
        for v in verbs:
            if v in seg:
                return v
    return None


# alive==dead: a dead character performing a living action, unless memory/flashback framing
_DEATH_MEMORY_GUARDS = (
    "想起", "记得", "回忆", "忆起", "曾经", "生前", "死前", "临终", "遗言", "遗体",
    "遗物", "遗像", "尸", "已故", "亡", "坟", "墓", "灵位", "牌位", "画像", "祭", "悼",
    "梦", "若还", "如果还", "仿佛还", "好像还", "似乎还", "当年", "那时", "过去",
)
_ALIVE_ACTION_VERBS = (
    "说道", "说", "开口", "喊道", "喊", "叫道", "问道", "问", "答道", "回答", "点头",
    "摇头", "起身", "站起", "坐起", "转身", "抬手", "抬起", "伸手", "迈步", "走来",
    "走向", "走进", "走到", "拔出", "握住", "握紧", "睁开", "看向", "望向", "盯着",
    "拿起", "举起", "笑了", "招手", "摆手", "开门",
    "said", "spoke", "walked", "nodded", "smiled",
)

# missing_limb synonym groups (normalise right_arm / 右臂 / 右手 → one group)
_LIMB_GROUPS: dict[str, tuple[str, ...]] = {
    "right_arm": ("右臂", "右手", "右胳膊", "右手臂", "右臂膀", "right arm", "right hand"),
    "left_arm": ("左臂", "左手", "左胳膊", "左手臂", "左臂膀", "left arm", "left hand"),
    "right_leg": ("右腿", "右脚", "右膝", "right leg", "right foot"),
    "left_leg": ("左腿", "左脚", "左膝", "left leg", "left foot"),
}
_LIMB_ACTION_VERBS = (
    "握", "攥", "抓", "举", "抬", "挥", "拔", "持", "拿", "伸", "搭", "按", "拍", "捏",
    "扯", "拽", "推", "抱", "捧", "接", "指", "划", "端", "提", "掂", "甩", "挡",
    "gripped", "raised", "grabbed", "clenched", "held", "swung",
)
_BOTH_HANDS = ("双手", "两手", "两只手", "双臂", "两臂", "十指", "both hands")
_LIMB_MISSING_GUARDS = (
    "断", "残", "失去", "没有了", "空荡荡", "仅存", "唯一", "缺", "截", "空袖", "断口",
    "残肢", "伤口", "本应", "本该", "曾经的", "想起", "记得",
)

# location: broadened "still at the wrong place" phrasings
_STILL_AT_PHRASES = (
    "还在", "仍在", "仍旧在", "依旧在", "依然在", "还停留", "仍停留", "还留在", "仍留在",
    "还待在", "仍待在", "依旧待在", "依然待在", "还守在", "仍守在", "还困在", "仍困在",
    "迟迟没有离开", "迟迟未离开", "没有离开", "未曾离开", "尚未离开",
    "still at", "still in", "remained at", "remained in", "was still at",
)

# has_item lost: a character using an item they no longer possess
_ITEM_POSSESS_VERBS = (
    "拿出", "掏出", "取出", "抽出", "拔出", "拿起", "握住", "握紧", "握", "举起", "举",
    "挥舞", "挥", "抓起", "攥", "佩", "掂", "提着", "捧着", "抚摸", "擦拭",
    "pulled out", "drew", "raised", "gripped",
)
_ITEM_LOSS_GUARDS = (
    "失去", "丢了", "丢失", "没了", "不见了", "遗失", "早已不在", "曾经的", "已经没有",
    "不在手中", "空空", "已断", "碎了", "毁了", "想起", "记得", "回忆", "怀念",
)

# Deterministic coverage for structured physical-state facts.  Free-form values
# such as "tired" or "wounded" are intentionally not inferred as contradictions:
# they can legitimately change inside the scene and remain advisory-only.
_STATE_RECOVERY_GUARDS = (
    "醒来", "苏醒", "恢复意识", "康复", "痊愈", "重新能够", "不再", "曾经",
    "回忆", "梦见", "如果", "假如", "试图", "尝试", "却没能", "但失败",
    "woke", "awoke", "recovered", "used to", "remembered", "dreamed", "tried",
)
_STATE_NEGATION_GUARDS = (
    "不能", "无法", "没法", "未能", "没有", "不再", "差点", "险些", "试图", "尝试",
    "cannot", "can't", "could not", "couldn't", "unable", "failed to", "tried to",
)
_VISUAL_ACTIONS = (
    "看见", "看到", "望见", "瞥见", "瞧见", "目睹", "注视", "端详", "读到", "阅读",
    "saw", "looked", "watched", "read", "glimpsed",
)
_HEARING_ACTIONS = ("听见", "听到", "听清", "听出", "heard", "listened")
_SPEAKING_ACTIONS = (
    "说道", "说", "开口", "回答", "喊道", "叫道", "低语", "耳语",
    "said", "spoke", "answered", "whispered", "shouted",
)
_WALKING_ACTIONS = (
    "站起", "起身", "走向", "走到", "迈步", "奔跑", "跑向", "跳起",
    "stood", "walked", "ran", "jumped",
)
_ASSISTIVE_PERCEPTION_GUARDS = (
    "盲杖", "摸索", "听声辨位", "读屏", "屏幕阅读器", "借助", "助听器", "读唇",
    "braille", "screen reader", "hearing aid", "lip-read",
)

_COLOR_ALIASES: dict[str, tuple[str, ...]] = {
    "black": ("黑色", "乌黑", "墨黑", "黑发", "black"),
    "brown": ("棕色", "褐色", "栗色", "棕发", "brown", "chestnut"),
    "blond": ("金色", "金发", "浅金", "blond", "blonde", "golden"),
    "red": ("红色", "赤红", "红发", "red", "auburn"),
    "white": ("白色", "雪白", "银白", "白发", "银发", "white", "silver"),
    "gray": ("灰色", "灰白", "灰发", "gray", "grey"),
    "blue": ("蓝色", "湛蓝", "蓝眸", "blue"),
    "green": ("绿色", "碧绿", "绿眸", "green"),
}
_HAIR_ANCHORS = ("头发", "发丝", "发色", "长发", "短发", "hair")
_EYE_ANCHORS = ("眼睛", "眼眸", "瞳孔", "眸子", "eye", "eyes")
_APPEARANCE_MEMORY_GUARDS = ("曾经", "从前", "旧照", "照片", "回忆", "梦里", "假如", "伪装", "染成")

_ABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "swim": ("游泳", "泅水", "游水", "swim"),
    "read": ("阅读", "读书", "读到", "read"),
    "write": ("写字", "书写", "write"),
    "speak": _SPEAKING_ACTIONS,
    "see": _VISUAL_ACTIONS,
    "hear": _HEARING_ACTIONS,
    "walk": _WALKING_ACTIONS,
    "fly": ("飞行", "飞起", "腾空", "fly", "flew"),
    "magic": ("施法", "释放魔法", "念动咒语", "施展法术", "cast a spell", "used magic"),
}


def _clause_has_negated_or_nonactual_action(clause: str) -> bool:
    return any(guard in clause for guard in (*_STATE_NEGATION_GUARDS, *_STATE_RECOVERY_GUARDS))


def _physical_state_kind(value_lower: str) -> str | None:
    normalized = value_lower.replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("unconscious", "coma", "昏迷", "失去意识")):
        return "unconscious"
    if any(token in normalized for token in ("blind", "失明", "看不见")):
        return "blind"
    if any(token in normalized for token in ("deaf", "失聪", "听不见")):
        return "deaf"
    if any(token in normalized for token in ("mute", "失语", "不能说话")):
        return "mute"
    if any(token in normalized for token in ("paralyzed", "paralysed", "瘫痪", "无法行走")):
        return "paralyzed"
    return None


def _physical_state_missing_limb(value_lower: str) -> str | None:
    normalized = value_lower.replace("-", "_").replace(" ", "_")
    for group, words in _LIMB_GROUPS.items():
        group_tokens = (group, *words)
        if any(token in normalized for token in group_tokens) and any(
            marker in normalized
            for marker in ("severed", "amputated", "missing", "断", "截肢", "失去")
        ):
            return group
    return None


def _canonical_color(value_lower: str) -> str | None:
    for canonical, aliases in _COLOR_ALIASES.items():
        if any(alias in value_lower for alias in aliases):
            return canonical
    return None


def _appearance_contract(value_lower: str) -> tuple[str, str] | None:
    normalized = value_lower.replace("=", ":")
    if any(prefix in normalized for prefix in ("hair:", "hair_color:", "头发:", "发色:")):
        color = _canonical_color(normalized)
        return ("hair", color) if color else None
    if any(prefix in normalized for prefix in ("eye:", "eyes:", "eye_color:", "眼睛:", "瞳色:")):
        color = _canonical_color(normalized)
        return ("eyes", color) if color else None
    if normalized in {"bald", "光头", "秃头", "头发全无"}:
        return ("bald", "bald")
    return None


def _negative_ability_contract(value_lower: str) -> tuple[str, tuple[str, ...]] | None:
    normalized = value_lower.strip().replace("=", ":")
    prefixes = ("cannot:", "unable:", "lost:", "no:", "不能:", "无法:", "失去能力:")
    payload = next((normalized[len(prefix):].strip() for prefix in prefixes if normalized.startswith(prefix)), None)
    if payload is None:
        for suffix in ("_unavailable", "_lost", "_disabled"):
            if normalized.endswith(suffix):
                payload = normalized[: -len(suffix)]
                break
    if not payload:
        return None
    for canonical, aliases in _ABILITY_ALIASES.items():
        if payload == canonical or any(alias == payload for alias in aliases):
            return canonical, aliases
    # A structured negative contract may use a project-specific concrete action.
    # Match it literally, but never infer synonyms for arbitrary prose.
    if len(payload) >= 2:
        return payload, (payload,)
    return None


def _norm_limb_group(value_lower: str) -> str | None:
    """Map a missing_limb fact value (right_arm / 右臂 / 右手 …) to a canonical group key."""
    for key, words in _LIMB_GROUPS.items():
        if value_lower == key or value_lower in key or key in value_lower:
            return key
        for w in words:
            if w in value_lower or value_lower in w:
                return key
    return None


@dataclass(frozen=True, slots=True)
class EntityReference:
    """正文里指向一个实体的写法：全部小写的匹配形式，外加证据里用的原样写法（一一对应）。"""

    entity_id: str
    names: tuple[str, ...]
    labels: tuple[str, ...]
    display: str

    @classmethod
    def of(
        cls,
        entity_id: str,
        extra_names: tuple[str, ...] = (),
        *,
        display: str | None = None,
    ) -> EntityReference:
        seen: dict[str, str] = {}
        for label in (*extra_names, entity_id):
            clean = str(label or "").strip()
            if clean and clean.lower() not in seen:
                seen[clean.lower()] = clean
        return cls(
            entity_id=entity_id,
            names=tuple(seen),
            labels=tuple(seen.values()),
            display=display or entity_id,
        )

    def in_clause(self, clause: str) -> bool:
        return any(name in clause for name in self.names)

    def verb_after(self, clause: str, verbs: tuple[str, ...], window: int) -> tuple[str, str] | None:
        """第一个后面 ``window`` 字以内跟着动作的写法：(原样写法, 动作)。"""
        for name, label in zip(self.names, self.labels, strict=True):
            verb = _verb_after(clause, name, verbs, window)
            if verb:
                return label, verb
        return None

    def referred_by(self, clauses: list[str], index: int) -> bool:
        return any(_clause_refers_to_entity(clauses, index, name) for name in self.names)


@dataclass(frozen=True, slots=True)
class _FactCheck:
    """一条事实对一段正文的检查输入。"""

    entity: EntityReference
    fact_key: str
    fact_value: str
    value_lower: str
    text_lower: str
    clauses: list[str]
    known_locations: set[str]

    def violation(self, *, expected: str, actual: str, evidence: str) -> ConsistencyViolation:
        return ConsistencyViolation(
            fact_key=self.fact_key,
            expected=expected,
            actual=actual,
            entity_id=self.entity.entity_id,
            evidence=evidence,
            entity_name=self.entity.display,
        )


def _check_alive(check: _FactCheck) -> ConsistencyViolation | None:
    """alive==dead：死去的角色做着活人的动作（回忆 / 闪回 / 遗体之类的框定不算）。"""
    if check.value_lower != "dead":
        return None
    for clause in check.clauses:
        if not check.entity.in_clause(clause):
            continue
        if any(g in clause for g in _DEATH_MEMORY_GUARDS):
            continue  # memory / flashback / corpse framing → not a contradiction
        found = check.entity.verb_after(clause, _ALIVE_ACTION_VERBS, window=10)
        if found:
            label, verb = found
            return check.violation(expected="dead", actual="appears alive in text", evidence=f"{label}…{verb}")
    return None


def _check_location(check: _FactCheck) -> ConsistencyViolation | None:
    """location：正文说角色「还在」另一个已知的地方。"""
    wrong_locs = {loc for loc in check.known_locations if loc and loc != check.value_lower}
    for clause in check.clauses:
        if not check.entity.in_clause(clause):
            continue
        if check.value_lower and check.value_lower in clause:
            continue  # correct location mentioned → assume consistent
        if not any(p in clause for p in _STILL_AT_PHRASES):
            continue
        # 一句里同时出现几个错地名时取最先出现的那个（以前按集合次序取，随进程的哈希种子变）
        present_wrong = min(
            (w for w in wrong_locs if w in clause),
            key=lambda w: (clause.index(w), -len(w), w),
            default=None,
        )
        if present_wrong:
            return check.violation(
                expected=check.fact_value,
                actual=f"text places {check.entity.display} at {present_wrong}",
                evidence=clause[:80],
            )
    return None


def _check_missing_limb(check: _FactCheck) -> ConsistencyViolation | None:
    """missing_limb：角色用了已经失去的肢体（或断了一条胳膊还「双手」做事）。"""
    if not check.value_lower:
        return None
    group = _norm_limb_group(check.value_lower)
    if not group:
        return None
    limb_words = _LIMB_GROUPS[group]
    for clause_index, clause in enumerate(check.clauses):
        if not check.entity.referred_by(check.clauses, clause_index):
            continue
        if any(g in clause for g in _LIMB_MISSING_GUARDS):
            continue  # clause describes the loss, not a use of the limb
        gap = _min_gap(clause, limb_words, _LIMB_ACTION_VERBS)
        if gap is not None and gap <= 6:
            return check.violation(
                expected=f"missing: {check.fact_value}",
                actual="text uses the missing limb",
                evidence=clause[:80],
            )
        # using BOTH hands when one arm is gone (blueprint's 双手握剑 example)
        if group in ("right_arm", "left_arm"):
            gap2 = _min_gap(clause, _BOTH_HANDS, _LIMB_ACTION_VERBS)
            if gap2 is not None and gap2 <= 6:
                return check.violation(
                    expected=f"missing: {check.fact_value}",
                    actual="text uses both hands despite a missing arm",
                    evidence=clause[:80],
                )
    return None


def _check_has_item(check: _FactCheck) -> ConsistencyViolation | None:
    """has_item=lost:X：角色拿出 / 握着已经遗失的物品。"""
    if not check.value_lower.startswith("lost:"):
        return None
    lost_item = check.value_lower.replace("lost:", "").strip()
    if not lost_item:
        return None
    for clause_index, clause in enumerate(check.clauses):
        if not check.entity.referred_by(check.clauses, clause_index):
            continue
        if lost_item not in clause:
            continue
        if any(g in clause for g in _ITEM_LOSS_GUARDS):
            continue
        gap = _min_gap(clause, (lost_item,), _ITEM_POSSESS_VERBS)
        if gap is not None and gap <= 6:
            return check.violation(
                expected=f"item lost: {lost_item}",
                actual="text shows character using the lost item",
                evidence=clause[:80],
            )
    return None


_INCAPACITY_ACTIONS: dict[str, tuple[str, ...]] = {
    "unconscious": tuple(dict.fromkeys((*_ALIVE_ACTION_VERBS, *_SPEAKING_ACTIONS, *_WALKING_ACTIONS))),
    "blind": _VISUAL_ACTIONS,
    "deaf": _HEARING_ACTIONS,
    "mute": _SPEAKING_ACTIONS,
    "paralyzed": _WALKING_ACTIONS,
}


def _check_physical_state(check: _FactCheck) -> ConsistencyViolation | None:
    """physical_state：只认明确、稳定的失能（断肢、昏迷、失明、失聪、失语、瘫痪）。"""
    missing_limb = _physical_state_missing_limb(check.value_lower)
    if missing_limb:
        limb_violation = _check_missing_limb(
            _FactCheck(
                entity=check.entity,
                fact_key="missing_limb",
                fact_value=missing_limb,
                value_lower=missing_limb.lower(),
                text_lower=check.text_lower,
                clauses=check.clauses,
                known_locations=check.known_locations,
            )
        )
        if limb_violation is not None:
            return check.violation(
                expected=check.fact_value,
                actual=limb_violation.actual,
                evidence=limb_violation.evidence,
            )

    state_kind = _physical_state_kind(check.value_lower)
    if state_kind is None:
        return None
    for clause in check.clauses:
        if not check.entity.in_clause(clause) or _clause_has_negated_or_nonactual_action(clause):
            continue
        if state_kind in {"blind", "deaf"} and any(
            guard in clause for guard in _ASSISTIVE_PERCEPTION_GUARDS
        ):
            continue
        found = check.entity.verb_after(clause, _INCAPACITY_ACTIONS[state_kind], window=14)
        if found:
            return check.violation(
                expected=check.fact_value,
                actual=f"text shows an action incompatible with {state_kind}: {found[1]}",
                evidence=clause[:120],
            )
    return None


def _check_appearance(check: _FactCheck) -> ConsistencyViolation | None:
    """appearance：只认结构化的发色 / 瞳色或光头。"""
    contract = _appearance_contract(check.value_lower)
    if contract is None:
        return None
    feature, expected_color = contract
    anchors = _HAIR_ANCHORS if feature in {"hair", "bald"} else _EYE_ANCHORS
    for clause in check.clauses:
        if not check.entity.in_clause(clause) or any(g in clause for g in _APPEARANCE_MEMORY_GUARDS):
            continue
        if feature == "bald":
            if any(marker in clause for marker in ("一头长发", "满头长发", "浓密头发", "thick hair", "long hair")):
                return check.violation(
                    expected=check.fact_value,
                    actual="text gives the character a full head of hair",
                    evidence=clause[:120],
                )
            continue
        expected_aliases = _COLOR_ALIASES.get(expected_color, ())
        if expected_aliases and _min_gap(clause, anchors, expected_aliases) is not None:
            continue
        for actual_color, aliases in _COLOR_ALIASES.items():
            if actual_color == expected_color:
                continue
            gap = _min_gap(clause, anchors, aliases)
            if gap is not None and gap <= 5:
                return check.violation(
                    expected=check.fact_value,
                    actual=f"text describes {feature} colour as {actual_color}",
                    evidence=clause[:120],
                )
    return None


def _check_ability(check: _FactCheck) -> ConsistencyViolation | None:
    """ability：明确的否定契约（cannot: / unable: / lost: / no: …）。"""
    contract = _negative_ability_contract(check.value_lower)
    if contract is None:
        return None
    ability_name, action_tokens = contract
    for clause in check.clauses:
        if not check.entity.in_clause(clause) or _clause_has_negated_or_nonactual_action(clause):
            continue
        found = check.entity.verb_after(clause, action_tokens, window=16)
        if found:
            return check.violation(
                expected=check.fact_value,
                actual=f"text shows forbidden ability '{ability_name}': {found[1]}",
                evidence=clause[:120],
            )
    return None


# 每个可查的事实键一个检查函数；键集合与 taxonomy.CHECKABLE_FACT_KEYS 相同（test 钉住）。
_FACT_CHECKERS = {
    "alive": _check_alive,
    "location": _check_location,
    "missing_limb": _check_missing_limb,
    "has_item": _check_has_item,
    "physical_state": _check_physical_state,
    "appearance": _check_appearance,
    "ability": _check_ability,
}


def check_fact_against_text(
    text_lower: str,
    entity: EntityReference,
    fact_key: str,
    fact_value: str,
    *,
    known_locations: set[str] | None = None,
) -> ConsistencyViolation | None:
    """Contradiction detection for hard facts (blueprint §15: hard facts only).

    Clause-level proximity + synonym matching so the detector survives realistic
    prose, not just textbook-exact phrasings. Returns at most one violation.
    Soft facts (tone, relationship nuance) are deliberately out of scope.
    """
    checker = _FACT_CHECKERS.get(fact_key)
    if checker is None:
        return None
    return checker(
        _FactCheck(
            entity=entity,
            fact_key=fact_key,
            fact_value=fact_value,
            value_lower=fact_value.lower(),
            text_lower=text_lower,
            clauses=_split_clauses(text_lower),
            known_locations=known_locations or set(),
        )
    )
