import { apiGet, apiPost } from "./lib/client.js";
import { WsWorks, wsKey } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsDiagnosis } from "./ws-diagnosis-summary.jsx";
import { WrDocs, WrDocVersions, WrRecovery } from "./wr-doc-store.jsx";
import { hasAuthorText, stripLegacyDraftPlaceholder } from "./manuscript-html.js";
import { copyGateAdoptMessage, isCopyGateError } from "./ws-copy-gate.js";
import { fidPatchView, fidRankText, fidStyleStepView, fidVerdict } from "./ws-fidelity-model.js";
import {
  RUN_JOB_STATUS_LABELS, RUN_JOB_TERMINAL_STATUSES, scnPipeStepName, scnParaText,
  scnGateLog, scnFriendly, scnRunUiAbortError, scnStyleNoticeLabel, scnRunRecordFromWorkbench,
} from "./ws-scene-derive.js";

/* ==========================================================
   AI 起草台 — 与后端说话的部分
   ----------------------------------------------------------
   · 起草：后端 scenes run 管线（POST run/jobs；进度与终态只认后端任务）
   · 恢复：本地没有运行记录时从 workbench 取回这一场的最新产出
   · 终选：盲化候选、提交选择、从断点续跑
   · 归档：adopt-current 单入口；作者稿存在时先备份或只存候选
   提示词由后端 config/prompts.yaml 组装；这里不造任何正文。
   ========================================================== */

const NOT_SYNCED_MESSAGE = "这一场还没同步到后端目录——稍候片刻或刷新后重试。";

/* 目录 sid → 后端 scene id；目录还没同步到后端时是 null。 */
async function scnSceneIdOf(sid) {
  return (await WsCatalog.__backendSceneId(sid)) || null;
}
async function scnRequireSceneId(sid) {
  const sceneId = await scnSceneIdOf(sid);
  if (!sceneId) throw new Error(NOT_SYNCED_MESSAGE);
  return sceneId;
}

function scnThrowIfAborted(signal) {
  if (signal && signal.aborted) throw scnRunUiAbortError();
}
function scnPollDelay(delayMs, signal) {
  scnThrowIfAborted(signal);
  if (!signal) return new Promise(resolve => setTimeout(resolve, delayMs));
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal.removeEventListener("abort", abort);
      resolve();
    };
    const abort = () => {
      clearTimeout(timer);
      reject(scnRunUiAbortError());
    };
    const timer = setTimeout(finish, delayMs);
    signal.addEventListener("abort", abort, { once: true });
  });
}

/* 等待终态。页面传 lifecycle.waitForTerminal（由场景页唯一的任务控制条轮询 latest，终态时兑现），
   这里就不再自己每 2 秒去问 run-jobs/{id}——过去同一个任务被两个轮询者同时问。
   没传（冒烟脚本、单测直接调用）时退回自己轮询。没有客户端时限：一次 LLM 调用本来就可能要 15 分钟，
   过去 5 分钟一到就把还在跑的任务报成「超时」。 */
/* 运行记录里「像不像」的一行：首稿第几位（在不在范围）→ 风格步做了什么 → 补丁留没留 → 终稿第几位 */
function scnFidelityLogText(fidelity) {
  if (!fidelity) return "";
  const reading = (label, r) => {
    if (!r) return "";
    const verdict = fidVerdict(r);
    return `${label}${fidRankText(r.percentile)}${verdict ? `（${verdict.label}）` : ""}`;
  };
  const step = fidStyleStepView(fidelity.style_step);
  const patch = fidPatchView(fidelity.patch);
  const parts = [
    reading("首稿", fidelity.first_draft),
    step ? step.text : "",
    reading("修改稿", fidelity.revision),
    patch ? patch.text : "",
    reading("终稿", fidelity.final),
  ].filter(Boolean);
  return parts.length ? `像不像：${parts.join("；")}` : "";
}

async function scnWaitForTerminalJob(job, sceneId, lifecycle, trackedGet, signal) {
  if (RUN_JOB_TERMINAL_STATUSES.has(job.status)) return job;
  if (lifecycle && typeof lifecycle.waitForTerminal === "function") {
    const settled = await lifecycle.waitForTerminal(job, sceneId);
    scnThrowIfAborted(signal);
    if (settled) return settled;
    // null：控制条那边看到的是别的任务（另一个标签页又起了一次、或一条迟到的旧 latest）——
    // 认不准就自己盯住这一个任务，宁可多一个轮询者也不卡在「运行中」。
  }
  let last = job;
  while (!RUN_JOB_TERMINAL_STATUSES.has(last.status)) {
    await scnPollDelay(2000, signal);
    scnThrowIfAborted(signal);
    try {
      last = await trackedGet(`/api/v1/run-jobs/${job.job_id}`);
    } catch (e) {
      scnThrowIfAborted(signal);
    }
  }
  return last;
}

