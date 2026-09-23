"""风格参考 v3（V1 / E7 / V8）— 唯一抄袭门 ``services/reference_copy_gate.py``。

判定与 ``check_plagiarism(ngram_size=8, threshold_chars=12)`` 等价、命中只记哈希与位置、按书建一次索引、
同一稿按文本哈希缓存；受保护专名来自画像的生成期禁用词（含 protected_auto）与环境变量；每条进正文的路径
（成稿门、写作台采纳 AI 建议 / 局部改写）都过它。全部合成文本。
"""

from __future__ import annotations

import pytest

from novel_system.db.models import (
    AuthorDraft,
    AuthorDraftProposal,
    ChapterGoal,
    PassagePatchCandidate,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceParagraph,
)
from novel_system.services import reference_copy_gate as gate_module
from novel_system.services.errors import DomainError
from novel_system.services.reference_copy_gate import (
    THRESHOLD_CHARS,
    check_reference_copy,
    check_reference_copy_for_scope,
    copy_block_author_action,
    reset_reference_copy_gate_cache,
)
from novel_system.services.style_policy import StylePolicy, style_policy_live
from novel_system.services.style_reference.validation.plagiarism import check_plagiarism
from tests.reference_copy_fixtures import PROTECTED_NAME, REFERENCE_PASSAGE, seed_bound_reference

PROJECT_ID = "P_COPY_GATE"
CHAPTER_ID = "P_COPY_GATE_CH01"
SCENE_ID = "P_COPY_GATE_CH01_SC01"

OTHER_PARAGRAPHS = (
    "码头的吊车在雾里停了三天，工头每天早上都来敲一次铁皮门，说船期又推后了。",
    "她把旧车票夹进字典里，翻到的那一页正好是讲潮汐的词条，墨水已经褪成了浅灰色。",
)


@pytest.fixture(autouse=True)
def _fresh_gate_cache():
    reset_reference_copy_gate_cache()
    yield
    reset_reference_copy_gate_cache()


def _seed_scene(session) -> SceneCard:
    session.add(StoryProject(project_id=PROJECT_ID, title="抄袭门", outline_text="", planning_mode="snowflake"))
    session.add(ChapterGoal(chapter_id=CHAPTER_ID, project_id=PROJECT_ID, chapter_goal="目标", display_order=1))
    scene = SceneCard(
        scene_id=SCENE_ID,
        chapter_id=CHAPTER_ID,
        project_id=PROJECT_ID,
        scene_seq=1,
        scene_goal="推进",
        onstage_chars_json=[],
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=SCENE_ID))
    session.commit()
    return scene


def _bind(session, **kwargs) -> dict[str, str]:
    return seed_bound_reference(
        session,
        seed=kwargs.pop("seed", "gate"),
        scope="project",
        scope_ref_id=PROJECT_ID,
        paragraphs=kwargs.pop("paragraphs", (REFERENCE_PASSAGE, *OTHER_PARAGRAPHS)),
        **kwargs,
    )


def test_verbatim_sixty_chars_are_blocked_and_reported_without_source_text(session) -> None:
    """评审探针：参考书连续 60 字照抄，旧「来源安全」判安全；新门拦下，只留位置 / 长度 / 指纹。"""
    scene = _seed_scene(session)
    refs = _bind(session)
    policy = style_policy_live(session, scene, freeze_contract=False)
    prefix = "雨停了以后，"
    copied = REFERENCE_PASSAGE[:60]
    text = f"{prefix}{copied}。她没回头。"

    check = check_reference_copy(session, text, policy=policy)

    assert check.blocked is True
    (hit,) = check.hits
    assert (hit.start, hit.end) == (len(prefix), len(prefix) + len(copied))
    assert hit.book_id == refs["book_id"]
    audit = check.audit()
    assert audit["safe"] is False and audit["checked_books"] == [refs["book_id"]]
    assert copied[:THRESHOLD_CHARS] not in str(audit)
    action = copy_block_author_action(check, target_ref=f"scene:{SCENE_ID}")
    assert f"第 {len(prefix) + 1}–{len(prefix) + len(copied)} 字" in action["message"]
    assert copied[:THRESHOLD_CHARS] not in str(action)


