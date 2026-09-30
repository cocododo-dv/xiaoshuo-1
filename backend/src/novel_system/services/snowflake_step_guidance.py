"""雪花十步的写作指引：每一步的说明、检查清单、评分维度与编辑器字段的提示语 / 占位例句。

2026-09-30 从 ``snowflake_steps.py`` 拆出（B06-09），``snowflake_steps`` 仍然转出这里的每一个名字。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from novel_system.services.snowflake_step_catalog import MATERIALIZATION_REQUIRED_STEPS, step_definition_view


_DEFAULT_GUIDANCE = {
    "instruction": "先写出当前层的可用版本，再根据后续发现回修前面的层级。",
    "checklist": ["保持具体", "保留代价", "让下一层能继续展开"],
    "rubric": {},
    "required_for_materialization": False,
    "timebox_minutes": 60,
    "source": "snowflake_method_summary",
}


_REFERENCE_STEP_INSTRUCTIONS: dict[str, str] = {
    "book_brief": (
        "回答以下三个问题：\n\n"
        "① 我的故事类型/流派是什么？\n"
        "② 这类故事的魅力在哪里？为何读者会喜欢？\n"
        "③ 我理想中的读者是谁？他们的特征是？"
    ),
    "one_sentence_summary": (
        "格式参考：\n"
        "「一位[有特点的角色]必须[完成某个目标]，但[核心障碍阻拦着他]。」\n\n"
        "要求：\n"
        "① 不超过 40 字（原书的尺度是 25 个英文词；能更短更好）\n"
        "② 突出主角独特性\n"
        "③ 明确故事核心目标\n"
        "④ 制造悬念，不透露结局"
    ),
    "one_paragraph_summary": (
        "五句话对应五个结构节点：\n\n"
        "第①句：背景与主角登场\n"
        "第②句：第一灾难→第一幕终点（主角被迫卷入，无法回头）\n"
        "第③句：第二灾难→第二幕中点（世界观被打碎，开始改变）\n"
        "第④句：第三灾难→第二幕终点（局势失控，逼向终局）\n"
        "第⑤句：第三幕→决战与收尾：主角成功还是失败，结局是喜、悲，还是苦乐参半——三种都合法，但要自己选定\n\n"
        "三次灾难各逼一件事：第一灾逼主角投入（再也退不出），第二灾逼他从错误的信念转向正确的（道德前提在这里翻转），第三灾逼所有人走向终局。"
    ),
    "character_sheets": (
        "每个角色包含：\n\n"
        "• 角色定位：主角/反派/导师/伙伴...\n"
        "• 具体目标：这个故事里他要达成什么？\n"
        "• 抽象野心：他人生最深处渴望什么？\n"
        "• 核心价值观：「没有什么比___更重要」（写2–3条，互相有张力）\n"
        "• 阻碍：什么阻止他实现目标？\n"
        "• 顿悟：故事结束时他学到/改变了什么？\n"
        "• 一句话故事线：这个角色自己的故事，一句话\n"
        "• 一段话故事线：把那一句扩成一段——他怎样进入故事、三次灾难怎样打在他身上、他的结局\n\n"
        "⚠️ 每个角色都是自己故事的主角，包括反派。"
    ),
    "short_synopsis": (
        "将第2步的五句话各自扩展为一段：\n\n"
        "第1段：世界观细节、主角背景、初始状态与内心冲突\n"
        "第2段：触发事件，第一灾难的具体过程与影响\n"
        "第3段：第二幕挣扎，第二灾难如何改变主角认知\n"
        "第4段：局势升级，第三灾难如何将所有人逼向绝境\n"
        "第5段：高潮决战走向，主角命运，故事如何收尾"
    ),
    "character_synopses": (
        "每个角色的背景故事包含：\n\n"
        "• 成长经历：哪些关键事件塑造了他的性格？\n"
        "• 内心世界：他真正渴望的是什么？为何渴望？\n"
        "• 在故事中的作用：他如何推动主线剧情？\n"
        "• 与其他角色的关系纠葛\n"
        "• 视角故事：从这个角色的视角把整本书讲一遍（半页到一页）——他看见什么、以为什么、要什么\n\n"
        "⚠️ 特别提示：给反派足够的理解——他相信自己是对的。"
    ),
    "long_synopsis": (
        "将一页梗概的每一段扩展为约一页（600–1000 字，长篇取上限），恰好五段：\n\n"
        "• 加入具体的场景设定（时间、地点、氛围）\n"
        "• 详细的角色行动与反应\n"
        "• 关键对话的要点提示\n"
        "• 情感变化的节点\n"
        "• 次要情节线的穿插\n\n"
        "章节表另列：它是分章的真相，五段展开是场景列表的素材。"
    ),
    "character_bibles": (
        "每个角色全档案包含四个维度：\n\n"
        "【外貌信息】年龄、身高体重、外貌特征、着装风格\n"
        "【性格信息】幽默感、性格类型、爱好偏好\n"
        "【环境信息】家庭背景、教育、工作、重要人际关系\n"
        "【心理信息】最好/最坏的童年记忆、性格优缺点、最大希望与恐惧、人生哲学、「他人眼中的他」vs「他自己眼中的他」"
    ),
    "scene_list": (
        "场景列表格式：\n\n"
        "「编号 | 类型（主动/反应）| 视角人物 | 地点/时间 | 坩埚（困住角色的力量）| 结果/转变」\n\n"
        "⚠️ 铁律三条：\n"
        "① 每个场景必须包含冲突（内部或外部）\n"
        "② 没有冲突的场景→删除\n"
        "③ 一场挫折之后有三种走法：切到另一条 POV 线、直接开下一场主动场景、或在下一目标不明显时写一场反应场景（反应→困境→决定）——反应场是少数，可以缩成两段概述，不要机械交替\n\n"
        "视角人物选这一场里损失最大的那个人——损失最大的人情感最强烈；只用一个 POV 的书无需选择。\n"
        "场景坩埚每场都要新：新旧坩埚可以有部分相同，但至少一部分被打破、至少一部分不同。"
    ),
    "scene_details": (
        "【主动场景】\n"
        "目标：角色想达成什么？好目标过五关——能拍下来、装得进这一场的时间槽、对这个 POV 可能、难但不可笑、合乎他的价值观与志向\n"
        "坩埚：什么力量将角色困在这个处境里？（世界、其他角色、角色自身，或它们的组合）\n"
        "冲突：多轮尝试→受阻的循环，逐级升级——至少两轮，关键场可以很多轮；升到顶就冲破，不平台化\n"
        "挫折：最后一次尝试，越短越好；以主角衡量，结尾比开场更糟（POV 是对手时，对手得手就是挫折）；非赢不可时写带代价的胜利；制造「开放循环」迫使读者翻页\n\n"
        "【反应场景】\n"
        "反应：情感先于理性——用身体/行为呈现，别直说「他很害怕」；合性格；与挫折成比例\n"
        "困境：真正的两难——每个选项都有代价，最好落在角色两条价值观的断层线上（见 04）；两难期间不行动\n"
        "决定：逼着走的一步、能当下一场的目标、承认风险、全押——不承诺就不算决定，一承诺场景就结束\n\n"
        "挫折接反应、或直接接下一个目标；决定接目标——链条不能断，但不要机械交替，反应场是少数。\n"
        "破例要知道理由：某一场故意不按三拍走（收尾的叙述交代、「冲突：无」的过场），在「破例理由」里写明，规则层就不再把缺的三拍算缺失。"
    ),
}


_REFERENCE_GUIDANCE_TIMEBOX = {
    "long_synopsis": 120,
    "scene_list": 180,
    "scene_details": 180,
}


_STEP_GUIDANCE = {
    step_key: {
        "instruction": instruction,
        "checklist": [],
        "timebox_minutes": _REFERENCE_GUIDANCE_TIMEBOX.get(step_key, 60),
        "source": "snowflake_method_summary",
    }
    for step_key, instruction in _REFERENCE_STEP_INSTRUCTIONS.items()
}


_GUIDANCE_CHECKLIST = {
    "book_brief": [
        "写出具体读者承诺，而不是只写宽泛类型。",
        "把类型爽点和压力、阻力、代价、情绪连起来。",
        "安全规则只允许借鉴抽象技法，不复制文本或桥段。",
    ],
    "one_sentence_summary": [
        "在一句因果句里保留主角、目标、阻力和代价。",
        "足够具体，后续场景能据此判断取舍。",
        "除非项目本身需要，否则不要提前交代完整结局。",
    ],
    "one_paragraph_summary": [
        "五句分别承担开局、三次递进转折和结局方向。",
        "每次转折都改变主角下一步能做什么。",
        "灾难链能扩展为场景，不需要重新发明前提。",
    ],
    "scene_list": [
        "保持场景 ID、章节顺序和 POV 连续。",
        "每个场景都有职责：施压、受阻、结果或转向。",
        "场景摘要具体到足以进入下一轮规划。",
    ],
    "scene_details": [
        "每场先选主形态：主动场景或反应场景。",
        "主动场景有目标、冲突、挫折；反应场景有反应、困境、决定。",
        "如果同一场兼有行动和反应，后续字段可以保留为补充。",
    ],
}


_GUIDANCE_RUBRIC = {
    "读者承诺": "后续生成选择能否依据这条承诺被接受或否决？",
    "因果压力": "这一层是否让下一事件更难、更贵或更不可逆？",
    "角色压力": "目标、价值、阻力和变化是否能在页面上看见？",
    "场景可写性": "结果能否变成一个有目标、阻力、代价和转折的具体场景？",
    "连续性": "是否保留项目语言、已确认事实、ID 和顺序？",
}


for _step_key, _guidance in _STEP_GUIDANCE.items():
    _guidance["checklist"] = _GUIDANCE_CHECKLIST.get(
        _step_key,
        [
            "保留已确认事实、ID、顺序和项目语言。",
            "补入具体压力或代价，不只做解释性扩写。",
            "让下一层雪花更容易继续写。",
        ],
    )
    _guidance["rubric"] = deepcopy(_GUIDANCE_RUBRIC)
    _guidance["required_for_materialization"] = _step_key in MATERIALIZATION_REQUIRED_STEPS


_FIELD_HELP: dict[str, dict[str, str]] = {
    "category": {"hint": "故事所属类型或货架位置。", "placeholder": "都市悬疑 / 奇幻冒险 / 现实情感"},
    "target_reader": {"hint": "谁会被这个故事稳定取悦。", "placeholder": "喜欢旧案、关系代价和持续反转的读者。"},
    "story_kind": {"hint": "这到底是哪一种故事体验。", "placeholder": "一个人物在压力下追查真相并付出关系代价的故事。"},
    "delight_reason": {"hint": "读者继续翻页的核心原因。", "placeholder": "每个线索都让真相更近，也让主角付出更多。"},
    "genre_promise": {"hint": "类型给读者的承诺。", "placeholder": "调查推进真相，关系代价放大抉择。"},
    "expected_reader_emotion": {"hint": "希望读者持续感到什么。", "placeholder": "压力、怀疑、心疼和必须继续读的未完成感。"},
    "summary": {"hint": "压缩主角、目标、阻力和代价。", "placeholder": "一名回城女子追查旧案，却发现真相会撕开家族。"},
    "sentences": {"hint": "五句分别承载开局、三次灾难和结局。", "placeholder": "每行一句，保持因果递进。"},
    "paragraphs": {"hint": "把上一层的每个节点扩成一段。", "placeholder": "围绕一个结构节点展开行动、阻力和代价。"},
    "characters": {"hint": "每个重要人物都是自己故事里的主角。", "placeholder": "先添加主角、盟友、对手。"},
    "scenes": {"hint": "每个场景都要推动信息、关系或行动目标变化。", "placeholder": "按章节顺序列出场景。"},
    "scene_crucible": {"hint": "困住人物、让他们不能轻易退出的力量。", "placeholder": "退缩会让上一场损失固化，继续行动又会付出新代价。"},
    "crucible": {"hint": "困住人物、让他们不能轻易退出的力量。", "placeholder": "退缩会让上一场损失固化，继续行动又会付出新代价。"},
    "goal": {"hint": "主动场景里可被拍出来的具体目标：装得进这一场、对他可能、难但不可笑、合乎他的价值观。", "placeholder": "拿到离开许可。"},
    "conflict": {"hint": "多轮尝试和受阻、逐级升级，不只是一次拒绝；回合数没有上限。", "placeholder": "提出证据被否定；要求见律师被拖延；激怒对方反而暴露新风险。"},
    "setback": {"hint": "以主角衡量：结尾更糟，或赢了但付出代价；POV 是对手时，对手得手就是挫折。", "placeholder": "拿到线索，却发现线索指向最亲近的人。"},
    "reaction": {"hint": "先身体和情绪，后理性分析；合性格；与挫折成比例。", "placeholder": "手发抖、反复回想上一场坏消息，随后才意识到真正损失。"},
    "dilemma": {"hint": "两个选择都要付出真实代价，最好落在角色两条价值观的断层线上。", "placeholder": "公开会伤害家人；沉默会让真相再次被掩埋。"},
    "decision": {"hint": "逼着走、能当下一场目标、承认风险、全押——必须触发下一场的新目标。", "placeholder": "决定去见掌握时间线的人。"},
    "exception_reason": {"hint": "故意不按三拍走时的理由；写了理由，缺的三拍与坩埚不再算缺失。", "placeholder": "全书收尾的叙述交代，没有新的冲突。"},
    "cost_requirement": {"hint": "角色为这个选择或结果具体付出了什么代价——免费的选择等于注水。", "placeholder": "拿到线索的同时，永久失去了这个线人的信任。"},
    "moral_premise": {"hint": "人物误信什么，又会学会什么。", "placeholder": "沉默不能保护人，承担代价才可能结束伤害。"},
}


def editor_payload(step_key: str) -> dict[str, Any]:
    editor = deepcopy(step_definition_view(step_key)["editor"])
    _enrich_editor_fields(editor.get("fields") or [])
    return editor


def step_guidance(step_key: str) -> dict[str, Any]:
    return deepcopy(_STEP_GUIDANCE.get(step_key) or _DEFAULT_GUIDANCE)


def _enrich_editor_fields(fields: list[dict[str, Any]]) -> None:
    for field in fields:
        key = str(field.get("key") or "")
        help_text = _FIELD_HELP.get(key)
        if help_text:
            field.setdefault("hint", help_text["hint"])
            field.setdefault("placeholder", help_text["placeholder"])
        if isinstance(field.get("fields"), list):
            _enrich_editor_fields(field["fields"])
        for mode in field.get("scene_modes") or []:
            if isinstance(mode.get("fields"), list):
                _enrich_editor_fields(mode["fields"])
