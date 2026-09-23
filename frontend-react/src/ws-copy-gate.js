/* ==========================================================
   抄袭门拦下时给作者的话（2026-09-23 风格参考 v3 · P6b）
   ----------------------------------------------------------
   后端唯一的抄袭门（reference_copy_gate）只在「与绑定的参考书连续 12 字以上相同」时拦下 AI 续写 / 局部改写 / 采用 /
   归档，回 409 SOURCE_SAFETY_BLOCKED；details.reference_copy 只有位置与计数（第几字到第几字、哈希），从不带参考原文。
   这里把位置说成中文——「第 3–15 字与参考书原文连续相同」——位置是被拦下的那段文字（AI 的这一版 / 这条改写 /
   这一稿）里的第几字。
   参考书的受保护专名（人名、地名、设定名）**从不拦**（专名表是模型认的，难免收进日常词）：成稿门把它报成一条不拦的
   警告（final_text_gate.warnings 里的 source_safety:protected_term，带命中的词），归档 / 采用 / 提升之后告诉作者一声
   （finalGateNotes）；被拦下的 409 里若同时有专名，只顺带提一句「这一项不拦」。
   写作台（续写、选区改写、提升）与起草台（采用）共用。纯函数，不写 window。
   ========================================================== */

const MAX_PLACES = 3;
const MAX_TERMS = 6;

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

/* 被拦下的地方（只有原文重合会拦）：["第 3–15 字与参考书原文连续相同"] */
export function copyGatePlaces(error) {
  const audit = (error && error.details && error.details.reference_copy) || {};
  const out = [];
  const copied = spans(audit.hits, audit.hit_count);
  if (copied) out.push(`${copied}与参考书原文连续相同`);
  else if (Number(audit.hit_count) > 0) out.push(`有 ${Number(audit.hit_count)} 处与参考书原文连续相同`);
  return out;
}

/* 一共几处（采用时只说计数：采用的稿子里可能带着排版标记，字位不一定对得上作者看到的正文）；named = 顺带查到的专名（不拦） */
export function copyGateCount(error) {
  const audit = (error && error.details && error.details.reference_copy) || {};
  const hits = Number(audit.hit_count != null ? audit.hit_count : (audit.hits || []).length) || 0;
  const named = Number(audit.protected_hit_count != null ? audit.protected_hit_count : (audit.protected_hits || []).length) || 0;
  return { hits, named };
}

/* 被拦下的这一段里同时有专名：只顺带一句（专名从不拦） */
function namedAside(error) {
  const { named } = copyGateCount(error);
  return named ? `另有 ${named} 处用了参考书里的专名——这一项不拦，定稿前可以换成自己的。` : "";
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
  const { hits } = copyGateCount(error);
  const what = hits ? `有 ${hits} 处与参考书原文连续相同` : "有与参考书原文相同的地方";
  return `这一稿里${what}，不能采用：改写这些地方（或退回重写）之后再采用。稿子已保留，写作台的正文没有改动。${namedAside(error)}`;
}

/* 写作台把草稿提升为权威正文时被拦下（草稿保留） */
export function copyGatePromoteMessage(error) {
  const places = copyGatePlaces(error);
  const where = places.length ? `：${places.join("；")}` : "里有与参考书原文相同的地方";
  return `正文${where}，不能提升为权威正文。把这几处改写成你自己的句子后再提升；草稿已安全保留，没有改动。${namedAside(error)}`;
}

/* ---- 成稿门的不拦警告（归档 / 采用 / 提升之后告诉作者一声）----
   final_text_gate.warnings 里风格参考的两条：
   · source_safety:protected_term —— 正文用了参考书的专名（带 terms、hit_count）；不拦；
   · source_safety:unavailable —— 原文重合检查这一边没做成（绑定的书已删 / 绑定解析失败）；不拦。
   其余警告（文学质量 Q3 等）有自己的去处，这里不管。 */
export const FINAL_GATE_PROTECTED_TERM = "source_safety:protected_term";
export const FINAL_GATE_SOURCE_UNAVAILABLE = "source_safety:unavailable";

function quotedTerms(terms) {
  const list = (Array.isArray(terms) ? terms : []).map((t) => String(t || "").trim()).filter(Boolean);
  if (!list.length) return "";
  const shown = list.slice(0, MAX_TERMS).map((t) => `「${t}」`).join("");
  return list.length > MAX_TERMS ? `${shown}等 ${list.length} 个词` : shown;
}

/* 一条成稿门警告 → 给作者看的中文（认不得的返回 ""） */
export function finalGateWarningText(warning) {
  const key = String((warning && warning.issue_key) || "");
  if (key === FINAL_GATE_PROTECTED_TERM) {
    const terms = quotedTerms(warning.terms);
    const count = Number(warning.hit_count) || 0;
    const what = terms ? `用了参考书里的专名${terms}${count ? `（共 ${count} 处）` : ""}` : "用了参考书里的专名";
    return `正文${what}。这一项不拦归档；是参考书里的人名、地名或设定名的话，建议换成你自己的——只是日常用词被误收进专名表的，可以到文风画像的禁用词里删掉它。`;
  }
  if (key === FINAL_GATE_SOURCE_UNAVAILABLE) {
    const missing = Array.isArray(warning.missing_books) && warning.missing_books.length;
    return missing
      ? "绑定的参考书已不在书库里，这一次没能做原文重合检查；正文照常归档。"
      : "这一场的参考书绑定没能解析，这一次没能做原文重合检查；正文照常归档。";
  }
  return "";
}

/* 成稿门结果（adopt / promote 回包里的 validation.final_text_gate，或 final_text_gate 本身）→ 要告诉作者的几句话 */
export function finalGateNotes(gateOrResponse) {
  const src = gateOrResponse && typeof gateOrResponse === "object" ? gateOrResponse : {};
  const gate = (src.validation && src.validation.final_text_gate) || src.final_text_gate || src;
  const warnings = Array.isArray(gate && gate.warnings) ? gate.warnings : [];
  return warnings.map(finalGateWarningText).filter(Boolean);
}
