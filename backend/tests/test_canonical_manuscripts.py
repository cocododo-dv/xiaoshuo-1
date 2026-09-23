from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    NarrativeEvent,
    OperationLog,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryProject,
)
from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.canonical_manuscripts import CanonicalSceneService
from novel_system.services.errors import DomainError


def _seed_scene(
    session,
    key: str,
    *,
    draft_content: str = "<p>作者改过的正文。</p>",
    approved: bool = False,
) -> dict[str, str]:
    project_id = f"CANON_{key}"
    chapter_id = f"{project_id}_CH01"
    scene_id = f"{chapter_id}_SC01"
    old_final_id = f"final_scene_{scene_id}_v1"
    draft_id = f"author_draft_scene_{scene_id}"
    session.add(
        StoryProject(
            project_id=project_id,
            title=f"Canonical {key}",
            outline_text="canonical promotion test",
            current_chapter_id=chapter_id,
            approved_chapter_ids_json=[chapter_id] if approved else [],
        )
    )
    session.flush()
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            planned_scene_count=1,
            display_order=1,
            chapter_goal="作者正文必须成为唯一真相",
        )
    )
    session.flush()
    session.add(ChapterState(chapter_id=chapter_id, aggregate_block_reason="none"))
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            scene_goal="完成一次安全提升",
            is_chapter_last=1,
        )
    )
    session.flush()
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_status="archived",
            current_final_scene_row_id=old_final_id,
            narrative_sync_status="synced",
            narrative_sync_final_scene_row_id=old_final_id,
        )
    )
    session.add(
        FinalScene(
            row_id=old_final_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            content="旧权威正文。",
            content_hash=hashlib.sha256("旧权威正文。".encode("utf-8")).hexdigest(),
            status="archived",
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash=f"bundle_hash_{scene_id}",
            source_kind="generation",
        )
    )
    session.add(
        AuthorDraft(
            draft_id=draft_id,
            object_type="scene",
            object_id=scene_id,
            source_text_ref=f"final_scene:{old_final_id}",
            content=draft_content,
            revision_no=2,
            status="current",
        )
    )
    session.flush()
    Archiver(session).archive_final_scene(scene_id, old_final_id)
    CanonContinuityService(session).verify_scene_complete(
        project_id,
        scene_id,
        actor_ref="test",
        note="fixture verifies the previous final before carry-forward",
        expected_final_scene_row_id=old_final_id,
    )
    aggregate = Aggregator(session).run_final_aggregate(chapter_id)
    assert aggregate and aggregate["status"] == "created"
    session.commit()
    return {
        "project_id": project_id,
        "chapter_id": chapter_id,
        "scene_id": scene_id,
        "old_final_id": old_final_id,
        "draft_id": draft_id,
    }


def _promote(client, seeded: dict[str, str], *, key: str, **overrides):
    body = {
        "base_revision_no": 2,
        "expected_current_final_scene_row_id": seeded["old_final_id"],
        "narrative_effect": "facts_unchanged",
        "accepted_warning_codes": [],
        **overrides,
    }
    return client.post(
        f"/api/v1/author-drafts/{seeded['draft_id']}/promote-canonical",
        json=body,
        headers={"X-Idempotency-Key": f"canonical-promote-{key}"},
    )


def test_content_safety_acknowledgement_is_rechecked_and_audited_at_archive(
    client,
    session,
    monkeypatch,
) -> None:
    seeded = _seed_scene(
        session,
        "CONTENT_SAFETY",
        draft_content="<p>角色只有16岁，段落明确描写两人的性行为。</p>",
    )
    # 风格参考 v3：没有绑定、没有全局受保护词时抄袭门本来就放行，不必再打桩

    blocked = _promote(client, seeded, key="content-safety-blocked")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CONTENT_SAFETY_REVIEW_REQUIRED"

    accepted = _promote(
        client,
        seeded,
        key="content-safety-accepted",
        accepted_warning_codes=["sexual_content_with_minor_indicators"],
    )
    assert accepted.status_code == 200
    final_id = accepted.json()["data"]["final_scene_row_id"]
    attempt = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.scene_id == seeded["scene_id"],
            AttemptTracker.step == "archive",
        )
    ).scalars().all()[-1]
    gate = attempt.details_json["final_text_gate"]
    assert gate["content_hash"] == accepted.json()["data"]["content_hash"]
    assert gate["content_safety"]["acknowledged_codes"] == [
        "sexual_content_with_minor_indicators"
    ]
    assert final_id == attempt.details_json["final_scene_row_id"]


