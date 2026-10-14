"""事实键的词表：抽取 / 手工补录写进账本的键与值，统一成硬质检认得的写法（叶子：只 import ``taxonomy``）。

键写成 ``<种类>`` 或 ``<种类>:<槽>``。硬质检认得七个种类（``taxonomy.CHECKABLE_FACT_KEYS``）；其中
``has_item`` / ``appearance`` / ``ability`` 按槽分开记——一件物品、一处外貌、一种能力各占一条，后来的同槽事实
才盖过它（重放按键取最新的一条：以前一个人只有一格 ``has_item``，拿到第二件东西就把第一件盖掉了）。槽外的值用
几个固定的英文词（``dead`` / ``alive``、``held`` / ``lost``、``cannot`` / ``can``），作者界面再翻成中文。

``checkable_form`` 把带槽的键并回各检查器认的写法（``has_item:断剑`` + ``lost`` → ``lost:断剑``，与早先手写的
``has_item = lost:断剑`` 同一个契约）。别的键照旧只进起草提示词，不做关键词矛盾检查。

``normalize_fact`` 只在意思明确的时候改写：种类认得、值认得、物品名干净才换成规范写法，拿不准就原样留着——
规范键会进阻断级的硬质检，错认一条比漏认一条代价大。
"""
from __future__ import annotations

import re

from novel_system.services.narrative.taxonomy import CHECKABLE_FACT_KEYS

# 抽取输出里 fact_kind 的取值：七个可查种类 + other（其余一切，键名自由）。次序就是提示词里列的次序。
FACT_KINDS = ("alive", "location", "has_item", "missing_limb", "physical_state", "appearance", "ability")
OTHER_KIND = "other"
EXTRACTION_FACT_KINDS = (*FACT_KINDS, OTHER_KIND)
# 按槽分开记的种类
SLOTTED_KINDS = frozenset({"has_item", "appearance", "ability"})
# 只有这两类事件说的是人物自己的可见状态；「得知」「关系变化」的事实永远不是可查种类（林远得知谁拿着钥匙，
# 不等于林远拿着钥匙）。重放按实体叠事实、不看事件类型，所以这两类事件要是用了可查的键名，就加前缀让开：
# 不然「得知 location = 苏晚在码头」会被硬质检当成林远自己在码头。
STATE_EVENT_TYPES = frozenset({"character_state", "location_change"})
_NON_STATE_KEY_PREFIXES = {"character_learns": "learned_", "relation_change": "relation_"}

DEAD = "dead"
ALIVE = "alive"
HELD = "held"
LOST = "lost"
CANNOT = "cannot"
CAN = "can"

_DEAD_WORDS = (
    "dead", "deceased", "killed", "died", "死亡", "已死", "身亡", "遇害", "被杀", "死去", "丧命", "毙命",
    "去世", "过世", "牺牲", "殒命", "死了", "被害",
)
# 带这些字就不当「已死」：假死、没死、生还……
_DEATH_NEGATIONS = ("没", "未", "非", "不是", "假", "诈", "幸免", "生还", "活着", "复活", "not ", "fake")
_ALIVE_WORDS = ("alive", "活着", "生还", "幸存", "没死", "未死", "仍在世", "还在世")

_ITEM_RECOVERED_WORDS = ("找回", "取回", "寻回", "夺回", "拿回", "要回", "失而复得", "recovered", "regained", "got back")
_ITEM_LOST_WORDS = (
    "lost", "destroyed", "given away", "given_away", "confiscated", "stolen", "失去", "丢失", "遗失", "丢了",
    "弄丢", "被夺", "被抢", "被偷", "被收走", "被没收", "没收", "收缴", "上交", "交出", "交给", "送给", "送出",
    "交由", "转交", "烧毁", "销毁", "损毁", "毁掉", "毁了", "砸碎", "不见了", "没了",
)
_ITEM_HELD_WORDS = (
    "held", "holds", "has", "obtained", "gained", "acquired", "received", "持有", "获得", "得到", "拿到", "取得",
    "捡到", "带着", "收下", "保管", "拥有", "获取", "到手", "收起", "揣着",
)

_ABILITY_REGAINED_WORDS = ("regained", "恢复", "重新能", "重新会", "学会", "又能")
_ABILITY_LOST_WORDS = ("cannot", "can't", "unable", "lost", "不能", "不会", "无法", "失去", "丧失", "没法", "再也")
_ABILITY_HELD_WORDS = ("can", "able", "能够", "会")

