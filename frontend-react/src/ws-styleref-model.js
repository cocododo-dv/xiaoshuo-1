/* ==========================================================
   风格参考 · 说法与纯派生（2026-09-23 v3 重建）
   三步：参考书 → 学习文风 → 用于作品。这里放各页共用的说法与判断：
   · 参考方式 / 起草方式 / 维度状态 / 导入时的三档原文范围——每个选项一句真话（它会怎样改变起草时带上的东西）；
   · 一本书现在走到哪一步（书库徽标、页头、步骤条、落点）；
   · 作者读得懂的出错说法与下一步（按错误码 + 后端的 author_action，不给作者看英文原话）；
   · 估算、耗时、百分比的格式；活动条目的文案；界面偏好 ws_sr_ui_v1。
   词表（16 维、段落类型、章内位置、场面 / 情绪标签、作业叫法）在 ws-labels.js，这里只引用。
   不依赖 React，不写 window，可单测。
   ========================================================== */
import { isChineseMessage } from "./lib/messages.js";
import {
  STYLE_DIMENSIONS,
  STYLE_LAYER_LABELS,
  STYLE_LAYER_ORDER,
  styleJobKindLabel,
  styleLayerOf,
} from "./ws-labels.js";

/* ---------- 用于作品：绑定配置的四个旋钮 ---------- */

export const SR_SAMPLE_WINDOWS_MIN = 0;
export const SR_SAMPLE_WINDOWS_MAX = 16;
export const SR_SAMPLE_WINDOWS_DEFAULT = 12;

/* 参考方式（reference_mode）：三选一，每个一句真话 */
export const SR_REFERENCE_MODES = [
  {
    id: "full",
    label: "全面模仿",
    badge: "推荐",
    detail: "起草时带上这本书的原文样例窗（按这一场挑）、文风卡和声音习惯。",
  },
  {
    id: "samples_only",
    label: "只用原文样例",
    badge: "对照",
    detail: "只带原文样例窗，不带文风卡和声音习惯——用来对照文风卡帮上了多少。",
  },
  {
    id: "card_only",
    label: "只用文风卡",
    badge: "不发原文",
    detail: "只带文风卡和声音习惯，不发原文段落（卡上的例子至多 11 个字，不含本书专名）。",
  },
];

/* 起草方式（draft_mode） */
export const SR_DRAFT_MODES = [
  { id: "style_first", label: "作者手笔直起", badge: "推荐", detail: "首稿直接照这位作者的写法起草。" },
  { id: "neutral_first", label: "先中性后润色", badge: "对照", detail: "先写一版中性的首稿，再改成这位作者的写法——用来对照直起的效果。" },
];

/* 维度状态（dimension_states）：给当前作品设的，写在绑定上 */
export const SR_DIMENSION_STATES = [
  { id: "emphasize", label: "重点", detail: "排在文风卡最前、多带一句，挑样例时优先挑示范这一维手法的片段" },
  { id: "normal", label: "正常", detail: "按辨识度带上" },
  { id: "exclude", label: "不学", detail: "文风卡里不带这一维" },
];

export function srDefaultConfig() {
  const states = {};
  STYLE_DIMENSIONS.forEach((dim) => { states[dim] = "normal"; });
  return { reference_mode: "full", sample_windows: SR_SAMPLE_WINDOWS_DEFAULT, dimension_states: states, draft_mode: "style_first" };
}

/* 任意（可能缺键）的配置 → 四键齐全的 v3 配置（与后端 normalize_binding_config 同一口径） */
export function srNormalizeConfig(raw) {
  const base = srDefaultConfig();
  const config = raw && typeof raw === "object" ? raw : {};
  const mode = SR_REFERENCE_MODES.some((m) => m.id === config.reference_mode) ? config.reference_mode : base.reference_mode;
  let windows = Number(config.sample_windows);
  if (config.sample_windows == null || !Number.isFinite(windows)) windows = base.sample_windows;
  windows = Math.max(SR_SAMPLE_WINDOWS_MIN, Math.min(SR_SAMPLE_WINDOWS_MAX, Math.round(windows)));
  const draft = SR_DRAFT_MODES.some((m) => m.id === config.draft_mode) ? config.draft_mode : base.draft_mode;
  const states = { ...base.dimension_states };
  const rawStates = config.dimension_states && typeof config.dimension_states === "object" ? config.dimension_states : {};
  for (const [dim, state] of Object.entries(rawStates)) {
    if (dim in states && SR_DIMENSION_STATES.some((s) => s.id === state)) states[dim] = state;
  }
  return { reference_mode: mode, sample_windows: windows, dimension_states: states, draft_mode: draft };
}

/* 只比三个顶层旋钮（用于作品页的表单；维度状态在文风画像页单独写） */
export function srSettingsEqual(a, b) {
  const x = srNormalizeConfig(a);
  const y = srNormalizeConfig(b);
  return x.reference_mode === y.reference_mode && x.sample_windows === y.sample_windows && x.draft_mode === y.draft_mode;
}

