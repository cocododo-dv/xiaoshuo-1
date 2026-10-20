"""风格对照进场景诊断（2026-10-20）：对照检查做完不再是死胡同。

实库：同一场（CH01_SC10）的对照检查给了 15 条「在哪里、怎么改」，界面一条没显示，写作台深改里也一条没有——检查的读数
记在风格参考自己的表里，场景诊断不认。现在这一场按绑定画像最近一次对照检查（几次取中位）里低于及格线、给了改法的维，
各成一条 ``source=style`` 的发现：钉到改法引的那句原文所在的编辑器段落，改稿后对不上的标「已过时」，忽略按 signal id。
"""

from __future__ import annotations

import uuid

from novel_system.db.models import StyleFidelityReading, utcnow
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_diagnosis.vocabulary import DIAGNOSIS_SOURCES, STYLE_FINDING_READING_SOURCE
from novel_system.services.style_reference import readings
from tests.support.style_first_fixtures import bound_scene

DRAFT = (
    "<p>闸门拉开的时候，雨城的潮气扑了林昭一脸。</p>"
    "<p>许望的眼神冷得像两枚铁钉，盯着案卷上那行字。</p>"
    "<p>那封旧信，就是在这间档案室里不见的。</p>"
)


def _dim(score, *, fix="", note="", applicable=True, raw=None) -> dict:
    entry = {"score": score, "raw": raw if raw is not None else score, "note": note, "applicable": applicable}
    if fix:
        entry["fix"] = fix
    return entry


JUDGE = {
    "kind": "reference_judge",
    "overall": 6.4,
    "raw_overall": 7.0,
    "pass_score": 6.0,
    "samples": 3,
    "calibration": {"state": "calibrated"},
    "summary": "整体仍偏向严肃写实。",
    "dimensions": {
        # 低于及格线、给了改法、引的原文在第 3 段
        "theme.emotional_tone": _dim(5.6, note="说到旧信时语调忽然庄重。", fix="在「那封旧信」处，用一句自嘲遮掩心酸。"),
        # 低于及格线、引的原文在第 2 段
        "narrative.perspective": _dim(4.8, note="又滑回了正经的旁观视角。", fix="在「许望的眼神冷得像两枚铁钉」处，换成他对自己逞能的自嘲。"),
        # 低于及格线、引的不是原文（评审转述了）：仍是一条，只是没钉到段落
        "theme.motifs": _dim(3.9, note="缺少反差意象。", fix="在「林昭出场」处补一笔旧雨伞。"),
        # 达标：不进清单
        "language.vocabulary": _dim(7.1, note="口语词用得好。", fix="句末多垫几个语气词。"),
        # 低于及格线但没给改法：不进清单（无从下手的不算一条）
        "scene.dialogue": _dim(5.0, note="对白像汇报。"),
        # 这一维分不清（作数的分为空）：不进清单
        "scene.environment": _dim(None, raw=5.0, note="环境偏纪实。", fix="加一段雨声。"),
    },
}


def _seed(session, key: str, *, draft: str = DRAFT):
    scene, _bundle, _book_id, profile_id = bound_scene(session, key)
    service = AuthorDraftService(session)
    current = service.ensure("scene", scene.scene_id, actor_ref="writer")["draft"]
    service.save(current["draft_id"], {"content": draft, "base_revision_no": current["revision_no"]}, actor_ref="writer")
    session.commit()
    return scene, profile_id


