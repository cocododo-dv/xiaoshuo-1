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
# 2026-09-23(v3 I10):只由省略号 / 句点组成的行(「……」「......」「。。。」)是停顿或沉默,不是场界。
_ELLIPSIS_ONLY_RE = re.compile(r"^[\s…⋯\.。]+$")


def is_scene_break_paragraph(text: str) -> bool:
    """段落是否为纯符号的场分隔行(不含任何汉字,≤40 字符;单独一行的省略号不算)。"""
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > 40 or _CJK_RE.search(stripped):
        return False
    if _ELLIPSIS_ONLY_RE.match(stripped):
        return False
    return _SCENE_BREAK_RE.match(stripped) is not None


# 空行型场界的切段口径(与 split_paragraphs 的两种切法一一对应)与各自的阈值:
# 空行切段时段与段之间本来就隔一个空行,要 3 个以上换行(两个以上空行)才是场界;
# 单换行切段(网文 TXT)时段与段之间只有一个换行,出现一个空行(2 个换行)就是场界。
_SCENE_BREAK_BASES: tuple[tuple[str, str, int], ...] = (
    ("blank_line", r"\n\s*\n", 3),
    ("single_newline", r"\n+", 2),
)


def gap_preserving_text(text: str) -> str:
    """与 :func:`normalize_text` 同一清洗(统一换行、剥控制字符、去首尾空白),但**不合并**多余空行。

    空行型场界要在这份文本上数:它切出的段与 ``split_paragraphs(normalize_text(text))`` 逐段相同,
    只是段与段之间的换行数还在。
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def _bodies_and_gaps(text: str, separator: str) -> tuple[list[str], list[int]]:
    """按 ``separator`` 切出的非空段(strip 后)与每段之前的换行数(首段之前记 0)。"""
    bodies: list[str] = []
    gaps: list[int] = []
    pending = 0
    for position, part in enumerate(re.split(f"({separator})", text)):
        if position % 2 == 1:  # 捕获组 = 分隔符
            pending += part.count("\n")
            continue
        stripped = part.strip()
        if not stripped:
            pending += part.count("\n")
            continue
        gaps.append(pending if bodies else 0)
        bodies.append(stripped)
        pending = 0
    return bodies, gaps


def explicit_scene_breaks(text: str, paragraph_bodies: list[str] | None = None) -> list[int]:
    """原文(已统一换行、**尚未**合并多余空行,见 :func:`gap_preserving_text`)里的空行型场界。

    返回「其后有场界」的段落索引。``paragraph_bodies`` 是 :func:`split_paragraphs` 在同一本书
    (合并空行后)上**实际**切出的段(剥副文本之前):按它判断切段口径——空行切段时 3 个以上换行
    是场界,退化为单换行切段(网文 TXT)时一个空行就是场界,编号与段落表一致;哪种口径都切不出
    这些段时返回 []。不给 ``paragraph_bodies`` 时按空行口径(旧行为)。
    """
    if not text:
        return []
    for _basis, separator, threshold in _SCENE_BREAK_BASES:
        bodies, gaps = _bodies_and_gaps(text, separator)
        if paragraph_bodies is not None and bodies != list(paragraph_bodies):
            continue
        return sorted({index - 1 for index in range(1, len(bodies)) if gaps[index] >= threshold})
    return []


def scene_break_indexes(
    gap_text: str,
    raw_bodies: list[str],
    kept: list[bool],
) -> list[int]:
    """导入期的场界:「其后有场界」的段落索引,按**剥离副文本之后**的段落编号。

    - 空行型场界按 ``split_paragraphs`` 实际用的切段口径数(:func:`explicit_scene_breaks`);
    - 剥副文本时不丢场界:落在被剥段之后的场界挪到它之前最近的保留段上;挪到全书最后一段
      之后(后面已没有场)的不记;
    - 纯符号分隔行(非省略号行)本身记为场界。

    ``raw_bodies`` 是剥离前的全部段,``kept[i]`` 表示第 i 段保留(不是副文本)。
    """
    new_index: list[int | None] = []
    last_kept: list[int | None] = []
    count = 0
    previous: int | None = None
    for keep in kept:
        if keep:
            new_index.append(count)
            previous = count
            count += 1
        else:
            new_index.append(None)
        last_kept.append(previous)
    breaks: set[int] = set()
    for raw in explicit_scene_breaks(gap_text, raw_bodies):
        if 0 <= raw < len(last_kept):
            mapped = last_kept[raw]
            if mapped is not None and mapped < count - 1:
                breaks.add(mapped)
    for raw, body in enumerate(raw_bodies):
        mapped = new_index[raw] if raw < len(new_index) else None
        if mapped is not None and is_scene_break_paragraph(body):
            breaks.add(mapped)
    return sorted(breaks)


def remap_scene_breaks(
    breaks: list[int],
    old_to_new: dict[int, int],
) -> list[int]:
    """段落删除 / 重编号之后搬运已记录的场界(刷新工具用)。

    ``old_to_new`` 是保留段的旧编号 → 新编号。落在被删段之后的场界挪到它之前最近的保留段;
    挪到最后一段之后(后面已没有场)的不记。
    """
    import bisect

    if not breaks or not old_to_new:
        return []
    kept_old = sorted(old_to_new)
    last_new = max(old_to_new.values())
    result: set[int] = set()
    for old in breaks:
        position = bisect.bisect_right(kept_old, int(old)) - 1
        if position < 0:
            continue
        mapped = old_to_new[kept_old[position]]
        if mapped < last_new:
            result.add(mapped)
    return sorted(result)


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
