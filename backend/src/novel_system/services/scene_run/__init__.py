"""一场场景运行（预算 → 规划 → bundle → 首稿 → 硬 QC → 风格稿 / 终选 → 软 QC → 准终稿 → 归档）的实现。

``services.orchestrator.Orchestrator`` 是对外的门面（构造函数与重导出的名字）；这里放它的零件。

纯零件：

- :mod:`.constants` 写进检查点的跳过原因与阶段名；
- :mod:`.near_final_gate` 准终稿重写稿的门（被拒原因、gate 小结、警告）；
- :mod:`.results` 运行结果的装配（QC 摘要、准终稿 payload、警告合并、finality）；
- :mod:`.snapshots` 检查点里哈希的行快照（键名与取值即契约）。

``Orchestrator`` 直接继承的 mixin（彼此只经 ``self`` 互调，不互相导入，也不导入门面）：

- :mod:`.kernel` 检查点内核（执行归属四字段、守卫、读写、哈希）；
- :mod:`.critique` 软 QC 前的自动批评产品（装配、复验、被拒产品的恢复）；
- :mod:`.planning` 规划检查点（``planning_ready`` 子游标 0..3）的读回与复验；
- :mod:`.drafts` bundle、首稿与硬 QC 的读回，生成产品的账本父调用核对；
- :mod:`.style_candidates` 风格稿候选的工作项、读回与复验、Best-of-N 份数、匿名终选门；
- :mod:`.soft_qc` 软 QC（``soft_qc_ready`` 子游标 0..3）的驱动、修补留用 / 回退与检查点存取；
- :mod:`.near_final_stage` 准终稿（``near_final_ready`` 子游标 0..3）的驱动、重写门与检查点存取；
- :mod:`.lifecycle` 运行的入口、执行归属、终态分类、失败审计与已归档重放；
- :mod:`.archive` 归档尾段（``near_final_ready`` 子游标 4..11 与 ``archived``）。

包本身不导入子模块（免得包与子模块互相导入成环）。
"""
