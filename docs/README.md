# 文档导航

本文档是仓库文档的统一入口，最后核查日期为 2026-09-23。若日期化计划、旧证据与现行代码冲突，以根目录 `README.md`、本页列出的运行时契约和当前代码为准。

## 日常使用

- [操作手册](operator-manual.md)：React 正式工作台的入口、创作主线、异常处理和数据边界。
- [运行安全与资源边界](runtime-safety.md)：网络、令牌、额度、内容复核、路径导入、恢复和备份约束。
- [正史连续性与长篇记忆](canon-continuity.md)：正文事实候选、证据复核、权威提交、上下文注入和历史数据迁移。
- [雪花方法契约](snowflake-method-contract.md)：十步、场景形态、闸门与失效规则逐条对照 Ingermanson 的雪花写作法——项目字段、硬闸门、有意的差异与阶段 A–E 变更记录。
- [一条书脊：构思 → 目录 → 三张台子](book-spine-catalog-contract.md)：雪花整理出来的章与场怎样真的长在目录里（幕的口径、空白占位章、书签）、场景身份为什么是 `scene_id`、目录载荷里的设计卡与工作状态、「现在该写哪一场」的唯一规则、确认即同步与四种留给作者看差异的卡、章必须是故事序上连续的一段；2026-09-19 阶段 X 的现行契约。
- [雪花构思 · 教练、要点、方向与生成](snowflake-coach-and-directions.md)：构思视图里 AI 的四个概念（要点 / 方向 / 改写 / 生成）怎么分工、每个入口做什么、`fe-candidates` / `generate` / `assistant` 的契约与迁移 0088；2026-09-17 阶段 U 把「候选」页签并进教练之后的现行契约。
- [场景诊断：一份记录、一处展示](scene-diagnosis-2026-09-22.md)：文学质量的 21 维规则、写作台的节奏检查、起草台的准定稿评审与 AI 深评合成同一种发现（稳定 signal id、中文、钉到段落），写作台深改面板是唯一的展示处；忽略按 id 记并反向作用到文学质量与成稿门；改写请求带着发现；深评拒绝式；第二轮（§7）：节奏检查按参考作者校准、「AI 看这一处」局部深评、成稿中心「AI 通读本章」与诊断页签、全书每场 / 每章的计数；2026-09-22 的现行契约。
- [风格参考（现行说明）](style-reference.md)：参考书 → 学习文风 → 用于作品三步；分类 / 学习文风 / 对照检查三种持久作业、文风卡与维度状态、每场冻结的样例窗、参考方式三选一、按「像不像」读数决定的风格步与定向修改、唯一抄袭门与受保护专名、接口、配置、部署与排障。
- [Ingermanson 雪花方法核心思想提取](ingermanson-snowflake-ideas.md)：两本原著（长篇篇第 19–20 章、场景篇全书）的方法要点——故事与坩埚、主动 / 反应场景的三拍标准、写后分诊、十步与时间盒；雪花方法契约所对照的方法依据。

## 开发与发布

- [发布检查清单](release-checklist.md)：CI、Windows、React E2E 和 WSL Chroma 发布门。

## 当前专项记录

- [章节编排 LLM 接入设计（2026-07-16，已实现）](chapter-arrangement-llm-design-2026-07-16.md)：章节蓝图一等公民 + 上下文底座 + 候选/补全/体检三通道与只填空补丁纪律。
- [雪花「整理成章节结构」重新设计（2026-07-25，已实施）](snowflake-chaptering-design-2026-07-25.md)：构思侧章表一等公民 + 可预览分章 + scene_id 撞号与幽灵场两个数据缺陷的修复方案；现行契约见上面的雪花方法契约。
- [风格参考 v3 变更记录（2026-09-23）](style-reference-v3-2026-09-23.md)：以参考为标准的一次性重构——作者的决定、16 维度与策略选择器的评估结论、目标架构与接口、全部问题台账与各工作包的完成日志；现行说明见上面的「风格参考」。
- 风格参考的历史文档（只描述当时的实现，现行说明见 [风格参考](style-reference.md)），归档在 `docs/history/style/`：
  [v1.1 设计](history/style/style_reference_module_design_v1.1.md)、[实施账本](history/style/style-reference-progress.md)、[Phase 3 完成记录](history/style/style-reference-phase3-backlog.md)、[RAG v2 内容克制检索](history/style/style-reference-rag-content-independence.md)、[运行时契约与反馈闭环](history/style/style-reference-runtime-contract.md)、[动态模仿 v2（2026-08-20）](history/style/style-reference-dynamic-imitation-v2-2026-08-20.md)、[风格模仿 v2 执行方案（2026-09-05）](history/style/style-imitation-v2-plan-2026-09-05.md)、[样例优先（2026-09-09）](history/style/style-exemplar-first-2026-09-09.md)、[风格直起与结构跟随（2026-09-12）](history/style/style-first-draft-plan-2026-09-12.md)、[保真修补（2026-09-14）](history/style/style-fidelity-fixes-2026-09-14.md)、[风格参考优先（2026-09-22）](history/style/style-reference-first-2026-09-22.md)。

## 文档维护规则

1. 根 README 只保留当前启动、主流程、数据迁移和验证入口。
2. 操作手册只描述正式 React 工作台。
3. `output/`、测试截图、PID、日志、IDE 配置和测试缓存不得提交。需要长期引用的运行结论应归档为小型、可复算的摘要或 manifest。
4. 一次性审计、已完成迁移包和过期实施计划不在主线长期保留；Git 历史承担追溯职责。
5. 日期化证据不得被改写成“当前状态”；当前 Alembic head、命令和能力边界必须重新从代码或根 README 核对。
6. 被新的现行说明取代的专项文档移到 `docs/history/<领域>/`，顶部加一行「历史文档」横幅指向现行说明；正文保持原样。
