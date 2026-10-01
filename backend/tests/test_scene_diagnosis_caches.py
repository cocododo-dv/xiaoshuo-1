"""场景诊断的进程缓存与参考书校准的读数（2026-09-29 重构，审计 B05-07 / X01-03 / X01-09 / X01-10 / X01-11）。

1. 校准读数按书的版本缓存：就地重标段落类型（段型修订号 +1）之后重算，段数与时间都没变也一样。
2. 两本书的读数同时留在缓存里：两部作品各绑一本书，交替诊断不会一再重读全书。
3. 一次请求只查一次书的版本：逐场诊断不再逐场数一遍参考书段落表；书存着段落根哈希时一遍都不数。
4. 规则 / 节奏发现的缓存每场只留最新一份、多线程下不出错；校准说明与载荷一份校准只渲染一次。
"""

from __future__ import annotations

import sys
import threading

import pytest
from sqlalchemy import event, update

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    SceneCard,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.literary_quality import RuleCalibration
from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_diagnosis.calibration import calibration_from_reference
from novel_system.services.scene_diagnosis.findings import SceneFindingsCache
from novel_system.services.style_policy import StylePolicy

PROJECT_ID = "PROJECT_DIAG_CACHE"
CHAPTER_ID = "DIAG_CACHE_CH01"
DRAFT_HTML = "<p>门外很安静。她突然意识到，自己一直在等这一刻。</p><p>许望没有回答。录音里传来三声钟响。</p>"


def _reference_paragraphs(units: int = 12, per_unit: int = 120) -> list[str]:
    """合成的参考书正文：没有标题段、没有场界（收尾只能靠转场段补），每段 60 个互不成词的字。"""

    book: list[str] = []
    for unit in range(units):
        for index in range(per_unit):
            offset = (unit * per_unit + index) * 7 % 1500
            book.append("".join(chr(0x4E00 + (offset + step) % 2000) for step in range(60)) + "。")
    return book


def _seed_book(session, book_id: str, *, types: list[str] | None = None, title: str = "参考书") -> None:
    paragraphs = _reference_paragraphs()
    types = types or ["narration"] * len(paragraphs)
    session.add(StyleReferenceBook(book_id=book_id, title=title, source_kind="upload", cloud_policy="segments_only", text_checksum=f"sum_{book_id}", stats_json={}))
    session.add(StyleReferenceRun(run_id=f"run_{book_id}", book_id=book_id, status="completed", phase="synthesize", dispatch_state="completed", requested_layers_json=[]))
    session.add_all(
        [
            StyleReferenceParagraph(
                paragraph_id=f"{book_id}_para_{index}",
                book_id=book_id,
                paragraph_index=index,
                paragraph_type=types[index],
                start_offset=0,
                end_offset=len(text),
                text=text,
                char_count=len(text),
            )
            for index, text in enumerate(paragraphs)
        ]
    )
    session.add(StyleReferenceProfile(profile_id=f"prof_{book_id}", book_id=book_id, run_id=f"run_{book_id}", title="画像", profile_json={}))
    session.commit()


def _seed_scenes(session, count: int) -> list[str]:
    session.add(StoryProject(project_id=PROJECT_ID, title="诊断缓存", outline_text=""))
    session.add(ChapterGoal(chapter_id=CHAPTER_ID, project_id=PROJECT_ID, planned_scene_count=count, chapter_goal="她必须决定。"))
    scene_ids: list[str] = []
    for index in range(count):
        scene_id = f"{CHAPTER_ID}_SC{index + 1:02d}"
        scene_ids.append(scene_id)
        session.add(SceneCard(scene_id=scene_id, chapter_id=CHAPTER_ID, project_id=PROJECT_ID, scene_seq=index + 1, scene_goal="她开口。", beats_json=[]))
        session.add(
            AuthorDraft(
                draft_id=f"draft_{scene_id}",
                object_type="scene",
                object_id=scene_id,
                source_text_ref=f"scene:{scene_id}",
                content=DRAFT_HTML,
                status="current",
            )
        )
    session.commit()
    return scene_ids


