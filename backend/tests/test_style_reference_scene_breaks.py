"""2026-09-14 风格保真修补 WP5:场级结构——场分隔识别、导入期记录、结构画像的场统计、
样例窗口不跨场、分章提议按参考章长推每章场数。"""

from __future__ import annotations

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.exemplar_index import book_windows
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.structure import compute_structure_card, render_structure_card
from novel_system.services.style_reference.text_utils import (
    explicit_scene_breaks,
    is_scene_break_paragraph,
)


@pytest.fixture(autouse=True)
def _reset_yaml_cache():
    clear_config_cache()
    yield
    clear_config_cache()


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------


def test_scene_break_detectors() -> None:
    for text in ("***", "＊＊＊", "———", "——", "※ ※ ※", "~~~~", "- - -", "=====", "◇◇◇", "· · ·"):
        assert is_scene_break_paragraph(text), text
    # 2026-09-23 v3(I10):单独一行的省略号是停顿 / 沉默,不是场界
    for text in ("第一章", "……他走了", "", "*注*", "一", "1", "***太长" + "*" * 40, "……", "......", "。。。", "⋯⋯"):
        assert not is_scene_break_paragraph(text), text
    text = "段一\n\n段二\n\n\n\n段三\n\n段四\n\n\n段五"
    assert explicit_scene_breaks(text) == [1, 3]
    assert explicit_scene_breaks("段一\n\n段二") == []
    assert explicit_scene_breaks("") == []


def test_ingest_records_scene_breaks(session) -> None:
    from novel_system.services.style_reference.ingest import IngestService

    prose = "他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯。"
    text = "\n\n".join(
        [
            "第一章 灯下",
            prose + "一",
            prose + "二",
            "***",
            prose + "三",
            prose + "四",
        ]
    ) + "\n\n\n\n" + prose + "五\n\n" + prose + "六"
    result = IngestService(session, llm_enabled=False).ingest_upload(
        raw_bytes=text.encode("utf-8"),
        file_name="breaks.txt",
        title="场界",
        author_label="作者",
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )
    stats = result.book.stats_json
    # 段 0 章题、1–2 正文、3 = ***、4–5 正文、(三个换行) 6、7
    assert stats["scene_breaks"] == [3, 5]
    assert stats["paratext_dropped"] == 0


def _ingest_text(session, text: str, name: str):
    from novel_system.services.style_reference.ingest import IngestService

    return IngestService(session, llm_enabled=False).ingest_upload(
        raw_bytes=text.encode("utf-8"),
        file_name=f"{name}.txt",
        title=name,
        author_label="作者",
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )


def test_single_newline_books_count_one_blank_line_as_a_scene_break(session) -> None:
    """2026-09-23 v3(I10):网文 TXT 按单换行切段时,一个空行就是场界,编号与段落表一致
    (旧实现按空行口径数段,编号错位,还要求 3 个换行)。"""
    line = "他沿着堤岸往北走,风从河面上刮过来,带着一股潮湿的铁锈味,远处的灯一盏一盏灭了下去。" * 2
    first = "\n".join(f"{i}:{line}" for i in range(20))
    second = "\n".join(f"{i + 20}:{line}" for i in range(20))
    result = _ingest_text(session, first + "\n\n" + second, "single_newline")
    assert result.paragraphs_count == 40
    assert result.book.stats_json["scene_breaks"] == [19]


def test_blank_line_breaks_survive_paratext_removal_and_ellipsis_lines_are_not_breaks(session) -> None:
    prose = "她把茶杯推到桌子另一头,没有说话,窗外的雨又密了一层,檐下的水声连成了线。"
    text = (
        f"{prose}一\n\n{prose}二\n\n[1] 这是一条脚注,不是作者的文字。\n\n\n\n"
        f"{prose}三\n\n……\n\n{prose}四\n\n\n\n{prose}五"
    )
    result = _ingest_text(session, text, "paratext_breaks")
    stats = result.book.stats_json
    assert stats["paratext_dropped"] == 1
    # 剥掉脚注后:0 一、1 二、2 三、3 ……、4 四、5 五;脚注之后的空行场界挪到「二」之后,「……」不是场界
    assert result.paragraphs_count == 6
    assert stats["scene_breaks"] == [1, 4]


def test_explicit_scene_breaks_follow_the_basis_of_the_actual_split() -> None:
    from novel_system.services.style_reference.text_utils import (
        explicit_scene_breaks as breaks,
        remap_scene_breaks,
    )

    text = "甲\n乙\n\n丙\n丁"
    assert breaks(text, ["甲", "乙", "丙", "丁"]) == [1]  # 单换行口径:一个空行即场界
    assert breaks(text, ["甲\n乙", "丙\n丁"]) == []  # 空行口径:一个空行只是段界
    assert breaks(text, ["对不上"]) == []
    # 删段 / 重编号后的搬运:落在被删段之后的挪到前一个保留段,挪到最后一段之后的不记
    assert remap_scene_breaks([1, 3, 5], {0: 0, 1: 1, 3: 2, 4: 3, 5: 4}) == [1, 2]
    assert remap_scene_breaks([2], {0: 0, 1: 1, 3: 2}) == [1]
    assert remap_scene_breaks([7], {0: 0, 1: 1}) == []


# ---------------------------------------------------------------------------
# 结构画像
# ---------------------------------------------------------------------------