/* 维度状态的一句汇总：「2 维重点 · 1 维不学」；全是正常时「16 维都按正常学」 */
export function srDimensionStatesSummary(states) {
  const values = Object.values(srNormalizeConfig({ dimension_states: states }).dimension_states);
  const emphasized = values.filter((s) => s === "emphasize").length;
  const excluded = values.filter((s) => s === "exclude").length;
  if (!emphasized && !excluded) return "16 维都按正常学";
  return [emphasized ? `${emphasized} 维重点` : null, excluded ? `${excluded} 维不学` : null].filter(Boolean).join(" · ");
}

export function srReferenceModeMeta(id) {
  return SR_REFERENCE_MODES.find((m) => m.id === id) || SR_REFERENCE_MODES[0];
}

export function srDraftModeMeta(id) {
  return SR_DRAFT_MODES.find((m) => m.id === id) || SR_DRAFT_MODES[0];
}

/* 书的原文范围压过绑定的参考方式（与后端 binding_config.effective_reference_mode 同一口径）：
   「起草不发原文」（segments_only）的书起草时只用文风卡，不管绑定里设的是哪一种 */
export function srEffectiveReferenceMode(mode, cloudPolicy) {
  const m = SR_REFERENCE_MODES.some((x) => x.id === mode) ? mode : "full";
  return cloudPolicy === "segments_only" && m !== "card_only" ? "card_only" : m;
}

/* 一条绑定的一句话：「全面模仿 · 12 窗 · 作者手笔直起」（只用文风卡时不说窗数）。
   effectiveMode：真正生效的参考方式（书的原文范围压过设置时，按它说并注明，不把没在用的窗数说成在用） */
export function srConfigSummary(config, { effectiveMode = null } = {}) {
  const c = srNormalizeConfig(config);
  const mode = SR_REFERENCE_MODES.some((m) => m.id === effectiveMode) ? effectiveMode : c.reference_mode;
  const parts = [`${srReferenceModeMeta(mode).label}${mode !== c.reference_mode ? "（这本书起草不发原文）" : ""}`];
  if (mode !== "card_only") parts.push(`${c.sample_windows} 窗`);
  parts.push(srDraftModeMeta(c.draft_mode).label);
  return parts.join(" · ");
}

/* 一条绑定（后端 binding_payload）的一句话：按后端给的 effective_reference_mode 说；没有（界面先写的乐观值）
   就按书的原文范围推 */
export function srBindingSummary(binding, { cloudPolicy = null } = {}) {
  if (!binding) return "";
  const config = srNormalizeConfig(binding.config);
  const effective = binding.effective_reference_mode || srEffectiveReferenceMode(config.reference_mode, cloudPolicy);
  return srConfigSummary(config, { effectiveMode: effective });
}

/* 这条绑定是不是这部作品自己的：作品层、目标就是这部作品。旧版的全局绑定（scope=global）对所有没有自己应用的
   作品生效，不算哪一部的——在某一部作品的页面上改它 / 解除它会波及全部作品，只能只读 */
export function srBindingOwnedByWork(binding, workId) {
  return !!binding && !!workId && binding.scope === "project" && binding.scope_ref_id === workId;
}

export function srIsLegacyGlobalBinding(binding) {
  return !!binding && binding.scope === "global";
}

/* ---------- 导入：原文能发到哪里（书的 cloud_policy） ---------- */

/* 每一档说的都是后端真做到的事（docs/style-reference.md §11）：
   · 仅本机：按「接收这份提示的节点」判——走云端的节点拿不到这本书的任何东西（样例、文风卡、声音、专名表都不送），
     起草节点会 409 停下，不降级成没有参考的提示去照样调用；
   · 中间一档（旧 id segments_only）：分类、学习要把整段正文分批发给云端（这是它和「仅本机」的区别，所以名字不能叫
     「只发短句」）；只有起草、改稿、评审这些用参考的提示被压成只用文风卡：卡上的例子至多 11 个字、不含本书专名，
     红线里的专名清单照带（让模型避开这些名字）；
   · 可发送全文：起草时也可以带原文样例窗。 */
export const SR_CLOUD_POLICIES = [
  {
    id: "local_only",
    label: "仅本机模型",
    detail: "正文和从它学出来的文风卡、声音习惯、本书专名都只交给本机模型（如 Ollama）：分类、学习、起草、对照检查都要走本机模型；哪一步用的是云端模型，那一步就拿不到这本书的任何内容（起草会停下来提示你换成本机模型）。",
  },
  {
    id: "segments_only",
    label: "起草不发原文",
    hint: "分类和学习时正文照样发给云端",
    detail: "分类和学习时，云端模型会分批读整段的正文；之后起草、改稿和评审只送文风卡、声音习惯和防照搬红线（卡上的例子至多 11 个字、不含本书专名；红线列出本书专名让模型避开），不送原文段落。",
  },
  {
    id: "allow_full_cloud",
    label: "可发送全文",
    detail: "分类、学习和起草都可以把原文段落发给已配置的云端模型；起草时可以带原文样例窗，最像。",
  },
];

