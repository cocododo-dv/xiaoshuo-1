/* ==========================================================
   写作台 AI 面的共用小件（2026-09-21）
   ----------------------------------------------------------
   · wrAiError(error)：把续写 / 内联改写的失败翻成作者能照做的一句话。
     过去托盘、抽屉、内联弹层各写一份，而且看的是 message 文本：没配模型时说
     「生成失败，请稍后重试」（让人白等），断网时反倒说「请去配置模型」。
     现在只看 ApiRequestError 的 code / status / details。
   · wrContinueDirection(proposal, index)：后端一次生成三条续写，分别按
     动作推进 / 关系压力 / 悬念 三个方向（author_drafts.CONTINUATION_VARIANT_DIRECTIONS，
     proposal_source 的后缀就是方向），候选卡片据此命名，不再叫「候选 1/2/3」。
   · wrContinueCandidates(generated)：generate-set 的响应 → 候选卡片数据。
   · WR_RW_ACTIONS / wrToneInstr：选区工具条的改写指令。
   纯函数模块，不读 store、不写 window；真正发请求的在 ws-writer-requests.js。
   ========================================================== */
import { copyGateGenerationMessage, isCopyGateError } from "./ws-copy-gate.js";

/* kind: copy（AI 写出来的每一版都照搬了参考书，被抄袭门拦下丢掉）| config（去系统设置）| not-ready（场景还没同步好）
        | empty（模型没给出可用结果）| unclear（服务器没说清原因：重试，也给去系统设置）| retry
   offersSettings：提示里要不要同时给「去系统设置」（config 与 unclear 为 true）。
   抄袭门的拒绝也带 author_action（「去改写这些位置」），所以要先认它：过去它被当成「没有可用的模型」。 */
export function wrAiError(error) {
  const code = String((error && error.code) || "");
  const details = (error && error.details) || {};
  const status = Number(error && error.status) || 0;
  const nextAction = String(details.next_action || "");
  if (isCopyGateError(error)) {
    return { kind: "copy", message: copyGateGenerationMessage(error), actionLabel: "再试一次" };
  }
  if (
    /_LLM_NOT_CONFIGURED$|^LLM_(NOT_CONFIGURED|DISABLED|REQUIRED)/.test(code)
    || details.author_action
    || /^configure_/.test(nextAction)
    || code === "no-model"
  ) {
    return {
      kind: "config",
      message: "这里要用到 AI 模型，但现在没有可用的模型。到系统设置里配置并启用一个模型，再回来重试。",
      actionLabel: "去系统设置",
      offersSettings: true,
    };
  }
  if (code === "no-scene" || code === "no-draft" || code === "AUTHOR_DRAFT_UNAVAILABLE" || code === "SCENE_NOT_READY") {
    return {
      kind: "not-ready",
      message: "这一场还没有同步到服务器，稍等几秒再试。",
      actionLabel: "重试",
    };
  }
  if (code === "no-result") {
    return {
      kind: "empty",
      message: "模型这次没有给出可用的结果。换个说法，或者稍后再试。",
      actionLabel: "重试",
    };
  }
  if (code === "NETWORK_ERROR" || code === "REQUEST_TIMEOUT") {
    return { kind: "retry", message: "连不上服务器。检查网络或后端是否在运行，然后重试。", actionLabel: "重试" };
  }
  /* 服务器兜底的「内部错误、不可重试」：说不清原因。选区改写接口在没有可用模型时就落在这里
     （它没把模型执行失败翻成带 next_action 的业务错误），所以两条路都给，不替作者猜。 */
  if (code === "INTERNAL_ERROR" && details.retryable === false) {
    return {
      kind: "unclear",
      message: "服务器没能完成这次请求，已写的正文不受影响。常见原因是还没有可用的 AI 模型：先到系统设置里看一眼；设置没问题就稍后重试。",
      actionLabel: "重试",
      offersSettings: true,
    };
  }
  if (status >= 500 || (error && error.retryable)) {
    return { kind: "retry", message: "服务器这次没有处理成功，已写的正文不受影响。稍后重试。", actionLabel: "重试" };
  }
  return { kind: "retry", message: "这次没有生成成功，已写的正文不受影响。可以再试一次。", actionLabel: "重试" };
}

/* 本地构造的失败（没有场景 / 没有草稿 / 空结果）也走同一个映射 */
export function wrAiLocalError(code) {
  return Object.assign(new Error(code), { code });
}

