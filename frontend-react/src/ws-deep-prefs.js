import { wsKey } from "./ws-works.jsx";
import { apiGet, apiPatch, apiPost } from "./lib/client.js";

/* ==========================================================
   深改面板的数据（2026-09-29 从 ws-deep.jsx 拆出）
   ----------------------------------------------------------
   · 决定日志 / 忽略清单：按场景写穿到本机（wr-deep-log: / wr-deep-skip:），服务端 deep-review/preferences
     是真相（带修订号；冲突时 wrDxMergePreferences 合并后重试，见 ws-writer-deep-posture.js）
   · 诊断请求：取诊断、AI 深评、AI 看这一处
   ws-deep.jsx 照旧转出这里的全部名字。ESM 模块，不写 window。
   ========================================================== */

const dxKey = (base) => (wsKey ? wsKey(base) : base);

/* ---- 决定日志 / 忽略清单（按场景写穿到本机，服务端是真相）---- */
function wrDxLog(sid) {
  try { return JSON.parse(localStorage.getItem(dxKey("wr-deep-log:" + sid))) || []; } catch (e) { return []; }
}
function wrDxPushLog(sid, text) {
  const list = [{ at: Date.now(), text }, ...wrDxLog(sid)].slice(0, 30);
  try { localStorage.setItem(dxKey("wr-deep-log:" + sid), JSON.stringify(list)); } catch (e) {}
  return list;
}
function wrDxSkips(sid) {
  try { return new Set(JSON.parse(localStorage.getItem(dxKey("wr-deep-skip:" + sid))) || []); } catch (e) { return new Set(); }
}
function wrDxWriteSkips(sid, set) {
  try { localStorage.setItem(dxKey("wr-deep-skip:" + sid), JSON.stringify([...set])); } catch (e) {}
}
function wrDxAddSkip(sid, key) {
  const s = wrDxSkips(sid); s.add(key); wrDxWriteSkips(sid, s);
}
function wrDxRemoveSkip(sid, key) {
  const s = wrDxSkips(sid); s.delete(key); wrDxWriteSkips(sid, s);
}

function wrDxSnapshot(sid) {
  return { decision_log: wrDxLog(sid), ignored_issue_keys: [...wrDxSkips(sid)] };
}

function wrDxApplyPreferences(sid, preferences) {
  const decisionLog = Array.isArray(preferences?.decision_log) ? preferences.decision_log.slice(0, 30) : [];
  const ignoredKeys = Array.isArray(preferences?.ignored_issue_keys)
    ? [...new Set(preferences.ignored_issue_keys)].slice(0, 200)
    : [];
  try { localStorage.setItem(dxKey("wr-deep-log:" + sid), JSON.stringify(decisionLog)); } catch (e) {}
  try { localStorage.setItem(dxKey("wr-deep-skip:" + sid), JSON.stringify(ignoredKeys)); } catch (e) {}
  return { decision_log: decisionLog, ignored_issue_keys: ignoredKeys };
}

function wrDxMergePreferences(remote, local, { localIgnoredAuthoritative = false } = {}) {
  const seenLogs = new Set();
  const decisionLog = [...(local?.decision_log || []), ...(remote?.decision_log || [])]
    .filter((entry) => {
      const key = `${entry?.at ?? ""}:${entry?.text ?? ""}`;
      if (!entry?.text || seenLogs.has(key)) return false;
      seenLogs.add(key);
      return true;
    })
    .sort((a, b) => Number(b.at || 0) - Number(a.at || 0))
    .slice(0, 30);
  const ignoredSource = localIgnoredAuthoritative
    ? (local?.ignored_issue_keys || [])
    : [...(local?.ignored_issue_keys || []), ...(remote?.ignored_issue_keys || [])];
  return {
    decision_log: decisionLog,
    ignored_issue_keys: [...new Set(ignoredSource)].slice(0, 200),
  };
}

async function wrDxLoadPreferences(sid) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(sid)}/deep-review/preferences`);
}

async function wrDxSavePreferences(sid, snapshot, baseRevisionNo) {
  return apiPatch(`/api/v1/scenes/${encodeURIComponent(sid)}/deep-review/preferences`, {
    decision_log: (snapshot?.decision_log || []).slice(0, 30),
    ignored_issue_keys: [...new Set(snapshot?.ignored_issue_keys || [])].slice(0, 200),
    base_revision_no: baseRevisionNo,
  });
}

/* ---- 诊断：服务端一份 ---- */
async function wrDxFetch(backendId) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(backendId)}/deep-review`);
}
async function wrDxRunAi(backendId) {
  return apiPost(`/api/v1/scenes/${encodeURIComponent(backendId)}/deep-review`, {});
}
/* 「AI 看这一处」：body 是 { signal_id } （复核一条发现）或 { paragraph_index, excerpt }（独立看一段），可带 question */
async function wrDxReviewPassage(backendId, body) {
  return apiPost(`/api/v1/scenes/${encodeURIComponent(backendId)}/deep-review/passage`, body || {});
}

/* 发现的 ignored 按本机忽略清单重算（忽略 / 恢复不必等服务端往返） */
function wrDxWithIgnored(findings, skips) {
  return (findings || []).map((f) => ({ ...f, ignored: skips.has(f.signal_id) }));
}

export {
  wrDxLog, wrDxPushLog, wrDxAddSkip, wrDxRemoveSkip, wrDxSkips, wrDxSnapshot,
  wrDxApplyPreferences, wrDxMergePreferences, wrDxLoadPreferences, wrDxSavePreferences,
  wrDxFetch, wrDxRunAi, wrDxReviewPassage, wrDxWithIgnored,
};
