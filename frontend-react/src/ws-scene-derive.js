/* ==========================================================
   AI 起草台 — 纯推导（不碰网络、不碰 localStorage、不碰 React）
   ----------------------------------------------------------
   · 后端 token → 作者读得懂的话：任务状态、管线步位、七步进度、风格链路提示、参考窗口
   · workbench → 运行记录：scnRunRecordFromWorkbench 是起草（scnRun）与恢复（scnHydrateFromBackend）
     共用的唯一一份——过去两边各抄一份，差一个字段就各说各话
   · 裁决只认后端 author_state（可归档 / 质量建议 / 已证实的硬问题 / 等你终选）。
     2026-09-21 删掉了前端自己的「质检」（短句率 / 句式重复 / 超长句的红绿判词与划线）
     和永远对不上的「戏剧卡对齐」——它们和后端按参考作者放宽的门互相矛盾。
   ========================================================== */

/* ---- 场景在台面上的状态词：本地「从没提交过」的场叫「待起草」；「排队中」只留给后端真的排上了队的任务
   （job.status=queued）。过去两者都叫「排队」，同一场在头部写「排队」、在书脊上写「待起草」。 ---- */
const STATE_LABEL = { running: "运行中", queued: "待起草", ready: "待复核", archived: "已归档" };
const STATE_TONE = { running: "accent", queued: "neutral", ready: "warn", archived: "ok" };
const JOB_QUEUED_LABEL = "排队中";
const stateLabelOf = (state, jobQueued) => (state === "queued" && jobQueued ? JOB_QUEUED_LABEL : (STATE_LABEL[state] || STATE_LABEL.queued));
const stateToneOf = (state, jobQueued) => (state === "queued" && jobQueued ? "info" : (STATE_TONE[state] || "neutral"));

/* ---- 运行任务（/run-jobs）的状态与步位 ---- */
const RUN_JOB_POLLING_STATUSES = new Set(["queued", "running", "cancel_requested"]);
const RUN_JOB_CANCELABLE_STATUSES = new Set(["queued", "running"]);
const RUN_JOB_TERMINAL_STATUSES = new Set(["cancelled", "completed", "failed", "blocked"]);
const RUN_JOB_STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  completed: "已完成",
  failed: "运行失败",
  blocked: "已阻断",
};
/* 后端 scenes run 管线的 current_step（scene_run_jobs.SCENE_RUN_STAGE_ORDER）→ 作者可读标签。
   2026-09-12 风格直起：neutral_running 步位不动、内容换——绑定 draft_mode=style_first 时这一步
   写的是作者手笔首稿，标签按 workbench generation_summary.draft_mode 切换；词表外的 token 原样回显。 */
const RUN_JOB_STEP_LABELS = {
  // 管线阶段之外的几个步位：过去原样回显成「运行任务 · 已阻断 · preflight_blocked」
  queued: "等待接管",
  preflight_blocked: "起草前检查未通过",
  blocked: "管线中止",
  cancelled: "已取消",
  planning_running: "规划蓝图",
  bundle_built: "上下文已冻结",
  neutral_running: "中性稿",
  hard_qc_running: "硬质检",
  style_running: "风格稿",
  soft_qc_running: "软质检",
  rewrite_running: "近终稿改写",
  acceptance_review_running: "近终稿评审",
  near_final: "近终稿",
  // Best-of-N 关键场景的暂停点：管线停在这里等作者盲选一稿（过去原样回显英文词）
  awaiting_candidate_selection: "等你终选",
  archived: "已归档",
};
const RUN_JOB_STYLE_FIRST_DRAFT_LABEL = "首稿（作者手笔）";
function runJobStepLabel(step, draftMode) {
  const token = step == null ? "" : String(step);
  if (!token) return "";
  if (token === "neutral_running" && draftMode === "style_first") return RUN_JOB_STYLE_FIRST_DRAFT_LABEL;
  return RUN_JOB_STEP_LABELS[token] || token;
}

