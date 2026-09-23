"""「学习文风」作业（2026-09-23 风格参考 v3：作业表 kind=learn，台账 N9 / U11 / I12 / E2 / E5 / E6 / E7 / E10 / N1 / N3）。

钉住：
- 七步（窗口 → 选窗 → 四层抽取 → 文风卡 → 本书专名 → 窗口标签 → 画像）都走、游标记录每一步；四层读**同一组**窗口；
- 抽取证据逐字核对（伪造 / 段号错且不唯一的引文丢掉，引文坐标是库里原文的坐标），重试一次并**合并**两次的有效发现；
- 文风卡：每句带依据的发现与证据引文；含本书专名 / 与原书 ≥12 字连续重合 / 带统计数字 / 没有依据的句子丢掉；
  ≤11 字的作者原话可以留；
- 画像写 v3 键（文风卡、行状态、声音 + 作者自身分布、去掉 chapters 的结构画像、规划 / 叙事指引、learned_from、
  按证据计的引文数），不写 exemplar_windows / scene_samples_index / style_features；
- 重新学习就地更新同一份画像（profile_id 不变、version_tag +1、active、绑定不动），作者的 ✓ / ✗ 沿用；
- 受保护专名只替换 ``protected_auto`` 行，作者录入的行不动；
- 全书每个窗口都打上标签（分批、最多 3 批并行、对不上整批重试），gist 里的专名换成代称；
- 在每一步被杀之后续跑，已完成的步骤不再调模型；取消；丢了所有权（删书）之后不再写、不再调；
- 路由：各种 409 / 404、作业派发、活动清单、文风卡行状态。
"""

from __future__ import annotations

import random
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, update

