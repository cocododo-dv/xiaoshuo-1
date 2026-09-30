import { SCENE_STATE_META } from "./labels/catalog.js";

/* ==========================================================
   章节编排 — 结构常量（幕框架 / 场的进度点）。章节真相来自后端目录；章的阶段与叫法在 labels/catalog.js
   （chapterStage / CHAPTER_STATE_META，主页、成稿中心同一份）。
   tone 是 ws-ui 的语义色（Tag / data-tone）：accent | ok | warn | danger | info | neutral。
   ========================================================== */

/* 三卷。tone 给卷标签和结构镜头的卷线用，只是区分三段的颜色，不代表好坏。 */
const ARR_ACTS = [
  { id: "act1", n: "卷一", tone: "ok" },
  { id: "act2", n: "卷二", tone: "warn" },
  { id: "act3", n: "卷三", tone: "accent" },
];

/* 场景生产状态：叫法与语气色取 ws-labels 的 SCENE_STATE_META，dot 是进度点的填色 */
const ARR_SCENE_STATE = {
  done:    { ...SCENE_STATE_META.done, dot: "var(--ok)" },
  writing: { ...SCENE_STATE_META.writing, dot: "var(--accent)" },
  todo:    { ...SCENE_STATE_META.todo, dot: "var(--line-3)" },
};

export { ARR_ACTS, ARR_SCENE_STATE };
