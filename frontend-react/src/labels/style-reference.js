/* ==========================================================
   labels/style-reference — 风格参考的一张词表（2026-09-29 从 ws-labels.js 拆出）：
   16 维（与后端 card.DIMENSION_LABELS 逐字相同）、段落类型、样例窗口在章里的位置与配额、
   场面 / 情绪标签（与后端 tags.py 同一套词）、参考书作业的叫法。纯函数，不写 window。
   ========================================================== */

/* ---------- 风格参考（参考书）：一张词表 ----------
   16 维的名字与后端 services/style_reference/card.py 的 DIMENSION_LABELS 逐字相同（ws-labels.test.js 读后端源码
   比对）；场面 / 情绪标签与后端 tags.py 的 SITUATION_TAGS / MOOD_TAGS 同一套词。风格参考的各页、起草台与成稿中心
   说到这些词都从这里取，不再各写一份。 */

export const STYLE_LAYER_ORDER = ["language", "narrative", "scene", "theme"];

export const STYLE_LAYER_LABELS = {
  language: "语言",
  narrative: "叙事",
  scene: "场景",
  theme: "主题",
};

export const STYLE_DIMENSION_LABELS = {
  "language.sentence_structure": "句式结构",
  "language.vocabulary": "词汇选择",
  "language.rhetoric": "修辞手法",
  "language.punctuation": "标点节奏",
  "narrative.perspective": "叙事视角",
  "narrative.pacing": "节奏控制",
  "narrative.time_handling": "时间处理",
  "narrative.information_density": "信息密度",
  "scene.environment": "环境描写",
  "scene.character_portrayal": "人物刻画",
  "scene.dialogue": "对话写法",
  "scene.sensory_priority": "感官优先",
  "theme.emotional_tone": "情感基调",
  "theme.values": "价值取向",
  "theme.motifs": "母题意象",
  "theme.narrative_philosophy": "叙事哲学",
};

/* 16 维的固定顺序（语言 → 叙事 → 场景 → 主题，每层 4 维；与后端 SubDimension 同序） */
export const STYLE_DIMENSIONS = Object.keys(STYLE_DIMENSION_LABELS);

export function styleDimensionLabel(dimension) {
  return STYLE_DIMENSION_LABELS[dimension] || String(dimension || "");
}

export function styleLayerOf(dimension) {
  const layer = String(dimension || "").split(".", 1)[0];
  return STYLE_LAYER_LABELS[layer] ? layer : "";
}

/* 段落类型（分类作业给每一段标的类型）；unclassified 是分类作业还没轮到的段。 */
export const PARAGRAPH_TYPE_LABELS = {
  narration: "叙述",
  dialogue: "对话",
  description_env: "环境",
  psychology: "心理",
  action: "动作",
  description_char: "人物",
  transition: "转场",
  flashback: "闪回",
  unclassified: "未分类",
};

export function paragraphTypeLabel(type) {
  return PARAGRAPH_TYPE_LABELS[type] || String(type || "");
}

/* 样例窗口 / 场景在章里的位置 */
export const WINDOW_POSITION_LABELS = {
  opening: "章首",
  closing: "章末",
  whole: "整章",
  middle: "章中",
};

export function windowPositionLabel(position) {
  return WINDOW_POSITION_LABELS[position] || "";
}

/* 一场的样例窗是按哪条配额选进来的（后端 inject/selection 的 slot；本场预览与起草台「本场参考窗口」共用）。
   2026-09-24 O1：窗口标签不再有「手法」，改记这一窗最能示范的维度（16 维的键），配额随之叫「维度示范」；
   O1 之前冻结的场（style_reference_scene_windows 旧行）还带旧的 slot 名，照旧给个叫法。 */
export const STYLE_WINDOW_SLOT_LABELS = {
  position: "按章内位置挑",
  situation: "按场面挑",
  dimension: "维度示范",
  texture: "按对白 / 叙述的质地挑",
  typical: "全书典型片段",
  revise_dimension: "示范要改的维度",
  device: "维度示范",
  revise_device: "示范要改的维度",
};

export function styleWindowSlotLabel(slot) {
  return STYLE_WINDOW_SLOT_LABELS[slot] || "";
}

/* 场面 / 情绪标签（学习作业给全书窗口打的、场景蓝图给一场标的，同一套词） */
export const STYLE_SITUATION_TAGS = [
  "日常闲谈", "对峙审问", "争吵冲突", "打斗追逐", "危机应对", "独处内省", "回忆往事", "说明设定",
  "群像场面", "情感交流", "喜剧桥段", "悬疑揭示", "赶路转场", "计划商议", "开章引入", "收章落点",
];
export const STYLE_MOOD_TAGS = ["紧张", "诙谐", "伤感", "温情", "压抑", "热血", "荒诞", "悬疑", "平静", "恐惧"];

/* 参考书的耗时作业（作业表的 kind） */
const STYLE_JOB_KIND_LABELS = {
  classify: "段落分类",
  learn: "学习文风",
  check: "对照检查",
};

export function styleJobKindLabel(kind) {
  return STYLE_JOB_KIND_LABELS[kind] || "";
}
