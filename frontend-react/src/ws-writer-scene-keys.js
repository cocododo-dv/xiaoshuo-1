import { emit } from "./lib/events.js";
import { readyWorkId } from "./lib/ready-work.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WR_ANNO_KEY_PREFIX, wrAnnoLoad, wrAnnoSave } from "./ws-writer-annotations.js";
import { wrNotesFollowRename } from "./ws-writer-notes.jsx";

/* ==========================================================
   写作台 · 换了名字的场，本机这一层跟过去（2026-10，W1 复核跟进）
   ----------------------------------------------------------
   乐观新建的场先用临时 sid（tmp_…），后端建好、目录重读之后换成稳定的 scene_id。正文的本机这一层由 WrDocs.followCatalog
   搬（W1）；批注（wr-anno:<sid>，只存本机）与本场笔记的缓存 / 未同步标记（wr-notes:* / wr-notes-pending:*）过去留在临时 sid
   的键下——新建那几秒写的批注、没同步上的笔记从此没人读。
   目录每次装载成功后（WsCatalog.onLoaded）按目录的别名把它们搬到新名字下：批注按 id 并进新名字下已有的清单（一条不丢；
   并不下就原样留着），笔记交给 wrNotesFollowRename（正开着的那一份由它自己换名时搬）。
   只认这一次打开里目录记得的别名：刷新之后别名没了，留在临时 sid 下的批注认不出是哪一场（与正文同一个已知边界）。
   ESM 模块，不写 window；写作台加载时登记一次。
   ========================================================== */

const TMP_SID = /^tmp_/;
const NOTES_PREFIXES = ["wr-notes:", "wr-notes-pending:"];

function storageKeys() {
  const keys = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) keys.push(localStorage.key(i));
  } catch (e) { /* 存储被禁：什么也不搬 */ }
  return keys.filter(Boolean);
}

/* 临时 sid 现在叫什么（目录还没换名、或认不出时为 null） */
function renamedSid(sid) {
  if (!TMP_SID.test(sid)) return null;
  try {
    const hit = WsCatalog.sceneById(sid);
    const current = hit && hit.scene ? hit.scene.sid : null;
    return current && current !== sid ? current : null;
  } catch (e) {
    return null;
  }
}

function moveAnnotations(fromKey, toKey) {
  const moving = wrAnnoLoad(fromKey);
  if (!moving.length) {
    try { localStorage.removeItem(fromKey); } catch (e) {}
    return true;
  }
  const kept = wrAnnoLoad(toKey);
  const known = new Set(kept.map((item) => item.id));
  if (!wrAnnoSave(toKey, [...kept, ...moving.filter((item) => !known.has(item.id))])) return false;
  try { localStorage.removeItem(fromKey); } catch (e) {}
  return true;
}

export function wrFollowRenamedScenes(workId) {
  if (!workId || workId !== readyWorkId(WsWorks)) return;
  const suffix = `::${workId}`;
  const notes = new Map();
  storageKeys().forEach((key) => {
    if (!key.endsWith(suffix)) return;
    if (key.startsWith(WR_ANNO_KEY_PREFIX)) {
      const sid = key.slice(WR_ANNO_KEY_PREFIX.length, -suffix.length);
      const current = renamedSid(sid);
      if (current && moveAnnotations(key, `${WR_ANNO_KEY_PREFIX}${current}${suffix}`)) emit("ws:anno-change", { sid: current });
      return;
    }
    const prefix = NOTES_PREFIXES.find((item) => key.startsWith(item));
    if (!prefix) return;
    const sid = key.slice(prefix.length, -suffix.length);
    const current = renamedSid(sid);
    if (current) notes.set(sid, current);
  });
  notes.forEach((current, sid) => { wrNotesFollowRename(sid, current); });
}

WsCatalog.onLoaded((workId) => { wrFollowRenamedScenes(workId); });
/* 写作台是按需加载的视图：加载时目录可能已经装载过了（上面的登记赶不上那一次）——先跟一次 */
Promise.resolve().then(() => {
  try { if (WsCatalog.ready()) wrFollowRenamedScenes(readyWorkId(WsWorks)); } catch (e) { /* 目录还没装载 */ }
});
