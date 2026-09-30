"""规则引擎的词表（纯数据）。``FAULT_LEXICONS`` 是「命中即毛病」的那几张——按参考书校准时只动它们；抉择 / 压力 /
代价 / 收尾动作这些「缺席才是毛病」的词表不动。
"""

from __future__ import annotations


MODEL_VOICE_TERMS = (
    "suddenly realized",
    "somehow meaningful",
    "somehow",
    "for some reason",
    "couldn't help but",
    "as if fate",
    "everything changed forever",
    "忽然意识到",
    "突然意识到",
    "不知为何",
    "某种意义上",
    "仿佛命运",
    "气氛十分尴尬",
    "一切都变得",
    "微微一笑",
    "心中一紧",
    "不禁",
    "心头一热",
    "深吸一口气",
    "缓缓说道",
    "淡淡地说",
    "轻轻地",
    "默默地",
    "眼中闪过",
    "嘴角微扬",
    "语气平淡",
    "脸上露出",
    "声音低沉",
    "嘴角勾起",
)


EXPOSITORY_DIALOGUE_TERMS = (
    "as you know",
    "because",
    "let me explain",
    "the truth is",
    "i explain",
    "i must explain",
    "you need to know",
    "因为",
    "所以",
    "其实",
    "你知道",
    "我解释",
    "真相是",
    "这是为了",
)


CHOICE_TERMS = (
    "choose",
    "choice",
    "decide",
    "decision",
    "cannot both",
    "could not both",
    "either",
    " or ",
    "must",
    "had to",
    "cost",
    "pay",
    "risk",
    "give up",
    "refuse",
    "选择",
    "决定",
    "不能同时",
    "要么",
    "还是",
    "必须",
    "代价",
    "牺牲",
    "放弃",
)


PRESSURE_TERMS = (
    "cannot both",
    "could not both",
    "must choose",
    "had to choose",
    "at the cost",
    "risk",
    "or save",
    "or the",
    "pay",
    "give up",
    "不能同时",
    "只能",
    "必须选择",
    "代价",
    "冒险",
    "牺牲",
)


SUMMARY_ENDING_TERMS = (
    "in the end",
    "everything changed forever",
    "from then on",
    "she understood",
    "he understood",
    "finally realized",
    "all of this",
    "这一刻",
    "从此",
    "终于明白",
    "一切都",
    "他知道",
    "她知道",
)


ENDING_ACTION_TERMS = (
    "opened",
    "closed",
    "left",
    "took",
    "put",
    "handed",
    "raised",
    "fell",
    "scraped",
    "ran",
    "turned",
    "crossed",
    "stepped",
    "broke",
    "pressed",
    "held",
    "放",
    "推",
    "开",
    "关",
    "走",
    "递",
    "举",
    "落",
    "转身",
    "按",
    "握",
)


CHAPTER_SET_NEXT_PULL_TERMS = (
    "hook",
    "next",
    "arrived",
    "arrives",
    "left",
    "opened",
    "handed",
    "pressed",
    "sent",
    "called",
    "revealed",
    "refused",
    "released",
    "下一",
    "入口",
    "名单",
    "反证",
    "钩子",
    "推开",
    "递给",
    "交给",
    "离开",
    "按下",
    "拨通",
    "公开",
    "拒绝",
    "证人",
    "保护",
)


NEGATED_ACTION_CONTEXT_TERMS = (
    "no ",
    "not ",
    "never ",
    "without ",
    "did not ",
    "does not ",
    "没有",
    "并未",
    "未曾",
    "不能",
    "不再",
    "无",
)


REPETITIVE_ACTION_TERMS = (
    "turned",
    "looked",
    "nodded",
    "smiled",
    "sighed",
    "stepped",
    "held",
    "opened",
    "closed",
    "转身",
    "看",
    "点头",
    "笑",
    "叹气",
    "走",
    "握",
    "打开",
    "关上",
)


IMAGE_TERMS = (
    "moon",
    "fog",
    "shadow",
    "light",
    "dark",
    "rain",
    "wind",
    "blood",
    "fire",
    "mirror",
    "door",
    "key",
    "water",
    "hand",
    "eye",
    "window",
    "月",
    "雾",
    "影",
    "光",
    "雨",
    "风",
    "血",
    "火",
    "镜",
    "门",
    "钥匙",
    "手",
    "眼",
)


ATMOSPHERIC_IMAGE_TERMS = (
    "moon",
    "shadow",
    "dark",
    "rain",
    "wind",
    "fog",
    "cold",
    "light",
    "月",
    "月光",
    "影",
    "阴影",
    "冷",
    "风",
    "雾",
    "雾气",
    "光",
)


