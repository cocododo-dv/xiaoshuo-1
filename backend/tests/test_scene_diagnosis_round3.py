"""场景诊断第三轮（2026-09-22，「重构修复优化」四个开放项）。

1. 局部深评看整场：焦点可以是一段范围；跨段的矛盾带 ``related``（另一段的原话钉到段）；焦点有交集的旧行退位。
2. 21 维规则按参考书校准：作者的常用词从词表里去掉（稿子里过量的仍提示），作者常态的维度降为提示；
   文学质量视图读同一份校准。
3. 章级通读只通读改过的场：按每场正文哈希判新旧，未改的场沿用上次的发现（``carried_from``），没改过不调模型。
4. 计数随写回传：作者稿保存 / 忽略保存 / 深评载荷都带 ``diagnosis_rollup``；``totals`` 由条目汇总。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    WriterEvaluation,
)
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.literary_quality import (
    RULE_DIMENSION_HABIT_SHARE,
    RULE_NEEDLE_HABIT_PER_10K,
    analyze_literary_quality,
    calibrated_lexicons,
)
from novel_system.services.scene_diagnosis import (
    BoundProfile,
    LITERARY_REVISION_RUBRIC_ID,
    SceneDiagnosisService,
    compute_reference_rules,
    passage_scope,
    rule_calibration_from_reference,
    summarize_counts,
)
from tests.test_scene_diagnosis import (
    CHAPTER_ID,
    PROJECT_ID,
    SCENE2_ID,
    SCENE_ID,
    _add_second_scene,
    _distinct_chars,
    _finding,
    _scripted_runner,
    _seed_scene,
)


# ---------------------------------------------------------------------------
# 2. 规则按参考书校准
# ---------------------------------------------------------------------------


def _reference_book(units: int = 20, paragraphs_per_unit: int = 120, habit: str = "突然意识到") -> list[str]:
    """一本合成的参考书：每个单元一个标题段 + 120 段（≈7,200 字，切成两个窗口 + 一个末尾）；
    每第三段用一次 ``habit``（每万字 ≈50 次），从不用「不知为何」。"""

    book: list[str] = []
    for unit in range(units):
        book.append(f"第{unit + 1}章 无题")
        for index in range(paragraphs_per_unit):
            offset = (unit * paragraphs_per_unit + index) * 7 % 1500
            body = "".join(chr(0x4E00 + (offset + step) % 2000) for step in range(60))
            if index % 3 == 0:
                body = body[:20] + habit + body[20:]
            book.append(body + "。")
    return book


def test_rule_calibration_waives_the_reference_authors_words_and_demotes_habitual_dimensions() -> None:
    stats = compute_reference_rules(_reference_book())
    assert stats["windows"] > 0 and stats["endings"] == 20 and stats["chars"] > 100_000
    assert stats["needle_rates"]["突然意识到"] >= RULE_NEEDLE_HABIT_PER_10K
    assert "不知为何" not in stats["needle_rates"]
    # 随手拼的字里没有抉择 / 代价词：这两条「缺席才是毛病」的规则在每个窗口上都响
    assert stats["dimension_shares"]["no_choice_scene"] >= RULE_DIMENSION_HABIT_SHARE
    assert stats["dimension_shares"]["painless_scene"] >= RULE_DIMENSION_HABIT_SHARE

    calibration = rule_calibration_from_reference(stats)
    assert calibration.active and calibration.source == "reference" and calibration.signature != "default"
    assert "突然意识到" in calibration.habitual_needles and "不知为何" not in calibration.habitual_needles
    assert {"no_choice_scene", "painless_scene"} <= calibration.habitual_dimensions
    assert calibration.as_dict()["habitual_needles"][0] == "突然意识到"

    # 词表：作者的常用词不再命中；不是常用词的照旧
    plain = "她突然意识到门开了。门外的人没有进来。"
    _, house = analyze_literary_quality(plain)
    assert any(item["dimension"] == "model_voice" and item["needle"] == "突然意识到" for item in house)
    _, calibrated = analyze_literary_quality(plain, calibration=calibration)
    assert not any(item["dimension"] == "model_voice" for item in calibrated)
    _, other = analyze_literary_quality("不知为何，门开了。门外的人没有进来。", calibration=calibration)
    assert any(item["dimension"] == "model_voice" and item["needle"] == "不知为何" for item in other)
    # 稿子里用得比参考作者还密得多（至少 3 次、4 倍以上）：那是过量，照提示
    overused = "她突然意识到门开了。他突然意识到她在看。她又突然意识到什么。"
    assert "突然意识到" in calibrated_lexicons(calibration, overused)["model_voice"]
    _, flagged = analyze_literary_quality(overused, calibration=calibration)
    assert any(item["dimension"] == "model_voice" and item["needle"] == "突然意识到" for item in flagged)

    # 维度：作者常态的规则降为提示，带 calibrated 说明；房风下仍是 revision
    _, house_absence = analyze_literary_quality(plain)
    assert next(item for item in house_absence if item["dimension"] == "no_choice_scene")["severity"] == "revision"
    demoted = next(item for item in calibrated if item["dimension"] == "no_choice_scene")
    assert demoted["severity"] == "info" and demoted["calibrated"]["kind"] == "dimension_habit"
    assert demoted["calibrated"]["share"] >= RULE_DIMENSION_HABIT_SHARE

    # 没有校准（房风）时词表原样、不加 calibrated
    assert calibrated_lexicons(None, plain)["model_voice"][0] == "suddenly realized"
    assert not any("calibrated" in item for item in house)


def _bind_reference_book(session, monkeypatch, *, paragraphs: list[str]) -> None:
    session.add(StyleReferenceBook(book_id="book_r3", title="龙族", source_kind="upload", cloud_policy="segments_only", text_checksum="r3"))
    session.add(StyleReferenceRun(run_id="run_r3", book_id="book_r3", status="completed", phase="synthesize", dispatch_state="completed", requested_layers_json=["language"]))
    session.add_all(
        [
            StyleReferenceParagraph(
                paragraph_id=f"para_r3_{index}",
                book_id="book_r3",
                paragraph_index=index,
                paragraph_type="narration",
                start_offset=0,
                end_offset=len(text),
                text=text,
                char_count=len(text),
            )
            for index, text in enumerate(paragraphs)
        ]
    )
    session.add(StyleReferenceProfile(profile_id="prof_r3", book_id="book_r3", run_id="run_r3", title="龙族画像", profile_json={"voice_signature": {"deliberate_repetition": False}}))
    session.commit()
    monkeypatch.setattr(
        SceneDiagnosisService,
        "binding_profile",
        lambda self, scene: (True, BoundProfile(profile_id="prof_r3", book_id="book_r3", deliberate_repetition=False)),
    )


def test_bound_scene_reads_the_reference_rule_calibration_and_the_quality_view_agrees(client: TestClient, session, monkeypatch) -> None:
    _seed_scene(session)  # 第 1 段有「突然意识到」——房风词表下是一条模型腔
    house = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert house["style_bound"] is False and house["craft_calibration"]["rules"] is None
    assert _finding(house, "rules", "model_voice")["evidence"]["excerpt"] == "突然意识到"

    _bind_reference_book(session, monkeypatch, paragraphs=_reference_book(units=12))
    bound = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert bound["style_bound"] is True
    rules = bound["craft_calibration"]["rules"]
    assert rules and "突然意识到" in rules["habitual_needles"] and "painless_scene" in rules["habitual_dimensions"]
    assert "常用词" in bound["craft_calibration"]["note"] and "只作提示" in bound["craft_calibration"]["note"]
    assert not [item for item in bound["findings"] if item["source"] == "rules" and item["dimension"] == "model_voice"], "参考作者的常用词不是毛病"
    absence = _finding(bound, "rules", "painless_scene")  # 夹具正文里有抉择词，没有代价词
    assert absence["severity"] == "info" and absence["calibrated"]["kind"] == "dimension_habit" and "只作提示" in absence["why"]
    assert absence["house_taste"] is True

    # 文学质量视图读同一份校准：这一场的条目不再有模型腔，并标出校准来源
    overview = client.get("/api/v1/literary-quality/overview", params={"project_id": PROJECT_ID}).json()["data"]
    item = next(entry for entry in overview["items"] if entry["scene_id"] == SCENE_ID)
    assert item["rule_calibration"]["habitual_needles"][0] == "突然意识到"
    assert "model_voice" not in item["open_dimensions"]
    assert next(entry for entry in item["findings"] if entry["dimension"] == "painless_scene")["severity"] == "info"
    # 同一条发现在两处 id 相同
    assert {entry["signal_id"] for entry in item["findings"]} <= {entry["signal_id"] for entry in bound["findings"]}


# ---------------------------------------------------------------------------
# 1. 局部深评看整场
# ---------------------------------------------------------------------------


def test_passage_scope_marks_focus_context_and_the_rest_of_the_scene() -> None:
    paragraphs = [f"第{index}段的正文。" for index in range(1, 7)]
    scope = passage_scope(paragraphs, [2, 3])
    assert scope["focus"] == [2, 3] and scope["start"] == 1 and scope["end"] == 4 and scope["whole_scene"] is True
    assert scope["abbreviated"] == 0
    assert "【焦点段 3】第3段的正文。" in scope["text"] and "【焦点段 4】第4段的正文。" in scope["text"]
    assert "【上下文 2】第2段的正文。" in scope["text"] and "【上下文 5】第5段的正文。" in scope["text"]
    assert "【第 1 段】第1段的正文。" in scope["text"] and "【第 6 段】第6段的正文。" in scope["text"]
    assert scope["focus_text"] == "第3段的正文。\n\n第4段的正文。"
    # 焦点段不算上下文；越界的焦点被丢掉
    assert passage_scope(paragraphs, [0, 99])["focus"] == [0]

    # 超长的场：远段只留开头（标「·略」），焦点与邻段仍是全文
    long = [_distinct_chars(1000)[index:] + "。" for index in range(20)]
    scope = passage_scope(long, [10], full_chars=12000)
    assert scope["whole_scene"] is False and scope["abbreviated"] == 17
    assert "【第 1 段·略】" in scope["text"] and "……" in scope["text"]
    assert f"【焦点段 11】{long[10]}" in scope["text"] and f"【上下文 12】{long[11]}" in scope["text"]


CROSS_OUTPUT = {
    "verdict": "no_finding",
    "assessment": "第三段说她没有再看他，但第二段的钟响还在等她回头。",
    "findings": [
        {
            "lens": "story",
            "dimension": "character_contradiction",
            "severity": "revision",
            "issue": "她「没有再看他」和前面等他回答的姿态矛盾。",
            "recommendation": "让她在钟响之后才转开视线。",
            "evidence_excerpt": "她没有再看他",
            "why_it_matters": "人物的注意力要连贯。",
            "related_excerpt": "录音里传来三声钟响",
            "related_paragraph_index": 2,
            "relation": "contradiction",
        }
    ],
    "rewrite_brief": "把「她没有再看他」挪到钟响之后。",
}


def test_passage_review_over_a_range_sees_the_whole_scene_and_pins_cross_paragraph_findings(client: TestClient, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    calls: list = []
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _scripted_runner(CROSS_OUTPUT, calls))
    _seed_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_start": 1, "paragraph_end": 2})
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    review = payload["passage_review"]
    assert review["focus_paragraphs"] == [1, 2] and review["paragraph_index"] == 1
    assert review["paragraph_start"] == 0 and review["paragraph_end"] == 2 and review["whole_scene"] is True
    prompt = calls[-1]["user_prompt"]
    assert "Focus paragraphs: 2–3" in prompt and "the whole scene is shown" in prompt
    assert "【焦点段 2】许望没有回答" in prompt and "【焦点段 3】她知道真相" in prompt and "【上下文 1】门外很安静" in prompt
    assert "## Cross-Paragraph Check" in prompt

    # 跨段的发现：证据钉在焦点段，另一段的原话钉到它所在的段（模型给的是 1 起的序号）
    cross = next(item for item in payload["findings"] if item["source"] == "ai" and item["dimension"] == "character_contradiction")
    assert cross["evidence"]["paragraph_index"] == 2 and cross["origin"]["kind"] == "passage"
    assert cross["origin"]["focus_paragraphs"] == [1, 2]
    assert cross["related"] == {
        "excerpt": "录音里传来三声钟响",
        "paragraph_index": 1,
        "start": cross["related"]["start"],
        "end": cross["related"]["end"],
        "kind": "contradiction",
        "stale": False,
        "label": "矛盾",
    }
    assert isinstance(cross["related"]["start"], int) and cross["related"]["end"] > cross["related"]["start"]
    assert payload["diagnosis_rollup"]["scenes"][SCENE_ID]["open"] == payload["summary"]["open"]

    # 焦点有交集的旧行退位；不相交的并存
    again = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_index": 2}).json()["data"]
    assert [entry["focus_paragraphs"] for entry in again["passage_reviews"]] == [[2]]
    apart = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_index": 0}).json()["data"]
    assert [entry["focus_paragraphs"] for entry in apart["passage_reviews"]] == [[2], [0]]

    # 复核一条跨段发现：另一段也进焦点
    verify = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"signal_id": cross["signal_id"]})
    assert verify.status_code == 200, verify.text
    prompt = calls[-1]["user_prompt"]
    assert "【焦点段 2】许望没有回答" in prompt and "【焦点段 3】她知道真相" in prompt
    assert "Related passage (paragraph 2): 录音里传来三声钟响" in prompt
    assert verify.json()["data"]["passage_review"]["about_signal_ids"] == [cross["signal_id"]]

    # 范围颠倒也认；范围越界 → 400
    assert client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_start": 2, "paragraph_end": 1}).status_code == 200
    bad = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_start": 7, "paragraph_end": 9})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "WRITER_PASSAGE_REVIEW_TARGET_INVALID"


# ---------------------------------------------------------------------------
# 3. 只通读改过的场
# ---------------------------------------------------------------------------


def _chapter_output(*findings: dict) -> dict:
    return {
        "overall_score": 0.6,
        "scores": {"choice_pressure": 0.5},
        "findings": list(findings),
        "revision_brief": [],
        "requires_human_review": False,
        "lens_evaluations": [],
    }


FIRST_SCENE_FINDING = {
    "lens": "prose",
    "dimension": "information_rhythm",
    "severity": "taste",
    "issue": "钟响来得太早。",
    "recommendation": "把钟响挪后。",
    "evidence_excerpt": "录音里传来三声钟响",
    "why_it_matters": "信息节奏。",
}
SECOND_SCENE_FINDING = {
    "lens": "story",
    "dimension": "choice_pressure",
    "severity": "revision",
    "issue": "停钟没有代价。",
    "recommendation": "让停钟惊动别人。",
    "evidence_excerpt": "许望把钟停了",
    "why_it_matters": "代价。",
}
CHAPTER_LEVEL_FINDING = {
    "lens": "reader",
    "dimension": "ending_drive",
    "severity": "blocking",
    "issue": "本章开头的承诺到结尾没有兑现。",
    "recommendation": "让最后一场回答第一场的问题。",
    "evidence_excerpt": "",
    "why_it_matters": "章的收束。",
}


def test_chapter_read_through_can_cover_only_the_changed_scenes(client: TestClient, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    _seed_scene(session)
    _add_second_scene(session)
    calls: list = []
    monkeypatch.setattr(
        "novel_system.services.writer_deep_review.LLMNodeRunner",
        _scripted_runner(_chapter_output(FIRST_SCENE_FINDING, SECOND_SCENE_FINDING, CHAPTER_LEVEL_FINDING), calls),
    )

    # 第一次：整章
    first = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review", json={}).json()["data"]
    assert first["ai"]["status"] == "current" and first["ai"]["scope"] == "all"
    assert first["ai"]["reviewed_scene_ids"] == [SCENE_ID, SCENE2_ID] and first["ai"]["carried_scene_ids"] == []
    assert first["ai"]["changed_scene_ids"] == [] and first["ai"]["incremental_available"] is False
    assert "【第 1 场】" in calls[-1]["user_prompt"] and "【第 1 场 · 本次通读】" not in calls[-1]["user_prompt"]
    first_id = first["ai"]["evaluation_id"]
    row = session.get(WriterEvaluation, first_id)
    recorded = row.contract_field_refs_json["scenes"]
    assert [item["scene_id"] for item in recorded] == [SCENE_ID, SCENE2_ID] and all(item["sha256"] for item in recorded)

    # 改了第二场的字：按哈希判——只有第二场是改过的
    service = AuthorDraftService(session)
    draft = service.ensure_blank("scene", SCENE2_ID, actor_ref="writer")["draft"]
    service.save(draft["draft_id"], {"content": "<p>许望没有停钟。</p><p>她把证据袋放回抽屉。</p>", "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    stale = client.get(f"/api/v1/chapters/{CHAPTER_ID}/deep-review").json()["data"]
    assert stale["ai"]["status"] == "stale" and stale["ai"]["changed_scene_ids"] == [SCENE2_ID] and stale["ai"]["changed_count"] == 1
    assert stale["ai"]["incremental_available"] is True
    assert [entry["changed_since_review"] for entry in stale["scenes"]] == [False, True]
    # 未改的第一场：钉在它上面的通读发现仍在；改过的第二场：引的那句已经不在
    assert [item["dimension"] for item in stale["scenes"][0]["findings_from_chapter"]] == ["information_rhythm"]
    assert stale["scenes"][1]["findings_from_chapter"] == []

    # 第二次：只通读改过的场
    new_second = {**SECOND_SCENE_FINDING, "issue": "放回抽屉太轻。", "recommendation": "让抽屉锁不上。", "evidence_excerpt": "她把证据袋放回抽屉"}
    monkeypatch.setattr(
        "novel_system.services.writer_deep_review.LLMNodeRunner",
        _scripted_runner(_chapter_output(new_second, CHAPTER_LEVEL_FINDING), calls),
    )
    second = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review", json={"scope": "changed"}).json()["data"]
    prompt = calls[-1]["user_prompt"]
    assert "【第 1 场 · 未改 · 摘要】" in prompt and "【第 2 场 · 本次通读】" in prompt
    assert "（开头）门外很安静" in prompt and "（结尾）她知道真相" in prompt
    assert "上次通读对这一场的发现：" in prompt and "[information_rhythm] 钟响来得太早。" in prompt
    assert "## Read-Through Scope" in prompt and "Scenes 2 changed" in prompt
    assert "### Previous Chapter-Level Findings" in prompt and "本章开头的承诺到结尾没有兑现" in prompt
    assert "许望没有停钟。" in prompt and "录音里传来三声钟响。" not in prompt.split("## Read-Through Scope")[0].split("【第 1 场 · 未改 · 摘要】")[1].split("【第 2 场")[0].replace("上次通读对这一场的发现：", "")

    assert second["ai"]["status"] == "current" and second["ai"]["scope"] == "changed"
    assert second["ai"]["reviewed_scene_ids"] == [SCENE2_ID] and second["ai"]["carried_scene_ids"] == [SCENE_ID]
    assert second["ai"]["carried_from"] == first_id and second["ai"]["evaluation_id"] != first_id
    assert second["ai"]["changed_scene_ids"] == [] and second["ai"]["incremental_available"] is False
    carried = second["scenes"][0]["findings_from_chapter"]
    assert [item["dimension"] for item in carried] == ["information_rhythm"]
    assert carried[0]["origin"]["carried_from"] == first_id and carried[0]["origin"]["evaluation_id"] == second["ai"]["evaluation_id"]
    assert second["scenes"][0]["carried"] is True and second["scenes"][1]["carried"] is False
    assert [item["issue"] for item in second["scenes"][1]["findings_from_chapter"]] == ["放回抽屉太轻。"]
    assert [item["dimension"] for item in second["chapter_findings"]] == ["ending_drive"]
    # 写作台里第一场的那条发现还是同一条 id，来源标着沿用
    scene1 = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    landed = _finding(scene1, "ai", "information_rhythm")
    assert landed["origin"]["kind"] == "chapter" and landed["origin"]["carried_from"] == first_id
    assert scene1["chapter_review"]["status"] == "current"

    # 没有场改过：不调模型，载荷说明
    before = len(calls)
    unchanged = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review", json={"scope": "changed"}).json()["data"]
    assert unchanged["notice"]["code"] == "CHAPTER_REVIEW_UP_TO_DATE" and len(calls) == before
    assert unchanged["ai"]["evaluation_id"] == second["ai"]["evaluation_id"]

    # 上一轮没记哈希（老的行）：scope=changed 退回整章
    row = session.get(WriterEvaluation, second["ai"]["evaluation_id"])
    row.contract_field_refs_json = None
    session.commit()
    fallback = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review", json={"scope": "changed"}).json()["data"]
    assert fallback["ai"]["scope"] == "all" and "【第 1 场】" in calls[-1]["user_prompt"] and "【第 1 场 · 未改 · 摘要】" not in calls[-1]["user_prompt"]

    # 请求体不认别的 scope
    assert client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review", json={"scope": "some"}).status_code in {400, 422}


# ---------------------------------------------------------------------------
# 4. 计数随写回传
# ---------------------------------------------------------------------------


def test_counts_travel_with_every_write_and_add_up_like_the_summary(client: TestClient, session) -> None:
    draft_id = _seed_scene(session)
    _add_second_scene(session)

    payload = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    rollup = payload["diagnosis_rollup"]
    assert rollup["chapter_id"] == CHAPTER_ID and set(rollup["scenes"]) == {SCENE_ID, SCENE2_ID}
    assert rollup["scenes"][SCENE_ID]["open"] == payload["summary"]["open"] > 0
    assert rollup["chapters"][CHAPTER_ID]["open"] == rollup["scenes"][SCENE_ID]["open"] + rollup["scenes"][SCENE2_ID]["open"]
    assert client.get(f"/api/v1/scenes/{SCENE_ID}/diagnosis-rollup").json()["data"] == rollup

    summary = client.get(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary").json()["data"]
    assert summary["scenes"][SCENE_ID] == rollup["scenes"][SCENE_ID] and summary["chapters"][CHAPTER_ID] == rollup["chapters"][CHAPTER_ID]
    assert summarize_counts(summary["scenes"], summary["chapters"]) == summary["totals"]

    # 作者稿一存：响应带这一场 / 这一章的新计数（字改短了，发现少了）
    revision = AuthorDraftService(session).ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]["revision_no"]
    saved = client.patch(f"/api/v1/author-drafts/{draft_id}", json={"content": "<p>她走了。</p>", "base_revision_no": revision}).json()["data"]
    assert saved["changed"] is True and "words_rollup" in saved
    after = saved["diagnosis_rollup"]
    assert after["scenes"][SCENE_ID]["open"] < rollup["scenes"][SCENE_ID]["open"]
    assert after == client.get(f"/api/v1/scenes/{SCENE_ID}/diagnosis-rollup").json()["data"]
    # 没改字的保存不带（也不算）
    same = client.patch(f"/api/v1/author-drafts/{draft_id}", json={"content": "<p>她走了。</p>", "base_revision_no": revision + 1}).json()["data"]
    assert same["changed"] is False and "diagnosis_rollup" not in same

    # 忽略一条：偏好保存的响应也带计数
    current = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    target = next(item for item in current["findings"] if not item["ignored"])
    prefs = client.patch(
        f"/api/v1/scenes/{SCENE_ID}/deep-review/preferences",
        json={"decision_log": [], "ignored_issue_keys": [target["signal_id"]], "base_revision_no": current["preferences"]["revision_no"]},
    ).json()["data"]
    assert prefs["diagnosis_rollup"]["scenes"][SCENE_ID]["ignored"] == 1
    assert prefs["diagnosis_rollup"]["scenes"][SCENE_ID]["open"] == current["summary"]["open"] - 1
