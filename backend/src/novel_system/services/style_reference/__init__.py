"""Style Reference (style_reference) service package.

现行说明见 ``docs/style-reference.md``（v3 重构的设计与台账见 ``docs/style-reference-v3-2026-09-23.md``）。

**这个 ``__init__`` 不导入任何子模块**（2026-09-24 清理 S4：原来重导出的 39 个名字零使用者）。包内任何模块第一次被导入时
都会先跑这个文件；这里若导入了回头依赖 ``services.style_policy`` 的模块（``binding_apply``），``style_policy`` →
``binding_config`` → 本文件 → ``binding_apply`` → ``style_policy``（还没初始化完）就在新解释器里 ImportError（M1，
``tests/test_style_reference_import_order.py`` 守着「包导入不拉起任何子模块」）。一律按子模块导入：
``from novel_system.services.style_reference.<module> import X``。学习 / 分类 / 检查作业的处理器在各自模块导入时注册
（``register_job_handler``），由 ``api/app.py`` 显式导入。
"""