def test_promote_scene_author_draft_rebuilds_canonical_chain_atomically_and_replays(client, session) -> None:
    seeded = _seed_scene(
        session,
        "HAPPY",
        draft_content="<p>她走进门。</p><script>alert('x')</script><p>灯灭了 &amp; 风停了。</p>",
    )
    first_event_id = f"evt_{seeded['scene_id']}_cause"
    second_event_id = f"evt_{seeded['scene_id']}_effect"
    session.add_all(
        [
            NarrativeEvent(
                event_id=first_event_id,
                project_id=seeded["project_id"],
                chapter_id=seeded["chapter_id"],
                scene_id=seeded["scene_id"],
                scene_seq=1,
                event_type="state_change",
                entity_type="object",
                entity_id="audit_marker",
                fact_key="canonical_test_marker",
                fact_value="cause_preserved",
            ),
            NarrativeEvent(
                event_id=second_event_id,
                project_id=seeded["project_id"],
                chapter_id=seeded["chapter_id"],
                scene_id=seeded["scene_id"],
                scene_seq=1,
                event_type="state_change",
                entity_type="object",
                entity_id="audit_marker",
                fact_key="canonical_test_effect",
                fact_value="effect_preserved",
                causal_predecessor_id=first_event_id,
            ),
        ]
    )
    session.commit()
    event_snapshot_before = [
        (
            row.event_id,
            row.project_id,
            row.chapter_id,
            row.scene_id,
            row.fact_key,
            row.fact_value,
            row.causal_predecessor_id,
        )
        for row in session.query(NarrativeEvent)
        .filter(NarrativeEvent.event_id.in_([first_event_id, second_event_id]))
        .order_by(NarrativeEvent.event_id.asc())
        .all()
    ]
    assert len(event_snapshot_before) == 2

    first = _promote(client, seeded, key="happy")
    replay = _promote(client, seeded, key="happy")

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-Idempotency-Status"] == "replayed"
    data = first.json()["data"]
    new_final_id = data["final_scene_row_id"]
    assert new_final_id != seeded["old_final_id"]
    assert data["narrative_sync_status"] == "synced"
    assert data["canonical_dirty"] is False

    session.expire_all()
    old_final = session.get(FinalScene, seeded["old_final_id"])
    new_final = session.get(FinalScene, new_final_id)
    state = session.get(SceneRunState, seeded["scene_id"])
    draft = session.get(AuthorDraft, seeded["draft_id"])
    assert old_final is not None and old_final.content == "旧权威正文。"
    assert old_final.status == "superseded"
    assert old_final.superseded_by_final_scene_row_id == new_final_id
    assert new_final is not None
    assert new_final.content == "她走进门。\n灯灭了 & 风停了。"
    assert "alert" not in new_final.content
    assert new_final.content_hash == hashlib.sha256(new_final.content.encode("utf-8")).hexdigest()
    assert new_final.source_kind == "author_draft"
    assert new_final.source_author_draft_id == seeded["draft_id"]
    assert new_final.source_author_draft_revision_no == 2
    assert new_final.parent_final_scene_row_id == seeded["old_final_id"]
    assert state is not None and state.current_final_scene_row_id == new_final_id
    assert state.narrative_sync_status == "synced"
    assert state.narrative_sync_final_scene_row_id == new_final_id
    assert draft is not None and draft.last_promoted_revision_no == 2
    assert draft.last_promoted_final_scene_row_id == new_final_id

    active_scene_memories = session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"], active_flag=1).all()
    assert len(active_scene_memories) == 1
    assert active_scene_memories[0].content == new_final.content
    active_chapter_memories = session.query(ChapterMemory).filter_by(
        chapter_id=seeded["chapter_id"], aggregate_stage="final", active_flag=1
    ).all()
    assert len(active_chapter_memories) == 1
    assert active_chapter_memories[0].content == new_final.content
    logs = session.query(OperationLog).filter_by(
        event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
    ).all()
    assert len(logs) == 1
    assert logs[0].payload_json["narrative_events_preserved"] is True
    event_snapshot_after = [
        (
            row.event_id,
            row.project_id,
            row.chapter_id,
            row.scene_id,
            row.fact_key,
            row.fact_value,
            row.causal_predecessor_id,
        )
        for row in session.query(NarrativeEvent)
        .filter(NarrativeEvent.event_id.in_([first_event_id, second_event_id]))
        .order_by(NarrativeEvent.event_id.asc())
        .all()
    ]
    assert event_snapshot_after == event_snapshot_before


