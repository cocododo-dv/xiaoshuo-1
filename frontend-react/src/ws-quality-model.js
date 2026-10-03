import { chapterLabelById, sceneLabelById } from "./labels/catalog.js";
import {
  FINDING_SEVERITY_ORDER, RULE_DIMENSION_LABELS, ruleDimensionLabel,
} from "./labels/finding.js";

/* ==========================================================
   文学质量的纯模型（从 ws-quality.jsx 拆出，2026-09-22）
   规则维度与严重度的中文名（在 labels/finding.js，成稿中心的诊断页签与写作台深改抽屉读同一份）、
   文本层、巡检对象的人话名字与分数格式、筛选项。一条发现怎么显示在 ws-finding-ui.jsx（与成稿中心共用）。
   不读 store、不碰 React：视图（ws-quality.jsx）与单测都从这里拿。
   ========================================================== */

/* 规则维度的中文名（旧名照旧转出：单测与视图从这里拿） */
export const QUALITY_DIMS = RULE_DIMENSION_LABELS;
export const QUALITY_DIM_KEYS = Object.keys(QUALITY_DIMS);
/* 后端新加的维度前端还没有中文名时，不把英文键摊给作者 */
export const qDimLabel = ruleDimensionLabel;

/* 「风险维度」筛选项：巡检回包里带着服务端的维度表（dimensions: [{ dimension, label }]）就用它，
   还没巡检过 / 旧回包没有时用本地这一份（与服务端逐字相同）。 */
export function qDimensionOptions(serverDimensions) {
  const list = (Array.isArray(serverDimensions) ? serverDimensions : []).filter((d) => d && d.dimension);
  if (list.length) return list.map((d) => ({ key: d.dimension, label: d.label || ruleDimensionLabel(d.dimension) }));
  return QUALITY_DIM_KEYS.map((key) => ({ key, label: ruleDimensionLabel(key) }));
}

/* text_layer 下拉（后端另支持 runtime）。「章记忆终稿」一层已删（2026-10-03 作者的决定：读时现拼之后它与
   「整章拼装」几乎一样，后端对它回 400） */
export const QUALITY_TEXT_LAYERS = [
  { v: "author_draft_preferred", l: "作者稿优先" },
  { v: "runtime_final_scene", l: "运行末场" },
  { v: "chapter_assembled", l: "整章拼装" },
];
export const QUALITY_MIN_SEVERITIES = FINDING_SEVERITY_ORDER;
/* 巡检结果里每条的实际文本层（比筛选项多两个具体层） */
export const Q_ITEM_LAYER = {
  author_draft: "作者稿",
  author_draft_preferred: "作者稿",
  runtime_final_scene: "运行末场",
  chapter_assembled: "整章拼装",
  runtime: "运行稿",
};

/* ---- helpers ---- */
export const qPct = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Math.round(v * 100));
export const qScore = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : `${Math.round(v * 100)} 分`);
/* 一项的风险维度：后端给了 open_dimensions（去掉作者在写作台忽略过的发现之后还开着的维度）就用它，
   旧载荷回落到 signals 里 risk 为真的维度 */
export function qRiskDims(item) {
  if (item && Array.isArray(item.open_dimensions)) return item.open_dimensions.filter(Boolean);
  const sig = (item && item.signals) || {};
  return Object.keys(sig).filter((k) => sig[k] && sig[k].risk);
}

/* 巡检对象的人话名字：第 N 章 · 章名 / 第 N 章 · 第 M 场「场题」；原始 id 放 title */
export function qObjectLabel(item, chapters) {
  if (!item) return "";
  if (item.object_type === "scene") return sceneLabelById(chapters, item.scene_id || item.object_id, { withTitle: true });
  return chapterLabelById(chapters, item.chapter_id || item.object_id);
}