from novel_system.db.models import (
    LlmCall,
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceQuote,
    StyleReferenceRun,
    StyleReferenceWindow,
    SystemConfigSnapshot,
    utcnow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference import learn_job, learn_tags
from novel_system.services.style_reference.card import card_from_profile_json
from novel_system.services.style_reference.card_states import set_card_line_state
from novel_system.services.style_reference.jobs import (
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    StyleJobService,
    register_job_handler,
    run_job_inline,
)
from tests.style_reference_factories import make_book
from novel_system.services.style_reference.tags import MOOD_TAGS, SITUATION_TAGS, TAGS_VERSION
from tests.learn_fakes import (
    NODE_PROTECTED,
    NODE_SYNTH,
    NODE_TAGS,
    FakeLearnLLM,
    SimulatedCrash,
    default_card,
    default_extract,
    paragraphs_of,
)

PREFIX = "/api/v2/style-reference"
NAMES = ("韩小暖", "程铁", "苏半夏")
PLACES = ("铁灰城", "雾港")
ORG = "雾港同盟"
PROTECTED = ("韩小暖", "程铁", "苏半夏", "铁灰城", ORG)
KINDS = {"铁灰城": "place", ORG: "organization"}
RIGHTS = {"rights_declaration": {"declared": True, "send_rights": True, "analysis_rights": True}}


def learn_rows(chapters: int = 6, per_chapter: int = 40, seed: str = "learn") -> list[dict]:
    """合成书：章题 + 对白 / 叙述 / 心理 / 动作四种段，每段带唯一的序号（段内引文不重复）。"""
    rng = random.Random(seed)
    rows: list[dict] = []
    serial = 0

    def add(text: str, ptype: str) -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    for chapter in range(1, chapters + 1):
        add(f"第{chapter}章 雨夜", "transition")
        for _ in range(per_chapter):
            serial += 1
            name = rng.choice(NAMES)
            place = rng.choice(PLACES)
            roll = rng.random()
            if roll < 0.4:
                add(f"“{name}，你真要去{place}吗？”对面的人把第{serial}盏灯拨暗了些，笑得有点勉强。", "dialogue")
            elif roll < 0.7:
                add(f"{name}在{place}的屋檐下站了很久，数到第{serial}滴雨才转身，心里骂了一句这鬼天气。", "narration")
            elif roll < 0.85:
                add(f"{name}想，这是第{serial}回了，{ORG}的人还是没来，这事多半要黄。", "psychology")
            else:
                add(f"{name}抓起外套冲出门，第{serial}步就踩进水坑，裤腿一直湿到膝盖。", "action")
    return rows


def seed_book(session, book_id: str = "learn_book", *, rows: list[dict] | None = None, cloud_policy: str = "allow_full_cloud") -> str:
    make_book(
        session,
        book_id,
        title="雨夜集",
        paragraphs=rows if rows is not None else learn_rows(),
        cloud_policy=cloud_policy,
        stats=dict(RIGHTS) if cloud_policy != "local_only" else {},
    )
    session.commit()
    return book_id


@pytest.fixture(autouse=True)
def _fast_learning(monkeypatch):
    """退避不等待、轮询快;每个用例重新登记处理器(作业框架的测试会临时换掉处理器)。"""
    monkeypatch.setattr(learn_job, "CALL_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(learn_job, "WAIT_POLL_SECONDS", 0.05)
    register_job_handler(JOB_KIND_LEARN, learn_job.run_learn_job)


def _use(monkeypatch, fake: FakeLearnLLM) -> FakeLearnLLM:
    monkeypatch.setattr(learn_job, "resolve_learn_client", lambda: (fake, True))
    return fake


def _fake(**kwargs) -> FakeLearnLLM:
    kwargs.setdefault("protected", PROTECTED)
    kwargs.setdefault("kinds", KINDS)
    return FakeLearnLLM(**kwargs)


def _start(book_id: str, **kwargs) -> str:
    with SessionLocal() as db:
        job = learn_job.start_learn_job(db, book_id, **kwargs)
        db.commit()
        return job.job_id


def _job(job_id: str) -> StyleReferenceJob:
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        db.expunge(job)
        return job


def _profile(profile_id: str) -> StyleReferenceProfile:
    with SessionLocal() as db:
        profile = db.get(StyleReferenceProfile, profile_id)
        db.expunge(profile)
        return profile


def _learn(monkeypatch, session, fake: FakeLearnLLM | None = None, book_id: str = "learn_book") -> tuple[FakeLearnLLM, StyleReferenceJob]:
    fake = _use(monkeypatch, fake or _fake())
    if session.get(StyleReferenceBook, book_id) is None:
        seed_book(session, book_id)
    job_id = _start(book_id)
    run_job_inline(job_id)
    return fake, _job(job_id)


# ---------------------------------------------------------------- happy path


def test_learn_job_runs_every_phase_and_writes_a_v3_profile(session, monkeypatch) -> None:
    fake, job = _learn(monkeypatch, session)
    assert job.state == "succeeded", job.error_json
    cursor = job.cursor_json
    assert cursor["phases_done"] == list(learn_job.PHASE_ORDER)
    # 调用:四层各一次、文风卡一次、专名一次、标签按批
    assert fake.count("style_ref_extract_") == 4
    assert fake.count(NODE_SYNTH) == 1 and fake.count(NODE_PROTECTED) == 1
    windows = session.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == "learn_book")).all()
    assert fake.count(NODE_TAGS) == len(learn_tags.plan_tag_batches([(w.window_no, w.chars) for w in windows]))

    result = job.result_json
    profile = _profile(result["profile_id"])
    assert result["profile_created"] is True and profile.status == "active" and profile.version_tag == "v1"
    pj = profile.profile_json
    assert pj["profile_version"] == "style_profile_v3"
    for key in ("exemplar_windows", "scene_samples_index", "style_features", "narrative_patterns", "banned_replication_rules", "calibration_guidance"):
        assert key not in pj
    card = card_from_profile_json(pj)
    assert card is not None and len(card.dimensions) == 16
    lines = card.all_lines()
    assert lines and all(line.evidence_quote_ids and line.finding_ids for _dim, line in lines)
    assert card.temperament == ["越危险越要开玩笑", "玩笑底下压着孤独"]
    measurable = {e.dimension: e.measurable_features for e in card.dimensions}
    assert "sent_len_mean" in pj["dimension_card"]["dimensions"][0]["measurable_features"] or any(measurable.values())
    assert any(line.mandatory for _dim, line in lines)
    assert pj["card_line_states"] == {}
    # 证据引文按证据计(台账 E10)
    assert all(v["quote_count"] > 0 for v in pj["sub_dimensions"].values())
    assert set(pj["sub_dimensions"]) == {entry.dimension for entry in card.dimensions}
    # 声音:绝对习惯句 + 作者自身分布;结构画像去掉 chapters
    assert pj["voice"]["habits"] and pj["voice"]["distribution"]["window_count"] == len(windows)
    # 过渡别名:旧读者(注入的声音习惯行、漂移、诊断)读 voice_signature,同源、不带分布
    assert pj["voice_signature"]["features"] == pj["voice"]["features"]
    assert pj["voice_signature"]["habits"] == pj["voice"]["habits"] and "distribution" not in pj["voice_signature"]
    assert "chapters" not in pj["structure_card"] and pj["structure_card"]["chapter_count"] == 6
    assert pj["planning_guidance"] == ["开场：先抛一句闲话再进正事", "收场：落在一个具体的小动作上"]
    assert pj["narrative_guidance"] and all("避免：" in l or not l.startswith("不") for l in pj["narrative_guidance"])
    learned = pj["learned_from"]
    assert learned["types_revision"] == 0 and learned["root"] == windows[0].root_sha256
    assert learned["index_version"] == windows[0].index_version and learned["kernel_version"]
    assert learned["card_version"] == "dimension_card_v1" and learned["job_id"] == job.job_id
    assert learned["extraction"]["windows"] == sorted(w.window_no for w in windows)
    assert pj["protected_terms_version"].startswith("pt_v1_")
    assert pj["reference_basis"]["book_id"] == "learn_book"
    assert pj["metrics_baseline"] == {}  # 测试书的 stats 没有导入期指标
    # 血缘 run:done、只作血缘
    run = session.get(StyleReferenceRun, result["run_id"])
    assert run.status == "done" and run.dispatch_state == "learn_job" and profile.run_id == run.run_id
    assert run.coverage_json["learn_job_id"] == job.job_id and run.coverage_json["sub_dimensions"]
    # 受保护专名
    terms = session.scalars(select(StyleReferenceBannedTerm).where(StyleReferenceBannedTerm.profile_id == profile.profile_id)).all()
    assert {t.term for t in terms} == set(PROTECTED)
    assert all(t.source == "protected_auto" and t.scope == "generation" for t in terms)
    assert {t.term: t.replacement_hint for t in terms}["铁灰城"] == "用本书自己的地点"
    # 标签:每个窗口都有
    assert all(w.tags_version == TAGS_VERSION and w.tags_json["situations"] == ["日常闲谈"] for w in windows)
    assert result["windows"]["tagged"] == len(windows) == result["windows"]["index"]
    # 结果:计数 / 调用 / 耗时
    assert result["findings"]["total"] == 16 * 3 and result["findings"]["avoid"] == 16
    assert result["llm"]["calls"] == len(fake.calls)
    assert set(result["seconds"]["by_phase"]) == set(learn_job.PHASE_ORDER)
    assert result["card"]["lines"] == len(lines)
    # 记账:step 记作业 / 步骤
    steps = session.scalars(select(LlmCall.step)).all()
    assert all(step.startswith(f"learn:{job.job_id}:") for step in steps)
    assert f"learn:{job.job_id}:extract:language:1" in steps
    # 进度:走到最后一步
    assert job.progress_json["done"] == job.progress_json["total"]


def test_every_layer_reads_the_same_window_set_with_paragraph_markers(session, monkeypatch) -> None:
    fake, job = _learn(monkeypatch, session)
    assert job.state == "succeeded"
    extract_payloads = [payload for node, payload in fake.payloads if node.startswith("style_ref_extract_")]
    assert len(extract_payloads) == 4
    windows = [p["windows"] for p in extract_payloads]
    assert all(w == windows[0] for w in windows)
    assert {p["layer"] for p in extract_payloads} == {"language", "narrative", "scene", "theme"}
    paras = paragraphs_of(extract_payloads[0])
    assert [n for n, _t in paras] == list(range(1, len(paras) + 1))
    # 章题不进样本
    assert not any(text.startswith("第") and "章" in text[:4] for _n, text in paras)