/* ---- 完整一跑（FE-ALIGN F6）：投递 run job → 等终态 → workbench 取产出 ----
   第三个参数是旧签名留下的位置（冒烟脚本与测试仍按四参调用），不再使用。
   lifecycle：{ signal, runPolicy, resumeBudget, onJobCreated(job, sceneId), waitForTerminal(job, sceneId) } */
async function scnRun(item, note, _prevText, lifecycle = {}) {
  const signal = lifecycle && lifecycle.signal;
  const trackedGet = (path) => signal ? apiGet(path, { signal }) : apiGet(path);
  const trackedPost = (path, body) => signal ? apiPost(path, body, { signal }) : apiPost(path, body);
  scnThrowIfAborted(signal);
  const sceneId = await scnSceneIdOf(item.sid);
  scnThrowIfAborted(signal);
  if (!sceneId) throw new Error(NOT_SYNCED_MESSAGE);
  const t0 = Date.now();
  // G3：作者改写指令随任务下发（后端注入风格生成阶段的提示词）。
  // 起草台是作者在场的交互式工作流：严格模式把 Q2 建议停在可采纳态，
  // 由「采纳并归档」留下明确接受记录；无 Q2 时后端仍可按契约自动完成。
  const body = { run_policy: (lifecycle && lifecycle.runPolicy) || "strict" };
  const authorNote = note == null ? "" : String(note).trim();
  if (Array.from(authorNote).length > 2000) {
    const error = new Error("作者改写指令不能超过 2000 个字符，请精简后重试；系统没有截断或提交这段指令。");
    error.code = "AUTHOR_NOTE_TOO_LONG";
    throw error;
  }
  if (authorNote) body.author_note = authorNote;
  if (lifecycle && lifecycle.resumeBudget === true) body.resume_budget = true;
  let job;
  try {
    job = await trackedPost(`/api/v1/scenes/${sceneId}/run/jobs`, body);
  } catch (e) {
    scnThrowIfAborted(signal);
    throw scnFriendly(e);
  }
  scnThrowIfAborted(signal);
  try {
    if (lifecycle && typeof lifecycle.onJobCreated === "function") lifecycle.onJobCreated(job, sceneId);
  } catch (e) {}
  const last = await scnWaitForTerminalJob(job, sceneId, lifecycle, trackedGet, signal);
  scnThrowIfAborted(signal);
  /* 终态后先看产出：需人工审阅的 blocked 也可能已有草稿，照实呈现 */
  let wb = null;
  try { wb = await trackedGet(`/api/v1/scenes/${sceneId}/workbench`); } catch (e) {}
  scnThrowIfAborted(signal);
  const pipeState = wb && wb.scene_run_state ? wb.scene_run_state.scene_status : last.status;
  // 后端原子归档了终稿（reliable / 无警告路径）：服务端改了这一场的正文，只拉这一章的诊断角标
  if (pipeState === "archived") { try { WsDiagnosis.refreshScene(sceneId); } catch (e) {} }
  const record = scnRunRecordFromWorkbench(wb, { job: last, authorNote, pipeState });
  const budgetBlock = record.budgetBlock;
  if (!record.draft.length) {
    if (budgetBlock) {
      const error = new Error(`${budgetBlock.label}——可显式追加后从持久化检查点继续。`);
      error.code = budgetBlock.code;
      error.budgetBlock = budgetBlock;
      throw error;
    }
    // 异步任务透出结构化 missing_fields（与同步 run/full 同源）→ 引导能点名缺哪些字段
    throw scnFriendly({ code: last.error_code || "", message: last.error_text || `任务以「${last.status}」结束且没有产出正文（${last.current_step || "—"}）`, details: { missing_fields: last.missing_fields || [] } });
  }
  const secs = Math.round((Date.now() - t0) / 1000);
  const tm = (off) => new Date(t0 + off * 1000).toTimeString().slice(0, 8);
  const stepName = scnPipeStepName(pipeState);
  // reliable / 无警告路径可能已经由后端原子归档：不能把 author_state=archived 的 can_archive=false
  // 误渲染成硬问题，也不能再展示待裁决按钮。
  record.state = pipeState === "archived" ? "archived" : "ready";
  record.log = [
    { t: tm(0), who: "system", text: "已提交后端起草任务（预检 → 蓝图 → 首稿 → 硬质检 → 风格稿 → 软质检 → 近终稿）" },
    authorNote ? { t: tm(0), who: "system", text: "改写指令已随任务下发（注入风格生成阶段，优先级最高）" } : null,
    { t: tm(secs), who: "pipeline", text: `管线结束：${RUN_JOB_STATUS_LABELS[last.status] || "已结束"}${stepName ? `，停在「${stepName}」` : ""}${record.draftMode ? `，首稿 ${record.draftMode === "style_first" ? "作者手笔" : "中性"}` : ""}，${record.words} 字，用时 ${secs} 秒` },
    budgetBlock
      ? { t: tm(secs), who: "pipeline", text: `${budgetBlock.label}；已有正文与恢复点均已保留，需作者显式追加预算后续跑` }
      : scnGateLog(record.gate, tm(secs)),
    record.styleNotices.length
      ? { t: tm(secs), who: "pipeline", text: `风格提示 ${record.styleNotices.length} 条：${record.styleNotices.map(scnStyleNoticeLabel).join("；")}` }
      : null,
    scnFidelityLogText(record.styleFidelity)
      ? { t: tm(secs), who: "pipeline", text: `${scnFidelityLogText(record.styleFidelity)}（证据栏「像不像」）` }
      : null,
    record.styleWindows
      ? { t: tm(secs), who: "pipeline", text: `本场提示放入参考书原文窗口 ${record.styleWindows.windows.length} 个（${record.styleWindows.windows.reduce((sum, w) => sum + (w.chars || 0), 0)} 字），见证据栏「本场参考窗口」` }
      : null,
  ].filter(Boolean);
  record.cost = [
    { k: "用时", v: `${secs} 秒`, mono: true },
    { k: "字数", v: String(record.words), mono: true },
  ];
  return record;
}

