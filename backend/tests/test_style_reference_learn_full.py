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
    merges = [p for p in synth_payloads if "findings" in p]
    cards = [p for p in synth_payloads if "findings" not in p]
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
    assert len(synth) == 1 and "findings" not in synth[0]
    assert second.cursor_json["full"]["reused_reading"] and second.cursor_json["full"]["reused_consolidation"]
    # 修订:上一版的句子都带去了,假模型 keep 了每维的 do → 原句、原 id,✓ 跟着走
    previous_ids = {line["id"] for entry in synth[0]["previous_card"] for line in entry["lines"]}
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
    assert fake3.count(EXTRACT) == 12 and sum(1 for n, p in fake3.payloads if n == NODE_SYNTH and "findings" in p) == 16


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
        if crash_node == NODE_SYNTH and "findings" not in payload:
            return None
        state["seen"] += 1
        if state["seen"] == 2:
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
        merged_again = {p["dimension"]["key"] for n, p in after if n == NODE_SYNTH and "findings" in p}
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