def test_fabricated_quotes_are_dropped_and_spans_point_at_the_raw_text(session, monkeypatch) -> None:
    def script(node, payload, _call_no):
        if node != "style_ref_extract_language":
            return None
        output = default_extract(payload)
        first = output["dimensions"][0]["observations"][0]
        first["evidence"][0]["quote"] = "这句话书里根本没有出现过"  # 伪造
        # 段号错,但引文(带唯一序号的一小段)在整组样本里只出现一次 → 反查对上
        _number, text = next((n, t) for n, t in paragraphs_of(payload) if "滴雨" in t)
        unique = text[text.index("第") : text.index("滴雨") + 2]
        output["dimensions"][0]["observations"][1]["evidence"][1] = {"p": 999999, "quote": unique}
        return output

    fake, job = _learn(monkeypatch, session, _fake(script=script))
    assert job.state == "succeeded"
    language = session.scalars(
        select(StyleReferenceFinding).where(StyleReferenceFinding.sub_dimension == "language.sentence_structure")
    ).all()
    statements = {f.statement for f in language}
    assert "句式结构上作者的第一种做法" not in statements  # 伪造的那条只剩一处证据 → 丢
    assert "句式结构上作者的第二种做法" in statements  # 段号错但引文在样本里唯一 → 反查对上
    for quote in session.scalars(select(StyleReferenceQuote)).all():
        paragraph = session.get(StyleReferenceParagraph, quote.paragraph_id)
        assert paragraph.text[quote.span_start : quote.span_end] == quote.quote_text
        assert quote.illustrates_dims and quote.extracted_features["anchor_kind"] == "paragraph_quote"
    assert job.cursor_json["layers_done"]["language"]["rejected"] >= 1


def test_retry_merges_the_valid_findings_of_both_attempts(session, monkeypatch) -> None:
    """第一次只有前两维合格 → 重试(带问题清单) → 第二次补齐后两维;两次的有效发现合并(台账 E5)。"""
    calls = {"n": 0}

    def script(node, payload, _call_no):
        if node != "style_ref_extract_scene":
            return None
        calls["n"] += 1
        output = default_extract(payload, tag="甲" if calls["n"] == 1 else "乙")
        if calls["n"] == 1:
            for entry in output["dimensions"][2:]:
                entry["observations"] = []
        return output

    fake, job = _learn(monkeypatch, session, _fake(script=script))
    assert job.state == "succeeded"
    assert fake.count("style_ref_extract_scene") == 2
    retried = [flag for node, flag in fake.extra_instructions if node == "style_ref_extract_scene"]
    assert retried == [False, True]
    scene = session.scalars(select(StyleReferenceFinding).where(StyleReferenceFinding.sub_dimension.like("scene.%"))).all()
    statements = {f.statement for f in scene}
    # 前两维:第一次的发现保留,第二次同维的新发现也合并进来(每维观察 ≤5)
    assert "环境描写上作者的第一种做法甲" in statements and "环境描写上作者的第一种做法乙" in statements
    # 后两维:只有第二次的
    assert "对话写法上作者的第一种做法乙" in statements and "对话写法上作者的第一种做法甲" not in statements
    extraction = session.scalars(
        select(StyleReferenceExtraction).where(StyleReferenceExtraction.sub_dimension == "scene.dialogue")
    ).one()
    assert extraction.purpose == "full_retry" and extraction.raw_payload_json["attempts"] == 2
    assert job.cursor_json["layers_done"]["scene"]["attempts"] == 2


def test_a_failing_layer_call_is_retried_once_then_fails_the_job_resumably(session, monkeypatch) -> None:
    def script(node, _payload, _call_no):
        return "fail" if node == "style_ref_extract_theme" else None

    fake, job = _learn(monkeypatch, session, _fake(script=script))
    assert job.state == "failed"
    assert job.error_json["code"] == "STYLE_REFERENCE_LEARN_FAILED"
    assert job.error_json["details"]["reason_code"] == "extract_failed" and job.error_json["retryable"] is True
    assert fake.count("style_ref_extract_theme") == 2
    assert set(job.cursor_json["layers_done"]) == {"language", "narrative", "scene"}
    run = session.get(StyleReferenceRun, job.cursor_json["run_id"])
    session.refresh(run)
    assert run.status == "failed"

    healthy = _use(monkeypatch, _fake())
    with SessionLocal() as db:
        resumed = learn_job.start_learn_job(db, "learn_book", resume=True)
        db.commit()
        assert resumed.job_id == job.job_id
    run_job_inline(job.job_id)
    done = _job(job.job_id)
    assert done.state == "succeeded" and done.attempt == 2
    # 续跑只补主题层,已完成的三层不再调
    assert healthy.count("style_ref_extract_") == 1 and healthy.calls[0] == "style_ref_extract_theme"


# ---------------------------------------------------------------- card assembly / filtering


def test_card_lines_with_protected_terms_overlap_numbers_or_no_refs_are_dropped(session, monkeypatch) -> None:
    book_rows = learn_rows()
    narration = next(row["text"] for row in book_rows if row["paragraph_type"] == "narration")
    long_excerpt = narration[narration.index("屋檐下") : narration.index("才转身")]  # 与原书 ≥12 字连续重合
    short_excerpt = "心里骂了一句这鬼天气"  # 10 字的作者原话
    assert len(long_excerpt.replace("，", "")) >= 12

    def script(node, payload, _call_no):
        if node != NODE_SYNTH:
            return None
        card = default_card(payload)
        first = card["dimensions"][0]
        ref = first["do"][0]["refs"][0]
        first["do"] = [
            {"text": "像韩小暖那样开口就骂天气", "refs": [ref], "mandatory": True, "distinctiveness": 0.9},
            {"text": f"照原书写「{long_excerpt}」", "refs": [ref], "mandatory": False, "distinctiveness": 0.8},
            {"text": f"用作者的原话「{short_excerpt}」起句", "refs": [ref], "mandatory": False, "distinctiveness": 0.7},
        ]
        second = card["dimensions"][1]
        ref2 = second["do"][0]["refs"][0]
        second["do"] = [
            {"text": "平均句长保持在28字左右", "refs": [ref2], "mandatory": False, "distinctiveness": 0.5},
            {"text": "没有依据的一句", "refs": ["f9999"], "mandatory": False, "distinctiveness": 0.5},
            {"text": "对白里夹一句自嘲，大约每十句一次", "refs": [ref2], "mandatory": False, "distinctiveness": 0.6},
        ]
        card["temperament"].append("像程铁一样嘴硬")
        return card

    fake = _fake(script=script)
    _use(monkeypatch, fake)
    seed_book(session, rows=book_rows)
    job_id = _start("learn_book")
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded", job.error_json
    card = card_from_profile_json(_profile(job.result_json["profile_id"]).profile_json)
    texts = [line.text for _dim, line in card.all_lines()]
    assert not any("韩小暖" in t for t in texts)  # 专名
    assert not any(long_excerpt in t for t in texts)  # ≥12 字重合
    assert any(short_excerpt in t for t in texts)  # ≤11 字的作者原话可以留
    assert "平均句长保持在28字左右" not in texts  # 统计数字
    assert "没有依据的一句" not in texts
    assert "对白里夹一句自嘲，大约每十句一次" in texts
    assert "像程铁一样嘴硬" not in card.temperament
    dropped = job.result_json["card"]
    assert dropped["dropped"]["numbers"] == 1 and dropped["dropped"]["ungrounded"] == 1
    assert dropped["filtered_at_finalize"]["protected_term"] >= 2 and dropped["filtered_at_finalize"]["source_overlap"] == 1


