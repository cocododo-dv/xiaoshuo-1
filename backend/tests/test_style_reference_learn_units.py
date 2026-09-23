"""「学习文风」作业的纯函数零件：选窗、分层抽取的核对与合并、文风卡的组装 / 对账 / 过滤、行状态沿用、
受保护专名的候选统计与核对、窗口标签的分批与严格解析、提示词契约（2026-09-23 风格参考 v3）。"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from novel_system.services.style_reference import learn_card, learn_extract, learn_select, learn_tags
from novel_system.services.style_reference.card import CardLine, DimensionCard, DimensionEntry, line_id_for
from novel_system.services.style_reference.learn_card import (
    CardAssembly,
    DimensionMetaRow,
    FindingRef,
    assemble_card,
    carry_line_states,
    derive_narrative_guidance,
    filter_card,
    has_statistics,
    line_claims,
    reconcile_card,
)
from novel_system.services.style_reference.learn_extract import (
    ExtractionSet,
    SetParagraph,
    merge_layer_parses,
    needs_retry,
    parse_layer_output,
)
from novel_system.services.style_reference.learn_select import WindowMeta, select_extraction_windows
from novel_system.services.style_reference.protected_terms import (
    mask_protected,
    parse_protected_terms,
    proper_noun_candidates,
    protected_terms_version,
)
from novel_system.services.style_reference.tags import MOOD_TAGS, SITUATION_TAGS

# ---------------------------------------------------------------- 选窗


def _windows(count: int = 60, chars: int = 3800) -> list[WindowMeta]:
    out = []
    for i in range(1, count + 1):
        position = "opening" if i % 6 == 1 else ("closing" if i % 6 == 0 else "middle")
        dialogue = (i % 10) / 10
        mix = {"dialogue": dialogue, "narration": 1 - dialogue}
        if i % 13 == 0:
            mix = {"psychology": 0.5, "narration": 0.5}
        if i % 17 == 0:
            mix = {"action": 0.4, "narration": 0.6}
        if i % 19 == 0:
            mix = {"description_env": 0.5, "narration": 0.5}
        out.append(
            WindowMeta(
                window_no=i,
                start_index=i * 100,
                end_index=i * 100 + 50,
                chapter_no=(i - 1) // 3 + 1,
                position=position,
                chars=chars,
                dialogue_share=dialogue,
                typicality=-abs(i - 30) / 30,
                type_mix=mix,
            )
        )
    return out


def test_selection_is_deterministic_spread_and_covers_the_strata() -> None:
    windows = _windows()
    first = select_extraction_windows(windows, seed="book-a")
    again = select_extraction_windows(list(reversed(windows)), seed="book-a")
    other = select_extraction_windows(windows, seed="book-b")
    assert first.window_nos == again.window_nos
    assert first.window_nos != other.window_nos
    assert first.window_nos == sorted(first.window_nos)  # 原书顺序
    assert 10 <= len(first.windows) <= 12
    assert first.chars <= learn_select.MAX_CHARS and first.chars >= learn_select.TARGET_CHARS - 3800
    strata = set(first.strata.values())
    assert {"opening", "closing", "dialogue", "narration"} <= strata
    assert {"psychology", "action", "description"} & strata
    # 一章至多一窗(候选够的时候),全书前后都有
    chapters = [w.chapter_no for w in first.windows]
    assert len(chapters) == len(set(chapters))
    assert min(w.window_no for w in first.windows) <= 20 and max(w.window_no for w in first.windows) >= 40


def test_selection_takes_everything_from_a_small_book_and_never_exceeds_the_cap() -> None:
    small = _windows(count=5, chars=3000)
    selection = select_extraction_windows(small, seed="small")
    assert selection.window_nos == [1, 2, 3, 4, 5]
    assert set(selection.strata.values()) == {"all"}
    huge = _windows(count=40, chars=9000)
    capped = select_extraction_windows(huge, seed="huge")
    assert capped.chars <= learn_select.MAX_CHARS
    assert select_extraction_windows([], seed="none").windows == ()


# ---------------------------------------------------------------- 分层抽取:核对 / 重试 / 合并


def _ext_set() -> ExtractionSet:
    texts = [
        "　　他把灯芯拨小了些，说天亮前一定要走。",
        "雨又密起来了，桂树被打得低了头。",
        "“你听见了吗？”她问，声音压得很低。",
        "风从渡口吹过来，带着一点河水的腥气。",
    ]
    paragraphs = {
        n: SetParagraph(
            number=n,
            paragraph_id=f"p{n}",
            paragraph_index=n,
            paragraph_type="narration",
            window_no=1,
            text=learn_extract.compact_ws(text),
            raw_text=text,
        )
        for n, text in enumerate(texts, start=1)
    }
    return ExtractionSet(windows=[{"window": 1, "text": "…"}], paragraphs=paragraphs)


def _finding(statement: str, quotes: list[tuple[int, str]], **extra) -> dict:
    return {
        "statement": statement,
        "confidence": "high",
        "distinctiveness": 0.7,
        "evidence": [{"p": p, "quote": q} for p, q in quotes],
        **extra,
    }


GOOD = [(1, "把灯芯拨小了些"), (2, "桂树被打得低了头")]


def test_parse_layer_output_verifies_quotes_caps_and_drops_vague_statements() -> None:
    ext = _ext_set()
    structured = {
        "dimensions": [
            {
                "dimension": "language.sentence_structure",
                "model_default": "通用写法句子长短平均",
                "devices": ["长短句错落", "超过八个字的手法名不要", "长短句错落"],
                "distinctiveness": 8,  # 0–10 尺度也认
                "observations": [
                    *[_finding(f"短句收在动作之后第{c}式", GOOD) for c in "一二三四五六"],
                    _finding("文笔优美，画面感强", GOOD),
                ],
                "avoid": [
                    _finding("不写长定语而是拆成短句", GOOD),
                    _finding("不堆形容词而是只给一个动作", GOOD),
                    _finding("第三条避免会被截掉", GOOD),
                ],
            },
            {
                "dimension": "language.vocabulary",
                "model_default": "",
                "devices": [],
                "distinctiveness": 0.4,
                "observations": [
                    _finding("引文伪造的一条", [(1, "书里没有这句话"), (2, "桂树被打得低了头")]),
                    _finding("段号错但引文唯一", [(4, "他把灯芯拨小了些"), (3, "声音压得很低")]),
                    _finding("同一处引两次不算两条证据", [(1, "把灯芯拨小了些"), (1, "把灯芯拨小了些")]),
                ],
                "avoid": [],
            },
            {"dimension": "theme.values", "observations": [_finding("别层的维度", GOOD)]},
        ]
    }
    parse = parse_layer_output(structured, "language", ext)
    assert not parse.malformed
    structure = [f for f in parse.findings if f.dimension == "language.sentence_structure"]
    assert len([f for f in structure if f.kind == "observation"]) == learn_extract.MAX_OBSERVATIONS == 5
    assert len([f for f in structure if f.kind == "forbidden_pattern"]) == learn_extract.MAX_AVOID == 2
    assert not any("文笔优美" in f.statement for f in parse.findings)
    meta = parse.meta["language.sentence_structure"]
    assert meta.devices == ["长短句错落"] and meta.distinctiveness == 0.8
    vocab = {f.statement: f for f in parse.findings if f.dimension == "language.vocabulary"}
    assert set(vocab) == {"段号错但引文唯一"}
    repaired = vocab["段号错但引文唯一"].evidence[0]
    assert repaired.paragraph.paragraph_id == "p1"
    # 坐标是库里原文(带全角缩进)的坐标
    raw = ext.paragraphs[1].raw_text
    assert raw[repaired.span[0] : repaired.span[1]] == repaired.quote == "他把灯芯拨小了些"
    assert not any(f.dimension == "theme.values" for f in parse.findings)
    # 两维没有任何观察通过 → 值得重试
    assert needs_retry(parse)
    assert any("language.rhetoric" in p for p in parse.problems)
    malformed = parse_layer_output({"nope": 1}, "language", ext)
    assert malformed.malformed and needs_retry(malformed)


def test_merge_keeps_the_valid_findings_of_both_attempts_and_the_caps() -> None:
    ext = _ext_set()

    def layer(statements: dict[str, list[str]]) -> dict:
        return {
            "dimensions": [
                {"dimension": dim, "model_default": f"默认{dim[-4:]}", "devices": ["手法甲"], "distinctiveness": 0.5,
                 "observations": [_finding(s, GOOD) for s in items], "avoid": []}
                for dim, items in statements.items()
            ]
        }

    first = parse_layer_output(
        layer({"language.sentence_structure": ["第一次的甲观察", "第一次的乙观察"], "language.vocabulary": ["第一次的丙观察"]}),
        "language",
        ext,
    )
    second = parse_layer_output(
        layer(
            {
                "language.sentence_structure": ["第一次的甲观察", "第二次的新观察一", "第二次的新观察二", "第二次的新观察三", "第二次的新观察四"],
                "language.rhetoric": ["第二次才有的修辞"],
                "language.punctuation": ["第二次才有的标点"],
            }
        ),
        "language",
        ext,
    )
    merged = merge_layer_parses(first, second)
    statements = [f.statement for f in merged.findings if f.dimension == "language.sentence_structure"]
    assert statements[:2] == ["第一次的甲观察", "第一次的乙观察"]
    assert len(statements) == 5 and statements.count("第一次的甲观察") == 1
    assert {f.dimension for f in merged.findings} == {
        "language.sentence_structure",
        "language.vocabulary",
        "language.rhetoric",
        "language.punctuation",
    }
    assert not needs_retry(merged)


def test_shrink_to_fit_drops_whole_windows_filler_first() -> None:
    def para(n: int, window: int) -> SetParagraph:
        return SetParagraph(number=n, paragraph_id=f"p{n}", paragraph_index=n, paragraph_type="narration", window_no=window, text="字" * 10)

    ext = ExtractionSet(
        windows=[{"window": w, "text": "字" * (100 * w)} for w in (1, 2, 3, 4, 5)],
        paragraphs={n: para(n, n) for n in (1, 2, 3, 4, 5)},
        strata={1: "opening", 2: "typical", 3: "dialogue", 4: "typical", 5: "closing"},
    )
    fitted, dropped = learn_extract.shrink_to_fit(ext, lambda candidate: len(candidate.windows) <= 3)
    assert dropped == [4, 2]  # 补位的典型窗先卸,大的先
    assert [w["window"] for w in fitted.windows] == [1, 3, 5] and set(fitted.paragraphs) == {1, 3, 5}
    never, dropped_all = learn_extract.shrink_to_fit(ext, lambda _c: False)
    assert len(never.windows) == learn_extract.MIN_SET_WINDOWS and len(dropped_all) == 2


# ---------------------------------------------------------------- 文风卡:组装 / 对账 / 过滤


def _refs() -> list[FindingRef]:
    return [
        FindingRef("f1", "find_1", "language.rhetoric", "observation", "喻体取自游戏", "high", ("q1", "q2"), ("甲", "乙")),
        FindingRef("f2", "find_2", "language.rhetoric", "observation", "降格比喻", "high", ("q3", "q4"), ("丙", "丁")),
        FindingRef("f3", "find_3", "language.punctuation", "observation", "逗号切碎", "medium", ("q5", "q6", "q7"), ()),
        FindingRef("f4", "find_4", "language.punctuation", "forbidden_pattern", "不用分号", "low", ("q8",), ()),
    ]


def test_assemble_card_maps_refs_drops_bad_lines_and_caps_mandatory() -> None:
    structured = {
        "profile_title": "游戏梗与降格比喻的市井叙事腔调很长很长很长",
        "qualitative_summary": "作者爱用游戏梗。平均句长28字。玩笑底下压着孤独。",
        "temperament": ["越危险越要开玩笑", "平均每千字3次玩笑"],
        "dimensions": [
            {
                "dimension": "language.rhetoric",
                "summary": "比喻取材日常",
                "model_default": "比喻取自然景物",
                "distinctiveness": 0.95,
                "devices": ["游戏梗", "降维比喻"],
                "do": [
                    {"text": "代替自然景物，比喻从游戏里取", "refs": ["f1", "F2"], "mandatory": True, "distinctiveness": 0.9},
                    {"text": "把大事比作「泡面」一样的小事", "refs": ["2"], "mandatory": True, "distinctiveness": 0.8},
                    {"text": "没有依据的一句", "refs": ["f99"], "mandatory": True, "distinctiveness": 0.9},
                    {"text": "第三条有依据的留下", "refs": ["f1"], "mandatory": False, "distinctiveness": 0.1},
                    {"text": "第四条超过上限", "refs": ["f1"], "mandatory": False, "distinctiveness": 0.1},
                ],
                "avoid": [{"text": "不写宛如仙境", "refs": ["f1"], "mandatory": True, "distinctiveness": 0.4}],
            },
            {
                "dimension": "language.punctuation",
                "do": [
                    {"text": "每千字用45个逗号", "refs": ["f3"], "mandatory": True, "distinctiveness": 0.9},
                    {"text": "逗号把动作切成一小截一小截", "refs": ["f3"], "mandatory": True, "distinctiveness": 0.9},
                    {"text": "问句接问句，大约每十句一次", "refs": ["f3"], "mandatory": True, "distinctiveness": 0.85},
                ],
                "avoid": [],
            },
            {"dimension": "scene.unknown", "do": [{"text": "不认识的维", "refs": ["f1"]}]},
        ],
        "planning_guidance": ["开场:先抛一句闲话", "收场落在一个小动作上", "节奏：每场3次反转"],
    }
    assembly = assemble_card(structured, _refs(), {"language.rhetoric": DimensionMetaRow(devices=["心里吐槽"])}, generated_at="t")
    card = assembly.card
    assert card is not None and len(card.dimensions) == 16
    rhetoric = card.entry("language.rhetoric")
    texts = [line.text for line in rhetoric.lines]
    assert texts == ["代替自然景物，比喻从游戏里取", "把大事比作「泡面」一样的小事", "第三条有依据的留下", "不写宛如仙境"]
    first = rhetoric.lines[0]
    assert first.finding_ids == ["find_1", "find_2"] and first.evidence_quote_ids == ["q1", "q2", "q3", "q4"]
    assert first.line_id == line_id_for("language.rhetoric", first.text)
    assert rhetoric.devices == ["游戏梗", "降维比喻", "心里吐槽"]
    assert not rhetoric.lines[3].mandatory  # avoid 行不标必须
    punctuation = [line.text for line in card.entry("language.punctuation").lines]
    assert "每千字用45个逗号" not in punctuation and "问句接问句，大约每十句一次" in punctuation
    mandatory = [line for _d, line in card.all_lines() if line.mandatory]
    assert len(mandatory) == learn_card.MAX_MANDATORY
    assert card.temperament == ["越危险越要开玩笑"]
    assert assembly.qualitative_summary == "作者爱用游戏梗。玩笑底下压着孤独。"
    assert assembly.profile_title == structured["profile_title"][:20]
    assert assembly.planning_guidance == ["开场：先抛一句闲话", "场景：收场落在一个小动作上"]
    assert assembly.dropped == {"ungrounded": 1, "over_limit": 1, "numbers": 1, "unknown_dimension": 1}
    assert assemble_card("not a dict", _refs(), {}, generated_at="t").card is None


def test_line_claims_read_the_nearest_cue() -> None:
    assert line_claims("少用分号，多用逗号切开") == {"punct_semicolon_per_1k": "rare", "punct_comma_per_1k": "frequent"}
    assert line_claims("不常用逗号") == {"punct_comma_per_1k": "rare"}
    assert line_claims("逗号用得密") == {"punct_comma_per_1k": "frequent"}
    assert line_claims("省略号几乎不用") == {"punct_ellipsis_per_1k": "rare"}
    assert line_claims("写对白时用逗号") == {}
    assert line_claims("以短句为主，长句很少") == {"sentence_majority": "short"}
    assert has_statistics("平均28字") and has_statistics("约三成，即30%")
    assert not has_statistics("用「还剩3秒」这样的倒计时") and not has_statistics("大约每十句一次")


def _card_with(lines: list[tuple[str, CardLine]]) -> DimensionCard:
    by_dim: dict[str, list[CardLine]] = {}
    for dim, line in lines:
        by_dim.setdefault(dim, []).append(line)
    return DimensionCard(dimensions=[DimensionEntry(dimension=dim, lines=items) for dim, items in by_dim.items()])


def _line(text: str, *, kind: str = "do", quotes: int = 2, distinct: float = 0.5) -> CardLine:
    return CardLine(
        line_id=line_id_for("language.punctuation", text),
        text=text,
        kind=kind,
        evidence_quote_ids=[f"q{i}" for i in range(quotes)],
        distinctiveness=distinct,
    )


def test_reconcile_drops_the_weaker_of_two_contradicting_lines_and_lines_against_the_measure() -> None:
    dense = _line("逗号用得密，一句切成几截", quotes=4)
    sparse = _line("逗号稀疏，一口气说完", quotes=2, distinct=0.9)
    dash = _line("常用破折号插入说明", quotes=6)
    card = _card_with([("language.punctuation", dense), ("language.punctuation", sparse), ("language.punctuation", dash)])
    # 逗号实测在两道门槛之间:实测不裁决,证据多的留下;破折号作者几乎不用 → 「常用破折号」与实测相反
    features = {"punct_comma_per_1k": 30.0, "punct_dash_per_1k": 0.05}
    reconciled, dropped = reconcile_card(card, voice_features=features)
    texts = [line.text for _d, line in reconciled.all_lines()]
    assert texts == ["逗号用得密，一句切成几截"]
    assert dropped == {"contradiction": 1, "contradicts_measure": 1}
    # 逗号很密的作者:「逗号稀疏」直接被实测否掉
    reconciled, dropped = reconcile_card(_card_with([("language.punctuation", sparse)]), voice_features={"punct_comma_per_1k": 60.0})
    assert reconciled.all_lines() == [] and dropped == {"contradicts_measure": 1}
    # 平均句长 32 字的作者说「短句为主」 → 否掉
    short = _line("以短句为主", quotes=3)
    reconciled, dropped = reconcile_card(_card_with([("language.sentence_structure", short)]), voice_features={"sent_len_mean": 32.0})
    assert dropped == {"contradicts_measure": 1}
    assert reconcile_card(None, voice_features={}) == (None, {})


def test_filter_card_drops_protected_and_overlapping_text_everywhere() -> None:
    keep = _line("对白里夹一句自嘲")
    named = _line("像韩小暖那样说话")
    copied = _line("照抄原书的一整句话十二个字以上")
    card = DimensionCard(
        dimensions=[
            DimensionEntry(
                dimension="scene.dialogue",
                summary="韩小暖式的对白",
                model_default="通用对白规整",
                lines=[keep, named, copied],
                devices=["韩小暖梗", "自嘲"],
            )
        ],
        temperament=["嘴硬心软", "像韩小暖一样嘴硬"],
    )
    assembly = CardAssembly(
        card=card,
        profile_title="韩小暖体",
        qualitative_summary="作者爱自嘲。韩小暖是主角。",
        planning_guidance=["开场：先让韩小暖开口", "收场：落在一个小动作上"],
    )
    filtered, dropped = filter_card(
        assembly,
        protected=["韩小暖"],
        overlaps=lambda text: "十二个字以上" in text,
    )
    entry = filtered.card.entry("scene.dialogue")
    assert [line.text for line in entry.lines] == ["对白里夹一句自嘲"]
    assert entry.summary == "" and entry.model_default == "通用对白规整" and entry.devices == ["自嘲"]
    assert filtered.card.temperament == ["嘴硬心软"]
    assert filtered.planning_guidance == ["收场：落在一个小动作上"]
    assert filtered.qualitative_summary == "作者爱自嘲。" and filtered.profile_title == ""
    # 计的是丢掉的句子(卡片行 / 气质 / 规划行);概述、默认写法、手法名里的专名就地清掉
    assert dropped == {"protected_term": 3, "source_overlap": 1}


def test_carry_line_states_keeps_exclusions_and_restores_missing_pins() -> None:
    old_pin = _line("旧卡上被✓的一句")
    kept = _line("新旧卡都有的一句")
    old = _card_with([("language.punctuation", old_pin), ("language.punctuation", kept)])
    new = _card_with([("language.punctuation", kept), ("language.punctuation", _line("新卡的新句"))])
    states = {old_pin.line_id: "pinned", kept.line_id: "excluded", "cl_gone0000000": "excluded", "cl_bad": "maybe"}
    card, carried_states, carried = carry_line_states(new, old, states)
    texts = [line.text for _d, line in card.all_lines()]
    assert texts == ["新旧卡都有的一句", "新卡的新句", "旧卡上被✓的一句"]
    restored = [line for _d, line in card.all_lines() if line.line_id == old_pin.line_id][0]
    assert restored.source == "pinned_carryover" and carried == 1
    assert carried_states == {old_pin.line_id: "pinned", kept.line_id: "excluded", "cl_gone0000000": "excluded"}
    assert carry_line_states(None, old, states)[0] is None


def test_narrative_guidance_marks_avoid_lines() -> None:
    card = DimensionCard(
        dimensions=[
            DimensionEntry(
                dimension="narrative.pacing",
                distinctiveness=0.9,
                lines=[
                    CardLine(text="危急时用倒计时压缩时间", kind="do", mandatory=True),
                    CardLine(text="拖长静态铺陈", kind="avoid"),
                ],
            ),
            DimensionEntry(dimension="language.rhetoric", lines=[CardLine(text="语言层不进叙事指引")]),
        ]
    )
    assert derive_narrative_guidance(card) == ["危急时用倒计时压缩时间", "避免：拖长静态铺陈"]
    assert derive_narrative_guidance(None) == []


# ---------------------------------------------------------------- 受保护专名


def _book_texts() -> list[str]:
    """人名 / 地名 / 组织名出现在各种各样的上下文里(真实的书就是这样),夹着大量普通叙述。"""
    names = ("韩小暖", "程铁", "苏半夏")
    places = ("铁灰城", "雾港")
    actions = ("站了很久", "看了看表", "叹了口气", "笑出了声", "转过身去", "把伞收起", "没有说话", "点了根烟")
    texts = []
    for i in range(120):
        name = names[i % 3]
        other = names[(i + 1) % 3]
        place = places[i % 2]
        action = actions[i % len(actions)]
        texts.append(f"{name}在{place}{action}，心想这雨下到第{i}天了。")
        texts.append(f"“{other}，你说雾港同盟的人会来吗？”{name}{actions[(i + 3) % len(actions)]}，又问了一遍。")
        texts.append(f"风从码头那边吹过来，{actions[(i + 5) % len(actions)]}的人越来越少，天也暗了。")
    return texts


def test_proper_noun_candidates_find_names_and_prefer_the_longest_unit() -> None:
    candidates = proper_noun_candidates(_book_texts(), limit=40)
    terms = [c.term for c in candidates]
    for name in ("韩小暖", "程铁", "苏半夏", "铁灰城", "雾港同盟"):
        assert name in terms, (name, terms)
    assert "小暖" not in terms and "港同盟" not in terms  # 总是更长的名字的一部分
    assert not any(t[0] in "的了着是在" or t[-1] in "的了着是在" for t in terms)
    han = next(c for c in candidates if c.term == "韩小暖")
    assert han.count >= 60 and "韩小暖" in han.context
    assert proper_noun_candidates(_book_texts(), limit=40) == candidates  # 确定性
    assert proper_noun_candidates([]) == []


def test_candidates_skip_function_words_and_redundant_compounds() -> None:
    """「名字 + 虚词」「觉得自己」、虚词本身、两个更常见的名字拼成的串不占候选名额;以虚词开头的称号(去掉只剩一个字)留着。"""
    rng = random.Random("candidates")
    verbs = ("停下", "回头", "抬手", "笑了", "转身", "开口", "愣住", "皱眉")
    tails = ("站在门口", "坐在车里", "走进屋子", "看着远处", "等在桥头", "蹲在墙角")
    texts = []
    for i in range(90):
        texts.append(f"韩小暖忽然{rng.choice(verbs)}，第{i}次回头看。")
        texts.append(f"程铁和苏半夏{rng.choice(tails)}，谁也没动。")
        texts.append(f"大家长{rng.choice(verbs)}之前，谁都觉得自己{rng.choice(verbs)}得太早。")
        texts.append(f"苏半夏说程铁越来越像他爹，韩小暖{rng.choice(verbs)}。")
        texts.append(f"那天程铁一个人去了码头，苏半夏在屋里{rng.choice(verbs)}。")
    terms = [c.term for c in proper_noun_candidates(texts, limit=60)]
    for name in ("韩小暖", "程铁", "苏半夏", "大家长"):
        assert name in terms, (name, terms)
    for noise in ("韩小暖忽然", "程铁和苏半夏", "觉得自己", "越来越", "自己", "忽然"):
        assert noise not in terms, (noise, terms)


def test_parse_protected_terms_keeps_only_terms_that_occur_verbatim() -> None:
    corpus = "\n".join(_book_texts())
    structured = {
        "terms": [
            {"term": "韩小暖", "kind": "person"},
            {"term": "「铁灰城」", "kind": "place"},
            {"term": "雾港同盟", "kind": "faction"},
            {"term": "不存在的名字", "kind": "person"},
            {"term": "韩小暖", "kind": "person"},
            {"term": "韩", "kind": "person"},
            {"term": "越来越", "kind": "term"},  # 虚词不可能是专名(原书里有也不要)
            "苏半夏",
        ]
    }
    terms = parse_protected_terms(structured, corpus)
    assert [(t.term, t.kind) for t in terms] == [
        ("韩小暖", "person"),
        ("铁灰城", "place"),
        ("雾港同盟", "term"),
        ("苏半夏", "term"),
    ]
    assert parse_protected_terms({"nope": 1}, corpus) == []
    assert protected_terms_version(terms) == protected_terms_version(list(reversed(terms)))
    assert mask_protected("韩小暖和苏半夏在铁灰城", [t.as_dict() for t in terms]) == "某人和某设定在某地"


# ---------------------------------------------------------------- 窗口标签


def test_tag_batches_respect_window_and_char_limits() -> None:
    windows = [(n, 3900) for n in range(1, 21)]
    batches = learn_tags.plan_tag_batches(windows)
    assert [n for batch in batches for n in batch] == list(range(1, 21))
    assert all(len(batch) <= learn_tags.TAG_BATCH_WINDOWS for batch in batches)
    assert all(len(batch) * 3000 <= learn_tags.TAG_BATCH_MAX_CHARS + 3000 for batch in batches)
    clipped = learn_tags.clip_window_text("字" * 4000)
    assert len(clipped) < 3100 and "（中略）" in clipped
    assert learn_tags.clip_window_text("短") == "短"


def test_tag_output_is_parsed_strictly() -> None:
    good = {
        "windows": [
            {"window": 3, "situations": ["日常闲谈", "编的场面"], "moods": ["诙谐"], "devices": ["降维比喻", "不在表里"], "gist": "韩小暖在码头等人"},
            {"window": 4, "situations": ["危机应对"], "moods": ["紧张", "恐惧", "平静"], "devices": [], "gist": "一场追逐"},
        ]
    }
    tags = learn_tags.parse_tag_output(good, [3, 4], devices=["降维比喻"], protected_terms=[{"term": "韩小暖", "kind": "person"}])
    assert tags[3] == {"situations": ["日常闲谈"], "moods": ["诙谐"], "devices": ["降维比喻"], "gist": "某人在码头等人"}
    assert tags[4]["moods"] == ["紧张", "恐惧"]
    for bad in (
        {"windows": [good["windows"][0]]},  # 缺一窗
        {"windows": [*good["windows"], {"window": 9}]},  # 多出
        {"windows": [good["windows"][0], good["windows"][0], good["windows"][1]]},  # 重复
        {"nope": []},
    ):
        with pytest.raises(learn_tags.TagBatchMismatch):
            learn_tags.parse_tag_output(bad, [3, 4], devices=[])


# ---------------------------------------------------------------- 提示词 / 节点契约


def test_learn_prompts_and_node_routes_are_aligned() -> None:
    from novel_system.services.llm_client import load_model_routing_config
    from novel_system.services.llm_node_registry import get_llm_node_spec
    from novel_system.services.prompt_builder import load_prompt_templates
    from novel_system.services.style_reference.learn_llm import (
        LEARN_NODE_IDS,
        TEMPLATE_CONTRACT,
        template_meets_contract,
    )

    root = Path(__file__).resolve().parents[2]
    templates = load_prompt_templates(root / "config" / "prompts.yaml")
    routing = load_model_routing_config(root / "config" / "models.yaml")
    assert set(TEMPLATE_CONTRACT) == set(LEARN_NODE_IDS)
    for node_id in LEARN_NODE_IDS:
        spec = get_llm_node_spec(node_id)
        route = routing.task_routing[node_id]
        assert spec is not None and spec.template_name == node_id and spec.status == "active"
        assert spec.temperature == route.temperature == 0.0
        assert spec.max_output_tokens == route.max_output_tokens
        assert templates[node_id].input_token_budget > 0
        # 作业开工前查的模板契约:仓库里的模板都满足(旧抽取 schema 不满足,见作业测试)
        assert template_meets_contract(node_id, templates[node_id])
    for layer in ("language", "narrative", "scene", "theme"):
        template = templates[f"style_ref_extract_{layer}"]
        assert template.version == "2026-09-23.v6"
        assert "model_default" in template.system_prompt and "devices" in template.system_prompt
        assert "全面模仿" in template.system_prompt and "不得引用、复述" not in template.system_prompt
    synth = templates["style_ref_synthesize_profile"]
    assert synth.version == "2026-09-23.v9"
    assert "对账" in synth.system_prompt and "voice_habits" in synth.system_prompt
    assert "不得逐字抄写这些行" not in synth.system_prompt  # 不再要求卡片重述声音(台账 E1)
    tag_schema = templates["style_ref_tag_windows"].structured_schema
    item = tag_schema["properties"]["windows"]["items"]["properties"]
    assert item["situations"]["items"]["enum"] == list(SITUATION_TAGS)
    assert item["moods"]["items"]["enum"] == list(MOOD_TAGS)
    assert "style_ref_supplement_evidence" not in templates
    assert get_llm_node_spec("style_ref_supplement_evidence") is None
    assert get_llm_node_spec("style_ref_tag_windows").reasoning_level == "off"
