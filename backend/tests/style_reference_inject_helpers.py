"""风格参考 v3 注入测试的共享夹具（合成书、文风卡画像、旧画像、绑定、场景、冻结了契约的 bundle）。

通用的造数（书 + 段落、画像、绑定、窗口、合成文风卡）在 ``tests/style_reference_factories.py``；这里是注入测试专用的组合。

全部是合成内容（无真实作者原文 / 作品人物）：段落由 ``test_style_reference_windows.synthetic_rows`` 按种子生成，
文风卡的句子是泛化的写法描述。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    StoryProject,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION,
    build_style_runtime_contract,
)
from novel_system.services.style_reference.windows import load_windows
from tests.style_reference_factories import (
    DEVICES,
    VOICE_HABITS,
    card_payload,
    make_binding,
    make_book,
    make_windows,
)
from tests.test_style_reference_windows import synthetic_rows

RIGHTS = {"rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True}}
PROJECT_ID = "PRJ_INJ3"

LEGACY_PROFILE_JSON = {
    "qualitative_summary": "克制观察，动作先于解释；对白短促。",
    "style_features": ["用「便」「却」承接，少用「然而」", "平均句长约 29 字，短句占两成", "名词具体到器物"],
    "narrative_patterns": ["关键信息放段首一次给出", "每 3 段插入一次回忆"],
    "calibration_guidance": ["若解释过多就改回动作"],
    "banned_replication_rules": ["禁复用参考书的专名与独特意象"],
    "generation_safe_forbidden_findings": [
        {"finding_id": "f1", "sub_dimension": "language.rhetoric", "statement": "不让天气替人伤心", "status": "pending"},
        {"finding_id": "f2", "sub_dimension": "theme.values", "statement": "不把宏大使命写得比日子重", "status": "pending"},
        {"finding_id": "f3", "sub_dimension": "scene.dialogue", "statement": "对白不超过 3 轮无动作", "status": "pending"},
        {"finding_id": "f4", "sub_dimension": "scene.dialogue", "statement": "被驳回的禁忌", "status": "rejected"},
    ],
    "metrics_baseline": {"avg_sentence_length": {"mean": 20.0, "std": 5.0}},
    "scene_samples_index": {"narration": ["q1", "q2"]},
    "exemplar_windows": {"version": "exemplar_windows_v2", "windows": [{"start": 0}]},
    "sub_dimensions": {"language.rhetoric": {"observation_count": 3}},
}


# 旧画像（学习作业跑之前的形状）：v2 合成期的正向 / 叙事 / 校准 / 禁忌列表 + 声音习惯 + 叙事机制 + 量化基线。
# 句子都是泛化的写法描述（无真实作者原文）。取代原 test_style_reference_injection_v2._profile_json。
LEGACY_NARRATIVE_PATTERNS = [
    "关键信息放段首一次给出，之后不回头解释",
    "对白之间用一两句动作把停顿落到实物上",
    "情绪只在动作里泄露，不直接命名",
    "场景收束落在一个具体物件或声音上",
    "回忆只以一句嵌进当下动作，不另起段",
    "人物的判断后置，先给可见线索",
]
LEGACY_STYLE_FEATURES = [
    "用「便」「却」承接，少用「然而」「于是」",
    "对白多无引导词，有引导词时置于引语后",
    "短句主导，连续短句切断长句的地方多在转折处",
    "逗号密集、句号稀疏，一句常含三到四个停顿",
    "四字格偏低，不堆成语",
    "名词具体到器物层面，形容词克制",
]
LEGACY_CALIBRATION = ["若解释过多就改回动作", "若比喻密集就删到只剩一个"]
LEGACY_BANNED_RULES = ["禁复用参考书的专名与独特意象", "禁堆砌华丽形容词", "禁在段末点题"]


def legacy_profile_json(*, with_voice: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "narrative_summary": "克制观察，动作先于解释；对白短促，停顿落在器物上。",
        "qualitative_summary": "克制观察，动作先于解释；对白短促，停顿落在器物上。",
        "style_features": list(LEGACY_STYLE_FEATURES),
        "narrative_patterns": list(LEGACY_NARRATIVE_PATTERNS),
        "calibration_guidance": list(LEGACY_CALIBRATION),
        "banned_replication_rules": list(LEGACY_BANNED_RULES),
        "narrative_guidance": list(LEGACY_NARRATIVE_PATTERNS[:5]),
        "metrics_baseline": {
            "avg_sentence_length": {"mean": 11.0, "std": 3.0},
            "short_sentence_ratio": {"mean": 0.45, "std": 0.08},
            "paragraph_mean_chars": {"mean": 62.0, "std": 20.0},
        },
    }
    if with_voice:
        payload["voice_signature"] = {
            "version": "voice_signature_v1",
            "features": {"sent_len_mean": 11.0},
            "habits": list(VOICE_HABITS),
            "deliberate_repetition": False,
        }
    return payload


def seed_full(
    seed: str,
    *,
    with_voice: bool = True,
    cloud_policy: str = "allow_full_cloud",
    chapters: int = 14,
    per_chapter: int = 100,
) -> tuple[str, str]:
    """合成书（每章约两窗）+ 旧画像；返回 (book_id, profile_id)。自开会话并提交。"""
    from novel_system.db.session import SessionLocal

    with SessionLocal() as session:
        return seed_reference(
            session,
            seed,
            chapters=chapters,
            per_chapter=per_chapter,
            card=False,
            cloud_policy=cloud_policy,
            profile_json=legacy_profile_json(with_voice=with_voice),
        )


def bind_profile(
    repo: StyleReferenceRepository,
    *,
    binding_id: str,
    profile_id: str,
    scope: str,
    scope_ref_id: str,
    strategy: str = "mixed",
    config_json: dict[str, Any] | None = None,
):
    """与原 ``test_style_reference_injection_v2._bind`` 同形（调用方自己提交）。"""
    return make_binding(
        repo.session,
        profile_id,
        binding_id=binding_id,
        scope=scope,
        scope_ref_id=scope_ref_id,
        strategy=strategy,
        config_json=config_json,
    )


def seed_synthetic_book(
    session: Session,
    book_id: str,
    rows: list[dict[str, Any]],
    *,
    stats: dict[str, Any] | None = None,
    cloud_policy: str = "allow_full_cloud",
) -> str:
    """合成书 + 段落（一次批量写入；每章约 6,000 字 → 章首 / 章末两窗）。"""
    make_book(session, book_id, title="合成书", paragraphs=rows, stats=stats, cloud_policy=cloud_policy)
    session.commit()
    return book_id


def seed_reference(
    session: Session,
    key: str,
    *,
    chapters: int = 16,
    per_chapter: int = 110,
    card: bool = True,
    legacy: bool = False,
    cloud_policy: str = "allow_full_cloud",
    rights: bool = True,
    profile_json: dict[str, Any] | None = None,
    terms: tuple[str, ...] = (),
) -> tuple[str, str]:
    """合成书 + 段落 + 画像（文风卡 / 旧画像 / 两者）+ 生成期禁用词；返回 (book_id, profile_id)。"""
    book_id = seed_synthetic_book(
        session,
        f"inj_book_{key}",
        synthetic_rows(key, chapters=chapters, per_chapter=per_chapter),
        stats=RIGHTS if rights else {},
        cloud_policy=cloud_policy,
    )
    repo = StyleReferenceRepository(session)
    run_id = f"inj_run_{key}"
    repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
    payload: dict[str, Any] = dict(profile_json or {})
    if profile_json is None:
        payload["voice_signature"] = {"version": "voice_signature_v1", "habits": list(VOICE_HABITS), "deliberate_repetition": False}
        if legacy:
            payload.update(json.loads(json.dumps(LEGACY_PROFILE_JSON)))
        if card:
            quote_id = f"inj_quote_{key}"
            repo.create_quote(
                quote_id=quote_id,
                book_id=book_id,
                paragraph_id=f"{book_id}_p00003",
                span_start=0,
                span_end=10,
                quote_text="他把灯芯拨小了些，屋里的影子便大了一圈，像有人在墙上慢慢站起来又坐下去，坐了很久很久",
                illustrates_dims=["language.rhetoric"],
                extracted_features={},
            )
            payload["dimension_card"] = card_payload(with_evidence=True, quote_id=quote_id)
    profile_id = f"inj_profile_{key}"
    repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=run_id,
        title="合成风格",
        status="active",
        profile_json=payload,
        coverage_json={},
        source_finding_ids_json=[],
    )
    for index, term in enumerate(terms):
        repo.create_banned_term(
            term_id=f"inj_term_{key}_{index}",
            profile_id=profile_id,
            term=term,
            source="protected_auto",
            scope="generation",
        )
    session.commit()
    return book_id, profile_id


def bind(
    session: Session,
    profile_id: str,
    *,
    binding_id: str,
    scope: str = "project",
    scope_ref_id: str = PROJECT_ID,
    strategy: str = "mixed",
    config_json: dict[str, Any] | None = None,
):
    binding = make_binding(
        session,
        profile_id,
        binding_id=binding_id,
        scope=scope,
        scope_ref_id=scope_ref_id,
        strategy=strategy,
        config_json=config_json,
    )
    session.commit()
    return binding


def seed_scene(
    session: Session,
    scene_id: str,
    *,
    project_id: str = PROJECT_ID,
    chapter_id: str = "INJ_CH01",
    scene_seq: int | None = None,
    is_last: bool = False,
    pov: str = "CHAR_A",
    onstage: tuple[str, ...] = ("CHAR_A", "CHAR_B"),
    brief: dict[str, Any] | None = None,
) -> SceneCard:
    if session.get(StoryProject, project_id) is None:
        session.add(StoryProject(project_id=project_id, title="合成作品", outline_text=""))
    if session.get(ChapterGoal, chapter_id) is None:
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                planned_scene_count=3,
                chapter_goal="一次追问变成一个选择。",
                writer_brief_json={},
            )
        )
    if scene_seq is None:
        # 不指定时取本章下一个空位（≥2：默认不是章首场）
        existing = session.scalar(select(func.max(SceneCard.scene_seq)).where(SceneCard.chapter_id == chapter_id))
        scene_seq = max(int(existing or 0), 1) + 1
    scene = SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=scene_seq,
        pov_character_id=pov,
        onstage_chars_json=list(onstage),
        location="渡口",
        scene_goal="逼问船什么时候到",
        beats_json=["追问", "回避", "决定自己去等"],
        exit_change="决定独自等船。",
        hook="",
        writer_brief_json=dict(brief or {"scene_form": "proactive", "goal": "逼问船家", "conflict": "船家回避", "setback": "船不来了"}),
        target_length_band="medium",
        scene_type="proactive",
        is_chapter_last=1 if is_last else 0,
    )
    session.add(scene)
    session.commit()
    return scene


def frozen_bundle(session: Session, *, bundle_id: str, project_id: str = PROJECT_ID, scene: SceneCard | None = None) -> dict[str, Any]:
    """冻结了当前生效绑定的契约的 bundle（与 BundleBuilder 写的键同形）。"""
    from novel_system.services.style_reference.inject.bindings import (
        ordered_character_ids,
        resolve_binding_layers,
    )

    layers = resolve_binding_layers(
        session,
        project_id,
        "scene_generation",
        character_ids=ordered_character_ids(
            getattr(scene, "pov_character_id", None), getattr(scene, "onstage_chars_json", None)
        ),
        scene_id=getattr(scene, "scene_id", None),
    )
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    session.commit()
    return {
        "bundle_id": bundle_id,
        "snapshot": {
            "source_version_refs": {
                "style_reference_runtime_contract_version": STYLE_RUNTIME_CONTRACT_VERSION,
                "style_reference_runtime_contract_status": "frozen",
                "style_reference_runtime_contract_hash": contract["contract_hash"],
            },
            "inline_digests": {
                "_style_reference_runtime_contract": json.dumps(contract, ensure_ascii=False, sort_keys=True),
            },
        },
    }


def tag_windows(session: Session, book_id: str, tags: dict[int, dict[str, Any]]) -> int:
    """按窗口号直接写 v2 标签（``{situations, moods, dimensions, gist}``）；返回写了几窗。"""
    numbers = {int(w.window_no) for w in make_windows(session, book_id, tags=tags)}
    session.commit()
    return sum(1 for no in tags if int(no) in numbers)


def window_numbers(session: Session, book_id: str) -> list[int]:
    return [int(w.window_no) for w in load_windows(session, book_id)]


__all__ = [
    "DEVICES",
    "LEGACY_NARRATIVE_PATTERNS",
    "LEGACY_STYLE_FEATURES",
    "bind_profile",
    "legacy_profile_json",
    "seed_full",
    "LEGACY_PROFILE_JSON",
    "PROJECT_ID",
    "RIGHTS",
    "VOICE_HABITS",
    "bind",
    "card_payload",
    "frozen_bundle",
    "seed_reference",
    "seed_scene",
    "seed_synthetic_book",
    "tag_windows",
    "window_numbers",
]