/* 进度条的七个阶段——只由后端 token（job.current_step / scene_run_state.scene_status）推出来，
   不再有按计时器走的假百分比。scnRunStageIndex 认不出的 token 返回 -1，由调用方决定怎么显示。 */
const RUN_STAGES = [
  { id: "preflight", name: "预检" },
  { id: "draft", name: "首稿" },
  { id: "hard_qc", name: "硬质检" },
  { id: "style", name: "风格稿" },
  { id: "soft_qc", name: "软质检" },
  { id: "near_final", name: "近终稿" },
  { id: "archive", name: "归档" },
];
function scnRunStageIndex(token) {
  const t = String(token || "");
  if (!t) return -1;
  if (t === "archived") return 6;
  // 顺序有讲究：hard_qc_*_rewrite_required 同时含 rewrite，必须先认成硬质检
  if (t.includes("soft_qc")) return 4;
  if (t.includes("hard_qc")) return 2;
  if (t.includes("candidate") || t === "critical_scene_human_gate") return 3;
  if (t.includes("style")) return 3;
  if (t.includes("neutral") || t.includes("draft")) return 1;
  if (/near_final|rewrite|acceptance/.test(t)) return 5;
  if (/^(queued|ready|preflight|planning|bundle)/.test(t)) return 0;
  return -1;
}
/* 管线停在哪一步的那句话（日志用；认不出的 token 不念英文）。 */
function scnPipeStepName(token) {
  const index = scnRunStageIndex(token);
  return index >= 0 ? RUN_STAGES[index].name : "";
}

/* workbench generation_summary.draft_mode（当前运行冻结契约的起草方式）→ "style_first" | "neutral_first" | null */
function scnDraftModeFrom(wb) {
  const summary = wb && wb.generation_summary;
  const mode = summary && typeof summary === "object" ? String(summary.draft_mode || "") : "";
  return mode === "style_first" || mode === "neutral_first" ? mode : null;
}

/* ---- 2026-09-14 风格保真修补（WP4.2「作者看得见」）：风格链路提示 + 本场参考窗口 ----
   后端每次运行都算出 STYLE_* notices（generation_summary.notices）和这一场提示里实际放入的
   参考书样例窗口（generation_summary.style_windows：{book_id, profile_id, step, windows[]}，窗口
   只有段落序号闭区间与读数，不含原文）。这里把两者规整后记进运行记录（随 scnRunSave 持久化）；
   提示条按严重度着色，窗口面板展开时才按区间取原文。词表外的 code 回退为「code: message」，
   绝不吞掉后端的新提示。 */
