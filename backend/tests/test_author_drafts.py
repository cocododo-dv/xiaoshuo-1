from __future__ import annotations

import json

import pytest

from novel_system.db.session import SessionLocal
from novel_system.db.models import (
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorPreferenceProfile,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    LlmCall,
    ReviewItem,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.llm_client import LLMResponse
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.errors import DomainError
from tests.support.seed import seed_chapter, seed_project, seed_scene


@pytest.fixture(autouse=True)
def _online_author_llm(monkeypatch):
    """假生成已退役：作者稿 AI 建议/结构反提取统一走在线记账替身（按 node_id 派发）。

    单个 candidate_brief 同时带 scene 与 chapter 两套字段——结构反提取的归一化各取所需、
    忽略无关键，故场景稿/章节稿共用同一替身即可。显式设 llm_enabled 过路由闸；
    自带 monkeypatch 的用例（355/540）在测试体内二次 setattr 覆盖此替身。"""
    import json as _json

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request, *, accounting_hook=None):  # noqa: ANN001
        node_id = request.node_id
        if node_id == "author_proposal_generate":
            payload = {
                "content": "在线替身：保留作者的场景，但把选择的代价显性化。",
                "rationale": "遵循作者指令，并规避被否决的套路。",
            }
        elif node_id == "author_structure_extract":
            payload = {
                "candidate_brief": {
                    "character_desire": "主角想立刻查清那晚的真相。",
                    "reader_question": "袖口里的东西会不会被发现？",
                    "obstacle": "对方守着关键物件不肯松口。",
                    "choice_under_pressure": "是当场拆穿，还是暂时压下。",
                    "core_promise": "真相与保护不能同时兑现。",
                    "plot_movement": "旧信把主角带回事发地。",
                    "character_shift": "从回避转向承担代价。",
                    "chapter_question": "谁在暗处盯着？",
                    "ending_aftertaste": "真相是新的风险，而不是终点。",
                },
                "uncertainty_notes": [],
                "rationale": "从作者稿反向提取戏剧意图。",
            }
        else:
            raise AssertionError(f"unexpected author-draft node: {node_id}")
        response = LLMResponse(
            request_id=f"resp_{node_id}",
            provider="test-online-provider",
            model=request.model,
            text=_json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_{node_id}", "usage": {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36}},
            usage={"input_tokens": 12, "output_tokens": 24, "total_tokens": 36},
            finish_reason="stop",
        )
        if accounting_hook is not None:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response

    monkeypatch.setattr("novel_system.services.llm_client.LLMClient.generate", fake_generate)


def test_scene_target_uses_scene_project_when_legacy_chapter_has_no_project(session) -> None:
    project_id = "PROJECT_SCENE_AUTHORITY"
    session.add(StoryProject(project_id=project_id, title="Scene authority", outline_text=""))
    session.add(ChapterGoal(chapter_id="CH_SCENE_AUTHORITY", chapter_goal="legacy", planned_scene_count=1))
    session.add(
        SceneCard(
            scene_id="CH_SCENE_AUTHORITY_SC01",
            chapter_id="CH_SCENE_AUTHORITY",
            project_id=project_id,
            scene_seq=1,
            scene_goal="scene-owned project",
        )
    )
    session.commit()

    target = AuthorDraftService(session)._target_payload("scene", "CH_SCENE_AUTHORITY_SC01")

    assert target["project_id"] == project_id


def _create_chapter(chapter_id: str, *, planned_scene_count: int = 2) -> None:
    project_id = f"PRJ_{chapter_id}"
    seed_project(project_id, title=f"Author draft project {chapter_id}", outline_text="Writer-first author draft test.")
    seed_chapter(
        chapter_id,
        project_id=project_id,
        planned_scene_count=planned_scene_count,
        chapter_goal=f"目标 {chapter_id}",
        main_plot_push="推进主线",
        emotional_target="情绪转折",
        ending_effect="留下余味",
    )


