"""风格参考 v3（2026-09-23）— 注入渲染包：参考怎样进每一个提示（契约文档 §2.3 / §2.5 / §3）。

- ``request``：:class:`~novel_system.services.style_reference.inject.request.StyleRenderRequest`——一次渲染的全部输入
  （角色 / 落点 / 窗数上限 / 场景与设计 / 改稿维 / 近期偏差 / 接收节点），取代改属性传参（J16）；
- ``bindings``：绑定解析（scene > character（POV 在前）> project > global），只有最具体的一层生效（J7）；
  ``describe_binding_layers`` 只查列、不渲染（U10）；作用域优先级只写在这里一次（``SCOPE_RANK``）；
- ``selection``：按本场设计挑样例、每场冻结一次（J2 / J3 / J4 / N4）；
- ``render``：``render_style``——参考方式三选一（N8 / J8）、文风卡 + 声音 + 样例 + 红线、按角色的口径；进程内缓存（J1）；
- ``fit``：贪心压预算（J14）；``audit``：不含正文的审计；
- ``preview``：与起草同一套选窗、同一个块次序的预览（J12）；
- ``gaps``：近期常见偏差（N7）；``routing``：模板 → 接收节点表（H1）。

本文件不导入任何子模块（2026-09-24 清理：原来的惰性 ``__getattr__`` 门面零使用者，且 ``preview`` 依赖
``style_policy`` → ``runtime_contract`` → ``inject.bindings``，包导入拉起子模块会成环）：一律
``from novel_system.services.style_reference.inject.<module> import X``。
"""
