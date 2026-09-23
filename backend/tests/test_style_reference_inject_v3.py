"""风格参考 v3 · 注入（P4）：inject/ 包、每场冻结选窗、参考方式、文风卡、旧画像替身、契约 v2、贪心拟合、适配器。

全部合成数据（``style_reference_inject_helpers``）；无真实模型、无真实作者原文。
"""

from __future__ import annotations

import json
import time
from collections import Counter

import pytest
from sqlalchemy import func, select

from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph, StyleReferenceSceneWindows
from novel_system.services.context_budget import estimate_tokens
from novel_system.services.style_policy import policy_from_contract, reset_style_policy_cache
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    PLANNING_FEW_SHOT_K_CAP,
    REVIEW_FEW_SHOT_K_CAP,
    STYLE_USER_TAIL_KEY,
    apply_style_user_tail,
    inject_style_reference_prefix,
)
from novel_system.services.style_reference.config_loader import clear_config_cache
from novel_system.services.style_reference.inject.bindings import (
    describe_binding_layers,
    most_specific_binding,
    resolve_binding_layers,
)
from novel_system.services.style_reference.inject.fit import fit_rendered
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.inject.render import (
    NOTICE_BOOK_CHANGED,
    SAMPLE_HEADERS,
    render_style,
    reset_render_cache,
)
from novel_system.services.style_reference.inject.request import (
    ROLE_DRAFT,
    ROLE_PLAN,
    ROLE_REVIEW,
    ROLE_REVISE,
    StyleRenderRequest,
    infer_role,
)
from novel_system.services.style_reference.inject.selection import (
    SLOT_DEVICE,
    SLOT_POSITION,
    SLOT_SITUATION,
    IndexWindow,
    compute_selection,
    derive_situation_tags,
    load_index,
    resolve_scene_selection,
    selection_quotas,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION_V1,
    STYLE_RUNTIME_CONTRACT_VERSION_V2,
    _json_hash,
    build_style_runtime_contract,
    reset_contract_memo,
    style_runtime_contract_from_bundle,
    validate_style_runtime_contract,
)
from novel_system.services.style_reference.schemas import (
    FEW_SHOT_CLOSING_MANDATE,
    FEW_SHOT_CLOSING_MANDATE_FINAL,
)
from novel_system.services.style_reference.windows import ensure_window_index, load_windows
from tests.style_reference_inject_helpers import (
    DEVICES,
    PROJECT_ID,
    VOICE_HABITS,
    bind,
    frozen_bundle,
    seed_reference,
    seed_scene,
    tag_windows,
)


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_config_cache()
    reset_render_cache()
    reset_contract_memo()
    reset_style_policy_cache()
    yield
    reset_render_cache()


def _policy(session, key: str, *, config: dict | None = None, scene=None, **seed_kwargs):
    _book_id, profile_id = seed_reference(session, key, **seed_kwargs)
    bind(session, profile_id, binding_id=f"inj_bind_{key}", config_json=config or {})
    layers = resolve_binding_layers(session, PROJECT_ID, "scene_generation")
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    session.commit()
    return policy_from_contract(contract, mode="frozen"), profile_id


def _refs(audit: dict) -> list[int]:
    return [int(ref["window_no"]) for ref in audit.get("few_shot_window_refs") or []]


# ---------------------------------------------------------------------------
# 每场冻结选窗（J2）
# ---------------------------------------------------------------------------


def test_every_node_of_one_scene_sees_the_same_frozen_windows(session) -> None:
    _book_id, profile_id = seed_reference(session, "freeze")
    bind(session, profile_id, binding_id="inj_bind_freeze")
    scene = seed_scene(session, "INJ_SC_FREEZE")
    bundle = frozen_bundle(session, bundle_id="B_FREEZE", scene=scene)
    base = {"system_prompt": "SYS", "token_budget": {}}

    draft = inject_style_reference_prefix(session, dict(base), scene, bundle, placement=PLACEMENT_USER_TAIL)
    session.commit()
    revise = inject_style_reference_prefix(session, dict(base), scene, bundle, placement=PLACEMENT_USER_TAIL, role="revise")
    review = inject_style_reference_prefix(session, dict(base), scene, bundle, role="review")
    patch = inject_style_reference_prefix(session, dict(base), scene, bundle, few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP)
    session.commit()

    selected = [
        ref["window_no"]
        for ref in session.scalars(select(StyleReferenceSceneWindows)).one().window_refs_json
    ]
    assert session.scalar(select(func.count()).select_from(StyleReferenceSceneWindows)) == 1
    draft_windows = _refs(draft["_style_reference_runtime_audit"])
    assert len(draft_windows) == 12 and set(draft_windows) == set(selected)
    assert set(_refs(revise["_style_reference_runtime_audit"])) == set(selected)
    # 评审取冻结选窗的前 4 窗、补丁（旧参数 k≤3 → 规划口径）前 3 窗——都是同一组窗的前缀
    assert set(_refs(review["_style_reference_runtime_audit"])) == set(selected[:REVIEW_FEW_SHOT_K_CAP])
    assert set(_refs(patch["_style_reference_runtime_audit"])) == set(selected[:PLANNING_FEW_SHOT_K_CAP])
    assert draft["_style_reference_runtime_audit"]["selection"]["persisted"] is True
    assert review["_style_reference_runtime_audit"]["role"] == "review"


def test_selection_never_depends_on_the_draft_being_refined(session) -> None:
    _book_id, profile_id = seed_reference(session, "nodraft")
    bind(session, profile_id, binding_id="inj_bind_nodraft")
    scene = seed_scene(session, "INJ_SC_NODRAFT")
    bundle = frozen_bundle(session, bundle_id="B_NODRAFT", scene=scene)
    a = inject_style_reference_prefix(
        session, {"system_prompt": ""}, scene, bundle, placement=PLACEMENT_USER_TAIL, context_text="“你来了。”她说。" * 40
    )
    reset_render_cache()
    session.query(StyleReferenceSceneWindows).delete()
    session.commit()
    b = inject_style_reference_prefix(
        session, {"system_prompt": ""}, scene, bundle, placement=PLACEMENT_USER_TAIL, context_text="他想了很久。" * 80
    )
    assert _refs(a["_style_reference_runtime_audit"]) == _refs(b["_style_reference_runtime_audit"])


