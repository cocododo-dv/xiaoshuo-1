"""「学习文风 · 通读全书」与「在上一版上修订」（2026-10-19）。

钉住：
- 切批：相连的整章装进一批，超长的章按窗口边界切成几部分；章与章之间的零碎段归前一章；一批的样本按段落区间取、
  跳过章题；
- 通读：每批 × 每层各一次调用，载荷带 scope；全书从第一个窗口到最后一个窗口的正文段都被读到；某一维在一批里空着
  不重问；崩溃后续跑只补没读完的份；
- 归并：组里的编号必须是同一维同一类的发现、一条只进一组；按「几批里见到」排、每维留观察 ≤5 / 避免 ≤2；文风卡读
  归并后的发现（带 seen_in / of），卡句连到归并后的发现与成员的原文引文；
- 沿用：同一身份的第二次通读不再读、不再归并，只重写文风卡；``reread`` 从头读；估算预告能沿用；
- 修订：两种方式都把上一版文风卡交给合成，``keep`` 沿用原句与原 id（作者的 ✓ 跟着走），指向 ✗ 掉的句子 → 丢。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    StyleReferenceWindow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import learn_job, learn_run
from novel_system.services.style_reference.book_text import non_body_kind
from novel_system.services.style_reference.card import (
    CardLine,
    DimensionCard,
    DimensionEntry,
    card_from_profile_json,
    line_id_for,
)
from novel_system.services.style_reference.card_states import set_card_line_state
from novel_system.services.style_reference.jobs import JOB_KIND_LEARN, StyleJobService, register_job_handler, run_job_inline
from novel_system.services.style_reference.learn_card import FindingRef, PreviousCard, assemble_card
from novel_system.services.style_reference.learn_consolidate import (
    STATUS_CONSOLIDATED,
    RawFinding,
    parse_consolidation,
    rank_groups,
    support_confidence,
)
from novel_system.services.style_reference.learn_extract import build_range_set
from novel_system.services.style_reference.learn_select import (
    ChapterSpan,
    batch_chapter_label,
    chapter_spans,
    plan_full_batches,
)
from novel_system.services.style_reference.text_utils import compact_ws
from tests.learn_fakes import NODE_SYNTH, FakeLearnLLM, SimulatedCrash, default_extract, paragraphs_of
from tests.style_reference_factories import make_book
from tests.support.style_reference import load_job

pytestmark = pytest.mark.usefixtures("style_workers")

PREFIX = "/api/v2/style-reference"
BOOK = "full_book"
RIGHTS = {"rights_declaration": {"declared": True, "send_rights": True, "analysis_rights": True}}
NAMES = ("林昭", "周远", "沈青")
EXTRACT = "style_ref_extract_"


def full_rows(chapters: int = 6, per_chapter: int = 40) -> list[dict]:
    rng = random.Random("full-learn")
    rows: list[dict] = []
    serial = 0

    def add(text: str, ptype: str) -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    for chapter in range(1, chapters + 1):
        add(f"第{chapter}章 旧信", "transition")
        for _ in range(per_chapter):
            serial += 1
            name = rng.choice(NAMES)
            if rng.random() < 0.5:
                add(f"“{name}，雨城的第{serial}封信到了没有？”他把案卷往桌上一推，笑得有点勉强。", "dialogue")
            else:
                add(f"{name}在雨城的屋檐下数到第{serial}滴雨，心里骂了一句这鬼天气，转身进了门。", "narration")
    return rows


def seed(session) -> str:
    make_book(session, BOOK, title="雨城旧信", paragraphs=full_rows(), cloud_policy="allow_full_cloud", stats=dict(RIGHTS))
    session.commit()
    return BOOK


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(learn_run, "CALL_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(learn_run, "WAIT_POLL_SECONDS", 0.05)
    # 合成书每章约 1.6k 字:上限 3.5k 字 → 两章一批
    monkeypatch.setattr(learn_run, "FULL_BATCH_MAX_CHARS", 3_500)
    monkeypatch.setattr(learn_job, "FULL_BATCH_MAX_CHARS", 3_500)
    register_job_handler(JOB_KIND_LEARN, learn_run.run_learn_job)


def _use(monkeypatch, fake: FakeLearnLLM) -> FakeLearnLLM:
    monkeypatch.setattr(learn_run, "resolve_learn_client", lambda: (fake, True))
    return fake


def _start(**kwargs) -> str:
    with SessionLocal() as db:
        job = learn_job.start_learn_job(db, BOOK, **kwargs)
        db.commit()
        return job.job_id


def _learn(monkeypatch, fake: FakeLearnLLM | None = None, **kwargs) -> tuple[FakeLearnLLM, StyleReferenceJob]:
    fake = _use(monkeypatch, fake or FakeLearnLLM())
    job_id = _start(**kwargs)
    run_job_inline(job_id)
    return fake, load_job(job_id)


def _is_card(payload: dict) -> bool:
    """合成节点上跑的五种调用里，写文风卡的那一种（归并 findings / 场面 situation / 伏笔配对 plants / 伏笔手法 pairs 之外）。"""
    return not any(key in payload for key in ("findings", "situation", "plants", "pairs"))


def _is_merge(payload: dict) -> bool:
    return "findings" in payload and "situation" not in payload


def _profile(profile_id: str) -> StyleReferenceProfile:
    with SessionLocal() as db:
        profile = db.get(StyleReferenceProfile, profile_id)
        db.expunge(profile)
        return profile


# ---------------------------------------------------------------- 切批 / 样本（纯函数）


def test_plan_packs_whole_chapters_and_splits_only_a_chapter_longer_than_a_batch() -> None:
    sizes = {1: 1000, 2: 1500, 3: 900, 4: 5000}
    spans = [
        ChapterSpan(chapter_no=1, start=0, end=9, windows=((1, 9),)),
        ChapterSpan(chapter_no=2, start=10, end=19, windows=((11, 19),)),
        ChapterSpan(chapter_no=3, start=20, end=29, windows=((21, 29),)),
        # 第 4 章三个窗口,每窗约 1/3
        ChapterSpan(chapter_no=4, start=30, end=59, windows=((31, 39), (40, 49), (50, 59))),
    ]

    def chars_of(start: int, end: int) -> int:
        total = 0
        for span in spans:
            lo, hi = max(start, span.start), min(end, span.end)
            if lo <= hi:
                total += sizes[span.chapter_no] * (hi - lo + 1) // (span.end - span.start + 1)
        return total

    plan = plan_full_batches(spans, chars_of, max_chars=2600)
    # 第 3 章与第 4 章的第一部分装得进一批;第 4 章按窗口边界切成三部分
    assert [[(s["chapter"], s["part"], s["parts"]) for s in b["segments"]] for b in plan] == [
        [(1, 1, 1), (2, 1, 1)],
        [(3, 1, 1), (4, 1, 3)],
        [(4, 2, 3)],
        [(4, 3, 3)],
    ]
    # 第 4 章的各部分首尾相接、覆盖整章
    parts = [(s["start"], s["end"]) for b in plan for s in b["segments"] if s["chapter"] == 4]
    assert parts == [(30, 39), (40, 49), (50, 59)]
    assert [b["no"] for b in plan] == [0, 1, 2, 3]
    assert all(b["chars"] <= 2600 for b in plan)
    assert batch_chapter_label(plan[0]) == "第 1–2 章" and batch_chapter_label(plan[1]) == "第 3–4 章"
    assert batch_chapter_label(plan[2]) == "第 4 章（第 2/3 部分）"


def test_chapter_spans_hand_the_gap_between_chapters_to_the_previous_chapter() -> None:
    rows = [
        {"window_no": 0, "start_index": 1, "end_index": 8, "chapter_no": 1, "chars": 800},
        {"window_no": 1, "start_index": 9, "end_index": 15, "chapter_no": 1, "chars": 700},
        {"window_no": 2, "start_index": 20, "end_index": 30, "chapter_no": 2, "chars": 900},
    ]
    spans = chapter_spans(rows)
    assert [(s.chapter_no, s.start, s.end) for s in spans] == [(1, 1, 19), (2, 20, 30)]
    assert spans[0].windows == ((1, 8), (9, 15))


def test_a_range_set_reads_every_body_paragraph_and_skips_chapter_titles() -> None:
    rows = [(i, "narration", text, f"p{i}") for i, text in enumerate(["第1章 旧信", "林昭推开门。", "", "周远没有回头。", "第2章 案卷", "沈青笑了。"])]
    ext_set = build_range_set(
        rows,
        [{"chapter": 1, "start": 0, "end": 3, "part": 1, "parts": 1}, {"chapter": 2, "start": 4, "end": 5, "part": 1, "parts": 1}],
        window_starts=[(1, 3, 7), (5, 5, 8)],
    )
    assert [w["chapter"] for w in ext_set.windows] == [1, 2]
    assert [w["position"] for w in ext_set.windows] == ["整章", "整章"]
    assert [p.text for p in ext_set.paragraphs.values()] == ["林昭推开门。", "周远没有回头。", "沈青笑了。"]
    assert [p.window_no for p in ext_set.paragraphs.values()] == [7, 7, 8]
    assert ext_set.windows[0]["text"].startswith("【1】林昭推开门。")


# ---------------------------------------------------------------- 归并（纯函数）


def _raw(ref: str, batch: int, kind: str = "observation", statement: str = "短句收尾") -> RawFinding:
    return RawFinding(
        ref=ref,
        finding_id=f"f_{ref}",
        dimension="language.sentence_structure",
        kind=kind,
        statement=statement,
        confidence="high",
        batch=batch,
        distinctiveness=None,
        quote_ids=(f"q_{ref}_a", f"q_{ref}_b"),
    )


def test_consolidation_checks_refs_and_ranks_groups_by_how_many_batches_show_them() -> None:
    sent = [
        _raw("r1", 0), _raw("r2", 1), _raw("r3", 1), _raw("r4", 2),
        _raw("r5", 0, "forbidden_pattern", "不用长从句"), _raw("r6", 3),
    ]
    structured = {
        "model_default": "通用写法句子整齐",
        "devices": ["急刹短句", "急刹短句"],
        "distinctiveness": 0.8,
        "groups": [
            # r5 是 avoid,进不了观察组;r9 不存在
            {"kind": "observation", "statement": "长段之后用一个短句急刹", "refs": ["r1", "r2", "r4", "r5", "r9"], "distinctiveness": 0.7},
            # r2 已经进过一组;r3 / r6 有效
            {"kind": "observation", "statement": "叙述里夹一句自问", "refs": ["r2", "r3", "r6"], "distinctiveness": 0.9},
            {"kind": "avoid", "statement": "不用层层嵌套的长从句", "refs": ["5"], "distinctiveness": 0.5},
            # 没有一条有效编号 → 丢
            {"kind": "observation", "statement": "这一组的编号全是错的", "refs": ["r40"], "distinctiveness": 0.5},
            {"kind": "observation", "statement": "空", "refs": ["r6"], "distinctiveness": 0.5},
        ],
    }
    result = parse_consolidation(structured, "language.sentence_structure", sent)
    assert not result.malformed and result.problems == []
    assert [(g.statement, [m.ref for m in g.members]) for g in result.groups] == [
        ("长段之后用一个短句急刹", ["r1", "r2", "r4"]),
        ("叙述里夹一句自问", ["r3", "r6"]),
        ("不用层层嵌套的长从句", ["r5"]),
    ]
    assert result.devices == ["急刹短句"] and result.assigned == 6 and result.rejected == 2
    ranked = rank_groups(result.groups)
    assert [g.support for g in ranked] == [3, 2, 1]
    assert support_confidence(3, 10) == "high" and support_confidence(1, 10) == "medium" and support_confidence(1, 40) == "low"
    # 输出不成形 / 几乎没有发现进组 → 要重问
    assert parse_consolidation({"groups": None}, "language.sentence_structure", sent).malformed
    lazy = parse_consolidation(
        {"groups": [{"kind": "observation", "statement": "长段之后用一个短句急刹", "refs": ["r1"]}]},
        "language.sentence_structure",
        sent,
    )
    assert lazy.problems


# ---------------------------------------------------------------- 在上一版上修订（纯函数）


def test_keep_reuses_the_previous_line_and_an_excluded_line_never_comes_back() -> None:
    dim = "scene.dialogue"
    old_do = CardLine(line_id=line_id_for(dim, "对白里夹一句抱怨"), text="对白里夹一句抱怨", kind="do")
    old_bad = CardLine(line_id=line_id_for(dim, "对白都很长"), text="对白都很长", kind="do")
    previous_card = DimensionCard(dimensions=[DimensionEntry(dimension=dim, lines=[old_do, old_bad])])
    previous = PreviousCard.from_card(previous_card, {old_bad.line_id: "excluded"})
    assert list(previous.lines) == [old_do.line_id] and previous.rejected == ("对白都很长",)
    findings = [FindingRef(ref="f1", finding_id="fd1", dimension=dim, kind="observation", statement="s", confidence="high", quote_ids=("q1",), quotes=("引文",))]
    structured = {
        "dimensions": [
            {
                "dimension": dim,
                "summary": "对白",
                "model_default": "",
                "distinctiveness": 0.6,
                "devices": [],
                "do": [
                    {"text": "换了个说法的抱怨", "refs": ["f1"], "keep": old_do.line_id, "mandatory": False, "distinctiveness": 0.5},
                    {"text": "随便写的", "refs": ["f1"], "keep": old_bad.line_id, "mandatory": False, "distinctiveness": 0.5},
                    {"text": "对白都很长", "refs": ["f1"], "keep": "", "mandatory": False, "distinctiveness": 0.5},
                    # keep 指向别的维 / 不存在的 id → 当新句
                    {"text": "说话人放在后面", "refs": ["f1"], "keep": "cl_nope", "mandatory": False, "distinctiveness": 0.5},
                ],
                "avoid": [],
            }
        ]
    }
    assembly = assemble_card(structured, findings, {}, generated_at="t", previous=previous)
    lines = assembly.card.all_lines()
    assert [(line.text, line.source) for _d, line in lines] == [("对白里夹一句抱怨", "kept"), ("说话人放在后面", "synthesis")]
    assert lines[0][1].line_id == old_do.line_id and assembly.kept == 1
    assert assembly.dropped["excluded_by_author"] == 2


# ---------------------------------------------------------------- 通读全书（作业）


def _body_paragraph_ids(session) -> set[str]:
    windows = session.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == BOOK)).all()
    first, last = min(w.start_index for w in windows), max(w.end_index for w in windows)
    rows = session.execute(
        select(StyleReferenceParagraph.paragraph_index, StyleReferenceParagraph.text).where(StyleReferenceParagraph.book_id == BOOK)
    ).all()
    return {
        compact_ws(text)
        for index, text in rows
        if first <= index <= last and compact_ws(text) and non_body_kind(compact_ws(text)) is None
    }


def test_full_learn_reads_every_batch_merges_by_dimension_and_writes_the_profile(session, monkeypatch) -> None:
    seed(session)
    fake, job = _learn(monkeypatch, mode="full")
    assert job.state == "succeeded", job.error_json
    cursor = job.cursor_json
    assert cursor["phases_done"] == list(learn_job.PHASE_ORDER)
    plan = cursor["plan"]["batches"]
    assert len(plan) == 3 and [len(b["segments"]) for b in plan] == [2, 2, 2]
    # 每批 × 每层各一次;每维一次归并(16 维都有发现);合成 / 专名各一次
    assert fake.count(EXTRACT) == 3 * 4
    synth_payloads = [p for node, p in fake.payloads if node == NODE_SYNTH]
    merges = [p for p in synth_payloads if _is_merge(p)]
    cards = [p for p in synth_payloads if _is_card(p)]
    assert len(merges) == 16 and len(cards) == 1
    # 抽取载荷带 scope,所有批合起来读到了全书每一段正文
    extract_payloads = [p for node, p in fake.payloads if node.startswith(EXTRACT)]
    assert {(p["scope"]["batch"], p["scope"]["batches"]) for p in extract_payloads} == {(1, 3), (2, 3), (3, 3)}
    assert extract_payloads[0]["scope"]["chapters"] in {"第 1–2 章", "第 3–4 章", "第 5–6 章"}
    seen = {text for p in extract_payloads for _n, text in paragraphs_of(p)}
    assert seen == _body_paragraph_ids(session)
    # 归并:各批同一说法 → 一条,支持 = 3 批;每维 2 观察 + 1 避免
    assert merges[0]["batches"] == 3 and {f["batch"] for f in merges[0]["findings"]} == {1, 2, 3}
    run_id = job.result_json["run_id"]
    merged = session.scalars(
        select(StyleReferenceFinding).where(StyleReferenceFinding.run_id == run_id, StyleReferenceFinding.status == STATUS_CONSOLIDATED)
    ).all()
    assert len(merged) == 16 * 3 and all(f.confidence == "high" for f in merged)
    raw = session.scalars(
        select(StyleReferenceFinding).where(StyleReferenceFinding.run_id == run_id, StyleReferenceFinding.status != STATUS_CONSOLIDATED)
    ).all()
    assert len(raw) == 3 * 16 * 3
    # 归并后的发现的证据指向成员已核对过的引文,取自不同的批
    evidence_quotes = session.scalars(
        select(StyleReferenceEvidence.quote_id).where(StyleReferenceEvidence.finding_id == merged[0].finding_id)
    ).all()
    raw_quotes = set(
        session.scalars(
            select(StyleReferenceEvidence.quote_id).where(StyleReferenceEvidence.finding_id.in_([f.finding_id for f in raw]))
        ).all()
    )
    assert len(evidence_quotes) == 4 and set(evidence_quotes) <= raw_quotes
    merge_rows = session.scalars(
        select(StyleReferenceExtraction).where(StyleReferenceExtraction.run_id == run_id, StyleReferenceExtraction.purpose == "consolidate")
    ).all()
    assert len(merge_rows) == 16
    meta = next(iter(merge_rows[0].raw_payload_json["finding_meta"].values()))
    assert meta["support"] == 3 and meta["batches"] == [0, 1, 2] and len(meta["members"]) == 3
    # 文风卡读归并后的发现(带支持数),卡句连到归并后的发现
    card_payload = cards[0]
    first_obs = card_payload["dimensions"][0]["observations"][0]
    assert first_obs["seen_in"] == 3 and first_obs["of"] == 3
    assert card_payload["coverage"] == {"mode": "full", "batches": 3, "chapters": 6}
    assert "previous_card" not in card_payload
    profile = _profile(job.result_json["profile_id"])
    card = card_from_profile_json(profile.profile_json)
    merged_ids = {f.finding_id for f in merged}
    assert all(set(line.finding_ids) <= merged_ids for _d, line in card.all_lines())
    assert set(profile.source_finding_ids_json) == merged_ids
    extraction = profile.profile_json["learned_from"]["extraction"]
    assert extraction["mode"] == "full" and extraction["batches"] == 3 and extraction["chapters"] == 6
    assert extraction["reused_reading"] is False and extraction["read_run_id"] == run_id
    result = job.result_json
    assert result["mode"] == "full" and result["reading"]["units"] == 12 and result["reading"]["raw_findings"] == 144
    assert result["reading"]["consolidated_findings"] == 48
    run = session.get(StyleReferenceRun, run_id)
    assert run.coverage_json["full"]["read_complete"] is True and run.coverage_json["full"]["consolidated"] is True
    assert job.progress_json["done"] == job.progress_json["total"]
    assert job.progress_json["phase_label"].startswith("通读全书")


def test_an_empty_dimension_in_one_batch_is_not_asked_again(session, monkeypatch) -> None:
    seed(session)

    def script(node, payload, _call_no):
        if node == "style_ref_extract_narrative" and payload.get("scope", {}).get("batch") == 2:
            output = default_extract(payload)
            output["dimensions"][2]["observations"] = []  # 这两章里没有插叙
            return output
        return None

    fake, job = _learn(monkeypatch, FakeLearnLLM(script=script), mode="full")
    assert job.state == "succeeded", job.error_json
    assert fake.count("style_ref_extract_narrative") == 3
    assert not any(retry for node, retry in fake.extra_instructions if node.startswith(EXTRACT))


def test_a_second_full_learn_reuses_the_reading_and_the_merge_and_revises_the_card(session, monkeypatch) -> None:
    seed(session)
    _fake1, first = _learn(monkeypatch, mode="full")
    assert first.state == "succeeded", first.error_json
    profile_id = first.result_json["profile_id"]
    card = card_from_profile_json(_profile(profile_id).profile_json)
    pinned = next(line for _d, line in card.all_lines() if line.kind == "do")
    with SessionLocal() as db:
        set_card_line_state(db, profile_id, pinned.line_id, "pinned")
        db.commit()
    # 估算预告:上一次通读能沿用
    with SessionLocal() as db:
        estimate = learn_job.estimate_learning(db, db.get(StyleReferenceBook, BOOK))
    assert estimate["full"]["batches"] == 3 and estimate["full"]["reusable"]["run_id"] == first.result_json["run_id"]

    fake, second = _learn(monkeypatch, mode="full")
    assert second.state == "succeeded", second.error_json
    assert fake.count(EXTRACT) == 0
    synth = [p for node, p in fake.payloads if node == NODE_SYNTH]
    cards = [p for p in synth if _is_card(p)]
    # 不再读书、不再归并、伏笔配对也沿用:只重写文风卡(与每次都在上一版上修订的伏笔手法)
    assert len(cards) == 1 and not any(_is_merge(p) or "plants" in p for p in synth)
    assert second.cursor_json["full"]["reused_reading"] and second.cursor_json["full"]["reused_consolidation"]
    assert second.cursor_json["foreshadow"]["reused"] is True
    # 修订:上一版的句子都带去了,假模型 keep 了每维的 do → 原句、原 id,✓ 跟着走
    previous_ids = {line["id"] for entry in cards[0]["previous_card"] for line in entry["lines"]}
    assert pinned.line_id in previous_ids
    profile = _profile(profile_id)
    new_card = card_from_profile_json(profile.profile_json)
    new_do = {line.line_id: line for _d, line in new_card.all_lines() if line.kind == "do"}
    assert pinned.line_id in new_do and new_do[pinned.line_id].text == pinned.text and new_do[pinned.line_id].source == "kept"
    assert profile.profile_json["card_line_states"] == {pinned.line_id: "pinned"}
    assert second.result_json["card"]["kept_from_previous"] == len(new_do)
    assert profile.version_tag == "v2"
    # 卡句仍连到第一次归并出来的发现
    first_merged = {
        f.finding_id
        for f in session.scalars(
            select(StyleReferenceFinding).where(
                StyleReferenceFinding.run_id == first.result_json["run_id"], StyleReferenceFinding.status == STATUS_CONSOLIDATED
            )
        )
    }
    assert set(profile.source_finding_ids_json) == first_merged
    assert profile.profile_json["learned_from"]["extraction"]["reused_reading"] is True

    # reread:从头再读、再归并
    fake3, third = _learn(monkeypatch, mode="full", reread=True)
    assert third.state == "succeeded", third.error_json
    assert fake3.count(EXTRACT) == 12 and sum(1 for n, p in fake3.payloads if n == NODE_SYNTH and _is_merge(p)) == 16


def _stale(job_id: str) -> None:
    with SessionLocal() as db:
        old = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
        db.execute(update(StyleReferenceJob).where(StyleReferenceJob.job_id == job_id).values(heartbeat_at=old))
        db.commit()


@pytest.mark.parametrize("crash_node", ["style_ref_extract_scene", NODE_SYNTH])
def test_a_crash_while_reading_or_merging_resumes_without_redoing_finished_work(session, monkeypatch, crash_node: str) -> None:
    seed(session)
    state = {"seen": 0, "crashed": False}

    def script(node, payload, _call_no):
        if node != crash_node or state["crashed"]:
            return None
        if crash_node == NODE_SYNTH and not _is_merge(payload):
            return None
        state["seen"] += 1
        # 3 个并行:第 4 次调用要等前面至少一份落库才会派发——崩的时候一定有做完的
        if state["seen"] == (4 if crash_node == NODE_SYNTH else 2):
            state["crashed"] = True
            return "crash"
        return None

    fake = _use(monkeypatch, FakeLearnLLM(script=script))
    job_id = _start(mode="full")
    with pytest.raises(SimulatedCrash):
        run_job_inline(job_id)
    stuck = load_job(job_id)
    assert stuck.state == "running"
    before = len(fake.calls)
    _stale(job_id)
    with SessionLocal() as db:
        assert job_id in StyleJobService(db).sweep()
        db.commit()
    run_job_inline(job_id)
    job = load_job(job_id)
    assert job.state == "succeeded", job.error_json
    # 崩溃时在飞的别的调用(死掉那一轮的守护线程)还会打到假模型上——只看续跑有没有重做已经落库的份
    after = fake.payloads[before:]
    if crash_node.startswith(EXTRACT):
        done_units = set(stuck.cursor_json.get("units_done") or [])
        assert done_units
        redone = {f"{p['scope']['batch'] - 1}:{node[len(EXTRACT):]}" for node, p in after if node.startswith(EXTRACT)}
        assert not (redone & done_units)
        assert redone == {f"{b}:{layer}" for b in range(3) for layer in ("language", "narrative", "scene", "theme")} - done_units
    else:
        done_dims = set(stuck.cursor_json.get("consolidated_dims") or [])
        assert done_dims
        assert not any(n.startswith(EXTRACT) for n, _p in after)
        merged_again = {p["dimension"]["key"] for n, p in after if n == NODE_SYNTH and _is_merge(p)}
        assert not (merged_again & done_dims) and len(merged_again) == 16 - len(done_dims)
    merged = session.scalars(
        select(StyleReferenceFinding).where(
            StyleReferenceFinding.run_id == job.result_json["run_id"], StyleReferenceFinding.status == STATUS_CONSOLIDATED
        )
    ).all()
    assert len(merged) == 16 * 3


def test_a_quick_relearn_is_revised_on_the_previous_card_and_honours_exclusions(session, monkeypatch) -> None:
    seed(session)
    _fake1, first = _learn(monkeypatch)
    assert first.state == "succeeded" and first.cursor_json["mode"] == "quick"
    profile_id = first.result_json["profile_id"]
    card = card_from_profile_json(_profile(profile_id).profile_json)
    do_lines = [line for dim, line in card.all_lines() if line.kind == "do" and dim == card.dimensions[0].dimension]
    excluded = do_lines[0]
    with SessionLocal() as db:
        set_card_line_state(db, profile_id, excluded.line_id, "excluded")
        db.commit()
    fake, second = _learn(monkeypatch)
    assert second.state == "succeeded", second.error_json
    payload = next(p for node, p in fake.payloads if node == NODE_SYNTH)
    assert payload["rejected_lines"] == [excluded.text]
    assert excluded.line_id not in {line["id"] for entry in payload["previous_card"] for line in entry["lines"]}
    new_card = card_from_profile_json(_profile(profile_id).profile_json)
    texts = {line.text for _d, line in new_card.all_lines()}
    assert excluded.text not in texts
    assert second.result_json["card"]["kept_from_previous"] > 0
    assert second.result_json["mode"] == "quick" and second.result_json["reading"] is None
    assert _profile(profile_id).profile_json["learned_from"]["extraction"]["mode"] == "quick"


def test_learn_route_takes_the_mode_and_reports_both_estimates(client, session, monkeypatch) -> None:
    import novel_system.api.routes.style_reference as sr_routes

    seed(session)
    fake = _use(monkeypatch, FakeLearnLLM())
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    monkeypatch.setattr(sr_routes, "dispatch_job", lambda job_id: None)
    bad = client.post(f"{PREFIX}/books/{BOOK}/learn", json={"mode": "everything"})
    assert bad.status_code == 422
    response = client.post(f"{PREFIX}/books/{BOOK}/learn", json={"mode": "full", "reread": True})
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["job_id"]
    params = load_job(job_id).params_json
    assert params["mode"] == "full" and params["reread"] is True
    run_job_inline(job_id)
    info = client.get(f"{PREFIX}/books/{BOOK}/learn").json()["data"]
    assert info["learn"]["state"] == "succeeded"
    full = info["estimate"]["full"]
    assert full["batches"] >= 1 and full["calls"]["consolidate"] == 16 and full["reusable"] is not None
    assert info["estimate"]["calls"]["extract"] == 4
    detail = client.get(f"{PREFIX}/profiles/{info['learn']['profile_id']}").json()["data"]["profile"]
    assert detail["learned_from"]["extraction"]["mode"] == "full"
    assert detail["learned_from"]["extraction"]["batches"] == load_job(job_id).result_json["reading"]["batches"]


# ---------------------------------------------------------------- 顺带学的结构与示范窗（2026-10-19）


def _range_set():
    rows = [(i, "narration", f"第{i}段，林昭在雨城的旧信里找到一句话。", f"p{i}") for i in range(12)]
    segments = [
        {"chapter": 7, "start": 0, "end": 5, "part": 1, "parts": 1},
        {"chapter": 8, "start": 6, "end": 11, "part": 1, "parts": 2},
    ]
    return build_range_set(rows, segments)


def test_structure_output_keeps_only_real_scene_turns_and_whole_chapter_records() -> None:
    from novel_system.services.style_reference.learn_structure import parse_structure_output

    ext_set = _range_set()
    structured = {
        "scene_starts": [
            {"p": 3, "shift": "地点"},
            {"p": 1, "shift": "时间"},  # 第 7 章的第一段:章的开头,不算换场
            {"p": 7, "shift": "视点"},  # 第 8 章(切过的章)的第一段
            {"p": 9, "shift": "回忆"},  # 不在词表
            {"p": 40, "shift": "地点"},  # 不在这一批
            {"p": 10, "shift": "事件"},
            {"p": 3, "shift": "时间"},  # 重复
        ],
        "chapters": [
            {"window": 1, "function": "高潮", "hook": "反转", "threads": 9, "povs": 0},
            {"window": 2, "function": "推进", "hook": "悬念未解", "threads": 2, "povs": 1},  # 不是整章
            {"window": 1, "function": "铺垫", "hook": "平稳收束", "threads": 1, "povs": 1},  # 同一片段第二次
            {"window": 3, "function": "推进", "hook": "反转", "threads": 1, "povs": 1},  # 没这个片段
        ],
    }
    out = parse_structure_output(structured, ext_set)
    assert out["scene_starts"] == [[2, "地点"], [9, "事件"]]
    assert out["chapters"] == [{"chapter": 7, "function": "高潮", "hook": "反转", "threads": 6, "povs": 1}]
    assert parse_structure_output({"scene_starts": "x"}, ext_set) == {"scene_starts": [], "chapters": [], "plants": [], "payoffs": []}


def test_learned_scenes_measure_scene_lengths_and_chapter_craft_sums_up_the_chapters() -> None:
    from novel_system.services.style_reference.learn_structure import chapter_craft, learned_scene_stats, merge_batches

    starts, records = merge_batches(
        {
            "1": {"scene_starts": [[30, "时间"]], "chapters": [{"chapter": 2, "function": "高潮", "hook": "反转", "threads": 3, "povs": 2}]},
            "0": {
                "scene_starts": [[5, "地点"], [8, "地点"]],
                "chapters": [
                    {"chapter": 1, "function": "铺垫", "hook": "悬念未解", "threads": 2, "povs": 1},
                ],
            },
            "2": {"scene_starts": [], "chapters": [
                {"chapter": 3, "function": "推进", "hook": "悬念未解", "threads": 2, "povs": 1},
                {"chapter": 4, "function": "高潮", "hook": "悬念未解", "threads": 1, "povs": 1},
            ]},
        }
    )
    assert starts == [(5, "地点"), (8, "地点"), (30, "时间")]
    assert [r["chapter"] for r in records] == [1, 2, 3, 4]
    stats = learned_scene_stats([(1, 0, 19), (2, 20, 39), (3, 40, 49)], starts, lambda a, b: (b - a + 1) * 100)
    assert stats["scene_break_style"] == "learned" and stats["scene_chapters_read"] == 3
    assert stats["scene_break_chapters"] == 2
    # 第 1 章 3 场(500 / 300 / 1200 字)、第 2 章 2 场(1000 / 1000)、第 3 章 1 场(1000)
    assert stats["scenes_per_chapter"]["median"] == 2 and stats["scene_chars"]["median"] == 1000
    assert stats["scene_shift_distribution"] == {"时间": 0.333, "地点": 0.667}
    craft = chapter_craft(records)
    assert craft["chapters_read"] == 4
    assert craft["function_distribution"] == {"高潮": 0.5, "铺垫": 0.25, "推进": 0.25}
    assert list(craft["hook_distribution"]) == ["悬念未解", "反转"]
    assert craft["climax_gap"] == 2 and craft["threads_per_chapter"]["median"] == 2
    assert chapter_craft([]) is None and learned_scene_stats([], starts, lambda a, b: 1) is None


def test_learned_scenes_drive_the_scene_scale_and_the_planning_block() -> None:
    from novel_system.services.style_reference.structure_card import reference_scene_scale, scene_sizes_measured
    from novel_system.services.style_reference.structure_render import render_structure_card_parts

    card = {
        "chapter_count": 10,
        "has_chapter_markers": True,
        "chapter_chars": {"median": 18000, "p10": 9000, "p90": 33000},
        "paragraphs_per_chapter": {"median": 240, "p10": 100, "p90": 500},
        "scene_break_style": "learned",
        "scene_chapters_read": 10,
        "scenes_per_chapter": {"median": 4, "p10": 2, "p90": 7},
        "scene_chars": {"median": 4200, "p10": 1500, "p90": 9000},
        "scene_shift_distribution": {"地点": 0.5, "时间": 0.3, "视点": 0.2},
        "chapter_craft": {
            "chapters_read": 10,
            "function_distribution": {"推进": 0.5, "高潮": 0.2, "铺垫": 0.2, "日常": 0.1},
            "hook_distribution": {"悬念未解": 0.6, "反转": 0.3, "平稳收束": 0.1},
            "threads_per_chapter": {"median": 2, "p10": 1, "p90": 3},
            "povs_per_chapter": {"median": 1, "p10": 1, "p90": 2},
            "climax_gap": 4,
        },
    }
    assert scene_sizes_measured(card) and not scene_sizes_measured({"scene_break_style": "none"})
    scale = reference_scene_scale(card, scenes_in_chapter=3)
    assert scale["basis"] == "learned_scene_breaks" and scale["raw_scene_chars"] == 4200
    block, _samples = render_structure_card_parts({"structure_card": card}, include_samples=False)
    assert "- 场：通读时按时间 / 地点 / 视点 / 事件的转换切出（10 章），每章约 4 场" in block
    assert "换场多靠地点 50%、时间 30%、视点 20%" in block
    assert "- 章的功能（通读 10 章）：推进 50%、高潮 20%、铺垫 20%、日常 10%；高潮章之间约隔 4 章" in block
    assert "- 章末怎么收：悬念未解 60%、反转 30%、平稳收束 10%" in block
    assert "- 一章之内：通常交织 2 条线（p90 3）、1 个视点人物（p90 2）" in block
    assert "每章约 4 场、场长约 4,200 字" in block


def test_full_learn_also_learns_scene_turns_chapter_craft_and_where_each_dimension_is_shown(session, monkeypatch) -> None:
    from novel_system.services.style_reference.inject.selection import load_index
    from novel_system.services.style_reference.windows import set_window_tags

    seed(session)
    fake, job = _learn(monkeypatch, mode="full")
    assert job.state == "succeeded", job.error_json
    # 叙事层顺带交的结构:每批记进游标、读完记进 run 行
    units = job.cursor_json["structure_units"]
    assert sorted(units) == ["0", "1", "2"]
    run = session.get(StyleReferenceRun, job.result_json["run_id"])
    assert run.coverage_json["full"]["structure"] == units
    assert job.result_json["structure"] == {"scene_starts": 6, "chapters": 6}
    narrative = [p for node, p in fake.payloads if node == "style_ref_extract_narrative"]
    assert len(narrative) == 3
    structure = _profile(job.result_json["profile_id"]).profile_json["structure_card"]
    assert structure["scene_break_style"] == "learned" and structure["scene_chapters_read"] == 6
    assert structure["scenes_per_chapter"]["median"] == 2
    craft = structure["chapter_craft"]
    assert craft["chapters_read"] == 6 and craft["function_distribution"] == {"推进": 0.833, "高潮": 0.167}
    assert craft["hook_distribution"] == {"悬念未解": 0.5, "反转": 0.5}
    # 文风卡合成看得到这些结构事实
    card_payload = next(p for node, p in fake.payloads if node == NODE_SYNTH and _is_card(p))
    assert any(fact.startswith("章末怎么收") for fact in card_payload["structure_facts"])
    # 示范窗:有证据的窗,选窗索引用证据维
    windows = session.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == BOOK)).all()
    with_evidence = [w for w in windows if (w.tags_json or {}).get("evidence")]
    assert with_evidence and job.result_json["windows"]["with_evidence"] == len(with_evidence)
    sample = with_evidence[0].tags_json["evidence"]
    assert sample["version"] == "evidence_v1" and sample["profile_id"] == job.result_json["profile_id"]
    assert sample["lines"] and all(n >= 1 for n in sample["counts"].values())
    index, _root, _notices = load_index(session, BOOK, build=False)
    by_no = {w.window_no: w for w in index}
    for window in with_evidence:
        evidence_dims = tuple(window.tags_json["evidence"]["dimensions"])
        expected = evidence_dims or tuple(window.tags_json.get("dimensions") or ())
        assert by_no[window.window_no].dimensions == expected
    # 重打标签不丢证据
    target = with_evidence[0]
    set_window_tags(session, BOOK, {target.window_no: {"situations": ["日常闲谈"], "moods": ["平静"], "dimensions": []}}, tags_version="t")
    session.commit()
    session.refresh(target)
    assert target.tags_json["evidence"] == sample and target.tags_json["situations"] == ["日常闲谈"]

    # 沿用这次通读时结构一起沿用,不再调叙事层
    fake2, second = _learn(monkeypatch, mode="full")
    assert second.state == "succeeded", second.error_json
    assert fake2.count(EXTRACT) == 0
    again = _profile(second.result_json["profile_id"]).profile_json["structure_card"]
    assert again["chapter_craft"] == craft and again["scene_chars"] == structure["scene_chars"]


def test_quick_learn_writes_evidence_for_the_windows_it_read_and_learns_no_structure(session, monkeypatch) -> None:
    seed(session)
    fake, job = _learn(monkeypatch)
    assert job.state == "succeeded", job.error_json
    narrative = next(p for node, p in fake.payloads if node == "style_ref_extract_narrative")
    assert "scope" not in narrative
    structure = _profile(job.result_json["profile_id"]).profile_json["structure_card"]
    assert structure["scene_break_style"] == "none" and "chapter_craft" not in structure
    assert "structure" not in job.result_json
    windows = session.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == BOOK)).all()
    assert job.result_json["windows"]["with_evidence"] == sum(1 for w in windows if (w.tags_json or {}).get("evidence")) > 0


# ---------------------------------------------------------------- 全书例子 / 分布 / 按场面的写法（2026-10-19 第三轮）


def test_situation_ranking_keeps_findings_concentrated_in_a_kind_of_scene() -> None:
    from novel_system.services.style_reference.learn_situations import FindingSpot, rank_situation_findings

    # 10 窗:0–3 打斗追逐,其余日常闲谈
    windows = {no: (["打斗追逐"] if no < 4 else ["日常闲谈"]) for no in range(10)}
    spots = [
        # 引文全在打斗窗:集中度 = 1 / 0.4 = 2.5
        FindingSpot("f_fight", "language.rhetoric", "打斗时用降格比喻", (("q1", 0, "像一只被踩了尾巴的猫"), ("q2", 1, "刀光乱得像过年"), ("q3", 2, "长长的一句话说了很久很久"))),
        # 一半一半:打斗占 0.5 / 0.4 = 1.25 < 1.5
        FindingSpot("f_even", "scene.dialogue", "对白里夹抱怨", (("q4", 0, "烦死了"), ("q5", 5, "又来了"))),
        # 只有一处打斗引文:不够两处
        FindingSpot("f_once", "narrative.pacing", "短句急刹", (("q6", 3, "停。"), ("q7", 7, "走。"), ("q8", 8, "等等。"))),
        # 引文不在任何窗里
        FindingSpot("f_out", "theme.motifs", "雨夜", (("q9", None, "雨"),)),
    ]
    ranked = rank_situation_findings(windows, spots, min_windows=3)
    assert [c.situation for c in ranked] == ["打斗追逐"]
    fight = ranked[0]
    assert fight.windows == 4 and fight.of_windows == 10
    assert [(f["ref"], f["finding_id"], f["lift"], f["quotes_in"]) for f in fight.findings] == [("s1", "f_fight", 2.5, 3)]
    assert fight.findings[0]["quote"] == "刀光乱得像过年"  # 最短的那处
    # 场面太少的不做
    assert rank_situation_findings(windows, spots, min_windows=5) == []


def test_situation_lines_need_grounding_keep_the_previous_wording_and_go_through_the_card_filters() -> None:
    from novel_system.services.style_reference.learn_situations import (
        SituationCandidates,
        filter_situation_card,
        parse_situation_lines,
    )

    candidates = SituationCandidates(
        situation="打斗追逐",
        windows=4,
        of_windows=10,
        findings=[{"ref": "s1", "finding_id": "f1", "quote_id": "q1"}, {"ref": "s2", "finding_id": "f2", "quote_id": "q2"}],
    )
    structured = {
        "lines": [
            {"text": "打斗里用日常物件打比方", "refs": ["s1"], "keep": ""},
            {"text": "没有依据的一句", "refs": ["s9"], "keep": ""},
            {"text": "出手前数到3", "refs": ["s2"], "keep": ""},  # 阿拉伯数字
            {"text": "随便换个说法", "refs": ["S2"], "keep": "cl_old"},
        ]
    }
    lines = parse_situation_lines(structured, candidates, previous={"cl_old": "动手之前先让人物嘴硬一句"})
    assert [(line["text"], line["source"], line["finding_ids"]) for line in lines] == [
        ("打斗里用日常物件打比方", "synthesis", ["f1"]),
        ("动手之前先让人物嘴硬一句", "kept", ["f2"]),
    ]
    assert lines[1]["line_id"] == "cl_old" and lines[0]["line_id"].startswith("cl_")
    card = filter_situation_card(
        {"打斗追逐": {"windows": 4, "lines": lines}, "日常闲谈": {"windows": 9, "lines": [{**lines[0], "text": "林昭爱说反话"}]}},
        lambda text: "林昭" in text,
    )
    assert card["version"] == "situation_card_v1"
    assert [(e["situation"], [line["text"] for line in e["lines"]]) for e in card["situations"]] == [
        ("打斗追逐", ["打斗里用日常物件打比方", "动手之前先让人物嘴硬一句"])
    ]
    assert filter_situation_card({"打斗追逐": {"windows": 4, "lines": lines}}, lambda text: True) is None


def test_the_drafting_card_gets_the_lines_of_this_scenes_situations() -> None:
    from novel_system.services.style_reference.card import plan_card_block
    from novel_system.services.style_reference.inject.blocks import FIT_SITUATIONS_UNIT, DimensionCardSource
    from novel_system.services.style_reference.situation_card import situation_lines_for

    profile_json = {
        "situation_card": {
            "version": "situation_card_v1",
            "situations": [
                {"situation": "日常闲谈", "windows": 30, "lines": [{"line_id": "cl_a", "text": "闲谈里句句带刺"}]},
                {"situation": "打斗追逐", "windows": 12, "lines": [
                    {"line_id": "cl_b", "text": "打斗里用日常物件打比方"},
                    {"line_id": "cl_c", "text": "动手之前先嘴硬一句"},
                ]},
                {"situation": "回忆往事", "windows": 9, "lines": [{"line_id": "cl_d", "text": "回忆用现在时插进来"}]},
            ],
        }
    }
    picked = situation_lines_for(profile_json, ["打斗追逐", "情感交流", "回忆往事", "日常闲谈"], max_situations=2)
    assert picked == [("打斗追逐", "打斗里用日常物件打比方"), ("打斗追逐", "动手之前先嘴硬一句"), ("回忆往事", "回忆用现在时插进来")]
    assert situation_lines_for({}, ["打斗追逐"]) == []
    card = DimensionCard(
        dimensions=[DimensionEntry(dimension="scene.dialogue", label="对话写法", lines=[CardLine(line_id="cl_x", text="对白短，带口头禅")])]
    )
    text = plan_card_block(card, situation_lines=picked, budget_chars=2000).text
    assert "[这一场的场面]" in text and "- 打斗追逐：打斗里用日常物件打比方" in text
    assert text.index("[这一场的场面]") > text.index("对话写法")
    source = DimensionCardSource(
        card, role="draft", dimension_states={}, line_states={}, recent_gaps=(), budget_chars=2000, situation_lines=picked
    )
    assert FIT_SITUATIONS_UNIT in source.drop_order
    assert "[这一场的场面]" not in source.render(frozenset({FIT_SITUATIONS_UNIT}))
    # 预算紧时先保这一场的场面,再保各维的第一句
    from novel_system.services.style_reference.card import CARD_HEADER_DRAFT, CARD_SITUATIONS_TITLE

    budget = len(CARD_HEADER_DRAFT) + len(CARD_SITUATIONS_TITLE) + len("- 打斗追逐：打斗里用日常物件打比方") + 4
    tight = plan_card_block(card, situation_lines=picked[:1], budget_chars=budget)
    assert "打斗里用日常物件打比方" in tight.text and "对话写法" not in tight.text


def test_full_learn_writes_situation_lines_coverage_and_serves_every_example(client, session, monkeypatch) -> None:
    from novel_system.services.style_reference import learn_situations

    # 合成书的窗口都是「日常闲谈」:放低门槛,让这一类场面进来
    monkeypatch.setattr(learn_situations, "LIFT_MIN", 0.5)
    monkeypatch.setattr(learn_situations, "MIN_WINDOWS", 1)
    seed(session)
    fake, job = _learn(monkeypatch, mode="full")
    assert job.state == "succeeded", job.error_json
    assert job.cursor_json["phases_done"] == list(learn_job.PHASE_ORDER)
    situation_payloads = [p for node, p in fake.payloads if node == NODE_SYNTH and "situation" in p]
    assert len(situation_payloads) == 1 and situation_payloads[0]["situation"] == "日常闲谈"
    assert situation_payloads[0]["card_lines"] and situation_payloads[0]["previous_lines"] == []
    assert job.result_json["situations"] == {"日常闲谈": 2}
    profile_id = job.result_json["profile_id"]
    pj = _profile(profile_id).profile_json
    entry = pj["situation_card"]["situations"][0]
    assert entry["situation"] == "日常闲谈" and len(entry["lines"]) == 2
    situation_line = entry["lines"][0]
    # 每句的依据落在哪几章:文风卡句与场面句都有
    card = card_from_profile_json(pj)
    card_line = next(line for _d, line in card.all_lines() if line.kind == "do")
    coverage = pj["line_evidence"][card_line.line_id]
    assert coverage["quotes"] >= 4 and all(chapter >= 1 for chapter, _n in coverage["chapters"])
    assert pj["line_evidence"][situation_line["line_id"]]["quotes"] >= 1
    # 画像页:每句带覆盖、按场面一节、全书章数
    detail = client.get(f"{PREFIX}/profiles/{profile_id}").json()["data"]["profile"]
    assert detail["book_chapters"] == 6
    assert detail["situations"][0]["situation"] == "日常闲谈" and detail["situations"][0]["lines"][0]["coverage"]
    shown = next(line for dim in detail["dimensions"] for line in dim["lines"] if line["line_id"] == card_line.line_id)
    assert shown["coverage"]["quotes"] == coverage["quotes"]
    # 全书例子:按原书顺序、带章,条数与覆盖一致
    examples = client.get(f"{PREFIX}/profiles/{profile_id}/card-lines/{card_line.line_id}/examples").json()["data"]
    assert examples["total"] == coverage["quotes"] and examples["text"] == card_line.text
    indexes = [ex["paragraph_index"] for ex in examples["examples"]]
    assert indexes == sorted(indexes) and all(ex["text"] and ex["chapter"] for ex in examples["examples"])
    assert examples["chapters"] == coverage["chapters"]
    limited = client.get(f"{PREFIX}/profiles/{profile_id}/card-lines/{card_line.line_id}/examples?limit=1").json()["data"]
    assert len(limited["examples"]) == 1 and limited["total"] == coverage["quotes"]
    of_situation = client.get(f"{PREFIX}/profiles/{profile_id}/card-lines/{situation_line['line_id']}/examples").json()["data"]
    assert of_situation["label"] == "日常闲谈" and of_situation["total"] >= 1
    missing = client.get(f"{PREFIX}/profiles/{profile_id}/card-lines/cl_000000000000/examples")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "STYLE_REFERENCE_CARD_LINE_NOT_FOUND"
    # 再学一次:上一版的场面句交给模型,keep 了就是原句原 id
    fake2, second = _learn(monkeypatch, mode="full")
    assert second.state == "succeeded", second.error_json
    again = next(p for node, p in fake2.payloads if node == NODE_SYNTH and "situation" in p)
    assert [line["id"] for line in again["previous_lines"]] == [line["line_id"] for line in entry["lines"]]
    new_entry = _profile(profile_id).profile_json["situation_card"]["situations"][0]
    assert [line["line_id"] for line in new_entry["lines"]] == [line["line_id"] for line in entry["lines"]]
    assert all(line["source"] == "kept" for line in new_entry["lines"])


# ---------------------------------------------------------------- 伏笔与回收（2026-10-19 第四轮）


def _threads():
    plants = [
        {"id": "P1", "chapter": 1, "kind": "悬念提问", "gist": "旧信里少了一页"},
        {"id": "P2", "chapter": 2, "kind": "物件", "gist": "案卷里夹着一把钥匙"},
        {"id": "P3", "chapter": 5, "kind": "台词", "gist": "林昭说雨城从不下雪"},
        {"id": "P4", "chapter": 6, "kind": "悬念提问", "gist": "谁在门外敲了三下"},
    ]
    payoffs = [
        {"id": "R1", "chapter": 2, "style": "揭晓真相", "gist": "少的那页在抽屉底", "setup": "旧信缺页"},
        {"id": "R2", "chapter": 14, "style": "物件再现", "gist": "钥匙打开了地下室", "setup": "钥匙"},
        {"id": "R3", "chapter": 5, "style": "台词呼应", "gist": "当天就下了雪", "setup": "不下雪"},
        {"id": "R4", "chapter": 3, "style": "反转", "gist": "配到更晚的埋上", "setup": "?"},
    ]
    return plants, payoffs


def test_matches_are_checked_and_statistics_come_from_the_pairs() -> None:
    from novel_system.services.style_reference.learn_foreshadow import foreshadow_stats, parse_matches

    plants, payoffs = _threads()
    matches = parse_matches(
        {
            "matches": [
                {"payoff": "r1", "plant": "p1"},
                {"payoff": "R2", "plant": "P2"},
                {"payoff": "R3", "plant": "P3"},
                {"payoff": "R4", "plant": "P4"},  # 埋在收之后:不认
                {"payoff": "R1", "plant": "P2"},  # 同一处收第二次:不认
                {"payoff": "R9", "plant": "P1"},  # 没这处收
            ]
        },
        plants,
        payoffs,
    )
    assert matches == {"R1": "P1", "R2": "P2", "R3": "P3"}
    stats = foreshadow_stats(plants, payoffs, matches, chapters_read=[1, 2, 3, 5, 6, 14])
    assert stats["planted"] == 4 and stats["pairs"] == 3 and stats["paid_share"] == 0.75
    assert stats["distance"] == {"median": 1, "p10": 0, "p90": 10}
    assert stats["same_chapter_share"] == 0.333 and stats["long_range_share"] == 0.333
    assert list(stats["plant_kinds"]) == ["悬念提问", "物件", "台词"]
    assert stats["payoff_styles"] == {"揭晓真相": 0.333, "物件再现": 0.333, "台词呼应": 0.333}
    # 章末悬着:1 章 P1;2 章 P2(P1 已收);3 章 P2;5 章 P2(P3 当章收);6 章 P2、P4;14 章 P4
    assert stats["open_per_chapter"]["median"] == 1 and stats["open_per_chapter"]["p90"] == 2
    assert foreshadow_stats([], payoffs, {}, chapters_read=[1]) is None


def test_payoffs_are_sent_in_chunks_that_fit_and_craft_lines_are_grounded() -> None:
    from novel_system.services.style_reference.learn_foreshadow import (
        craft_pairs,
        match_chunks,
        merge_planning,
        parse_craft_lines,
    )

    plants, payoffs = _threads()
    chunks = match_chunks(plants, payoffs, lambda payload: len(payload["payoffs"]) <= 2, book_title="雨城旧信")
    assert [[r["id"] for r in chunk] for chunk in chunks] == [["R1", "R2"], ["R3", "R4"]]
    pairs = craft_pairs(plants, payoffs, {"R1": "P1", "R2": "P2", "R3": "P3"}, limit=2)
    assert [p["distance"] for p in pairs] == [0, 1]  # 按隔的章数排,均匀取
    lines = parse_craft_lines(
        {
            "lines": [
                {"text": "伏笔：把关键物件写成随手一笔", "refs": ["t1"], "keep": ""},
                {"text": "隔3章就收", "refs": ["t2"], "keep": ""},  # 阿拉伯数字
                {"text": "没有依据", "refs": ["t9"], "keep": ""},
                {"text": "换个说法", "refs": ["T2"], "keep": "cl_prev"},
            ]
        },
        [p["id"] for p in pairs],
        previous={"cl_prev": "疑问当章提出、当章就给半个答案"},
    )
    assert [(line["text"], line["source"]) for line in lines] == [
        ("把关键物件写成随手一笔", "synthesis"),
        ("疑问当章提出、当章就给半个答案", "kept"),
    ]
    assert lines[1]["line_id"] == "cl_prev"
    planning = [f"开场：第{i}句" for i in range(10)]
    merged = merge_planning(planning, lines, cap=10)
    assert len(merged) == 10 and merged[-2:] == ["伏笔：把关键物件写成随手一笔", "伏笔：疑问当章提出、当章就给半个答案"]
    assert merge_planning(["开场：一", "伏笔：旧的"], lines[:1], cap=10) == ["开场：一", "伏笔：把关键物件写成随手一笔"]


def test_the_planning_block_tells_how_the_author_plants_and_pays_off() -> None:
    from novel_system.services.style_reference.structure_render import render_structure_card_parts

    card = {
        "chapter_count": 60,
        "has_chapter_markers": True,
        "chapter_chars": {"median": 18000, "p10": 9000, "p90": 33000},
        "foreshadowing": {
            "chapters_read": 60, "planted": 240, "payoffs": 190, "pairs": 170, "paid_share": 0.7,
            "distance": {"median": 3, "p10": 0, "p90": 18},
            "same_chapter_share": 0.25, "long_range_share": 0.15,
            "plant_kinds": {"悬念提问": 0.4, "物件": 0.2, "台词": 0.2, "人物细节": 0.1, "反常举动": 0.1},
            "payoff_styles": {"揭晓真相": 0.5, "反转": 0.3, "物件再现": 0.2},
            "open_per_chapter": {"median": 6, "p10": 2, "p90": 11},
        },
    }
    block, _samples = render_structure_card_parts({"structure_card": card}, include_samples=False)
    assert (
        "- 伏笔（通读 60 章）：埋下 240 处、回收 70%；从埋到收中位隔 3 章（p10 0 · p90 18），同一章就收的 25%、隔十章以上的 15%"
        in block
    )
    assert "- 伏笔种类：悬念提问 40%、物件 20%、台词 20%、人物细节 10%；回收方式：揭晓真相 50%、反转 30%、物件再现 20%" in block
    assert "- 同时悬着的伏笔：每章末约 6 处（p90 11）" in block


def test_full_learn_pairs_plants_with_payoffs_and_turns_them_into_planning_lines(session, monkeypatch) -> None:
    seed(session)
    fake, job = _learn(monkeypatch, mode="full")
    assert job.state == "succeeded", job.error_json
    assert job.cursor_json["phases_done"] == list(learn_job.PHASE_ORDER)
    matches_calls = [p for node, p in fake.payloads if node == NODE_SYNTH and "plants" in p]
    assert len(matches_calls) == 1 and len(matches_calls[0]["plants"]) == 6 and len(matches_calls[0]["payoffs"]) == 5
    craft_calls = [p for node, p in fake.payloads if node == NODE_SYNTH and "pairs" in p]
    assert len(craft_calls) == 1 and craft_calls[0]["previous_lines"] == []
    assert all(pair["distance"] == 1 for pair in craft_calls[0]["pairs"])
    pj = _profile(job.result_json["profile_id"]).profile_json
    stats = pj["structure_card"]["foreshadowing"]
    assert stats["planted"] == 6 and stats["pairs"] == 5 and stats["paid_share"] == 0.833
    assert stats["distance"]["median"] == 1 and stats["open_per_chapter"]["median"] == 1
    assert pj["planning_guidance"][-1] == "伏笔：伏笔多半藏在一句玩笑里，下一章就让它变成正事"
    assert [line["text"] for line in pj["foreshadow_lines"]] == ["伏笔多半藏在一句玩笑里，下一章就让它变成正事"]
    assert job.result_json["foreshadow"] == {"pairs": 5, "lines": 1, "reused_matches": False}
    # 情节梗概不进画像
    assert "留下的疑问" not in str(pj)
    # 文风卡合成看得到伏笔统计
    card_payload = next(p for node, p in fake.payloads if node == NODE_SYNTH and _is_card(p))
    assert any(fact.startswith("伏笔（通读") for fact in card_payload["structure_facts"])
    run = session.get(StyleReferenceRun, job.result_json["run_id"])
    assert len(run.coverage_json["full"]["foreshadow"]["matches"]) == 5

    # 再学一次:配对沿用(不再调配对),手法在上一版上修订(原句原 id)
    fake2, second = _learn(monkeypatch, mode="full")
    assert second.state == "succeeded", second.error_json
    assert not any("plants" in p for node, p in fake2.payloads if node == NODE_SYNTH)
    craft2 = next(p for node, p in fake2.payloads if node == NODE_SYNTH and "pairs" in p)
    assert [line["id"] for line in craft2["previous_lines"]] == [pj["foreshadow_lines"][0]["line_id"]]
    pj2 = _profile(second.result_json["profile_id"]).profile_json
    assert pj2["foreshadow_lines"][0]["line_id"] == pj["foreshadow_lines"][0]["line_id"]
    assert pj2["foreshadow_lines"][0]["source"] == "kept" and second.result_json["foreshadow"]["reused_matches"] is True


def test_a_craft_line_naming_the_books_own_people_never_reaches_the_profile(session, monkeypatch) -> None:
    seed(session)

    def script(node, payload, _call_no):
        if node == NODE_SYNTH and "pairs" in payload:
            return {"lines": [{"text": "林昭的谜团总在下一章解开", "refs": [payload["pairs"][0]["id"]], "keep": ""}]}
        return None

    fake, job = _learn(monkeypatch, FakeLearnLLM(script=script, protected=("林昭",)), mode="full")
    assert job.state == "succeeded", job.error_json
    pj = _profile(job.result_json["profile_id"]).profile_json
    assert pj["foreshadow_lines"] == [] and not any(line.startswith("伏笔：") for line in pj["planning_guidance"])
