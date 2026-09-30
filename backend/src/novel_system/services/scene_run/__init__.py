"""一场场景运行（预算 → 规划 → bundle → 首稿 → 硬 QC → 风格稿 / 终选 → 软 QC → 准终稿 → 归档）的实现。

``services.orchestrator.Orchestrator`` 是对外的门面；这里放它的零件：

- :mod:`.constants` 写进检查点的跳过原因与阶段名；
- :mod:`.near_final_gate` 准终稿重写稿的门（被拒原因、gate 小结、警告）；
- :mod:`.results` 运行结果的装配（QC 摘要、准终稿 payload、警告合并、finality）；
- :mod:`.snapshots` 检查点里哈希的行快照（键名与取值即契约）。
- :mod:`.kernel` 检查点内核（执行归属四字段、守卫、读写、哈希），``Orchestrator`` 直接继承。

包本身不导入子模块（免得包与子模块互相导入成环）。
"""