def test_stale_selection_is_reselected_when_the_book_changed(session) -> None:
    book_id, profile_id = seed_reference(session, "stale")
    policy, _ = _policy_from_existing(session, "stale", profile_id)
    request = StyleRenderRequest(role=ROLE_DRAFT, scene_id="SC_STALE", bundle_id="B1")
    first = resolve_scene_selection(session, policy, request)
    session.commit()
    again = resolve_scene_selection(session, policy, request)
    assert again.reused and again.refs == first.refs
    # 改一段正文 → 根哈希变 → 索引重建 → 冻结行按新索引重选并覆盖
    paragraph = session.scalars(
        select(StyleReferenceParagraph).where(StyleReferenceParagraph.book_id == book_id).order_by(StyleReferenceParagraph.paragraph_index)
    ).first()
    paragraph.text = paragraph.text + "（改）"
    book = session.get(StyleReferenceBook, book_id)
    stats = dict(book.stats_json)
    stats.pop("paragraph_root_sha256", None)
    stats.pop("paragraph_count", None)
    book.stats_json = stats
    session.commit()
    fresh = resolve_scene_selection(session, policy, request)
    assert not fresh.reused and fresh.root != first.root
    assert session.scalar(select(func.count()).select_from(StyleReferenceSceneWindows)) == 1


def _policy_from_existing(session, key: str, profile_id: str, *, config: dict | None = None):
    bind(session, profile_id, binding_id=f"inj_bind_{key}", config_json=config or {})
    layers = resolve_binding_layers(session, PROJECT_ID, "scene_generation")
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    session.commit()
    return policy_from_contract(contract, mode="frozen"), contract


# ---------------------------------------------------------------------------
# 选窗配额（J3 / J4 / N4）
# ---------------------------------------------------------------------------


