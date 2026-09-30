"""三条归档路径各做哪些收尾（B03-25，[批准#22]）：流水线、作者「采纳并归档」、成稿中心「晋升」。

以前哪些收尾跟着做，取决于作者按的是哪个按钮：卷汇总（``Aggregator.maybe_aggregate_volume``）只有流水线做，之后
喂进起草提示的远景氛围就看这场是怎么归档的。现在三条路径的确定性收尾对齐，差别如下。这里只是说明，行为在各条路径
自己的代码里；每一条都由行为测试钉住（``tests/test_archive_effects_plan.py``；流水线那一边由归档尾段的检查点测试
钉住，例如 ``test_scene_run_checkpoint_archive.py::test_chapter_last_sub9_volume_boundary_crash_reuses_same_summary``），
改了哪一条就改对应的测试：

- 「像不像」读数：三条路径都记——流水线在归档第 11 步，另外两条由 ``Archiver.archive_final_scene`` 记；
- 章汇总：流水线只在章末那一场重建（归档第 8 步）；晋升（成稿中心，以及带作者稿的采纳——React 的「采纳并归档」
  总带作者稿 ``exact_author_draft``，走的就是晋升）每次都重建；不带作者稿的两种采纳（已归档的重放、流水线稿，只有
  API 兼容调用会走）不重建——章级读者一律按当前各场终稿现拼（重评 R13 复核补充 1、3），这里不照搬流水线「只在
  章末」的规则；
- 卷汇总跟着章汇总走：流水线在章末那一场（归档第 9 步）；晋升在章汇总之后（:func:`aggregate_volume_after_chapter`：
  卷边界、幂等都由 ``maybe_aggregate_volume`` 自己管，失败只降级、不挡归档，与流水线第 9 步同一条规则）；不重建
  章汇总的路径也不做——卷汇总读的就是各章的章汇总；
- 只在流水线做的：正文事件抽取（可选的 LLM 节点，归档第 6 步）、章级准终稿评审（LLM，归档第 10 步）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.aggregator import Aggregator

_LOGGER = logging.getLogger(__name__)


def aggregate_volume_after_chapter(session: Session, chapter_id: str) -> dict[str, Any]:
    """章汇总刚重建：接着卷汇总（章数不到卷边界就是 no_op）。在自己的保存点里做，失败只降级、记日志。"""
    try:
        with session.begin_nested():
            result = Aggregator(session).maybe_aggregate_volume(chapter_id)
        return dict(result or {"status": "no_op", "reason": "no_result"})
    except Exception as exc:  # noqa: BLE001 — 卷汇总是远景氛围，失败不挡归档
        _LOGGER.warning("volume aggregation degraded for chapter %s", chapter_id, exc_info=True)
        return {"status": "degraded", "error_code": type(exc).__name__}