const STYLE_NOTICE_LABELS = {
  STYLE_FIRST_DRAFT: "首稿已按参考作者手笔直起",
  STYLE_DRAFT_FALLBACK_NEUTRAL: "风格稿未通过，已回退为中性稿",
  STYLE_INJECTION_MISS: "参考已绑定，但本次没有注入任何风格块",
  STYLE_INJECTION_DEGRADED: "风格注入失败或被输入预算裁掉",
  STYLE_PLAGIARISM_HIT: "与参考原文重叠（抄袭红线命中）",
  STYLE_BANNED_TERM_HIT: "命中参考画像的禁用词",
  STYLE_GATE_UNAVAILABLE: "参考来源安全检查未能执行",
};
/* 后端严重度 info / warning / error / blocking → 三档着色（blocking 与 error 同色，另带 is-blocking） */
function scnStyleNoticeSeverity(value) {
  const key = String(value || "").toLowerCase();
  if (key === "error" || key === "blocking") return "error";
  if (key === "warning") return "warning";
  return "info";
}
function scnStyleNoticeLabel(notice) {
  const code = String((notice && notice.code) || "");
  return STYLE_NOTICE_LABELS[code] || code;
}
/* workbench / run 结果里的 generation_summary.notices → [{code, severity, message, blocking, hitCount?, stage?}] */
function scnStyleNoticesFrom(wb) {
  const summary = wb && wb.generation_summary;
  const raw = summary && typeof summary === "object" ? summary.notices : null;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((n) => n && typeof n === "object" && n.code)
    .map((n) => {
      const item = {
        code: String(n.code),
        severity: scnStyleNoticeSeverity(n.severity),
        blocking: String(n.severity || "").toLowerCase() === "blocking",
        message: typeof n.message === "string" ? n.message : "",
      };
      if (Number.isFinite(Number(n.hit_count)) && n.hit_count != null) item.hitCount = Number(n.hit_count);
      if (n.stage) item.stage = String(n.stage);
      return item;
    });
}
/* generation_summary.style_windows → {bookId, profileId, step, windows: [{start, end, chapter, position, paragraphType, paragraphs, chars}]} | null */
function scnStyleWindowsFrom(wb) {
  const summary = wb && wb.generation_summary;
  const raw = summary && typeof summary === "object" ? summary.style_windows : null;
  if (!raw || typeof raw !== "object" || !Array.isArray(raw.windows)) return null;
  const windows = raw.windows
    .filter((w) => w && typeof w === "object" && Number.isInteger(w.start) && Number.isInteger(w.end) && w.start >= 0 && w.end >= w.start)
    .map((w) => ({
      start: w.start,
      end: w.end,
      chapter: Number.isInteger(w.chapter) ? w.chapter : 0,
      position: String(w.position || ""),
      paragraphType: String(w.paragraph_type || ""),
      paragraphs: Number.isInteger(w.paragraphs) && w.paragraphs > 0 ? w.paragraphs : (w.end - w.start + 1),
      chars: Number.isInteger(w.chars) ? w.chars : 0,
    }));
  if (!windows.length) return null;
  return {
    bookId: raw.book_id ? String(raw.book_id) : null,
    profileId: raw.profile_id ? String(raw.profile_id) : null,
    step: raw.step ? String(raw.step) : null,
    windows,
  };
}
const STYLE_WINDOW_POSITION_LABELS = { opening: "章首", middle: "章中", closing: "章尾", whole: "整章" };
const STYLE_PARAGRAPH_TYPE_LABELS = {
  dialogue: "对白", narration: "叙述", psychology: "心理", description_env: "环境描写",
  description_char: "人物描写", action: "动作", transition: "过渡", flashback: "回忆",
};
const STYLE_WINDOW_STEP_LABELS = { style_draft: "风格稿", neutral_draft: "首稿", scene_literary_rewrite: "近终稿重写稿" };
/* 一行窗口标签：第{章}章 · 位置 · 段型 · 第{起}–{止}段 · {字}字。后端段落序号从 0 起，作者看到的是从 1 起的段号。 */
/* 「在哪 · 多长」两件事：第 1 章章首的叙述 · 第 1–60 段（3800 字）。
   词表外的位置 / 段落类型（后端新加的英文键）不念给作者，只留章号和段号。 */
function scnStyleWindowLabel(w) {
  const chapter = w.chapter > 0 ? `第${w.chapter}章` : "";
  const position = STYLE_WINDOW_POSITION_LABELS[w.position] || "";
  const ptype = STYLE_PARAGRAPH_TYPE_LABELS[w.paragraphType] || "";
  const where = `${chapter}${position}${ptype ? `${chapter || position ? "的" : ""}${ptype}` : ""}`;
  const range = `第${w.start + 1}–${w.end + 1}段（${w.chars}字）`;
  return where ? `${where} · ${range}` : range;
}
function scnStyleWindowKey(bookId, w) { return `${bookId || ""}:${w.start}:${w.end}`; }

/* ---- 起草稿 → 台面用的段落结构与字数（只有读数，没有判词）----
   函数名 scnQC 保留：测试夹具用它造运行记录。段落结构 {id, parts:[{text}]} 是持久化在 scn-run:<sid>
   里的形状；旧运行记录里的段落可能带着风险划线切出来的多段 parts，一律按纯文本拼回。 */