def _synthetic_index(count: int = 300, chapters: int = 60, *, tags: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] | None = None) -> list[IndexWindow]:
    per = max(1, count // chapters)
    windows = []
    for no in range(1, count + 1):
        chapter = (no - 1) // per + 1
        offset = (no - 1) % per
        position = "opening" if offset == 0 else ("closing" if offset == per - 1 else "middle")
        situations, devices = (tags or {}).get(no, ((), ()))
        windows.append(
            IndexWindow(
                window_no=no,
                start=no * 100,
                end=no * 100 + 60,
                chapter=chapter,
                position=position,
                chars=3000,
                paragraphs=40,
                dialogue_share=(no % 7) / 10,
                typicality=-1.0 - ((no * 37) % 100) / 100,
                situations=situations,
                devices=devices,
            )
        )
    return windows


def test_whole_index_coverage_over_many_scenes() -> None:
    """全书轮换：600 场里每一窗都能被选到（旧轮换池只有 k×6 = 72 窗），常规 / 章首场都铺得开。"""
    index = _synthetic_index(522, 90)
    counts: Counter = Counter()
    opening_counts: Counter = Counter()
    for i in range(600):
        for ref in compute_selection(index, k=12, seed=f"SC{i:04d}|book|v"):
            counts[ref.window_no] += 1
        for ref in compute_selection(index, k=12, seed=f"OP{i:04d}|book|v", position="opening"):
            if ref.slot == SLOT_POSITION:
                opening_counts[ref.window_no] += 1
    coverage = len(counts) / len(index)
    assert coverage >= 0.95, coverage
    assert max(counts.values()) <= 60  # 没有「永远同几窗」
    opening_windows = {w.window_no for w in index if w.position in ("opening", "whole")}
    assert len(set(opening_counts) & opening_windows) / len(opening_windows) >= 0.9


def test_one_window_per_chapter_before_relaxing() -> None:
    index = _synthetic_index(300, 60)
    refs = compute_selection(index, k=12, seed="SC_CHAPTERS")
    assert len(refs) == 12
    assert len({ref.chapter for ref in refs}) == 12
    tiny = _synthetic_index(20, 4)  # 只有 4 章：放宽到同章不相邻
    refs = compute_selection(tiny, k=6, seed="SC_TINY")
    assert len(refs) == 6 and len({ref.chapter for ref in refs}) == 4
    starts = sorted((ref.start, ref.end) for ref in refs)
    assert all(b[0] > a[1] + 1 for a, b in zip(starts, starts[1:]))


def test_small_book_uses_all_windows() -> None:
    index = _synthetic_index(5, 5)
    refs = compute_selection(index, k=12, seed="SC_SMALL")
    assert sorted(ref.window_no for ref in refs) == [1, 2, 3, 4, 5]


def test_position_quota_for_opening_and_closing_scenes() -> None:
    index = _synthetic_index(300, 60)
    for position in ("opening", "closing"):
        refs = compute_selection(index, k=12, seed=f"SC_{position}", position=position)
        matched = [ref for ref in refs if ref.slot == SLOT_POSITION]
        assert len(matched) == 3 and all(ref.position == position for ref in matched)
        assert refs[:3] == matched  # 位置窗排在选窗顺序最前（评审 / 规划先拿到它们）
    assert selection_quotas(12, has_position=True) == {"position": 3, "situation": 4, "device": 2}
    assert selection_quotas(4, has_position=True) == {"position": 1, "situation": 1, "device": 1}
    assert selection_quotas(12, has_position=False)["position"] == 0


def test_situation_and_device_quotas_use_tagged_windows_only() -> None:
    tags = {no: (("对峙审问",), ()) for no in range(1, 301, 11)}
    tags.update({no: ((), ("倒计时",)) for no in range(5, 301, 13)})
    index = _synthetic_index(300, 60, tags=tags)
    refs = compute_selection(index, k=12, seed="SC_TAGS", situation_tags=["对峙审问"], devices=["倒计时"])
    situation = [ref for ref in refs if ref.slot == SLOT_SITUATION]
    device = [ref for ref in refs if ref.slot == SLOT_DEVICE]
    assert len(situation) == 4 and all("对峙审问" in ref.situations for ref in situation)
    assert len(device) == 2 and all("倒计时" in ref.devices for ref in device)
    # 没打标签的书：标签配额不计，典型度抽样补满
    untagged = compute_selection(_synthetic_index(300, 60), k=12, seed="SC_TAGS", situation_tags=["对峙审问"], devices=["倒计时"])
    assert len(untagged) == 12 and not [ref for ref in untagged if ref.slot in (SLOT_SITUATION, SLOT_DEVICE)]


def test_derive_situation_tags_from_scene_design(session) -> None:
    solo = seed_scene(
        session,
        "INJ_SC_SOLO",
        onstage=("CHAR_A",),
        brief={"scene_form": "reactive", "reaction": "他想起当年的事", "dilemma": "走还是留", "decision": "留下"},
    )
    tags = derive_situation_tags(solo)
    assert tags[0] == "独处内省" and "回忆往事" in tags and len(tags) <= 3
    fight = seed_scene(session, "INJ_SC_FIGHT", brief={"goal": "突围", "conflict": "追兵开枪", "setback": "受伤"})
    assert "打斗追逐" in derive_situation_tags(fight)
    assert derive_situation_tags(None) == ()


def test_dimension_emphasis_and_revise_pull_device_windows(session) -> None:
    book_id, profile_id = seed_reference(session, "devices")
    ensure_window_index(session, book_id)
    numbers = [int(w.window_no) for w in load_windows(session, book_id)]
    tagged = numbers[5:9]
    tag_windows(session, book_id, {no: {"devices": ["倒计时"]} for no in tagged}, devices=("倒计时",))
    policy, _ = _policy_from_existing(
        session, "devices", profile_id, config={"dimension_states": {"narrative.pacing": "emphasize"}}
    )
    draft = render_style(session, policy, StyleRenderRequest(scene_id="SC_DEV", bundle_id="B"))
    device_refs = [ref for ref in draft.window_refs if ref["slot"] == SLOT_DEVICE]
    assert len(device_refs) == 2 and all(ref["window_no"] in tagged for ref in device_refs)
    # 改稿要改「修辞」维（卡里的手法没有窗口示范）→ 不换；要改「节奏」维 → 换进示范倒计时的窗
    plain = render_style(session, policy, StyleRenderRequest(role=ROLE_REVISE, scene_id="SC_DEV", bundle_id="B", revise_dimensions=("language.rhetoric",)))
    assert {r["window_no"] for r in plain.window_refs} == {r["window_no"] for r in draft.window_refs}
    swapped = render_style(session, policy, StyleRenderRequest(role=ROLE_REVISE, scene_id="SC_DEV", bundle_id="B", revise_dimensions=("narrative.pacing",)))
    new = {r["window_no"] for r in swapped.window_refs} - {r["window_no"] for r in draft.window_refs}
    assert 1 <= len(new) <= 2 and new <= set(tagged)


# ---------------------------------------------------------------------------
# 参考方式（N8 / J8）与渲染口径（J16）
# ---------------------------------------------------------------------------


def test_reference_modes_render_what_they_say(session) -> None:
    policy, _ = _policy(session, "modes_full")
    full = render_style(session, policy, StyleRenderRequest(role=ROLE_DRAFT, placement=PLACEMENT_USER_TAIL, scene_id="SC_M"))
    assert "[文风卡]" in full.system_prefix and "[声音特征]" in full.system_prefix and "严格禁止" in full.system_prefix
    assert full.user_tail.count("\n- (第") == 12 and FEW_SHOT_CLOSING_MANDATE in full.user_tail
    assert full.stats["few_shot_windows"] == 12 and full.stats["positive_lines"] >= 3

    samples_policy, _ = _policy(session, "modes_samples", config={"reference_mode": "samples_only"})
    samples = render_style(session, samples_policy, StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="SC_M"))
    assert "[文风卡]" not in samples.system_prefix and "[声音特征]" not in samples.system_prefix
    assert "严格禁止" in samples.system_prefix and samples.stats["few_shot_windows"] == 12

    card_policy, _ = _policy(session, "modes_card", config={"reference_mode": "card_only"})
    card = render_style(session, card_policy, StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="SC_M"))
    assert card.user_tail == "" and card.stats["few_shot_windows"] == 0
    assert "[文风卡]" in card.system_prefix and "（例：「" in card.system_prefix
    example = card.system_prefix.split("（例：「", 1)[1].split("」）", 1)[0]
    assert len(example) <= 60

    seg_policy, _ = _policy(session, "modes_seg", cloud_policy="segments_only")
    assert seg_policy.reference_mode == "card_only"
    seg = render_style(session, seg_policy, StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="SC_M"))
    assert seg.stats["few_shot_windows"] == 0 and "[文风卡]" in seg.system_prefix


def test_no_english_paragraph_types_and_chinese_position_tags(session) -> None:
    policy, _ = _policy(session, "labels")
    rendered = render_style(session, policy, StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="SC_L", position="opening"))
    lines = [line for line in rendered.user_tail.splitlines() if line.startswith("- (")]
    assert len(lines) == 12
    for line in lines:
        tag = line[3 : line.index(")")]
        assert tag.startswith("第") and "章" in tag
        assert not any(word in tag for word in ("narration", "dialogue", "psychology", "连续", "字"))
    assert any("·章首" in line for line in lines)
    assert "本场是本章的第一场" in rendered.user_tail
    assert rendered.user_tail.rstrip().endswith(FEW_SHOT_CLOSING_MANDATE_FINAL)


