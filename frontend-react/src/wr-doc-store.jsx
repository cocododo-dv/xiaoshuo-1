import { htmlToParagraphs } from "./manuscript-html.js";
import { countChars } from "./lib/text.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocs } from "./wr-doc-sync.js";
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

const REFUSAL_CODES = new Set(["CHAPTER_APPROVED_LOCKED", "AUTHOR_DRAFT_NOT_CURRENT", "AUTHOR_DRAFT_NOT_FOUND"]);
const TRANSIENT_CODES = new Set([
  "NETWORK_ERROR", "REQUEST_TIMEOUT", "REQUEST_ABORTED", "REQUEST_FAILED", "DATABASE_BUSY", "IDEMPOTENCY_REQUEST_IN_PROGRESS",
  "AUTHOR_DRAFT_UNAVAILABLE", "AUTHOR_DRAFT_STALE_READ", "AUTHOR_DRAFT_NOT_SYNCED",
]);

/* 保存没成是一时的（断网、超时、服务端出错、场景一时没就绪）：下一次保存 / 离开这一场 / 重新联网时会再发。
   服务端明确拒绝的（章已锁定、作者稿已不是当前的一份、其余 4xx）不是 */
function transientSaveError(error) {
  if (!error) return true;
  if (REFUSAL_CODES.has(error.code)) return false;
  if (error.retryable || TRANSIENT_CODES.has(error.code)) return true;
  const status = Number(error.status);
  return !status || status >= 500 || status === 429;
}

function lockedRestoreError() {
  return Object.assign(new Error("这一章已批准锁定，恢复不了：请先到成稿中心重新打开本章，再恢复这份记录。这份记录还在。"), {
    code: "CHAPTER_APPROVED_LOCKED",
  });
}

/* 服务端拒绝这一稿的原因（作者读得懂的话；认不得的照实给代码） */
function refusalReason(error) {
  const code = error && error.code;
  if (code === "CHAPTER_APPROVED_LOCKED") return "这一章已批准锁定，要先到成稿中心重新打开本章";
  if (code === "AUTHOR_DRAFT_NOT_CURRENT" || code === "AUTHOR_DRAFT_NOT_FOUND") return "服务端的这份作者稿已不是当前的一份，刷新后再试";
  return code ? `服务端拒绝了这次保存（${code}）` : "服务端拒绝了这次保存";
}

/* 恢复稿交给 WrDocs 之后没能同步上服务端：按那一刻的真实情况说清它眼下在哪、之后会怎样（同步与恢复中心照原话显示）。
   409：编辑器是不是已经换成了服务端版本，看 WrDocs 那一刻的状态说（复核二 W1-R2A-3 · W1-R2B-6）；一时的失败才说
   「之后会再同步」；服务端明确拒绝的（这期间章在别处批准了……）永远存不上：编辑器和本机缓存换回服务端的版本
   （恢复稿在这份记录里，恢复前的正文在自动备份里，之后又写的字 WrDocs 先留进同步与恢复），说它拒绝了什么（复核二 W1-R2A-2）。 */
function restoreFailure(error, sid) {
  // WrDocs 自己拒绝的（这一刻章已批准锁定）：编辑器和本机缓存都没动
  if (error && error.code === "CHAPTER_APPROVED_LOCKED" && !error.status) return lockedRestoreError();
  if (error && error.code === "AUTHOR_DRAFT_CONFLICT") {
    const state = WrDocs.state(sid);
    const message = state && state.conflictPending
      ? "这一场在别处有更新：服务端的最新版本还没读下来，读到之后编辑器会换成它。恢复稿没有同步上去；这份记录还在，可以比较后再恢复。"
      : "这一场在别处有更新，编辑器已换成服务端的最新版本；这份记录还在，可以比较后再恢复。";
    return Object.assign(new Error(message), { code: "AUTHOR_DRAFT_CONFLICT", cause: error });
  }
  if (transientSaveError(error)) {
    return Object.assign(new Error("已恢复到编辑器和这台电脑的本机缓存，但还没同步到服务端（网络或服务端出错）。下一次保存或离开这一场时会再同步；这份记录仍保留。"), {
      code: "RECOVERY_NOT_SYNCED",
      cause: error,
    });
  }
  const reverted = WrDocs.dropLocal(sid);
  return Object.assign(new Error(reverted
    ? `服务端没有接受这份恢复稿：${refusalReason(error)}。编辑器换回了服务端上的正文；这份记录还在。`
    : `服务端没有接受这份恢复稿：${refusalReason(error)}。恢复稿在编辑器和这台电脑的本机缓存里，没有同步到服务端；这份记录还在。`), {
    code: "RECOVERY_REFUSED",
    cause: error,
  });
}

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
    // 章已批准锁定：写作台对它只读、也不替它保存，恢复进去的字不会同步——先停下，说清要先重新打开本章（复核二 W1-R2A-2）
    if (WrDocs.locked(entry.sid)) throw lockedRestoreError();
    // 先和服务端对齐（这一场这次还没打开过、或冲突后服务端版本还没读到时）：下面自动备份的「当前正文」
    // 就是服务端眼下那一版，不是这台电脑上可能过时的缓存；读不到服务器就停下，不盲目覆盖。
    await WrDocs.hydrate(entry.sid);
    // 目录里没有这一场的后端 id（删到回收站了、或还没同步到后端）：没有地方可存，不动编辑器和本机缓存
    const docState = WrDocs.state(entry.sid);
    if (!docState || !docState.draftId) {
      throw Object.assign(new Error("这一场不在目录里了（也许已移到回收站），恢复不了；这份记录还在，可以复制文字或导出。"), {
        code: "RECOVERY_SCENE_UNAVAILABLE",
      });
    }
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
    // 编辑器与本机缓存在同一个调用里换成恢复稿（WrDocs.replace 同步通知写作台）：之后要同步的就是作者眼前的这一稿，
    // 服务器这次没存上也一样——它停在本机，下一次保存或离开这一场时再发，不会有一份作者看不见的正文被悄悄提升或冲刷上去。
    let settled;
    try {
      settled = await WrDocs.replace(entry.sid, entry.html || "", { reason: "restore" });
    } catch (error) {
      throw restoreFailure(error, entry.sid);
    }
    notifyRecoveryChanged(entry, "restored");
    return { entry, replacedBackup, carried: settled.carried, state: WrDocs.state(entry.sid) };
  },
  /* 重试同步 = 恢复 + 服务端存下的正是这一稿时移出列表。它发出去之前就被随后的改动取代了（存下的是更新的一稿）：
     这份记录本身没有同步上去——记录留着，抛 RECOVERY_SUPERSEDED（原话给作者看），不报成功。 */
  async retry(id) {
    const result = await WrRecovery.restore(id);
    if (result.carried === false) {
      throw Object.assign(new Error("已恢复为当前草稿，但它发出去之前就被随后改过的一稿取代，同步到服务端的是那一稿；这份记录先留着，确认无误后可删除。"), {
        code: "RECOVERY_SUPERSEDED",
      });
    }
    recoveryRemove(id);
    return { ...result, removed: true };
  },
};


/* 目录每次装载成功后预热当前在写那一场（经 WrDocs 的属性调用：单测会 spy 它） */
WsCatalog.onLoaded(() => WrDocs.hydrateActive());

Object.assign(window, { WrDocs, WrDocVersions, WrRecovery });

export { WrDocs, WrDocVersions, WrRecovery };