export function srCloudPolicyMeta(id) {
  return SR_CLOUD_POLICIES.find((p) => p.id === id) || null;
}

/* 界面各处提到这一档时用同一个名字（「用于作品」的说明、本场预览的提示） */
export const SR_SEGMENTS_ONLY_LABEL = srCloudPolicyMeta("segments_only").label;

/* 导入权属声明（后端 ingest 的 rights_declaration）：分析权必勾；非「仅本机」还要发送权 */
export const SR_RIGHTS_TERMS = {
  analysis: "我确认拥有对这本书做文风分析的权利，只用来学习写法，不复刻原文、人物或桥段。",
  send: "我确认有权把这本书的正文按所选范围发送给已配置的云端模型。",
};

export function srPolicyNeedsSendRights(cloudPolicy) {
  return cloudPolicy !== "local_only";
}

export function srRightsReady(cloudPolicy, rights) {
  if (!rights || rights.analysis_rights !== true) return false;
  return !srPolicyNeedsSendRights(cloudPolicy) || rights.send_rights === true;
}

/* ---------- 出错：作者读得懂的一句话 + 下一步 ---------- */

const SR_ERROR_TEXT = {
  STYLE_REFERENCE_LLM_REQUIRED: "这一步要用模型，但还没有接入可用的模型。",
  STYLE_REFERENCE_CLOUD_POLICY_BLOCKED: "这本书设为「仅本机模型」，但这一步用的是云端模型：换成本机模型，或用别的范围重新导入。",
  STYLE_REFERENCE_CLOUD_POLICY_INVALID: "这本书的原文范围设置不对，不能交给模型：重新导入并选一档范围。",
  STYLE_REFERENCE_SEND_RIGHTS_REQUIRED: "这本书没有确认发送权，不能发给云端模型：重新导入并勾选发送权。",
  STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED: "发给云端模型之前要先确认发送权。",
  STYLE_REFERENCE_BOOK_DUPLICATE: "书库里已经有同一份文本。",
  STYLE_REFERENCE_BOOK_EMPTY: "这个文件里没有可以当参考的正文。",
  STYLE_REFERENCE_UPLOAD_TOO_LARGE: "文件太大了（上限 10 MB）。",
  STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED: "只能导入纯文本（.txt）或 Markdown（.md）。",
  STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID: "权属声明没有填对，请重新勾选后再导入。",
  STYLE_REFERENCE_BOOK_NOT_FOUND: "这本书已经不在书库里了。",
  STYLE_REFERENCE_BOOK_NOT_READY: "这本书的段落分类还没完成：等它完成（或「继续分类」）之后再学。",
  STYLE_REFERENCE_BOOK_CLASSIFYING: "这本书正在重新分类段落：等它完成再学习文风。",
  STYLE_REFERENCE_BOOK_LEARNING: "这本书正在学习文风：等学完再重新分类。",
  STYLE_REFERENCE_LEARN_ALREADY_ACTIVE: "这本书已经在学习文风了。",
  STYLE_REFERENCE_LEARN_NOTHING_TO_RESUME: "没有中断的学习可以继续，直接「学习文风」即可。",
  STYLE_REFERENCE_LEARN_NOT_ACTIVE: "这本书现在没有在学习。",
  STYLE_REFERENCE_LEARN_CONFIG_MISSING: "学习文风用到的模型节点还没配好（或提示词是旧版本）。",
  STYLE_REFERENCE_INPUT_TOO_SMALL: "这本书的正文太少，学不出可靠的文风。",
  STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE: "这本书已经在分类段落了。",
  STYLE_REFERENCE_CLASSIFICATION_NOT_ACTIVE: "这本书现在没有在分类。",
  STYLE_REFERENCE_CLASSIFICATION_NOTHING_TO_RESUME: "没有中断的分类可以继续。",
  STYLE_REFERENCE_PROFILE_NOT_FOUND: "这份文风画像已经不在了。",
  STYLE_REFERENCE_PROFILE_HAS_NO_CARD: "这份画像还没有文风卡：先学习文风。",
  STYLE_REFERENCE_PROFILE_STALE: "这份画像的依据变过了：先重新学习文风，再用于作品。",
  STYLE_REFERENCE_PROFILE_ARCHIVED: "这份画像已经归档，不能再用于作品。",
  STYLE_REFERENCE_CARD_LINE_NOT_FOUND: "这一句已经不在文风卡上了（可能刚重新学过）。",
  STYLE_REFERENCE_APPLY_TARGET_NOT_FOUND: "要用这本书的作品已经不在了。",
  STYLE_REFERENCE_BINDING_NOT_FOUND: "这条应用已经解除了。",
  STYLE_REFERENCE_PROJECT_NOT_FOUND: "当前作品已经不在了。",
  STYLE_REFERENCE_APPLY_PARAM_INVALID: "用于作品的设置不对，请刷新页面后重试。",
  STYLE_REFERENCE_BOOK_ENCODING_UNSUPPORTED: "读不出这个文件的文字编码：另存为 UTF-8 编码的文本再导入。",
  STYLE_REFERENCE_BANNED_TERM_INVALID: "禁用词不能是空的。",
  STYLE_REFERENCE_BANNED_TERM_NOT_FOUND: "这个词已经删掉了。",
  STYLE_REFERENCE_BANNED_TERM_PROTECTED: "预置的禁用词不能删。",
  STYLE_REFERENCE_PARAGRAPH_RANGE_INVALID: "要读的段落范围不对。",
  /* 作业的失败与收尾（活动面板、学习 / 分类卡片上的「没有完成：……」） */
  STYLE_REFERENCE_JOB_FAILED: "后台作业出了意外停下了，请稍后重试。",
  STYLE_REFERENCE_JOB_CANCELLED: "已经取消了。",
  STYLE_REFERENCE_IMPORT_CANCELLED: "已经取消了。",
  STYLE_REFERENCE_JOB_NOT_FOUND: "这项作业已经不在了。",
  STYLE_REFERENCE_JOB_NOT_RESUMABLE: "这项作业不能接着做了，重新开始即可。",
  STYLE_REFERENCE_JOB_ALREADY_ACTIVE: "这本书已经有一项作业在跑了，等它结束再试。",
  STYLE_REFERENCE_CLASSIFICATION_FAILED: "有一批段落模型重试后仍没分出类型：检查模型接入后「继续分类」，只补没分出来的段。",
  STYLE_REFERENCE_CLASSIFY_INCOMPLETE: "还有段落没分出类型：「继续分类」只补剩下的段。",
  STYLE_REFERENCE_CLASSIFY_LLM_CALL_FAILED: "分类时模型调用失败：检查模型接入后「继续分类」。",
  STYLE_REFERENCE_CLASSIFY_OUTPUT_MISMATCH: "模型的分类结果和段落对不上：「继续分类」只重发没分出来的段。",
  STYLE_REFERENCE_CLASSIFY_ROUTE_MISSING: "段落分类用的模型节点还没配好：去系统配置补齐后再「继续分类」。",
  STYLE_REFERENCE_CLASSIFY_PROMPT_MISSING: "段落分类的提示词模板缺了：同步提示词模板后再「继续分类」。",
  STYLE_REFERENCE_CLASSIFY_PROMPT_RENDER_FAILED: "段落分类的提示词模板用不了（可能是旧版本）：同步提示词模板后再「继续分类」。",
  STYLE_REFERENCE_CLASSIFY_CONFIG_LOAD_FAILED: "读不出段落分类的模型配置：检查系统配置后再「继续分类」。",
  STYLE_REFERENCE_PARAGRAPHS_CHANGED: "分类期间这本书的段落被改动过，这次分类停下了：重新分类即可。",
  STYLE_REFERENCE_LEARN_FAILED: "学习文风没有完成。",
  STYLE_REFERENCE_LEARN_LLM_CALL_FAILED: "学习时模型调用失败：检查模型接入后「继续学习」。",
  NETWORK_ERROR: "连不上后端：检查后端是否在运行后重试。",
  REQUEST_TIMEOUT: "等太久了没有回音，稍后再试。",
};