def test_review_and_plan_roles_get_their_own_headers_in_system(session) -> None:
    policy, _ = _policy(session, "roles")
    review = render_style(session, policy, StyleRenderRequest(role=ROLE_REVIEW, placement=PLACEMENT_USER_TAIL, scene_id="SC_R"))
    plan = render_style(session, policy, StyleRenderRequest(role=ROLE_PLAN, scene_id="SC_R"))
    revise = render_style(session, policy, StyleRenderRequest(role=ROLE_REVISE, placement=PLACEMENT_USER_TAIL, scene_id="SC_R"))
    assert review.user_tail == "" and review.stats["few_shot_windows"] == 4
    assert plan.user_tail == "" and plan.stats["few_shot_windows"] == 3
    assert SAMPLE_HEADERS[ROLE_REVIEW] in review.system_prefix and "写本场时以这些片段的手笔为准" not in review.system_prefix
    assert SAMPLE_HEADERS[ROLE_PLAN] in plan.system_prefix and "写本场时以这些片段的手笔为准" not in plan.system_prefix
    assert review.system_prefix.index("[风格样例]") < review.system_prefix.index("[文风卡]")
    assert "评审时逐维对照" in review.system_prefix
    assert "规划时按这里的气质与手法" in plan.system_prefix
    assert "改完要比原稿更像这位作者" in revise.user_tail and "改稿时逐维对照" in revise.system_prefix
    assert infer_role("user_tail", None) == ROLE_DRAFT
    assert infer_role("system", 3) == ROLE_PLAN and infer_role("system", 4) == ROLE_REVIEW
    assert infer_role("system", None) == ROLE_DRAFT


def test_card_rendering_honours_dimension_and_line_states(session) -> None:
    policy, profile_id = _policy(
        session,
        "states",
        config={"dimension_states": {"theme.emotional_tone": "emphasize", "scene.dialogue": "exclude"}},
    )
    profile = StyleReferenceRepository(session).get_profile(profile_id)
    rhetoric = [d for d in profile.profile_json["dimension_card"]["dimensions"] if d["dimension"] == "language.rhetoric"][0]
    rendered = render_style(session, policy, StyleRenderRequest(scene_id="SC_S", recent_gaps=("逗号比作者少，句子一口气说到底",)))
    card = rendered.system_prefix.split("[文风卡]", 1)[1]
    assert "气质（必须）：危急关头用自嘲冲淡紧张" in card
    assert "【重点】情感基调" in card and "对话写法" not in card
    assert card.index("【重点】情感基调") < card.index("修辞手法")
    assert "[近期常见偏差]" in card and "句子一口气说到底" in card
    assert "[作者不这么写]" in card and "不让天气替人伤心" in card
    # 作者划掉的句不用；钉住的句（哪怕辨识度最低）永远带上
    from novel_system.services.style_reference.card import line_id_for

    low = line_id_for("language.rhetoric", rhetoric["lines"][2]["text"])
    top = line_id_for("language.rhetoric", rhetoric["lines"][0]["text"])
    profile.profile_json = {**profile.profile_json, "card_line_states": {low: "pinned", top: "excluded"}}
    session.commit()
    layers = resolve_binding_layers(session, PROJECT_ID, "scene_generation")
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    card2 = render_style(session, policy_from_contract(contract, mode="frozen"), StyleRenderRequest(scene_id="SC_S")).system_prefix
    assert "一段里常叠两三个比方" in card2 and "紧张处拿日常小物件" not in card2


def test_legacy_profile_renders_a_card_substitute_without_digit_lines(session) -> None:
    policy, _ = _policy(
        session,
        "legacy",
        card=False,
        legacy=True,
        config={"dimension_states": {"theme.values": "exclude"}},
    )
    rendered = render_style(session, policy, StyleRenderRequest(scene_id="SC_LEG"))
    prefix = rendered.system_prefix
    assert "[正向风格特征]" in prefix and "[禁忌模式]" in prefix and "[文风卡]" not in prefix
    assert "名词具体到器物" in prefix and "若解释过多就改回动作" in prefix
    body_lines = [line for line in prefix.splitlines() if line.startswith("- [") or line.startswith("- 不") or line.startswith("- 禁")]
    assert body_lines and not any(ch.isdigit() for line in body_lines for ch in line)
    assert "29" not in prefix and "3 轮" not in prefix and "每 3 段" not in prefix
    assert "不把宏大使命写得比日子重" not in prefix  # 不学的维（theme.values）的禁忌不出现
    assert "被驳回的禁忌" not in prefix
    assert "风格分布指导" not in prefix and "[风格检索样例]" not in prefix
    audit = rendered.audit
    assert audit["legacy_profile"] is True and audit["legacy_digit_lines_dropped"] >= 3


def test_red_line_carries_protected_terms_and_is_never_truncated(session) -> None:
    policy, _ = _policy(session, "redline", terms=("甲乙社", "丙丁剑"))
    rendered = render_style(session, policy, StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="SC_RL"))
    assert "- 丙丁剑" in rendered.system_prefix and "- 甲乙社" in rendered.system_prefix
    red_line = rendered.system_prefix[rendered.system_prefix.index("## 严格禁止") :]
    parts = rendered.parts
    base = "系" * 5000
    base_tokens = estimate_tokens(base) + estimate_tokens("用")
    card = estimate_tokens(parts.card.render())
    one_window = max(estimate_tokens(w.line) for w in parts.windows)
    abstract = card + estimate_tokens(parts.voice) + estimate_tokens(parts.red_line) + estimate_tokens(parts.closing) + 200
    # 一窗 + 卡装得下：去掉多余的窗，卡与红线原样
    fitted, audit = fit_rendered(rendered, base_system_prompt=base, user_prompt="用", target_input_tokens=base_tokens + abstract + one_window + 300)
    assert audit["compacted"] and fitted.system_prefix.endswith(red_line)
    assert 1 <= fitted.stats["few_shot_windows"] < 12 and fitted.stats["positive_lines"] == rendered.stats["positive_lines"]
    # 一窗都放不下：不要样例窗，卡、声音、红线放回来
    no_windows, nw_audit = fit_rendered(rendered, base_system_prompt=base, user_prompt="用", target_input_tokens=base_tokens + abstract)
    assert no_windows.stats["few_shot_windows"] == 0 and no_windows.user_tail == ""
    assert no_windows.system_prefix.endswith(red_line) and "[文风卡]" in no_windows.system_prefix
    assert nw_audit["policy"].endswith("no_windows_v3") or nw_audit["policy"] == "drop_sample_windows_v3"
    # 连红线都放不下：整份参考都不发（红线从不被截半）
    starved, starved_audit = fit_rendered(rendered, base_system_prompt=base, user_prompt="用", target_input_tokens=base_tokens + 10)
    assert starved.system_prefix == "" and starved.user_tail == "" and starved_audit["style_payload_omitted"]


