"""场景诊断统一（2026-09-22）：一场正文「哪里有问题」只有一份记录。

覆盖：作者稿 HTML → 段落；规则 / 节奏发现的统一形状、中文、稳定 id 与段落定位；忽略清单按
signal_id 生效并反向作用到文学质量视图与成稿门；AI 深评拒绝式、结果并入、改稿后标 stale；
准定稿评审并入；改写请求带发现（类别 / 策略 / 标签按维度）。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneDraft,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
    WriterEvaluation,
)
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.literary_quality import (
    DIMENSION_LABELS,
    QUALITY_DIMENSIONS,
    analyze_literary_quality,
    describe_rule_finding,
    ignored_rule_dimensions,
    rule_signal_id,
)
from novel_system.services.llm_client import LLMResponse, OnlineAccountedExecution
from novel_system.services.near_final import NEAR_FINAL_RUBRIC_ID as PIPELINE_NEAR_FINAL_RUBRIC_ID
from novel_system.services.scene_diagnosis import (
    BoundProfile,
    CRAFT_ECHO_HABIT_PER_1K,
    CRAFT_SAME_OPENING_HABIT_PER_1K,
    LITERARY_REVISION_PASSAGE_RUBRIC_ID,
    LITERARY_REVISION_RUBRIC_ID,
    NEAR_FINAL_RUBRIC_ID,
    DiagnosisText,
    SceneDiagnosisService,
    calibration_from_reference,
    candidate_category_for_dimension,
    compute_reference_craft,
    craft_findings,
    locate_in_paragraphs,
    manuscript_paragraphs,
    rule_findings,
)
from novel_system.services.writer_deep_review import WriterDeepReviewService

PROJECT_ID = "PROJECT_DIAG"
CHAPTER_ID = "DIAG_CH01"
SCENE_ID = "DIAG_CH01_SC01"

# 第 1 段：贴邻叠句「安静，安静」+ 模型腔「突然意识到」；第 3 段：连续三句以「她」开头 + 模型腔「她知道」
DRAFT_HTML = (
    "<p>门外很安静，安静到能听见潮水。她突然意识到，自己一直在等这一刻。</p>"
    "<p>许望没有回答。录音里传来三声钟响。</p>"
    "<p>她知道真相必须公开。她把证据袋压进袖口。她没有再看他。</p>"
)


def _seed_scene(session, *, draft_html: str | None = DRAFT_HTML) -> str | None:
    session.add(StoryProject(project_id=PROJECT_ID, title="诊断统一", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="她必须决定是否公开证据。",
            main_plot_push="把旧档案线推进到公开真相的选择。",
            emotional_target="从职业克制转向道德压力。",
            ending_effect="读者知道她已经不能只做修复师。",
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="她发现关键录音，但必须决定公开还是隐藏。",
            beats_json=["修复录音", "听见编号", "决定暂缓公开"],
            exit_change="她把一半证据藏起来。",
            hook="录音最后出现她自己的心跳声。",
        )
    )
    session.commit()
    if draft_html is None:
        return None
    service = AuthorDraftService(session)
    draft = service.ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]
    saved = service.save(draft["draft_id"], {"content": draft_html, "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    return saved["draft"]["draft_id"] if "draft" in saved else draft["draft_id"]


def _finding(payload: dict, source: str, dimension: str) -> dict:
    hits = [item for item in payload["findings"] if item["source"] == source and item["dimension"] == dimension]
    assert hits, f"expected a {source}:{dimension} finding, got {[(f['source'], f['dimension']) for f in payload['findings']]}"
    return hits[0]


# ---------------------------------------------------------------------------
# 正文 → 段落
# ---------------------------------------------------------------------------


def test_manuscript_paragraphs_follow_the_editor_block_list() -> None:
    html = "<p>第一段。</p><blockquote><p>引文里的一段。</p></blockquote><p>第三段 &amp; 实体。</p>"
    # querySelectorAll("p, blockquote") 的文档序：p、blockquote（textContent 含嵌套 p）、嵌套 p、p
    assert manuscript_paragraphs(html) == ["第一段。", "引文里的一段。", "引文里的一段。", "第三段 & 实体。"]
    assert manuscript_paragraphs("纯文本第一段\n\n第二段") == ["纯文本第一段", "第二段"]
    assert manuscript_paragraphs("<div>没有 p 的第一段</div><div>第二段</div>") == ["没有 p 的第一段", "第二段"]
    assert manuscript_paragraphs("") == []


def test_locate_in_paragraphs_prefers_exact_offsets_and_falls_back_to_the_paragraph() -> None:
    paragraphs = ["门外很安静，安静到能听见潮水。", "她突然  意识到什么。"]
    assert locate_in_paragraphs(paragraphs, "安静，安静") == {"excerpt": "安静，安静", "paragraph_index": 0, "start": 3, "end": 8}
    # 空白压缩后才对得上：只到段，不给偏移
    assert locate_in_paragraphs(paragraphs, "突然 意识到") == {"excerpt": "突然 意识到", "paragraph_index": 1, "start": None, "end": None}
    assert locate_in_paragraphs(paragraphs, "不在正文里") is None


# ---------------------------------------------------------------------------
# 规则 / 节奏发现的统一形状
# ---------------------------------------------------------------------------


def test_rule_findings_are_chinese_stable_and_pinned_to_paragraphs() -> None:
    text = DiagnosisText(layer="author_draft", ref="author_draft:x", content=DRAFT_HTML, paragraphs=manuscript_paragraphs(DRAFT_HTML))
    findings = rule_findings(text)
    by_dimension = {item["dimension"]: item for item in findings}

    voice = by_dimension["model_voice"]
    assert voice["source"] == "rules"
    assert voice["label"] == "模型腔"
    assert voice["severity"] == "revision"
    assert "突然意识到" in voice["issue"] or "她知道" in voice["issue"]
    assert voice["recommendation"] == "把抽象的「领悟」换成具体的选择、动作或感官后果。"
    assert voice["evidence"]["paragraph_index"] in {0, 2}
    paragraph = text.paragraphs[voice["evidence"]["paragraph_index"]]
    assert paragraph[voice["evidence"]["start"] : voice["evidence"]["end"]] == voice["evidence"]["excerpt"]
    assert voice["signal_id"].startswith("rules:model_voice:") and len(voice["signal_id"].split(":")[-1]) == 8
    assert voice["quality_signal_id"] == voice["signal_id"]
    assert voice["patch"]["candidate_category"] == "de_model_voice"

    # 有位置的发现都钉在段落上；整场缺席的（anchor=scene）没有位置，id 钉在 scene 上
    for item in findings:
        assert (item["evidence"] is None) == (item["anchor"] == "scene"), item["signal_id"]
        if item["anchor"] == "scene":
            assert item["signal_id"] == f"rules:{item['dimension']}:scene"

    # 一段没有抉择也没有收尾动作的文字：缺席发现无处可钉，结尾类钉在最后一段
    flat = DiagnosisText(layer="author_draft", ref=None, content="", paragraphs=["门外很安静。", "潮水退了。一切都变得不一样了。"])
    flat_findings = {item["dimension"]: item for item in rule_findings(flat)}
    assert flat_findings["no_choice_scene"]["evidence"] is None
    assert flat_findings["no_choice_scene"]["signal_id"] == "rules:no_choice_scene:scene"
    ending = flat_findings["summary_ending"]
    assert ending["anchor"] == "ending"
    assert ending["evidence"]["paragraph_index"] == 1
    # 命中的是词表里先到的那个词（「一切都」或「一切都变得」）：都钉在结尾句上
    assert "一切都变得不一样了".startswith(ending["evidence"]["excerpt"]) and len(ending["evidence"]["excerpt"]) >= 3

    # 稳定：同一句在别处多了字，id 不变
    moved = DiagnosisText(layer="author_draft", ref="author_draft:x", content="", paragraphs=["开头多了一段。", *text.paragraphs])
    moved_ids = {item["dimension"]: item["signal_id"] for item in rule_findings(moved)}
    assert moved_ids["model_voice"] == voice["signal_id"]


def test_rule_signal_id_and_chinese_description_cover_every_dimension() -> None:
    assert set(DIMENSION_LABELS) == set(QUALITY_DIMENSIONS)
    for dimension in QUALITY_DIMENSIONS:
        finding = {"dimension": dimension, "issue": "en", "recommendation": "en", "needle": "手", "anchor": "text"}
        issue, fix = describe_rule_finding(finding)
        assert issue and "{needle}" not in issue
        assert rule_signal_id(finding).startswith(f"rules:{dimension}:")
    # 没有 needle 的问题句不留空括号
    issue, _ = describe_rule_finding({"dimension": "model_voice", "needle": "", "anchor": "scene"})
    assert "「」" not in issue and "{needle}" not in issue


def test_craft_findings_port_the_writer_rules_and_defer_paragraph_scale_to_a_reference() -> None:
    # 171 个互不相同的字：够长，但没有可当作叠句的重复
    long_paragraph = "".join(chr(0x4E00 + index) for index in range(171))
    paragraphs = ["门外很安静，安静到能听见潮水。", long_paragraph, "她走了。她停了。她笑了。"]
    text = DiagnosisText(layer="author_draft", ref=None, content="", paragraphs=paragraphs)
    findings = {item["dimension"]: item for item in craft_findings(text)}
    assert findings["adjacent_echo"]["evidence"] == {"excerpt": "安静，安静", "paragraph_index": 0, "start": 3, "end": 8}
    assert findings["adjacent_echo"]["label"] == "贴邻叠句"
    assert findings["long_paragraph"]["issue"] == "第 2 段偏长（171 字）。"
    assert findings["same_opening"]["issue"] == "连续三句以「她」开头。"
    assert findings["same_opening"]["evidence"]["paragraph_index"] == 2
    # 有风格绑定但没有可校准的读数（默认阈值）：三条都在，标 house_taste
    bound = {item["dimension"]: item for item in craft_findings(text, house_taste=True)}
    assert set(bound) == {"adjacent_echo", "long_paragraph", "same_opening"}
    assert all(item["house_taste"] for item in bound.values())


def test_candidate_category_knows_both_vocabularies() -> None:
    assert candidate_category_for_dimension("model_voice") == "de_model_voice"
    assert candidate_category_for_dimension("dialogue_subtext") == "dialogue_rewrite"
    assert candidate_category_for_dimension("expository_dialogue") == "dialogue_rewrite"
    assert candidate_category_for_dimension("summary_ending") == "ending_pressure"
    assert candidate_category_for_dimension("adjacent_echo") == "action_replace"
    assert candidate_category_for_dimension("author_instruction") == "local_patch"


def test_near_final_rubric_id_is_pinned_to_the_pipeline_constant() -> None:
    assert NEAR_FINAL_RUBRIC_ID == PIPELINE_NEAR_FINAL_RUBRIC_ID


# ---------------------------------------------------------------------------
# 载荷 + 忽略清单
# ---------------------------------------------------------------------------


def test_deep_review_get_is_the_unified_scene_diagnosis(client: TestClient, session) -> None:
    draft_id = _seed_scene(session)

    response = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["text"] == {
        "layer": "author_draft",
        "ref": f"author_draft:{draft_id}",
        "sha256": payload["text"]["sha256"],
        "paragraph_count": 3,
        "chars": payload["text"]["chars"],
    }
    assert payload["style_bound"] is False
    sources = {item["source"] for item in payload["findings"]}
    assert sources == {"rules", "craft"}
    echo = _finding(payload, "craft", "adjacent_echo")
    assert echo["evidence"] == {"excerpt": "安静，安静", "paragraph_index": 0, "start": 3, "end": 8}
    assert _finding(payload, "craft", "same_opening")["evidence"]["paragraph_index"] == 2
    assert payload["summary"]["open"] == len(payload["findings"])
    assert payload["summary"]["ignored"] == 0
    assert payload["ai"]["status"] == "not_run"
    assert payload["review"]["status"] == "not_run"
    assert payload["preferences"] == {"revision_no": 0, "decision_log": [], "ignored_issue_keys": []}
    # 排序：严重度在前，同级按段落
    ranks = {"blocking": 0, "revision": 1, "taste": 2, "info": 3}
    assert [ranks[item["severity"]] for item in payload["findings"]] == sorted(ranks[item["severity"]] for item in payload["findings"])


def test_ignoring_a_finding_in_the_writer_hides_it_everywhere(client: TestClient, session) -> None:
    _seed_scene(session)
    diagnosis = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    voice = _finding(diagnosis, "rules", "model_voice")

    saved = client.patch(
        f"/api/v1/scenes/{SCENE_ID}/deep-review/preferences",
        json={"decision_log": [{"at": 1, "text": "忽略 · 模型腔"}], "ignored_issue_keys": [voice["signal_id"]], "base_revision_no": 0},
    )
    assert saved.status_code == 200

    # 深改面板：发现还在，标 ignored，不计入 open
    after = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    flagged = _finding(after, "rules", "model_voice")
    assert flagged["ignored"] is True
    assert after["summary"]["ignored"] == 1
    assert after["summary"]["open"] == diagnosis["summary"]["open"] - 1
    assert after["preferences"]["ignored_issue_keys"] == [voice["signal_id"]]

    # 文学质量视图：同一个 id，忽略过的不再列出，但说明有几条被忽略
    overview = client.get("/api/v1/literary-quality/overview", params={"project_id": PROJECT_ID}).json()["data"]
    item = next(entry for entry in overview["items"] if entry["object_type"] == "scene" and entry["object_id"] == SCENE_ID)
    assert item["ignored_count"] == 1
    assert item["ignored_findings"] == [{"signal_id": voice["signal_id"], "dimension": "model_voice", "label": "模型腔"}]
    assert voice["signal_id"] not in {finding["signal_id"] for finding in item["findings"]}
    assert "model_voice" not in item["open_dimensions"]
    assert item["signals"]["model_voice"]["risk"] is True, "分数是文本的事实，忽略是作者的决定"
    diagnosis_rule_ids = {finding["signal_id"] for finding in diagnosis["findings"] if finding["source"] == "rules"}
    overview_ids = {finding["signal_id"] for finding in item["findings"]} | {voice["signal_id"]}
    assert overview_ids == diagnosis_rule_ids
    assert all(finding["issue"] and finding["label"] for finding in item["findings"]), "视图读服务端给的中文"
    assert item["recommended_next_action"]["signal_id"] in diagnosis_rule_ids


def test_ignored_dimensions_drop_the_final_gate_warning(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, SCENE_ID)
    plain = " ".join(manuscript_paragraphs(DRAFT_HTML))
    _, findings = analyze_literary_quality(plain)
    voice_ids = [rule_signal_id(item) for item in findings if item["dimension"] == "model_voice"]
    assert voice_ids

    gate = FinalTextGateService(session)
    before = gate._literary(scene, plain)
    assert "literary:model_voice" in {warning["issue_key"] for warning in before["warnings"]}

    scene.deep_review_ignored_keys_json = voice_ids
    session.flush()
    after = gate._literary(scene, plain)
    assert "literary:model_voice" not in {warning["issue_key"] for warning in after["warnings"]}
    assert after["ignored_dimensions"] == ["model_voice"]
    assert "model_voice" in after["risky_dimensions"], "审计仍记风险维度"
    assert ignored_rule_dimensions(plain, voice_ids) == {"model_voice"}
    # 只忽略了一条、同维度还有别的发现时，维度不算拍过板
    assert ignored_rule_dimensions(plain, voice_ids[:1]) == ({"model_voice"} if len(voice_ids) == 1 else set())


# ---------------------------------------------------------------------------
# AI 深评并入 + 时效
# ---------------------------------------------------------------------------


class _DeepReviewRunner:
    def __init__(self, session, **kwargs) -> None:
        self.session = session

    @property
    def provider_execution_mode(self):
        return "online"

    def run(self, **kwargs):
        assert kwargs["node_id"] == "writer_deep_review"
        prompt_text = kwargs["user_prompt"]
        assert "Scene Structure (Snowflake)" not in prompt_text or "Goal" in prompt_text
        return SimpleNamespace(
            llm_call_id="llm_call_diag_deep_review",
            response=SimpleNamespace(
                structured_output={
                    "overall_score": 0.58,
                    "scores": {"choice_pressure": 0.5, "voice_distinction": 0.7},
                    "findings": [
                        {
                            "lens": "story",
                            "dimension": "choice_pressure",
                            "severity": "revision",
                            "issue": "选择被说出来了，没有落成动作。",
                            "recommendation": "让她把证据袋交出去，或者锁起来。",
                            "evidence_excerpt": "她把证据袋压进袖口",
                            "why_it_matters": "读者要看见代价。",
                        },
                        {
                            "lens": "prose",
                            "dimension": "voice_distinction",
                            "severity": "taste",
                            "issue": "叙述声音偏平。",
                            "recommendation": "让句子长短跟着她的呼吸走。",
                            "evidence_excerpt": "这句话已经不在正文里了",
                            "why_it_matters": "声音是辨识度。",
                        },
                    ],
                    "revision_brief": [{"dimension": "choice_pressure", "classification": "revision", "action": "把选择落成动作。", "priority": "high"}],
                    "requires_human_review": False,
                    "lens_evaluations": [
                        {"lens": "story", "overall_score": 0.5, "scores": {"choice_pressure": 0.5}, "findings": [], "revision_brief": []}
                    ],
                }
            ),
        )


def test_ai_deep_review_merges_into_the_diagnosis_and_goes_stale_when_the_text_changes(client: TestClient, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _DeepReviewRunner)
    draft_id = _seed_scene(session)

    response = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["ai"]["status"] == "current"
    assert payload["ai"]["overall_score"] == 0.58
    assert payload["ai"]["revision_brief"][0]["action"] == "把选择落成动作。"
    # 模型只分了 story 一组；漏掉的 prose 镜头由顶层发现重建（_normalize_lens_evaluations）
    lenses = {item["lens"]: item for item in payload["ai"]["lenses"]}
    assert set(lenses) == {"story", "prose"}
    assert lenses["story"] == {"lens": "story", "label": "故事", "overall_score": 0.5}
    assert payload["status"] == "reviewed" and payload["latest_evaluation"]["evaluator_llm_call_id"] == "llm_call_diag_deep_review"

    pressure = _finding(payload, "ai", "choice_pressure")
    assert pressure["label"] == "抉择压力" and pressure["lens"] == "story"
    assert pressure["evidence"]["paragraph_index"] == 2 and pressure["stale"] is False
    assert pressure["why"] == "读者要看见代价。"
    assert pressure["origin"]["evaluation_id"] == payload["ai"]["evaluation_id"]
    assert pressure["patch"]["revision_strategy"] == "让她把证据袋交出去，或者锁起来。"
    voice = _finding(payload, "ai", "voice_distinction")
    assert voice["evidence"] is None and voice["stale"] is True
    assert voice["severity"] == "taste"
    assert payload["summary"]["stale"] == 1

    # 作者改了正文：整份深评标 stale（深改面板提示重新深评），规则发现照常按新正文算
    service = AuthorDraftService(session)
    current = service.ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]
    assert current["draft_id"] == draft_id
    service.save(current["draft_id"], {"content": "<p>门外很安静。她把证据袋交给了许望。</p>", "base_revision_no": current["revision_no"]}, actor_ref="writer")
    session.commit()

    after = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert after["ai"]["status"] == "stale"
    assert after["text"]["paragraph_count"] == 1
    assert _finding(after, "ai", "choice_pressure")["stale"] is True, "证据已经改掉了"

    # 再跑一次：旧的一轮退位，只剩一份 aggregate
    again = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review")
    assert again.status_code == 200
    session.expire_all()
    rows = session.query(WriterEvaluation).filter_by(object_type="scene", object_id=SCENE_ID, rubric_id=LITERARY_REVISION_RUBRIC_ID, parent_evaluation_id=None).all()
    assert sorted(row.status for row in rows) == ["completed", "superseded"]


def test_near_final_review_findings_join_the_diagnosis(client: TestClient, session) -> None:
    draft_id = _seed_scene(session)
    # 准定稿评审记的是管线稿行（source_draft:<scene_drafts.row_id>）；作者稿与它一字不差 → 评审是「对着这份字」的
    session.add(
        SceneDraft(
            row_id="sd_diag",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            stage="style_draft",
            content=DRAFT_HTML,
            source_bundle_id="bundle_diag",
            source_bundle_hash="hash_diag",
        )
    )
    session.add(
        WriterEvaluation(
            evaluation_id="near_final_eval_diag",
            object_type="scene",
            object_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            scene_id=SCENE_ID,
            rubric_id=NEAR_FINAL_RUBRIC_ID,
            source_text_ref="source_draft:sd_diag",
            lens="near_final_acceptance",
            overall_score=0.61,
            scores_json={},
            findings_json=[
                {
                    "dimension": "model_voice_risk",
                    "severity": "revision",
                    "issue": "准终稿仍保留模型腔：突然意识到。",
                    "recommendation": "把概括性判断改成动作。",
                    "evidence_excerpt": "突然意识到",
                    "evidence_location": "scene body",
                    "why_it_matters": "叙述不能替读者总结。",
                },
                {},
                {"severity": "taste", "evidence_excerpt": "", "issue": "", "recommendation": ""},
            ],
            revision_brief_json=[{"dimension": "model_voice_risk", "action": "去掉总结句。", "priority": "medium"}],
            failure_class="prose_model_voice",
            status="completed",
        )
    )
    session.commit()

    payload = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    review = _finding(payload, "review", "model_voice_risk")
    assert review["label"] == "模型腔"
    # 真实安装上的准定稿评审存过 [{}]（中转把空成员解码成 {}）：没有字的成员不成为一条「unknown」发现
    assert [item["dimension"] for item in payload["findings"] if item["source"] == "review"] == ["model_voice_risk"]
    assert review["evidence"]["paragraph_index"] == 0 and review["evidence"]["excerpt"] == "突然意识到"
    assert review["origin"]["evaluation_id"] == "near_final_eval_diag"
    assert payload["review"]["status"] == "current", "作者稿与评审看的管线稿一字不差"
    assert payload["review"]["failure_class"] == "prose_model_voice"
    assert payload["review"]["revision_brief"][0]["action"] == "去掉总结句。"
    assert payload["ai"]["status"] == "not_run", "准定稿评审不冒充 AI 深评"

    # 作者改了字：评审就是改前的了
    service = AuthorDraftService(session)
    current = service.ensure_blank("scene", SCENE_ID, actor_ref="writer")["draft"]
    assert current["draft_id"] == draft_id
    service.save(current["draft_id"], {"content": "<p>门外很安静。她把证据袋交给了许望。</p>", "base_revision_no": current["revision_no"]}, actor_ref="writer")
    session.commit()
    assert client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]["review"]["status"] == "stale"


# ---------------------------------------------------------------------------
# 改写请求带着发现
# ---------------------------------------------------------------------------


class _ScriptedPatchClient(OnlineAccountedExecution):
    def __init__(self) -> None:
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        output = {
            "patches": [
                {
                    "target_text_ref": "author_draft:test",
                    "source_excerpt": "她突然意识到，自己一直在等这一刻。",
                    "replacement_text": "她把手从门把上拿开。这一刻她等了很久。",
                    "patch_type": "replace_excerpt",
                    "changed_dimensions": ["model_voice"],
                    "why_it_helps": "把领悟换成动作。",
                },
                {
                    "target_text_ref": "author_draft:test",
                    "source_excerpt": "她突然意识到，自己一直在等这一刻。",
                    "replacement_text": "门把在她掌心里发烫。",
                    "patch_type": "replace_excerpt",
                    "changed_dimensions": ["model_voice", "perception_filter"],
                    "why_it_helps": "用物件承载停顿。",
                },
            ],
            "rationale": "保留事实。",
            "manual_only": True,
        }
        return LLMResponse(
            request_id=f"patch_req_{len(self.requests)}",
            provider="fake",
            model=request.model,
            text="{}",
            structured_output=output,
            response_format=request.response_format,
            raw_response={"id": "patch_req", "model": request.model},
            usage={"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
            finish_reason="stop",
        )

    def generate_accounted(self, request, *, accounting_hook):
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def test_patch_candidate_from_a_finding_learns_by_dimension_and_carries_the_instruction(session) -> None:
    draft_id = _seed_scene(session)
    diagnosis = SceneDiagnosisService(session).payload(SCENE_ID)
    voice = _finding(diagnosis, "rules", "model_voice")
    llm = _ScriptedPatchClient()

    result = WriterDeepReviewService(session, llm_client=llm).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "chapter_id": CHAPTER_ID,
            "scene_id": SCENE_ID,
            "source_draft_id": draft_id,
            "source_excerpt": "她突然意识到，自己一直在等这一刻。",
            "issue_dimension": voice["dimension"],
            "quality_signal_id": voice["signal_id"],
            "issue_note": voice["issue"],
            "instruction": voice["recommendation"],
        },
        actor_ref="writer",
    )

    candidate = result["candidate"]
    assert candidate["quality_signal_id"] == voice["signal_id"]
    assert candidate["issue_dimension"] == "model_voice"
    assert candidate["candidate_category"] == "de_model_voice"
    assert candidate["revision_strategy"] == voice["recommendation"]
    assert candidate["preference_tags"] == ["去模型腔", "少抽象总结"]
    user_prompt = llm.requests[-1].messages[-1]["content"]
    assert "Issue Dimension: model_voice" in user_prompt
    assert f"Diagnosed Issue: {voice['issue']}" in user_prompt
    assert f"Author Instruction: {voice['recommendation']}" in user_prompt
    assert "## Current Author Draft Context" in user_prompt and "许望没有回答" in user_prompt

    # 工具条的自由改写：维度是 author_instruction，画像记作者的那句话
    free = WriterDeepReviewService(session, llm_client=llm).create_patch_candidate(
        {
            "object_type": "scene",
            "object_id": SCENE_ID,
            "scene_id": SCENE_ID,
            "source_draft_id": draft_id,
            "source_excerpt": "许望没有回答。",
            "issue_dimension": "author_instruction",
            "instruction": "更凝练",
        },
        actor_ref="writer",
    )["candidate"]
    assert free["candidate_category"] == "local_patch"
    assert free["revision_strategy"] == "更凝练"
    assert free["preference_tags"] == ["更凝练"]
    assert free["quality_signal_id"] is None
    assert "Author Instruction: 更凝练" in llm.requests[-1].messages[-1]["content"]

    # 修补候选随诊断载荷返回（最近的在前）
    after = SceneDiagnosisService(session).payload(SCENE_ID)
    assert [row["patch_id"] for row in after["patch_candidates"]] == [free["patch_id"], candidate["patch_id"]]


def test_scene_without_text_diagnoses_nothing(client: TestClient, session) -> None:
    _seed_scene(session, draft_html=None)
    payload = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert payload["text"]["layer"] == "none"
    assert payload["findings"] == []
    assert payload["summary"]["open"] == 0


# ---------------------------------------------------------------------------
# 第二轮（同日）：节奏检查按参考作者校准
# ---------------------------------------------------------------------------


def _distinct_chars(count: int) -> str:
    return "".join(chr(0x4E00 + index) for index in range(count))


def test_reference_craft_calibration_raises_the_paragraph_limit_and_drops_the_authors_habits() -> None:
    reference = ["门外很安静，安静到能听见潮水。"] * 30 + [_distinct_chars(300)] * 10 + ["她走了。她停了。她笑了。"] * 5
    stats = compute_reference_craft(reference)
    assert stats["paragraphs"] == 45 and stats["long_paragraph_p95"] == 300
    assert stats["echo_per_1k"] > CRAFT_ECHO_HABIT_PER_1K and stats["same_opening_per_1k"] > CRAFT_SAME_OPENING_HABIT_PER_1K

    calibration = calibration_from_reference(profile_id="prof", book_id="book", book_title="龙族", stats=stats, deliberate_repetition=False)
    assert calibration.source == "reference"
    assert calibration.long_paragraph_chars == 300
    assert calibration.flag_echo is False and calibration.flag_same_opening is False
    assert "《龙族》" in calibration.note and "300" in calibration.note and "常这么写" in calibration.note

    text = DiagnosisText(layer="author_draft", ref=None, content="", paragraphs=["门外很安静，安静到能听见潮水。", _distinct_chars(200), "她走了。她停了。她笑了。"])
    assert {item["dimension"] for item in craft_findings(text)} == {"adjacent_echo", "long_paragraph", "same_opening"}
    assert craft_findings(text, calibration=calibration, house_taste=True) == []
    longer = DiagnosisText(layer="author_draft", ref=None, content="", paragraphs=[_distinct_chars(301)])
    flagged = craft_findings(longer, calibration=calibration)
    assert [item["dimension"] for item in flagged] == ["long_paragraph"]
    assert "超过参考作者段落的长尾 300 字" in flagged[0]["issue"]

    # 参考作者不常这么写：照常提示、阈值不低于 170；刻意重复的作者：两条重复检查都不提示
    sparse_stats = {"paragraphs": 100, "long_paragraph_p95": 120, "echo_per_1k": 1.0, "same_opening_per_1k": 2.0}
    sparse = calibration_from_reference(profile_id="p", book_id="b", book_title=None, stats=sparse_stats, deliberate_repetition=False)
    assert sparse.flag_echo and sparse.flag_same_opening and sparse.long_paragraph_chars == 170
    deliberate = calibration_from_reference(profile_id="p", book_id="b", book_title="书", stats=sparse_stats, deliberate_repetition=True)
    assert not deliberate.flag_echo and not deliberate.flag_same_opening and "刻意" in deliberate.note


def test_diagnosis_calibrates_craft_to_the_bound_reference_book(client: TestClient, session, monkeypatch) -> None:
    _seed_scene(session)
    reference = ["门外很安静，安静到能听见潮水。"] * 30 + ["她走了。她停了。她笑了。"] * 5 + [_distinct_chars(300)] * 10
    session.add(StyleReferenceBook(book_id="book_diag", title="龙族", source_kind="upload", cloud_policy="segments_only", text_checksum="diag"))
    session.add(StyleReferenceRun(run_id="run_diag", book_id="book_diag", status="completed", phase="synthesize", dispatch_state="completed", requested_layers_json=["language"]))
    session.add_all(
        [
            StyleReferenceParagraph(
                paragraph_id=f"para_diag_{index}",
                book_id="book_diag",
                paragraph_index=index,
                paragraph_type="narration",
                start_offset=0,
                end_offset=len(text),
                text=text,
                char_count=len(text),
            )
            for index, text in enumerate(reference)
        ]
    )
    session.add(
        StyleReferenceProfile(
            profile_id="prof_diag",
            book_id="book_diag",
            run_id="run_diag",
            title="龙族画像",
            profile_json={"voice_signature": {"deliberate_repetition": False}},
        )
    )
    session.commit()
    monkeypatch.setattr(
        SceneDiagnosisService,
        "binding_profile",
        lambda self, scene: (True, BoundProfile(profile_id="prof_diag", book_id="book_diag", deliberate_repetition=False)),
    )

    payload = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert payload["style_bound"] is True
    calibration = payload["craft_calibration"]
    assert calibration["source"] == "reference" and calibration["book_title"] == "龙族" and calibration["paragraphs"] == 45
    assert calibration["long_paragraph_chars"] == 300
    assert calibration["flag_echo"] is False and calibration["flag_same_opening"] is False
    assert calibration["note"].startswith("按《龙族》校准")
    assert not [item for item in payload["findings"] if item["source"] == "craft"], "叠句与句首重复是这位作者的习惯"
    assert all(item["house_taste"] for item in payload["findings"] if item["source"] == "rules")
    # 第二次读走进程缓存：结果一致
    assert client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]["craft_calibration"] == calibration


def test_binding_profile_resolves_the_most_specific_active_binding_without_loading_profile_json(session) -> None:
    """轻量解析（不加载几十万字的 profile_json）：scene > character > project > global，画像不 active 的绑定不算。"""

    _seed_scene(session)
    session.add(StyleReferenceBook(book_id="book_a", title="甲", source_kind="upload", cloud_policy="segments_only", text_checksum="a"))
    session.add(StyleReferenceBook(book_id="book_b", title="乙", source_kind="upload", cloud_policy="segments_only", text_checksum="b"))
    session.add(StyleReferenceRun(run_id="run_a", book_id="book_a", status="completed", phase="synthesize", dispatch_state="completed", requested_layers_json=[]))
    session.add(StyleReferenceRun(run_id="run_b", book_id="book_b", status="completed", phase="synthesize", dispatch_state="completed", requested_layers_json=[]))
    session.add(StyleReferenceProfile(profile_id="prof_a", book_id="book_a", run_id="run_a", title="甲画像", status="active", profile_json={"voice_signature": {"deliberate_repetition": True}}))
    session.add(StyleReferenceProfile(profile_id="prof_b", book_id="book_b", run_id="run_b", title="乙画像", status="active", profile_json={}))
    session.add(StyleReferenceProfile(profile_id="prof_draft", book_id="book_b", run_id="run_b", title="草稿画像", status="draft", profile_json={}))
    session.flush()
    session.add(StyleReferenceInjectionBinding(binding_id="bind_project", profile_id="prof_a", scope="project", scope_ref_id=PROJECT_ID, task_type="scene_generation", strategy="mixed", config_json={}, status="active"))
    session.commit()
    scene = session.get(SceneCard, SCENE_ID)

    bound, profile = SceneDiagnosisService(session).binding_profile(scene)
    assert bound is True and profile == BoundProfile(profile_id="prof_a", book_id="book_a", deliberate_repetition=True)

    # 场景层的绑定比项目层具体；画像还是草稿的绑定不算
    session.add(StyleReferenceInjectionBinding(binding_id="bind_scene_draft", profile_id="prof_draft", scope="scene", scope_ref_id=SCENE_ID, task_type="scene_generation", strategy="mixed", config_json={}, status="active"))
    session.commit()
    assert SceneDiagnosisService(session).binding_profile(scene)[1].profile_id == "prof_a"
    session.add(StyleReferenceInjectionBinding(binding_id="bind_scene", profile_id="prof_b", scope="scene", scope_ref_id=SCENE_ID, task_type="scene_generation", strategy="mixed", config_json={}, status="active"))
    session.commit()
    bound, profile = SceneDiagnosisService(session).binding_profile(scene)
    assert bound is True and profile == BoundProfile(profile_id="prof_b", book_id="book_b", deliberate_repetition=False)

    # 另一个任务类型 / 不 active 的绑定：不算
    session.query(StyleReferenceInjectionBinding).delete()
    session.add(StyleReferenceInjectionBinding(binding_id="bind_other", profile_id="prof_a", scope="project", scope_ref_id=PROJECT_ID, task_type="fine_tuning", strategy="mixed", config_json={}, status="active"))
    session.add(StyleReferenceInjectionBinding(binding_id="bind_off", profile_id="prof_a", scope="project", scope_ref_id=PROJECT_ID, task_type="scene_generation", strategy="mixed", config_json={}, status="archived"))
    session.commit()
    assert SceneDiagnosisService(session).binding_profile(scene) == (False, None)


# ---------------------------------------------------------------------------
# 「AI 看这一处」：局部深评
# ---------------------------------------------------------------------------


def _scripted_runner(output: dict, calls: list):
    class _Runner:
        def __init__(self, session, **kwargs) -> None:
            self.session = session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                llm_call_id=f"llm_call_scripted_{len(calls)}",
                response=SimpleNamespace(structured_output=output),
            )

    return _Runner


PASSAGE_OUTPUT = {
    "verdict": "does_not_hold",
    "assessment": "「突然意识到」在这里是人物对自己的一句嘲讽，不是叙述替读者总结。",
    "findings": [
        {
            "lens": "prose",
            "dimension": "information_rhythm",
            "severity": "taste",
            "issue": "钟响那一句来得太早。",
            "recommendation": "把钟响挪到她开口之后。",
            "evidence_excerpt": "录音里传来三声钟响",
            "why_it_matters": "信息释放的先后决定悬念。",
        }
    ],
    "rewrite_brief": "删掉「突然意识到」，让她直接做动作。",
}


def test_passage_review_verifies_one_finding_and_joins_the_diagnosis(client: TestClient, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    calls: list = []
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _scripted_runner(PASSAGE_OUTPUT, calls))
    _seed_scene(session)
    before = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    voice = _finding(before, "rules", "model_voice")

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/deep-review/passage",
        json={"signal_id": voice["signal_id"], "question": "这是自嘲吗？"},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    review = payload["passage_review"]
    assert review["verdict"] == "does_not_hold" and review["verdict_label"] == "不成立"
    assert review["about_signal_id"] == voice["signal_id"]
    assert review["paragraph_index"] == voice["evidence"]["paragraph_index"]
    assert review["findings_count"] == 1 and review["status"] == "current"
    call = calls[-1]
    assert call["node_id"] == "writer_deep_review" and call["step"] == "writer_passage_review"
    # 段落窗口真的在用户消息里（不只是模板里提到「焦点段」）：焦点段 + 下一段，标着记号，没有 HTML
    assert "【焦点段】门外很安静" in call["user_prompt"] and "【上下文】许望没有回答" in call["user_prompt"]
    assert "## Finding To Verify" in call["user_prompt"] and "## Author's Question" in call["user_prompt"] and "这是自嘲吗" in call["user_prompt"]
    assert "<p>" not in call["user_prompt"], "模型看的是可见文字，不是作者稿的 HTML"
    assert call["prompt"]["template_name"] == "writer_passage_review"

    verified = _finding(payload, "rules", "model_voice")
    assert verified["opinion"]["verdict"] == "does_not_hold"
    assert verified["opinion"]["rewrite_brief"] == "删掉「突然意识到」，让她直接做动作。"
    assert verified["opinion"]["assessment"].startswith("「突然意识到」在这里")
    rhythm = _finding(payload, "ai", "information_rhythm")
    assert rhythm["origin"]["kind"] == "passage" and rhythm["origin"]["about_signal_id"] == voice["signal_id"]
    assert rhythm["evidence"]["paragraph_index"] == 1
    assert len(payload["passage_reviews"]) == 1

    # 同一条再看一次：旧的退位，面板只留最新的意见
    again = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"signal_id": voice["signal_id"]})
    assert again.status_code == 200
    assert len(again.json()["data"]["passage_reviews"]) == 1
    session.expire_all()
    rows = session.query(WriterEvaluation).filter_by(object_type="scene", object_id=SCENE_ID, rubric_id=LITERARY_REVISION_PASSAGE_RUBRIC_ID).all()
    assert sorted(row.status for row in rows) == ["completed", "superseded"]
    # 全场深评没跑过，局部深评不冒充它
    assert again.json()["data"]["ai"]["status"] == "not_run"


def test_passage_review_on_a_paragraph_without_a_finding_and_its_guards(client: TestClient, session, monkeypatch) -> None:
    _seed_scene(session)
    denied = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_index": 1})
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "WRITER_DEEP_REVIEW_LLM_REQUIRED"

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    calls: list = []
    output = {"verdict": "holds", "assessment": "这一段没有要改的。", "findings": [], "rewrite_brief": ""}
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _scripted_runner(output, calls))

    bad = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"paragraph_index": 99})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "WRITER_PASSAGE_REVIEW_TARGET_INVALID"
    unknown = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"signal_id": "rules:model_voice:deadbeef"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "WRITER_PASSAGE_REVIEW_FINDING_NOT_FOUND"

    # 选中一句原话：按它找到段落；没有要复核的发现时 holds 归为 no_finding
    ok = client.post(f"/api/v1/scenes/{SCENE_ID}/deep-review/passage", json={"excerpt": "许望没有回答"})
    assert ok.status_code == 200
    review = ok.json()["data"]["passage_review"]
    assert review["paragraph_index"] == 1 and review["about_signal_id"] is None
    assert review["verdict"] == "no_finding" and review["verdict_label"] == "没有要改的"
    assert "## Finding To Verify" in calls[-1]["user_prompt"] and "(none" in calls[-1]["user_prompt"]
    assert "## Author's Question" not in calls[-1]["user_prompt"]
    assert "【焦点段】许望没有回答" in calls[-1]["user_prompt"]


# ---------------------------------------------------------------------------
# 「AI 通读本章」：章级深评落到各场
# ---------------------------------------------------------------------------

SCENE2_ID = "DIAG_CH01_SC02"
SCENE2_HTML = "<p>许望把钟停了。</p><p>她终于开口，把证据袋放在桌上。</p>"


def _add_second_scene(session, html: str = SCENE2_HTML) -> str:
    session.add(
        SceneCard(
            scene_id=SCENE2_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=2,
            scene_goal="她开口。",
            beats_json=[],
            exit_change="",
            hook="",
        )
    )
    session.commit()
    service = AuthorDraftService(session)
    draft = service.ensure_blank("scene", SCENE2_ID, actor_ref="writer")["draft"]
    service.save(draft["draft_id"], {"content": html, "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    return draft["draft_id"]


def test_chapter_read_through_lands_findings_on_scenes_and_keeps_chapter_level_ones(client: TestClient, session) -> None:
    _seed_scene(session)
    _add_second_scene(session)
    session.add(
        WriterEvaluation(
            evaluation_id="chapter_eval_diag",
            object_type="chapter",
            object_id=CHAPTER_ID,
            chapter_id=CHAPTER_ID,
            scene_id=None,
            rubric_id=LITERARY_REVISION_RUBRIC_ID,
            source_text_ref=f"chapter_assembled:{CHAPTER_ID}",
            lens="aggregate",
            overall_score=0.52,
            scores_json={},
            findings_json=[
                {
                    "lens": "story",
                    "dimension": "choice_pressure",
                    "severity": "revision",
                    "issue": "第二场的选择说出来了，没有落成动作。",
                    "recommendation": "让证据袋真的离开她的手。",
                    "evidence_excerpt": "把证据袋放在桌上",
                    "why_it_matters": "读者要看见代价。",
                },
                {
                    "lens": "reader",
                    "dimension": "ending_drive",
                    "severity": "blocking",
                    "issue": "本章开头的承诺到结尾没有兑现。",
                    "recommendation": "让最后一场回答第一场的问题。",
                    "evidence_excerpt": "",
                    "why_it_matters": "章的收束。",
                },
            ],
            revision_brief_json=[{"action": "让最后一场回答第一场的问题。", "priority": "high"}],
            status="completed",
        )
    )
    session.commit()

    chapter = client.get(f"/api/v1/chapters/{CHAPTER_ID}/deep-review").json()["data"]
    assert chapter["ai"]["status"] == "current" and chapter["ai"]["overall_score"] == 0.52
    assert [entry["scene_id"] for entry in chapter["scenes"]] == [SCENE_ID, SCENE2_ID]
    second = chapter["scenes"][1]
    assert [item["dimension"] for item in second["findings_from_chapter"]] == ["choice_pressure"]
    assert second["findings_from_chapter"][0]["evidence"]["paragraph_index"] == 1
    assert second["findings_from_chapter"][0]["origin"]["kind"] == "chapter"
    assert [item["dimension"] for item in chapter["chapter_findings"]] == ["ending_drive"]
    assert chapter["chapter_findings"][0]["evidence"] is None and chapter["chapter_findings"][0]["stale"] is False
    assert chapter["summary"]["chapter_level"] == 1 and chapter["summary"]["blocking"] >= 1
    assert chapter["summary"]["scenes"] == 2
    assert chapter["status"] == "reviewed" and chapter["latest_evaluation"]["evaluation_id"] == "chapter_eval_diag"
    assert chapter["ai"]["revision_brief"][0]["action"] == "让最后一场回答第一场的问题。"

    # 写作台里第二场的诊断也有这一条（origin 通读）；第一场没有
    scene2 = client.get(f"/api/v1/scenes/{SCENE2_ID}/deep-review").json()["data"]
    landed = _finding(scene2, "ai", "choice_pressure")
    assert landed["origin"]["kind"] == "chapter" and landed["origin"]["evaluation_id"] == "chapter_eval_diag"
    assert scene2["chapter_review"] == {"status": "current", "evaluation_id": "chapter_eval_diag", "created_at": scene2["chapter_review"]["created_at"], "findings_here": 1}
    scene1 = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]
    assert not [item for item in scene1["findings"] if (item.get("origin") or {}).get("kind") == "chapter"]
    assert scene1["chapter_review"]["findings_here"] == 0

    # 改了第二场的字：整章的通读就是改前的
    service = AuthorDraftService(session)
    draft = service.ensure_blank("scene", SCENE2_ID, actor_ref="writer")["draft"]
    service.save(draft["draft_id"], {"content": "<p>她走了。</p>", "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    after = client.get(f"/api/v1/chapters/{CHAPTER_ID}/deep-review").json()["data"]
    assert after["ai"]["status"] == "stale"
    assert after["chapter_findings"] and all(item["stale"] for item in after["chapter_findings"] if item["context"]), "引的那句已经改掉"


def test_chapter_read_through_runs_the_node_on_scene_marked_plain_text(client: TestClient, session, monkeypatch) -> None:
    _seed_scene(session)
    _add_second_scene(session)
    denied = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review")
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "WRITER_DEEP_REVIEW_LLM_REQUIRED"

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    calls: list = []
    output = {
        "overall_score": 0.6,
        "scores": {"choice_pressure": 0.5},
        "findings": [
            {
                "lens": "story",
                "dimension": "choice_pressure",
                "severity": "revision",
                "issue": "第二场开头停钟的动作没有代价。",
                "recommendation": "让停钟惊动别人。",
                "evidence_excerpt": "许望把钟停了",
                "why_it_matters": "代价。",
            }
        ],
        "revision_brief": [{"dimension": "choice_pressure", "classification": "revision", "action": "让停钟惊动别人。", "priority": "medium"}],
        "requires_human_review": False,
        "lens_evaluations": [],
    }
    monkeypatch.setattr("novel_system.services.writer_deep_review.LLMNodeRunner", _scripted_runner(output, calls))

    response = client.post(f"/api/v1/chapters/{CHAPTER_ID}/deep-review")
    assert response.status_code == 200
    prompt = calls[-1]["user_prompt"]
    assert "【第 1 场】" in prompt and "【第 2 场】" in prompt and "<p>" not in prompt
    payload = response.json()["data"]
    assert payload["ai"]["status"] == "current" and payload["ai"]["evaluation_id"]
    assert payload["scenes"][1]["findings_from_chapter"][0]["evidence"]["paragraph_index"] == 0
    assert payload["chapter_findings"] == []


# ---------------------------------------------------------------------------
# 全书计数
# ---------------------------------------------------------------------------


def test_project_diagnosis_summary_counts_every_scene_and_chapter(client: TestClient, session) -> None:
    _seed_scene(session)
    _add_second_scene(session)

    summary = client.get(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary").json()["data"]
    scene1 = client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"]["summary"]
    assert summary["scenes"][SCENE_ID]["open"] == scene1["open"]
    assert summary["scenes"][SCENE_ID]["blocking"] == scene1["by_severity"]["blocking"]
    assert summary["scenes"][SCENE_ID]["chapter_id"] == CHAPTER_ID and summary["scenes"][SCENE_ID]["ai_status"] == "not_run"
    assert summary["chapters"][CHAPTER_ID]["scenes"] == 2
    assert summary["chapters"][CHAPTER_ID]["open"] == summary["scenes"][SCENE_ID]["open"] + summary["scenes"][SCENE2_ID]["open"]
    assert summary["totals"]["scenes"] == 2 and summary["totals"]["scenes_with_text"] == 2
    assert summary["totals"]["open"] == summary["chapters"][CHAPTER_ID]["open"]
    assert summary["totals"]["chapters_reviewed"] == 0

    # 在写作台忽略一条：计数跟着少一
    voice = _finding(client.get(f"/api/v1/scenes/{SCENE_ID}/deep-review").json()["data"], "rules", "model_voice")
    saved = client.patch(
        f"/api/v1/scenes/{SCENE_ID}/deep-review/preferences",
        json={"decision_log": [], "ignored_issue_keys": [voice["signal_id"]], "base_revision_no": 0},
    )
    assert saved.status_code == 200
    after = client.get(f"/api/v1/projects/{PROJECT_ID}/diagnosis-summary").json()["data"]
    assert after["scenes"][SCENE_ID]["open"] == summary["scenes"][SCENE_ID]["open"] - 1
    assert after["scenes"][SCENE_ID]["ignored"] == 1
    assert after["totals"]["ignored"] == 1

    assert client.get("/api/v1/projects/nope/diagnosis-summary").status_code == 404