_APPEARANCE_SLOTS = {
    "hair": ("hair", "hair_color", "头发", "发色", "发"),
    "eyes": ("eye", "eyes", "eye_color", "眼睛", "瞳色", "眼眸", "瞳孔", "眸色"),
}

# 规范种类之外常见的键名（抽取模型自己起的、作者手填的）：只认意思没有歧义的几个
_KEY_ALIASES: dict[str, str] = {
    **dict.fromkeys(
        ("current_location", "whereabouts", "position", "place", "位置", "所在", "所在地", "地点", "去向"),
        "location",
    ),
    **dict.fromkeys(
        ("alive_status", "life_status", "is_alive", "is_dead", "dead", "death", "died", "生死", "存活", "死亡", "生死状态"),
        "alive",
    ),
    **dict.fromkeys(
        ("physical_condition", "health", "health_status", "consciousness", "身体状况", "身体状态", "健康", "意识"),
        "physical_state",
    ),
    **dict.fromkeys(("lost_limb", "amputation", "amputated_limb", "断肢", "残肢", "失去肢体"), "missing_limb"),
}
# 物品类的键：值里得读出一件干净的物品名才改写；带方向的键顺带给出得失
_ITEM_KEYS: dict[str, str | None] = {
    **dict.fromkeys(("item", "items", "holds", "possesses", "possession", "inventory", "持有", "持有物品", "物品"), None),
    **dict.fromkeys(
        (
            "item_gained", "gained_item", "obtained_item", "acquired_item", "received_item", "item_obtained",
            "item_acquired", "item_received", "picked_up", "获得物品", "得到物品",
        ),
        HELD,
    ),
    **dict.fromkeys(
        (
            "item_lost", "lost_item", "dropped_item", "gave_away", "item_given", "item_given_away",
            "confiscated_item", "destroyed_item", "失去物品", "丢失物品",
        ),
        LOST,
    ),
}
_ITEM_VERB_PREFIX_RE = re.compile(
    r"^(?:获得了|获得|得到了|得到|拿到了|拿到|获取了|获取|取得了|取得|捡到了|捡到|收下了|收下|持有|带着|"
    r"丢失了|丢失|失去了|失去|遗失了|遗失|丢了|交出了|交出|上交了|上交)"
)
_ITEM_NAME_MAX_CHARS = 16
_SLOT_MAX_CHARS = 40
_UNCLEAN_ITEM_RE = re.compile(r"[。！？!?；;，,、：:\s（）()]")
_SLOT_QUOTES = "「」『』“”\"'‘’《》[]【】"


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _clean_slot(raw: str) -> str:
    return str(raw or "").strip().strip(_SLOT_QUOTES).strip()[:_SLOT_MAX_CHARS]


def split_fact_key(fact_key: str) -> tuple[str, str]:
    """(种类, 槽)：可查种类的键不分大小写，槽原样保留；不是可查种类 → (整个键, "")。"""
    key = str(fact_key or "").strip()
    head, sep, slot = key.partition(":")
    kind = head.strip().lower()
    if kind in CHECKABLE_FACT_KEYS:
        return kind, _clean_slot(slot) if sep else ""
    return key, ""