/* ---- 后端水合：本地没有运行记录（换浏览器 / 页面关闭前没取回）时，
   从 scenes workbench 恢复这一场的最新产出为一条可裁决的运行。
   后端 SceneRunState 才是管线真相；目录场景卡已 done 的按已归档呈现。 ---- */
async function scnHydrateFromBackend(sid, { signal, terminalJob } = {}) {
  scnThrowIfAborted(signal);
  const sceneId = await scnSceneIdOf(sid);
  scnThrowIfAborted(signal);
  if (!sceneId) return null;
  let wb = null;
  try {
    wb = signal
      ? await apiGet(`/api/v1/scenes/${sceneId}/workbench`, { signal })
      : await apiGet(`/api/v1/scenes/${sceneId}/workbench`);
  } catch (e) {
    scnThrowIfAborted(signal);
    return null;
  }
  const authorNote = terminalJob && typeof terminalJob.author_note === "string" ? terminalJob.author_note : "";
  const record = scnRunRecordFromWorkbench(wb, { job: terminalJob, authorNote });
  const now = new Date().toTimeString().slice(0, 8);
  if (!record.draft.length) {
    // 新浏览器没有本地运行记录。预算断点可能在产出首稿之前就停下，但持久化检查点与作者指令都还能续跑：
    // 返回这份恢复态，不要抹掉作者唯一的「追加预算」出口。
    const budgetBlock = record.budgetBlock;
    if (!budgetBlock) return null;
    return {
      ...record,
      state: "queued",
      attempts: [{
        n: 1,
        time: "后端恢复",
        result: "等待追加预算",
        tone: "gold",
        note: authorNote ? "原作者指令已从任务恢复" : "持久化检查点可续跑",
      }],
      attempt: 1,
      at: Date.now(),
      error: `${budgetBlock.label}；尚未产出正文，检查点与原作者指令已保留。请追加预算后继续。`,
      log: [{ t: now, who: "pipeline", text: `${budgetBlock.label}；尚未产出正文，检查点与原作者指令已从后端恢复` }],
      cost: [],
      recoveredWithoutDraft: true,
    };
  }
  const hit = WsCatalog.sceneById(sid);
  const done = !!(hit && hit.scene && hit.scene.state === "done") || record.pipeState === "archived";
  const stepName = scnPipeStepName(record.pipeState);
  record.state = done ? "archived" : "ready";
  record.attempt = 1;
  record.at = Date.now();
  record.attempts = [{ n: 1, time: "后端恢复", result: done ? "已归档" : "待裁决", tone: done ? "sage" : "gold", note: "从后端管线取回的最新产出" }];
  record.log = [
    { t: now, who: "system", text: `已从后端取回这一场的最新产出${stepName ? `（管线停在「${stepName}」）` : ""}——运行在别处完成，或页面关闭前没来得及取回` },
    scnGateLog(record.gate, now),
  ].filter(Boolean);
  record.cost = [
    { k: "来源", v: "从后端取回" },
    { k: "字数", v: String(record.words), mono: true },
  ];
  return record;
}

