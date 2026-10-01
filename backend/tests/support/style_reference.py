"""风格参考测试共用的书 / 画像 / 绑定 / 作品骨架（以前散在各测试文件里、互相 import）。

更早就有的助手模块照旧：``tests/style_reference_factories.py``（书 / 画像 / 绑定 / 窗口的行级构造）、
``tests/style_reference_route_helpers.py``（经路由导入一本书并等分类完成）、``tests/style_reference_inject_helpers.py``。
"""

from __future__ import annotations

import random
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    FinalScene,
    LlmCall,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import voice_signature as vs
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.structure import compute_structure_card
from tests.style_reference_factories import make_book, make_profile
from tests.style_reference_route_helpers import import_book


# ---------------------------------------------------------------- 合成参考书：章题 + 对白 / 叙述比例随章变化的段落行（test_style_reference_windows）


_NAMES = ("老周", "小满", "阿禾", "陈叔")
_PLACES = ("院子", "渡口", "灶间", "巷口", "桥头")


_DIALOGUE = (
    "“{a}，你到底去不去？”{b}把灯芯拨小了些。",
    "“去。”{a}说，“等雨停了就走。”",
    "“你听见了吗？”",
    "{b}问：“船什么时候到？”",
    "“别问了，吃饭吧。”",
)


_NARRATION = (
    "{a}没有回答，先把袖口的水拧了拧，{p}里两只碗一只是干的。",
    "雨又密起来了，{p}那株桂树被打得低了头，叶子上的水一颗一颗落在石阶上。",
    "{a}在{p}站了很久，直到天色暗下去，才慢慢转身。",
    "他想起三年前的那个冬天，{b}也是这样站在{p}，一句话也不说。",
    "风从{p}吹过来，带着一点河水的腥气。",
)


def synthetic_rows(seed: str = "a", chapters: int = 8, per_chapter: int = 70) -> list[dict]:
    """合成书的段落行(章题 + 正文):对白 / 叙述比例随章变化,段长随机。"""
    rng = random.Random(seed)
    rows: list[dict] = []

    def add(text: str, ptype: str) -> None:
        rows.append({"paragraph_index": len(rows), "text": text, "paragraph_type": ptype})

    for chapter in range(1, chapters + 1):
        add(f"第{chapter}章 灯下", "transition")
        dialogue_bias = 0.25 + 0.5 * rng.random()
        for _ in range(per_chapter):
            a, b = rng.sample(_NAMES, 2)
            p = rng.choice(_PLACES)
            if rng.random() < dialogue_bias:
                text = rng.choice(_DIALOGUE).format(a=a, b=b, p=p)
                ptype = "dialogue"
            else:
                text = "".join(rng.choice(_NARRATION).format(a=a, b=b, p=p) for _ in range(rng.randint(1, 3)))
                ptype = "narration"
            add(text, ptype)
    return rows


def seed_book(session, book_id: str, rows: list[dict], *, stats: dict | None = None) -> str:
    make_book(session, book_id, title="合成书", paragraphs=rows, stats=stats)
    session.commit()
    return book_id


# ---------------------------------------------------------------- 经路由导入一本小样书、直接经服务落一条学习血缘（test_style_reference_routes）


SAMPLE_TXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

记得那年她还在的时候。

