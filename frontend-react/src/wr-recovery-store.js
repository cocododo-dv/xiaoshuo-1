import { sanitizeManuscriptHTML } from "./manuscript-html.js";
import { randomSuffix } from "./lib/ids.js";
import { emit } from "./lib/events.js";
import { WsWorks } from "./ws-works.jsx";

/* ==========================================================
   同步与恢复 · 本机的恢复记录（2026-09-29 从 wr-doc-store.jsx 原样搬出）
   冲突副本、未同步稿、覆盖前备份、AI 候选：存 localStorage 的 wr-recovery:v1:<id>，配额不足时退到会话内存。
   旧版「wr-doc:<sid>::<work>:conflict-<t>」键照旧列出。每次变化广播 ws:recovery-changed。
   ========================================================== */

const WR_RECOVERY_PREFIX = "wr-recovery:v1:";
const volatileRecoveries = new Map();

/* 当前作品 id（本机键 / 内存表的命名空间，加载占位也照用）。try 是有用的：不少单测 mock 的 WsWorks 没有 activeId */
function activeWorkId() {
  try { return WsWorks.activeId() || ""; } catch (e) { return ""; }
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
  emit("ws:recovery-changed", { action, entry });
}

/* workId 省略 = 当前作品。正文 store 的异步收尾（409 之后的冲突副本等）显式给出这一场所属的作品：
   那时作者可能已经切到另一部作品，记录不能挂到那一部名下。 */
function recoveryCreate({ sid, html, type = "conflict", reason = "", label = "", source = "writer", requireDurable = false, workId } = {}) {
  const createdAt = Date.now();
  const id = `${createdAt.toString(36)}-${randomSuffix(6)}`;
  const entry = {
    id,
    version: 1,
    workId: workId == null ? activeWorkId() : workId,
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

export {
  activeWorkId, isStorageQuotaError, storageFailure, notifyRecoveryChanged, recoveryCreate, recoveryList, recoveryRemove,
};