/* 后端的原话能不能直接给作者看：见 lib/messages.js（与对照检查同一个口径） */
export const srIsChineseMessage = isChineseMessage;

/* 出错 → { code, message, action }。message 优先按错误码给固定的中文；认不出的码用后端的中文原话，
   英文原话一律不给作者看（fallback 也只能给中文：调用方不要把 error.message 当 fallback 传进来）。
   action：{ type: "settings" | "open_book" | "learn", label, bookId? } 或 null。 */
export function srErrorInfo(error, fallback = "操作没有完成，请稍后重试。") {
  const code = (error && error.code) || "";
  const details = (error && error.details) || {};
  const serverMessage = String((error && error.message) || "");
  const safeFallback = srIsChineseMessage(fallback) ? fallback : "操作没有完成，请稍后重试。";
  let message = SR_ERROR_TEXT[code] || (srIsChineseMessage(serverMessage) ? serverMessage : safeFallback);
  /* 学习失败：后端的中文原话说得更具体（哪一步、为什么），有就用它 */
  if (code === "STYLE_REFERENCE_LEARN_FAILED" && srIsChineseMessage(serverMessage)) message = serverMessage;
  if (code === "STYLE_REFERENCE_BOOK_DUPLICATE" && details.title) {
    message = `书库里已经有同一份文本：《${details.title}》。`;
  }
  const authorAction = details.author_action || null;
  let action = null;
  if (code === "STYLE_REFERENCE_BOOK_DUPLICATE" && details.book_id) {
    action = { type: "open_book", label: "打开这本", bookId: String(details.book_id) };
  } else if (authorAction && authorAction.view === "systemConfig") {
    action = { type: "settings", label: "去设置模型" };
  } else if (code === "STYLE_REFERENCE_LLM_REQUIRED" || code === "STYLE_REFERENCE_LEARN_CONFIG_MISSING") {
    action = { type: "settings", label: "去设置模型" };
  } else if (authorAction && authorAction.action === "learn_style") {
    action = { type: "learn", label: "去学习文风", bookId: authorAction.book_id ? String(authorAction.book_id) : null };
  }
  return { code, message, action };
}

