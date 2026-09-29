/* ==========================================================
   ws-labels — 词表的转出门面（2026-09-21 前端重构；2026-09-29 按领域拆开）
   ----------------------------------------------------------
   原来一个模块装四套互不相干的词表（目录、待办、记账与模型节点、风格参考），入口包因此带着全部。
   现在各住各的，模块直接 import 自己要的那一份：
   · labels/catalog.js —— 章 / 场的叫法与章节状态（chapterLabel、sceneLabel、CHAPTER_STATE_META、manuscriptStage……）；
   · labels/review.js —— 待办来源、写作偏好；
   · labels/llm.js —— 模型记账状态、模型节点的中文名；
   · labels/style-reference.js —— 风格参考的 16 维、段落类型、样例窗、场面 / 情绪标签、作业叫法。
   这里原样转出全部名字，旧的 import 路径（与 ws-labels.test.js）照旧可用。纯 ESM，不写 window。
   ========================================================== */

export * from "./labels/catalog.js";
export * from "./labels/review.js";
export * from "./labels/llm.js";
export * from "./labels/style-reference.js";