function scnParaText(para) {
  return ((para && para.parts) || []).map(part => part.text).join("");
}
function scnQC(paras) {
  const draft = (paras || [])
    .map((p, i) => ({ id: p.id || `p${i + 1}`, parts: [{ text: String((p && p.text) || "") }] }))
    .filter(p => p.parts[0].text);
  const words = draft.map(p => p.parts[0].text).join("").replace(/\s/g, "").length;
  return { draft, words, verdict: { words } };
}
function scnReQC(draft) {
  const paras = (draft || []).map(p => ({ id: p.id, text: scnParaText(p) }));
  return paras.length ? scnQC(paras) : null;
}

/* 一条后端裁决条目给作者看的那句话；没附说明的条目不把英文 issue_key 甩给作者。 */
function scnFindingText(finding) {
  if (!finding || typeof finding !== "object") return "";
  return String(finding.human_readable_reason || finding.message || "").trim();
}
/* 说明里一个汉字都没有（多半是原样透出的异常文本，例如校验失败的英文堆栈）：
   不当作给作者的一句话摆出来，页面上折叠成「查看原文」。 */
function scnFindingIsPlainLanguage(text) {
  return /[㐀-鿿]/.test(String(text || ""));
}

/* ---- 作者可见状态门（Wave 2 · 治理 §5.3/§5.4）----
   从 workbench/status 的 author_state 投影提取「无法继续 vs 有稿建议修改」：
   · hard_blocked（verified Q0/Q1）→ 不可归档，正文保留可接管
   · quality_warning（Q2/Q3）→ 有稿可归档，警告随行
   gate 随运行记录持久化，裁决条据此分开展示、归档前先拦。 ---- */
function scnGateFrom(src) {
  const a = src && src.author_state;
  if (!a || typeof a !== "object") return null;
  return {
    authorState: a.author_state || null,
    blocking: Array.isArray(a.blocking_findings) ? a.blocking_findings : [],
    warnings: Array.isArray(a.quality_warnings) ? a.quality_warnings : [],
    recommended: Array.isArray(a.recommended_actions) ? a.recommended_actions : [],
    canArchive: a.can_archive !== false,
  };
}
function scnGateLog(gate, tm) {
  if (!gate) return null;
  if (gate.authorState === "hard_blocked") {
    const reasons = gate.blocking.map(scnFindingText).filter(scnFindingIsPlainLanguage).join("；");
    return { t: tm, who: "pipeline", text: `有 ${gate.blocking.length || 1} 条已证实的硬问题${reasons ? "：" + reasons : ""}——正文已保留，这一稿暂不能归档` };
  }
  if (gate.authorState === "quality_warning") {
    return { t: tm, who: "pipeline", text: `有 ${gate.warnings.length} 条质量建议随稿附上——可以直接采纳归档，也可以按建议改后重跑` };
  }
  if (gate.authorState === "awaiting_author_choice") {
    return { t: tm, who: "pipeline", text: "关键场景写出了几份候选稿，等你终选一稿后管线自动续跑" };
  }
  return null;
}

/* ---- 失败给明确引导（执行契约缺字段 / LLM 未启用 / 预检不过），不装假进度 ---- */
function scnFriendly(e) {
  const code = (e && e.code) || "";
  const msg = (e && e.message) || String(e || "");
  if (code === "SCENE_EXECUTION_CONTRACT_BLOCKED") {
    const miss = (((e && e.details) || {}).missing_fields || []).join("、");
    return new Error(`这一场的执行契约还缺关键字段${miss ? `（${miss}）` : ""}——雪花整理出来的场回构思第 10 步补齐并确认（场景卡自动跟上），手加的场在章节编排把场景卡补全。`);
  }
  if (code === "VOICE_PROFILE_MISSING" || code === "RELATION_PROFILE_MISSING") {
    // 2026-09-20：这项前置检查已经取消（声线 / 关系卡早就没有地方能写，它拦下的是每一部真实作品的每一场）。
    // 只有取消之前留下的旧任务行还带着这个码——如实告诉作者重跑即可，不再有「补齐声线卡」这个动作。
    return new Error("上一次起草被「声线 / 关系卡」前置检查拦下了——这项检查已经取消，直接点「开始起草」重跑即可。");
  }
  if (/LLM/i.test(code) || /llm|provider|api.?key/i.test(msg)) {
    return new Error("AI 起草需要可用的 LLM：请到「系统设置 → 模型与接入」配置并启用后重试。原始信息：" + msg);
  }
  return new Error("起草失败：" + msg);
}