def _create_scene(scene_id: str, *, chapter_id: str, scene_seq: int, is_chapter_last: int = 0) -> None:
    seed_scene(
        scene_id,
        chapter_id=chapter_id,
        scene_seq=scene_seq,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="档案室",
        scene_goal=f"场景目标 {scene_id}",
        beats_json=["发现", "选择"],
        exit_change="关系改变",
        hook="尾钩",
        target_length_band="medium",
        scene_type="reunion",
        is_chapter_last=is_chapter_last,
    )


def _create_project(session, project_id: str = "PRJ_OPEN") -> None:
    session.add(
        StoryProject(
            project_id=project_id,
            title=f"Project {project_id}",
            outline_text="A writer-first project.",
            planning_mode="snowflake",
        )
    )
    session.commit()


def _finalize_scene(session, scene_id: str, chapter_id: str, content: str, *, suffix: str = "v1") -> str:
    row_id = f"final_scene_{scene_id}_{suffix}"
    state = session.get(SceneRunState, scene_id)
    assert state is not None
    state.scene_status = "archived"
    state.current_final_scene_row_id = row_id
    session.add(
        FinalScene(
            row_id=row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            content=content,
            status="approved",
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash=f"hash_{scene_id}",
        )
    )
    session.commit()
    return row_id