def test_greedy_fit_sheds_windows_before_card_lines_and_is_fast(session) -> None:
    policy, _ = _policy(session, "greedy")
    rendered = render_style(session, policy, StyleRenderRequest(role=ROLE_DRAFT, scene_id="SC_G"))  # system 落点：样例在前缀里
    full_card = rendered.stats["positive_lines"]
    started = time.perf_counter()
    fitted, audit = fit_rendered(rendered, base_system_prompt="", user_prompt="u", target_input_tokens=estimate(rendered) // 2)
    elapsed = time.perf_counter() - started
    assert audit["policy"] == "shed_sample_windows_v3" and fitted.stats["positive_lines"] == full_card
    assert 1 <= fitted.stats["few_shot_windows"] < 12
    assert elapsed < 1.0
    parts = rendered.parts
    first = next(w for w in parts.windows if w.priority == 0)  # 选窗顺序第一的窗最后才去
    tight = estimate_tokens(first.line) + estimate_tokens(parts.red_line) + estimate_tokens(parts.samples_header) + 120
    lean, lean_audit = fit_rendered(rendered, base_system_prompt="", user_prompt="u", target_input_tokens=tight)
    assert lean.stats["few_shot_windows"] == 1 and lean_audit["dropped_card_units"] >= 1
    assert lean.stats["positive_lines"] < full_card


def estimate(rendered) -> int:
    return estimate_tokens(rendered.system_prefix) + estimate_tokens(rendered.user_tail)


# ---------------------------------------------------------------------------
# 书被改过（J6）
# ---------------------------------------------------------------------------


def test_root_mismatch_renders_from_the_live_index_with_a_notice(session) -> None:
    book_id, profile_id = seed_reference(session, "changed")
    policy, contract = _policy_from_existing(session, "changed", profile_id)
    before = render_style(session, policy, StyleRenderRequest(scene_id="SC_CH"))
    assert NOTICE_BOOK_CHANGED not in before.audit["notices"]
    last = session.scalars(
        select(StyleReferenceParagraph).where(StyleReferenceParagraph.book_id == book_id).order_by(StyleReferenceParagraph.paragraph_index.desc())
    ).first()
    last.text = last.text + "又添了一句。"
    book = session.get(StyleReferenceBook, book_id)
    stats = dict(book.stats_json)
    stats.pop("paragraph_root_sha256", None)
    stats.pop("paragraph_count", None)
    book.stats_json = stats
    session.commit()
    reset_render_cache()
    after = render_style(session, policy, StyleRenderRequest(scene_id="SC_CH2"))
    assert NOTICE_BOOK_CHANGED in after.audit["notices"]
    assert after.stats["few_shot_windows"] == 12  # 不再悄悄砍样例


# ---------------------------------------------------------------------------
# 契约 v2（J1 / J6 / J7 / E9）
# ---------------------------------------------------------------------------


def test_contract_v2_freezes_one_slim_layer(session) -> None:
    book_id, profile_id = seed_reference(session, "v2", legacy=True, card=False)
    _b2, char_profile = seed_reference(session, "v2char")
    _b3, other_profile = seed_reference(session, "v2other")
    bind(session, profile_id, binding_id="v2_project")
    bind(session, other_profile, binding_id="v2_char_b", scope="character", scope_ref_id="CHAR_B")
    bind(session, char_profile, binding_id="v2_char_pov", scope="character", scope_ref_id="CHAR_A")
    layers = resolve_binding_layers(session, PROJECT_ID, "scene_generation", character_ids=["CHAR_A", "CHAR_B"])
    assert [b.binding_id for b in layers] == ["v2_project", "v2_char_pov", "v2_char_b"]
    assert most_specific_binding(layers).binding_id == "v2_char_pov"  # POV 优先，不是最后一层
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    assert contract["contract_version"] == STYLE_RUNTIME_CONTRACT_VERSION_V2 and contract["schema_version"] == 2
    assert contract["layer_count"] == 1 and contract["binding_ids"] == ["v2_char_pov"]
    layer = contract["layers"][0]
    assert "sample_quote_refs" not in layer and "sample_paragraph_refs" not in layer
    assert set(layer["binding"]["config_json"]) == {"reference_mode", "sample_windows", "dimension_states", "draft_mode"}
    book = layer["book"]
    assert book["cloud_policy"] == "allow_full_cloud" and book["cloud_llm_allowed_at_freeze"] is True
    assert len(book["paragraph_root_sha256"]) == 64 and book["paragraph_count"] > 0
    assert "chapters" not in (layer["profile"]["profile_json"].get("structure_card") or {})
    # 旧画像：只冻结白名单里的兼容键；样例索引 / 窗口索引 / 子维统计不进契约
    legacy = build_style_runtime_contract(StyleReferenceRepository(session), [layers[0]], task_type="scene_generation")
    frozen_json = legacy["layers"][0]["profile"]["profile_json"]
    assert {"style_features", "narrative_patterns", "metrics_baseline"} <= set(frozen_json)
    assert not {"scene_samples_index", "exemplar_windows", "sub_dimensions", "generation_safe_forbidden_findings"} & set(frozen_json)
    assert [f["finding_id"] for f in legacy["layers"][0]["forbidden_findings"]] == ["f1", "f2", "f3", "f4"]
    assert len(json.dumps(legacy, ensure_ascii=False)) < 20000
    # 根哈希存进了书的 stats（下次构建不再现算）
    assert session.get(StyleReferenceBook, book_id).stats_json.get("paragraph_root_sha256")


def test_contract_v2_rejects_sample_refs_and_tampering(session) -> None:
    policy, contract = _policy_from_existing(session, "tamper", seed_reference(session, "tamper")[1])
    assert validate_style_runtime_contract(contract) == contract
    tampered = json.loads(json.dumps(contract))
    tampered["layers"][0]["binding"]["config_json"]["sample_windows"] = 3
    with pytest.raises(ValueError):
        validate_style_runtime_contract(tampered)
    with_refs = json.loads(json.dumps(contract))
    with_refs["layers"][0]["sample_quote_refs"] = []
    layer = dict(with_refs["layers"][0])
    layer.pop("layer_hash")
    with_refs["layers"][0]["layer_hash"] = _json_hash(layer)
    body = dict(with_refs)
    body.pop("contract_hash")
    with_refs["contract_hash"] = _json_hash(body)
    with pytest.raises(ValueError, match="sample references"):
        validate_style_runtime_contract(with_refs)


def test_v1_contracts_in_old_bundles_still_validate_and_render(session) -> None:
    book_id, profile_id = seed_reference(session, "v1")
    bind(session, profile_id, binding_id="v1_bind")
    layer = {
        "order": 0,
        "binding": {
            "binding_id": "v1_bind",
            "profile_id": profile_id,
            "scope": "project",
            "scope_ref_id": PROJECT_ID,
            "task_type": "scene_generation",
            "strategy": "mixed",
            "status": "active",
            "config_json": {"intensity": 100},
        },
        "profile": {
            "profile_id": profile_id,
            "book_id": book_id,
            "run_id": "inj_run_v1",
            "version_tag": "",
            "status": "active",
            "profile_json": {"voice_signature": {"habits": list(VOICE_HABITS)}},
            "source_finding_ids_json": [],
        },
        "forbidden_findings": [],
        "banned_terms": [],
        "sample_quote_refs": [],
        "sample_paragraph_refs": [],
        "book": {"book_id": book_id, "text_checksum": "x", "cloud_llm_allowed_at_freeze": True},
    }
    layer["layer_hash"] = _json_hash(layer)
    contract = {
        "schema_version": 1,
        "contract_version": STYLE_RUNTIME_CONTRACT_VERSION_V1,
        "task_type": "scene_generation",
        "profile_ids": [profile_id],
        "binding_ids": ["v1_bind"],
        "layer_count": 1,
        "layers": [layer],
    }
    contract["contract_hash"] = _json_hash(contract)
    assert validate_style_runtime_contract(contract)["contract_hash"] == contract["contract_hash"]
    scene = seed_scene(session, "SC_V1")
    bundle = {
        "bundle_id": "B_V1",
        "snapshot": {
            "source_version_refs": {"style_reference_runtime_contract_status": "frozen"},
            "inline_digests": {"_style_reference_runtime_contract": json.dumps(contract)},
        },
    }
    injected = inject_style_reference_prefix(session, {"system_prompt": ""}, scene, bundle, placement=PLACEMENT_USER_TAIL)
    audit = injected["_style_reference_runtime_audit"]
    assert audit["outcome"] == "hit" and audit["runtime_contract_mode"] == "frozen"
    assert audit["render_stats"]["few_shot_windows"] == 12 and "[声音特征]" in injected["system_prompt"]
    assert audit["policy"]["draft_mode"] == "neutral_first"  # v1 缺 draft_mode → 先中性


def test_validation_is_memoised_and_returns_fresh_copies(session, monkeypatch) -> None:
    from novel_system.services.style_reference import runtime_contract as rc

    _policy_obj, contract = _policy_from_existing(session, "memo", seed_reference(session, "memo")[1])
    calls = {"v2": 0}
    original = rc._validate_v2

    def counting(payload):
        calls["v2"] += 1
        return original(payload)

    monkeypatch.setattr(rc, "_validate_v2", counting)
    reset_contract_memo()
    first = validate_style_runtime_contract(contract)
    first["layers"][0]["binding"]["scope"] = "mutated"
    second = validate_style_runtime_contract(contract)
    assert calls["v2"] == 1 and second["layers"][0]["binding"]["scope"] == "project"
    raw = json.dumps(contract)
    bundle = {"inline_digests": {"_style_reference_runtime_contract": raw}}
    assert style_runtime_contract_from_bundle(bundle)["contract_hash"] == contract["contract_hash"]
    assert style_runtime_contract_from_bundle(bundle)["contract_hash"] == contract["contract_hash"]
    assert calls["v2"] == 1


# ---------------------------------------------------------------------------
# 适配器（审计形状、现解析、缓存）
# ---------------------------------------------------------------------------


def test_adapter_audit_shape_and_user_tail(session) -> None:
    _book_id, profile_id = seed_reference(session, "adapter")
    bind(session, profile_id, binding_id="inj_bind_adapter")
    scene = seed_scene(session, "INJ_SC_ADAPTER", scene_seq=1)
    bundle = frozen_bundle(session, bundle_id="B_ADAPTER", scene=scene)
    prompt = {"system_prompt": "BASE", "token_budget": {"target_input_tokens": 200000}}
    injected = inject_style_reference_prefix(
        session, prompt, scene, bundle, placement=PLACEMENT_USER_TAIL, final_user_prompt="写这一场。"
    )
    assert injected["system_prompt"].startswith("[STYLE_REFERENCE]\n") and injected["system_prompt"].endswith("BASE")
    tail = injected[STYLE_USER_TAIL_KEY]
    assert apply_style_user_tail(injected, "写这一场。").endswith(tail)
    audit = injected["_style_reference_runtime_audit"]
    for key in (
        "outcome",
        "role",
        "placement",
        "reference_mode",
        "contract_hash",
        "profile_ids",
        "binding_ids",
        "render_stats",
        "few_shot_window_refs",
        "selection",
        "blocks",
        "notices",
        "legacy_profile",
        "prefix_chars",
        "prefix_sha256",
        "runtime_contract_status",
        "runtime_contract_mode",
        "budget_fit",
    ):
        assert key in audit, key
    assert audit["outcome"] == "hit" and audit["role"] == "draft" and audit["placement"] == "user_tail"
    assert audit["runtime_contract_status"] == "frozen" and audit["profile_ids"] == [profile_id]
    assert audit["budget_fit"]["policy"] == "no_compaction_needed"
    ref = audit["few_shot_window_refs"][0]
    assert {"start", "end", "chapter", "position", "paragraphs", "chars", "window_no", "slot"} <= set(ref)
    text = json.dumps(audit, ensure_ascii=False)
    assert "灯芯" not in text and "渡口" not in text  # 审计不含正文
    # 章首场：位置窗排在最前，尾块有开章补充
    assert "本场是本章的第一场" in tail


def test_adapter_live_path_records_the_contract_hash(session) -> None:
    _book_id, profile_id = seed_reference(session, "live")
    bind(session, profile_id, binding_id="inj_bind_live")
    scene = seed_scene(session, "INJ_SC_LIVE")
    injected = inject_style_reference_prefix(session, {"system_prompt": ""}, scene, None, few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP)
    audit = injected["_style_reference_runtime_audit"]
    assert audit["runtime_contract_mode"] == "live" and audit["runtime_contract_status"] == "live"
    assert len(audit["contract_hash"]) == 64 and audit["role"] == "plan"
    assert audit["render_stats"]["few_shot_windows"] == 3
    # 没有绑定 → 严格 no-op
    other = seed_scene(session, "INJ_SC_NONE", project_id="PRJ_OTHER", chapter_id="OTHER_CH")
    assert inject_style_reference_prefix(session, {"system_prompt": "X"}, other, None) == {"system_prompt": "X"}


def test_adapter_absent_and_degraded_bundles(session) -> None:
    scene = seed_scene(session, "INJ_SC_ABSENT")
    absent = {"source_version_refs": {"style_reference_runtime_contract_status": "absent"}, "inline_digests": {}}
    assert inject_style_reference_prefix(session, {"system_prompt": "X"}, scene, absent) == {"system_prompt": "X"}
    broken = {
        "source_version_refs": {"style_reference_runtime_contract_status": "frozen"},
        "inline_digests": {"_style_reference_runtime_contract": "{not json"},
    }
    degraded = inject_style_reference_prefix(session, {"system_prompt": "X"}, scene, broken)
    assert degraded["system_prompt"] == "X"
    assert degraded["_style_reference_runtime_audit"]["outcome"] == "degraded"


def test_render_is_cached_per_scene_and_role(session, monkeypatch) -> None:
    from novel_system.services.style_reference.inject import render as render_module

    policy, _ = _policy(session, "cache")
    calls = {"n": 0}
    original = render_module.resolve_scene_selection

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(render_module, "resolve_scene_selection", counting)
    request = StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="SC_CACHE", bundle_id="B")
    first = render_style(session, policy, request)
    second = render_style(session, policy, request)
    assert first is second and calls["n"] == 1
    render_style(session, policy, StyleRenderRequest(role=ROLE_REVIEW, scene_id="SC_CACHE", bundle_id="B"))
    assert calls["n"] == 2