雪花从天空飘落。
""".encode("utf-8")


def import_sample_book(client: TestClient, fake: Any | None = None) -> str:
    """2026-09-15 严格 LLM:导入必须有 LLM——用假分类器顶替运行时客户端,并等后台分类完成。"""
    return import_book(client, text=SAMPLE_TXT, fake=fake)


def seed_full_chain(book_id: str) -> tuple[str, str, str]:
    """直接用 service 层快速建 run + finding(含 2 evidence)+ profile,绕过 LLM 调用。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        run_id = f"sr_run_route_{book_id[-6:]}"
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        extraction_id = f"sr_ext_route_{book_id[-6:]}"
        repo.create_extraction(
            extraction_id=extraction_id,
            book_id=book_id,
            run_id=run_id,
            layer="language",
            sub_dimension="language.rhetoric",
            raw_payload_json={},
            status="done",
            validation_errors_json=[],
            purpose="extract",
        )
        finding_id = f"sr_find_route_{book_id[-6:]}"
        repo.create_finding(
            finding_id=finding_id,
            book_id=book_id,
            run_id=run_id,
            extraction_id=extraction_id,
            sub_dimension="language.rhetoric",
            finding_kind="observation",
            statement="测试 observation 描述",
            confidence="high",
            status="pending",
        )
        # 2 evidence(≥2 强约束):1 条真实段落引文 + 1 条合成反例
        paragraphs = repo.list_paragraphs(book_id)
        repo.create_quote(
            quote_id=f"sr_quote_route_a_{book_id[-6:]}",
            book_id=book_id,
            paragraph_id=paragraphs[0].paragraph_id if paragraphs else None,
            span_start=0,
            span_end=10,
            quote_text="真实段落引文文本",
            illustrates_dims=["language.rhetoric"],
            extracted_features={},
        )
        repo.create_quote(
            quote_id=f"sr_quote_route_b_{book_id[-6:]}",
            book_id=book_id,
            paragraph_id=None,
            span_start=0,
            span_end=8,
            quote_text="合成反例文本",
            illustrates_dims=["language.rhetoric"],
            extracted_features={},
        )
        repo.create_evidence(
            evidence_id=f"sr_ev_route_a_{book_id[-6:]}",
            finding_id=finding_id,
            quote_id=f"sr_quote_route_a_{book_id[-6:]}",
            anchor_kind="paragraph_quote",
        )
        repo.create_evidence(
            evidence_id=f"sr_ev_route_b_{book_id[-6:]}",
            finding_id=finding_id,
            quote_id=f"sr_quote_route_b_{book_id[-6:]}",
            anchor_kind="counter_example",
        )
        profile_id = f"sr_profile_route_{book_id[-6:]}"
        repo.create_profile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="测试 profile",
            status="draft",
            profile_json={
                "narrative_summary": "ns",
                "scene_samples_index": {},
                "calibration_guidance": ["calib A"],
            },
            coverage_json={},
            source_finding_ids_json=[finding_id],
        )
        session.commit()
    return run_id, finding_id, profile_id


# ---------------------------------------------------------------- 带结构卡的画像（test_style_reference_structure）


def structure_rows(*, markers: bool = True) -> list[dict]:
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


def voice_shares(first: float, second: float, third: float) -> dict:
    return {
        "version": "voice_signature_v1",
        "features": {
            "person_first_share": first,
            "person_second_share": second,
            "person_third_share": third,
        },
        "habits": [],
    }


def profile_json_with_structure(**overrides) -> dict:
    payload = {
        "style_features": ["短句克制"],
        "structure_card": compute_structure_card(structure_rows(), voice_signature=voice_shares(0.7, 0.1, 0.2)),
        "planning_guidance": ["对白：对白短促，常以一句反问收束", "情绪基调：冷而不哀"],
    }
    payload.update(overrides)
    return payload


def seed_style_binding(
    session,
    *,
    project_id: str,
    profile_json: dict,
    seed: str = "st",
    scope: str = "project",
    config_json: dict | None = None,
    send_rights: bool = True,
) -> str:
    """一本带权利声明的书 + 一次学习 + 一份活跃画像 + 一条活跃绑定（旧策略列 A），返回画像 id。"""
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
            scope=scope,
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="A",
            config_json=dict(config_json or {}),
            status="active",
        )
    )
    session.commit()
    return f"sr_profile_{seed}"


# ---------------------------------------------------------------- 一部按章 / 场排好的作品、参考画像的声音特征（test_style_reference_style_continuity）


def reference_features(**overrides: float) -> dict[str, float]:
    """以「一般中文小说」基线均值为底、只改写几个节奏特征的参考画像声音签名。"""
    baseline = vs.load_voice_baseline()
    features = {name: float(baseline["features"][name]["mean"]) for name in vs.FEATURE_NAMES}
    features.update(overrides)
    return features


def short_dense_reference() -> dict[str, float]:
    """短句、密集停顿的参考：句均十字、逗号极密。"""
    return reference_features(
        sent_len_mean=10.0,
        sent_len_p90=18.0,
        punct_comma_per_1k=120.0,
        sent_pauses_mean=3.5,
        clause_len_mean=5.0,
    )


