import { htmlToParagraphs } from "./manuscript-html.js";
import { countChars } from "./lib/text.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocs, notifyLoaded } from "./wr-doc-sync.js";
import { cacheRead, cacheReadForWork } from "./wr-doc-cache.js";
import { WrDocVersions, diffSentences } from "./wr-doc-versions.js";
import { activeWorkId, notifyRecoveryChanged, recoveryCreate, recoveryList, recoveryRemove } from "./wr-recovery-store.js";

/* ==========================================================
   写作台正文的门面（2026-09-29 拆分）：
     wr-doc-sync.js       WrDocs——作者稿的水合、保存状态机（一次一个请求、只留最新一稿、409 冲突副本）、提升为权威正文
     wr-doc-cache.js      本机这一层：读缓存、未同步标记、会话内存里的那一份
     wr-doc-versions.js   WrDocVersions——修订历史与句级对比
     wr-recovery-store.js 本机恢复记录（冲突副本、未同步稿、备份、AI 候选）
   这里放 WrRecovery（同步与恢复中心用：列、比、恢复、重试），登记目录装载后的预热，转出三个对象。
   ========================================================== */

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
      ...diffSentences(htmlToParagraphs(current), htmlToParagraphs(entry.html || "")),
    };
  },
  async restore(id) {
    const entry = recoveryList().find(item => item.id === id);
    if (!entry) throw Object.assign(new Error("恢复记录已不存在"), { code: "RECOVERY_NOT_FOUND" });
    assertRecoveryWork(entry);
    // 先和服务端对齐（这一场这次还没打开过、或冲突后服务端版本还没读到时）：下面自动备份的「当前正文」
    // 就是服务端眼下那一版，不是这台电脑上可能过时的缓存；读不到服务器就停下，不盲目覆盖。
    await WrDocs.hydrate(entry.sid);
    const current = cacheRead(entry.sid) || "";
    const hasCurrent = countChars(htmlToParagraphs(current).join("")) > 0;
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


/* 目录每次装载成功后预热当前在写那一场（经 WrDocs 的属性调用：单测会 spy 它） */
WsCatalog.onLoaded(() => WrDocs.hydrateActive());

Object.assign(window, { WrDocs, WrDocVersions, WrRecovery });

export { WrDocs, WrDocVersions, WrRecovery };