def is_dead_value(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    if lowered == DEAD:
        return True
    return _contains_any(lowered, _DEAD_WORDS) and not _contains_any(lowered, _DEATH_NEGATIONS)


def _alive_value(value: str) -> str | None:
    lowered = str(value or "").strip().lower()
    if is_dead_value(lowered):
        return DEAD
    if _contains_any(lowered, _ALIVE_WORDS):
        return ALIVE
    return None


def _item_value(value: str) -> str | None:
    lowered = str(value or "").strip().lower()
    if lowered in {HELD, LOST}:
        return lowered
    if _contains_any(lowered, _ITEM_RECOVERED_WORDS):
        return HELD
    if _contains_any(lowered, _ITEM_LOST_WORDS):
        return LOST
    if _contains_any(lowered, _ITEM_HELD_WORDS):
        return HELD
    return None


def _ability_value(value: str) -> str | None:
    lowered = str(value or "").strip().lower()
    if lowered in {CANNOT, CAN}:
        return lowered
    if _contains_any(lowered, _ABILITY_REGAINED_WORDS):
        return CAN
    if _contains_any(lowered, _ABILITY_LOST_WORDS):
        return CANNOT
    if _contains_any(lowered, _ABILITY_HELD_WORDS):
        return CAN
    return None


def _appearance_slot(slot: str) -> str:
    lowered = slot.lower()
    for canonical, aliases in _APPEARANCE_SLOTS.items():
        if lowered in aliases:
            return canonical
    return slot


def _item_name(value: str) -> str | None:
    """值里读出的一件干净的物品名（去掉「获得了」之类的动词头）；像一句话就 None。"""
    name = _ITEM_VERB_PREFIX_RE.sub("", _clean_slot(value)).strip()
    if not name or len(name) > _ITEM_NAME_MAX_CHARS or _UNCLEAN_ITEM_RE.search(name):
        return None
    return name


def _normalize_has_item(slot: str, value: str, direction: str | None) -> tuple[str, str] | None:
    """物品事实的规范写法 ``has_item:<物品>`` = held / lost；读不出物品名 → None。"""
    clean_value = str(value or "").strip()
    if not slot:
        head, sep, rest = clean_value.partition(":")
        if sep and head.strip().lower() in {LOST, "失去", "丢失", "遗失"}:
            # 早先的写法 has_item = lost:断剑
            item = _clean_slot(rest)
            return (f"has_item:{item}", LOST) if item else None
        item = _item_name(clean_value)
        if item is None:
            return None
        return f"has_item:{item}", direction or _item_value(clean_value) or HELD
    return f"has_item:{slot}", direction or _item_value(clean_value) or clean_value


def normalize_fact(event_type: str, fact_key: str, fact_value: str) -> tuple[str, str]:
    """规范的 (键, 值)。意思拿不准就原样返回（键、值都去掉首尾空白）。"""
    key = str(fact_key or "").strip()
    value = str(fact_value or "").strip()
    if event_type not in STATE_EVENT_TYPES:
        prefix = _NON_STATE_KEY_PREFIXES.get(event_type)
        if prefix and split_fact_key(key)[0] in CHECKABLE_FACT_KEYS:
            return f"{prefix}{key}", value
        return key, value
    if event_type == "location_change":
        return "location", value
    kind, slot = split_fact_key(key)
    if kind not in CHECKABLE_FACT_KEYS:
        alias = key.lower().replace("-", "_").replace(" ", "_")
        if alias in _ITEM_KEYS:
            return _normalize_has_item("", value, _ITEM_KEYS[alias]) or (key, value)
        kind = _KEY_ALIASES.get(alias, "")
        if not kind:
            return key, value
        if kind == "alive" and _alive_value(value) is None:
            return key, value
    if kind == "alive":
        return "alive", _alive_value(value) or value
    if kind == "has_item":
        return _normalize_has_item(slot, value, None) or (key, value)
    if kind == "ability":
        if not slot:
            return "ability", value
        return f"ability:{slot}", _ability_value(value) or value
    if kind == "appearance":
        return (f"appearance:{_appearance_slot(slot)}" if slot else "appearance"), value
    return kind, value


def compose_extracted_fact(event_type: str, fact_kind: str, fact_key: str, fact_value: str) -> tuple[str, str]:
    """抽取模型给的 (种类, 键, 值) → 账本里的 (键, 值)。

    种类是结构化输出里的枚举（中转按 schema 约束解码，选不出表外的值）；带槽的种类把 ``fact_key`` 当槽（物品名、
    发色 / 瞳色、能力），``other`` 照旧用模型起的键。「得知」「关系变化」一律按 other 处理。"""
    kind = str(fact_kind or "").strip().lower()
    key = str(fact_key or "").strip()
    if event_type not in STATE_EVENT_TYPES or kind not in FACT_KINDS:
        return normalize_fact(event_type, key, fact_value)
    if kind in SLOTTED_KINDS:
        slot = key[len(kind) + 1:] if key.lower().startswith(f"{kind}:") else key
        slot = "" if slot.strip().lower() == kind else slot
        return normalize_fact(event_type, f"{kind}:{slot}" if slot.strip() else kind, fact_value)
    return normalize_fact(event_type, kind, fact_value)


def checkable_form(fact_key: str, fact_value: str) -> tuple[str, str] | None:
    """硬质检读的 (种类, 值)：带槽的键并回各检查器认的写法；不是可查种类 → None。"""
    kind, slot = split_fact_key(fact_key)
    if kind not in CHECKABLE_FACT_KEYS:
        return None
    value = str(fact_value or "").strip()
    if not slot or kind not in SLOTTED_KINDS:
        return kind, value
    lowered = value.lower()
    if kind == "has_item":
        return kind, f"lost:{slot}" if lowered == LOST else value
    if kind == "ability":
        return kind, f"cannot:{slot}" if lowered == CANNOT else value
    canonical_slot = _appearance_slot(slot)
    if canonical_slot == "hair" and lowered in {"bald", "光头", "秃头", "头发全无"}:
        return kind, lowered
    return kind, f"{canonical_slot}:{value}"
