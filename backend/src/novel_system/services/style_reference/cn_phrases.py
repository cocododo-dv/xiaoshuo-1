"""风格参考 · 给作者 / 模型看的中文计数与频率说法（叶子：只依赖 ``value_coercion``）。

习惯句与差距说明一律不写阿拉伯数字：「每千字约三个」「大约每十句一次」「约四成」。声音签名的习惯句
（``voice_signature.render_voice_habits``）与风格步的差距说法（``style_step.level_words``）共用这一份；
标点「常用 / 几乎不用」的门槛表 :data:`PUNCT_HABITS` 同理，学文风卡时写标点句（``learn_card``）也照它。
"""

from __future__ import annotations

from novel_system.services.value_coercion import finite_or_zero

CN_DIGITS = "零一二三四五六七八九"


def cn_int(value: int) -> str:
    """0–999 的中文读法(「两」用于量词前由调用方处理);超出按「上千」。"""
    number = max(0, int(value))
    if number < 10:
        return CN_DIGITS[number]
    if number < 20:
        return "十" + (CN_DIGITS[number % 10] if number % 10 else "")
    if number < 100:
        tens, ones = divmod(number, 10)
        return CN_DIGITS[tens] + "十" + (CN_DIGITS[ones] if ones else "")
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        head = CN_DIGITS[hundreds] + "百"
        if rest == 0:
            return head
        if rest < 10:
            return head + "零" + CN_DIGITS[rest]
        tens, ones = divmod(rest, 10)
        return head + CN_DIGITS[tens] + "十" + (CN_DIGITS[ones] if ones else "")
    return "上千"


def cn_count(value: int) -> str:
    """量词前的数:2 → 「两」,其余同 :func:`cn_int`。"""
    return "两" if int(value) == 2 else cn_int(value)


def rate_phrase(rate: float, unit: str) -> str:
    """每千字的频率 → 「每千字约三个」/「每两千字约一处」/ ""(几乎没有)。"""
    value = finite_or_zero(rate)
    if value >= 1.0:
        return f"每千字约{cn_count(round(value))}{unit}"
    if value >= 0.2:
        return f"每{cn_count(round(1.0 / value))}千字约一{unit}"
    return ""


def every_n_sentences(ratio: float) -> str:
    """每句出现的比例 → 「几乎每句都有」/「大约每十句一次」/ ""(没有)。"""
    value = finite_or_zero(ratio)
    if value <= 0:
        return ""
    n = max(1, int(round(1.0 / value)))
    if n <= 1:
        return "几乎每句都有"
    return f"大约每{cn_count(n)}句一次"


def tenths_phrase(share: float) -> str:
    """0–1 的比例 → 「约四成」/「不到一成」/「几乎全部」。"""
    value = finite_or_zero(share)
    if value >= 0.95:
        return "几乎全部"
    if value < 0.05:
        return "不到一成"
    return f"约{cn_int(max(1, round(value * 10)))}成"


# 标点「常用 / 很少用」的绝对门槛(每千字):(测量核特征, 标点的名字, 低于它算几乎不用, 高于它算常用)。
PUNCT_HABITS: tuple[tuple[str, str, float, float], ...] = (
    ("punct_ellipsis_per_1k", "省略号", 0.2, 2.0),
    ("punct_dash_per_1k", "破折号", 0.2, 1.5),
    ("punct_semicolon_per_1k", "分号", 0.2, 1.0),
    ("punct_exclamation_per_1k", "感叹号", 0.5, 4.0),
    ("punct_question_per_1k", "问号", 0.5, 6.0),
    ("punct_colon_per_1k", "冒号", 0.3, 4.0),
    ("punct_enumeration_per_1k", "顿号", 0.3, 5.0),
)


__all__ = [
    "CN_DIGITS",
    "PUNCT_HABITS",
    "cn_count",
    "cn_int",
    "every_n_sentences",
    "rate_phrase",
    "tenths_phrase",
]
