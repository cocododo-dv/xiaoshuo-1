import { apiGet, apiPatch, apiPost } from "./lib/client.js";
import { storeAlert } from "./lib/store-utils.js";
import { manuscriptToDocHTML, sanitizeManuscriptHTML } from "./manuscript-html.js";
import { WsDiagnosis } from "./ws-diagnosis-summary.jsx";
import { wsToast } from "./ws-notify.jsx";

/* ==========================================================
   WrDocs — 写作器正文文档 store（FE-ALIGN Phase 3）
   ----------------------------------------------------------
   正文真相源 = author-drafts 主路径（POST ensure + PATCH /{draft_id}），
   字数统计与目录 rollup 由保存响应回流（words_rollup）。
   localStorage 的 wr-doc:<sid> 键退化为「同步读缓存」：写作器在
   render/effect 里同步取文档，API 负责水合与持久化；跨浏览器以
   服务端为准（水合覆盖缓存，本地未保存改动优先）。
   sid = 目录 slug（ch08s3）；后端 scene_id 经 WsCatalog 映射。
   冲突（409 AUTHOR_DRAFT_CONFLICT）：以服务端为准重新水合。
   ========================================================== */

const wrKeyOf = (sid) => (window.wsKey ? window.wsKey("wr-doc:" + sid) : "wr-doc:" + sid);
// Wave 1（治理 · 设计项 5）：保存失败的持久化标记——dirty 只在内存 docMeta，
// 重启浏览器即丢，下次水合会用服务端旧版静默覆盖较新的本地稿。标记跨会话存活，
// 启动水合时据此走「冲突副本 + 作者选择」而不是静默覆盖。
const wrPendingKeyOf = (sid) => (window.wsKey ? window.wsKey("wr-doc-pending:" + sid) : "wr-doc-pending:" + sid);
const WR_RECOVERY_PREFIX = "wr-recovery:v1:";
const volatileDocs = new Map();
const volatileRecoveries = new Map();

function activeWorkId() {
  try { return (window.WsWorks && window.WsWorks.activeId && window.WsWorks.activeId()) || ""; } catch (e) { return ""; }
}

function isStorageQuotaError(error) {
  return !!(error && (
    error.name === "QuotaExceededError"
    || error.name === "NS_ERROR_DOM_QUOTA_REACHED"
    || error.code === 22
    || error.code === 1014
  ));
}

function storageFailure(error, message = "浏览器本地存储空间不足") {
  return Object.assign(new Error(message), {
    code: isStorageQuotaError(error) ? "LOCAL_STORAGE_QUOTA" : "LOCAL_STORAGE_UNAVAILABLE",
    cause: error,
  });
}

function notifyRecoveryChanged(entry, action = "changed") {
  try {
    window.dispatchEvent(new CustomEvent("ws:recovery-changed", { detail: { action, entry } }));
  } catch (e) {}
}