def test_recent_gaps_reach_the_first_draft_card(session) -> None:
    from novel_system.db.models import StyleFidelityReading

    _book_id, profile_id = seed_reference(session, "gaps")
    bind(session, profile_id, binding_id="inj_bind_gaps")
    scene = seed_scene(session, "INJ_SC_GAPS")
    for index in range(4):
        session.add(
            StyleFidelityReading(
                reading_id=f"rd_{index}",
                project_id=PROJECT_ID,
                scene_id=f"S{index}",
                profile_id=profile_id,
                source="pipeline",
                stage="first_draft",
                text_sha256="0" * 64,
                reading_json={"out_of_band": [{"feature": "punct_comma_per_1k", "direction": "low", "z": -2.5}]},
                created_at=f"2026-09-23T00:00:0{index}",
            )
        )
    session.commit()
    injected = inject_style_reference_prefix(session, {"system_prompt": ""}, scene, None, placement=PLACEMENT_USER_TAIL)
    assert "[近期常见偏差]" in injected["system_prompt"] and "逗号比作者少" in injected["system_prompt"]
    review = inject_style_reference_prefix(session, {"system_prompt": ""}, scene, None, role="review")
    assert "[近期常见偏差]" not in review["system_prompt"]


# ---------------------------------------------------------------------------
# 预览（J12）与轻量层查询（U10）
# ---------------------------------------------------------------------------