/* ---------- 估算与格式 ---------- */

export function srFormatPct(value) {
  const n = Number(value) * 100;
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  const text = abs > 0 && abs < 10 ? n.toFixed(1).replace(/\.0$/, "") : String(Math.round(n));
  return `${text}%`;
}

export function srFormatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* 分钟：<1 说「不到 1 分钟」，≥60 说「约 N 小时 M 分钟」 */
export function srFormatMinutes(minutes) {
  const m = Number(minutes);
  if (!Number.isFinite(m) || m < 1) return "不到 1 分钟";
  if (m < 60) return `约 ${Math.round(m)} 分钟`;
  const h = Math.floor(m / 60);
  const rest = Math.round(m - h * 60);
  return rest ? `约 ${h} 小时 ${rest} 分钟` : `约 ${h} 小时`;
}

/* token / 字数：≥1 万写「N 万」 */
export function srFormatCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")} 万`;
  return n.toLocaleString("zh-CN");
}

/* 字数：「980 字」「4.4 万字」（万后面不再空一格） */
export function srFormatChars(value) {
  const text = srFormatCount(value);
  if (text === "—") return text;
  return text.endsWith("万") ? `${text}字` : `${text} 字`;
}

export function srFormatWhen(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${hh}:${mm}`;
}

/* 重新分类（就地重标类型）的费用：GET …/classification/estimate */
export function srClassifyEstimateText(estimate) {
  if (!estimate) return null;
  const calls = Number(estimate.est_calls || estimate.batches || 0);
  return `约 ${calls.toLocaleString("zh-CN")} 次模型调用，输入约 ${srFormatCount(estimate.est_input_tokens)} token、输出约 ${srFormatCount(estimate.est_output_tokens)} token，${srFormatMinutes(estimate.est_minutes)}（${estimate.parallel || 3} 路并行，重试不计在内）`;
}

/* 学一次的调用数：GET …/learn 的 estimate。每层读的原文是后端的上限（约 4.4 万字）；书没那么长时按全书字数说 */
export function srLearnEstimateText(estimate, { bookChars = null } = {}) {
  if (!estimate) return null;
  const calls = estimate.calls || {};
  const cap = estimate.est_input_chars && estimate.est_input_chars.extract_per_call;
  const whole = Number(bookChars) > 0 ? Number(bookChars) : null;
  const perCall = cap && whole ? Math.min(cap, whole) : cap;
  const read = perCall ? `每层读约 ${srFormatChars(perCall)}原文` : null;
  if (estimate.est_calls) {
    return [
      `约 ${estimate.est_calls} 次模型调用（分层读原文 ${calls.extract || 4} 次、写文风卡 1 次、识别本书专名 1 次、给全书片段打标签 ${calls.tags} 批）`,
      read,
      "重试不计在内",
    ].filter(Boolean).join("，");
  }
  return [`至少 ${Number(calls.extract || 4) + 2} 次模型调用，另加给全书片段打标签（整理完窗口才知道要几批）`, read].filter(Boolean).join("，");
}

/* 为什么建议重新学习 */
export const SR_RELEARN_TEXT = {
  legacy_profile: "这是旧版画像，还没有文风卡：学习文风后换成文风卡（就地更新，用在作品上的设置不变）。",
  types_changed: "段落类型已更新，建议重新学习：挑样本、打标签都要看段落类型。",
  text_changed: "这本书的正文变过了，建议重新学习。",
};

export function srRelearnText(reason) {
  return SR_RELEARN_TEXT[reason] || null;
}

/* 段落类型的来源：legacy_heuristic（旧版导入用启发式规则标的）要提醒并给「用模型重新分类」 */
export function srProvenanceView(provenance) {
  if (!provenance || typeof provenance !== "object") return { kind: "unknown", legacy: false, agreement: null };
  const agreement = provenance.agreement != null && Number.isFinite(Number(provenance.agreement)) ? Number(provenance.agreement) : null;
  if (provenance.source === "legacy_heuristic") {
    return {
      kind: "legacy_heuristic",
      legacy: true,
      agreement,
      heuristicParagraphs: Number(provenance.heuristic_paragraphs || 0),
      llmParagraphs: Number(provenance.llm_paragraphs || 0),
    };
  }
  if (provenance.source === "offline_heuristic") return { kind: "offline_heuristic", legacy: true, agreement: null, heuristicParagraphs: Number(provenance.heuristic_paragraphs || 0), llmParagraphs: 0 };
  return { kind: "llm", legacy: false, agreement, llmParagraphs: Number(provenance.llm_paragraphs || 0) };
}

/* ---------- 一本书现在走到哪一步 ---------- */

/* 这本书是否用在了当前作品上（applied_projects 里有当前作品） */
export function srAppliedToWork(book, workId) {
  if (!book || !workId) return null;
  return (book.appliedProjects || []).find((item) => item && item.project_id === workId) || null;
}

/* 「用模型重新分类」（就地重标，mode=retype）没做完：书全程是 ready，失败 / 取消只记在最近的分类作业上
   （book.classification）。没做完 → { cancelled, batchesDone, batchesTotal, error, resumable }，否则 null。 */
export function srRetypeUnfinished(book) {
  const c = book && book.classification;
  if (!c || book.rawStatus !== "ready" || c.mode !== "retype") return null;
  if (c.state !== "failed" && c.state !== "cancelled") return null;
  return {
    cancelled: c.state === "cancelled",
    batchesDone: Number(c.batches_done || 0),
    batchesTotal: Number(c.batches_total || 0),
    error: c.error || null,
    resumable: c.resumable !== false,
  };
}

/* 删书确认里「哪些作品正在用」：每本书列出用着它的全部作品（applied_projects 的作品名），不只当前作品 */
export function srDeleteBooksUsage(books, { workId = null, workTitle = "" } = {}) {
  const lines = [];
  const works = new Set();
  for (const book of books || []) {
    const projects = ((book && book.appliedProjects) || []).filter((p) => p && p.project_id);
    if (!projects.length) continue;
    const names = [];
    for (const p of projects) {
      works.add(p.project_id);
      const name = p.project_title || (p.project_id === workId && workTitle) || "一部未命名的作品";
      if (!names.includes(name)) names.push(name);
    }
    lines.push(`${names.map((n) => `《${n}》`).join("、")}正在用《${book.title}》的文风`);
  }
  if (!lines.length) return "";
  return `${lines.join("；")}。删除后，${works.size > 1 ? "这些作品" : "这部作品"}起草新场景时不再带它的文风。`;
}

/* running: { classify, learn }（活动表里这本书正在跑的作业：{ percentText } 或 null） */
export function srBookPipeline(book, { running = {}, workId = null } = {}) {
  if (!book) return null;
  const raw = book.rawStatus;
  if (running.classify) return { key: "classifying", label: `分类中 ${running.classify.percentText || ""}`.trim(), tone: "warn" };
  if (raw === "ingesting") return { key: "classifying", label: "分类中", tone: "warn" };
  if (raw === "cancelling") return { key: "cancelling", label: "取消中", tone: "neutral" };
  if (raw === "failed") return { key: "classify_failed", label: "分类未完成", tone: "danger" };
  if (running.learn) return { key: "learning", label: `学习中 ${running.learn.percentText || ""}`.trim(), tone: "warn" };
  const profile = book.profile;
  if (srAppliedToWork(book, workId)) {
    return profile && profile.needs_relearn
      ? { key: "applied_relearn", label: "当前作品在用 · 建议重学", tone: "warn" }
      : { key: "applied", label: "当前作品在用", tone: "ok" };
  }
  if (profile) {
    if (profile.needs_relearn) return { key: "relearn", label: profile.relearn_reason === "legacy_profile" ? "旧版画像" : "建议重新学习", tone: "warn" };
    return { key: "learned", label: "已学好", tone: "info" };
  }
  if (book.learn && (book.learn.state === "failed" || book.learn.state === "cancelled")) {
    return { key: "learn_failed", label: "学习未完成", tone: "danger" };
  }
  return { key: "to_learn", label: "待学习", tone: "neutral" };
}

/* 三步：book（参考书）/ learn（学习文风）/ apply（用于作品）；之后是随时可用的 check（对照检查：像不像）。
   tool = 不是要「做完」的一步：不参与落点，状态只有「等前一步 / 进行中 / 随时可查」。 */
export const SR_STAGES = [
  { id: "book", name: "参考书", icon: "BookOpen" },
  { id: "learn", name: "学习文风", icon: "Sparkles" },
  { id: "apply", name: "用于作品", icon: "Pen" },
  { id: "check", name: "对照检查", icon: "Target", tool: true },
];

export const SR_STAGE_STATE_LABEL = {
  done: "已完成", running: "进行中", attention: "需处理", todo: "未开始", blocked: "等前一步", open: "随时可查",
};

/* running：{ classify, learn, check }（活动表里这本书正在跑的作业） */
export function srStageStates(book, { running = {}, workId = null } = {}) {
  const states = { book: "done", learn: "todo", apply: "todo", check: "blocked" };
  if (!book) return states;
  const raw = book.rawStatus;
  if (running.classify || raw === "ingesting" || raw === "cancelling") states.book = "running";
  else if (raw === "failed") states.book = "attention";
  else if (srRetypeUnfinished(book)) states.book = "attention";
  else if (srProvenanceView(book.provenance).legacy) states.book = "attention";
  const ready = raw === "ready" && !running.classify;
  const profile = book.profile;
  if (!ready) states.learn = "blocked";
  else if (running.learn) states.learn = "running";
  else if (profile && !profile.needs_relearn) states.learn = "done";
  else if (profile || (book.learn && (book.learn.state === "failed" || book.learn.state === "cancelled"))) states.learn = "attention";
  else states.learn = "todo";
  if (!profile) states.apply = "blocked";
  else if (srAppliedToWork(book, workId)) states.apply = "done";
  else states.apply = "todo";
  if (!profile) states.check = "blocked";
  else if (running.check) states.check = "running";
  else states.check = "open";
  return states;
}

/* 落点：用在当前作品上的书落在「用于作品」；否则第一个没做完的步骤（对照检查不是要做完的一步，不当落点） */
export function srLandingStage(states, { applied = false } = {}) {
  if (applied) return "apply";
  for (const stage of SR_STAGES) {
    if (stage.tool) continue;
    if (states && states[stage.id] !== "done") return stage.id;
  }
  return "apply";
}

/* ---------- 文风画像：按层分组 ---------- */

/* profile.dimensions（后端按辨识度排） → [{ layer, label, dims }]，层按固定顺序，层内保持辨识度顺序 */
export function srDimensionGroups(dimensions) {
  const list = Array.isArray(dimensions) ? dimensions : [];
  return STYLE_LAYER_ORDER.map((layer) => ({
    layer,
    label: STYLE_LAYER_LABELS[layer],
    dims: list.filter((d) => d && styleLayerOf(d.dimension) === layer),
  })).filter((group) => group.dims.length);
}

/* ---------- 参考书活动 ---------- */

/* 条目的叫法：作业表条目按 kind（段落分类区分导入 / 重新分类 / 重标类型），其余用后端给的 kind_label */
export function srActivityKindLabel(entry) {
  if (!entry) return "";
  if (entry.kind === "classify") {
    if (entry.mode === "import") return "导入 · 段落分类";
    if (entry.mode === "retype") return "用模型重新分类";
    return "段落分类";
  }
  return styleJobKindLabel(entry.kind) || entry.kind_label || entry.kind || "";
}

export function srActivityActive(entry) {
  return !!entry && (entry.status === "queued" || entry.status === "running");
}

/* 一个作业为什么没完成（作业表的 error_json：{code, message, details}）：按错误码说中文；认不出的码只用后端的
   中文原话——作业边界记下的「TypeError: …」这类英文原话不给作者看，换成一句中文 */
export function srJobErrorText(error, { resumable = false } = {}) {
  const fallback = resumable ? "出了意外停下了，可以从断点继续。" : "出了意外停下了，请稍后重试。";
  if (!error) return fallback;
  // 作业边界兜底记下的失败（原话是异常串）：能不能「继续」看这个作业本身
  if (error.code === "STYLE_REFERENCE_JOB_FAILED") return `后台作业${fallback}`;
  return srErrorInfo(error, fallback).message;
}

function srStripKindPrefix(label, entry) {
  const text = String(label || "");
  const kind = styleJobKindLabel(entry.kind) || entry.kind_label || "";
  return kind && text.startsWith(`${kind} · `) ? text.slice(kind.length + 3) : text;
}

/* 一条活动 → { percent, percentText, detail, active }（面板与各页的进度条共用） */
export function srActivityView(entry, now = Date.now()) {
  if (!entry) return { percent: 0, percentText: "0%", detail: "", active: false };
  const active = srActivityActive(entry);
  let percent = 0;
  if (entry.status === "succeeded") percent = 100;
  else if (entry.percent != null && Number.isFinite(Number(entry.percent))) percent = Math.max(0, Math.min(99, Math.round(Number(entry.percent))));
  const elapsed = entry.elapsed_seconds != null
    ? Number(entry.elapsed_seconds)
    : Math.max(0, (now - (entry.startedAt || now)) / 1000);
  const parts = [];
  if (entry.status === "queued") parts.push("排队中");
  else if (entry.status === "running") {
    const label = srStripKindPrefix(entry.phase_label, entry) || "进行中";
    const steps = entry.steps && Number(entry.steps.total) > 0 ? ` ${entry.steps.done}/${entry.steps.total}` : "";
    parts.push(`${label}${steps}`);
    if (entry.cancel_requested) parts.push("正在取消");
    if (entry.stalled) parts.push("后台进程重启过，稍后自动接着跑");
  } else if (entry.status === "succeeded") parts.push("完成");
  else if (entry.status === "cancelled") parts.push("已取消");
  else if (entry.status === "failed") parts.push(`没有完成：${srJobErrorText(entry.error, { resumable: !!entry.resumable })}`);
  if (Number.isFinite(elapsed) && elapsed > 0) parts.push(`${active ? "已用" : "用时"} ${srFormatDuration(elapsed)}`);
  if (active && entry.eta_seconds != null) parts.push(`预计还需 ${srFormatDuration(entry.eta_seconds)}`);
  if (Number(entry.llm_calls) > 0) parts.push(`模型调用 ${entry.llm_calls} 次`);
  return { percent, percentText: `${percent}%`, detail: parts.join(" · "), active };
}

/* ---------- 书库排序、筛选、书脊色 ---------- */

export function srSortBooks(books, workId) {
  const list = Array.isArray(books) ? books.slice() : [];
  if (!workId) return list;
  return list
    .map((b, i) => ({ b, i, pin: srAppliedToWork(b, workId) ? 0 : 1 }))
    .sort((x, y) => (x.pin - y.pin) || (x.i - y.i))
    .map((x) => x.b);
}

export function srFilterBooks(books, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return books;
  return books.filter((b) => `${b.title || ""} ${b.author || ""}`.toLowerCase().includes(q));
}

const SR_SPINE_COLORS = ["crimson", "gold", "slate", "sage"];
export function srSpineColor(bookId) {
  let h = 0;
  for (const ch of String(bookId || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return SR_SPINE_COLORS[h % SR_SPINE_COLORS.length];
}

/* ---------- 界面偏好 ws_sr_ui_v1（上次打开的书与步骤；只是便利） ---------- */
export const SR_UI_PREFS_KEY = "ws_sr_ui_v1";
const SR_STAGE_IDS = new Set(SR_STAGES.map((s) => s.id));

function srCleanPref(entry) {
  return entry && typeof entry === "object" && entry.bookId
    ? { bookId: String(entry.bookId), stage: SR_STAGE_IDS.has(entry.stage) ? entry.stage : null }
    : null;
}

export function srReadUiPrefs(storage) {
  try {
    const store = storage || globalThis.localStorage;
    const raw = store && store.getItem(SR_UI_PREFS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (!parsed || typeof parsed !== "object") return { last: null, works: {} };
    const works = {};
    if (parsed.works && typeof parsed.works === "object") {
      for (const [key, value] of Object.entries(parsed.works)) {
        const clean = srCleanPref(value);
        if (clean) works[key] = clean;
      }
    }
    return { last: srCleanPref(parsed.last), works };
  } catch (e) {
    return { last: null, works: {} };
  }
}

export function srRememberUi(workId, entry, storage) {
  if (!entry || !entry.bookId) return;
  try {
    const store = storage || globalThis.localStorage;
    if (!store) return;
    const prefs = srReadUiPrefs(store);
    const value = srCleanPref(entry);
    const works = { ...prefs.works };
    if (workId) works[workId] = value;
    const keys = Object.keys(works);
    if (keys.length > 20) keys.slice(0, keys.length - 20).forEach((k) => { delete works[k]; });
    store.setItem(SR_UI_PREFS_KEY, JSON.stringify({ last: value, works }));
  } catch (e) { /* 存不进就算了 */ }
}

/* 进入页面时打开哪本书：本次会话刚看的 → 当前作品在用的 → 这部作品上次打开的 → 上次打开的 → 第一本 */
export function srPickLandingBook(books, { prefs, workId, session = null } = {}) {
  const list = Array.isArray(books) ? books : [];
  if (!list.length) return null;
  const has = (id) => !!id && list.some((b) => b.id === id);
  if (session && has(session.bookId)) return { bookId: session.bookId, stage: session.stage || null, source: "session" };
  const applied = workId ? list.find((b) => srAppliedToWork(b, workId)) : null;
  if (applied) return { bookId: applied.id, stage: null, source: "applied" };
  const p = prefs || { last: null, works: {} };
  const own = workId && p.works ? p.works[workId] : null;
  if (own && has(own.bookId)) return { bookId: own.bookId, stage: own.stage || null, source: "work" };
  if (p.last && has(p.last.bookId)) return { bookId: p.last.bookId, stage: p.last.stage || null, source: "last" };
  return { bookId: list[0].id, stage: null, source: "first" };
}

/* 「参考书活动」面板在哪：宽屏在左栏书库里，≤1280 在页头「参考书库」打开的书库里 */
export const SR_ACTIVITY_WHERE = "「参考书库」的「参考书活动」";

/* ---------- 当前作品的场（本场预览、对照检查选一场） ---------- */

/* srLoadWorkScenes 的章节 → 下拉框分组 [{ label, scenes: [{ value, label }] }]（没有场的章不列） */
export function srSceneOptions(chapters) {
  return (chapters || []).map((chapter) => ({
    label: `第 ${chapter.no} 章${chapter.title ? ` · ${chapter.title}` : ""}`,
    scenes: (chapter.scenes || []).map((scene, index) => ({
      value: scene.sceneId,
      label: `第 ${chapter.no} 章 · 第 ${index + 1} 场${scene.title ? `「${scene.title}」` : ""}`,
    })),
  })).filter((group) => group.scenes.length);
}

/* 场景 id → 「第 3 章 · 第 2 场「码头」」（找不到给空串） */
export function srSceneLabel(chapters, sceneId) {
  for (const group of srSceneOptions(chapters)) {
    const hit = group.scenes.find((scene) => scene.value === sceneId);
    if (hit) return hit.label;
  }
  return "";
}