def _add_check(session, scene, profile_id, *, text: str = DRAFT, judge: dict = JUDGE, source: str = STYLE_FINDING_READING_SOURCE):
    row = StyleFidelityReading(
        reading_id=f"sfr_{uuid.uuid4().hex[:16]}",
        project_id=scene.project_id,
        scene_id=scene.scene_id,
        profile_id=profile_id,
        source=source,
        stage="manual" if source == STYLE_FINDING_READING_SOURCE else "final",
        draft_ref="author_draft:test",
        text_sha256=readings.text_sha256(text),
        char_count=len(text),
        percentile=72.0,
        distance=0.4,
        reading_json={"within_range": True, "visible_sha256": readings.visible_text_sha256(text)},
        judge_json=judge,
        created_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return row


def _style(payload: dict) -> list[dict]:
    return [finding for finding in payload["findings"] if finding["source"] == "style"]


def test_the_style_source_reads_check_readings_only() -> None:
    assert "style" in DIAGNOSIS_SOURCES
    assert STYLE_FINDING_READING_SOURCE == readings.SOURCE_MANUAL_CHECK


def test_a_check_below_the_pass_line_becomes_findings_pinned_to_the_writer_paragraphs(session) -> None:
    scene, profile_id = _seed(session, "styfind")
    reading = _add_check(session, scene, profile_id)
    payload = SceneDiagnosisService(session).payload(scene.scene_id)

    found = {finding["dimension"]: finding for finding in _style(payload)}
    assert set(found) == {"theme.emotional_tone", "narrative.perspective", "theme.motifs"}, "达标 / 没改法 / 分不清的维不出"
    tone = found["theme.emotional_tone"]
    assert tone["label"] == "情感基调" and tone["severity"] == "revision"
    assert tone["issue"] == "说到旧信时语调忽然庄重。" and tone["recommendation"].startswith("在「那封旧信」处")
    assert tone["evidence"]["paragraph_index"] == 2 and tone["evidence"]["excerpt"] == "那封旧信"
    assert tone["evidence"]["start"] == 0 and tone["stale"] is False
    assert "评审 3 次取中位" in tone["why"] and tone["origin"]["reading_id"] == reading.reading_id
    assert found["narrative.perspective"]["evidence"]["paragraph_index"] == 1
    assert found["theme.motifs"]["evidence"] is None and found["theme.motifs"]["stale"] is False, "转述的引文钉不到段，但检查是对着这份字的"

    block = payload["style"]
    assert block["status"] == "current" and block["bound"] is True
    assert block["overall"] == 6.4 and block["samples"] == 3 and block["judge_state"] == "calibrated"
    assert payload["summary"]["by_source"]["style"] == 3
    # 同一份字再查一次、改法指着同一处：signal id 不变（忽略过的仍是忽略）
    _add_check(session, scene, profile_id)
    again = {finding["dimension"]: finding["signal_id"] for finding in _style(SceneDiagnosisService(session).payload(scene.scene_id))}
    assert again["theme.emotional_tone"] == tone["signal_id"]


def test_after_an_edit_the_check_is_stale_and_lost_quotes_are_marked(session) -> None:
    scene, profile_id = _seed(session, "stystale")
    _add_check(session, scene, profile_id)
    service = AuthorDraftService(session)
    current = service.ensure("scene", scene.scene_id, actor_ref="writer")["draft"]
    edited = DRAFT.replace("许望的眼神冷得像两枚铁钉", "许望骂了一句，眼睛却没离开案卷")
    service.save(current["draft_id"], {"content": edited, "base_revision_no": current["revision_no"]}, actor_ref="writer")
    session.commit()

    payload = SceneDiagnosisService(session).payload(scene.scene_id)
    assert payload["style"]["status"] == "stale"
    found = {finding["dimension"]: finding for finding in _style(payload)}
    assert found["theme.emotional_tone"]["stale"] is False and found["theme.emotional_tone"]["evidence"]["paragraph_index"] == 2
    assert found["narrative.perspective"]["stale"] is True and found["narrative.perspective"]["evidence"] is None


def test_saving_the_same_visible_text_as_html_keeps_the_check_current(session) -> None:
    """写作台把终稿的纯文本存成 <p> 段落：原文哈希变了，可见文字没变——仍是对着现在这份字的检查。"""
    plain = "闸门拉开的时候，雨城的潮气扑了林昭一脸。\n那封旧信，就是在这间档案室里不见的。"
    scene, profile_id = _seed(session, "styhtml", draft="<p>闸门拉开的时候，雨城的潮气扑了林昭一脸。</p><p>那封旧信，就是在这间档案室里不见的。</p>")
    _add_check(session, scene, profile_id, text=plain)
    payload = SceneDiagnosisService(session).payload(scene.scene_id)
    assert payload["style"]["status"] == "current"


def test_ignored_style_findings_stop_counting_and_other_sources_are_untouched(client, session) -> None:
    scene, profile_id = _seed(session, "styignore")
    _add_check(session, scene, profile_id)
    payload = client.get(f"/api/v1/scenes/{scene.scene_id}/deep-review").json()["data"]
    target = next(finding for finding in _style(payload) if finding["dimension"] == "theme.motifs")
    response = client.patch(
        f"/api/v1/scenes/{scene.scene_id}/deep-review/preferences",
        json={"ignored_issue_keys": [target["signal_id"]], "base_revision_no": payload["preferences"]["revision_no"]},
    )
    assert response.status_code == 200, response.text
    after = client.get(f"/api/v1/scenes/{scene.scene_id}/deep-review").json()["data"]
    ignored = next(finding for finding in _style(after) if finding["signal_id"] == target["signal_id"])
    assert ignored["ignored"] is True and after["summary"]["by_source"]["style"] == 2


def test_only_checks_of_the_bound_profile_count_and_a_single_pipeline_judgement_does_not(session) -> None:
    scene, profile_id = _seed(session, "styscope")
    _add_check(session, scene, profile_id, source=readings.SOURCE_PIPELINE)
    payload = SceneDiagnosisService(session).payload(scene.scene_id)
    assert _style(payload) == [] and payload["style"]["status"] == "not_run", "起草流程的评审只问一次，不进清单"
    _add_check(session, scene, "sr_profile_someone_else")
    assert _style(SceneDiagnosisService(session).payload(scene.scene_id)) == [], "换过画像：旧画像的检查不算"
    unusable = {**JUDGE, "calibration": {"state": "unusable"}}
    _add_check(session, scene, profile_id, judge=unusable)
    payload = SceneDiagnosisService(session).payload(scene.scene_id)
    assert _style(payload) == [] and payload["style"]["status"] == "current", "评审不可用：检查算做过，但它的分不出发现"
