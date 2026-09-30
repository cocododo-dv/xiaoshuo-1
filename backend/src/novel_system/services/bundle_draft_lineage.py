"""「前文声音锚」读的是哪一稿：一场最新的、真正已风格化的草稿（B03-19 从 bundle_builder 拆出）。

风格稿未过安全门时写成主 style_draft 行的已批准中性稿、与中性稿逐字相同的行都不算；风格直起（style_first）下
中性步位的首稿本身就是目标文风，没有更晚的风格稿时它算。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import AttemptTracker, SceneDraft

# 「前文声音锚」取上一场最新的**已风格化**稿：style_draft 本体、反模板重写、软补丁、
# 安全挽救稿都算；中性稿 / rejected 行不算（前者无目标文风，后者是被否决的文本）。
STYLED_DRAFT_STAGES: tuple[str, ...] = (
    "style_draft",
    "de_template",
    "style_patch",
    "style_salvage",
)
# scene_generation 在风格稿未过确定性安全门时，把**已批准的中性稿**原文写成主
# ``style_draft`` 行（provider 原稿另存为 ``style_rejected``），并在 AttemptTracker
# ``details_json.content_source`` 上打这个标记；行本身没有字段能区分。
NEUTRAL_FALLBACK_CONTENT_SOURCE = "approved_neutral_fallback"
# 2026-09-12 风格直起:style_first 下中性步位的首稿已按参考作者手笔写成——它的正文**是**
# 目标文风,既不该被当成「中性稿」排除,也可以直接作前文声音锚(无更晚的风格稿时)。
STYLE_FIRST_DRAFT_CONTENT_SOURCE = "style_first_draft"


def _normalized_draft_text(content: str | None) -> str:
    return str(content or "").strip()


def _neutral_fallback_styled_row_ids(session: Session, scene_id: str) -> set[str]:
    """该场景 AttemptTracker 标记为「回退到中性稿」的 styled 行 ``row_id`` 集合。"""
    row_ids: set[str] = set()
    for details in (
        session.execute(select(AttemptTracker.details_json).where(AttemptTracker.scene_id == scene_id)).scalars().all()
    ):
        if not isinstance(details, dict):
            continue
        if details.get("content_source") != NEUTRAL_FALLBACK_CONTENT_SOURCE:
            continue
        row_id = details.get("row_id")
        if isinstance(row_id, str) and row_id:
            row_ids.add(row_id)
    return row_ids


def _style_first_neutral_row_ids(session: Session, scene_id: str) -> set[str]:
    """该场景 AttemptTracker 标记为「首稿直起」的 ``neutral_draft`` 行 ``row_id`` 集合。"""
    row_ids: set[str] = set()
    for details in (
        session.execute(
            select(AttemptTracker.details_json).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "neutral_draft",
            )
        )
        .scalars()
        .all()
    ):
        if not isinstance(details, dict):
            continue
        if details.get("content_source") != STYLE_FIRST_DRAFT_CONTENT_SOURCE:
            continue
        row_id = details.get("row_id")
        if isinstance(row_id, str) and row_id:
            row_ids.add(row_id)
    return row_ids


def _neutral_draft_texts(session: Session, scene_id: str) -> set[str]:
    """该场景所有**真正中性**的 ``neutral_draft`` 行正文（规范化后）集合。

    style_first 的首稿虽然落在 ``neutral_draft`` 行,却已是目标文风,不算中性正文。
    """
    style_first_rows = _style_first_neutral_row_ids(session, scene_id)
    return {
        _normalized_draft_text(content)
        for row_id, content in session.execute(
            select(SceneDraft.row_id, SceneDraft.content).where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage == "neutral_draft",
            )
        ).all()
        if row_id not in style_first_rows
    }


def _latest_style_first_draft(session: Session, scene_id: str) -> SceneDraft | None:
    """最新一条首稿直起的 ``neutral_draft`` 行(未被否决);没有 → ``None``。"""
    row_ids = _style_first_neutral_row_ids(session, scene_id)
    if not row_ids:
        return None
    return (
        session.execute(
            select(SceneDraft)
            .where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage == "neutral_draft",
                SceneDraft.status != "rejected",
                SceneDraft.row_id.in_(sorted(row_ids)),
            )
            .order_by(SceneDraft.created_at.desc(), SceneDraft.row_id.desc())
        )
        .scalars()
        .first()
    )


def latest_styled_draft_for_scene(session: Session, scene_id: str) -> SceneDraft | None:
    """某场景最新一条**真正**已风格化、未被否决的 ``SceneDraft``。

    stage 在 ``STYLED_DRAFT_STAGES`` 内且未被否决只是必要条件：风格稿未过安全门时
    scene_generation 会把已批准的中性稿原文写成主 ``style_draft`` 行
    （``STYLE_DRAFT_FALLBACK_NEUTRAL``），这种行承载的是无目标文风的中性散文，
    作为「前文声音锚」会把下一场钉在中性稿上。因此再跳过两类行：
    AttemptTracker 标记 ``content_source == approved_neutral_fallback`` 的行，
    以及正文与同场景任一 ``neutral_draft`` 行完全相同的行。都被跳过 → ``None``。
    """
    rows = (
        session.execute(
            select(SceneDraft)
            .where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage.in_(STYLED_DRAFT_STAGES),
                SceneDraft.status != "rejected",
            )
            .order_by(SceneDraft.created_at.desc(), SceneDraft.row_id.desc())
        )
        .scalars()
        .all()
    )
    fallback_row_ids = _neutral_fallback_styled_row_ids(session, scene_id)
    neutral_texts = _neutral_draft_texts(session, scene_id)
    for row in rows:
        if row.row_id in fallback_row_ids:
            continue
        if _normalized_draft_text(row.content) in neutral_texts:
            continue
        return row
    # 2026-09-12 风格直起:还没有合格的风格稿时,首稿直起的中性步位行本身就是目标文风。
    return _latest_style_first_draft(session, scene_id)
