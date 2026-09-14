"""Style Reference v1.1 文本工具(纯函数)。

本文件的函数全部从仓库内已验证的实现拷贝而来,**不通过 import 旧模块复用**
(全局纪律 C:新模块不依赖旧模块);旧模块在整体下线时一并删除。

拷贝来源:
- `decode_text`             ← `services/reference_learning.py:1638-1644` `_decode_text`
- `normalize_text`          ← `services/reference_learning.py:2123-2124` `_normalize_text`
- `compute_text_checksum`   ← `services/reference_learning.py:199` 内联实现
- `split_paragraphs`        ← `services/reference_learning.py:2131` 内联 + offset 追踪
- `split_sentences`         ← `services/literary_quality.py:1701-1702` `_sentences`(扩 `…`;
  2026-09 v2 重写:闭引号归并前句、ASCII 句点仅在空白 / 行尾前切、不产生纯标点片段)
- `extract_dialogue_spans`  ← `services/literary_quality.py:1694-1698` `_dialogue_spans`
- `compact_ws`              ← `services/literary_quality.py:1748-1749` `_compact_ws`

新模块的 book_id 命名为 `sr_book_{sha256[:12]}`(旧模块用 `refbook_{sha256[:12]}`)。
"""

from __future__ import annotations

import hashlib
import re

from novel_system.services.errors import DomainError


def decode_text(raw: bytes) -> str:
    """按 UTF-8 → GB18030 顺序尝试解码;均失败 raise DomainError。"""
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DomainError(
        "STYLE_REFERENCE_BOOK_ENCODING_UNSUPPORTED",
        "reference book must be UTF-8 or GB18030 text",
        status_code=400,
    )