/* ---- 队列成员的后端派生：项目内进过管线的场（GET /scene-run-states，scene_status 已离开 ready）→ sid 列表。
   本地队列从此只是这份管线真相的读缓存，换浏览器时队列成员可恢复。
   读不到时返回 null 而不是 []——场景页据此决定「这一场没进过管线、不必问 latest」，读不到就不能这么断定。 ---- */
async function scnBackendRunSids() {
  const workId = WsWorks.activeId();
  if (!workId || workId === "__loading__") return null;
  let data = null;
  try { data = await apiGet(`/api/v1/scene-run-states?project_id=${encodeURIComponent(workId)}`); } catch (e) { return null; }
  const items = (data && data.items) || [];
  if (!items.length) return [];
  try {
    if (!WsCatalog.get().length) await WsCatalog.__refresh(workId);
  } catch (e) {}
  const bySceneId = {};
  try {
    WsCatalog.get().forEach(c => (c.scenes || []).forEach(s => { if (s.backendId) bySceneId[s.backendId] = s.sid; }));
  } catch (e) {}
  // 端点按 updated_at 倒序返回：最近有动静的场排前面
  return items.map(it => bySceneId[it.scene_id]).filter(Boolean);
}
async function scnBackendQueueSids() {
  return (await scnBackendRunSids()) || [];
}

/* ---- 生命周期预算追加：显式、带理由，从持久化检查点继续 ---- */
async function scnTopupBudget(sid, budgetBlock) {
  const sceneId = await scnRequireSceneId(sid);
  const raw = budgetBlock && budgetBlock.topup && typeof budgetBlock.topup === "object" ? budgetBlock.topup : {};
  const topup = Object.fromEntries(Object.entries(raw).filter(([, value]) => Number.isInteger(value) && value > 0));
  if (!Object.keys(topup).length) throw new Error("后端没有给出可以追加的预算量，这一场暂时不能续跑；刷新后再试。");
  return apiPost(`/api/v1/scenes/${sceneId}/budget/topup`, {
    ...topup,
    reason: "作者在起草台确认追加生命周期预算并从持久化检查点继续",
  });
}

/* ---- 候选终选（Wave 3 · 治理 §5.5）----
   关键场景管线暂停在 awaiting_author_choice：盲化候选（后端 blinded_order 随机序、默认无分数）
   → 作者整稿选择 → resume 从批判修订 / 质检续跑到归档。
   终选一次写入：改选须显式 reopen（后端锁定，SELECTION_LOCKED 上抛）。 ---- */
async function scnCandidates(sid) {
  const sceneId = await scnRequireSceneId(sid);
  return apiGet(`/api/v1/scenes/${sceneId}/style-candidates`);
}
async function scnSelectCandidate(sid, rowId, opts) {
  const sceneId = await scnRequireSceneId(sid);
  return apiPost(`/api/v1/scenes/${sceneId}/style-candidates/${encodeURIComponent(rowId)}/select`, opts || {});
}
async function scnResumeAfterSelection(sid) {
  const sceneId = await scnRequireSceneId(sid);
  return apiPost(`/api/v1/scenes/${sceneId}/resume-after-selection`, {});
}
/* 终选续跑之后取回这一场：续跑又撞上预算边界时，断点记成「终选续跑」，追加后走 resume 而不是重新起草。 */
async function scnHydrateAfterSelection(sid, resumed) {
  const block = resumed && resumed.lifecycle_budget_block;
  const fresh = await scnHydrateFromBackend(sid, block ? {
    terminalJob: { error_code: block.code, error_text: block.message },
  } : undefined);
  if (fresh && block && fresh.budgetBlock) fresh.budgetBlock = { ...fresh.budgetBlock, resumeMode: "selection" };
  return fresh;
}

/* ---- 归档（治理 §5.2 归档单入口）----
   「完成」的真值在后端：POST adopt-current 携带浏览器当前正文和作者稿 base revision，服务端在同一事务内
   保存并提升精确修订。成功响应后只吸收权威修订到写作器缓存、回写字数、目录卡置 done——done 只由服务端
   archived 响应映射，不先本地置位。后端拒绝时不动本地任何状态，如实返回失败原因。 ---- */
