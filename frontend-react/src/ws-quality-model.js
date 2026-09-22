import { chapterLabelById, sceneLabelById } from "./ws-labels.js";

/* ==========================================================
   文学质量的纯模型（从 ws-quality.jsx 拆出，2026-09-22）
   21 维与严重度的中文名、每维的「问题 / 改法」、文本层、巡检对象的人话名字与分数格式。
   不读 store、不碰 React：视图（ws-quality.jsx）与单测都从这里拿。
   ========================================================== */

/* 21 维中文标签（含蓝图 v2 新增三维：感知过滤 / 自我重复 / 冲突过净） */
export const QUALITY_DIMS = {
  model_voice: "模型腔",
  image_homogeneity: "意象同质",
  repetitive_action: "动作重复",
  expository_dialogue: "说明式对白",
  no_choice_scene: "无抉择场景",
  summary_ending: "概述式收尾",
  choice_pressure: "抉择压力",
  ending_drive: "收束驱动",
  template_action_reuse: "模板动作复用",
  image_field_reuse: "意象场复用",
  syntax_monotony: "句式单调",
  false_clarity: "虚假清晰",
  valid_ambiguity: "有效留白",
  painless_scene: "无痛场景",
  decorative_imagery: "装饰性意象",
  dialogue_as_report: "对白即汇报",
  over_explained_motive: "过度解释动机",
  false_poetic_closure: "伪诗意收束",
  perception_filter: "感知过滤",
  self_repetition: "自我重复",
  conflict_too_clean: "冲突过净",
};
export const QUALITY_DIM_KEYS = Object.keys(QUALITY_DIMS);
/* 后端新加的维度前端还没有中文名时，不把英文键摊给作者（原键放在 title 里） */
export const qDimLabel = (k) => QUALITY_DIMS[k] || "其他维度";

/* 每个维度发现的问题与改法（后端给的是英文句子，原文放在 title 里）。
   带具体词的问题（「反复出现的意象：手」）从英文句尾取出那个词。 */
const QUALITY_DIM_NOTES = {
  model_voice: ["可能是模型腔，或一句空泛的情绪捷径。", "把抽象的「领悟」换成具体的选择、动作或感官后果。"],
  image_homogeneity: ["同一个意象反复出现", "留一个锚定意象，其余靠动作、物件、温度、声音或空间变化换质感。"],
  repetitive_action: ["同一个动作节拍反复出现", "留下最有力的一拍，其余换成选择、物件移动、沉默或走位变化。"],
  template_action_reuse: ["动作节拍在重复同一个句子模板。", "留一拍，其余变换走位、物件、沉默或人物之间的压力。"],
  image_field_reuse: ["氛围意象承担了太多重复的工作", "让一个意象负责氛围，下一拍靠物件、决定或身体位置推进。"],
  expository_dialogue: ["对白在做解释，而不是施压或留潜台词。", "把事实挪进动作、沉默、矛盾或只答一半的回答里。"],
  dialogue_as_report: ["对白在汇报情节，没有改变人物之间的压力。", "把事实变成不肯回答的问题、指控、筹码或关系里的伤口。"],
  no_choice_scene: ["这一段在纸面上看不到明确的抉择。", "给人物两个不能兼得的选项，让其中一个看得见地付出代价。"],
  choice_pressure: ["抉择缺少看得见的压力或代价。", "用动作写出这个选择丢掉、冒险或拒绝了什么。"],
  summary_ending: ["结尾在解释效果，而不是落在动作或画面上。", "删掉概括句，停在最后一个不可逆的动作上。"],
  ending_drive: ["最后一拍没有把读者推进下一场。", "用新的动作、物件移动、到来、离开、揭露或拒绝收尾。"],
  false_poetic_closure: ["结尾用诗意的笃定收束，而不是一个硬的下一步动作。", "停在看得见的动作、交出的物件、拒绝、离开或不可逆的揭露上。"],
  decorative_imagery: ["意象更多在渲染氛围，没有推动行动、关系、信息或主题。", "只留下能改变人物行为、揭出隐瞒或加重代价的那个意象。"],
  syntax_monotony: ["连续几句用了同一种句式。", "用一个短句、一个压住不说的反应，或从后果开头的句子打断节奏。"],
  false_clarity: ["把本该由压力揭示的东西直接告诉了读者。", "删掉解释，让选择、拒绝、交出物件或沉默替读者推断。"],
  over_explained_motive: ["动机被直接解释，而不是被逼成行动或省略。", "让读者从人物拒绝、拖延、隐瞒或在压力下的选择里推断动机。"],
  painless_scene: ["结构也许清楚，但没有人疼：看不到具体的损失、背叛、风险或牺牲。", "让人物在纸面上付出代价：丢掉资源、伤一段关系、藏起什么，或背弃一个价值。"],
  perception_filter: ["叙述用「看见 / 感到」这类感知动词转述，而不是直接呈现。", "删掉感知动词，让刺激直接落成动作、物件或感官细节。"],
  self_repetition: ["有一句实质内容被逐字重复。", "事实只说一次；把重复的句子换成新的后果、反应或信息。"],
  conflict_too_clean: ["冲突解决得太干净。", "留下代价或余波。"],
};
const Q_TOKEN_DIMS = new Set(["image_homogeneity", "repetitive_action", "image_field_reuse"]);