def _rows_with_breaks(*, breaks: bool) -> list[dict]:
    rows: list[dict] = []

    def add(text: str, ptype: str = "narration") -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    prose = "老周把账本合上，坐了很久，灯芯烧到一半，屋里的影子便大了一圈。"
    for chapter in range(1, 4):
        add(f"第{chapter}章 灯下", "transition")
        for i in range(4):
            add(f"第{chapter}章第{i}段：" + prose)
        if breaks and chapter <= 2:
            add("***")
        for i in range(4, 8):
            add(f"第{chapter}章第{i}段：" + prose)
    return rows


def test_structure_card_counts_scenes_from_explicit_breaks() -> None:
    card = compute_structure_card(_rows_with_breaks(breaks=True))
    assert card["chapter_count"] == 3
    assert card["scene_break_style"] == "explicit"
    assert card["scene_break_chapters"] == 2
    assert card["scenes_per_chapter"]["median"] == 2
    assert card["scene_chars"]["median"] > 0
    assert [entry["scene_count"] for entry in card["chapters"]] == [2, 2, None]
    # 符号分隔行不入正文段数
    assert all(entry["paragraph_count"] == 8 for entry in card["chapters"])
    rendered = render_structure_card({"structure_card": card})
    assert "- 场：有显式场分隔（2 章），每章约 2 场" in rendered
    assert "每章约 2 场、场长约" in rendered

    plain = compute_structure_card(_rows_with_breaks(breaks=False))
    assert plain["scene_break_style"] == "none" and plain["scene_break_chapters"] == 0
    assert "- 场：无显式场分隔" in render_structure_card({"structure_card": plain})


def test_structure_card_uses_ingest_recorded_blank_line_breaks() -> None:
    rows = _rows_with_breaks(breaks=False)
    # 导入期记录:第 1 章第 3 段(索引 4)与第 2 章第 3 段(索引 13)之后有空行型场界
    card = compute_structure_card(rows, scene_breaks=[4, 13])
    assert card["scene_break_style"] == "explicit"
    assert [entry["scene_count"] for entry in card["chapters"]] == [2, 2, None]


def test_exemplar_windows_never_cross_a_scene_break() -> None:
    rows: list[dict] = []

    def add(text: str, ptype: str = "narration") -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    prose = "他把湿伞靠在墙角，没有立刻进屋，只听院门外那阵水声慢慢过去，才抬手去拨灯芯，屋子里暗下去一半。"
    add("第一章 灯下", "transition")
    for i in range(40):
        add(f"第{i}段：" + prose * 3)  # 约 150 字
        if i == 9:
            add("***")
    cut, _count, _chapters = book_windows(rows, window_paragraphs=60, window_max_chars=4000, min_window_chars=600)
    starts = [(win[0]["index"], win[-1]["index"]) for _chapter, _position, win in cut]
    # 第一窗在 *** 前封窗(索引 1–10),第二窗从 *** 之后开始(索引 12 起)
    assert starts[0] == (1, 10)
    assert starts[1][0] == 12
    assert all(not (start <= 11 <= end) for start, end in starts)
    # 空行型场界同样封窗
    cut2, _count2, _chapters2 = book_windows(
        [row for row in rows if row["text"] != "***"], scene_breaks=[20], window_max_chars=4000, min_window_chars=600
    )
    assert any(win[-1]["index"] == 20 for _chapter, _position, win in cut2)


# ---------------------------------------------------------------------------
# 分章提议按参考章长推每章场数
# ---------------------------------------------------------------------------


def test_chaptering_uses_the_reference_chapter_scale_when_the_author_set_nothing(session) -> None:
    from tests.test_snowflake_chapters_after_scenes import PROJECT_ID, _chapters, _seed
    from tests.style_reference_inject_helpers import bind_profile as _bind, seed_full as _seed_full

    from novel_system.services.snowflake_chaptering import SnowflakeChapteringService

    service = _seed(session, count=12)
    session.commit()
    _book_id, profile_id = _seed_full("chapscale")
    with SessionLocal() as other:
        repo = StyleReferenceRepository(other)
        profile = repo.get_profile(profile_id)
        profile.profile_json = {
            **profile.profile_json,
            "structure_card": {
                "version": "structure_card_v1",
                "chapter_count": 40,
                "chapter_chars": {"median": 3000, "p10": 2000, "p90": 4500},
                "scene_break_style": "explicit",
                "scene_chars": {"median": 1500, "p10": 900, "p90": 2400},
            },
        }
        _bind(
            repo,
            binding_id="bind_chapscale",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=PROJECT_ID,
            strategy="mixed",
            config_json={"intensity": 100},
        )
        other.commit()
    chaptering = SnowflakeChapteringService(session)
    result = chaptering.propose_from_scenes(PROJECT_ID, {})
    # 3000 / 1500 = 每章 2 场 → 12 场(灾一 / 灾二 / 灾三 各自收章、按幕比例分配)分 6 章;默认每章 3 场只分 4 章
    assert result["created_chapter_count"] == 6
    assert len(_chapters(session)) == 6
    from novel_system.db.models import OperationLog
    from sqlalchemy import select as _select

    log = session.execute(
        _select(OperationLog).where(OperationLog.event_type == "snowflake_chapter_plan_proposed").order_by(OperationLog.operation_id.desc())
    ).scalars().first()
    assert log.payload_json["reference_hint"]["scenes_per_chapter"] == 2
    assert log.payload_json["reference_hint"]["chapter_chars_median"] == 3000
    # 作者显式给了每章场数 → 参考不介入
    result = chaptering.propose_from_scenes(PROJECT_ID, {"scenes_per_chapter": 3, "replace": True})
    assert result["created_chapter_count"] == 4
    del service