def _bind(monkeypatch, books_by_scene: dict[str, str]) -> None:
    def policy(self, scene):
        book_id = books_by_scene[scene.scene_id]
        return StylePolicy(bound=True, style_first=True, mode="live", profile_id=f"prof_{book_id}", book_id=book_id)

    monkeypatch.setattr(SceneDiagnosisService, "style_policy", policy)


class _Statements:
    """会话连接上发出的 SQL（按片段数）。"""

    def __init__(self, session) -> None:
        self.engine = session.get_bind()
        self.statements: list[str] = []

    def _record(self, _conn, _cursor, statement, *_args) -> None:
        self.statements.append(statement)

    def __enter__(self) -> "_Statements":
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    def count(self, *fragments: str) -> int:
        return sum(1 for statement in self.statements if all(fragment in statement for fragment in fragments))


def _full_book_reads(statements: _Statements) -> int:
    # 整本书的段落正文按段序读一遍（算校准读数时才读）
    return statements.count("style_reference_paragraphs.text", "FROM style_reference_paragraphs", "ORDER BY")


# ---------------------------------------------------------------------------
# 1. 就地重标段落类型之后重算（X01-10）
# ---------------------------------------------------------------------------


def test_an_in_place_retype_recomputes_the_endings_calibration(session, monkeypatch) -> None:
    [scene_id] = _seed_scenes(session, 1)
    paragraphs = _reference_paragraphs()
    # 没有标题段也没有场界：收尾只能取转场段之前的那一段
    _seed_book(session, "book_retype", types=["transition" if index % 120 == 60 else "narration" for index in range(len(paragraphs))])
    _bind(monkeypatch, {scene_id: "book_retype"})
    scene = session.get(SceneCard, scene_id)

    before = SceneDiagnosisService(session).rule_calibration_for_scene(scene)
    assert before is not None and before.endings_source == "transitions" and before.endings >= 4

    # 「重新分类（就地）」：正文与段数都不变，只改段型；分类作业成功时把段型修订号加一
    session.execute(update(StyleReferenceParagraph).where(StyleReferenceParagraph.book_id == "book_retype").values(paragraph_type="narration"))
    book = session.get(StyleReferenceBook, "book_retype")
    book.stats_json = {**(book.stats_json or {}), "paragraph_types_revision": 1}
    session.commit()

    after = SceneDiagnosisService(session).rule_calibration_for_scene(scene)
    assert after is not None and after.endings_source == "none" and after.endings == 0


# ---------------------------------------------------------------------------
# 2. 两本书同时留在缓存里（X01-11）
# ---------------------------------------------------------------------------


def test_two_bound_books_do_not_evict_each_other(session, monkeypatch) -> None:
    scene_a, scene_b = _seed_scenes(session, 2)
    _seed_book(session, "book_a", title="甲")
    _seed_book(session, "book_b", title="乙")
    _bind(monkeypatch, {scene_a: "book_a", scene_b: "book_b"})
    scenes = [session.get(SceneCard, scene_a), session.get(SceneCard, scene_b)]

    with _Statements(session) as statements:
        for _ in range(3):
            for scene in scenes:
                # 每次新的服务实例（一次请求一个）：跨请求靠的是进程缓存
                calibration = SceneDiagnosisService(session).rule_calibration_for_scene(scene)
                assert isinstance(calibration, RuleCalibration) and calibration.active
    # 两本书各读一遍，之后交替诊断都命中缓存（以前单条缓存：每换一本书就把整本书重读、重算一遍）
    assert _full_book_reads(statements) == 2


# ---------------------------------------------------------------------------
# 3. 一次请求只查一次书的版本（X01-03 / X01-09）
# ---------------------------------------------------------------------------