def test_same_text_first_promotion_records_author_provenance_then_new_key_is_noop(client, session) -> None:
    seeded = _seed_scene(session, "SAME_TEXT", draft_content="<p>旧权威正文。</p>")

    first = _promote(client, seeded, key="same-text-first")
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    author_final_id = first_data["final_scene_row_id"]
    assert author_final_id != seeded["old_final_id"]
    assert first_data["already_current"] is False

    session.expire_all()
    before_replay = {
        "final_total": session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memory_total": session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memory_active": session.query(SceneMemory).filter_by(
            scene_id=seeded["scene_id"], active_flag=1
        ).count(),
        "chapter_memory_total": session.query(ChapterMemory).filter_by(
            chapter_id=seeded["chapter_id"], aggregate_stage="final"
        ).count(),
        "chapter_memory_active": session.query(ChapterMemory).filter_by(
            chapter_id=seeded["chapter_id"], aggregate_stage="final", active_flag=1
        ).count(),
        "promotion_log_total": session.query(OperationLog).filter_by(
            event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
        ).count(),
    }

    second = _promote(
        client,
        seeded,
        key="same-text-second-key",
        expected_current_final_scene_row_id=author_final_id,
    )
    assert second.status_code == 200, second.text
    second_data = second.json()["data"]
    assert second_data["already_current"] is True
    assert second_data["derivation_reused"] is True
    assert second_data["final_scene_row_id"] == author_final_id

    session.expire_all()
    author_final = session.get(FinalScene, author_final_id)
    assert author_final is not None
    assert author_final.source_kind == "author_draft"
    assert author_final.source_author_draft_id == seeded["draft_id"]
    assert author_final.source_author_draft_revision_no == 2
    after_replay = {
        "final_total": session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memory_total": session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memory_active": session.query(SceneMemory).filter_by(
            scene_id=seeded["scene_id"], active_flag=1
        ).count(),
        "chapter_memory_total": session.query(ChapterMemory).filter_by(
            chapter_id=seeded["chapter_id"], aggregate_stage="final"
        ).count(),
        "chapter_memory_active": session.query(ChapterMemory).filter_by(
            chapter_id=seeded["chapter_id"], aggregate_stage="final", active_flag=1
        ).count(),
        "promotion_log_total": session.query(OperationLog).filter_by(
            event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
        ).count(),
    }
    assert before_replay == {
        "final_total": 2,
        "scene_memory_total": 2,
        "scene_memory_active": 1,
        "chapter_memory_total": 2,
        "chapter_memory_active": 1,
        "promotion_log_total": 1,
    }
    assert after_replay == before_replay


def test_same_revision_repairs_missing_chapter_derivation_instead_of_false_noop(client, session) -> None:
    seeded = _seed_scene(session, "DERIVATION_REPAIR")
    first = _promote(client, seeded, key="derivation-repair-first")
    assert first.status_code == 200, first.text
    final_id = first.json()["data"]["final_scene_row_id"]

    session.expire_all()
    active_chapter_memory = session.query(ChapterMemory).filter_by(
        chapter_id=seeded["chapter_id"], aggregate_stage="final", active_flag=1
    ).one()
    active_chapter_memory.active_flag = 0
    active_chapter_memory.runtime_eligible = 0
    active_chapter_memory.runtime_eligibility_basis = "test_missing_derivation"
    session.commit()
    before_chapter_total = session.query(ChapterMemory).filter_by(
        chapter_id=seeded["chapter_id"], aggregate_stage="final"
    ).count()

    repaired = _promote(
        client,
        seeded,
        key="derivation-repair-second",
        expected_current_final_scene_row_id=final_id,
    )

    assert repaired.status_code == 200, repaired.text
    repaired_data = repaired.json()["data"]
    assert repaired_data["already_current"] is True
    assert repaired_data.get("derivation_reused") is not True
    assert repaired_data["final_scene_row_id"] == final_id
    session.expire_all()
    assert session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count() == 2
    assert session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count() == 2
    assert session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"], active_flag=1).count() == 1
    assert session.query(ChapterMemory).filter_by(
        chapter_id=seeded["chapter_id"], aggregate_stage="final"
    ).count() == before_chapter_total + 1
    assert session.query(ChapterMemory).filter_by(
        chapter_id=seeded["chapter_id"], aggregate_stage="final", active_flag=1
    ).count() == 1
    assert session.query(OperationLog).filter_by(
        event_type="author_draft_promoted_canonical", object_ref=seeded["scene_id"]
    ).count() == 2


