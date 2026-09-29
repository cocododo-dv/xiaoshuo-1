/* ==========================================================
   labels/review — 待办来源与写作偏好的中文叫法（2026-09-29 从 ws-labels.js 拆出）。纯函数，不写 window。
   ========================================================== */

/* ---------- 待办 ---------- */

/* 待办卡的 source 是后端记下的来源；旧表行直接用 item_type（英文键）。 */
const REVIEW_SOURCE_LABELS = {
  author_preference_profile: "写作偏好",
  fe_card: "工作台",
};

export function reviewSourceLabel(source) {
  const key = String(source || "").trim();
  if (!key) return "";
  if (REVIEW_SOURCE_LABELS[key]) return REVIEW_SOURCE_LABELS[key];
  // 认不出的英文键不直接甩给作者
  return /^[a-z0-9_]+$/.test(key) ? "系统" : key;
}

/* 系统从作者手改 / 驳回提案里记下的写作倾向（后端 author_drafts 的 safe_preference_hints）。 */
const PREFERENCE_HINT_LABELS = {
  prefer_expansion: "偏好扩写",
  prefer_concise: "偏好精简",
  prefer_more_dialogue: "偏好多写对白",
  prefer_less_dialogue: "偏好少写对白",
  prefer_longer_paragraphs: "偏好更长的段落",
  prefer_shorter_paragraphs: "偏好更短的段落",
  prefer_longer_sentences: "偏好更长的句子",
  prefer_shorter_sentences: "偏好更短的句子",
  prefer_voice: "看重人物的声音",
  prefer_structure: "看重结构与铺垫回收",
  avoid_exposition: "少交代、少解释",
  avoid_dialogue_style: "不喜欢那种对白写法",
  avoid_tone: "不喜欢那种腔调",
  avoid_pacing: "在意节奏",
  other_safe_note: "其他备注",
};

export function preferenceHintLabel(key) {
  return PREFERENCE_HINT_LABELS[key] || "";
}
