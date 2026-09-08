"""启发式段落分类(无 LLM)。

NOVEL_SYSTEM_LLM_ENABLED=false 默认下的 fallback 路径。8 类 ParagraphType:
- dialogue / narration / psychology / description_env / description_char
- action / transition / flashback

按设计文档 §6.2 与 plan,本启发式 confidence=0.5,calibration 中标
`fallback_to_heuristic=true`;PR-5 前端在 books detail 显示降级横幅。

2026-09 v2(风格模仿 v2 · W2)修正三处系统性误判:
- 「含引号即对话」→「引号内字数 ≥ 段落一半 / 以 说：道：问： 等引导 / 引号领起或收尾」,
  并补齐 `‘’`(鲁迅等公版文本的对白引号);
- 「<30 字一律 transition」→ 只有含切换词(次日 / 回到 / 与此同时 / 后来 …)或章节标题
  形态才判 transition;
- 无切换词、无其它特征的短段**继承前一段(非 transition)类型**,narration 兜底——
  短段是叙事节拍的延续,不是结构切换。
"""

from __future__ import annotations

import re
from functools import lru_cache

from novel_system.services.style_reference.config_loader import load_optional_yaml_config
from novel_system.services.style_reference.segmentation.types import (
    ParagraphClassification,
    SegmentationResult,
)

# 启发式标记词
_DIALOGUE_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ('"', '"'),
)
_DIALOGUE_QUOTES = tuple(dict.fromkeys(q for pair in _DIALOGUE_QUOTE_PAIRS for q in pair))
_DIALOGUE_OPENERS = "“‘「『\""
_DIALOGUE_CLOSERS = "”’」』\""
_FLASHBACK_MARKERS = ("记得", "那年", "从前", "昔日", "旧时", "想起", "回忆", "当年")
_PSYCHOLOGY_MARKERS = ("想着", "觉得", "暗忖", "恍惚", "心里", "心中", "暗想", "想到")
_ACTION_VERBS = ("走", "跑", "推", "拉", "握", "扑", "转身", "起身", "蹲", "迈", "撞")
_ENV_MARKERS = ("屋", "山", "天", "路", "院", "墙", "树", "河", "雪", "雾", "云", "风", "雨")
_CHAR_MARKERS = ("脸", "眼", "眉", "嘴", "手", "穿着", "身上", "头发", "胡子", "皱纹")
# 场景 / 时间切换词:只对短段生效(长段含「后来」是叙述,不是切换)
_TRANSITION_MARKERS = (
    "次日", "翌日", "第二天", "第二日", "隔日", "隔天", "当晚", "当夜", "次年", "第二年",
    "日后", "之后", "以后", "过了", "转眼", "不久", "从此", "此后", "自此", "一晃",
    "回到", "与此同时", "同一时刻", "另一边", "另一头", "另一方面",
    "后来", "且说", "话说", "有一天", "这一天", "那一天", "一天早上", "一天晚上",
    "天亮", "天黑", "黄昏", "傍晚", "清晨", "半夜", "深夜", "几天", "几日", "几个月", "几年",
    "多年",
)
# 以「说 / 道 / 问」等引导引号(允许 `：` `，` 或 `——` 连接):`说道，‘…’` `问：“…”`
_SPEECH_VERBS = (
    "说道|说|道|问道|问|答道|答|喊道|喊|叫道|叫|嚷道|嚷|笑道|应道|应|念道|念|骂道|骂|"
    "唤道|唤|吼道|吼|劝道|劝|哼|嘀咕|嘟囔|喃喃|低声|大声|轻声|道是|回答"
)
_SPEECH_LEAD_RE = re.compile(
    r"(?:" + _SPEECH_VERBS + r")\s*[：:，,]?\s*(?:——|—)?\s*[" + re.escape(_DIALOGUE_OPENERS) + r"]"
)
# 引号收尾后紧跟说话人:`’他说。` `’驼背五少爷点着头说。`
_SPEECH_TAIL_RE = re.compile(
    r"[" + re.escape(_DIALOGUE_CLOSERS) + r"][^。！？!?…\n]{0,16}?(?:" + _SPEECH_VERBS + r")"
)
# 段尾以「说：」「喝道：——」收束,引语在下一段(对白的叙述引入段)
_SPEECH_LEAD_OUT_RE = re.compile(
    r"(?:" + _SPEECH_VERBS + r")\s*[：:，,]\s*(?:——|—)?\s*$"
)
# 单字引导动词(道 / 应 / 叫 / 念 / 说 / 问 …)会命中非言说复合词(知道 / 应该 / 道理 /
# 小说 / 据说 / 问题 …)。与 voice_signature 同源:先把 function_words.yaml
# `speech_verbs.exclusions` 从段落里剥离,再跑三条引导 / 收尾正则。补充项只在启发式
# 里用——加进 yaml 会改动 voice_baseline.yaml 的对白引导特征(需 build-baseline 重建)。
_EXTRA_SPEECH_EXCLUSIONS: tuple[str, ...] = ("叫做", "念头", "道具")
# 章节标题形态:第X章 / 卷X / 一 / (一) / 《题名》 / 序 / 楔子 …,可带 ≤30 字副题
_TITLE_RE = re.compile(
    r"^(?:"
    r"第\s*[零〇一二三四五六七八九十百千两\d]+\s*[章节回卷部集幕场篇]"
    r"|卷\s*[零〇一二三四五六七八九十百\d]+"
    r"|[一二三四五六七八九十百]{1,3}"
    r"|\d{1,3}"
    r"|[（(]\s*[一二三四五六七八九十\d]+\s*[）)]"
    r"|序[章幕言]?|楔子|尾声|后记|番外|引子|终章|上篇|中篇|下篇"
    r"|chapter\s*\d+"
    r"|《[^》]{1,40}》"
    r")(?:\s*[：:·—\-\s]\s*\S{1,30})?$",
    re.IGNORECASE,
)
_SENTENCE_END_CHARS = "。！？!?…"
_SHORT_PARAGRAPH_CHARS = 30
_TITLE_MAX_CHARS = 40
_INHERITABLE_TYPES = frozenset(
    {
        "dialogue",
        "narration",
        "psychology",
        "description_env",
        "description_char",
        "action",
        "flashback",
    }
)
_HEURISTIC_CONFIDENCE = 0.5