function recoveryCreate({ sid, html, type = "conflict", reason = "", label = "", source = "writer", requireDurable = false } = {}) {
  const createdAt = Date.now();
  const id = `${createdAt.toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const entry = {
    id,
    version: 1,
    workId: activeWorkId(),
    sid: sid || "",
    type,
    reason,
    label: label || (sid ? `场景 ${sid}` : "未命名稿件"),
    source,
    createdAt,
    html: sanitizeManuscriptHTML(html || ""),
    durable: true,
  };
  try {
    localStorage.setItem(WR_RECOVERY_PREFIX + id, JSON.stringify(entry));
  } catch (error) {
    entry.durable = false;
    entry.storageError = storageFailure(error).code;
    volatileRecoveries.set(id, entry);
    notifyRecoveryChanged(entry, "created");
    if (requireDurable) throw storageFailure(error, "本地备份空间不足，已停止覆盖；请先导出或清理恢复记录");
    return entry;
  }
  notifyRecoveryChanged(entry, "created");
  return entry;
}

function parseLegacyRecovery(key) {
  if (!key || !key.includes(":conflict-")) return null;
  const stamp = Number((key.match(/:conflict-(\d+)$/) || [])[1]) || Date.now();
  const head = key.replace(/:conflict-\d+$/, "");
  const match = /(?:^|:)wr-doc:([^:]+)(?:::([^:]+))?$/.exec(head);
  if (!match) return null;
  let html = "";
  try { html = localStorage.getItem(key) || ""; } catch (e) {}
  return {
    id: "legacy:" + key,
    version: 0,
    storageKey: key,
    workId: match[2] || "",
    sid: match[1] || "",
    type: "conflict",
    reason: "旧版冲突副本",
    label: `场景 ${match[1] || "未知"}`,
    source: "writer",
    createdAt: stamp,
    html,
    durable: true,
  };
}

function recoveryList() {
  const entries = new Map();
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (!key) continue;
      if (key.startsWith(WR_RECOVERY_PREFIX)) {
        try {
          const value = JSON.parse(localStorage.getItem(key) || "null");
          if (value && value.id) entries.set(value.id, { ...value, durable: true });
        } catch (e) {}
      } else {
        const legacy = parseLegacyRecovery(key);
        if (legacy) entries.set(legacy.id, legacy);
      }
    }
  } catch (e) {}
  volatileRecoveries.forEach((entry, id) => entries.set(id, entry));
  return [...entries.values()].sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
}

function recoveryRemove(id) {
  const entry = recoveryList().find(item => item.id === id);
  if (!entry) return false;
  try {
    if (entry.storageKey) localStorage.removeItem(entry.storageKey);
    else localStorage.removeItem(WR_RECOVERY_PREFIX + id);
  } catch (e) {}
  volatileRecoveries.delete(id);
  notifyRecoveryChanged(entry, "removed");
  return true;
}

function pendingRead(sid) {
  try { return localStorage.getItem(wrPendingKeyOf(sid)); } catch (e) { return null; }
}
function pendingWrite(sid) {
  try { localStorage.setItem(wrPendingKeyOf(sid), String(Date.now())); } catch (e) {}
}
function pendingClear(sid) {
  try { localStorage.removeItem(wrPendingKeyOf(sid)); } catch (e) {}
}

// 作品id::sid → { draftId, revision, hydrated, dirty, chain }
// 必须带作品前缀：同名 slug（ch01s1）在每部作品都存在，裸 sid 会把
// PATCH 打到上一部作品的 draft（跨作品数据污染）。
const docMeta = {};

function metaKeyOf(sid) {
  let work = "";
  try { work = (window.WsWorks && window.WsWorks.activeId()) || ""; } catch (e) {}
  return work + "::" + sid;
}

function meta(sid) {
  const key = metaKeyOf(sid);
  return docMeta[key] || (docMeta[key] = {
    draftId: null,
    revision: 0,
    hydrated: false,
    dirty: false,
    saveVersion: 0,
    chain: Promise.resolve(),
    serverContent: "",
    currentFinalSceneRowId: null,
    lastPromotedRevisionNo: null,
    lastPromotedFinalSceneRowId: null,
    canonicalDirty: true,
    lastSaveError: null,
    cacheError: null,
    localDurable: true,
  });
}

function finalIdFromRef(ref) {
  if (typeof ref !== "string" || !ref.startsWith("final_scene:")) return null;
  return ref.slice("final_scene:".length) || null;
}

function absorbServerState(m, data) {
  const draft = data && data.draft;
  if (draft) {
    if (draft.draft_id) m.draftId = draft.draft_id;
    if (Number.isInteger(draft.revision_no)) m.revision = draft.revision_no;
    if (Object.prototype.hasOwnProperty.call(draft, "content")) m.serverContent = sanitizeManuscriptHTML(draft.content || "");
    if (Object.prototype.hasOwnProperty.call(draft, "last_promoted_revision_no")) {
      m.lastPromotedRevisionNo = draft.last_promoted_revision_no;
    }
    if (Object.prototype.hasOwnProperty.call(draft, "last_promoted_final_scene_row_id")) {
      m.lastPromotedFinalSceneRowId = draft.last_promoted_final_scene_row_id;
    }
    if (typeof draft.canonical_dirty === "boolean") m.canonicalDirty = draft.canonical_dirty;
    else if (Number.isInteger(draft.revision_no)) m.canonicalDirty = draft.revision_no !== m.lastPromotedRevisionNo;
  }
  if (data && Object.prototype.hasOwnProperty.call(data, "runtime_final_ref")) {
    m.currentFinalSceneRowId = finalIdFromRef(data.runtime_final_ref);
  }
}

function stateSnapshot(sid) {
  const m = meta(sid);
  return {
    draftId: m.draftId,
    revision: m.revision,
    dirty: m.dirty,
    canonicalDirty: m.canonicalDirty,
    currentFinalSceneRowId: m.currentFinalSceneRowId,
    lastPromotedRevisionNo: m.lastPromotedRevisionNo,
    lastPromotedFinalSceneRowId: m.lastPromotedFinalSceneRowId,
    lastSaveError: m.lastSaveError,
    cacheError: m.cacheError,
    localDurable: m.localDurable,
  };
}

function notifyState(sid) {
  try {
    window.dispatchEvent(new CustomEvent("ws:wr-doc-state", { detail: { sid, ...stateSnapshot(sid) } }));
  } catch (e) {}
}

function cacheRead(sid) {
  const memoryKey = metaKeyOf(sid);
  if (volatileDocs.has(memoryKey)) return volatileDocs.get(memoryKey);
  try {
    const value = localStorage.getItem(wrKeyOf(sid));
    return value == null ? null : value;
  } catch (e) {
    return null;
  }
}

// 恢复中心会同时列出多部作品的记录。查看另一部作品时，差异必须和
// 那部作品自己的缓存比较，不能误拿当前作品的同名 sid 当基线。
function cacheReadForWork(sid, workId) {
  const currentWork = activeWorkId();
  if (!workId || workId === currentWork) return cacheRead(sid);
  const memoryKey = `${workId}::${sid}`;
  if (volatileDocs.has(memoryKey)) return volatileDocs.get(memoryKey);
  try {
    const value = localStorage.getItem(`wr-doc:${sid}::${workId}`);
    return value == null ? null : value;
  } catch (e) { return null; }
}
function cacheWrite(sid, html) {
  const safeHTML = sanitizeManuscriptHTML(html);
  volatileDocs.set(metaKeyOf(sid), safeHTML);
  try {
    localStorage.setItem(wrKeyOf(sid), safeHTML);
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error: storageFailure(error) };
  }
}

/* 本地稿被放进「同步与恢复」时告诉作者一声，并给一个直接打开它的按钮（入口在左侧导航栏底部）。
   外壳的提示层没挂上时（单测里单独加载 store）退回浏览器提示框。 */
function recoveryNotice(sid, message) {
  const shown = wsToast({
    message,
    tone: "warn",
    timeout: 12000,
    action: {
      label: "打开同步与恢复",
      onClick: () => { try { window.dispatchEvent(new CustomEvent("ws:recovery-open", { detail: { sid } })); } catch (e) {} },
    },
  });
  if (!shown) { try { window.alert(message); } catch (e) {} }
}

function notifyLoaded(sid) {
  try { window.dispatchEvent(new CustomEvent("ws:wr-doc-loaded", { detail: sid })); } catch (e) {}
}

async function backendSceneId(sid) {
  const cat = window.WsCatalog;
  if (!cat || !cat.__backendSceneId) return null;
  try { return await cat.__backendSceneId(sid); } catch (e) { return null; }
}

/* 文本 → 文档 HTML（服务端草稿以 \n 分段；写作器编辑器吃 <p> 段落） */
function toDocHTML(content) {
  return manuscriptToDocHTML(content);
}

/* HTML → 存库内容：原样存 HTML（count_words 服务端会剥标签） */

// 作品id::sid → 正在进行的 ensure。同一场同一时刻只发一次 POST ensure：版本列表、
// 某一版正文、draftId 常被同时调用（开发模式下 React 还会把挂载 effect 连跑两遍），
// 两个 ensure 带着同一个幂等键撞在一起，后一个会拿到 409 IDEMPOTENCY_REQUEST_IN_PROGRESS。
// 请求结束（成功或失败）即移除，之后的调用重新请求。
const ensureInflight = new Map();

async function ensureDraft(sid) {
  const m = meta(sid);
  if (m.draftId) return m;
  const key = metaKeyOf(sid);
  const pending = ensureInflight.get(key);
  if (pending) return pending;
  const request = (async () => {
    const sceneId = await backendSceneId(sid);
    if (!sceneId) return m;
    const data = await apiPost(`/api/v1/author-drafts/scene/${sceneId}/ensure`, {});
    absorbServerState(m, data);
    notifyState(sid);
    return m;
  })();
  const shared = request.finally(() => {
    if (ensureInflight.get(key) === shared) ensureInflight.delete(key);
  });
  ensureInflight.set(key, shared);
  return shared;
}

async function hydrate(sid) {
  const m = meta(sid);
  if (m.hydrated || m.hydrating) return;
  m.hydrating = true;
  try {
    await ensureDraft(sid);
    if (m.draftId != null && !m.dirty) {
      const html = toDocHTML(m.serverContent || "");
      const cached = cacheRead(sid);
      // Wave 1：上个会话保存失败留下的 pending 标记——本地较新稿不得被静默覆盖
      if (pendingRead(sid) != null) {
        if (cached != null && cached !== html) {
          const backup = recoveryCreate({
            sid,
            html: cached,
            type: "conflict",
            reason: "上次会话未同步，服务端已有不同版本",
            label: `场景 ${sid} · 未同步本地稿`,
          });
          if (!backup.durable) {
            // 配额不足时不能先覆盖再告诉作者“备份失败”。保留当前缓存为工作稿，
            // 服务端版本已经在 m.serverContent，可待作者清理空间后比较/重试。
            m.hydrated = true;
            m.dirty = true;
            m.localDurable = false;
            m.cacheError = Object.assign(new Error("本地恢复空间不足，未覆盖你的本地稿"), { code: backup.storageError || "LOCAL_STORAGE_QUOTA" });
            m.lastSaveError = m.cacheError;
            storeAlert(null, "发现未同步的本地正文，但浏览器存储空间不足。系统没有覆盖本地稿；请从左侧导航栏底部打开「同步与恢复」，导出内容或清理旧记录后重试。");
            notifyState(sid);
            return;
          }
          pendingClear(sid);
          recoveryNotice(sid, "上次会话有没保存到服务端的本地正文，已加载服务端版本。你的本地稿放进了「同步与恢复」，可以比较差异、恢复或导出。");
        } else {
          pendingClear(sid); // 内容一致（上次实际保上了）：静默消费标记
        }
      }
      if (html && html !== cached) {
        const cachedResult = cacheWrite(sid, html);
        m.localDurable = cachedResult.ok;
        m.cacheError = cachedResult.error;
        notifyLoaded(sid);
      } else if (!html && cached == null) {
        // 服务端空白草稿：保持缓存为空（视图显示开场占位）
      }
      m.hydrated = true;
    } else if (m.draftId != null) {
      m.hydrated = true; // 本地有未保存改动：本地优先，保存时带 revision 上行
    }
  } catch (e) {
    console.warn("[WrDocs] 文档水合失败:", sid, e);
  } finally {
    m.hydrating = false;
  }
}

async function pushSave(sid, html, saveVersion) {
  const m = meta(sid);
  html = sanitizeManuscriptHTML(html);
  try {
    await ensureDraft(sid);
    if (!m.draftId) {
      throw Object.assign(new Error("场景尚未就绪，草稿未保存到服务端"), { code: "AUTHOR_DRAFT_UNAVAILABLE" });
    }
    const data = await apiPatch(`/api/v1/author-drafts/${m.draftId}`, {
      content: html,
      base_revision_no: m.revision,
    });
    absorbServerState(m, data);
    const isLatestSave = saveVersion === m.saveVersion;
    m.dirty = !isLatestSave;
    if (isLatestSave) {
      m.lastSaveError = null;
      pendingClear(sid); // 只有最新版本成功，才能消费跨会话失败标记
    } else {
      pendingWrite(sid);
    }
    if (data && data.words_rollup && window.WsCatalog && window.WsCatalog.__applyWordsRollup) {
      window.WsCatalog.__applyWordsRollup(sid, data.words_rollup);
    }
    /* 2026-09-22 场景诊断第三轮：正文一存，服务端把这一场 / 这一章开着的发现数带回来，角标随之更新 */
    if (data && data.diagnosis_rollup) {
      try { WsDiagnosis.applyRollup(data.diagnosis_rollup); } catch (e) {}
    }
    notifyState(sid);
    return data;
  } catch (e) {
    m.lastSaveError = e;
    if (e && e.code === "AUTHOR_DRAFT_CONFLICT") {
      if (saveVersion < m.saveVersion) {
        // 旧请求冲突时，队列里已经有更新的本地正文。只刷新服务端 revision，
        // 绝不能按旧请求的 html 做恢复副本或把服务端内容覆盖到最新本地缓存。
        m.draftId = null;
        m.hydrated = false;
        m.dirty = true;
        pendingWrite(sid);
        await hydrate(sid);
        notifyState(sid);
        throw e;
      }
      // 服务端已被改（另一端保存）：以服务端为准重新水合。
      // 审计 P-12：覆盖前把本地未保存稿留一份副本，避免较新的本地编辑无痕丢失。
      const backup = recoveryCreate({
        sid,
        html,
        type: "conflict",
        reason: "服务端在别处更新（409 冲突）",
        label: `场景 ${sid} · 冲突本地稿`,
      });
      if (!backup.durable) {
        // 无持久备份就不允许水合覆盖当前缓存；留在 dirty 状态供作者导出。
        m.dirty = true;
        m.localDurable = false;
        m.cacheError = Object.assign(new Error("冲突稿无法持久备份，已停止覆盖"), { code: backup.storageError || "LOCAL_STORAGE_QUOTA" });
        pendingWrite(sid);
        storeAlert(null, "正文发生版本冲突，同时浏览器存储空间不足。系统已停止覆盖，本地稿仍在当前编辑器；请先从左侧导航栏底部的「同步与恢复」导出或清理恢复记录。");
        notifyState(sid);
        throw e;
      }
      m.draftId = null;
      m.hydrated = false;
      m.dirty = false;
      pendingClear(sid); // 409 路径已自带冲突副本，勿让水合再重复备份
      await hydrate(sid);
      recoveryNotice(sid, "这份正文在别处被修改过，已加载服务端的最新版本。你本地没保存上的内容放进了「同步与恢复」，可以比较差异、恢复或导出。");
    } else {
      // Wave 1：非 409 失败留持久化标记——重启后水合据此走冲突副本而非静默覆盖
      pendingWrite(sid);
      if (!m.localDurable) {
        recoveryCreate({
          sid,
          html,
          type: "unsynced",
          reason: "断网或服务端保存失败；浏览器缓存也不可用",
          label: `场景 ${sid} · 会话内未同步稿`,
        });
      }
      console.warn("[WrDocs] 正文保存失败（缓存已留底，下次保存重试）:", e);
    }
    notifyState(sid);
    throw e;
  }
}

/* ==========================================================
   WrDocVersions — 正文修订历史（FE-ALIGN F2）
   ----------------------------------------------------------
   成稿中心「对比」的数据源：后端每次保存都会落一行修订快照
   （author_draft_revisions），这里按 sid 取版本列表与任一版正文，
   并提供句级 diff（LCS）。draftId 复用 WrDocs 的 ensure 链路。
   ========================================================== */

function htmlToParas(raw) {
  if (!raw) return [];
  if (!/<\w+[^>]*>/.test(raw)) return String(raw).split(/\n+/).map(x => x.trim()).filter(Boolean);
  const div = document.createElement("div");
  div.innerHTML = sanitizeManuscriptHTML(raw);
  let paras = Array.from(div.querySelectorAll("p, li")).map(p => (p.textContent || "").trim()).filter(Boolean);
  if (!paras.length) {
    const t = (div.textContent || "").trim();
    paras = t ? t.split(/\n+/).map(x => x.trim()).filter(Boolean) : [];
  }
  return paras;
}

/* 句级 diff：A=旧版段落、B=新版段落 → 按 B 版式分段的 same/del/add 片段 */
function diffSentences(aParas, bParas) {
  const split = (paras) => {
    const out = [];
    (paras || []).forEach((p, pi) => {
      const parts = String(p).split(/(?<=[。！？!?；;…])/).map(s => s.trim()).filter(Boolean);
      (parts.length ? parts : [String(p)]).forEach(s => out.push({ p: pi, s }));
    });
    return out;
  };
  const A = split(aParas), B = split(bParas);
  const n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i].s === B[j].s ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const segs = [];
  let i = 0, j = 0, adds = 0, dels = 0;
  const delPara = () => (j < m ? B[j].p : (m ? B[m - 1].p : 0));
  while (i < n && j < m) {
    if (A[i].s === B[j].s) { segs.push({ t: "same", text: B[j].s, p: B[j].p }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { segs.push({ t: "del", text: A[i].s, p: delPara() }); dels++; i++; }
    else { segs.push({ t: "add", text: B[j].s, p: B[j].p }); adds++; j++; }
  }
  while (i < n) { segs.push({ t: "del", text: A[i].s, p: delPara() }); dels++; i++; }
  while (j < m) { segs.push({ t: "add", text: B[j].s, p: B[j].p }); adds++; j++; }
  const paras = [];
  segs.forEach(seg => {
    if (!paras.length || paras[paras.length - 1].p !== seg.p) paras.push({ p: seg.p, segs: [] });
    paras[paras.length - 1].segs.push(seg);
  });
  return { paras, adds, dels };
}

const WrDocVersions = {
  /* 版本列表（新→旧）：[{revisionNo, words, origin, at}]；无后端目录映射时返回 [] */
  async list(sid) {
    const m = await ensureDraft(sid);
    if (!m.draftId) return [];
    const data = await apiGet(`/api/v1/author-drafts/${m.draftId}/revisions`);
    return ((data && data.items) || []).map(r => ({
      revisionNo: r.revision_no,
      words: r.words || 0,
      origin: r.origin || "edited",
      at: r.created_at || "",
    }));
  },
  /* 某一版正文 → 段落数组（剥 HTML） */
  async paras(sid, revisionNo) {
    const m = await ensureDraft(sid);
    if (!m.draftId) return [];
    const data = await apiGet(`/api/v1/author-drafts/${m.draftId}/revisions/${revisionNo}`);
    return htmlToParas((data && data.revision && data.revision.content) || "");
  },
  diff: diffSentences,
};

const WrDocs = {
  /* 解析 sid → 后端 author-draft draft_id（不存在则 ensure 建一份空稿）；
     供"AI 续写"等需要真实 draft_id 发起 LLM 调用的功能复用同一份映射缓存。 */
  async draftId(sid) {
    if (!sid) return null;
    const m = await ensureDraft(sid);
    return m.draftId || null;
  },
  /* 同步读：返回缓存（可能为 null = 从未写过）；后台触发水合 */
  load(sid) {
    if (!sid) return null;
    hydrate(sid);
    return cacheRead(sid);
  },
  /* 显式等待服务端草稿水合；跨页面采用 AI 稿前用它确认作者正文是否已存在。 */
  async hydrate(sid) {
    if (!sid) return null;
    await hydrate(sid);
    return cacheRead(sid);
  },
  /* 写：缓存即时落地，API 串行保存（按 sid 链式，避免乱序覆盖） */
  save(sid, html) {
    if (!sid) return Promise.reject(Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" }));
    const m = meta(sid);
    const cached = cacheWrite(sid, html);
    m.localDurable = cached.ok;
    m.cacheError = cached.error;
    m.dirty = true;
    const saveVersion = ++m.saveVersion;
    m.canonicalDirty = true;
    m.lastSaveError = null;
    // 从本地写入开始就标记未同步。浏览器若在请求完成前退出，下次水合会先留恢复副本，
    // 不会把服务端旧稿静默盖回本地。
    pendingWrite(sid);
    notifyState(sid);
    // 调用方拿到本次保存的真实结果；内部队列单独吞掉失败，保证下次保存仍能继续。
    const operation = m.chain.catch(() => {}).then(() => pushSave(sid, html, saveVersion));
    m.chain = operation.catch(() => {});
    return operation;
  },
  /* 当前草稿、保存与权威正文同步状态的只读快照。 */
  state(sid) {
    if (!sid) return null;
    return stateSnapshot(sid);
  },
  /* 把已成功保存的场景草稿显式提升为权威正文。v1 仅支持“事实未变”。 */
  async promote(sid, options = {}) {
    if (!sid) throw Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" });
    const m = meta(sid);
    await m.chain;
    if (m.lastSaveError) throw m.lastSaveError;
    await ensureDraft(sid);
    if (!m.draftId) {
      throw Object.assign(new Error("场景尚未就绪，无法提升权威正文"), { code: "AUTHOR_DRAFT_UNAVAILABLE" });
    }
    if (m.dirty) {
      throw Object.assign(new Error("草稿仍有未保存改动"), { code: "AUTHOR_DRAFT_UNSAVED" });
    }
    const expectedFinal = Object.prototype.hasOwnProperty.call(options, "expectedCurrentFinalSceneRowId")
      ? options.expectedCurrentFinalSceneRowId
      : m.currentFinalSceneRowId;
    const data = await apiPost(`/api/v1/author-drafts/${m.draftId}/promote-canonical`, {
      base_revision_no: m.revision,
      expected_current_final_scene_row_id: expectedFinal == null ? null : expectedFinal,
      narrative_effect: options.narrativeEffect || "requires_reconcile",
      accepted_warning_codes: options.acceptedWarningCodes || [],
    });
    m.currentFinalSceneRowId = data.final_scene_row_id;
    m.lastPromotedRevisionNo = data.draft_revision_no;
    m.lastPromotedFinalSceneRowId = data.final_scene_row_id;
    m.canonicalDirty = Boolean(data.canonical_dirty);
    notifyState(sid);
    return data;
  },
  /* adopt-current 的 exact_author_draft 已在一个服务端事务内完成保存与提升。
     这里只吸收权威回包和刷新读缓存，绝不能再 PATCH 一次制造新修订。 */
  acceptCanonical(sid, html, data) {
    if (!sid) throw Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" });
    const m = meta(sid);
    const normalized = sanitizeManuscriptHTML(html || "");
    const serverDraft = data && data.author_draft;
    if (!serverDraft || !serverDraft.draft_id || !Number.isInteger(serverDraft.revision_no)) {
      throw Object.assign(new Error("归档响应缺少作者稿修订信息"), { code: "AUTHOR_DRAFT_ADOPTION_RESPONSE_INVALID" });
    }
    if (m.draftId && m.draftId !== serverDraft.draft_id) {
      throw Object.assign(new Error("归档响应属于另一份作者稿"), { code: "AUTHOR_DRAFT_ADOPTION_MISMATCH" });
    }
    const cached = cacheWrite(sid, normalized);
    m.localDurable = cached.ok;
    m.cacheError = cached.error;
    m.serverContent = normalized;
    m.dirty = false;
    m.lastSaveError = null;
    absorbServerState(m, {
      draft: { ...serverDraft, content: normalized },
      runtime_final_ref: data.final_scene_row_id ? `final_scene:${data.final_scene_row_id}` : null,
    });
    pendingClear(sid);
    notifyLoaded(sid);
    notifyState(sid);
    return stateSnapshot(sid);
  },
  /* 当前在写场景预热（目录装载后调用） */
  hydrateActive() {
    try {
      const w = window.WsCatalog && window.WsCatalog.writingScene();
      if (w && w.scene && w.scene.sid) hydrate(w.scene.sid);
    } catch (e) {}
  },
};

function assertRecoveryWork(entry) {
  const current = activeWorkId();
  if (entry && entry.workId && current && entry.workId !== current) {
    throw Object.assign(new Error("这份恢复稿属于另一部作品，请先切换到对应作品"), {
      code: "RECOVERY_WORK_MISMATCH",
      expectedWorkId: entry.workId,
      currentWorkId: current,
    });
  }
}

const WrRecovery = {
  list(options = {}) {
    const workId = Object.prototype.hasOwnProperty.call(options, "workId") ? options.workId : null;
    const items = recoveryList();
    return workId == null ? items : items.filter(item => item.workId === workId);
  },
  create(options) { return recoveryCreate(options); },
  createBackup(sid, html, reason = "覆盖前自动备份") {
    return recoveryCreate({
      sid,
      html,
      type: "backup",
      reason,
      label: `场景 ${sid} · 作者稿备份`,
      source: "author",
      requireDurable: true,
    });
  },
  createCandidate(sid, html, reason = "AI 候选，尚未覆盖作者稿") {
    return recoveryCreate({
      sid,
      html,
      type: "candidate",
      reason,
      label: `场景 ${sid} · AI 候选`,
      source: "ai",
    });
  },
  remove(id) { return recoveryRemove(id); },
  current(sid) { return cacheRead(sid) || ""; },
  diff(id) {
    const entry = recoveryList().find(item => item.id === id);
    if (!entry) return null;
    const current = cacheReadForWork(entry.sid, entry.workId) || "";
    return {
      entry,
      current,
      candidate: entry.html || "",
      ...diffSentences(htmlToParas(current), htmlToParas(entry.html || "")),
    };
  },
  async restore(id) {
    const entry = recoveryList().find(item => item.id === id);
    if (!entry) throw Object.assign(new Error("恢复记录已不存在"), { code: "RECOVERY_NOT_FOUND" });
    assertRecoveryWork(entry);
    const current = cacheRead(entry.sid) || "";
    const hasCurrent = htmlToParas(current).join("").replace(/\s/g, "").length > 0;
    let replacedBackup = null;
    if (hasCurrent && current !== (entry.html || "")) {
      // “恢复”本质上也是一次显式替换：先留下可撤销的当前稿，配额不足则
      // fail closed，不允许恢复工具反过来成为新的丢稿入口。
      replacedBackup = recoveryCreate({
        sid: entry.sid,
        html: current,
        type: "backup",
        reason: `恢复“${entry.label || entry.sid}”前自动备份当前正文`,
        label: `场景 ${entry.sid} · 作者稿备份`,
        source: "author",
        requireDurable: true,
      });
    }
    await WrDocs.save(entry.sid, entry.html || "");
    notifyLoaded(entry.sid);
    notifyRecoveryChanged(entry, "restored");
    return { entry, replacedBackup, state: WrDocs.state(entry.sid) };
  },
  async retry(id) {
    const result = await WrRecovery.restore(id);
    recoveryRemove(id);
    return result;
  },
};

Object.assign(window, { WrDocs, WrDocVersions, WrRecovery });

export { WrDocs, WrDocVersions, WrRecovery };