def test_contradicting_punctuation_lines_keep_the_better_evidenced_one(session, monkeypatch) -> None:
    def script(node, payload, _call_no):
        if node != NODE_SYNTH:
            return None
        card = default_card(payload)
        punct = next(d for d in card["dimensions"] if d["dimension"] == "language.punctuation")
        refs = [d["do"][0]["refs"][0] for d in card["dimensions"][:3]]
        punct["do"] = [
            {"text": "逗号用得密，把动作切成一小截一小截", "refs": refs, "mandatory": False, "distinctiveness": 0.8},
            {"text": "逗号稀疏，一句话一口气说到底", "refs": refs[:1], "mandatory": False, "distinctiveness": 0.9},
        ]
        punct["avoid"] = [{"text": "不用分号，也少用破折号", "refs": refs[:1], "mandatory": False, "distinctiveness": 0.5}]
        return card

    fake, job = _learn(monkeypatch, session, _fake(script=script))
    assert job.state == "succeeded", job.error_json
    card = card_from_profile_json(_profile(job.result_json["profile_id"]).profile_json)
    texts = [line.text for _dim, line in card.all_lines()]
    assert "逗号用得密，把动作切成一小截一小截" in texts
    assert "逗号稀疏，一句话一口气说到底" not in texts
    assert "不用分号，也少用破折号" in texts  # 测试书确实几乎不用分号 / 破折号:与实测一致
    dropped = job.result_json["card"]["dropped"]
    # 测试书逗号密(每千字五十多个):「逗号稀疏」先被实测否掉
    assert dropped.get("contradicts_measure", 0) == 1 and dropped.get("contradiction", 0) == 0


# ---------------------------------------------------------------- re-learn / line states / protected rows


def test_relearn_updates_the_same_profile_in_place_and_keeps_line_states(session, monkeypatch) -> None:
    _fake1, first = _learn(monkeypatch, session)
    profile_id = first.result_json["profile_id"]
    card = card_from_profile_json(_profile(profile_id).profile_json)
    lines = [line for _dim, line in card.all_lines()]
    pinned, excluded = lines[0], lines[1]
    with SessionLocal() as db:
        set_card_line_state(db, profile_id, pinned.line_id, "pinned")
        set_card_line_state(db, profile_id, excluded.line_id, "excluded")
        db.add(
            StyleReferenceInjectionBinding(
                binding_id="bind_relearn",
                profile_id=profile_id,
                scope="project",
                scope_ref_id="proj_1",
                task_type="scene_generation",
                strategy="mixed",
                config_json={},
                status="active",
            )
        )
        db.commit()

    # 第二次学习:合成不再写出被 ✓ 的那一句 → 从旧卡搬回;✗ 的状态保留
    def script(node, payload, _call_no):
        if node != NODE_SYNTH:
            return None
        card_out = default_card(payload)
        for entry in card_out["dimensions"]:
            entry["do"] = [line for line in entry["do"] if line["text"] != pinned.text]
        return card_out

    _use(monkeypatch, _fake(script=script))
    second_id = _start("learn_book")
    run_job_inline(second_id)
    second = _job(second_id)
    assert second.state == "succeeded", second.error_json
    assert second.result_json["profile_id"] == profile_id and second.result_json["profile_created"] is False
    profile = _profile(profile_id)
    assert profile.version_tag == "v2" and profile.status == "active"
    assert profile.profile_json["card_line_states"] == {pinned.line_id: "pinned", excluded.line_id: "excluded"}
    new_card = card_from_profile_json(profile.profile_json)
    carried = [line for _dim, line in new_card.all_lines() if line.line_id == pinned.line_id]
    assert carried and carried[0].source == "pinned_carryover"
    assert second.result_json["card"]["carried_pins"] == 1
    assert session.get(StyleReferenceInjectionBinding, "bind_relearn") is not None
    assert profile.run_id == second.result_json["run_id"] != first.result_json["run_id"]
    assert session.scalar(select(StyleReferenceProfile.profile_id).where(StyleReferenceProfile.book_id == "learn_book").where(StyleReferenceProfile.profile_id != profile_id)) is None


def test_protected_terms_replace_only_auto_rows(session, monkeypatch) -> None:
    _fake1, first = _learn(monkeypatch, session)
    profile_id = first.result_json["profile_id"]
    with SessionLocal() as db:
        db.add(
            StyleReferenceBannedTerm(
                term_id="sr_term_author",
                profile_id=profile_id,
                term="韩小暖",
                replacement_hint=None,
                source="user",
                scope="generation",
            )
        )
        # 作者录入的「韩小暖」:先删掉同词的 auto 行,模拟作者早就录过
        db.execute(
            StyleReferenceBannedTerm.__table__.delete().where(
                (StyleReferenceBannedTerm.profile_id == profile_id)
                & (StyleReferenceBannedTerm.term == "韩小暖")
                & (StyleReferenceBannedTerm.source == "protected_auto")
            )
        )
        db.commit()
    _use(monkeypatch, _fake(protected=("程铁", ORG)))
    second_id = _start("learn_book")
    run_job_inline(second_id)
    assert _job(second_id).state == "succeeded"
    rows = {(t.term, t.source) for t in session.scalars(select(StyleReferenceBannedTerm).where(StyleReferenceBannedTerm.profile_id == profile_id))}
    assert rows == {("韩小暖", "user"), ("程铁", "protected_auto"), (ORG, "protected_auto")}