def _set_final_aggregate(session, chapter_id: str, content: str) -> str:
    row_id = f"chapter_memory_final_{chapter_id}_v1"
    state = session.get(ChapterMemory, row_id)
    assert state is None
    session.add(
        ChapterMemory(
            row_id=row_id,
            chapter_id=chapter_id,
            aggregate_stage="final",
            content=content,
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    chapter_state = session.get(ChapterState, chapter_id)
    assert chapter_state is not None
    chapter_state.last_final_memory_row_id = row_id
    session.commit()
    return row_id


def test_ensure_and_save_scene_author_drafts_without_overwriting_runtime_outputs(client, session) -> None:
    _create_chapter("AD100")
    _create_scene("AD100_SC01", chapter_id="AD100", scene_seq=1)
    _create_scene("AD100_SC02", chapter_id="AD100", scene_seq=2, is_chapter_last=1)
    final_row_id = _finalize_scene(session, "AD100_SC01", "AD100", "场景运行终稿。")
    aggregate_row_id = _set_final_aggregate(session, "AD100", "章节最终聚合稿。")

    scene_response = client.post("/api/v1/author-drafts/scene/AD100_SC01/ensure")

    assert scene_response.status_code == 200
    ensured = scene_response.json()["data"]
    scene_draft = ensured["draft"]
    assert scene_draft["content"] == "场景运行终稿。"
    assert scene_draft["source_text_ref"] == f"final_scene:{final_row_id}"
    # 回包只有写作台读的两样：草稿与这一场当前权威正文的指针（台面上下文已删，B08-02）；
    # actor_ref 是幂等执行给每个写接口回包带上的审计字段
    assert set(ensured) == {"draft", "runtime_final_ref", "actor_ref"}
    assert ensured["runtime_final_ref"] == f"final_scene:{final_row_id}"

    save_response = client.patch(
        f"/api/v1/author-drafts/{scene_draft['draft_id']}",
        json={"content": "作者手工改过的场景稿。", "base_revision_no": 1},
    )

    assert save_response.status_code == 200
    saved_payload = save_response.json()["data"]
    saved = saved_payload["draft"]
    assert saved["content"] == "作者手工改过的场景稿。"
    assert saved["revision_no"] == 2
    assert set(saved_payload) == {"draft", "runtime_final_ref", "changed", "words_rollup", "diagnosis_rollup", "actor_ref"}

    session.expire_all()
    assert session.get(ChapterMemory, aggregate_row_id).content == "章节最终聚合稿。"
    assert session.get(FinalScene, final_row_id).content == "场景运行终稿。"
    assert {row.event_type for row in session.query(AuthorDraftEvent).all()} >= {"created", "edited"}


def test_author_drafts_are_scene_drafts_only(client, session) -> None:
    """章稿 / 作品稿只剩测试在建、库里没有（B08-22）：不再新建，也读不到。"""
    _create_chapter("AD110", planned_scene_count=1)
    project_id = session.get(ChapterGoal, "AD110").project_id
    for object_type, object_id in (("chapter", "AD110"), ("project", project_id)):
        for method, suffix in (("post", "ensure"), ("get", "current")):
            response = client.request(method, f"/api/v1/author-drafts/{object_type}/{object_id}/{suffix}")
            assert response.status_code == 400, (object_type, suffix, response.text)
            assert response.json()["error"]["code"] == "AUTHOR_DRAFT_TARGET_INVALID"
    assert session.query(AuthorDraft).count() == 0


def test_scene_draft_is_dirty_when_runtime_final_pointer_moves_after_promotion(client, session) -> None:
    _create_chapter("AD_POINTER", planned_scene_count=1)
    _create_scene("AD_POINTER_SC01", chapter_id="AD_POINTER", scene_seq=1, is_chapter_last=1)
    first_final_id = _finalize_scene(session, "AD_POINTER_SC01", "AD_POINTER", "作者已确认的正文。")
    ensured = client.post("/api/v1/author-drafts/scene/AD_POINTER_SC01/ensure")
    assert ensured.status_code == 200
    draft_data = ensured.json()["data"]["draft"]
    assert draft_data["canonical_dirty"] is True

    draft = session.get(AuthorDraft, draft_data["draft_id"])
    assert draft is not None
    draft.last_promoted_revision_no = draft.revision_no
    draft.last_promoted_final_scene_row_id = first_final_id
    session.commit()

    clean = client.get("/api/v1/author-drafts/scene/AD_POINTER_SC01/current")
    assert clean.status_code == 200
    assert clean.json()["data"]["runtime_final_ref"] == f"final_scene:{first_final_id}"
    assert clean.json()["data"]["draft"]["canonical_dirty"] is False

    second_final_id = _finalize_scene(
        session,
        "AD_POINTER_SC01",
        "AD_POINTER",
        "自动重写切换出的另一版正文。",
        suffix="v2",
    )
    drifted = client.post("/api/v1/author-drafts/scene/AD_POINTER_SC01/ensure")
    assert drifted.status_code == 200
    payload = drifted.json()["data"]
    assert payload["runtime_final_ref"] == f"final_scene:{second_final_id}"
    assert payload["draft"]["revision_no"] == 1
    assert payload["draft"]["last_promoted_final_scene_row_id"] == first_final_id
    assert payload["draft"]["canonical_dirty"] is True


def test_author_draft_save_uses_optimistic_locking(client, session) -> None:
    _create_chapter("AD200", planned_scene_count=1)
    _create_scene("AD200_SC01", chapter_id="AD200", scene_seq=1, is_chapter_last=1)
    _finalize_scene(session, "AD200_SC01", "AD200", "第一版。")
    draft = client.post("/api/v1/author-drafts/scene/AD200_SC01/ensure").json()["data"]["draft"]

    first_save = client.patch(
        f"/api/v1/author-drafts/{draft['draft_id']}",
        json={"content": "第二版。", "base_revision_no": 1},
    )
    stale_save = client.patch(
        f"/api/v1/author-drafts/{draft['draft_id']}",
        json={"content": "过期保存。", "base_revision_no": 1},
    )

    assert first_save.status_code == 200
    assert stale_save.status_code == 409
    assert stale_save.json()["error"]["code"] == "AUTHOR_DRAFT_CONFLICT"
    assert stale_save.json()["error"]["details"]["current_revision_no"] == 2


def test_author_draft_save_uses_database_compare_and_swap(session) -> None:
    project_id = "AD_CAS_PROJECT"
    draft_id = "author_draft_cas"
    session.add(StoryProject(project_id=project_id, title="CAS", outline_text=""))
    session.add(
        AuthorDraft(
            draft_id=draft_id,
            object_type="project",
            object_id=project_id,
            source_text_ref=f"project_discovery:{project_id}",
            content="first",
            revision_no=1,
            status="current",
        )
    )
    session.commit()

    stale_session = SessionLocal()
    winner_session = SessionLocal()
    try:
        cached = stale_session.get(AuthorDraft, draft_id)
        assert cached is not None and cached.revision_no == 1
        stale_session.commit()  # release the SQLite read transaction, keep identity-map state

        saved = AuthorDraftService(winner_session).save(
            draft_id,
            {"content": "winner", "base_revision_no": 1},
            actor_ref="winner",
        )
        winner_session.commit()
        assert saved["draft"]["revision_no"] == 2

        with pytest.raises(DomainError) as exc_info:
            AuthorDraftService(stale_session).save(
                draft_id,
                {"content": "stale overwrite", "base_revision_no": 1},
                actor_ref="stale",
            )
        assert exc_info.value.code == "AUTHOR_DRAFT_CONFLICT"
        assert exc_info.value.details["current_revision_no"] == 2
    finally:
        stale_session.close()
        winner_session.close()


def _scene_draft(client, key: str) -> dict:
    _create_chapter(key, planned_scene_count=1)
    _create_scene(f"{key}_SC01", chapter_id=key, scene_seq=1, is_chapter_last=1)
    return client.post(f"/api/v1/author-drafts/scene/{key}_SC01/ensure").json()["data"]["draft"]


def _generate_set(client, draft_id: str, key: str):
    return client.post(
        f"/api/v1/author-drafts/{draft_id}/proposals/generate-set",
        json={"mode": "continuation_variants", "instruction": "续写下一段，自然承接当前正文。"},
        headers={"X-Idempotency-Key": key},
    )


def _proposal_statuses(session, draft_id: str) -> dict[str, str]:
    session.expire_all()
    return {
        row.proposal_id: row.status
        for row in session.query(AuthorDraftProposal).filter_by(draft_id=draft_id).all()
    }


def test_generate_continuation_variants_as_one_idempotent_three_candidate_intent(
    client,
    session,
) -> None:
    """续写托盘需要三份独立续写，而不是三个同键请求或混合类型提案。"""

    draft = _scene_draft(client, "AD275_VARIANTS")
    response = _generate_set(client, draft["draft_id"], "continuation-variants-one-intent")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    proposals = data["proposals"]
    assert data["mode"] == "continuation_variants"
    assert len(proposals) == 3
    assert [item["proposal_type"] for item in proposals] == ["continuation"] * 3
    assert [item["proposal_kind"] for item in proposals] == ["continuation"] * 3
    assert len({item["proposal_id"] for item in proposals}) == 3
    assert [item["proposal_source"] for item in proposals] == [
        "writer_room_continuation_variants:action",
        "writer_room_continuation_variants:relationship",
        "writer_room_continuation_variants:suspense",
    ]
    assert all(item["status"] == "candidate" for item in proposals)
    # 每条候选都记一笔在这一场名下的 LLM 调用
    session.expire_all()
    stored_call = session.get(LlmCall, proposals[0]["source_llm_call_id"])
    assert stored_call is not None
    assert stored_call.node_id == "author_proposal_generate"
    assert (stored_call.scope_type, stored_call.scope_id, stored_call.scene_id) == (
        "scene",
        "AD275_VARIANTS_SC01",
        "AD275_VARIANTS_SC01",
    )
    session.expire_all()
    assert session.get(AuthorDraft, draft["draft_id"]).content == draft["content"]


def test_a_new_continuation_set_supersedes_the_previous_open_candidates(client, session) -> None:
    """每点一次「AI 续写」，上一组还开着的三条标成「已替换」（批准 #7）；同一个幂等键重放不再替换任何东西。"""

    draft = _scene_draft(client, "AD_CONT_SUPERSEDE")
    first = _generate_set(client, draft["draft_id"], "continuation-supersede-1").json()["data"]["proposals"]
    second = _generate_set(client, draft["draft_id"], "continuation-supersede-2").json()["data"]["proposals"]

    statuses = _proposal_statuses(session, draft["draft_id"])
    assert [statuses[item["proposal_id"]] for item in first] == ["superseded"] * 3
    assert [statuses[item["proposal_id"]] for item in second] == ["candidate"] * 3
    stored = session.get(AuthorDraftProposal, first[0]["proposal_id"])
    assert stored.merge_status == "superseded"

    replay = _generate_set(client, draft["draft_id"], "continuation-supersede-1")
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Idempotency-Status"] == "replayed"
    assert [item["proposal_id"] for item in replay.json()["data"]["proposals"]] == [item["proposal_id"] for item in first]
    assert _proposal_statuses(session, draft["draft_id"]) == statuses


def test_a_failed_continuation_set_leaves_the_previous_candidates_open(client, session, monkeypatch) -> None:
    """第三条续写失败时，记账已经在调用之间提交了前两条：上一组照旧开着，什么也不替换。"""
    from novel_system.services.llm_client import LLMClient

    draft = _scene_draft(client, "AD_CONT_FAILED")
    first = AuthorDraftService(session).generate_proposal_set(draft["draft_id"], {"mode": "continuation_variants"})
    session.commit()
    online = LLMClient.generate
    calls = {"count": 0}

    def flaky(self, request, *, accounting_hook=None):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] >= 3:
            raise RuntimeError("provider dropped the third continuation")
        return online(self, request, accounting_hook=accounting_hook)

    monkeypatch.setattr("novel_system.services.llm_client.LLMClient.generate", flaky)
    with pytest.raises(DomainError):
        AuthorDraftService(session).generate_proposal_set(draft["draft_id"], {"mode": "continuation_variants"})
    session.rollback()

    statuses = _proposal_statuses(session, draft["draft_id"])
    assert calls["count"] >= 3
    assert [statuses[item["proposal_id"]] for item in first["proposals"]] == ["candidate"] * 3
    assert "superseded" not in statuses.values()


def test_only_continuation_variants_are_generated(client, session) -> None:
    draft = _scene_draft(client, "AD_CONT_MODE")
    response = client.post(
        f"/api/v1/author-drafts/{draft['draft_id']}/proposals/generate-set",
        json={"mode": "daily", "instruction": "给结构、局部段落和语言三种方案。"},
        headers={"X-Idempotency-Key": "continuation-mode-daily"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "AUTHOR_DRAFT_PROPOSAL_MODE_UNSUPPORTED"
    assert session.query(AuthorDraftProposal).filter_by(draft_id=draft["draft_id"]).count() == 0


def test_saving_the_author_draft_learns_no_preferences_and_never_diffs(client, session, monkeypatch) -> None:
    """写作偏好学习已退役（批准 #6）：保存不再把整场新旧正文逐字比一遍，也不再写偏好档案和「写作偏好」待办卡。"""
    import difflib

    def no_diff(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("autosave must not diff the whole scene")

    monkeypatch.setattr(difflib, "SequenceMatcher", no_diff)
    draft = _scene_draft(client, "AD_NO_PREFERENCE")
    first = "<p>" + "林昭把旧信摊在案卷上，雨一直没停。" * 30 + "</p>"
    second = "<p>" + "她没有拆信，只是把台灯往案卷那边推了推。" * 12 + "</p>"
    revision = draft["revision_no"]
    for content in (first, second):
        saved = client.patch(f"/api/v1/author-drafts/{draft['draft_id']}", json={"content": content, "base_revision_no": revision})
        assert saved.status_code == 200, saved.text
        revision = saved.json()["data"]["draft"]["revision_no"]

    assert session.query(AuthorPreferenceProfile).count() == 0
    assert session.query(ReviewItem).filter_by(item_type="author_preference_profile").count() == 0


def test_retired_preference_cards_are_not_listed_in_the_inbox(client, session) -> None:
    from novel_system.services.review_cards import ReviewCardService

    _create_project(session, "PRJ_PREF_CARD")
    session.add(
        ReviewItem(
            review_id="review_author_pref_legacy",
            project_id="PRJ_PREF_CARD",
            item_type="author_preference_profile",
            status="pending",
            candidate_text="{}",
            candidate_payload_json={},
        )
    )
    session.commit()

    cards = ReviewCardService(session).list_cards("PRJ_PREF_CARD")["items"]
    assert "review_author_pref_legacy" not in {card["id"] for card in cards}
    assert session.get(ReviewItem, "review_author_pref_legacy") is not None  # 行留在库里


def test_the_continuation_prompt_carries_no_preference_section(client, session, monkeypatch) -> None:
    captured: list = []

    def fake_generate(self, request, *, accounting_hook=None):  # noqa: ANN001
        captured.append(request)
        payload = {"content": "她把灯推近了一点。", "rationale": "只推进下一拍。"}
        response = LLMResponse(
            request_id=f"resp_{len(captured)}",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"resp_{len(captured)}"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )
        if accounting_hook is not None:
            handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
            accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response

    monkeypatch.setattr("novel_system.services.llm_client.LLMClient.generate", fake_generate)
    draft = _scene_draft(client, "AD_CONT_PROMPT")
    response = _generate_set(client, draft["draft_id"], "continuation-prompt")
    assert response.status_code == 200, response.text
    assert len(captured) == 3
    prompt_text = json.dumps([request.messages for request in captured], ensure_ascii=False)
    assert "preference" not in prompt_text.lower()
    assert "续写下一段，自然承接当前正文。" in prompt_text


def test_the_continuation_snapshot_builds_no_digest_the_prompt_never_renders(client, session, monkeypatch) -> None:
    """R6 复核补充 (2)：PromptBuilder 只渲染 ``context_budget.SECTION_SPECS`` 里的摘要。续写要的正文与元数据
    另附在 user 消息后面（「## Current Author Draft」「## Current Metadata」），快照里再备一份 author_draft /
    target_metadata / proposal_request 摘要只会喂给审计的 bundle_hash，模型从来看不到。"""
    from novel_system.services.author_drafts import proposals
    from novel_system.services.context_budget import SECTION_SPECS

    snapshots: list[dict] = []
    user_prompts: list[str] = []
    real_build = proposals.PromptBuilder.build
    real_generate = proposals.LLMNodeRunner.run

    def recording_build(self, bundle_snapshot, template_name, **kwargs):  # noqa: ANN001
        if template_name == "author_proposal_generate":
            snapshots.append(bundle_snapshot)
        return real_build(self, bundle_snapshot, template_name, **kwargs)

    def recording_run(self, **kwargs):  # noqa: ANN001
        user_prompts.append(kwargs["user_prompt"])
        return real_generate(self, **kwargs)

    monkeypatch.setattr(proposals.PromptBuilder, "build", recording_build)
    monkeypatch.setattr(proposals.LLMNodeRunner, "run", recording_run)
    draft = _scene_draft(client, "AD_CONT_SNAPSHOT")
    response = _generate_set(client, draft["draft_id"], "continuation-snapshot")
    assert response.status_code == 200, response.text

    renderable = {key for _name, _label, digest_keys in SECTION_SPECS for key in digest_keys}
    assert len(snapshots) == 3
    for snapshot in snapshots:
        assert set(snapshot.get("inline_digests") or {}) <= renderable
        assert not snapshot.get("ordered_injections")
    # 模型拿到的正文与指令照旧在 user 消息里
    assert len(user_prompts) == 3
    for user_prompt in user_prompts:
        assert "## Current Author Draft" in user_prompt and "## Current Metadata" in user_prompt
        assert "续写下一段，自然承接当前正文。" in user_prompt


def test_ensure_creates_a_blank_scene_draft_when_the_scene_has_no_final(client, session) -> None:
    _create_chapter("AD500", planned_scene_count=1)
    _create_scene("AD500_SC01", chapter_id="AD500", scene_seq=1, is_chapter_last=1)

    scene_response = client.post("/api/v1/author-drafts/scene/AD500_SC01/ensure")

    assert scene_response.status_code == 200
    scene_draft = scene_response.json()["data"]["draft"]
    assert scene_draft["source_text_ref"] == "scene_card:AD500_SC01:blank"
    # 阶段 X：空白稿就是空白——场景卡常驻在正文旁边，不再抄成脚手架塞进正文
    assert scene_draft["content"] == ""
    assert session.query(FinalScene).count() == 0
    # ensure-blank 没有界面调用，已删（批准 #24a，见 test_retired_surface）：ensure 在没有权威正文时就给空白稿