def test_missing_or_corrupt_persisted_final_hash_never_uses_true_noop(client, session) -> None:
    missing = _seed_scene(session, "MISSING_FINAL_HASH")
    first = _promote(client, missing, key="missing-hash-first")
    assert first.status_code == 200, first.text
    final_id = first.json()["data"]["final_scene_row_id"]
    session.expire_all()
    final = session.get(FinalScene, final_id)
    assert final is not None
    expected_hash = final.content_hash
    final.content_hash = None
    session.commit()
    before_final_count = session.query(FinalScene).filter_by(scene_id=missing["scene_id"]).count()

    repaired = _promote(
        client,
        missing,
        key="missing-hash-repair",
        expected_current_final_scene_row_id=final_id,
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["data"]["already_current"] is True
    assert repaired.json()["data"].get("derivation_reused") is not True
    session.expire_all()
    assert session.get(FinalScene, final_id).content_hash == expected_hash
    assert session.query(FinalScene).filter_by(scene_id=missing["scene_id"]).count() == before_final_count

    corrupt = _seed_scene(session, "CORRUPT_FINAL_HASH")
    corrupt_first = _promote(client, corrupt, key="corrupt-hash-first")
    assert corrupt_first.status_code == 200, corrupt_first.text
    corrupt_final_id = corrupt_first.json()["data"]["final_scene_row_id"]
    session.expire_all()
    corrupt_final = session.get(FinalScene, corrupt_final_id)
    assert corrupt_final is not None
    corrupt_final.content_hash = "corrupt-persisted-hash"
    session.commit()
    before = {
        "final_total": session.query(FinalScene).filter_by(scene_id=corrupt["scene_id"]).count(),
        "scene_memory_total": session.query(SceneMemory).filter_by(scene_id=corrupt["scene_id"]).count(),
        "chapter_memory_total": session.query(ChapterMemory).filter_by(chapter_id=corrupt["chapter_id"]).count(),
        "promotion_log_total": session.query(OperationLog).filter_by(
            event_type="author_draft_promoted_canonical", object_ref=corrupt["scene_id"]
        ).count(),
    }

    blocked = _promote(
        client,
        corrupt,
        key="corrupt-hash-blocked",
        expected_current_final_scene_row_id=corrupt_final_id,
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "FINAL_SCENE_CONTENT_HASH_MISMATCH"
    session.expire_all()
    after = {
        "final_total": session.query(FinalScene).filter_by(scene_id=corrupt["scene_id"]).count(),
        "scene_memory_total": session.query(SceneMemory).filter_by(scene_id=corrupt["scene_id"]).count(),
        "chapter_memory_total": session.query(ChapterMemory).filter_by(chapter_id=corrupt["chapter_id"]).count(),
        "promotion_log_total": session.query(OperationLog).filter_by(
            event_type="author_draft_promoted_canonical", object_ref=corrupt["scene_id"]
        ).count(),
    }
    assert after == before
    assert session.get(FinalScene, corrupt_final_id).content_hash == "corrupt-persisted-hash"


def test_promote_requires_reconcile_publishes_text_but_keeps_canon_pending(client, session) -> None:
    seeded = _seed_scene(session, "RECONCILE")
    cause_id = f"evt_{seeded['scene_id']}_cause"
    effect_id = f"evt_{seeded['scene_id']}_effect"
    session.add_all(
        [
            NarrativeEvent(
                event_id=cause_id,
                project_id=seeded["project_id"],
                chapter_id=seeded["chapter_id"],
                scene_id=seeded["scene_id"],
                scene_seq=1,
                event_type="state_change",
                entity_type="object",
                entity_id="reconcile_marker",
                fact_key="cause",
                fact_value="unchanged",
            ),
            NarrativeEvent(
                event_id=effect_id,
                project_id=seeded["project_id"],
                chapter_id=seeded["chapter_id"],
                scene_id=seeded["scene_id"],
                scene_seq=1,
                event_type="state_change",
                entity_type="object",
                entity_id="reconcile_marker",
                fact_key="effect",
                fact_value="unchanged",
                causal_predecessor_id=cause_id,
            ),
        ]
    )
    session.commit()

    def snapshot() -> dict[str, list[tuple]]:
        return {
            "events": [
                (
                    row.event_id,
                    row.project_id,
                    row.chapter_id,
                    row.scene_id,
                    row.fact_key,
                    row.fact_value,
                    row.causal_predecessor_id,
                )
                for row in session.query(NarrativeEvent)
                .filter(NarrativeEvent.event_id.in_([cause_id, effect_id]))
                .order_by(NarrativeEvent.event_id.asc())
                .all()
            ],
            "finals": [
                (row.row_id, row.content, row.status, row.superseded_by_final_scene_row_id)
                for row in session.query(FinalScene)
                .filter_by(scene_id=seeded["scene_id"])
                .order_by(FinalScene.row_id.asc())
                .all()
            ],
            "scene_memories": [
                (row.row_id, row.content, row.final_scene_row_id, row.active_flag)
                for row in session.query(SceneMemory)
                .filter_by(scene_id=seeded["scene_id"])
                .order_by(SceneMemory.row_id.asc())
                .all()
            ],
            "chapter_memories": [
                (row.row_id, row.content, row.active_flag)
                for row in session.query(ChapterMemory)
                .filter_by(chapter_id=seeded["chapter_id"])
                .order_by(ChapterMemory.row_id.asc())
                .all()
            ],
        }

    before = snapshot()
    assert len(before["events"]) == 2

    response = _promote(
        client,
        seeded,
        key="reconcile",
        narrative_effect="requires_reconcile",
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["narrative_sync_status"] == "pending_extraction"
    assert data["canon_continuity"]["complete"] is False
    assert data["canon_continuity"]["status"] == "pending_extraction"
    session.expire_all()
    after = snapshot()
    assert after["events"] == before["events"]
    assert session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count() == 2
    state = session.get(SceneRunState, seeded["scene_id"])
    assert state is not None and state.current_final_scene_row_id == data["final_scene_row_id"]
    assert state.narrative_sync_status == "pending_extraction"
    assert session.get(AuthorDraft, seeded["draft_id"]).last_promoted_revision_no == 2


def test_promote_rejects_stale_draft_and_stale_canonical_pointer(client, session) -> None:
    stale_draft = _seed_scene(session, "STALE_DRAFT")
    draft_response = _promote(client, stale_draft, key="stale-draft", base_revision_no=1)
    assert draft_response.status_code == 409
    assert draft_response.json()["error"]["code"] == "AUTHOR_DRAFT_CONFLICT"

    stale_final = _seed_scene(session, "STALE_FINAL")
    final_response = _promote(
        client,
        stale_final,
        key="stale-final",
        expected_current_final_scene_row_id="some_other_final",
    )
    assert final_response.status_code == 409
    assert final_response.json()["error"]["code"] == "CANONICAL_BASE_CONFLICT"


def test_final_text_gate_blocks_before_any_canonical_write_even_without_caller_rollback(session) -> None:
    seeded = _seed_scene(session, "PREFLIGHT_GATE", draft_content="<p>正文里出现禁止内容。</p>")
    scene = session.get(SceneCard, seeded["scene_id"])
    assert scene is not None
    scene.forbidden_text = "禁止内容"
    session.commit()
    before = {
        "final_total": session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count(),
        "scene_memory_total": session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count(),
        "chapter_memory_total": session.query(ChapterMemory).filter_by(chapter_id=seeded["chapter_id"]).count(),
    }

    with pytest.raises(DomainError) as caught:
        CanonicalSceneService(session).promote_author_draft(
            seeded["draft_id"],
            {
                "base_revision_no": 2,
                "expected_current_final_scene_row_id": seeded["old_final_id"],
                "narrative_effect": "facts_unchanged",
                "accepted_warning_codes": [],
            },
        )
    assert caught.value.code == "FINAL_TEXT_CONTINUITY_BLOCKED"

    # Deliberately do not roll back. A direct service caller may catch the domain
    # error and continue using the session; preflight must have left it clean.
    session.flush()
    session.expire_all()
    assert session.query(FinalScene).filter_by(scene_id=seeded["scene_id"]).count() == before["final_total"]
    assert session.query(SceneMemory).filter_by(scene_id=seeded["scene_id"]).count() == before["scene_memory_total"]
    assert session.query(ChapterMemory).filter_by(chapter_id=seeded["chapter_id"]).count() == before["chapter_memory_total"]
    old_final = session.get(FinalScene, seeded["old_final_id"])
    state = session.get(SceneRunState, seeded["scene_id"])
    draft = session.get(AuthorDraft, seeded["draft_id"])
    assert old_final is not None and old_final.status == "archived"
    assert old_final.superseded_by_final_scene_row_id is None
    assert state is not None and state.current_final_scene_row_id == seeded["old_final_id"]
    assert state.narrative_sync_final_scene_row_id == seeded["old_final_id"]
    assert draft is not None and draft.last_promoted_revision_no is None
    assert draft.last_promoted_final_scene_row_id is None

    stateless = _seed_scene(session, "PREFLIGHT_STATELESS", draft_content="<p>仍有禁止内容。</p>")
    stateless_scene = session.get(SceneCard, stateless["scene_id"])
    stateless_state = session.get(SceneRunState, stateless["scene_id"])
    assert stateless_scene is not None and stateless_state is not None
    stateless_scene.forbidden_text = "禁止内容"
    session.delete(stateless_state)
    session.commit()
    with pytest.raises(DomainError) as stateless_caught:
        CanonicalSceneService(session).promote_author_draft(
            stateless["draft_id"],
            {
                "base_revision_no": 2,
                "expected_current_final_scene_row_id": None,
                "narrative_effect": "facts_unchanged",
            },
        )
    assert stateless_caught.value.code == "FINAL_TEXT_CONTINUITY_BLOCKED"
    session.flush()
    assert session.get(SceneRunState, stateless["scene_id"]) is None
    assert session.query(FinalScene).filter_by(scene_id=stateless["scene_id"]).count() == 1
    stateless_draft = session.get(AuthorDraft, stateless["draft_id"])
    assert stateless_draft is not None and stateless_draft.last_promoted_revision_no is None


def test_promote_source_safety_and_aggregate_failure_roll_back_everything(client, session, monkeypatch) -> None:
    unsafe = _seed_scene(session, "UNSAFE")
    from novel_system.services.reference_copy_gate import CopyCheck, CopyHit

    monkeypatch.setattr(
        "novel_system.services.final_text_gate.check_reference_copy",
        lambda *args, **kwargs: CopyCheck(
            blocked=True,
            hits=(CopyHit(start=0, end=12, matched_chars=12, sha256="0" * 16, book_id="book_x"),),
        ),
    )
    unsafe_response = _promote(client, unsafe, key="unsafe")
    assert unsafe_response.status_code == 409
    assert unsafe_response.json()["error"]["code"] == "SOURCE_SAFETY_BLOCKED"
    session.expire_all()
    assert session.query(FinalScene).filter_by(scene_id=unsafe["scene_id"]).count() == 1
    assert session.get(FinalScene, unsafe["old_final_id"]).status == "archived"

    monkeypatch.undo()
    blocked = _seed_scene(session, "AGG_BLOCKED")
    chapter_state = session.get(ChapterState, blocked["chapter_id"])
    chapter_state.aggregate_block_reason = "project_backtrack"
    session.commit()
    blocked_response = _promote(client, blocked, key="aggregate-blocked")
    assert blocked_response.status_code == 409
    assert blocked_response.json()["error"]["code"] == "CANONICAL_AGGREGATE_REBUILD_BLOCKED"
    session.expire_all()
    assert session.query(FinalScene).filter_by(scene_id=blocked["scene_id"]).count() == 1
    assert session.get(FinalScene, blocked["old_final_id"]).status == "archived"
    assert session.get(SceneRunState, blocked["scene_id"]).current_final_scene_row_id == blocked["old_final_id"]
    assert session.get(AuthorDraft, blocked["draft_id"]).last_promoted_revision_no is None


def test_promote_rejects_approved_chapter_and_non_scene_scope(client, session) -> None:
    approved = _seed_scene(session, "APPROVED", approved=True)
    approved_response = _promote(client, approved, key="approved")
    assert approved_response.status_code == 409
    assert approved_response.json()["error"]["code"] == "CHAPTER_APPROVED_LOCKED"

    chapter_draft = AuthorDraft(
        draft_id="author_draft_chapter_unsupported",
        object_type="chapter",
        object_id=approved["chapter_id"],
        source_text_ref="author_blank:chapter",
        content="整章稿不能绕过逐场权威链。",
        revision_no=1,
        status="current",
    )
    session.add(chapter_draft)
    session.commit()
    response = client.post(
        "/api/v1/author-drafts/author_draft_chapter_unsupported/promote-canonical",
        json={
            "base_revision_no": 1,
            "expected_current_final_scene_row_id": None,
            "narrative_effect": "facts_unchanged",
        },
        headers={"X-Idempotency-Key": "canonical-promote-chapter-unsupported"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUTHOR_DRAFT_PROMOTION_SCOPE_UNSUPPORTED"
