"""三条归档路径各做哪些收尾（B03-25，[批准#22]）：流水线、作者「采纳并归档」、成稿中心「晋升」。

以前哪些收尾跟着做，取决于作者按的是哪个按钮：卷汇总（``Aggregator.maybe_aggregate_volume``）只有流水线做，之后
喂进起草提示的远景氛围就看这场是怎么归档的。现在三条路径的确定性收尾对齐，差别写在这张表里：

- 归档终稿的「像不像」读数：流水线在归档第 11 步记，另外两条由 ``Archiver.archive_final_scene`` 记；
- 章汇总：流水线只在章末那一场重建（归档第 8 步）；晋升（含带作者稿的采纳）每次都重建；不带作者稿的两种采纳
  （已归档的重放、流水线稿）不重建——章级读者一律按当前各场终稿现拼（重评 R13 复核补充 1、3），这里不照搬
  流水线「只在章末」的规则；
- 卷汇总跟着章汇总走：章汇总重建之后接着做（卷边界、幂等都由 ``maybe_aggregate_volume`` 自己管），失败只降级、
  不挡归档（与流水线第 9 步同一条规则）；
- 只在流水线做的：正文事件抽取（可选的 LLM 节点）、章级准终稿评审（LLM）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from novel_system.services.aggregator import Aggregator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveEffectsPlan:
    path: Literal["pipeline", "author_adopt", "manuscript_promote"]
    # 章汇总什么时候重建：流水线的章末那一场（检查点第 8 步）/ 每次归档 / 不重建（读时现拼）
    chapter_aggregate: Literal["chapter_last_stage", "every_archive", "derive_on_read"]
    # 章汇总重建之后接着卷汇总
    volume_after_chapter_aggregate: bool
    # 归档终稿的「像不像」读数
    fidelity_reading: bool
    # 正文事件抽取、章级准终稿评审（LLM 节点）
    llm_effects: bool


PIPELINE = ArchiveEffectsPlan(
    path="pipeline",
    chapter_aggregate="chapter_last_stage",
    volume_after_chapter_aggregate=True,
    fidelity_reading=True,
    llm_effects=True,
)
MANUSCRIPT_PROMOTE = ArchiveEffectsPlan(
    path="manuscript_promote",
    chapter_aggregate="every_archive",
    volume_after_chapter_aggregate=True,
    fidelity_reading=True,
    llm_effects=False,
)
AUTHOR_ADOPT = ArchiveEffectsPlan(
    path="author_adopt",
    chapter_aggregate="derive_on_read",
    volume_after_chapter_aggregate=False,
    fidelity_reading=True,
    llm_effects=False,
)


def aggregate_volume_after_chapter(session: Session, chapter_id: str) -> dict[str, Any]:
    """章汇总刚重建：接着卷汇总（章数不到卷边界就是 no_op）。在自己的保存点里做，失败只降级、记日志。"""
    try:
        with session.begin_nested():
            result = Aggregator(session).maybe_aggregate_volume(chapter_id)
        return dict(result or {"status": "no_op", "reason": "no_result"})
    except Exception as exc:  # noqa: BLE001 — 卷汇总是远景氛围，失败不挡归档
        _LOGGER.warning("volume aggregation degraded for chapter %s", chapter_id, exc_info=True)
        return {"status": "degraded", "error_code": type(exc).__name__}