def _author_term(profile_id: str, term: str, scope: str) -> None:
    with SessionLocal() as db:
        db.add(
            StyleReferenceBannedTerm(
                term_id=f"sr_term_{uuid.uuid4().hex[:8]}",
                profile_id=profile_id,
                term=term,
                replacement_hint=None,
                source="user",
                scope=scope,
            )
        )
        db.commit()


def test_author_banned_terms_filter_the_card_and_an_emptied_card_leaves_the_profile_alone(session, monkeypatch) -> None:
    """作者给这份画像录的禁用词(任何域)同样过滤卡片;全被过滤掉时作业失败、画像(就地更新的对象)原样不动。"""
    _fake1, first = _learn(monkeypatch, session)
    profile_id = first.result_json["profile_id"]
    _author_term(profile_id, "第一条", "extraction")
    _use(monkeypatch, _fake())
    second_id = _start("learn_book")
    run_job_inline(second_id)
    second = _job(second_id)
    assert second.state == "succeeded", second.error_json
    texts = [line.text for _dim, line in card_from_profile_json(_profile(profile_id).profile_json).all_lines()]
    assert texts and not any("第一条" in t for t in texts) and any("第二条" in t for t in texts)
    assert second.result_json["card"]["filtered_at_finalize"]["protected_term"] >= 1
    before = _profile(profile_id)

    _author_term(profile_id, "第二条", "generation")
    _author_term(profile_id, "通用写法", "generation")
    _use(monkeypatch, _fake())
    third_id = _start("learn_book")
    run_job_inline(third_id)
    third = _job(third_id)
    assert third.state == "failed"
    assert third.error_json["code"] == "STYLE_REFERENCE_LEARN_FAILED"
    details = third.error_json.get("details") or {}
    assert details["reason_code"] == "card_filtered_empty" and details["retryable"] is False
    assert details["author_action"]["action"] == "review_banned_terms"
    after = _profile(profile_id)
    assert after.version_tag == before.version_tag and after.profile_json == before.profile_json
    assert after.status == "active"


# ---------------------------------------------------------------- tags


def test_every_window_is_tagged_in_parallel_batches_and_gists_are_masked(session, monkeypatch) -> None:
    monkeypatch.setattr(learn_tags, "TAG_BATCH_WINDOWS", 1)
    fake, job = _learn(monkeypatch, session, _fake(gist_name="韩小暖", delay=0.05))
    assert job.state == "succeeded"
    windows = session.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == "learn_book")).all()
    assert fake.count(NODE_TAGS) == len(windows) and len(windows) >= 4
    assert fake.max_inflight[NODE_TAGS] <= learn_job.PARALLEL_CALLS
    for window in windows:
        tags = window.tags_json
        assert set(tags["situations"]) <= set(SITUATION_TAGS) and tags["situations"] == ["日常闲谈"]
        assert set(tags["moods"]) <= set(MOOD_TAGS)
        assert tags["gist"] == "某人在院子里说话"  # 专名换成代称
        assert tags["devices"] and tags["devices"][0] in job.cursor_json["tags"]["devices"]
    tag_payload = next(payload for node, payload in fake.payloads if node == NODE_TAGS)
    assert tag_payload["situation_vocabulary"] == list(SITUATION_TAGS)


def test_a_mismatched_tag_batch_is_retried(session, monkeypatch) -> None:
    once = {"done": False}

    def script(node, payload, _call_no):
        if node == NODE_TAGS and not once["done"]:
            once["done"] = True
            return {"windows": []}  # 一窗都没给
        return None

    fake, job = _learn(monkeypatch, session, _fake(script=script))
    assert job.state == "succeeded"
    assert job.cursor_json["retries"] >= 1
    windows = session.scalars(select(StyleReferenceWindow).where(StyleReferenceWindow.book_id == "learn_book")).all()
    assert all(w.tags_json for w in windows)


# ---------------------------------------------------------------- restart / cancel / ownership


def _stale(job_id: str) -> None:
    with SessionLocal() as db:
        old = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
        db.execute(update(StyleReferenceJob).where(StyleReferenceJob.job_id == job_id).values(heartbeat_at=old))
        db.commit()


@pytest.mark.parametrize(
    "crash_at",
    ["windows", "select", "extract", "synthesize", "protected", "tags", "finalize"],
)
def test_resume_after_a_crash_in_each_phase_repeats_no_finished_llm_work(session, monkeypatch, crash_at: str) -> None:
    seed_book(session)
    crashed = {"done": False}
    if crash_at in ("windows", "select", "finalize"):
        target = {
            "windows": "ensure_window_index",
            "select": "select_extraction_windows",
            "finalize": "replace_protected_terms",
        }[crash_at]
        original = getattr(learn_job, target)

        def crashing(*args, **kwargs):
            if not crashed["done"]:
                crashed["done"] = True
                raise SimulatedCrash(target)
            return original(*args, **kwargs)

        monkeypatch.setattr(learn_job, target, crashing)
        fake = _use(monkeypatch, _fake())
    else:
        node = {
            "extract": "style_ref_extract_narrative",
            "synthesize": NODE_SYNTH,
            "protected": NODE_PROTECTED,
            "tags": NODE_TAGS,
        }[crash_at]

        def script(called, _payload, _call_no):
            if called == node and not crashed["done"]:
                crashed["done"] = True
                return "crash"
            return None

        fake = _use(monkeypatch, _fake(script=script))
    job_id = _start("learn_book")
    with pytest.raises(SimulatedCrash):
        run_job_inline(job_id)
    stuck = _job(job_id)
    assert stuck.state == "running"  # 工人「死了」,作业还挂在 running
    before = list(fake.calls)
    _stale(job_id)
    with SessionLocal() as db:
        assert job_id in StyleJobService(db).sweep()
        db.commit()
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded", job.error_json
    assert job.attempt == 2
    after = fake.calls[len(before):]
    done_before = set(stuck.cursor_json.get("phases_done") or [])
    if "extract" in done_before:
        assert not any(call.startswith("style_ref_extract_") for call in after)
    if "synthesize" in done_before:
        assert NODE_SYNTH not in after
    if "protected" in done_before:
        assert NODE_PROTECTED not in after
    layers_before = set((stuck.cursor_json.get("layers_done") or {}))
    for layer in layers_before:
        assert f"style_ref_extract_{layer}" not in after
    # 同一份画像、findings 没有重复落库
    profiles = session.scalars(select(StyleReferenceProfile).where(StyleReferenceProfile.book_id == "learn_book")).all()
    assert len(profiles) == 1
    findings = session.scalars(select(StyleReferenceFinding).where(StyleReferenceFinding.run_id == job.result_json["run_id"])).all()
    assert len(findings) == 16 * 3