FALSE_CLARITY_TERMS = (
    "she knew",
    "he knew",
    "finally understood",
    "suddenly realized",
    "everything became clear",
    "她知道",
    "他知道",
    "终于明白",
    "忽然意识到",
    "突然意识到",
    "真相必须",
    "一切都变得",
    "心中了然",
    "恍然大悟",
    "这一刻她明白",
    "这一刻他明白",
    "答案已经明确",
)


COST_TERMS = (
    "cost",
    "price",
    "risk",
    "lose",
    "lost",
    "sacrifice",
    "give up",
    "betray",
    "hide",
    "代价",
    "牺牲",
    "失去",
    "冒险",
    "背叛",
    "隐瞒",
    "误伤",
    "放弃",
    "不能同时",
    "只能",
)


DECORATIVE_IMAGE_TERMS = (
    "as if fate",
    "like fate",
    "old wound",
    "destiny",
    "atmosphere",
    "仿佛命运",
    "像旧伤疤",
    "旧伤疤",
    "命运",
    "冷意",
    "回声",
    "氛围",
    "像某种",
    "仿佛",
)


REPORT_DIALOGUE_TERMS = (
    "official report",
    "the report",
    "the truth is",
    "i explain",
    "let me explain",
    "evidence shows",
    "官方报告",
    "真相是",
    "我解释",
    "解释给你听",
    "证据显示",
    "报告里",
    "这是因为",
)


MOTIVE_EXPLANATION_TERMS = (
    "because she",
    "because he",
    "because they",
    "so she",
    "so he",
    "she knew she had to",
    "he knew he had to",
    "为了",
    "因为她",
    "因为他",
    "因为他们",
    "所以她",
    "所以他",
    "她知道自己必须",
    "他知道自己必须",
    "原因是",
)


POETIC_CLOSURE_TERMS = (
    "everything changed forever",
    "as if fate",
    "echo",
    "destiny",
    "一切都变得",
    "仿佛命运",
    "命运",
    "回声",
    "余韵",
    "她知道",
    "他知道",
)


POETIC_CLOSURE_ACTION_TERMS = (
    "opened",
    "closed",
    "left",
    "took",
    "handed",
    "raised",
    "crossed",
    "stepped",
    "pressed",
    "held",
    "放下",
    "推开",
    "打开",
    "关上",
    "走出",
    "递出",
    "举起",
    "落下",
    "转身",
    "按下",
    "握住",
)


PERCEPTION_FILTER_TERMS = (
    "she noticed",
    "he noticed",
    "she felt",
    "he felt",
    "she realized",
    "he realized",
    "she sensed",
    "he sensed",
    "她觉得",
    "他觉得",
    "她看到",
    "他看到",
    "她注意到",
    "他注意到",
    "她感到",
    "他感到",
    "她意识到",
    "他意识到",
    "似乎感觉到",
    "好像听到",
    "仿佛看到",
)


# §8: conflict-too-clean detection terms
CONFLICT_TERMS = (
    "争吵", "怒", "愤怒", "质问", "反对", "拒绝", "吼", "骂", "指责", "控诉",
    "冲突", "对峙", "反驳", "顶嘴", "争执", "不满", "激烈", "摔", "推开",
    "quarrel", "anger", "confront", "refuse", "accuse", "clash", "demand",
)


RECONCILIATION_TERMS = (
    "理解", "原谅", "接受", "点头", "叹气", "妥协", "释然", "握手", "拥抱",
    "和好", "笑了", "放下", "释怀", "微笑", "柔和", "温和", "缓和", "谅解",
    "understand", "forgive", "accept", "nod", "sigh", "compromise", "smile",
    "relent", "soften", "reconcile",
)


# 「命中即毛病」的词表——按参考书校准词表时只动这些；抉择 / 压力 / 代价 / 收尾动作是「缺席才是毛病」，不动
FAULT_LEXICONS: dict[str, tuple[str, ...]] = {
    "model_voice": MODEL_VOICE_TERMS,
    "expository_dialogue": EXPOSITORY_DIALOGUE_TERMS,
    "report_dialogue": REPORT_DIALOGUE_TERMS,
    "summary_ending": SUMMARY_ENDING_TERMS,
    "repetitive_action": REPETITIVE_ACTION_TERMS,
    "image": IMAGE_TERMS,
    "atmospheric_image": ATMOSPHERIC_IMAGE_TERMS,
    "false_clarity": FALSE_CLARITY_TERMS,
    "decorative_image": DECORATIVE_IMAGE_TERMS,
    "motive_explanation": MOTIVE_EXPLANATION_TERMS,
    "poetic_closure": POETIC_CLOSURE_TERMS,
    "perception_filter": PERCEPTION_FILTER_TERMS,
    "conflict": CONFLICT_TERMS,
    "reconciliation": RECONCILIATION_TERMS,
}