def test_preview_uses_the_drafting_selection_and_order(session) -> None:
    _book_id, profile_id = seed_reference(session, "preview")
    bind(session, profile_id, binding_id="inj_bind_preview")
    scene = seed_scene(session, "INJ_SC_PREVIEW", scene_seq=1)
    bundle = frozen_bundle(session, bundle_id="B_PREVIEW", scene=scene)
    drafted = inject_style_reference_prefix(session, {"system_prompt": ""}, scene, bundle, placement=PLACEMENT_USER_TAIL)
    session.commit()
    preview = preview_render(session, profile_id, {}, scene_id=scene.scene_id)
    assert [r["window_no"] for r in preview["window_refs"]] == _refs(drafted["_style_reference_runtime_audit"])
    assert preview["prefix"].index("[文风卡]") < preview["prefix"].index("[声音特征]") < preview["prefix"].index("严格禁止")
    assert preview["user_tail"].lstrip().startswith("[风格样例]")
    assert set(preview["stats"]) >= {"positive_lines", "few_shot_windows", "total_prefix_chars", "few_shot_k"}
    assert preview["fragments"]["few_shot_block"].count("\n- (第") == 12
    # 预览不写冻结行（起草那一行仍是唯一的一行）
    assert session.scalar(select(func.count()).select_from(StyleReferenceSceneWindows)) == 1
    card_only = preview_render(session, profile_id, {"reference_mode": "card_only"})
    assert card_only["stats"]["few_shot_windows"] == 0 and card_only["fragments"]["strategy"] == "A"


