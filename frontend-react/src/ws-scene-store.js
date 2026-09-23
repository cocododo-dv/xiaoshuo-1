import { wsKey } from "./ws-works.jsx";

/* ==========================================================
   AI 起草台 — 本机持久化（按作品隔离，wsKey 加作品前缀）
   ----------------------------------------------------------
   · scn-run:<sid>            每场的运行记录（只存 SCN_RUN_FIELDS；ws-catalog 迁移 sid 时认这个前缀）
   · scn-queue:v1             在办清单（sid 列表，最多 40 条）
   · scn-queue-dismissed:v1   作者移出过的场（最多留最近 200 条）
   这些都是后端管线真相的读缓存：刷新不丢、换浏览器再从 /scene-run-states + workbench 取回。
   ========================================================== */

const SCN_RUN_FIELDS = ["state", "draft", "verdict", "log", "attempts", "attempt", "at", "words", "gate", "budgetBlock", "authorNote", "rewriteBrief", "draftMode", "styleNotices", "styleWindows", "styleFidelity", "pipeState", "cost"];
const scnRunKey = (sid) => wsKey("scn-run:" + sid);
const scnQueueKey = () => wsKey("scn-queue:v1");
const scnDismissKey = () => wsKey("scn-queue-dismissed:v1");

function scnRunLoad(sid) {
  try { return JSON.parse(localStorage.getItem(scnRunKey(sid))) || null; } catch (e) { return null; }
}
function scnRunSave(sid, run) {
  try {
    const slim = {}; SCN_RUN_FIELDS.forEach(f => { if (run[f] !== undefined) slim[f] = run[f]; });
    localStorage.setItem(scnRunKey(sid), JSON.stringify(slim));
  } catch (e) {}
}
function scnQueueLoad() {
  try { return JSON.parse(localStorage.getItem(scnQueueKey())) || []; } catch (e) { return []; }
}
function scnQueueSave(sids) {
  try { localStorage.setItem(scnQueueKey(), JSON.stringify(sids.slice(0, 40))); } catch (e) {}
}

/* 移出队列的场：本地队列只是「在办清单」，但队列成员也会从后端 scene-run-states 恢复
   （换浏览器 / 后台跑完的场不该消失）。若不记下作者的移出意图，下次进页面这一场又会被恢复回来——
   删除就成了假动作。这里按作品持久化移出名单，重新入列时销名。 */
function scnQueueDismissLoad() {
  try {
    const raw = JSON.parse(localStorage.getItem(scnDismissKey()));
    return Array.isArray(raw) ? raw : [];
  } catch (e) { return []; }
}
const SCN_DISMISS_CAP = 200;
function scnQueueDismissSave(sids) {
  /* 留**最近**的 200 条。新移出的场追加在尾部，所以 slice(0, 200) 恰好把它们整批丢掉：
     名单一满，「移出」就退化成假动作——界面上那一场消失了，下次进页面恢复过滤名单里根本没有它，
     它又原样回到队列里。 */
  const kept = (sids || []).slice(-SCN_DISMISS_CAP);
  try { localStorage.setItem(scnDismissKey(), JSON.stringify(kept)); } catch (e) {}
  return kept;
}
function scnQueueDismissAdd(sids) {
  const next = scnQueueDismissLoad();
  (sids || []).forEach((sid) => { if (sid && !next.includes(sid)) next.push(sid); });
  /* 返回真正落盘的那份，而不是截断前的 next——调用方拿它当「现在的移出名单」用。 */
  return scnQueueDismissSave(next);
}
function scnQueueDismissClear(sids) {
  const drop = new Set(sids || []);
  if (!drop.size) return scnQueueDismissLoad();
  const next = scnQueueDismissLoad().filter((sid) => !drop.has(sid));
  return scnQueueDismissSave(next);
}

export { scnRunLoad, scnRunSave, scnQueueLoad, scnQueueSave, scnQueueDismissLoad, scnQueueDismissAdd, scnQueueDismissClear };