def seed_work(session, *, project_id: str, chapters: int = 1, scenes_per_chapter: int = 3) -> None:
    session.add(StoryProject(project_id=project_id, title="v2 continuity", outline_text="", planning_mode="snowflake"))
    for chapter_index in range(1, chapters + 1):
        chapter_id = f"{project_id}_CH{chapter_index:02d}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                planned_scene_count=scenes_per_chapter,
                chapter_goal=f"Chapter {chapter_index} goal.",
                display_order=chapter_index,
            )
        )
        for seq in range(1, scenes_per_chapter + 1):
            scene_id = f"{chapter_id}_SC{seq:02d}"
            session.add(
                SceneCard(
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    project_id=project_id,
                    scene_seq=seq,
                    onstage_chars_json=[],
                    scene_goal=f"Scene {seq} goal.",
                    is_chapter_last=1 if seq == scenes_per_chapter else 0,
                )
            )
            session.add(SceneRunState(scene_id=scene_id))
    session.commit()


def add_final_scene(session, *, scene_id: str, content: str, status: str = "archived") -> str:
    chapter_id = scene_id.rsplit("_SC", 1)[0]
    row_id = f"final_{scene_id}"
    session.add(
        FinalScene(
            row_id=row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            content=content,
            status=status,
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash="h",
        )
    )
    session.commit()
    return row_id


# ---------------------------------------------------------------- 本场参考窗口：一本按序号编段的书、一场带中性稿的场景、一条尝试记录（test_style_windows_surface）


def seed_numbered_book(session: Session, book_id: str, *, paragraphs: int = 100) -> None:
    make_book(
        session,
        book_id,
        title=f"参考书 {book_id}",
        cloud_policy="local_only",
        text_checksum=f"sha_{book_id}",
        total_chars=paragraphs * 10,
        paragraph_id="{book_id}_p{index:04d}",
        paragraphs=[
            {"text": f"{book_id} 第 {index} 段的原文。", "paragraph_type": "dialogue" if index % 3 == 0 else "narration"}
            for index in range(paragraphs)
        ],
    )


def seed_surface_profile(session: Session, *, book_id: str, profile_id: str) -> None:
    make_profile(session, book_id, profile_id=profile_id, title=f"画像 {profile_id}")


def seed_windows_scene(session: Session, *, project_id: str, scene_id: str = "CH701_SC01") -> SceneRunState:
    chapter_id = "CH701"
    session.add(StoryProject(project_id=project_id, title="WP4 windows", outline_text=""))
    session.add(
        ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="g")
    )
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A"],
            location="旧城门廊",
            scene_goal="reveal",
            beats_json=["arrival"],
            must_include_text=None,
            target_length_band="short",
            scene_type="reveal",
            is_chapter_last=0,
        )
    )
    # 生成摘要要能解析出 llm_call:中性稿行指向一条 LlmCall
    session.add(
        LlmCall(
            llm_call_id=f"llm_{scene_id}",
            step="neutral_draft",
            scope_type="scene",
            scope_id=scene_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
        )
    )
    session.add(
        SceneDraft(
            row_id=f"draft_{scene_id}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            stage="neutral_draft",
            content="x",
            source_bundle_id="bundle_v2",
            source_bundle_hash="h_v2",
            generation_llm_call_id=f"llm_{scene_id}",
        )
    )
    state = SceneRunState(
        scene_id=scene_id,
        scene_status="near_final",
        current_bundle_id="bundle_v2",
        current_neutral_draft_row_id=f"draft_{scene_id}",
    )
    session.add(state)
    session.commit()
    return state


def windows_attempt(
    scene_id: str,
    *,
    step: str,
    bundle_id: str,
    refs: list | None,
    status: str = "completed",
    profile_ids: list[str] | None = None,
) -> AttemptTracker:
    runtime: dict = {"outcome": "hit", "profile_ids": profile_ids or []}
    if refs is not None:
        runtime["few_shot_window_refs"] = refs
    return AttemptTracker(
        scene_id=scene_id,
        chapter_id="CH701",
        step=step,
        status=status,
        source_bundle_id=bundle_id,
        details_json={"style_reference_runtime": runtime},
    )
