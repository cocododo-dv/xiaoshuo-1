"""风格参考的原文重合检测（``validation/plagiarism.py``）。

:func:`check_plagiarism` 是规范化 n-gram 重合检测：唯一抄袭门（``services/reference_copy_gate``）、学习作业的
原文重合过滤（``CorpusOverlapIndex``）、自我重复守卫都用它的规范化与判定口径。

旧校验层（「回测」：量化 / 语义 / 禁忌模式三路校验、同步裁决、后台执行器与报告表）已删除；「像不像」由
「对照检查」回答——作业表上的 check 作业（``check_job.py``），结果写进读数表（读数 + 参考评审 + 抄袭门）。
"""

from __future__ import annotations

from novel_system.services.style_reference.validation.plagiarism import check_plagiarism

__all__ = ["check_plagiarism"]