function scnEscape(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function scnDraftHTML(draft) {
  return (draft || []).map(p => "<p>" + scnEscape(scnParaText(p)) + "</p>").join("");
}
function scnHTMLParas(raw) {
  if (!raw) return [];
  const node = document.createElement("div");
  node.innerHTML = raw;
  const items = [...node.querySelectorAll("p, li")].map(el => (el.textContent || "").trim()).filter(Boolean);
  return items.length ? items : ((node.textContent || "").trim() ? [(node.textContent || "").trim()] : []);
}
function scnAdoptionPreview(sid, draft) {
  const html = scnDraftHTML(draft);
  let existing = "";
  try { existing = WrRecovery.current(sid) || localStorage.getItem(wsKey("wr-doc:" + sid)) || ""; } catch (e) {}
  // 旧草稿开头可能留着空白页占位句：只去掉开头那一句再看有没有字。过去整份草稿里只要出现过这句话
  // 就当成空稿，作者正文后文恰好写到它时，AI 稿会不经确认直接覆盖。existing 本身不改（备份要逐字一致）。
  return {
    sid,
    html,
    existing,
    hasReal: hasAuthorText(existing),
    diff: WrDocVersions.diff(scnHTMLParas(stripLegacyDraftPlaceholder(existing)), scnHTMLParas(html)),
  };
}
async function scnPrepareAdoption(sid, draft) {
  try { await WrDocs.hydrate(sid); } catch (e) {
    throw Object.assign(new Error("无法核对服务器上的作者稿，已停止采用；请检查网络后重试"), {
      code: "AUTHOR_DRAFT_PREFLIGHT_FAILED",
      cause: e,
    });
  }
  return scnAdoptionPreview(sid, draft);
}
async function scnAdoptToDoc(sid, draft, gate, options = {}) {
  if (!sid) return { ok: false, reason: "没有场景卡" };
  // 只有真实 Q0/Q1 阻断归档——gate 前置拦截给即时反馈，后端 adopt-current 的 HARD_BLOCKED 409 仍是权威裁决
  // （绕过前端也拦得住）。显式「存为候选」不归档、不碰作者正文，被拦下的稿也可以存下来让作者自己接手。
  if (options.mode !== "candidate" && gate && gate.canArchive === false) {
    const count = (gate.blocking || []).length;
    return { ok: false, reason: `${count ? `有 ${count} 条` : "有"}已证实的硬问题，暂不能归档——正文已保留，处理或重跑后再采纳` };
  }
  const preview = await scnPrepareAdoption(sid, draft);
  const html = preview.html;
  const text = (draft || []).map(scnParaText).join("");
  // API 层也采用安全默认：任何未声明模式的调用，只要检测到作者正文，都先保存为候选。
  // 显式 overwrite 才可能进入覆盖路径，避免未来新增入口绕过页面对话框后又退回直接覆盖。
  const requestedMode = options.mode;
  if (requestedMode && !["candidate", "overwrite"].includes(requestedMode)) {
    return { ok: false, reason: "未知的采用模式，已停止以保护作者稿" };
  }
  const mode = requestedMode || (preview.hasReal ? "candidate" : "overwrite");
  if (mode === "candidate") {
    const candidate = WrRecovery.createCandidate(sid, html, "AI 起草台候选；未覆盖作者当前正文，也未归档");
    return {
      ok: true,
      archived: false,
      mode: "candidate",
      candidate,
      warning: candidate.durable === false ? "浏览器空间不足，候选仅保留在本次会话，请立即导出" : null,
    };
  }
  if (preview.hasReal && mode === "overwrite" && options.confirmed !== true) {
    return {
      ok: false,
      reason: "需要先查看差异并明确确认覆盖；作者稿没有被改动",
      confirmationRequired: true,
    };
  }
  let authorBackup = null;
  if (preview.hasReal) {
    try {
      const currentWorkId = WsWorks.activeId();
      authorBackup = options.authorBackupId
        ? WrRecovery.list().find(item => (
            item.id === options.authorBackupId
            && item.type === "backup"
            && item.source === "author"
            && item.sid === sid
            && item.workId === currentWorkId
            && item.html === preview.existing
            && item.durable !== false
          )) || null
        : null;
      if (!authorBackup) {
        authorBackup = WrRecovery.createBackup(sid, preview.existing, "AI 稿确认覆盖前自动备份作者正文");
      }
    } catch (error) {
      return { ok: false, reason: (error && error.message) || "作者稿备份失败，已停止覆盖", backupFailed: true };
    }
  }
  // 1) 后端归档单入口：确切 HTML + 作者稿 revision + 当前 FinalScene 指针在一个事务中完成保存与提升，
  //    不再让服务端自行猜测浏览器选中了哪份稿。
  let sceneId = null;
  try { sceneId = await scnSceneIdOf(sid); } catch (e) {}
  if (!sceneId) return { ok: false, reason: "这一场还没同步到后端目录——稍候片刻或刷新后重试" };
  const docState = WrDocs.state(sid);
  if (!docState || !docState.draftId || !Number.isInteger(docState.revision) || docState.revision < 1) {
    return { ok: false, reason: "无法取得服务器作者稿修订，已停止归档以避免正文错位" };
  }
  let adoption = null;
  try {
    adoption = await apiPost(`/api/v1/scenes/${sceneId}/adopt-current`, {
      accepted_warning_codes: Array.isArray(options.acceptedWarningCodes) ? options.acceptedWarningCodes : [],
      exact_author_draft: {
        draft_id: docState.draftId,
        base_revision_no: docState.revision,
        expected_current_final_scene_row_id: docState.currentFinalSceneRowId || null,
        content: html,
      },
    });
  } catch (e) {
    // 抄袭门拦下（与参考书原文连续相同 / 用了它的专名）：说成作者读得懂的话，只给处数，不给参考原文
    if (isCopyGateError(e)) return { ok: false, reason: copyGateAdoptMessage(e), error: e, authorBackup, copyBlocked: true };
    const code = (e && e.code) || "";
    const msg = (e && e.message) || String(e || "");
    return { ok: false, reason: `后端归档未通过（${code || "网络错误"}）：${msg}`, error: e, authorBackup };
  }
  // 2) 服务端已经保存并归档同一修订；这里只吸收回包，不再 PATCH 新修订。
  let cacheWarning = null;
  try {
    const synced = WrDocs.acceptCanonical(sid, html, adoption);
    if (synced && synced.localDurable === false) cacheWarning = "正文已安全归档到服务器，但浏览器缓存写入失败；刷新后可从服务器恢复";
  } catch (e) {
    cacheWarning = "正文已安全归档到服务器，但本地状态同步失败；请刷新页面从服务器恢复";
  }
  const hit = WsCatalog.sceneById(sid);
  const prev = hit && typeof hit.scene.words === "number" ? hit.scene.words : 0;
  const count = text.replace(/\s/g, "").length;
  try { WsCatalog.recordSceneWords(sid, count, prev); } catch (e) {}
  try {
    WsCatalog.set(WsCatalog.get().map(c => ({
      ...c, scenes: (c.scenes || []).map(s => s.sid === sid ? { ...s, state: "done" } : s),
    })));
  } catch (e) {}
  // 服务端改了这一场的正文：只拉这一章的诊断计数（主页 / 成稿中心的角标），不拉整本书
  try { WsDiagnosis.refreshScene(sceneId); } catch (e) {}
  // 3) 归档后重新拉服务端状态（起草台运行记录与管线真相收敛）
  try {
    const status = await apiGet(`/api/v1/scenes/${sceneId}/status`);
    return { ok: true, archived: true, words: count, authorBackup, cacheWarning, contentHash: adoption && adoption.content_hash, serverStatus: (status && status.scene_status) || "archived", authorState: status && status.author_state };
  } catch (e) {
    return { ok: true, archived: true, words: count, authorBackup, cacheWarning, contentHash: adoption && adoption.content_hash, serverStatus: "archived" };
  }
}

/* 参考窗口原文（证据栏展开时才取；最多 80 段，后端 capped 标记截断） */
function scnFetchStyleWindowText(bookId, w) {
  const query = `start=${encodeURIComponent(w.start)}&end=${encodeURIComponent(w.end)}`;
  return apiGet(`/api/v2/style-reference/books/${encodeURIComponent(bookId)}/paragraphs?${query}`);
}

export {
  scnRun, scnHydrateFromBackend, scnBackendRunSids, scnBackendQueueSids, scnTopupBudget,
  scnCandidates, scnSelectCandidate, scnResumeAfterSelection, scnHydrateAfterSelection,
  scnAdoptionPreview, scnPrepareAdoption, scnAdoptToDoc, scnFetchStyleWindowText,
};
