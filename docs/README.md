# 文档导航

本文档是仓库文档的统一入口，最后核查日期为 2026-09-13。若日期化计划、旧证据与现行代码冲突，以根目录 `README.md`、本页列出的运行时契约和当前代码为准。

## 日常使用

- [操作手册](operator-manual.md)：React 正式工作台的入口、创作主线、异常处理和数据边界。
- [运行安全与资源边界](runtime-safety.md)：网络、令牌、额度、内容复核、路径导入、恢复和备份约束。
- [正史连续性与长篇记忆](canon-continuity.md)：正文事实候选、证据复核、权威提交、上下文注入和历史数据迁移。
- [雪花方法契约](snowflake-method-contract.md)：十步、场景形态、闸门与失效规则逐条对照 Ingermanson 的雪花写作法——项目字段、硬闸门、有意的差异与阶段 A–E 变更记录。
- [Ingermanson 雪花方法核心思想提取](ingermanson-snowflake-ideas.md)：两本原著（长篇篇第 19–20 章、场景篇全书）的方法要点——故事与坩埚、主动 / 反应场景的三拍标准、写后分诊、十步与时间盒；雪花方法契约所对照的方法依据。

## 开发与发布

- [发布检查清单](release-checklist.md)：CI、Windows、React E2E 和 WSL Chroma 发布门。

## 当前专项记录

- [章节编排 LLM 接入设计（2026-07-16，已实现）](chapter-arrangement-llm-design-2026-07-16.md)：章节蓝图一等公民 + 上下文底座 + 候选/补全/体检三通道与只填空补丁纪律。
- [雪花「整理成章节结构」重新设计（2026-07-25，已实施）](snowflake-chaptering-design-2026-07-25.md)：构思侧章表一等公民 + 可预览分章 + scene_id 撞号与幽灵场两个数据缺陷的修复方案；现行契约见上面的雪花方法契约。
- [风格参考设计](style_reference_module_design_v1.1.md)、[实施账本](style-reference-progress.md)与[Phase 3 完成记录](style-reference-phase3-backlog.md)：后两者是历史实施依据，Phase 3 A/B/C 已全部完成。
- [风格参考动态模仿 v2](style-reference-dynamic-imitation-v2-2026-08-20.md)：任意参考语料契约、软分布提示、自然度门控、独立评测和开源融合决策。
- [风格参考 RAG v2：内容克制检索](style-reference-rag-content-independence.md)：结构化风格签名、旧索引迁移、合成 A/B 及证据边界。
- [风格参考运行时契约与反馈闭环](style-reference-runtime-contract.md)：冻结风格血缘、统一上下文/基线、降级规则和盲选终选（2026-09-14 起风格反馈层已删除，盲选门由 `NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED` 打开）。文末「v2 附记」记录 2026-09 新增的冻结键、Bundle section、notices 与 styled-draft gate。
- [风格参考 · 样例优先（2026-09-09，Step 1）](style-exemplar-first-2026-09-09.md)：v2 之后的三步计划第一步——原文样例成为主信号（默认约 2 万字、满强度约 2.4 万字原文进系统提示、按场景轮换、契约冻结整本书段落根哈希、预算按整窗口卸载）、四个风格通道提示词改写、解码惩罚归零；含改前改后的阅读方法与部署注意。
- [风格参考 · 风格直起与结构跟随（2026-09-12，Step 2 计划，最大化版）](style-first-draft-plan-2026-09-12.md)：三步计划第二步——有绑定时首稿直接以参考作者手笔从 bundle 写、`style_draft` 改为再靠近一层的复读、去模板门 / 自动批评 / 近终稿确定性门 / 成稿门 / 新鲜度词表在有绑定时整体让位、参考的结构画像进入雪花场景规划 / 章架构 / 章内规划 / 蓝图 / 章级评审；`binding.config_json.draft_mode` 冻结进运行时契约，`neutral_first` 即现状对照组。
- [风格参考 · 保真修补（2026-09-14）](style-fidelity-fixes-2026-09-14.md)：第三次评估后的修复方案——修复 / 补丁带风格、形状归一化让位、近终稿自动重写收紧、soft_qc 换强模型、新鲜度预算只留内容级、导入层按真实书修（章题、副文本、人称、文言标记）、全书窗口索引选窗、作者可见的通知与窗口、规划与写手侧注入；§8 为完成记录。
- [风格模仿 v2 执行方案（2026-09-05，实施中）](style-imitation-v2-plan-2026-09-05.md)：声音级模仿 / 叙事层迁移 / 跨场景一致性三项的根因、共享契约（`profile_json` 新键、Bundle 新 section、`injection_budget.yaml` 键、intensity 语义、`style_drift_observed` 事件）、W1–W8 工作包与验收；§5 为完成记录，与代码冲突时以该文为准并回写。

## 文档维护规则

1. 根 README 只保留当前启动、主流程、数据迁移和验证入口。
2. 操作手册只描述正式 React 工作台。
3. `output/`、测试截图、PID、日志、IDE 配置和测试缓存不得提交。需要长期引用的运行结论应归档为小型、可复算的摘要或 manifest。
4. 一次性审计、已完成迁移包和过期实施计划不在主线长期保留；Git 历史承担追溯职责。
5. 日期化证据不得被改写成“当前状态”；当前 Alembic head、命令和能力边界必须重新从代码或根 README 核对。