def test_cancel_during_a_call_stops_at_the_next_checkpoint_and_resume_finishes(session, monkeypatch) -> None:
    seed_book(session)
    fake = _use(monkeypatch, _fake(gate_node=NODE_SYNTH))
    job_id = _start("learn_book")
    worker = threading.Thread(target=run_job_inline, args=(job_id,))
    worker.start()
    assert fake.entered.wait(timeout=30)
    with SessionLocal() as db:
        job = learn_job.cancel_learn(db, "learn_book")
        db.commit()
        assert job is not None and job.cancel_requested == 1
    worker.join(timeout=30)
    fake.gate.set()
    job = _job(job_id)
    assert job.state == "cancelled"
    assert "synthesize" not in job.cursor_json["phases_done"]
    run = session.get(StyleReferenceRun, job.cursor_json["run_id"])
    session.refresh(run)
    assert run.status == "cancelled"

    healthy = _use(monkeypatch, _fake())
    _start("learn_book", resume=True)
    run_job_inline(job_id)
    assert _job(job_id).state == "succeeded"
    assert healthy.count("style_ref_extract_") == 0 and healthy.count(NODE_SYNTH) == 1


def test_deleting_the_book_mid_job_stops_the_worker_without_writes(session, monkeypatch) -> None:
    seed_book(session)
    fake = _use(monkeypatch, _fake(gate_node=NODE_PROTECTED))
    job_id = _start("learn_book")
    worker = threading.Thread(target=run_job_inline, args=(job_id,))
    worker.start()
    assert fake.entered.wait(timeout=30)
    from novel_system.services.style_reference.cleanup import purge_derived_data

    with SessionLocal() as db:
        StyleJobService(db).cancel_all_for_book("learn_book")
        purge_derived_data(db, "learn_book")
        db.commit()
    fake.gate.set()
    worker.join(timeout=30)
    assert not worker.is_alive()
    calls_after = list(fake.calls)
    assert NODE_TAGS not in calls_after  # 丢了所有权之后不再调
    assert session.scalar(select(StyleReferenceProfile.profile_id).where(StyleReferenceProfile.book_id == "learn_book")) is None


def test_job_without_llm_fails_with_llm_required(session, monkeypatch) -> None:
    seed_book(session)
    monkeypatch.setattr(learn_job, "resolve_learn_client", lambda: (None, False))
    job_id = _start("learn_book")
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "failed" and job.error_json["code"] == "STYLE_REFERENCE_LLM_REQUIRED"


# ---------------------------------------------------------------- start checks


def test_start_checks_book_state_active_jobs_and_policy(session, monkeypatch) -> None:
    seed_book(session)
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "missing_book")
    assert excinfo.value.status_code == 404

    session.execute(update(StyleReferenceBook).where(StyleReferenceBook.book_id == "learn_book").values(status="ingesting"))
    session.commit()
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book")
    assert excinfo.value.code == "STYLE_REFERENCE_BOOK_NOT_READY"
    session.execute(update(StyleReferenceBook).where(StyleReferenceBook.book_id == "learn_book").values(status="ready"))
    session.commit()

    # 就地重标段落类型:书是 ready,但作业表里有活动的分类作业
    classify = StyleJobService(session).create(JOB_KIND_CLASSIFY, book_id="learn_book", params={"mode": "retype"})
    session.commit()
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book")
    assert excinfo.value.code == "STYLE_REFERENCE_BOOK_CLASSIFYING"
    StyleJobService(session).request_cancel(classify.job_id)
    session.commit()

    first = learn_job.start_learn_job(session, "learn_book")
    session.commit()
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book")
    assert excinfo.value.code == "STYLE_REFERENCE_LEARN_ALREADY_ACTIVE"
    assert excinfo.value.details["job_id"] == first.job_id

    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book_x")
    assert excinfo.value.status_code == 404


def _activate_snapshot(session, category: str, parsed: dict) -> None:
    """直接写一份活动配置快照(模拟在这次发布之前就在系统配置里保存过 models / prompts 的安装)。"""
    session.add(
        SystemConfigSnapshot(
            snapshot_id=f"config_{category}_{uuid.uuid4().hex[:12]}",
            category=category,
            version=1,
            yaml_raw=yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False),
            parsed_json=parsed,
            validation_json={"ok": True, "message": f"{category} config is valid"},
            status="active",
            active_flag=1,
            activated_at=utcnow(),
            created_by="legacy-install",
        )
    )
    session.commit()


def test_start_refuses_missing_routes_and_stale_templates_before_creating_a_job(session, monkeypatch) -> None:
    """保存过 models 快照(没有两个新节点)/ prompts 快照(抽取还是旧 schema)的安装:建作业前就 409,指到系统配置。"""
    seed_book(session)
    _use(monkeypatch, _fake())
    config_dir = Path(__file__).resolve().parents[2] / "config"
    models = yaml.safe_load((config_dir / "models.yaml").read_text(encoding="utf-8"))
    for key in ("style_ref_protected_terms", "style_ref_tag_windows"):
        models["task_routing"].pop(key)
        (models.get("node_routing") or {}).pop(key, None)
    _activate_snapshot(session, "models", models)
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book")
    assert excinfo.value.code == "STYLE_REFERENCE_LEARN_CONFIG_MISSING" and excinfo.value.status_code == 409
    assert excinfo.value.details["missing_routes"] == ["style_ref_protected_terms", "style_ref_tag_windows"]
    assert excinfo.value.details["author_action"]["view"] == "systemConfig"
    assert session.scalars(select(StyleReferenceJob)).first() is None

    session.execute(update(SystemConfigSnapshot).values(active_flag=0, status="superseded"))
    session.commit()
    prompts = yaml.safe_load((config_dir / "prompts.yaml").read_text(encoding="utf-8"))
    old_scene = dict(prompts["templates"]["style_ref_extract_scene"])
    old_scene["version"] = "2026-08-19.v5"
    old_scene["structured_schema"] = {
        "type": "object",
        "required": ["observations"],
        "properties": {"observations": {"type": "array", "items": {"type": "object"}}, "forbidden_patterns": {"type": "array"}},
    }
    prompts["templates"]["style_ref_extract_scene"] = old_scene
    _activate_snapshot(session, "prompts", prompts)
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book")
    assert excinfo.value.code == "STYLE_REFERENCE_LEARN_CONFIG_MISSING"
    assert excinfo.value.details["stale_templates"] == ["style_ref_extract_scene"]
    assert "sync_prompt_templates" in excinfo.value.message
    assert session.scalars(select(StyleReferenceJob)).first() is None

    # 作业已建好之后才换成旧模板:开工时再查一次,不发任何调用
    session.execute(update(SystemConfigSnapshot).values(active_flag=0, status="superseded"))
    session.commit()
    fake = _use(monkeypatch, _fake())
    job_id = _start("learn_book")
    _activate_snapshot(session, "prompts", prompts)
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "failed" and job.error_json["code"] == "STYLE_REFERENCE_LEARN_CONFIG_MISSING"
    assert fake.calls == []