def test_hits_equal_check_plagiarism_even_with_inserted_punctuation(session) -> None:
    """判定与 check_plagiarism 等价：插空格 / 换标点照样命中，命中区间一致。"""
    scene = _seed_scene(session)
    _bind(session)
    policy = style_policy_live(session, scene, freeze_contract=False)
    disguised = "开头一句。" + " ".join(REFERENCE_PASSAGE[:20]) + "——" + OTHER_PARAGRAPHS[1][5:25] + "结尾。"
    corpus = [REFERENCE_PASSAGE, *OTHER_PARAGRAPHS]

    report = check_plagiarism(disguised, corpus)
    check = check_reference_copy(session, disguised, policy=policy)

    assert report.passed is False and check.blocked is True
    assert [(hit.position, hit.matched_length) for hit in report.hits] == [
        (hit.start, hit.matched_chars) for hit in check.hits
    ]
    # 11 字的巧合不算
    short = "开头一句。" + REFERENCE_PASSAGE[:11] + "。"
    assert check_reference_copy(session, short, policy=policy).hits == ()


def test_index_is_built_once_per_book_and_results_are_cached(session, monkeypatch) -> None:
    scene = _seed_scene(session)
    refs = _bind(session)
    policy = style_policy_live(session, scene, freeze_contract=False)
    builds: list[str] = []
    original_init = gate_module._BookCopyIndex.__init__

    def counting_init(self, book_id, texts):
        builds.append(book_id)
        original_init(self, book_id, texts)

    monkeypatch.setattr(gate_module._BookCopyIndex, "__init__", counting_init)
    first = check_reference_copy(session, "一段干净的正文，说的是另一件事。", policy=policy)
    again = check_reference_copy(session, "一段干净的正文，说的是另一件事。", policy=policy)
    other = check_reference_copy(session, "又一段干净的正文。", policy=policy)
    assert first is again, "同一稿对同一组书与词只扫一次"
    assert first.blocked is False and other.blocked is False
    assert builds == [refs["book_id"]], "一本书在进程里只建一次索引"

    # 段落表一变（新增一段），指纹变了，索引重建、旧结果不再复用
    session.add(
        StyleReferenceParagraph(
            paragraph_id="sr_par_copy_gate_late",
            book_id=refs["book_id"],
            paragraph_index=99,
            paragraph_type="narration",
            start_offset=0,
            end_offset=10,
            text="后来添进来的一段参考原文，足够长也足够特别。",
            char_count=22,
        )
    )
    session.commit()
    late = check_reference_copy(session, "他说：后来添进来的一段参考原文，足够长。", policy=policy)
    assert late.blocked is True
    assert builds == [refs["book_id"], refs["book_id"]]


def test_protected_names_come_from_generation_banned_terms_and_environment(session, monkeypatch) -> None:
    scene = _seed_scene(session)
    _bind(session, protected_terms=(PROTECTED_NAME,))
    monkeypatch.setenv("NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON", '["盐湾学院"]')
    policy = style_policy_live(session, scene, freeze_contract=False)
    text = f"{PROTECTED_NAME}从盐湾学院回来。"

    check = check_reference_copy(session, text, policy=policy)

    assert check.blocked is True and check.hits == ()
    assert [(hit.start, hit.end, hit.source) for hit in check.protected_hits] == [
        (0, len(PROTECTED_NAME), "protected_auto"),
        (len(PROTECTED_NAME) + 1, len(PROTECTED_NAME) + 5, "environment"),
    ]
    assert PROTECTED_NAME not in str(check.audit()) and "盐湾" not in str(check.audit())
    # 没有任何绑定时只查环境变量里的全局词
    unbound = check_reference_copy(session, text, policy=None)
    assert [hit.source for hit in unbound.protected_hits] == ["environment"]