const CONTINUE_DIRECTIONS = [
  { key: "action", label: "动作推进", tone: "crimson" },
  { key: "relationship", label: "关系压力", tone: "slate" },
  { key: "suspense", label: "悬念", tone: "gold" },
];

export function wrContinueDirection(proposal, index) {
  const source = String((proposal && proposal.proposal_source) || "");
  const slot = source.includes(":") ? source.slice(source.lastIndexOf(":") + 1) : "";
  return CONTINUE_DIRECTIONS.find((item) => item.key === slot)
    || CONTINUE_DIRECTIONS[index]
    || { key: "", label: `第 ${index + 1} 条`, tone: "slate" };
}

/* 续写提示的快捷词：只放「接着往下写」一类的指令（续写接口只追加下一拍，从不改已有正文）；
   有设计卡时按本场的拍子给「推进到『冲突』」这样的方向。改已有文字的动作在选中文字后的工具条里。 */
export function wrContinueChips(design) {
  const chips = [{ label: "续写下一段", prompt: "续写下一段，自然承接当前正文" }];
  const beats = (design && Array.isArray(design.beats)) ? design.beats : [];
  beats.filter((beat) => beat && beat.text).forEach((beat) => {
    const text = beat.text.length > 60 ? beat.text.slice(0, 60) + "…" : beat.text;
    chips.push({ label: `推进到「${beat.label}」`, prompt: `续写下一段，把情节推进到本场设计里的「${beat.label}」：${text}` });
  });
  chips.push(
    { label: "下一段放慢", prompt: "续写下一段，放慢节奏，多写可感的细节" },
    { label: "下一段用对话推进", prompt: "续写下一段，主要用人物对话推进" },
  );
  return chips;
}

function escapeHTML(text) {
  return String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function tidy(text) {
  return String(text || "").replace(/\s*\n\s*/g, "").trim();
}

/* 离线兜底产物是确定性占位文字——按「模型不可用」如实处理，不混进候选里 */
export function wrIsOfflinePlaceholder(rationale) {
  return /offline deterministic/i.test(String(rationale || ""));
}

/* generate-set 的响应 → 候选卡片 { id, approach, tone, note, html }。
   一条可用的都没有时抛本地错误（全是离线占位 → no-model，否则 no-result），由 wrAiError 翻译。 */
export function wrContinueCandidates(generated) {
  const cands = [];
  let offline = false;
  const proposals = Array.isArray(generated && generated.proposals) ? generated.proposals : [];
  proposals.forEach((proposal, index) => {
    const text = tidy(proposal && proposal.content);
    if (!text) return;
    if (wrIsOfflinePlaceholder(proposal && proposal.rationale)) { offline = true; return; }
    const direction = wrContinueDirection(proposal, index);
    cands.push({
      id: (proposal && proposal.proposal_id) || ("cand" + cands.length),
      approach: direction.label,
      tone: direction.tone,
      note: (proposal && proposal.rationale) || "",
      html: escapeHTML(text),
    });
  });
  if (!cands.length) throw wrAiLocalError(offline ? "no-model" : "no-result");
  return cands;
}

/* 选区工具条的四个快捷改写；「调音」按三根滑杆拼一句指令 */
export const WR_RW_ACTIONS = [
  { id: "polish", label: "润色", instr: "在不改变原意与人称的前提下润色这段文字，使其更精炼、更有文学质感" },
  { id: "shorter", label: "更凝练", instr: "把这段文字改写得更凝练简短，删去冗余与可省的修饰，保留关键意象" },
  { id: "concrete", label: "更具象", instr: "把这段文字改写得更具象可感，增加克制的细节与动作，避免空泛与抽象" },
  { id: "dialogue", label: "对话化", instr: "把这段叙述改写为以对话推进的形式，符合人物口吻，保留必要的动作提示" },
];

export function wrToneInstr(tone) {
  const lean = (value, lo, hi) => (value <= 22 ? "明显更" + lo : value <= 42 ? "略" + lo : value >= 78 ? "明显更" + hi : value >= 58 ? "略" + hi : null);
  const parts = [
    lean(tone.warm, "冷峻克制", "温情柔软"),
    lean(tone.expand, "凝练简短", "铺陈细腻"),
    lean(tone.direct, "含蓄留白", "直白有力"),
  ].filter(Boolean);
  if (!parts.length) return "在保持原意与人称的前提下，做一次自然的文学性润色";
  return "调整文字的语气，使其" + parts.join("、") + "；保持原意与人称不变";
}