export function qFindingText(finding) {
  const notes = QUALITY_DIM_NOTES[finding && finding.dimension];
  const english = String((finding && finding.issue) || "");
  if (!notes) return { issue: "", fix: "", english };
  let issue = notes[0];
  if (Q_TOKEN_DIMS.has(finding.dimension)) {
    const hit = /:\s*([^:]+?)\.?$/.exec(english);
    issue += hit ? `：${hit[1].trim()}。` : "。";
  }
  return { issue, fix: notes[1], english: [english, finding.recommendation].filter(Boolean).join("\n") };
}

/* 证据摘录来自作者稿（HTML），截断处可能带半个标签：只留文字 */
export function qPlainText(value) {
  return String(value || "")
    .replace(/^[a-z/]{1,6}>/i, "")
    .replace(/<[^>]*(>|$)/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/* severity 中文 + 色调（tone 是旧色板名，保留导出形状；视图换成 ws-ui 的语气） */
export const QUALITY_SEV = {
  blocking: { label: "阻断", tone: "crimson" },
  revision: { label: "修订", tone: "rose" },
  taste: { label: "审美", tone: "gold" },
  info: { label: "信息", tone: "slate" },
};
const Q_TONE = { crimson: "danger", rose: "accent", gold: "warn", slate: "info" };
export const qSevLabel = (s) => (QUALITY_SEV[s] ? QUALITY_SEV[s].label : s ? "其他" : "");
export const qSevTone = (s) => Q_TONE[QUALITY_SEV[s] ? QUALITY_SEV[s].tone : "slate"];

/* text_layer 下拉（后端另支持 runtime） */
export const QUALITY_TEXT_LAYERS = [
  { v: "author_draft_preferred", l: "作者稿优先" },
  { v: "runtime_final_scene", l: "运行末场" },
  { v: "chapter_memory_final", l: "章记忆终稿" },
  { v: "chapter_assembled", l: "整章拼装" },
];
export const QUALITY_MIN_SEVERITIES = ["blocking", "revision", "taste", "info"];
/* 巡检结果里每条的实际文本层（比筛选项多两个具体层） */
export const Q_ITEM_LAYER = {
  author_draft: "作者稿",
  author_draft_preferred: "作者稿",
  runtime_final_scene: "运行末场",
  chapter_memory_final: "章记忆终稿",
  chapter_assembled: "整章拼装",
  runtime: "运行稿",
};

/* ---- helpers ---- */
export const qPct = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Math.round(v * 100));
export const qScore = (v) => (v === null || v === undefined || Number.isNaN(v) ? "—" : `${Math.round(v * 100)} 分`);
export function qRiskDims(item) {
  const sig = (item && item.signals) || {};
  return Object.keys(sig).filter((k) => sig[k] && sig[k].risk);
}

/* 巡检对象的人话名字：第 N 章 · 章名 / 第 N 章 · 第 M 场「场题」；原始 id 放 title */
export function qObjectLabel(item, chapters) {
  if (!item) return "";
  if (item.object_type === "scene") return sceneLabelById(chapters, item.scene_id || item.object_id, { withTitle: true });
  return chapterLabelById(chapters, item.chapter_id || item.object_id);
}
