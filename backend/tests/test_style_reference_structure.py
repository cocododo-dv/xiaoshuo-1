"""结构跟随（2026-09-12 Step 2 Track B）：结构画像、场景手法与规划层注入。

覆盖：标题段切章（合成多章样本 + 鲁迅公版语料）、画像渲染 ≤1,500 字与样例封装、场景手法
派生与渲染、合成期写入两个新键（失败缺键）、场景蓝图摘要 + 验证器「无」、章规划上下文 slot
与提示词渲染、雪花场景步载荷成员与降载次序、七个规划模板版本；旧画像一律不渲染。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    SnowflakeStepRun,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMResponse
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.style_reference.segmentation.heuristic import (
    classify_heuristic_sequence,
    is_title_paragraph,
)
from novel_system.services.style_reference.structure import (
    PLANNING_GUIDANCE_MAX_LINES,
    STRUCTURE_CARD_MAX_CHARS,
    compute_structure_card,
    derive_planning_guidance,
    render_planning_guidance,
    render_structure_card,
    render_structure_card_parts,
)
from novel_system.services.style_reference.text_utils import normalize_text, split_paragraphs

GOLDEN_CORPUS = (
    Path(__file__).resolve().parent / "golden" / "style_reference" / "corpus" / "luxun_short_stories.txt"
)
SAMPLES_BOUNDARY = "[UNTRUSTED_REFERENCE_DATA:structure_samples]"
PROJECT_ID = "P_STRUCT"
CHAPTER_ID = "STRUCT_CH01"
SCENE_ID = "STRUCT_CH01_SC01"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _synthetic_rows(*, markers: bool = True) -> list[dict]:
    """五章合成书：第 n 章有 2n+2 段正文（首段 + n 组对白/叙述 + 末段），末章带落款日期行。"""
    rows: list[dict] = []

    def add(text: str, ptype: str) -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    for n in range(1, 6):
        if markers:
            add(f"第{n}章 灯下", "transition")
        add(f"第{n}章的开头：老周把账本合上，坐了很久。", "description_env" if n == 3 else "narration")
        for i in range(n):
            add(f"他说：“第{n}章第{i}句。”", "dialogue")
            add(f"第{n}章第{i}段叙述，账本上的数字还是对不上。", "narration")
        if n % 2:
            add(f"第{n}章的结尾，他说：“走吧。”", "dialogue")
        else:
            add(f"第{n}章的结尾：灯灭了，屋里只剩风声。", "narration")
        if n == 5:
            add("一九二四年二月七日", "narration")
    return rows


def _synthetic_book_text() -> str:
    return "\n\n".join(row["text"] for row in _synthetic_rows())


def _voice(first: float, second: float, third: float) -> dict:
    return {
        "version": "voice_signature_v1",
        "features": {
            "person_first_share": first,
            "person_second_share": second,
            "person_third_share": third,
        },
        "habits": [],
    }


def _profile_json_with_structure(**overrides) -> dict:
    payload = {
        "style_features": ["短句克制"],
        "structure_card": compute_structure_card(_synthetic_rows(), voice_signature=_voice(0.7, 0.1, 0.2)),
        "planning_guidance": ["对白：对白短促，常以一句反问收束", "情绪基调：冷而不哀"],
    }
    payload.update(overrides)
    return payload


def _seed_style_binding(
    session,
    *,
    project_id: str,
    profile_json: dict,
    send_rights: bool = True,
    seed: str = "st",
) -> None:
    session.add(
        StyleReferenceBook(
            book_id=f"sr_book_{seed}",
            title="Public domain source",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum=f"checksum-{seed}",
            stats_json={"rights_declaration": {"declared": True, "send_rights": send_rights}},
        )
    )
    session.add(StyleReferenceRun(run_id=f"sr_run_{seed}", book_id=f"sr_book_{seed}", status="done"))
    session.add(
        StyleReferenceProfile(
            profile_id=f"sr_profile_{seed}",
            book_id=f"sr_book_{seed}",
            run_id=f"sr_run_{seed}",
            title="Audited profile",
            status="active",
            profile_json=profile_json,
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id=f"sr_bind_{seed}",
            profile_id=f"sr_profile_{seed}",
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="A",
            status="active",
        )
    )
    session.commit()


def _seed_scene(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="结构跟随", outline_text="结构跟随样本大纲。"))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="一次重逢必须变成一个选择。",
            writer_brief_json={"chapter_promise": "重逢揭出危险的沉默"},
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="主角追问那个名字。",
            beats_json=["追问", "回避", "决定自己去查"],
            exit_change="老友成了嫌疑人。",
            hook="茶杯在名字出口时停住。",
            writer_brief_json={"choice_under_pressure": "信任老友还是独自调查"},
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()


class _Finding:
    def __init__(self, sub_dimension: str, statement: str, *, kind: str = "observation", status: str = "approved", confidence: str = "high"):
        self.sub_dimension = sub_dimension
        self.statement = statement
        self.finding_kind = kind
        self.status = status
        self.confidence = confidence


# ---------------------------------------------------------------------------
# B1 · 切章与画像
# ---------------------------------------------------------------------------


def test_is_title_paragraph_alias_recognises_title_shapes() -> None:
    for title in ("第一章 灯下", "第12章", "卷一", "《狂人日记》", "Chapter 3", "序", "楔子", "（一）", "一"):
        assert is_title_paragraph(title), title
    for body in ("他走了。", "一九二四年二月七日", "第一章的开头：老周把账本合上，坐了很久。", ""):
        assert not is_title_paragraph(body), body


def test_structure_card_splits_synthetic_book_at_chapter_titles() -> None:
    card = compute_structure_card(_synthetic_rows(), voice_signature=_voice(0.7, 0.1, 0.2))

    assert card["version"] == "structure_card_v1"
    assert card["has_chapter_markers"] is True
    assert card["chapter_marker_style"] == "第X章式"
    assert card["chapter_count"] == 5
    # 标题段与落款日期行不入章正文：第 n 章 2n+2 段
    assert [entry["paragraph_count"] for entry in card["chapters"]] == [4, 6, 8, 10, 12]
    assert card["paragraph_count"] == 40
    assert card["paragraphs_per_chapter"]["median"] == 8
    assert card["paragraphs_per_chapter"]["p10"] <= 8 <= card["paragraphs_per_chapter"]["p90"]
    assert card["chapter_chars"]["p10"] <= card["chapter_chars"]["median"] <= card["chapter_chars"]["p90"]
    assert card["chapter_chars"]["median"] == card["chapters"][2]["char_count"]
    # 对白段占比：18 / 40
    assert card["dialogue_share"] == 0.45
    assert card["chapters"][0]["dialogue_share"] == 0.5
    assert card["chapters"][1]["dialogue_share"] == 0.333
    assert 0 < card["dialogue_char_share"] < 1
    assert set(card["paragraph_type_shares"]) == {"dialogue", "narration", "description_env"}
    assert card["opening_type_distribution"] == {"narration": 4, "description_env": 1}
    assert card["closing_type_distribution"] == {"dialogue": 3, "narration": 2}
    # 每章的开合段型与摘录
    third = card["chapters"][2]
    assert third["index"] == 3 and third["opening_type"] == "description_env"
    assert third["opening_excerpt"].startswith("第3章的开头")
    assert third["closing_type"] == "dialogue" and third["closing_excerpt"].endswith("“走吧。”")
    last = card["chapters"][4]
    assert last["closing_excerpt"] == "第5章的结尾，他说：“走吧。”"  # 落款日期行被跳过
    # 人称取自 voice_signature
    assert card["person"]["dominant"] == "first" and card["person"]["first_share"] == 0.7
    # 样例跨全书取首 / 中 / 末章
    openings = card["samples"]["chapter_openings"]
    endings = card["samples"]["chapter_endings"]
    assert [item["chapter_index"] for item in openings] == [1, 3, 5]
    assert [item["chapter_index"] for item in endings] == [1, 3, 5]
    assert openings[1]["paragraph_type"] == "description_env"
    assert all(len(item["text"]) <= 150 and item["truncated"] is False for item in openings + endings)
    # 纯 JSON 值
    json.dumps(card, ensure_ascii=False)


def test_structure_card_without_markers_treats_the_book_as_one_chapter() -> None:
    rows = _synthetic_rows(markers=False)
    card = compute_structure_card(rows)

    assert card["has_chapter_markers"] is False
    assert card["chapter_marker_style"] is None
    assert card["chapter_count"] == 1
    assert card["paragraph_count"] == 40
    assert card["chapter_chars"]["median"] == card["total_chars"]
    assert card["person"] is None
    assert len(card["samples"]["chapter_openings"]) == 1
    assert len(card["samples"]["chapter_endings"]) == 1
    assert card["samples"]["chapter_openings"][0]["text"] == rows[0]["text"]
    assert card["samples"]["chapter_endings"][0]["text"] == "第5章的结尾，他说：“走吧。”"
    rendered = render_structure_card({"structure_card": card})
    assert "无章节标记" in rendered


def test_structure_card_truncates_long_excerpts_and_marks_them() -> None:
    long_opening = "开头" * 120
    rows = [
        {"paragraph_index": 0, "text": long_opening, "paragraph_type": "narration"},
        {"paragraph_index": 1, "text": "中间一段。", "paragraph_type": "narration"},
        {"paragraph_index": 2, "text": "结尾" * 120, "paragraph_type": "narration"},
    ]
    card = compute_structure_card(rows)
    opening = card["samples"]["chapter_openings"][0]
    ending = card["samples"]["chapter_endings"][0]
    assert len(opening["text"]) == 150 and opening["truncated"] is True
    assert len(ending["text"]) == 150 and ending["truncated"] is True
    rendered = render_structure_card({"structure_card": card})
    assert f"{opening['text']}……" in rendered
    assert f"……{ending['text']}" in rendered


def test_structure_card_on_luxun_corpus_detects_the_eleven_stories() -> None:
    text = normalize_text(GOLDEN_CORPUS.read_text(encoding="utf-8"))
    spans = split_paragraphs(text)
    types = classify_heuristic_sequence([body for _start, _end, body in spans])
    rows = [
        {"paragraph_index": index, "text": body, "paragraph_type": ptype}
        for index, ((_start, _end, body), (ptype, _conf)) in enumerate(zip(spans, types))
    ]
    card = compute_structure_card(rows)

    # 十一篇以《题名》起头（无 第X章 标记）——启发式能识别的就是这些
    assert card["has_chapter_markers"] is True
    assert card["chapter_marker_style"] == "《题名》式"
    assert card["chapter_count"] == 11
    assert card["chapters_listed"] == 11
    assert 3000 <= card["chapter_chars"]["median"] <= 7000
    assert card["chapter_chars"]["p10"] < card["chapter_chars"]["median"] < card["chapter_chars"]["p90"]
    assert card["dialogue_share"] > 0.4
    # 「（一九一八年四月。）」一类纯落款不是收章段（带编者按语的段落是正文，照常保留）
    bare_date = re.compile(r"[（(]?[一二三四五六七八九十〇零\d]+年[一二三四五六七八九十〇零\d月日]*[。]?[）)]?")
    for entry in card["chapters"]:
        assert bare_date.fullmatch(entry["closing_excerpt"]) is None, entry["closing_excerpt"]
    assert card["samples"]["chapter_openings"][0]["text"] == "今天晚上，很好的月光。"
    assert [item["chapter_index"] for item in card["samples"]["chapter_endings"]] == [1, 6, 11]
    # 画像随契约冻结进每个 bundle：体量有界
    assert len(json.dumps(card, ensure_ascii=False)) < 12_000


def test_render_structure_card_is_bounded_numeric_and_wraps_samples() -> None:
    profile_json = _profile_json_with_structure()
    stats, samples = render_structure_card_parts(profile_json)

    assert stats.startswith("[结构画像]")
    assert len(stats) <= STRUCTURE_CARD_MAX_CHARS
    assert "共 5 章" in stats and "第X章式" in stats
    assert "对白段占 45%" in stats
    assert "开章段型：叙述 4 章、环境描写 1 章" in stats
    assert "收章段型：对白 3 章、叙述 2 章" in stats
    assert "第一人称为主" in stats
    assert "规划提示：按此尺度规划" in stats
    # 样例：封装边界 + 自定义前导句 + 章首 / 章尾各 ≤3 条
    assert samples.startswith("下方是参考作者各章的开头与结尾片段原文")
    assert SAMPLES_BOUNDARY in samples and samples.rstrip().endswith("[/UNTRUSTED_REFERENCE_DATA]")
    assert "章首样例：" in samples and "章尾样例：" in samples
    assert samples.count("（第 ") == 6
    assert "第3章的开头" in samples and "“走吧。”" in samples
    # 合并渲染 = 画像 + 样例；不含样例时没有边界
    assert render_structure_card(profile_json) == f"{stats}\n{samples}"
    assert SAMPLES_BOUNDARY not in render_structure_card(profile_json, include_samples=False)
    # 旧画像 / 形状不对 → 空
    assert render_structure_card({"style_features": ["短句"]}) == ""
    assert render_structure_card({"structure_card": {"chapter_count": 0}}) == ""
    assert render_structure_card(None) == ""


def test_planning_guidance_derivation_round_robins_and_filters() -> None:
    corpus = ["她把灯芯拨小，屋里暗下去一半，谁也没有说话。"]
    findings = [
        _Finding("language.rhetoric", "语言层的观察不进规划"),
        _Finding("scene.dialogue", "对白短促，常以反问收束"),
        _Finding("scene.dialogue", "对白里少有称呼语", confidence="medium"),
        _Finding("scene.dialogue", "对白后常接一段沉默", confidence="low"),
        _Finding("theme.emotional_tone", "冷而不哀"),
        _Finding("theme.emotional_tone", "被驳回的不收", status="rejected"),
        _Finding("scene.environment", "环境只在情绪转折处出现"),
        _Finding("scene.environment", "禁忌不进观察行", kind="forbidden_pattern"),
        _Finding("scene.sensory_priority", "她把灯芯拨小，屋里暗下去一半，谁也没有说话"),  # 原文重合
        _Finding("theme.motifs", "对白短促，常以反问收束"),  # 与对白行重复
    ]
    lines = derive_planning_guidance(findings, corpus_texts=corpus)
    assert lines == [
        "对白：对白短促，常以反问收束",
        "环境：环境只在情绪转折处出现",
        "情绪基调：冷而不哀",
        "对白：对白里少有称呼语",
        "对白：对白后常接一段沉默",
    ]
    many = [_Finding("scene.dialogue", f"对白手法 {index}") for index in range(20)]
    assert len(derive_planning_guidance(many)) == PLANNING_GUIDANCE_MAX_LINES
    assert derive_planning_guidance([_Finding("language.vocabulary", "只有语言层")]) == []

    rendered = render_planning_guidance({"planning_guidance": lines})
    assert rendered.startswith("[场景手法]")
    assert "- 对白：对白短促，常以反问收束" in rendered
    assert rendered.count("\n- ") == 5
    assert render_planning_guidance({"planning_guidance": []}) == ""
    assert render_planning_guidance({"style_features": ["短句"]}) == ""
    # 存量列表超长也只渲染 ≤10 行
    overflow = render_planning_guidance({"planning_guidance": [f"行 {index}" for index in range(30)]})
    assert overflow.count("\n- ") == PLANNING_GUIDANCE_MAX_LINES


# ---------------------------------------------------------------------------
# B2 · 合成期写入
# ---------------------------------------------------------------------------


def _ingest_synthetic_book(session, seed: str) -> tuple[str, str]:
    from novel_system.services.style_reference.ingest import IngestService
    from novel_system.services.style_reference.repository import StyleReferenceRepository

    result = IngestService(session, llm_enabled=False).ingest_upload(
        raw_bytes=_synthetic_book_text().encode("utf-8"),
        file_name=f"book_{seed}.txt",
        title="合成五章",
        author_label="作者",
        cloud_policy="segments_only",
        rights_declaration={"analysis_rights": True, "send_rights": True},
    )
    book_id = result.book.book_id
    repo = StyleReferenceRepository(session)
    run_id = f"sr_run_{seed}"
    repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
    extraction_id = f"sr_ext_{seed}"
    repo.create_extraction(
        extraction_id=extraction_id,
        book_id=book_id,
        run_id=run_id,
        layer="scene",
        sub_dimension="scene.dialogue",
        raw_payload_json={},
        status="done",
        validation_errors_json=[],
        purpose="extract",
    )
    rows = [
        ("scene.dialogue", "observation", "对白短促，常以反问收束"),
        ("scene.dialogue", "forbidden_pattern", "不用长篇独白"),
        ("theme.emotional_tone", "observation", "冷而不哀"),
        ("language.rhetoric", "observation", "语言层的观察不进规划"),
    ]
    for index, (sub_dimension, kind, statement) in enumerate(rows):
        repo.create_finding(
            finding_id=f"sr_find_{seed}_{index}",
            book_id=book_id,
            run_id=run_id,
            extraction_id=extraction_id,
            sub_dimension=sub_dimension,
            finding_kind=kind,
            statement=statement,
            confidence="high",
            status="approved",
        )
    repo.create_quote(
        quote_id=f"sr_quote_{seed}",
        book_id=book_id,
        paragraph_id=None,
        span_start=0,
        span_end=10,
        quote_text="他说：“走吧。”",
        illustrates_dims=["scene.dialogue"],
        extracted_features={"paragraph_type": "dialogue"},
    )
    repo.create_evidence(
        evidence_id=f"sr_ev_{seed}",
        finding_id=f"sr_find_{seed}_0",
        quote_id=f"sr_quote_{seed}",
        anchor_kind="paragraph_quote",
        is_synthetic=0,
    )
    session.commit()
    return book_id, run_id


def _synthesis_fake():
    from types import SimpleNamespace

    from tests.accounted_llm_fakes import AccountedGenerateMixin

    response = {
        "profile_title": "合成五章风格",
        "narrative_summary": "短句、冷静叙述。",
        "style_features": ["短句克制"],
        "narrative_patterns": ["转折前先释放可见线索"],
        "banned_replication_rules": [],
        "calibration_guidance": [],
    }

    class _Client(AccountedGenerateMixin):
        def generate(self, request):  # noqa: ANN001
            return SimpleNamespace(
                structured_output=response,
                text=json.dumps(response, ensure_ascii=False),
                usage={},
                finish_reason="stop",
                provider="fake",
                model="fake",
                response_format="json_object",
                request_id=None,
                raw_response={},
            )

    return _Client()


def test_synthesize_writes_structure_card_and_planning_guidance(session) -> None:
    from novel_system.services.style_reference.profile_synthesizer import ProfileSynthesizer

    book_id, run_id = _ingest_synthetic_book(session, "structsyn")
    profile = ProfileSynthesizer(session, llm_client=_synthesis_fake(), llm_enabled=True).synthesize(
        book_id, run_id
    )
    session.commit()

    pj = profile.profile_json
    card = pj["structure_card"]
    assert card["version"] == "structure_card_v1"
    assert card["has_chapter_markers"] is True and card["chapter_count"] == 5
    assert card["chapter_marker_style"] == "第X章式"
    assert [entry["paragraph_count"] for entry in card["chapters"]] == [4, 6, 8, 10, 12]
    # 人称来自导入期写进 stats_json 的声音签名
    assert card["person"] is not None and card["person"]["dominant"] in {"first", "second", "third", "mixed"}
    # scene.* / theme.* 的 observation 才进规划指引：禁忌与语言层都不进
    assert pj["planning_guidance"] == ["对白：对白短促，常以反问收束", "情绪基调：冷而不哀"]
    # 既有键不受影响
    assert pj["narrative_guidance"] == ["转折前先释放可见线索"]
    assert "voice_signature" in pj


def test_synthesize_omits_the_keys_when_structure_computation_fails(session, monkeypatch, caplog) -> None:
    from novel_system.services.style_reference import profile_synthesizer as module

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("structure exploded")

    monkeypatch.setattr(module, "compute_structure_card", _boom)
    monkeypatch.setattr(module, "derive_planning_guidance", _boom)
    book_id, run_id = _ingest_synthetic_book(session, "structfail")
    with caplog.at_level("WARNING"):
        profile = module.ProfileSynthesizer(session, llm_client=_synthesis_fake(), llm_enabled=True).synthesize(
            book_id, run_id
        )
    session.commit()

    assert "structure_card" not in profile.profile_json
    assert "planning_guidance" not in profile.profile_json
    assert profile.profile_json["narrative_guidance"] == ["转折前先释放可见线索"]
    assert "structure card computation failed" in caplog.text
    assert "planning guidance derivation failed" in caplog.text


# ---------------------------------------------------------------------------
# B3 · project + global 绑定解析
# ---------------------------------------------------------------------------


def test_resolve_project_style_reference_degrades_and_renders(session, monkeypatch) -> None:
    from novel_system.services.style_reference import planning_context as module

    assert module.resolve_project_style_reference(session, PROJECT_ID) is None
    assert module.resolve_project_style_reference(session, None) is None

    # 旧画像：绑定存在但没有新键 → None
    _seed_style_binding(session, project_id=PROJECT_ID, profile_json={"style_features": ["短句"]}, seed="legacy")
    assert module.resolve_project_style_reference(session, PROJECT_ID) is None

    session.query(StyleReferenceInjectionBinding).delete()
    session.commit()
    _seed_style_binding(session, project_id=PROJECT_ID, profile_json=_profile_json_with_structure(), seed="live")
    reference = module.resolve_project_style_reference(session, PROJECT_ID)
    assert reference is not None
    assert set(reference) == {"contract_hash", "profile_id", "structure_card", "structure_samples", "planning_guidance"}
    assert len(reference["contract_hash"]) == 64
    assert reference["profile_id"] == "sr_profile_live"
    assert reference["structure_card"].startswith("[结构画像]")
    assert SAMPLES_BOUNDARY in reference["structure_samples"]
    assert reference["planning_guidance"].startswith("[场景手法]")
    assert module.structure_card_text(reference) == f"{reference['structure_card']}\n{reference['structure_samples']}"

    # 解析异常 → None（只记 debug 日志，不阻断规划）
    def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(module.InjectionService, "resolve_binding_layers", _boom)
    assert module.resolve_project_style_reference(session, PROJECT_ID) is None


def test_structure_samples_require_current_send_rights(session) -> None:
    from novel_system.services.style_reference.planning_context import resolve_project_style_reference

    _seed_style_binding(
        session, project_id=PROJECT_ID, profile_json=_profile_json_with_structure(), send_rights=False, seed="norights"
    )
    reference = resolve_project_style_reference(session, PROJECT_ID)
    assert reference is not None
    assert reference["structure_card"].startswith("[结构画像]")
    assert reference["structure_samples"] == ""


# ---------------------------------------------------------------------------
# B4(a) · 场景蓝图
# ---------------------------------------------------------------------------


def test_blueprint_snapshot_carries_structure_card_and_scene_craft(session) -> None:
    from novel_system.services.scene_blueprint import (
        SceneBlueprintService,
        _blueprint_user_prompt,
        snapshot_has_style_reference,
    )

    _seed_scene(session)
    _seed_style_binding(
        session,
        project_id=PROJECT_ID,
        profile_json=_profile_json_with_structure(narrative_guidance=["关键信息放段首一次给出"]),
        seed="bp",
    )
    service = SceneBlueprintService(session, llm_client=object())
    scene = session.get(SceneCard, SCENE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)
    source = service._source_snapshot(scene, chapter)
    snapshot = source["snapshot"]

    digests = snapshot["inline_digests"]
    assert digests["style_structure_card"].startswith("[结构画像]")
    assert SAMPLES_BOUNDARY in digests["style_structure_card"]
    assert digests["style_planning_guidance"].startswith("[场景手法]")
    assert "- 关键信息放段首一次给出" in digests["style_narrative_guidance"]
    assert "短句克制" not in digests["style_structure_card"]  # 语言层特征不进规划层
    slots = [item["slot"] for item in snapshot["ordered_injections"]]
    assert slots[-3:] == ["style_narrative_guidance", "style_structure_card", "style_planning_guidance"]
    contract_hash = snapshot["source_version_refs"]["style_reference_runtime_contract_hash"]
    assert len(contract_hash) == 64
    assert all(
        item["ref_id"] == contract_hash and item["digest_key"] == item["slot"]
        for item in snapshot["ordered_injections"]
        if item["slot"].startswith("style_")
    )
    refs = snapshot["source_version_refs"]
    assert refs["style_structure_card_chars"] == len(digests["style_structure_card"])
    assert refs["style_planning_guidance_line_count"] == 2
    assert refs["style_narrative_guidance_line_count"] == 1
    assert snapshot_has_style_reference(snapshot) is True

    # 两块不在 SECTION_SPECS 里：由蓝图 user prompt 直接渲染
    prompt = _blueprint_user_prompt("BASE", scene=scene, chapter=chapter, source=source)
    assert "## Style Reference — Structure Card" in prompt
    assert "## Style Reference — Scene Craft" in prompt
    assert prompt.index("## Style Reference — Structure Card") < prompt.index("## Scene Blueprint Target")
    assert "共 5 章" in prompt and "- 对白：对白短促，常以一句反问收束" in prompt
    template = load_prompt_templates()["scene_blueprint"]
    assert template.version == "2026-09-13.v7"
    assert "If a Style Reference — Narrative Mechanisms section is present" in template.task_prompt
    assert "Structure Card ([结构画像]) or Scene Craft ([场景手法])" in template.task_prompt
    assert "anti_summary_rule may be exactly 「无」" in template.task_prompt


def test_blueprint_snapshot_only_carries_what_the_profile_has(session) -> None:
    from novel_system.services.scene_blueprint import SceneBlueprintService, snapshot_has_style_reference

    _seed_scene(session)
    service = SceneBlueprintService(session, llm_client=object())
    scene = session.get(SceneCard, SCENE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)

    # 无绑定
    snapshot = service._source_snapshot(scene, chapter)["snapshot"]
    assert not any(key.startswith("style_") for key in snapshot["inline_digests"])
    assert snapshot_has_style_reference(snapshot) is False
    assert "style_reference_runtime_contract_hash" not in snapshot["source_version_refs"]

    # 只有场景手法、没有画像也没有叙事机制 → 只注入手法一块
    _seed_style_binding(
        session,
        project_id=PROJECT_ID,
        profile_json={"style_features": ["短句"], "planning_guidance": ["母题：灯与账本反复出现"]},
        seed="craft",
    )
    snapshot = service._source_snapshot(scene, chapter)["snapshot"]
    assert set(key for key in snapshot["inline_digests"] if key.startswith("style_")) == {"style_planning_guidance"}
    assert snapshot["source_version_refs"]["style_narrative_guidance_line_count"] == 0
    assert "style_structure_card_chars" not in snapshot["source_version_refs"]
    assert snapshot_has_style_reference(snapshot) is True


def test_blueprint_validator_accepts_none_only_with_a_style_reference() -> None:
    from novel_system.services.scene_blueprint import SCENE_BLUEPRINT_FIELDS, _validate_blueprint_payload

    payload = {field: f"{field} 内容" for field in SCENE_BLUEPRINT_FIELDS}
    payload["ending_action"] = "无"
    payload["anti_summary_rule"] = "None"

    normalized = _validate_blueprint_payload(payload, style_reference_present=True)
    assert normalized["ending_action"] == "无" and normalized["anti_summary_rule"] == "无"
    assert normalized["visible_desire"] == "visible_desire 内容"

    with pytest.raises(DomainError) as excinfo:
        _validate_blueprint_payload(payload, style_reference_present=False)
    assert excinfo.value.code == "SCENE_BLUEPRINT_INVALID"
    assert excinfo.value.details == {"field": "ending_action", "reason": "none_requires_style_reference"}
    with pytest.raises(DomainError):
        _validate_blueprint_payload(payload)
    # 其它字段的「无」不受此规则影响（维持既有非空校验）；空串照样拒绝
    other = {field: f"{field} 内容" for field in SCENE_BLUEPRINT_FIELDS}
    other["visible_desire"] = "无"
    assert _validate_blueprint_payload(other)["visible_desire"] == "无"
    other["ending_action"] = "  "
    with pytest.raises(DomainError):
        _validate_blueprint_payload(other, style_reference_present=True)


# ---------------------------------------------------------------------------
# B4(b) · 章规划上下文 + 章规划提示词
# ---------------------------------------------------------------------------


def test_chapter_planning_context_carries_the_style_reference_slot(session) -> None:
    from novel_system.services.chapter_plan_llm import _render_user_prompt
    from novel_system.services.chapter_planning_context import ChapterPlanningContextBuilder

    _seed_scene(session)
    context = ChapterPlanningContextBuilder(session).build(PROJECT_ID, CHAPTER_ID)
    assert "style_reference" not in context.prompt_payload
    assert "style_reference" not in context.degraded_slots  # 无绑定是常态，不是降级
    baseline_fingerprint = context.context_fingerprint

    _seed_style_binding(session, project_id=PROJECT_ID, profile_json=_profile_json_with_structure(), seed="cp")
    context = ChapterPlanningContextBuilder(session).build(PROJECT_ID, CHAPTER_ID)
    slot = context.prompt_payload["style_reference"]
    assert slot["profile_id"] == "sr_profile_cp"
    assert slot["structure_card"].startswith("[结构画像]") and "共 5 章" in slot["structure_card"]
    assert SAMPLES_BOUNDARY in slot["structure_samples"]
    assert slot["planning_guidance"].startswith("[场景手法]")
    assert "不复用" in slot["how_to_use"]
    assert len(context.source_version_refs["style_reference_runtime_contract_hash"]) == 64
    assert context.source_version_refs["style_reference_profile_id"] == "sr_profile_cp"
    assert context.context_fingerprint != baseline_fingerprint

    # 四个章规划节点共用 _render_user_prompt：画像按原样多行渲染在 JSON 载荷之后
    for name in ("chapter_story_architecture", "chapter_scene_plan_candidates", "chapter_scene_plan_fill", "chapter_plan_review"):
        prompt = _render_user_prompt(load_prompt_templates()[name], context.prompt_payload)
        json_part = prompt.split("Working payload:\n", 1)[1].split("\n\n", 1)[0]
        assert '"style_reference"' not in json_part and '"chapter_card"' in json_part
        assert "Reference author structure (style_reference" in prompt
        assert "\n[结构画像]" in prompt and f"\n{SAMPLES_BOUNDARY}\n" in prompt and "\n[场景手法]" in prompt
        assert prompt.index("[结构画像]") < prompt.index("Required top-level JSON keys")
    plain = _render_user_prompt(load_prompt_templates()["chapter_plan_review"], {"chapter_card": {"no": 1}})
    assert "Reference author structure" not in plain


# ---------------------------------------------------------------------------
# B4(c) · 雪花场景步载荷成员与降载次序
# ---------------------------------------------------------------------------


@pytest.fixture()
def captured_snowflake_requests(monkeypatch):
    from novel_system.services import snowflake_workspace_llm as mod

    captured: list = []

    def fake_execute(session, client, request, context, *, llm_call_id):  # noqa: ANN001
        captured.append(request)
        prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
        if '"step_key":"scene_list"' in prompt:
            payload = {
                "scenes": [
                    {
                        "scene_seq": 1,
                        "summary": "老周夜查账本",
                        "primary_form": "proactive",
                        "scene_type": "proactive",
                        "location": "账房",
                        "crucible": "退无可退",
                        "chapter_role": "起疑",
                        "pov_character_id": "LZ",
                    }
                ]
            }
        else:
            payload = {"summary": "老周为一本对不上的账本赔上后半生。"}
        return LLMResponse(
            request_id="r", provider="p", model="m", text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload, response_format="json_object", raw_response={}, usage={}, finish_reason="stop",
        )

    monkeypatch.setattr(mod, "execute_accounted_call", fake_execute)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_supplement_accounted_call", lambda self, **kwargs: None)
    return captured


def _seed_snowflake_project(session, project_id: str) -> None:
    session.add(
        StoryProject(
            project_id=project_id, title="账本", outline_text="账本", planning_mode="snowflake",
            snowflake_workflow_mode="explore", target_word_count=100000,
        )
    )
    session.flush()
    drafts = {
        "one_sentence_summary": {"summary": "老周为一本对不上的账本赔上后半生。"},
        "one_paragraph_summary": {"sentences": ["老周发现账本对不上", "东家逼他签字", "他把账本烧了"], "moral_premise": "沉默的代价"},
        "character_sheets": {"characters": [{"character_id": "LZ", "display_name": "老周", "role": "主角", "goal": "保住账房"}]},
        "long_synopsis": {"paragraphs": ["第一幕：账本", "第二幕：逼签", "第三幕：火"]},
    }
    for step_key, draft in drafts.items():
        session.add(
            SnowflakeStepRun(
                step_run_id=f"{project_id}-{step_key}", project_id=project_id, step_key=step_key,
                version=1, status="approved", draft_json=draft, health_json={}, input_refs_json={},
            )
        )
    session.commit()


def _snowflake_prompt_payload(request) -> dict:
    prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
    body = prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0]
    return json.loads(body)


def test_snowflake_scene_steps_carry_the_structure_reference(session, captured_snowflake_requests) -> None:
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
    from novel_system.services.snowflake_workspace_llm import _STRUCTURE_REFERENCE_STEPS

    assert _STRUCTURE_REFERENCE_STEPS == {"scene_list", "scene_details"}
    project_id = "prj-structure"
    _seed_snowflake_project(session, project_id)
    _seed_style_binding(session, project_id=project_id, profile_json=_profile_json_with_structure(), seed="snow")

    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    payload = _snowflake_prompt_payload(captured_snowflake_requests[-1])
    member = payload["style_reference_structure"]
    assert member["profile_id"] == "sr_profile_snow"
    assert member["structure_card"].startswith("[结构画像]") and "共 5 章" in member["structure_card"]
    assert SAMPLES_BOUNDARY in member["structure_samples"]
    assert member["planning_guidance"].startswith("[场景手法]")
    assert "不复用" in member["how_to_use"]
    assert payload["step_key"] == "scene_list"

    # 非排场步看不到它
    SnowflakeWorkspaceService(session).generate_step(project_id, "one_sentence_summary", {})
    payload = _snowflake_prompt_payload(captured_snowflake_requests[-1])
    assert payload["step_key"] == "one_sentence_summary"
    assert "style_reference_structure" not in payload


def test_snowflake_scene_list_without_binding_has_no_member(session, captured_snowflake_requests) -> None:
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    project_id = "prj-structure-none"
    _seed_snowflake_project(session, project_id)
    SnowflakeWorkspaceService(session).generate_step(project_id, "scene_list", {})
    assert "style_reference_structure" not in _snowflake_prompt_payload(captured_snowflake_requests[-1])


def test_snowflake_budget_sheds_samples_then_the_card_before_story_material() -> None:
    from novel_system.services.snowflake_prompt_budget import (
        STYLE_REFERENCE_STRUCTURE_KEY,
        _ladder,
        apply_snowflake_prompt_budget,
        estimate_payload_tokens,
    )

    names = [name for name, _ in _ladder("scene_list")]
    assert names.index("reference_scenes_to_identity") < names.index("drop_style_reference_samples")
    assert names.index("drop_style_reference_samples") + 1 == names.index("drop_style_reference_structure")
    assert names.index("drop_style_reference_structure") < names.index("truncate_long_prose")
    assert names.index("truncate_long_prose") < names.index("drop_reference_scenes")

    reference = {
        "profile_id": "sr_profile_x",
        "how_to_use": "只学结构与手法。",
        "structure_card": "[结构画像]\n- 章节标记：有，共 5 章\n- 章长：中位 1,200 字",
        "structure_samples": SAMPLES_BOUNDARY + "\n章首样例：\n" + ("样例原文" * 400) + "\n[/UNTRUSTED_REFERENCE_DATA]",
        "planning_guidance": "[场景手法]\n- 对白：短促",
    }
    payload = {
        "step_key": "scene_list",
        "project": {"project_id": "p", "title": "账本"},
        "upstream_steps": [{"step_key": "long_synopsis", "draft": {"paragraphs": ["第一幕：账本"]}}],
        "current_draft": {"scenes": []},
        STYLE_REFERENCE_STRUCTURE_KEY: dict(reference),
    }
    total = estimate_payload_tokens(payload)
    without_samples = estimate_payload_tokens(
        {**payload, STYLE_REFERENCE_STRUCTURE_KEY: {k: v for k, v in reference.items() if k != "structure_samples"}}
    )
    without_member = estimate_payload_tokens({k: v for k, v in payload.items() if k != STYLE_REFERENCE_STRUCTURE_KEY})
    assert without_member < without_samples < total

    # 预算刚好容不下样例：只卸样例，画像 + 手法保留，并留一句说明
    shed, report = apply_snowflake_prompt_budget(payload, budget_tokens=without_samples + 40, step_key="scene_list")
    assert report["applied"] == ["drop_style_reference_samples"] and report["within_budget"] is True
    member = shed[STYLE_REFERENCE_STRUCTURE_KEY]
    assert "structure_samples" not in member
    assert member["structure_card"] == reference["structure_card"]
    assert member["planning_guidance"] == reference["planning_guidance"]
    assert "样例因输入预算省略" in member["structure_samples_note"]
    assert shed["upstream_steps"] == payload["upstream_steps"]

    # 再紧一点：整张画像让位，本书材料与本步契约原样
    shed, report = apply_snowflake_prompt_budget(payload, budget_tokens=without_member + 5, step_key="scene_list")
    assert report["applied"] == ["drop_style_reference_samples", "drop_style_reference_structure"]
    assert report["within_budget"] is True
    assert STYLE_REFERENCE_STRUCTURE_KEY not in shed
    assert shed["upstream_steps"] == payload["upstream_steps"]
    assert shed["step_key"] == "scene_list" and shed["project"] == payload["project"]

    # 没有这个成员时两级都是空转，不谎报 applied
    plain = {k: v for k, v in payload.items() if k != STYLE_REFERENCE_STRUCTURE_KEY}
    _shed, report = apply_snowflake_prompt_budget(plain, budget_tokens=1, step_key="scene_list")
    assert "drop_style_reference_samples" not in report["applied"]
    assert "drop_style_reference_structure" not in report["applied"]


# ---------------------------------------------------------------------------
# B4(d) · 七个规划模板
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "version"),
    [
        ("scene_blueprint", "2026-09-13.v7"),
        ("chapter_story_architecture", "2026-09-12.v3"),
        ("chapter_scene_plan_candidates", "2026-09-12.v2"),
        ("chapter_scene_plan_fill", "2026-09-12.v3"),
        ("chapter_plan_review", "2026-09-12.v2"),
        ("snowflake_generate_scene_list", "2026-09-13.v8"),
        ("snowflake_generate_scene_details", "2026-09-13.v9"),
    ],
)
def test_planning_templates_are_bumped_and_follow_the_reference_structure(name: str, version: str) -> None:
    template = load_prompt_templates()[name]
    assert template.version == version
    assert "[结构画像]" in template.task_prompt and "[场景手法]" in template.task_prompt
    assert "characters, places, events, or sentences" in template.task_prompt
    assert "closing moves" in template.task_prompt
