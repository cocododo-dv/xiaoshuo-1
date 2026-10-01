from __future__ import annotations

import pytest


from tests.support.scene_pipeline import seed_story

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def test_l3_acceptance_smoke(client) -> None:
    seed_story(client)
    run_scene = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "acceptance-scene-1"},
    )
    assert run_scene.status_code == 200
    assert run_scene.json()["data"]["current_bundle_id"]