def _quoted_char_count(body: str) -> int:
    """引号内字数之和;未闭合的开引号计到段尾(多段连续引语的常见排版)。"""
    total = 0
    for opener, closer in _DIALOGUE_QUOTE_PAIRS:
        if opener not in body:
            continue
        if opener == closer:
            parts = body.split(opener)
            total += sum(len(parts[i]) for i in range(1, len(parts), 2))
            continue
        pattern = re.escape(opener) + r"([^" + re.escape(closer) + r"]*)(?:" + re.escape(closer) + r"|$)"
        total += sum(len(m.group(1)) for m in re.finditer(pattern, body, flags=re.DOTALL))
    return total


@lru_cache(maxsize=1)
def _speech_exclusions() -> tuple[str, ...]:
    """function_words.yaml `speech_verbs.exclusions` ∪ 启发式补充项,长词在前;缺文件时只剩补充项。"""
    raw = load_optional_yaml_config("function_words")
    speech = raw.get("speech_verbs") if isinstance(raw.get("speech_verbs"), dict) else {}
    items = speech.get("exclusions") if isinstance(speech, dict) else None
    words = [str(item or "").strip() for item in (items if isinstance(items, list) else [])]
    words.extend(_EXTRA_SPEECH_EXCLUSIONS)
    return tuple(sorted({word for word in words if word}, key=lambda word: (-len(word), word)))


def _strip_speech_exclusions(text: str) -> str:
    """剥离非言说复合词,只供引导 / 收尾动词正则使用(引语占比仍按原文算)。"""
    for word in _speech_exclusions():
        if word in text:
            text = text.replace(word, "")
    return text


def _is_dialogue(body: str) -> bool:
    """引号内字数 ≥ 段落一半;或以说 / 道 / 问引导(含段尾「说：」引入下一段);
    或引号领起 / 收尾且引语占比 ≥ 15%。"""
    stripped = body.strip()
    if not stripped:
        return False
    speech_view = _strip_speech_exclusions(stripped)
    if _SPEECH_LEAD_OUT_RE.search(speech_view):
        return True
    if not any(q in stripped for q in _DIALOGUE_QUOTES):
        return False
    quoted = _quoted_char_count(stripped)
    if quoted <= 0:
        return False
    if quoted * 2 >= len(stripped):
        return True
    if _SPEECH_LEAD_RE.search(speech_view) or _SPEECH_TAIL_RE.search(speech_view):
        # 引导词只在短段、或引语占比 ≥15% 的长段上判对白：长叙述段里嵌一句
        # 「说道，‘…’」不该让整段变成 dialogue（v2 集成复核）。
        if len(stripped) <= 120 or quoted * 100 >= len(stripped) * 15:
            return True
    quote_led = stripped.lstrip("—－-–　 ")[:1] in _DIALOGUE_OPENERS
    quote_ended = stripped.rstrip(_SENTENCE_END_CHARS)[-1:] in _DIALOGUE_CLOSERS
    return (quote_led or quote_ended) and quoted * 100 >= len(stripped) * 15