/* 终态任务（blocked / failed / cancelled）没有留下可审阅草稿时，给作者看的那句话。
   起草台有两条路会写这句话：startRun 的 catch（scnRun 抛出的 scnFriendly）与「终态任务恢复」effect
   （ws-scene-board-state.js）。两条路必须说同一句：过去 effect 用一句笼统的「任务已阻断…请检查阻断原因后重试」
   盖掉了 catch 里那句带着原因与出口的话（真实故障：作者只看到「被阻断」，不知道为什么，也没有地方可查）。 */
function scnTerminalJobMessage(job) {
  const status = String((job && job.status) || "");
  if (status === "cancelled") return "任务已取消，可重新起草";
  const code = String((job && job.error_code) || "");
  const text = String((job && job.error_text) || "");
  if (!code && !text) {
    return status === "blocked"
      ? "任务已阻断，没有产出可审阅的草稿，任务也没有留下阻断原因——请重试"
      : "任务运行失败，没有产出可审阅的草稿——请重试";
  }
  return scnFriendly({ code, message: text, details: { missing_fields: (job && job.missing_fields) || [] } }).message;
}

/* 页面换场 / 卸载时只停止前端跟踪，不伪造后端取消；这类中止用这个码识别，不当失败报给作者。 */
const SCN_RUN_UI_ABORTED = "SCENE_RUN_UI_ABORTED";
function scnRunUiAbortError() {
  const error = new Error("scene run UI tracking stopped");
  error.code = SCN_RUN_UI_ABORTED;
  return error;
}

/* 一份质检摘要里的改写指令条目。后端 workbench 已把 rewrite_brief 摊平成字符串列表
   （api/routes/scenes.py `_extract_rewrite_brief`）；qc-reports 明细路径还会带原始
   rewrite_brief_json 条目（{instruction} / {carry_note_text}），同样按后端规则取字段，
   不让对象条目拼成 "[object Object]"。 */
function scnRewriteBriefEntries(report) {
  if (!report || typeof report !== "object" || !Array.isArray(report.rewrite_brief)) return [];
  return report.rewrite_brief
    .map(entry => {
      if (typeof entry === "string") return entry.trim();
      if (entry && typeof entry === "object") return String(entry.instruction || entry.carry_note_text || "").trim();
      return "";
    })
    .filter(Boolean);
}
function scnRewriteBriefFrom(src) {
  const wb = src && typeof src === "object" ? src : {};
  /* GET /scenes/{id}/workbench 以 hard_qc_summary / soft_qc_summary 透出最近一次硬/软质检
     （api/routes/scenes.py `_serialize_qc_summary`）。顺序：硬质检先于软质检（硬是阻断级重写，
     软只是修补建议）；同类里服务端键名优先，早期契约名 hard_qc / soft_qc / latest_qc 仅兜底。 */
  const reports = [
    wb.hard_qc_summary, wb.hard_qc,
    wb.soft_qc_summary, wb.soft_qc,
    wb.latest_qc,
  ];
  for (const report of reports) {
    const brief = scnRewriteBriefEntries(report);
    if (brief.length) return brief.join("；");
  }
  const projection = scnGateFrom(src);
  return ((projection && projection.blocking) || [])
    .map(f => f.human_readable_reason || f.message || f.issue_key || f.kind)
    .filter(Boolean)
    .join("；");
}

