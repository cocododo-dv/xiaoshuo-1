"""雪花十步的步骤目录（叶子模块：纯数据 + 只读访问器，不 import 任何服务）。

目录（``SNOWFLAKE_STEP_CATALOG``）、步骤顺序、物化的硬门 / 提醒步、呈现方式与篇幅带、确认状态集合都在这里；
2026-09-30 从 ``snowflake_steps.py`` 拆出（B06-09），``snowflake_steps`` 仍然转出这里的每一个名字。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any


SNOWFLAKE_METHOD_VERSION = "2026-04-29.v2"


# 2026-09-13 阶段 C：反应场的呈现方式。full = 整场戏剧化；summary = 两段概述（约 200–500 字）。
# Ingermanson：反应场可以整场写、缩成概述、或略过——第一版做前两档；主动场恒为 full。
# 阶段 C：full（整场戏剧化）/ summary（两段概述）；阶段 I 补上原著的第三个选项 skip（页面上略过，
# 直接进下一场主动场景——反应 / 两难 / 决定照样写，它们决定下一场的目标，也进下一场的设计上下文）。
RENDERING_MODES: tuple[str, ...] = ("full", "summary", "skip")


# summary 场物化时拿到的数值篇幅带：起草 / 长度补丁按数值带硬约束，而不是靠「short」这种提示。
SUMMARY_LENGTH_BAND = "200-500"


def effective_rendering_mode(scene_type: Any, value: Any) -> str:
    """呈现方式的单一收口规则（2026-09-15 阶段 N）。

    - ``summary`` 对两种形态都合法：原著自己的第 1 场就是带两组三拍的「叙述概述」（主动场），
      收尾的几场也是叙述——一个主动场同样可以按两三段概述写；
    - ``skip`` 只对反应场合法（原著：略过反应场，直接进下一场主动场景）；
    - 非法值一律 ``full``。
    """
    mode = str(value or "").strip().lower()
    if mode not in RENDERING_MODES:
        return "full"
    if mode == "skip" and str(scene_type or "").strip().lower() != "reactive":
        return "full"
    return mode


# 阶段 D：第 6 步的分形——一页梗概的五段各扩成约一页，恰好五段。
LONG_SYNOPSIS_PARAGRAPHS = 5


#: 算「这一步已经定了」的步骤状态（确认 / 略过）。过期（stale）的步骤要作者点过「已复核」才算，见
#: ``snowflake_queries.step_gate_satisfied``。
CONFIRMED_STEP_STATUSES: frozenset[str] = frozenset({"approved", "skipped"})


MATERIALIZATION_REQUIRED_STEPS = [
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "scene_list",
    "scene_details",
]


MATERIALIZATION_WARNING_STEPS = [
    "character_sheets",
    "short_synopsis",
    "character_synopses",
    "long_synopsis",
    "character_bibles",
]


QUALITY_POLICY = {
    "flow_mode": "coach_flexible",
    "optional_steps_skippable_with_reason": True,
    "hard_gate": "materialization",
    "hard_required_steps": MATERIALIZATION_REQUIRED_STEPS,
    "warning_only_steps": MATERIALIZATION_WARNING_STEPS,
}


MATERIALIZATION_REQUIREMENTS = {
    "hard_required_steps": MATERIALIZATION_REQUIRED_STEPS,
    "warning_only_steps": MATERIALIZATION_WARNING_STEPS,
    "hard_blocker_statuses": ["missing", "skipped", "stale", "rewrite"],
}


SNOWFLAKE_STEP_CATALOG: list[dict[str, Any]] = [
    {
        "step_key": "book_brief",
        "label": "读者定位",
        "english_label": "Target Audience",
        "phase": "基础准备",
        "description": "明确你的小说类型和目标读者群体。你写作的核心目标是取悦你的读者——先知道为谁写，再决定写什么。",
        "default_draft": {
            "category": "",
            "target_reader": "",
            "story_kind": "",
            "delight_reason": "",
            "genre_promise": "",
            "expected_reader_emotion": "",
            # 阶段 J：全书的叙述人称与时态（Dynamite Scene 第 4 章的两个决定），起草时有约束力。
            "narrative_stance": "",
            "safety_rules": [
                "只借鉴抽象风格、结构经验与节奏手法。",
                "不得复制参考文本的人物、设定、桥段或标志性句式。",
            ],
        },
        "editor": {
            "kind": "form",
            "fields": [
                {"key": "category", "kind": "text", "label": "类型"},
                {"key": "target_reader", "kind": "textarea", "label": "目标读者"},
                {"key": "story_kind", "kind": "textarea", "label": "故事类型"},
                {"key": "delight_reason", "kind": "textarea", "label": "读者会沉迷的原因"},
                {"key": "genre_promise", "kind": "textarea", "label": "类型承诺"},
                {"key": "expected_reader_emotion", "kind": "textarea", "label": "期待读者情绪"},
                # 可选：留白即合法——没填就不算缺失，起草沿用风格参考或模型默认
                {"key": "narrative_stance", "kind": "textarea", "label": "叙述人称与时态", "optional": True},
                {"key": "safety_rules", "kind": "list", "label": "安全规则"},
            ],
        },
    },
    {
        "step_key": "one_sentence_summary",
        "label": "一句话概括",
        "english_label": "One-Sentence Summary",
        "phase": "雪花第1步",
        "description": "用一句话（最好 40 字以内，原书的尺度是 25 个英文词）概括整部小说。这是最强营销工具——让人听完就想说「告诉我更多！」",
        "default_draft": {"summary": ""},
        "editor": {
            "kind": "form",
            "fields": [{"key": "summary", "kind": "textarea", "label": "一句话概括"}],
        },
    },
    {
        "step_key": "one_paragraph_summary",
        "label": "一段话概括",
        "english_label": "One-Paragraph Summary",
        "phase": "雪花第2步",
        "description": "将一句话扩展为五句话，构建三幕结构与三个关键「灾难」转折点。这确保你的故事有扎实的戏剧骨架。",
        "default_draft": {
            "sentences": ["", "", "", "", ""],
            # 三幕灾难不再手填——它是五句脊的派生视图（句 2/3/4 即三次灾难，句 5 即结局）。
            # 由 derive_three_act() 读时派生，不入库，杜绝与五句脊双写漂移。
            "moral_premise": "",
        },
        "editor": {
            "kind": "form",
            "fields": [
                {"key": "sentences", "kind": "sentences", "label": "五个关键句"},
                {"key": "moral_premise", "kind": "textarea", "label": "主题前提"},
            ],
        },
    },
    {
        "step_key": "character_sheets",
        "label": "角色摘要表",
        "english_label": "Character Sheets",
        "phase": "雪花第3步",
        "description": "为每个重要角色创建基础档案。优秀小说由立体角色驱动——角色的价值观冲突产生了你所有的场景冲突。",
        # 阶段 L：protagonist_character_id——全书主角（挫折以此人衡量）；双主角作品由作者显式指定，
        # 缺席时退回「第一个定位为主角的人」。
        "default_draft": {"characters": [], "protagonist_character_id": ""},
        "editor": {
            "kind": "form",
            "fields": [
                {"key": "protagonist_character_id", "kind": "text", "label": "全书主角", "optional": True},
                {
                    "key": "characters",
                    "kind": "characters",
                    "label": "角色摘要表",
                    "template": {
                        "character_id": "",
                        "display_name": "",
                        "role": "",
                        "goal": "",
                        "ambition": "",
                        "values": [],
                        "conflict": "",
                        "epiphany": "",
                        "one_sentence_summary": "",
                        "one_paragraph_summary": "",
                    },
                }
            ],
        },
    },
    {
        "step_key": "short_synopsis",
        "label": "一页梗概",
        "english_label": "Short Synopsis",
        "phase": "雪花第4步",
        "description": "将五句话每一句扩展为一段（约100字），形成约500字的故事骨架。这是故事的第一次「填肉」。",
        "default_draft": {"paragraphs": ["", "", "", "", ""]},
        "editor": {
            "kind": "form",
            "fields": [{"key": "paragraphs", "kind": "paragraphs", "label": "段落梗概"}],
        },
    },
    {
        "step_key": "character_synopses",
        "label": "角色背景故事",
        "english_label": "Character Synopses",
        "phase": "雪花第5步",
        "description": "为每个重要角色写半页到一页的背景故事。理解角色为何成为这样的人，你才能写出他真实可信的行动。",
        "default_draft": {"characters": []},
        "editor": {
            "kind": "form",
            "fields": [
                {
                    "key": "characters",
                    "kind": "character_synopses",
                    "label": "角色背景故事",
                    "template": {
                        "character_id": "",
                        "display_name": "",
                        "role": "",
                        "synopsis": "",
                    },
                }
            ],
        },
    },
    {
        "step_key": "long_synopsis",
        "label": "长篇大纲",
        "english_label": "Long Synopsis",
        "phase": "雪花第6步",
        "description": "把一页梗概的每一段再扩成约一页（五段展开，第 6 步的分形）。章节表可以留空——章是列完场之后的包装决定，整理章节结构时按场景列表提议。",
        # 阶段 D：paragraphs 回到书里的第 6 步——五段各扩自一页梗概的一段（每段约一页）。
        # 章表仍是分章真相（物化分章读的是 chapters）；paragraphs 不再是章行的文本镜像。
        # 历史草稿里「NN 章名：一句话（灾一）」格式的段落只在没有 chapters 时被回退解析。
        "default_draft": {"paragraphs": ["", "", "", "", ""], "chapters": []},
        "editor": {
            "kind": "form",
            "fields": [
                {"key": "paragraphs", "kind": "paragraphs", "label": "五段展开（每段扩自一页梗概的一段）"},
                {
                    "key": "chapters",
                    "kind": "chapters",
                    "label": "章节表",
                    # 阶段 K：章是列完场之后的包装决定——07 可以不出章表，整理章节结构时按场景列表提议。
                    "optional": True,
                    "template": {
                        "row_uid": "",
                        "chapter_seq": 0,
                        "act": 1,
                        "title": "",
                        "summary": "",
                        "spine": "",
                        "chapter_goal": "",
                    },
                },
            ],
        },
    },
    {
        "step_key": "character_bibles",
        "label": "角色全档案",
        "english_label": "Character Bibles",
        "phase": "雪花第7步",
        "description": "为每个角色创建详尽的「角色圣经」，彻底了解你的角色——他们是真实存在于你脑海中的人。",
        "default_draft": {"characters": []},
        "editor": {
            "kind": "form",
            "fields": [
                {
                    "key": "characters",
                    "kind": "character_bibles",
                    "label": "角色全档案",
                    "template": {
                        "character_id": "",
                        "display_name": "",
                        "role": "",
                        "physical_profile": {
                            "age": "",
                            "height": "",
                            "appearance": "",
                            "style": "",
                        },
                        "personality_profile": {
                            "strongest_trait": "",
                            "weakest_trait": "",
                            "humor": "",
                            "preferences": [],
                        },
                        "environment_profile": {
                            "home": "",
                            "family_background": "",
                            "education": "",
                            "work": "",
                            "relationships": "",
                        },
                        "psychological_profile": {
                            "best_memory": "",
                            "worst_memory": "",
                            "deepest_fear": "",
                            "greatest_hope": "",
                            "philosophy": "",
                            "self_image": "",
                            "public_image": "",
                            "character_arc": "",
                        },
                    },
                }
            ],
        },
    },
    {
        "step_key": "scene_list",
        "label": "场景列表",
        "english_label": "Scene List",
        "phase": "雪花第8步",
        "description": "列出小说中每一个场景。场景是小说的基本单位——每个场景必须有冲突，必须是一个完整的缩微故事。",
        "default_draft": {"scenes": []},
        "editor": {
            "kind": "form",
            "fields": [
                {
                    "key": "scenes",
                    "kind": "scene_list",
                    "label": "场景列表",
                    "template": {
                        "row_uid": "",  # client mints uuid on add; immutable sync identity
                        "scene_id": "",  # system-assigned, render as locked chip
                        "chapter_id": "",  # system-assigned, locked
                        "chapter_title": "",
                        "chapter_goal": "",
                        "scene_seq": 1,  # the ONLY reorder handle
                        "pov_character_id": "",
                        "summary": "",
                        "primary_form": "proactive",
                        "scene_type": "proactive",
                        "chapter_role": "",
                        "location": "",
                        "crucible": "",
                    },
                    "readonly_fields": ["scene_id", "chapter_id", "row_uid"],
                }
            ],
        },
    },
    {
        "step_key": "scene_details",
        "label": "场景规划",
        "english_label": "Scene Planning",
        "phase": "雪花第9步",
        "description": "为每个场景规划关键信息。主动场景制造紧张；反应场景让人物消化挫败、做出下一个决定——它是少数，写不写、写多长由节奏决定，不要机械交替。",
        "default_draft": {"scenes": []},
        "editor": {
            "kind": "form",
            "fields": [
                {
                    "key": "scenes",
                    "kind": "scene_details",
                    "label": "场景规划",
                    "template": {
                        "row_uid": "",  # carried from scene_list; immutable sync identity
                        "scene_id": "",  # system-assigned, locked
                        "chapter_id": "",  # system-assigned, locked
                        "title": "",
                        "summary": "",
                        "primary_form": "proactive",
                        "scene_type": "proactive",
                        "location": "",
                        "scene_crucible": "",
                        "crucible": "",
                        "goal": "",
                        "conflict": "",
                        "setback": "",
                        "reaction": "",
                        "dilemma": "",
                        "decision": "",
                        "cost_requirement": "",
                        "target_length_band": "medium",
                        "rendering_mode": "full",
                        "must_include_text": "",
                        "exit_change": "",
                        "hook": "",
                        "beats_json": [],
                        # 阶段 J：原著第 9 步「列出在场人物、描述设定」与场景表的时间戳；分诊第 5 步「写下读者要经历的情绪」。
                        "onstage_chars_json": [],
                        "story_time": "",
                        "expected_reader_emotion": "",
                        # 阶段 N：作者的破例理由——原著「不过关也可以放行，但我要知道理由」（第 22 场「冲突：无」）。
                        "exception_reason": "",
                    },
                    "readonly_fields": ["scene_id", "chapter_id", "row_uid"],
                    "scene_modes": [
                        {
                            "value": "proactive",
                            "label": "主动场景",
                            "fields": [
                                {
                                    "key": "goal",
                                    "kind": "textarea",
                                    "label": "目标",
                                    # 阶段 O：原著好目标的五条——能拍下来、装得进这一场的时间槽、对这个 POV 可能、难但不可笑、合乎他的价值观与志向
                                    "hint": "角色想达成什么？能拍下来（读者知道「赢」长什么样）、装得进这一场的时间槽、对他可能、难但不可笑、合乎他的价值观与志向",
                                    "placeholder": "例：让警探放弃拘留，拿到离开许可",
                                    "rows": 2,
                                },
                                {
                                    "key": "crucible",
                                    "kind": "textarea",
                                    "label": "坩埚",
                                    "hint": "什么力量将角色困住、无法轻易逃脱？",
                                    "placeholder": "例：审讯室是封闭空间，主角有前科，警探手里有伪造的监控截图，且主角手机已被没收",
                                    "rows": 2,
                                },
                                {
                                    "key": "conflict",
                                    "kind": "textarea",
                                    "label": "冲突过程",
                                    # 阶段 O：回合数没有规则——至少两轮，关键场可以很多轮；张力逐级升到顶就冲破，不平台化
                                    "hint": "多轮「尝试→受阻」，逐级升级：至少两轮，全书最重要的场可以拉得很长；升到顶就冲破坩埚，不要平台化",
                                    "placeholder": "① 提供不在场证明→警探拿出监控截图否定\n② 要求见律师→以「证据收集期」为由拒绝\n③ 故意激怒警探犯程序错误→警探更冷静地追问",
                                    "rows": 4,
                                },
                                {
                                    "key": "setback",
                                    "kind": "textarea",
                                    "label": "挫折",
                                    "hint": "以主角衡量结尾更糟（POV 是对手时，对手得手就是挫折）——制造「开放循环」迫使读者翻页",
                                    "placeholder": "例：警探宣布以「妨碍司法」拘留48小时——正好是真凶行动的关键窗口期",
                                    "rows": 2,
                                },
                                {
                                    "key": "cost_requirement",
                                    "kind": "textarea",
                                    "label": "代价要求",
                                    "hint": "角色为这个结果具体付出了什么？免费的选择 = 注水",
                                    "placeholder": "例：虽然拿到了漏洞，但唯一的线人从此断联，这条消息来源永久失去了",
                                    "rows": 2,
                                },
                                {
                                    # 阶段 N：主动场也可以按叙述概述写（原著第 1 场、收尾几场）；略过只给反应场
                                    "key": "rendering_mode",
                                    "kind": "select",
                                    "label": "呈现方式",
                                    "hint": "整场戏剧化，还是两三段叙述概述（约 200–500 字）？原著自己的开场与收尾就是叙述概述",
                                    "options": [
                                        {"value": "full", "label": "完整场"},
                                        {"value": "summary", "label": "概述两段"},
                                    ],
                                },
                                {
                                    "key": "exception_reason",
                                    "kind": "textarea",
                                    "label": "破例理由",
                                    "hint": "这一场故意不按三拍走时写下理由（原著：不过关也可以放行，但要知道理由）；写了理由，缺的三拍与坩埚不再算缺失",
                                    "placeholder": "例：全书收尾的叙述交代，没有新的冲突；读者需要看到每个人的去向",
                                    "rows": 2,
                                    "optional": True,
                                },
                            ],
                        },
                        {
                            "value": "reactive",
                            "label": "反应场景",
                            "fields": [
                                {
                                    "key": "reaction",
                                    "kind": "textarea",
                                    "label": "反应",
                                    # 阶段 O：原著好反应的四条——呈现而不点名、合性格、（有时）反映价值观、与挫折成比例
                                    "hint": "先情感后理性——用身体/行为呈现，别直说「他很害怕」；合这个角色的性格；与上一场的挫折成比例（小挫折一句，大挫折几页）",
                                    "placeholder": "例：主角发现手在颤抖；脑子里反复回放那段被篡改的视频；连警探说话都听不进去；最后才意识到48小时意味着什么",
                                    "rows": 3,
                                },
                                {
                                    "key": "crucible",
                                    "kind": "textarea",
                                    "label": "坩埚",
                                    "hint": "是什么让角色无法回避这个困境？",
                                    "placeholder": "例：真凶今晚就要行动，主角却被关着，而且没有人相信他的话",
                                    "rows": 2,
                                },
                                {
                                    "key": "dilemma",
                                    "kind": "textarea",
                                    "label": "困境",
                                    # 阶段 O：两难落在角色的断层线上——两条互相矛盾的价值观（04 的「没有什么比___更重要」）被逼分出高下
                                    "hint": "真正的两难——每个选项都必须付出代价，最好落在角色的断层线上：他 04 里两条互相矛盾的价值观被逼分出高下；两难期间不行动，只权衡",
                                    "placeholder": "选A：认罪换取假释→永远背负污点，无法再执业，且以后无法追凶\n选B：继续抵抗→今晚真凶得逞，又一条人命，且自己罪名更重",
                                    "rows": 3,
                                },
                                {
                                    "key": "decision",
                                    "kind": "textarea",
                                    "label": "决定",
                                    # 阶段 O：原著好决定的四条——逼着走、能当下一场目标、承认风险、全押；不承诺就不算决定
                                    "hint": "角色最终如何选择？逼着对手走的一步、能当下一场的目标、对自己承认风险、全押——不承诺就不算决定，一承诺场景就结束",
                                    "placeholder": "例：决定认罪——但在签字前悄悄发出了一条给记者的暗语短信",
                                    "rows": 2,
                                },
                                {
                                    "key": "cost_requirement",
                                    "kind": "textarea",
                                    "label": "代价要求",
                                    "hint": "角色为这个决定具体付出了什么？免费的选择 = 注水",
                                    "placeholder": "例：认罪换来的不是安全，而是失去律师执照、也失去了亲手抓到真凶的机会",
                                    "rows": 2,
                                },
                                {
                                    "key": "rendering_mode",
                                    "kind": "select",
                                    "label": "呈现方式",
                                    "hint": "整场戏剧化、两段概述，还是页面上略过？Ingermanson：反应场是少数，可以缩成两段概述（约 200–500 字），也可以干脆略过——三拍照样写，它们决定下一场",
                                    "options": [
                                        {"value": "full", "label": "完整场"},
                                        {"value": "summary", "label": "概述两段"},
                                        {"value": "skip", "label": "略过（不落页）"},
                                    ],
                                },
                                {
                                    "key": "exception_reason",
                                    "kind": "textarea",
                                    "label": "破例理由",
                                    "hint": "这一场故意不按三拍走时写下理由（原著：不过关也可以放行，但要知道理由）；写了理由，缺的三拍与坩埚不再算缺失",
                                    "placeholder": "例：这一场只是让读者喘口气的过场，决定在上一场已经做了",
                                    "rows": 2,
                                    "optional": True,
                                },
                            ],
                        },
                    ],
                }
            ],
        },
    },
]


for _step_definition in SNOWFLAKE_STEP_CATALOG:
    _required_for_materialization = _step_definition["step_key"] in MATERIALIZATION_REQUIRED_STEPS
    _step_definition["required_for_materialization"] = _required_for_materialization
    _step_definition["skippable"] = not _required_for_materialization


STEP_ORDER = {step["step_key"]: index for index, step in enumerate(SNOWFLAKE_STEP_CATALOG)}


def list_step_definitions() -> list[dict[str, Any]]:
    return deepcopy(SNOWFLAKE_STEP_CATALOG)


def get_step_definition(step_key: str) -> dict[str, Any]:
    for step in SNOWFLAKE_STEP_CATALOG:
        if step["step_key"] == step_key:
            return deepcopy(step)
    raise KeyError(step_key)


# 只读视图（B06-04）：工作台每建一次要把整份目录深拷贝十几次、单步定义上百次，全是只读的循环与取值。
# 服务内部只读的地方用视图（不拷贝）；要改、或者要把一部分交出去的地方照旧用上面两个深拷贝访问器。
_STEP_VIEWS: tuple[Mapping[str, Any], ...] = tuple(MappingProxyType(step) for step in SNOWFLAKE_STEP_CATALOG)
_STEP_VIEW_BY_KEY: Mapping[str, Mapping[str, Any]] = MappingProxyType({view["step_key"]: view for view in _STEP_VIEWS})


def step_definition_views() -> tuple[Mapping[str, Any], ...]:
    """十步定义的只读视图，按方法顺序。不拷贝：嵌套的列表 / 字典是目录本身，只许读。"""
    return _STEP_VIEWS


def step_definition_view(step_key: str) -> Mapping[str, Any]:
    """一步定义的只读视图（不拷贝，只许读）；未知步骤与 ``get_step_definition`` 一样抛 ``KeyError``。"""
    return _STEP_VIEW_BY_KEY[step_key]


def step_label(step_key: str) -> str:
    try:
        return str(step_definition_view(step_key).get("label") or step_key)
    except KeyError:
        return step_key