def _is_title_shape(body: str) -> bool:
    stripped = body.strip()
    if not stripped or len(stripped) > _TITLE_MAX_CHARS:
        return False
    if any(ch in stripped for ch in _SENTENCE_END_CHARS):
        return False
    return _TITLE_RE.match(stripped) is not None


def _is_transition(body: str) -> bool:
    if _is_title_shape(body):
        return True
    if len(body) >= _SHORT_PARAGRAPH_CHARS:
        return False
    return any(marker in body for marker in _TRANSITION_MARKERS)


def _heuristic_classify_one(
    body: str,
    previous_type: str | None = None,
) -> tuple[str, float]:
    """对单段返回 (paragraph_type, confidence)。confidence 固定 0.5。

    规则优先级(高 → 低,确保语义特征压过纯字数判断):
    0. transition      章节标题形态(《题名》/ 第X章 / 一 …)
    1. dialogue        引语 ≥ 段落一半 / 说·道·问引导(含段尾「说：」)/ 引号领起·收尾且引语 ≥ 15%
    2. flashback       时间追忆标记词
    3. psychology      心理动词
    4. action          ≥3 个动作动词
    5. description_env ≥4 个环境名词
    6. description_char≥3 个人物描写名词
    7. transition      短段(<30 字)含场景 / 时间切换词
    8. 继承            短段无任何特征 → 前一段(非 transition)类型
    9. narration       兜底

    ``previous_type`` 由 `classify_heuristic_sequence` 传入(上一段的非 transition
    类型);单段调用时为 None,短段直接落 narration。
    """
    # 0. 章节标题形态是结构标记,先于一切语义规则
    if _is_title_shape(body):
        return "transition", _HEURISTIC_CONFIDENCE
    # 1. dialogue
    if _is_dialogue(body):
        return "dialogue", _HEURISTIC_CONFIDENCE
    # 2. flashback:时间追忆标记
    if any(w in body for w in _FLASHBACK_MARKERS):
        return "flashback", _HEURISTIC_CONFIDENCE
    # 3. psychology:心理动词
    if any(w in body for w in _PSYCHOLOGY_MARKERS):
        return "psychology", _HEURISTIC_CONFIDENCE
    # 4. action:多个动作动词
    action_hits = sum(body.count(v) for v in _ACTION_VERBS)
    if action_hits >= 3:
        return "action", _HEURISTIC_CONFIDENCE
    # 5. description_env:多个环境名词
    env_hits = sum(body.count(m) for m in _ENV_MARKERS)
    if env_hits >= 4:
        return "description_env", _HEURISTIC_CONFIDENCE
    # 6. description_char:多个人物描写名词
    char_hits = sum(body.count(m) for m in _CHAR_MARKERS)
    if char_hits >= 3:
        return "description_char", _HEURISTIC_CONFIDENCE
    # 7. transition:短段含切换词
    if _is_transition(body):
        return "transition", _HEURISTIC_CONFIDENCE
    # 8. 短段无特征:继承前一段类型(短段是节拍延续,不是结构切换)
    if len(body) < _SHORT_PARAGRAPH_CHARS and previous_type in _INHERITABLE_TYPES:
        return str(previous_type), _HEURISTIC_CONFIDENCE
    # 9. 兜底:narration
    return "narration", _HEURISTIC_CONFIDENCE


def classify_heuristic_sequence(bodies: list[str]) -> list[tuple[str, float]]:
    """按顺序分类一组段落正文,短段继承上一段的非 transition 类型。"""
    results: list[tuple[str, float]] = []
    previous_type: str | None = None
    for body in bodies:
        ptype, conf = _heuristic_classify_one(body, previous_type=previous_type)
        results.append((ptype, conf))
        if ptype in _INHERITABLE_TYPES:
            previous_type = ptype
    return results


def classify_heuristic(
    paragraphs: list[tuple[int, int, str]],
) -> SegmentationResult:
    """对全部段执行启发式分类(顺序感知,见 `classify_heuristic_sequence`)。"""
    classified = classify_heuristic_sequence([body for _start, _end, body in paragraphs])
    classifications: list[ParagraphClassification] = [
        ParagraphClassification(
            paragraph_index=idx,
            paragraph_type=ptype,
            confidence=conf,
            classifier_confidence_level="low",
        )
        for idx, (ptype, conf) in enumerate(classified)
    ]

    calibration = {
        "fallback_to_heuristic": True,
        "anchor_size": 0,
        "fast_model_agreement": None,
        "fallback_to_strong": False,
    }
    return SegmentationResult(classifications=classifications, calibration=calibration)
