"""PR-10 §13 — MetricsRecorder + Repository CRUD + migration smoke。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.metrics_recorder import MetricsRecorder
from novel_system.services.style_reference.repository import StyleReferenceRepository


def test_record_happy_persists_event():
    with SessionLocal() as session:
        event_id = MetricsRecorder.record(
            session,
            "injection_invoked",
            target_kind="scene",
            target_ref_id="CH001_SC01",
            profile_id="sr_profile_x",
            binding_id="sr_bind_x",
            outcome="hit",
            latency_ms=42,
            context={"strategy": "A"},
        )
        assert event_id is not None
        assert event_id.startswith("sr_metric_")
        session.commit()

        repo = StyleReferenceRepository(session)
        rows = repo.list_metric_events(event_kind="injection_invoked")
        assert len(rows) == 1
        assert rows[0].event_id == event_id
        assert rows[0].outcome == "hit"
        assert rows[0].latency_ms == 42
        assert rows[0].context_json == {"strategy": "A"}


def test_record_optional_fields_default_to_none():
    with SessionLocal() as session:
        event_id = MetricsRecorder.record(session, "qc_gate_decided", outcome="pass")
        assert event_id is not None
        session.commit()

        repo = StyleReferenceRepository(session)
        rows = repo.list_metric_events(event_kind="qc_gate_decided")
        assert len(rows) == 1
        row = rows[0]
        assert row.target_kind is None
        assert row.target_ref_id is None
        assert row.profile_id is None
        assert row.binding_id is None
        assert row.latency_ms is None


def test_record_failure_swallows_and_returns_none():
    """ORM 异常时 record 应返 None 而不上抛。"""
    with SessionLocal() as session:
        with patch.object(session, "flush", side_effect=RuntimeError("disk full")):
            event_id = MetricsRecorder.record(session, "validation_executed", outcome="pass")
        assert event_id is None


def test_list_metric_events_filters_by_kind_profile_and_since():
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_metric_event(
            event_id="sr_metric_a", event_kind="injection_invoked",
            profile_id="sr_profile_a", outcome="hit", created_at="2026-05-20T00:00:00Z",
        )
        repo.create_metric_event(
            event_id="sr_metric_b", event_kind="qc_gate_decided",
            profile_id="sr_profile_a", outcome="fail", created_at="2026-05-23T00:00:00Z",
        )
        repo.create_metric_event(
            event_id="sr_metric_c", event_kind="injection_invoked",
            profile_id="sr_profile_b", outcome="miss", created_at="2026-05-25T00:00:00Z",
        )
        session.commit()

        inj_rows = repo.list_metric_events(event_kind="injection_invoked")
        assert {r.event_id for r in inj_rows} == {"sr_metric_a", "sr_metric_c"}

        prof_rows = repo.list_metric_events(profile_id="sr_profile_a")
        assert {r.event_id for r in prof_rows} == {"sr_metric_a", "sr_metric_b"}

        recent = repo.list_metric_events(since_ts="2026-05-22T00:00:00Z")
        assert {r.event_id for r in recent} == {"sr_metric_b", "sr_metric_c"}


def test_list_metric_events_limit_and_order_desc():
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        for i in range(5):
            repo.create_metric_event(
                event_id=f"sr_metric_seq_{i}",
                event_kind="validation_executed",
                created_at=f"2026-05-2{i}T00:00:00Z",
            )
        session.commit()

        recent = repo.list_metric_events(event_kind="validation_executed", limit=2)
        assert [r.event_id for r in recent] == ["sr_metric_seq_4", "sr_metric_seq_3"]


def test_migration_creates_table_smoke():
    """运行时 schema 应包含 metric_events 表,仓储可直接读写。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_metric_event(
            event_id="sr_metric_smoke",
            event_kind="injection_invoked",
            outcome="hit",
        )
        session.commit()
        rows = repo.list_metric_events()
        assert len(rows) == 1


def test_failed_record_does_not_poison_outer_transaction():
    """风格参考 v3 V9:遥测写失败只回滚自己的保存点,外层事务照常提交。

    此前 ``record`` 直接 ``session.flush()``:一行事件 INSERT 失败(这里用重复主键复现)会让整个
    会话进入 ``PendingRollbackError``——异常被吞掉了,业务事务却跟着死掉。
    """
    from types import SimpleNamespace

    from sqlalchemy import select

    from novel_system.db.models import StyleReferenceMetricEvent
    from novel_system.services.style_reference import metrics_recorder

    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_metric_event(event_id="sr_metric_0123456789ab", event_kind="seed")
        session.commit()

        # 外层事务里一条尚未提交的业务写
        repo.create_metric_event(event_id="sr_metric_outer_business", event_kind="business")
        with patch.object(
            metrics_recorder.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="0123456789ab" + "0" * 20),
        ):
            event_id = MetricsRecorder.record(session, "qc_gate_decided", outcome="pass")
        assert event_id is None

        # 会话仍可用,外层的写照常提交
        session.commit()
        ids = set(session.scalars(select(StyleReferenceMetricEvent.event_id)).all())
        assert ids == {"sr_metric_0123456789ab", "sr_metric_outer_business"}
