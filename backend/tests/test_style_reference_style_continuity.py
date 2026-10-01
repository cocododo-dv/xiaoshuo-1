"""跨场景声音参照（契约读取）与归档读数槽位。

风格参考 v3（2026-09-23）删掉了漂移驾驶：归档期的 ``observe_style_drift`` / ``style_drift_observed`` 事件、
下一场 bundle 的漂移校准段与漂移优先选窗。这里留下：其它测试共用的种子 helper、契约读取
（``contract_deliberate_repetition``）与归档读数槽位的守卫。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    SceneCard,
    StyleReferenceMetricEvent,
)
from novel_system.services.scene_archive_effects import SceneArchiveEffects
from novel_system.services.style_reference import style_continuity as sc
from tests.support.style_reference import (
    add_final_scene as _add_final_scene,
    seed_style_binding as seed_binding,
    seed_work,
    short_dense_reference,
)

# 「偏长句、少逗号」成稿：每句四十余字、几乎没有逗号，≥300 可见字。
LONG_SENTENCE_TEXT = "\n\n".join(
    [
        "他沿着河岸一直走到那座旧桥的下面才停住脚步望着对岸渐渐亮起来的灯火想起许多年前的那个傍晚也是这样的天色。",
        "母亲站在门口等他回来手里还攥着那条没有织完的围巾风把她的头发吹得乱了她却一直没有抬手去理。",
        "他把行李放在门边沿着走廊走到尽头的书房里去桌上的茶早已经凉透了窗外的河水在暮色里显得格外沉静。",
        "对岸的灯陆续亮起来他没有开灯只坐在旧椅子上等着夜色完全落下来直到屋里再也看不见自己的手。",
    ]
    * 3
)


def _layer(scope: str, deliberate: bool | None, order: int = 0) -> dict:
    voice = {} if deliberate is None else {"voice_signature": {"deliberate_repetition": deliberate}}
    return {"order": order, "binding": {"scope": scope}, "profile": {"profile_json": voice}}


def test_contract_deliberate_repetition_reads_the_layer_that_takes_effect() -> None:
    """刻意复沓看生效的那一层（最具体的一层，J7——渲染与策略读的也是它），不是「任一层」（B10-24）：旧的多层 v1
    契约里作品层标了复沓、场景层没标，这一场的重复不能当成作者的复沓放过。"""
    single_on = {"layers": [_layer("project", True)]}
    single_off = {"layers": [_layer("project", False)]}
    assert sc.contract_deliberate_repetition(single_on) is True
    assert sc.contract_deliberate_repetition(single_off) is False
    # v1 多层（按层序由泛到具体）：场景层生效
    generic_marks_it = {"layers": [_layer("project", True, 0), _layer("scene", False, 1)]}
    assert sc.contract_deliberate_repetition(generic_marks_it) is False
    specific_marks_it = {"layers": [_layer("global", False, 0), _layer("character", True, 1)]}
    assert sc.contract_deliberate_repetition(specific_marks_it) is True
    # 角色层之间 POV 在前（层序靠前的那个）
    pov_first = {"layers": [_layer("character", True, 0), _layer("character", False, 1)]}
    assert sc.contract_deliberate_repetition(pov_first) is True
    assert sc.contract_deliberate_repetition({"layers": [_layer("project", None)]}) is False
    assert sc.contract_deliberate_repetition({"layers": []}) is False
    assert sc.contract_deliberate_repetition(None) is False


# ---------------------------------------------------------------------------
# 归档读数槽位（archive:style_drift:0，风格参考 v3 起记「像不像」读数）
# ---------------------------------------------------------------------------


def _effects(session) -> SceneArchiveEffects:
    return SceneArchiveEffects(session, None, execution_id=None, run_job_id=None)


def test_archive_reading_slot_writes_no_drift_event_and_passes_the_checkpoint_validator(session) -> None:
    from novel_system.services.errors import DomainError
    from novel_system.services.orchestrator import Orchestrator

    seed_work(session, project_id="P_W6_ARC", scenes_per_chapter=1)
    seed_binding(
        session,
        project_id="P_W6_ARC",
        seed="w6arc",
        profile_json={"voice_signature": {"features": short_dense_reference(), "deliberate_repetition": False}},
    )
    scene = session.get(SceneCard, "P_W6_ARC_CH01_SC01")
    _add_final_scene(session, scene_id=scene.scene_id, content=LONG_SENTENCE_TEXT)

    product = _effects(session)._record_archive_fidelity_reading(scene)
    session.commit()

    assert product["outcome"] == "not_applicable"
    kinds = set(session.scalars(select(StyleReferenceMetricEvent.event_kind)).all())
    assert "style_drift_observed" not in kinds
    # 已持久化的检查点按原 kind / step_key 续跑：读数槽位的产品必须过真实校验器
    orch = Orchestrator(session)
    reading_product = orch._archive_product(
        scene=scene,
        kind="style_drift",
        outcome=product["outcome"],
        step_key="archive:style_drift:0",
        input_hash=orch._text_hash(LONG_SENTENCE_TEXT),
        **{key: value for key, value in product.items() if key != "outcome"},
    )
    assert orch._validate_archive_drift_product(scene, reading_product, require_checkpoint_hash=False) == reading_product
    with pytest.raises(DomainError) as excinfo:
        orch._validate_archive_drift_product(
            scene, {**reading_product, "outcome": "not_an_outcome"}, require_checkpoint_hash=False
        )
    assert excinfo.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_style_continuity_no_longer_exposes_drift_steering() -> None:
    for name in ("observe_style_drift", "latest_drift_event", "latest_drift_calibration", "drift_ptype_priority"):
        assert not hasattr(sc, name), name
