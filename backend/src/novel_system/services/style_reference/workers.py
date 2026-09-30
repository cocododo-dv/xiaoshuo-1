"""风格参考作业的处理器与维护任务：显式登记（不靠模块导入的副作用）。

:func:`install_workers` 把三种作业的处理器（段落分类 ``import_job``、学习文风 ``learn_job``、对照检查
``check_job``，各带取消收尾钩子 / 能否续跑的规则）与风格作业的维护任务（``cleanup`` 的遥测留存）登记到
``jobs`` 的登记簿上。FastAPI lifespan 在启动清扫线程之前调用它；不经过应用、直接跑作业的地方（工具、测试）同样先
调用它。可以重复调用：同名登记覆盖。

以前这些登记写在各模块的末尾、靠 lifespan 里几行 ``# noqa: F401`` 的导入触发——漏导入一个模块，对应的任务就
静默不跑（§8.1 C8：遥测留存清理从来没跑过，就是这样漏的）。``jobs.start_job_sweeper`` 在还有种类没有处理器时
记一条错误日志。
"""

from __future__ import annotations

from novel_system.services.style_reference.jobs import (
    JOB_KIND_CHECK,
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    register_job_handler,
    register_maintenance_task,
)


def install_workers() -> None:
    """登记处理器与维护任务（见模块说明）。"""
    from novel_system.services.style_reference import check_job, cleanup, import_job, learn_job

    register_job_handler(
        JOB_KIND_CLASSIFY,
        import_job.run_classification_job,
        on_cancelled=import_job.on_classification_cancelled,
    )
    register_job_handler(
        JOB_KIND_LEARN,
        learn_job.run_learn_job,
        on_cancelled=learn_job.on_learn_cancelled,
        resumable=learn_job.learn_resumable,
    )
    register_job_handler(JOB_KIND_CHECK, check_job.run_check_job, resumable=check_job.check_never_resumes)
    # 清扫线程启动时跑一次、之后每 24 小时一次；登记的是模块里的函数，它在调用时才取 cleanup_metric_events
    register_maintenance_task(
        cleanup.METRIC_EVENTS_MAINTENANCE_TASK,
        cleanup.run_metric_events_retention,
        interval_seconds=cleanup.METRIC_EVENTS_CLEANUP_INTERVAL_SECONDS,
    )


__all__ = ["install_workers"]
