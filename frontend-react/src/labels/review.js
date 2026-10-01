/* ==========================================================
   labels/review — 待办来源的中文叫法（2026-09-29 从 ws-labels.js 拆出）。纯函数，不写 window。
   写作偏好学习整条链已删除（批准 #6，重评 R5）：它的来源名与倾向词表一并删掉，旧表的偏好行后端不再列出。
   ========================================================== */

/* ---------- 待办 ---------- */

/* 待办卡的 source 是后端记下的来源；旧表行直接用 item_type（英文键）。 */
const REVIEW_SOURCE_LABELS = {
  fe_card: "工作台",
};

export function reviewSourceLabel(source) {
  const key = String(source || "").trim();
  if (!key) return "";
  if (REVIEW_SOURCE_LABELS[key]) return REVIEW_SOURCE_LABELS[key];
  // 认不出的英文键不直接甩给作者
  return /^[a-z0-9_]+$/.test(key) ? "系统" : key;
}
