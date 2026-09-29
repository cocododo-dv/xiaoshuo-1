"""全系统的定期维护任务：与风格参考作业无关的那些（例如幂等重放缓存的保留期清理）。

登记簿是 ``background_jobs.MaintenanceRegistry`` 的一个全系统实例，由运行任务的巡检线程
（``background_recovery.start_run_job_sweeper``，每分钟一拍，启动那一拍不跑）顺带执行：每拍只跑到期的任务，
任务自己开会话、自己提交；抛出的异常只记日志，下一个间隔再试，不影响巡检本身。风格参考作业自己的维护任务
仍登记在 ``style_reference.jobs``（随风格作业清扫线程跑）。

约定与风格作业的维护任务相同：模块导入时登记，同名覆盖。``run_at_start=False`` 的任务从登记（或巡检线程启动）
那一刻起等满一个间隔才第一次跑——部署后的第一拍不做大批量删除，先备份、再清理。

本模块是叶子（只依赖 ``background_jobs``）：任何服务都能 import 它来登记，不会闭环。
"""

from __future__ import annotations

from novel_system.services.background_jobs import MaintenanceRegistry, MaintenanceTask

SYSTEM_MAINTENANCE = MaintenanceRegistry("system")


def register_maintenance_task(
    name: str,
    task: MaintenanceTask,
    *,
    interval_seconds: float,
    run_at_start: bool = True,
) -> None:
    """登记一项全系统维护任务（同名覆盖）。"""
    SYSTEM_MAINTENANCE.register(name, task, interval_seconds=interval_seconds, run_at_start=run_at_start)


def unregister_maintenance_task(name: str) -> None:
    SYSTEM_MAINTENANCE.unregister(name)


def reset_maintenance_schedule(*, now: float | None = None) -> None:
    """巡检线程启动时调用：先跑的任务第一拍就跑，等一个间隔的任务从现在起计时。"""
    SYSTEM_MAINTENANCE.reset_schedule(now=now)


def run_due_maintenance(*, now: float | None = None) -> list[str]:
    """跑一遍到期的全系统维护任务（``now`` 是单调时钟秒数，缺省当前）；返回这一轮跑了的任务名（失败的不算）。"""
    return SYSTEM_MAINTENANCE.run_due(now=now)
