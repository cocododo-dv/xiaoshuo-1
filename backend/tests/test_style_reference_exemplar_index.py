"""全书样例窗口的切窗规则(``exemplar_index.book_windows``;2026-09-23 风格参考 v3 起只剩切窗)。

原来的 dict 索引(``profile_json.exemplar_windows``)与辨识度打分、按段型选窗、证据引文兜底都已删除——持久化的窗口
索引在 ``windows.py``(测试 ``test_style_reference_windows.py``),按本场挑样例在 ``inject/selection.py``(测试
``test_style_reference_inject_v3.py``)。这里只钉住切窗本身:鲁迅黄金语料按篇切、每窗有界不重叠、位置标签;
副文本与太短的「章」不入窗。
"""

from __future__ import annotations

from pathlib import Path

from novel_system.services.style_reference.exemplar_index import book_windows
from novel_system.services.style_reference.segmentation.heuristic import classify_heuristic_sequence
from novel_system.services.style_reference.text_utils import normalize_text, split_paragraphs

_CORPUS = Path(__file__).resolve().parent / "golden" / "style_reference" / "corpus" / "luxun_short_stories.txt"


def test_luxun_corpus_splits_stories_into_bounded_windows() -> None:
    text = normalize_text(_CORPUS.read_text(encoding="utf-8"))
    spans = split_paragraphs(text)
    classified = classify_heuristic_sequence([body for _s, _e, body in spans])
    rows = [
        {"paragraph_index": i, "text": body, "paragraph_type": ptype}
        for i, ((_s, _e, body), (ptype, _conf)) in enumerate(zip(spans, classified))
    ]
    cut, _paragraph_count, chapter_count = book_windows(rows, window_paragraphs=60, window_max_chars=4000, min_window_chars=600)
    assert chapter_count == 11  # 十一篇以《题名》分章
    assert len(cut) >= 11
    previous_end = -1
    for _chapter, position, win in cut:
        chars = sum(row["chars"] for row in win)
        assert 600 <= chars <= 4000 * 1.25
        assert 1 <= len(win) <= 60 * 1.25
        assert win[0]["index"] > previous_end and win[-1]["index"] >= win[0]["index"]
        previous_end = win[-1]["index"]
        assert position in {"opening", "closing", "middle", "whole"}
    assert {position for _chapter, position, _win in cut} >= {"opening", "closing"}


def test_paratext_and_tiny_chapters_never_enter_a_window() -> None:
    rows: list[dict] = []

    def add(text: str, ptype: str = "narration") -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    add("第一章 卷首", "transition")
    for i in range(3):
        add(f"卷首语第{i}句，很短。")
    add("第二章 正文 The Real Thing", "transition")
    for i in range(12):
        if i == 6:
            add("[1] 这是一条脚注，不是作者的文字。")
        add(f"第二章第{i}段：" + "他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯。" * 2)
    cut, _count, chapter_count = book_windows(rows, min_window_chars=600)
    assert chapter_count == 2
    assert len(cut) == 1  # 卷首太短不入窗;脚注不计段
    chapter, position, win = cut[0]
    assert chapter == 2 and position == "whole" and len(win) == 12
    assert win[0]["index"] == 5 and win[-1]["index"] == 17