/* ---- 生命周期预算断点：任务因预算耗尽停下时，给出追加量与作者读得懂的标签 ---- */
const SCN_LIFECYCLE_BUDGET_CODES = new Set([
  "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
  "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
  "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
]);
function scnBudgetBlock(job, workbench) {
  const code = String((job && job.error_code) || "");
  if (!SCN_LIFECYCLE_BUDGET_CODES.has(code)) return null;
  const lifecycle = (workbench && workbench.scene_run_state && workbench.scene_run_state.lifecycle_budget) || {};
  let topup;
  let label;
  if (code === "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED") {
    const suggested = Number(lifecycle.recommended_topup_tokens || lifecycle.baseline_tokens || 6400);
    topup = { extra_tokens: Math.max(1, Math.trunc(suggested)) };
    label = "本场 token 生命周期预算已到派发边界";
  } else if (code === "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED") {
    topup = { extra_attempts: 1 };
    label = "本场业务尝试预算已用完";
  } else {
    topup = { extra_provider_attempts: 1 };
    label = "本场 provider 尝试预算已用完";
  }
  return {
    code,
    label,
    message: String((job && job.error_text) || "生命周期预算耗尽；已有正文已保留"),
    currentStep: String((job && job.current_step) || "blocked"),
    lifecycle,
    topup,
  };
}

/* ---- workbench → 运行记录 ---- */
/* workbench 里给作者读的那一稿：终稿 → 风格稿 → 首稿（中性或作者手笔）。 */
function scnWorkbenchContent(wb) {
  return (wb && ((wb.final_scene && wb.final_scene.content)
    || (wb.style_draft && wb.style_draft.content)
    || (wb.neutral_draft && wb.neutral_draft.content))) || "";
}
function scnSplitParas(content) {
  return String(content || "").split(/\n{2,}|\n/).map((x, i) => ({ id: "p" + (i + 1), text: x.trim() })).filter(p => p.text);
}
/* 起草与恢复共用的那部分运行记录：正文段落与字数、后端裁决、改写指令、起草方式、风格提示与窗口、预算断点。
   job：终态任务（预算断点从它的 error_code 认）；pipeState 缺省时读 workbench 的 scene_status。
   预算断点一律不可归档（blockReason=lifecycle_budget），不管后端投影怎么说。
   各自的 state / attempt / 日志 / 本次运行读数由调用方补上。 */
function scnRunRecordFromWorkbench(wb, { job = null, authorNote = "", pipeState } = {}) {
  const record = scnQC(scnSplitParas(scnWorkbenchContent(wb)));
  const status = pipeState === undefined
    ? (wb && wb.scene_run_state && wb.scene_run_state.scene_status)
    : pipeState;
  const gate = scnGateFrom(wb);
  const budgetBlock = scnBudgetBlock(job, wb);
  record.pipeState = String(status || "");
  record.gate = budgetBlock
    ? { ...(gate || { authorState: null, blocking: [], warnings: [], recommended: [] }), canArchive: false, blockReason: "lifecycle_budget" }
    : gate;
  record.rewriteBrief = scnRewriteBriefFrom(wb);
  record.authorNote = authorNote;
  record.draftMode = scnDraftModeFrom(wb);
  record.styleNotices = scnStyleNoticesFrom(wb);
  record.styleWindows = scnStyleWindowsFrom(wb);
  record.budgetBlock = budgetBlock;
  return record;
}

export {
  stateLabelOf, stateToneOf,
  RUN_JOB_POLLING_STATUSES, RUN_JOB_CANCELABLE_STATUSES, RUN_JOB_TERMINAL_STATUSES, RUN_JOB_STATUS_LABELS,
  runJobStepLabel, RUN_STAGES, scnRunStageIndex, scnPipeStepName, scnDraftModeFrom,
  STYLE_NOTICE_LABELS, scnStyleNoticeSeverity, scnStyleNoticeLabel, scnStyleNoticesFrom, scnStyleWindowsFrom,
  STYLE_WINDOW_STEP_LABELS, scnStyleWindowLabel, scnStyleWindowKey,
  scnParaText, scnQC, scnReQC, scnFindingText, scnFindingIsPlainLanguage, scnGateFrom, scnGateLog,
  scnFriendly, scnTerminalJobMessage, SCN_RUN_UI_ABORTED, scnRunUiAbortError,
  scnRewriteBriefFrom, scnRunRecordFromWorkbench,
};