def test_local_only_book_needs_local_routes_for_every_learn_node(session, monkeypatch) -> None:
    seed_book(session, "local_book", cloud_policy="local_only")
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "local_book")
    assert excinfo.value.code == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert excinfo.value.details["node_id"] in learn_job.LEARN_NODE_IDS


def test_tiny_book_with_every_layer_assessed_skip_needs_force(session) -> None:
    seed_book(session)
    session.execute(
        update(StyleReferenceBook)
        .where(StyleReferenceBook.book_id == "learn_book")
        .values(stats_json={**RIGHTS, "input_assessment": {"language": "skip", "narrative": "skip", "scene": "skip", "theme": "skip"}})
    )
    session.commit()
    with pytest.raises(DomainError) as excinfo:
        learn_job.start_learn_job(session, "learn_book")
    assert excinfo.value.code == "STYLE_REFERENCE_INPUT_TOO_SMALL"
    job = learn_job.start_learn_job(session, "learn_book", force=True)
    assert job.params_json["skipped_layers"] == []


def test_skipped_layers_are_not_extracted(session, monkeypatch) -> None:
    seed_book(session)
    session.execute(
        update(StyleReferenceBook)
        .where(StyleReferenceBook.book_id == "learn_book")
        .values(stats_json={**RIGHTS, "input_assessment": {"theme": "skip"}})
    )
    session.commit()
    fake = _use(monkeypatch, _fake())
    job_id = _start("learn_book")
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded"
    assert fake.count("style_ref_extract_theme") == 0 and fake.count("style_ref_extract_") == 3
    assert job.cursor_json["layers"] == ["language", "narrative", "scene"]


def test_a_structure_card_that_cannot_be_computed_only_leaves_that_key_empty(session, monkeypatch) -> None:
    """结构画像是确定性派生:算不出来时画像只缺这个键(合成不带结构事实),学习照常完成。"""

    def broken(*_args, **_kwargs):
        raise ValueError("no chapters")

    monkeypatch.setattr(learn_job, "compute_structure_card", broken)
    fake, job = _learn(monkeypatch, session)
    assert job.state == "succeeded", job.error_json
    pj = _profile(job.result_json["profile_id"]).profile_json
    assert pj["structure_card"] is None and pj["dimension_card"]["dimensions"]
    synth_payload = next(payload for node, payload in fake.payloads if node == NODE_SYNTH)
    assert synth_payload["structure_facts"] == []


# ---------------------------------------------------------------- card line states


def test_card_line_states_are_atomic_and_never_draft_the_profile(session, monkeypatch) -> None:
    _fake1, job = _learn(monkeypatch, session)
    profile_id = job.result_json["profile_id"]
    line = card_from_profile_json(_profile(profile_id).profile_json).all_lines()[0][1]
    with SessionLocal() as db:
        out = set_card_line_state(db, profile_id, line.line_id, "excluded")
        db.commit()
    assert out["card_line_states"] == {line.line_id: "excluded"} and out["status"] == "active"
    with SessionLocal() as db:
        set_card_line_state(db, profile_id, line.line_id, "pinned")
        db.commit()
    assert _profile(profile_id).profile_json["card_line_states"] == {line.line_id: "pinned"}
    with SessionLocal() as db:
        set_card_line_state(db, profile_id, line.line_id, None)
        db.commit()
    profile = _profile(profile_id)
    assert profile.profile_json["card_line_states"] == {} and profile.status == "active"
    assert card_from_profile_json(profile.profile_json) is not None  # 卡本身不动

    with SessionLocal() as db:
        with pytest.raises(DomainError) as excinfo:
            set_card_line_state(db, profile_id, "cl_000000000000", "pinned")
        assert excinfo.value.code == "STYLE_REFERENCE_CARD_LINE_NOT_FOUND"
        with pytest.raises(DomainError) as excinfo:
            set_card_line_state(db, profile_id, line.line_id, "maybe")
        assert excinfo.value.status_code == 400
        with pytest.raises(DomainError) as excinfo:
            set_card_line_state(db, "sr_profile_missing", line.line_id, "pinned")
        assert excinfo.value.status_code == 404


# ---------------------------------------------------------------- routes / activity