def test_unbound_and_empty_text_pass(session) -> None:
    scene = _seed_scene(session)
    assert check_reference_copy(session, REFERENCE_PASSAGE, policy=StylePolicy()).blocked is False
    _bind(session)
    policy = style_policy_live(session, scene, freeze_contract=False)
    assert check_reference_copy(session, "   ", policy=policy).blocked is False


def test_scope_helper_checks_the_live_binding_even_when_the_bundle_froze_none(session) -> None:
    """冻结「无绑定」之后才绑上的书照样要拦：正文可能在冻结之后才粘进参考原文。"""
    scene = _seed_scene(session)
    _bind(session)
    absent_bundle = {
        "source_version_refs": {"style_reference_runtime_contract_status": "absent"},
        "inline_digests": {"scene_card": "Goal"},
    }
    check = check_reference_copy_for_scope(
        session, "他想：" + REFERENCE_PASSAGE[:30], scope=scene, bundle_snapshot=absent_bundle
    )
    assert check.blocked is True


def test_applying_an_ai_proposal_that_copies_the_reference_is_refused(session) -> None:
    from novel_system.services.author_drafts import AuthorDraftService

    _seed_scene(session)
    _bind(session)
    session.add(
        AuthorDraft(
            draft_id="author_draft_copy_gate",
            object_type="scene",
            object_id=SCENE_ID,
            source_text_ref="author_blank:scene",
            content="<p>原来的一句。</p>",
            revision_no=3,
            status="current",
        )
    )
    session.add(
        AuthorDraftProposal(
            proposal_id="proposal_copy_gate",
            draft_id="author_draft_copy_gate",
            object_type="scene",
            object_id=SCENE_ID,
            proposal_type="scene_draft",
            content=f"<p>她抬头。{REFERENCE_PASSAGE[:40]}</p>",
            proposal_kind="whole_draft",
            status="candidate",
        )
    )
    session.commit()

    with pytest.raises(DomainError) as excinfo:
        AuthorDraftService(session).apply_proposal("proposal_copy_gate", {"apply_mode": "replace"})

    assert excinfo.value.code == "SOURCE_SAFETY_BLOCKED"
    action = excinfo.value.details["author_action"]
    assert action["title"].startswith("这条 AI 建议")
    assert REFERENCE_PASSAGE[:THRESHOLD_CHARS] not in str(excinfo.value.details)
    session.rollback()
    draft = session.get(AuthorDraft, "author_draft_copy_gate")
    assert draft.revision_no == 3 and draft.content == "<p>原来的一句。</p>"
    assert session.get(AuthorDraftProposal, "proposal_copy_gate").status == "candidate"


def test_accepting_a_passage_rewrite_that_copies_the_reference_is_refused(session) -> None:
    from novel_system.services.writer_deep_review import WriterDeepReviewService

    _seed_scene(session)
    _bind(session)
    session.add(
        PassagePatchCandidate(
            patch_id="passage_patch_copy_gate",
            object_type="scene",
            object_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            scene_id=SCENE_ID,
            source_excerpt="原来的一句。",
            issue_dimension="author_instruction",
            replacement_options_json=[
                {"option_id": "option_clean", "tone": "shorter", "replacement_text": "她没说话，把伞收好。"},
                {"option_id": "option_copy", "tone": "sharper", "replacement_text": REFERENCE_PASSAGE[:36]},
            ],
            status="candidate",
            author_decision="pending",
        )
    )
    session.commit()
    service = WriterDeepReviewService(session)

    with pytest.raises(DomainError) as excinfo:
        service.accept_patch_candidate("passage_patch_copy_gate", {"selected_option_id": "option_copy"})
    assert excinfo.value.code == "SOURCE_SAFETY_BLOCKED"
    assert excinfo.value.details["author_action"]["title"].startswith("这条改写")
    session.rollback()
    assert session.get(PassagePatchCandidate, "passage_patch_copy_gate").status == "candidate"

    accepted = service.accept_patch_candidate("passage_patch_copy_gate", {"selected_option_id": "option_clean"})
    assert accepted["candidate"]["status"] == "accepted"
