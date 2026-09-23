/* ==========================================================
   抄袭门拦下时给作者的话（2026-09-23 风格参考 v3 · P6b）
   ----------------------------------------------------------
   后端唯一的抄袭门（reference_copy_gate：与绑定的参考书连续 12 字以上相同，或用了参考书的受保护专名）拦下
   AI 续写 / 局部改写 / 采用时回 409 SOURCE_SAFETY_BLOCKED；details.reference_copy 只有位置与计数
   （第几字到第几字、哈希），从不带参考原文。这里把位置说成中文——「第 3–15 字与参考书原文连续相同」
   「第 20–22 字用了参考书里的专名」——位置是被拦下的那段文字（AI 的这一版 / 这条改写）里的第几字。
   写作台（续写、选区改写）与起草台（采用）共用。纯函数，不写 window。
   ========================================================== */

const MAX_PLACES = 3;

export function isCopyGateError(error) {
  const code = String((error && error.code) || "");
  const details = (error && error.details) || {};
  return code === "SOURCE_SAFETY_BLOCKED" && !!(details.reference_copy && typeof details.reference_copy === "object");
}

function spans(items, total) {
  const list = (Array.isArray(items) ? items : []).filter((item) => item && Number.isInteger(item.start) && Number.isInteger(item.end) && item.end > item.start);
  if (!list.length) return "";
  const shown = list.slice(0, MAX_PLACES).map((item) => `第 ${item.start + 1}–${item.end} 字`).join("、");
  const count = Math.max(Number(total) || 0, list.length);
  return count > Math.min(list.length, MAX_PLACES) ? `${shown}等 ${count} 处` : shown;
}

/* 被拦下的地方：["第 3–15 字与参考书原文连续相同", "第 20–22 字用了参考书里的专名（人名、地名等）"] */
export function copyGatePlaces(error) {
  const audit = (error && error.details && error.details.reference_copy) || {};
  const out = [];
  const copied = spans(audit.hits, audit.hit_count);
  if (copied) out.push(`${copied}与参考书原文连续相同`);
  else if (Number(audit.hit_count) > 0) out.push(`有 ${Number(audit.hit_count)} 处与参考书原文连续相同`);
  const named = spans(audit.protected_hits, audit.protected_hit_count);
  if (named) out.push(`${named}用了参考书里的专名（人名、地名等）`);
  else if (Number(audit.protected_hit_count) > 0) out.push(`有 ${Number(audit.protected_hit_count)} 处用了参考书里的专名（人名、地名等）`);
  return out;
}

/* 一共几处（采用时只说计数：采用的稿子里可能带着排版标记，字位不一定对得上作者看到的正文） */
export function copyGateCount(error) {
  const audit = (error && error.details && error.details.reference_copy) || {};
  const hits = Number(audit.hit_count != null ? audit.hit_count : (audit.hits || []).length) || 0;
  const named = Number(audit.protected_hit_count != null ? audit.protected_hit_count : (audit.protected_hits || []).length) || 0;
  return { hits, named };
}

/* AI 续写 / 选区改写在生成时就被拦下（每一版都照搬了参考书，已丢掉，正文没动） */
export function copyGateGenerationMessage(error, { what = "AI 给的几版" } = {}) {
  const places = copyGatePlaces(error);
  const where = places.length ? `（其中一版的${places.join("；")}）` : "";
  return `${what}都照搬了参考书原文${where}，已经丢掉，正文没有改动。换个说法再试一次，或者自己写这一段。`;
}

/* 选区改写已经替换进正文，但采纳时被拦下：说出那一句里的第几字，请作者改成自己的话 */
export function copyGateAcceptMessage(error) {
  const places = copyGatePlaces(error);
  const where = places.length ? `：${places.join("；")}` : "：里面有与参考书原文相同的地方";
  return `刚替换进正文的那句改写${where}。把这几处改成你自己的说法——这次替换没有记成采纳，定稿时同样会被拦下。`;
}

/* 起草台采用一稿时被拦下（稿子保留，写作台的正文没动） */
export function copyGateAdoptMessage(error) {
  const { hits, named } = copyGateCount(error);
  const parts = [];
  if (hits) parts.push(`${hits} 处与参考书原文连续相同`);
  if (named) parts.push(`${named} 处用了参考书里的专名`);
  const what = parts.length ? `有 ${parts.join("、")}` : "有与参考书原文相同的地方";
  return `这一稿里${what}，不能采用：改写这些地方（或退回重写）之后再采用。稿子已保留，写作台的正文没有改动。`;
}