def test_describe_binding_layers_is_cheap_and_marks_the_applied_layer(session, monkeypatch) -> None:
    from novel_system.services.style_reference.inject import render as render_module

    _b1, project_profile = seed_reference(session, "layers_p")
    _b2, scene_profile = seed_reference(session, "layers_s")
    bind(session, project_profile, binding_id="layers_project")
    bind(session, scene_profile, binding_id="layers_scene", scope="scene", scope_ref_id="SC_LAYERS", config_json={"sample_windows": 8})
    monkeypatch.setattr(render_module, "render_style", lambda *a, **k: pytest.fail("describe must not render"))
    data = describe_binding_layers(session, PROJECT_ID, "scene_generation", scene_id="SC_LAYERS")
    assert [layer["binding_id"] for layer in data["layers"]] == ["layers_project", "layers_scene"]
    assert [layer["applied"] for layer in data["layers"]] == [False, True]
    assert data["merged"]["binding_id"] == "layers_scene" and data["merged"]["sample_windows"] == 8
    assert [item["binding_id"] for item in data["deduplicated"]] == ["layers_project"]


# ---------------------------------------------------------------------------
# 规划层（L5）与适配器的调用方
# ---------------------------------------------------------------------------


def test_planning_context_gives_card_scene_and_theme_lines_with_temperament(session) -> None:
    from novel_system.services.style_reference.planning_context import (
        render_card_planning_guidance,
        resolve_project_style_reference,
    )

    _book_id, profile_id = seed_reference(session, "planning")
    bind(session, profile_id, binding_id="inj_bind_planning", config_json={"dimension_states": {"theme.emotional_tone": "emphasize"}})
    reference = resolve_project_style_reference(session, PROJECT_ID)
    guidance = reference["planning_guidance"]
    assert guidance.startswith("[场景手法]")
    lines = guidance.splitlines()[1:]
    assert lines[0] == "- 气质（必须）：危急关头用自嘲冲淡紧张"
    assert lines[1].startswith("- 【重点】情感基调：") and "惊险里夹着吐槽" in guidance
    assert "对话写法：对白你来我往地抢话" in guidance
    # 语言层 / 叙事层的句子不进规划（它们是起草的事）
    assert "紧张处拿日常小物件" not in guidance and "倒计时" not in guidance
    # 没有文风卡的旧画像照旧用 [场景手法] 观察陈述（这里没有 → 空）
    assert render_card_planning_guidance({"planning_guidance": ["场景：旧陈述"]}) == ""
    excluded = render_card_planning_guidance(
        StyleReferenceRepository(session).get_profile(profile_id).profile_json,
        {"theme.emotional_tone": "exclude", "scene.dialogue": "exclude"},
    )
    assert "情感基调" not in excluded and "对话写法" not in excluded and "气质" in excluded


def test_near_final_review_injects_style_prefix_and_degrades_on_error(session, monkeypatch) -> None:
    """准定稿评审复用模块级注入器；注入器抛错时回退基础提示（可选增强，不阻断评审）。"""
    from types import SimpleNamespace

    from novel_system.services import near_final as near_final_module
    from novel_system.services import style_prompt_injection as spi

    service = near_final_module.NearFinalAcceptanceService(session)
    scene = SimpleNamespace(scene_id="SC1", project_id="P1", pov_character_id=None, onstage_chars_json=[])
    calls: list[dict] = []

    def _fake_inject(sess, prompt, scene_arg, bundle, *, task_type, context_text, final_user_prompt, **_kwargs):
        calls.append({"task_type": task_type, "user": final_user_prompt})
        return {**prompt, "system_prompt": "[STYLE_REFERENCE]\nX\n[/STYLE_REFERENCE]\n\n" + prompt["system_prompt"]}

    monkeypatch.setattr(spi, "inject_style_reference_prefix", _fake_inject)
    injected = service._inject_style_reference_prefix(
        {"system_prompt": "base", "user_prompt": "u"}, scene, {"bundle_id": "b"}, context_text="正文", final_user_prompt="u + 正文"
    )
    assert injected["system_prompt"].startswith("[STYLE_REFERENCE]")
    assert calls == [{"task_type": "scene_generation", "user": "u + 正文"}]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injector down")

    monkeypatch.setattr(spi, "inject_style_reference_prefix", _boom)
    degraded = service._inject_style_reference_prefix(
        {"system_prompt": "base", "user_prompt": "u"}, scene, {"bundle_id": "b"}, context_text="正文", final_user_prompt="u"
    )
    assert degraded == {"system_prompt": "base", "user_prompt": "u"}


def test_v1_multi_layer_contracts_read_the_most_specific_layer() -> None:
    """旧 bundle 的 v1 多层契约：层序是「由泛到具体」且角色层 POV 在前——生效层按作用域挑，不取最后一层（J7）。"""
    from novel_system.services.style_reference.runtime_contract import contract_layer

    def _layer(order: int, scope: str, binding_id: str) -> dict:
        return {
            "order": order,
            "binding": {"binding_id": binding_id, "scope": scope, "strategy": "mixed", "config_json": {}},
            "profile": {"profile_id": f"p_{binding_id}", "book_id": "b"},
            "book": {"book_id": "b"},
        }

    contract = {
        "layers": [_layer(0, "project", "proj"), _layer(1, "character", "pov"), _layer(2, "character", "other")],
        "contract_hash": "h" * 64,
    }
    assert contract_layer(contract)["binding"]["binding_id"] == "pov"
    assert policy_from_contract(contract, mode="frozen").binding_id == "pov"
    contract["layers"].append(_layer(3, "scene", "scene"))
    assert contract_layer(contract)["binding"]["binding_id"] == "scene"
    assert contract_layer({"layers": []}) == {} and contract_layer(None) == {}