def test_learn_routes_create_dispatch_cancel_and_report(client, session, monkeypatch) -> None:
    import novel_system.api.routes.style_reference as sr_routes

    seed_book(session)
    fake = _use(monkeypatch, _fake())
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    dispatched: list[str] = []
    monkeypatch.setattr(sr_routes, "dispatch_job", lambda job_id: dispatched.append(job_id))

    resp = client.post(f"{PREFIX}/books/learn_book/learn", json={}, headers={"X-Idempotency-Key": "learn-1"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "queued" and dispatched == [data["job_id"]]
    assert data["learn"]["phase_label"] == "排队中"

    again = client.post(f"{PREFIX}/books/learn_book/learn", json={}, headers={"X-Idempotency-Key": "learn-2"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "STYLE_REFERENCE_LEARN_ALREADY_ACTIVE"

    run_job_inline(data["job_id"])
    book = client.get(f"{PREFIX}/books/learn_book").json()["data"]["book"]
    assert book["learn"]["state"] == "succeeded" and book["learn"]["profile_id"]
    listed = client.get(f"{PREFIX}/books").json()["data"]["books"][0]
    assert listed["learn"]["job_id"] == data["job_id"]
    detail = client.get(f"{PREFIX}/books/learn_book/learn").json()["data"]
    assert detail["learn"]["result"]["profile_id"] == book["learn"]["profile_id"]
    assert detail["estimate"]["calls"]["extract"] == 4 and detail["estimate"]["calls"]["tags"] >= 1
    assert {route["node_id"] for route in detail["routes"]} == set(learn_job.LEARN_NODE_IDS)

    activity = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    entry = next(item for item in activity if item.get("job_id") == data["job_id"])
    assert entry["kind"] == "learn" and entry["kind_label"] == "学习文风" and entry["title"] == "雨夜集"
    assert entry["phases_done"] == list(learn_job.PHASE_ORDER)

    # 找发现与证据(矩阵读的就是这个)
    runs = client.get(f"{PREFIX}/books/learn_book/runs").json()["data"]["runs"]
    assert runs[0]["dispatch_state"] == "learn_job"
    findings = client.get(f"{PREFIX}/runs/{runs[0]['run_id']}/findings?include=evidence").json()["data"]["findings"]
    assert findings and all(len(f["evidence"]) >= 2 for f in findings)
    assert "base_confidence" not in findings[0] and "user_vote" not in findings[0]

    # 取消:没有活动作业 → 409
    cancel = client.post(f"{PREFIX}/books/learn_book/learn/cancel", json={}, headers={"X-Idempotency-Key": "cancel-1"})
    assert cancel.status_code == 409 and cancel.json()["error"]["code"] == "STYLE_REFERENCE_LEARN_NOT_ACTIVE"


def test_learn_route_requires_an_llm(client, session, monkeypatch) -> None:
    import novel_system.api.routes.style_reference as sr_routes

    seed_book(session)
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (None, False))
    resp = client.post(f"{PREFIX}/books/learn_book/learn", json={}, headers={"X-Idempotency-Key": "learn-x"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "STYLE_REFERENCE_LLM_REQUIRED"


def test_card_line_route_and_removed_routes(client, session, monkeypatch) -> None:
    _fake1, job = _learn(monkeypatch, session)
    profile_id = job.result_json["profile_id"]
    line = card_from_profile_json(_profile(profile_id).profile_json).all_lines()[0][1]
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/card-lines/{line.line_id}",
        json={"state": "excluded"},
        headers={"X-Idempotency-Key": "line-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["card_line_states"] == {line.line_id: "excluded"}
    assert _profile(profile_id).status == "active"
    missing = client.post(
        f"{PREFIX}/profiles/{profile_id}/card-lines/cl_ffffffffffff",
        json={"state": "pinned"},
        headers={"X-Idempotency-Key": "line-2"},
    )
    assert missing.status_code == 404
    # 旧学习链路的写接口都没了
    assert client.post(f"{PREFIX}/books/learn_book/runs", json={}, headers={"X-Idempotency-Key": "r"}).status_code in (404, 405)
    run_id = job.result_json["run_id"]
    assert client.post(f"{PREFIX}/runs/{run_id}/synthesize", json={}, headers={"X-Idempotency-Key": "s"}).status_code in (404, 405)
    finding_id = session.scalars(select(StyleReferenceFinding.finding_id)).first()
    for path in (f"findings/{finding_id}/review", f"findings/{finding_id}/user-feedback"):
        assert client.post(f"{PREFIX}/{path}", json={}, headers={"X-Idempotency-Key": path}).status_code in (404, 405)


def test_evidence_counts_are_consistent_with_the_rows(session, monkeypatch) -> None:
    _fake1, job = _learn(monkeypatch, session)
    pj = _profile(job.result_json["profile_id"]).profile_json
    evidences = session.scalars(select(StyleReferenceEvidence)).all()
    assert sum(v["quote_count"] for v in pj["sub_dimensions"].values()) == len(evidences)


def test_worker_shutdown_mid_learning_requeues_instead_of_failing(session, monkeypatch) -> None:
    """--reload / 停服时正在学习:作业回到 queued(不是 failed),下次启动接着学,做完的阶段不再调模型。"""
    from novel_system.services.style_reference import jobs as jobs_module

    seed_book(session)
    shut = {"done": False}

    def script(called, _payload, _call_no):
        if called == NODE_SYNTH and not shut["done"]:
            shut["done"] = True
            jobs_module.shutdown_job_workers()
        return None

    fake = _use(monkeypatch, _fake(script=script))
    job_id = _start("learn_book")
    run_job_inline(job_id)
    paused = _job(job_id)
    assert paused.state == "queued" and paused.error_json is None and paused.owner_token is None
    assert "extract" in set(paused.cursor_json.get("phases_done") or [])
    before = list(fake.calls)
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded", job.error_json
    assert job.attempt == 2
    assert not any(call.startswith("style_ref_extract_") for call in fake.calls[len(before):])


def test_a_protected_name_the_author_removed_is_not_brought_back_by_relearning(client, session, monkeypatch) -> None:
    """作者在画像里删掉一个自动识别的专名（多半是误收的日常词）：记在画像上，重新学习不再把它加回来。"""
    from novel_system.services.style_reference.protected_terms import DISMISSED_KEY

    seed_book(session)
    _use(monkeypatch, _fake(protected=("程铁", ORG)))
    first_id = _start("learn_book")
    run_job_inline(first_id)
    profile_id = _job(first_id).result_json["profile_id"]
    term_id = session.scalars(
        select(StyleReferenceBannedTerm.term_id).where(
            StyleReferenceBannedTerm.profile_id == profile_id, StyleReferenceBannedTerm.term == "程铁"
        )
    ).one()
    resp = client.delete(f"/api/v2/style-reference/banned-terms/{term_id}", headers={"X-Idempotency-Key": "dismiss-1"})
    assert resp.status_code == 200, resp.text
    session.expire_all()
    assert session.get(StyleReferenceProfile, profile_id).profile_json[DISMISSED_KEY] == ["程铁"]

    _use(monkeypatch, _fake(protected=("程铁", ORG)))
    second_id = _start("learn_book")
    run_job_inline(second_id)
    assert _job(second_id).state == "succeeded"
    session.expire_all()
    rows = {(t.term, t.source) for t in session.scalars(select(StyleReferenceBannedTerm).where(StyleReferenceBannedTerm.profile_id == profile_id))}
    assert rows == {(ORG, "protected_auto")}
    assert session.get(StyleReferenceProfile, profile_id).profile_json[DISMISSED_KEY] == ["程铁"]