def normalize_text(text: str) -> str:
    """统一换行(CRLF / CR → LF)+ 合并 3+ 换行为双换行 + strip 首尾。

    剥除 ASCII 控制字符(\\x00-\\x08, \\x0b, \\x0c, \\x0e-\\x1f)以避免污染抽样池;
    保留 \\n / \\t。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# 2026-09-14 保真修补:副文本(盗版站声明 / 脚注 / 译注)不是作者的文字——导入时剥离,已导入的书在
# 结构样例、样例窗口与抽取采样处过滤。真实书上它们曾成为「章首样例」与样例窗口的一部分。
_PARATEXT_FOOTNOTE_RE = re.compile(
    r"^\s*(?:\[\d{1,3}\]|【\d{1,3}】|〔\d{1,3}〕|\(\d{1,3}\)|（\d{1,3}）|[①-⑳]|注\s*[：:]|译注\s*[：:])"
)
_PARATEXT_LINK_RE = re.compile(
    r"(?:https?://|www\.|[A-Za-z0-9-]+\.(?:com|net|org|cn|cc|me|info|top|xyz)\b)",
    re.IGNORECASE,
)
_PARATEXT_SITE_WORDS: tuple[str, ...] = (
    "用户上传",
    "本站",
    "电子书",
    "免费下载",
    "存储服务",
    "版权",
    "TXT",
    "txt",
    "更多精彩",
    "更新最快",
    "手打",
)


# 2026-09-14 保真修补(WP5):场分隔——纯符号行(*** / ——— / ※ / ~~~ …)与原文里 3 个以上连续换行。
# 结构画像据此得到每章场数与场长,样例窗口不跨场。
_SCENE_BREAK_RE = re.compile(
    r"^[\s\*＊※◇◆□■○●〇△▲☆★~～\-—―–_＿=＝#＃\.·•…、:：;；\|｜/／\\<>《》()（）\[\]【】]{1,40}$"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def is_scene_break_paragraph(text: str) -> bool:
    """段落是否为纯符号的场分隔行(不含任何汉字,≤40 字符)。"""
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > 40 or _CJK_RE.search(stripped):
        return False
    return _SCENE_BREAK_RE.match(stripped) is not None


def explicit_scene_breaks(text: str) -> list[int]:
    """原文(已统一换行、**尚未**合并多余空行)里 3 个以上连续换行所在的段落边界。

    返回「其后紧跟该空白的段落」在 :func:`split_paragraphs` 编号下的索引(空行切段口径;
    若该书退化为单换行切段,空行不是段界,返回空列表由调用方处理)。
    """
    if not text:
        return []
    breaks: list[int] = []
    count = 0
    for part in re.split(r"(\n\s*\n)", text):
        if not part:
            continue
        if part.startswith("\n") and part.strip() == "":
            if part.count("\n") >= 3 and count > 0:
                breaks.append(count - 1)
            continue
        if part.strip():
            count += 1
    return sorted(set(breaks))


# 单独命中即判副文本的强标记(「----用户上传之内容开始----」这类分隔线只含一个站点用语)
_PARATEXT_STRONG_WORDS: tuple[str, ...] = (
    "用户上传",
    "免费下载",
    "本站只提供",
    "版权与本站",
    "更新最快",
    "内容简介",
    "作者简介",
)


def is_paratext_paragraph(text: str) -> bool:
    """段落是否为副文本:脚注 / 译注、网址、盗版站声明(强标记单独命中,或 ≥2 个站点用语命中)。"""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if _PARATEXT_FOOTNOTE_RE.match(stripped):
        return True
    if any(word in stripped for word in _PARATEXT_STRONG_WORDS):
        return True
    hits = sum(1 for word in _PARATEXT_SITE_WORDS if word in stripped)
    if hits >= 2:
        return True
    # 网址只在短行或伴随站点用语时算副文本——小说正文里人物也会「键入网址」
    if _PARATEXT_LINK_RE.search(stripped) and (len(stripped) <= 60 or hits >= 1):
        return True
    return False


def compute_text_checksum(normalized_text: str) -> str:
    """对清洗后文本计算 SHA256 hexdigest。

    用作 book_id 前缀(`sr_book_{checksum[:12]}`)与去重键。
    """
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


# 空行切段退化判据:平均段长超过该值视为「单换行分段的网文 TXT」
# (正常中文段落几十到几百字;上千字说明空行其实是章节分隔)
_SINGLE_NEWLINE_FALLBACK_AVG_CHARS = 1500


def split_paragraphs(text: str) -> list[tuple[int, int, str]]:
    """按空行(`\\n\\s*\\n`)切段,返回 (start_offset, end_offset, body) 三元组列表。

    offset 是相对原文(已清洗)的字符位置,end_offset 不含尾字符。
    body 已 strip 首尾空白,但不剥内部换行。

    退化兜底:大量网络下载 TXT 用**单换行**分段(无空行),空行切分会把
    整章并成一「段」,污染段落级统计与分类。当空行切分的平均段长超过
    ``_SINGLE_NEWLINE_FALLBACK_AVG_CHARS`` 时,自动改按单换行切分。
    """
    paragraphs = _split_on(text, r"\n\s*\n")
    if paragraphs:
        avg_len = sum(len(body) for _s, _e, body in paragraphs) / len(paragraphs)
        if avg_len > _SINGLE_NEWLINE_FALLBACK_AVG_CHARS:
            fallback = _split_on(text, r"\n+")
            if len(fallback) > len(paragraphs):
                return fallback
    return paragraphs


def _split_on(text: str, separator_pattern: str) -> list[tuple[int, int, str]]:
    paragraphs: list[tuple[int, int, str]] = []
    offset = 0
    for part in re.split(separator_pattern, text):
        stripped = part.strip()
        if not stripped:
            offset += len(part) + 2  # 跨过空行分隔(近似)
            continue
        start = text.find(stripped, offset)
        if start < 0:
            start = offset
        end = start + len(stripped)
        paragraphs.append((start, end, stripped))
        offset = end
    return paragraphs


# 句末标点:中文全角句末 + ASCII `!?` + 省略号。ASCII `.` 单独处理(见下)。
_SENTENCE_TERMINATORS = "。！？!?…"
# 紧随句末标点的闭引号 / 右括号归并到前一句,而不是成为下一句的开头
# (`“走吧。”` 此前会切出一个只含 `”` 的幽灵句,拉低 avg_sentence_length)。
# ASCII `"` `'` 出现在句末标点之后时几乎总是闭引号,同样归并。
_SENTENCE_CLOSERS = "”’」』）)]】〕〉》\"'"
_SENTENCE_END_RE = re.compile(
    r"(?:[" + re.escape(_SENTENCE_TERMINATORS) + r"]+"
    # ASCII 句点只在(可选闭引号后)后随空白 / 行尾时才算句末:`3.5` / `www.example.com` 不切
    r"|\.+(?=[" + re.escape(_SENTENCE_CLOSERS) + r"]*(?:\s|$)))"
    r"(?P<closers>[" + re.escape(_SENTENCE_CLOSERS) + r"]*)"
)
_SENTENCE_WORD_RE = re.compile(r"[\w\u3400-\u9fff]")


def split_sentences(text: str) -> list[str]:
    """中文 + 英文分句:按 `。！？!?…` 切分,ASCII `.` 只在后随空白 / 行尾时切。

    - 句末标点本身不进入结果(与历史契约一致,句长统计不计终止符);
    - 紧随句末标点的闭引号 / 右括号(`”’」』）)]` 等)归并到前一句;
    - 不产生纯标点片段(无任何文字字符的片段被丢弃);
    - 返回 list[str],每项已 strip,空项去除。

    引号内不切分由上游 `extract_dialogue_spans` 单独处理。
    """
    text = str(text or "")
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        _append_sentence(sentences, text[start:match.start()] + match.group("closers"))
        start = match.end()
    _append_sentence(sentences, text[start:])
    return sentences


def _append_sentence(sentences: list[str], piece: str) -> None:
    piece = piece.strip()
    if not piece or _SENTENCE_WORD_RE.search(piece) is None:
        return
    sentences.append(piece)


def extract_dialogue_spans(text: str) -> list[str]:
    """提取引号内对话内容:英文双引号 / 中文弯引号 / 日式直引号。

    返回每段引号内的连续文本(已 compact_ws),用于 dialogue_ratio 等指标。
    """
    spans: list[str] = []
    spans.extend(re.findall(r'"([^"]+)"', text, flags=re.DOTALL))
    spans.extend(re.findall(r"“([^”]+)”", text, flags=re.DOTALL))
    spans.extend(re.findall(r"「([^」]+)」", text, flags=re.DOTALL))
    return [compact_ws(span) for span in spans if span.strip()]


def compact_ws(text: str) -> str:
    """压缩所有连续空白(空格 / 制表 / 换行)为单个空格 + strip。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()