def test_a_request_counts_the_reference_paragraphs_at_most_once(client, session, monkeypatch) -> None:
    scene_ids = _seed_scenes(session, 3)
    _seed_book(session, "book_one")
    _bind(monkeypatch, {scene_id: "book_one" for scene_id in scene_ids})
    client.get(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary")  # 冷的一次：读数算好进缓存

    with _Statements(session) as statements:
        summary = client.get(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary").json()["data"]
    assert set(summary["scenes"]) == set(scene_ids)
    # 三场绑同一本书：书的版本在这次请求里只数一遍段落表（以前逐场各数一遍）
    assert statements.count("count(style_reference_paragraphs.paragraph_id)") == 1
    assert _full_book_reads(statements) == 0

    # 书存着段落根哈希（写段落表的人改段落时会把它拿掉）：连这一遍都不用数
    book = session.get(StyleReferenceBook, "book_one")
    book.stats_json = {**(book.stats_json or {}), "paragraph_root_sha256": "root-sha", "paragraph_count": 1440}
    session.commit()
    with _Statements(session) as statements:
        again = client.get(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary").json()["data"]
    assert again["scenes"] == summary["scenes"]
    assert statements.count("count(style_reference_paragraphs.paragraph_id)") == 0


# ---------------------------------------------------------------------------
# 4. 发现缓存的线程安全；校准只渲染一次
# ---------------------------------------------------------------------------


def test_the_findings_cache_keeps_one_entry_per_scene_and_is_thread_safe() -> None:
    cache = SceneFindingsCache(maxsize=2)
    cache.put("s1", ("v1",), [{"signal_id": "a"}], [])
    cache.put("s1", ("v2",), [{"signal_id": "b"}], [])
    assert len(cache) == 1, "同一场只留最新的一份"
    assert cache.get("s1", ("v1",)) is None and cache.get("s1", ("v2",)) == ([{"signal_id": "b"}], [])
    hit = cache.get("s1", ("v2",))
    hit[0][0]["ignored"] = True
    assert cache.get("s1", ("v2",)) == ([{"signal_id": "b"}], []), "取出来的是副本，调用方改了不影响缓存"

    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for index in range(3000):
                scene_id = f"s{(offset + index) % 7}"
                cache.put(scene_id, (index,), [{"i": index}], [])
                cache.get(scene_id, (index,))
                cache.get(f"s{(offset + index + 3) % 7}", (index,))
        except BaseException as exc:  # noqa: BLE001 — 线程里的异常收集起来在主线程断言
            errors.append(exc)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 让线程在「取到键」与「挪到队尾」之间频繁切换
    try:
        threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert errors == []
    assert len(cache) <= 2


def test_a_calibration_renders_its_payload_and_note_once(monkeypatch) -> None:
    stats = {"paragraphs": 100, "long_paragraph_p95": 200, "echo_per_1k": 1.0, "same_opening_per_1k": 1.0}
    rule_stats = {
        "chars": 100_000,
        "windows": 40,
        "endings": 0,
        "endings_source": "none",
        "needle_rates": {f"词{index}": 1.0 + index for index in range(200)},
        "dimension_stats": {"model_voice": {"fired": 30, "n": 40}},
    }
    calibration = calibration_from_reference(profile_id="p", book_id="b", book_title="书", stats=stats, deliberate_repetition=False, rule_stats=rule_stats)
    calls: list[int] = []
    original = RuleCalibration.as_dict

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(RuleCalibration, "as_dict", counting)
    payloads = [calibration.as_dict() for _ in range(5)]
    assert all(payload == payloads[0] for payload in payloads)
    assert payloads[0]["note"].startswith("按《书》校准") and payloads[0]["rules"]["needle_count"] == 200
    # 载荷与说明各渲染一次规则那一半（一份校准在一次请求里要写进每一场的载荷）
    assert len(calls) == 2
    payloads[0]["rules"]["top_needles"].clear()
    assert calibration.as_dict()["rules"]["top_needles"], "给出去的是副本"


@pytest.mark.parametrize("house_taste", [False, True])
def test_moving_a_paragraph_boundary_is_a_new_findings_version(session, house_taste: bool) -> None:
    """只把段落分界挪到原有空格处：可见文字（段落之间一个空格）不变，段号变了——缓存不能把旧的段号交回来。"""

    from novel_system.services.scene_diagnosis import DEFAULT_CRAFT_CALIBRATION, DiagnosisText, cached_text_findings

    one = DiagnosisText(layer="author_draft", ref=None, content="", paragraphs=["门外很安静 她突然意识到，自己一直在等这一刻。"])
    two = DiagnosisText(layer="author_draft", ref=None, content="", paragraphs=["门外很安静", "她突然意识到，自己一直在等这一刻。"])
    assert one.plain == two.plain and one.sha256 == two.sha256
    first, _ = cached_text_findings("SC_BOUNDARY", one, calibration=DEFAULT_CRAFT_CALIBRATION, house_taste=house_taste)
    second, _ = cached_text_findings("SC_BOUNDARY", two, calibration=DEFAULT_CRAFT_CALIBRATION, house_taste=house_taste)
    voice_first = next(item for item in first if item["dimension"] == "model_voice")
    voice_second = next(item for item in second if item["dimension"] == "model_voice")
    assert voice_first["evidence"]["paragraph_index"] == 0
    assert voice_second["evidence"]["paragraph_index"] == 1


# ---------------------------------------------------------------------------
# 5. 逐场的查询是批量的（B05-03 / X01-12）
# ---------------------------------------------------------------------------


def _seed_reviewed_scene(session, scene_id: str, seq: int) -> None:
    from novel_system.db.models import WriterEvaluation

    session.add(SceneCard(scene_id=scene_id, chapter_id=CHAPTER_ID, project_id=PROJECT_ID, scene_seq=seq, scene_goal="她开口。", beats_json=[]))
    session.add(AuthorDraft(draft_id=f"draft_{scene_id}", object_type="scene", object_id=scene_id, source_text_ref=f"scene:{scene_id}", content=DRAFT_HTML, status="current"))
    finding = {"dimension": "choice_pressure", "severity": "revision", "issue": "选择没有落成动作。", "recommendation": "让她动手。", "evidence_excerpt": "录音里传来三声钟响"}
    for rubric, prefix in (("near_final_acceptance_v1", "nf"), ("literary_revision_v1", "ai")):
        session.add(
            WriterEvaluation(
                evaluation_id=f"{prefix}_{scene_id}",
                object_type="scene",
                object_id=scene_id,
                chapter_id=CHAPTER_ID,
                scene_id=scene_id,
                rubric_id=rubric,
                source_text_ref=f"author_draft:draft_{scene_id}",
                lens="aggregate",
                findings_json=[finding],
                status="completed",
            )
        )
    session.add(
        WriterEvaluation(
            evaluation_id=f"lens_{scene_id}",
            object_type="scene",
            object_id=scene_id,
            chapter_id=CHAPTER_ID,
            scene_id=scene_id,
            rubric_id="literary_revision_v1",
            lens="story",
            parent_evaluation_id=f"ai_{scene_id}",
            findings_json=[],
            status="completed",
        )
    )
    session.add(
        WriterEvaluation(
            evaluation_id=f"passage_{scene_id}",
            object_type="scene",
            object_id=scene_id,
            chapter_id=CHAPTER_ID,
            scene_id=scene_id,
            rubric_id="literary_revision_passage_v1",
            source_text_ref=f"author_draft:draft_{scene_id}",
            lens="passage",
            findings_json=[finding],
            contract_field_refs_json={"kind": "passage", "paragraph_index": 1, "focus_paragraphs": [1], "verdict": "holds"},
            status="completed",
        )
    )


def test_diagnosis_queries_do_not_grow_with_the_scene_count(client, session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="诊断缓存", outline_text=""))
    session.add(ChapterGoal(chapter_id=CHAPTER_ID, project_id=PROJECT_ID, planned_scene_count=8, chapter_goal="她必须决定。"))
    for index in range(2):
        _seed_reviewed_scene(session, f"{CHAPTER_ID}_SC{index + 1:02d}", index + 1)
    session.commit()

    def statements_for(path: str) -> tuple[int, dict]:
        session.expire_all()
        with _Statements(session) as statements:
            response = client.get(path)
        assert response.status_code == 200, response.json()
        return len(statements.statements), response.json()["data"]

    few_summary, summary = statements_for(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary")
    few_scene, scene_payload = statements_for(f"/api/v1/scenes/{CHAPTER_ID}_SC01/deep-review")
    assert summary["scenes"][f"{CHAPTER_ID}_SC01"]["open"] >= 3
    assert scene_payload["review"]["status"] == "current" and scene_payload["ai"]["lenses"] == [{"lens": "story", "label": "故事", "overall_score": None}]
    assert any((item.get("origin") or {}).get("kind") in {"scene", "passage"} for item in scene_payload["findings"] if item["source"] == "ai")

    for index in range(2, 8):
        _seed_reviewed_scene(session, f"{CHAPTER_ID}_SC{index + 1:02d}", index + 1)
    session.commit()
    many_summary, summary = statements_for(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary")
    many_scene, _ = statements_for(f"/api/v1/scenes/{CHAPTER_ID}_SC01/deep-review")
    assert len(summary["scenes"]) == 8
    # 多 6 场只多 6 条：每场的风格策略现解析（style_policy_live，一场一查）；草稿、终稿、评审行、局部深评、
    # 镜头行、冻结正文都是整组一次（以前每场六七条）
    assert many_summary - few_summary <= 6, (few_summary, many_summary)
    assert many_scene - few_scene <= 6, (few_scene, many_scene)


# ---------------------------------------------------------------------------
# 后台预热（X01-04）：绑定着的参考书的两份读数不等第一次诊断现算
# ---------------------------------------------------------------------------


def test_the_warmup_task_fills_both_reference_caches_off_the_request_path(session) -> None:
    """全系统维护登记簿上的预热任务：每 6 小时一次、启动后的第一拍不跑（热加载新代码时不做这件重活）；跑的时候
    把绑定着的每本书的规则与节奏读数算进进程缓存，之后读校准不再整本读段落。只读库、不写任何东西。"""
    from novel_system.services import maintenance
    from novel_system.services.literary_quality import calibration_source
    from novel_system.services.scene_diagnosis import calibration as craft
    from tests.style_reference_factories import make_binding

    _seed_book(session, "book_warm")
    session.get(StyleReferenceProfile, "prof_book_warm").status = "active"
    make_binding(session, "prof_book_warm", binding_id="bind_warm", scope="global", scope_ref_id=None)
    session.commit()

    registered = maintenance.SYSTEM_MAINTENANCE.tasks.get(craft.REFERENCE_CALIBRATION_WARMUP_TASK)
    assert registered is not None and registered[0] is craft.warm_reference_calibrations
    assert registered[1] >= 6 * 3600
    start = 1_000.0
    try:
        maintenance.reset_maintenance_schedule(now=start)
        assert craft.REFERENCE_CALIBRATION_WARMUP_TASK not in maintenance.run_due_maintenance(now=start + 60)
    finally:
        maintenance.reset_maintenance_schedule()

    rule_builds, craft_builds = calibration_source._RULE_STATS.builds, craft._CRAFT_STATS.builds
    with _Statements(session) as warm:
        assert craft.warm_reference_calibrations() == 1
    assert (calibration_source._RULE_STATS.builds, craft._CRAFT_STATS.builds) == (rule_builds + 1, craft_builds + 1)
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in warm.statements)

    with _Statements(session) as statements:
        calibration = craft.craft_calibration_for(session, calibration_source.bound_profile(session, "prof_book_warm"))
    assert calibration.source == "reference"
    assert _full_book_reads(statements) == 0
