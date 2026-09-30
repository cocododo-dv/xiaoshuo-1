"""bundle 冻结快照的逐字安全网（B03-19：拆 bundle_builder 之前先钉住）。

种一部固定 id、固定时刻的小作品，覆盖 bundle 的大部分段落——章目标与场景卡、结构简报、章 / 场作者简报、
蓝图与场面标签、人物压力、章架构、角色契约、章间过渡、前文声音锚、上一场记忆、新鲜度预算、场 / 章摘要、
卷摘要——再加一场绑了参考书（风格直起：参考尺度、运行时契约、前文不作声音），逐场比对：

- ``bundle_snapshot_hash``（BSHASH_v1 投影）；
- 注入段的顺序、``source_version_refs`` / ``inline_digests`` 的键序；
- 快照按插入顺序序列化后的 sha256（存库的字节随键序变）。

相似场景、声线卡 / 关系卡与作者偏好这几段已按批准 #1 / #15 / #6（重评 R1 / R8 / R5）从 bundle 删掉，删的时候
golden 重生成过一次；夹具也不再种声线卡、关系卡与偏好——这张网不管它们，bundle 里也不会再有这几段。

快照有意变化时（改了某一段的内容或顺序），用 ``BUNDLE_GOLDEN_REGEN=1`` 重生成 golden，并在提交里说明原因。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    FinalScene,
    GenerationPlanningArtifact,
    SceneBlueprint,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    StoryCharacter,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
    VolumeSummary,
)
from novel_system.services.bundle_builder import BundleBuilder

GOLDEN_PATH = Path(__file__).parent / "golden" / "bundle" / "bundle_snapshots.json"
PROJECT_ID = "P_GOLD"
T0 = "2026-09-01T08:00:00+00:00"


def _at(minutes: int) -> str:
    return f"2026-09-01T08:{minutes:02d}:00+00:00"


def _scene(chapter_id: str, seq: int, *, pov: str = "林昭", onstage: tuple[str, ...] = ("林昭", "许望"), **fields):
    scene_id = f"{chapter_id}_SC{seq:02d}"
    return SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=PROJECT_ID,
        scene_seq=seq,
        pov_character_id=pov,
        onstage_chars_json=list(onstage),
        location="雨城旧档案馆",
        scene_goal=f"林昭在第 {seq} 场逼许望交出旧信。",
        beats_json=["开柜", "对质", "旧信落地"],
        must_include_text="林昭把旧信塞回案卷。",
        exit_change="许望第一次承认他见过那封信。",
        hook="门外有人敲了三下。",
        target_length_band="medium",
        writer_brief_json={
            "character_desire": "林昭想当场查清旧信的来历。",
            "obstacle": "许望守着案卷不肯松口。",
            "choice_under_pressure": "拆穿许望，还是先保住证人。",
            "scene_form": "proactive",
            "scene_crucible": "旧信只有一封，两人都想要。",
            "goal": "拿到旧信。",
            "conflict": "许望步步设防。",
            "setback": "旧信被雨水泡烂了一半。",
        },
        created_at=T0,
        **fields,
    )


def _seed_book(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="旧信", outline_text="雨城", genre="悬疑", created_at=T0))
    for index, name in enumerate(("林昭", "许望"), start=1):
        session.add(
            StoryCharacter(
                character_id=name,
                project_id=PROJECT_ID,
                display_name=f"{name}（角色{index}）",
                role="主角" if index == 1 else "配角",
                created_at=T0,
                updated_at=T0,
            )
        )
    for index, chapter_id in enumerate(("P_GOLD_CH01", "P_GOLD_CH02"), start=1):
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=PROJECT_ID,
                chapter_goal=f"第 {index} 章：旧信的下落。",
                display_order=index,
                planned_scene_count=2,
                writer_brief_json={
                    "core_promise": "真相与保护不能同时兑现。",
                    "chapter_question": "谁在暗处盯着案卷？",
                },
                created_at=T0,
            )
        )
    scenes = [
        _scene("P_GOLD_CH01", 1),
        _scene("P_GOLD_CH01", 2),
        _scene("P_GOLD_CH02", 1),
        _scene("P_GOLD_CH02", 2, onstage=("林昭",)),
    ]
    session.add_all(scenes)
    for scene in scenes:
        session.add(SceneRunState(scene_id=scene.scene_id, scene_status="ready"))
    session.flush()

    # 已写成的三场：成稿、记忆与风格稿——下一场的上一场记忆、新鲜度预算、前文声音锚、章间过渡
    for seq, (chapter_id, scene_seq, text) in enumerate(
        (
            ("P_GOLD_CH01", 1, "林昭拉开柜门，旧信躺在案卷最底下。许望没有看她，只把雨伞放回门边。她把旧信塞回案卷。"),
            ("P_GOLD_CH01", 2, "许望终于承认，那封旧信他见过。林昭没说话，把案卷推回他面前，转身走进雨里。"),
            ("P_GOLD_CH02", 1, "雨城起雾了。林昭在档案馆门口等了一夜，旧信泡烂了一半，她还是把它交给了证人。"),
        ),
        start=1,
    ):
        scene_id = f"{chapter_id}_SC{scene_seq:02d}"
        session.add(
            FinalScene(
                row_id=f"final_gold_{seq}",
                scene_id=scene_id,
                chapter_id=chapter_id,
                content=text,
                status="archived",
                source_bundle_id="bundle_gold",
                source_bundle_hash="hash_gold",
                created_at=_at(seq),
            )
        )
        session.get(SceneRunState, scene_id).current_final_scene_row_id = f"final_gold_{seq}"
        session.add(
            SceneMemory(
                row_id=f"scene_memory_gold_{seq}",
                scene_id=scene_id,
                chapter_id=chapter_id,
                content=text,
                source_bundle_id="bundle_gold",
                final_scene_row_id=f"final_gold_{seq}",
                source_review_id=f"review_gold_{seq}",
                created_at=_at(seq),
            )
        )
        session.add(
            SceneDraft(
                row_id=f"style_draft_gold_{seq}",
                scene_id=scene_id,
                chapter_id=chapter_id,
                stage="style_draft",
                content=text,
                source_bundle_id="bundle_gold",
                source_bundle_hash="hash_gold",
                created_at=_at(seq),
            )
        )
    session.add(
        ChapterMemory(
            row_id="chapter_memory_gold_ch02",
            chapter_id="P_GOLD_CH02",
            aggregate_stage="final",
            content="第二章的摘要：旧信被泡烂了一半。",
            source_review_id="review_gold_ch02",
            active_flag=1,
            runtime_eligible=1,
            created_at=_at(3),
        )
    )
    # 第 2 场的蓝图（带场面标签）与两份规划产物
    session.add(
        SceneBlueprint(
            row_id="blueprint_gold_ch01_sc02",
            scene_id="P_GOLD_CH01_SC02",
            chapter_id="P_GOLD_CH01",
            blueprint_json={"scene_turn": "许望松口。", "situation_tags": ["confrontation", "revelation"]},
            status="accepted",
            created_at=_at(4),
        )
    )
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_gold_pressure",
            artifact_type="character_pressure_blueprint",
            object_type="scene",
            object_id="P_GOLD_CH01_SC02",
            chapter_id="P_GOLD_CH01",
            scene_id="P_GOLD_CH01_SC02",
            payload_json={"surface_goal": "拿到旧信。", "hidden_fear": "被许望看穿。"},
            status="active",
            created_at=_at(5),
            updated_at=_at(5),
        )
    )
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_gold_architecture",
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id="P_GOLD_CH01",
            chapter_id="P_GOLD_CH01",
            payload_json={"chapter_promise": "旧信的下落。", "escalation_path": ["开柜", "对质"]},
            status="active",
            created_at=_at(5),
            updated_at=_at(5),
        )
    )
    session.add(
        VolumeSummary(
            row_id="volume_summary_gold_v1",
            project_id=PROJECT_ID,
            volume_seq=1,
            chapter_id_start="P_GOLD_CH01",
            chapter_id_end="P_GOLD_CH01",
            chapter_count=1,
            atmosphere_summary="雨一直没停。",
            created_at=_at(7),
            updated_at=_at(7),
        )
    )
    # 第二章第 2 场单独绑一本参考书（场景作用域，缺省风格直起）
    session.add(
        StyleReferenceBook(
            book_id="sr_book_gold",
            title="公版参考",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum="checksum-gold",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
            created_at=T0,
            updated_at=T0,
        )
    )
    session.add(StyleReferenceRun(run_id="sr_run_gold", book_id="sr_book_gold", status="done", created_at=T0))
    session.add(
        StyleReferenceProfile(
            profile_id="sr_profile_gold",
            book_id="sr_book_gold",
            run_id="sr_run_gold",
            title="参考画像",
            status="active",
            profile_json={
                "voice_signature": {"features": {"sent_len_mean": 12.0}, "deliberate_repetition": False},
                "structure_card": {
                    "chapter_count": 12,
                    "chapter_chars": {"median": 9000},
                    "scene_break_style": "implicit",
                },
            },
            created_at=T0,
            updated_at=T0,
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id="sr_bind_gold",
            profile_id="sr_profile_gold",
            scope="scene",
            scope_ref_id="P_GOLD_CH02_SC02",
            task_type="scene_generation",
            strategy="mixed",
            config_json={},
            status="active",
            created_at=T0,
            updated_at=T0,
        )
    )
    session.commit()


def _fingerprint(snapshot: dict) -> dict:
    return {
        "bundle_snapshot_hash": None,
        "slots": [item["slot"] for item in snapshot["ordered_injections"]],
        "source_version_ref_keys": list(snapshot["source_version_refs"]),
        "inline_digest_keys": list(snapshot["inline_digests"]),
        "degraded_slots": list(snapshot.get("degraded_slots") or []),
        "snapshot_sha256": hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def test_frozen_bundles_match_the_golden_snapshots(session) -> None:
    _seed_book(session)
    actual: dict[str, dict] = {}
    for scene_id in ("P_GOLD_CH01_SC01", "P_GOLD_CH01_SC02", "P_GOLD_CH02_SC01", "P_GOLD_CH02_SC02"):
        built = BundleBuilder(session).build(scene_id)
        entry = _fingerprint(built["snapshot"])
        entry["bundle_snapshot_hash"] = built["bundle_snapshot_hash"]
        actual[scene_id] = entry
        session.commit()

    if os.environ.get("BUNDLE_GOLDEN_REGEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert actual == expected
