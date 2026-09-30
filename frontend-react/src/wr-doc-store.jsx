import { htmlToParagraphs, sanitizeManuscriptHTML } from "./manuscript-html.js";
import { countChars } from "./lib/text.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocs, refusalReason } from "./wr-doc-sync.js";
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

function lockedRestoreError() {
  return Object.assign(new Error("这一章已批准锁定，恢复不了：请先到成稿中心重新打开本章，再恢复这份记录。这份记录还在。"), {
    code: "CHAPTER_APPROVED_LOCKED",
  });
}

/* 恢复稿交给 WrDocs 之后没能同步上服务端：按那一刻的真实情况说清它眼下在哪、之后会怎样（同步与恢复中心照原话显示）。
   409：编辑器是不是已经换成了服务端版本，看 WrDocs 那一刻的状态说（复核二 W1-R2A-3 · W1-R2B-6）；服务端明确拒绝的
   （这期间章在别处批准了……，WrDocs 标 refused）永远存不上：WrDocs 已把编辑器和本机缓存换回服务端的版本（恢复稿在
   这份记录里，恢复前的正文在自动备份里，之后又写的字 WrDocs 先留进同步与恢复），说它拒绝了什么（复核二 W1-R2A-2）；
   其余是一时的（断网、超时、服务端出错、场景一时没就绪）：恢复稿停在本机，之后会再同步。
   恢复稿排在作者自己在起草台的采纳后面、采纳先落了地（WrDocs 标 replacedByAdoption）：换稿的是那次采纳，不是别处的修改——
   照实说（复核六 W1-R6A-1：过去这里说「这一场在别处有更新」） */
function restoreFailure(error, sid) {
  // WrDocs 自己拒绝的（这一刻章已批准锁定）：编辑器和本机缓存都没动
  if (error && error.code === "CHAPTER_APPROVED_LOCKED" && !error.status && !error.refused) return lockedRestoreError();
  if (error && error.code === "AUTHOR_DRAFT_CONFLICT" && error.replacedByAdoption) {
    return Object.assign(new Error("这一场刚换成了你在 AI 起草台采纳归档的稿，恢复稿没有同步上去；这份记录还在，可以比较后再恢复。"), {
      code: "AUTHOR_DRAFT_CONFLICT",
      cause: error,
    });
  }
  if (error && error.code === "AUTHOR_DRAFT_CONFLICT") {
    const state = WrDocs.state(sid);
    const message = state && state.conflictPending
      ? "这一场在别处有更新：服务端的最新版本还没读下来，读到之后编辑器会换成它。恢复稿没有同步上去；这份记录还在，可以比较后再恢复。"
      : "这一场在别处有更新，编辑器已换成服务端的最新版本；这份记录还在，可以比较后再恢复。";
    return Object.assign(new Error(message), { code: "AUTHOR_DRAFT_CONFLICT", cause: error });
  }
  if (error && error.refused) {
    return Object.assign(new Error(`服务端没有接受这份恢复稿：${refusalReason(error)}。编辑器换回了服务端上的正文；这份记录还在。`), {
      code: "RECOVERY_REFUSED",
      cause: error,
    });
  }
  return Object.assign(new Error("已恢复到编辑器和这台电脑的本机缓存，但还没同步到服务端（网络或服务端出错）。下一次保存或离开这一场时会再同步；这份记录仍保留。"), {
    code: "RECOVERY_NOT_SYNCED",
    cause: error,
  });
}

/* 这份记录现在恢复进哪一场（目录眼下的名字）：记录挂在乐观新建时的临时 sid 下、那一场之后换成了稳定的 scene_id——同一次会话里
   目录记得别名，刷新之后凭记录里的后端 scene_id 找（复核六 W1-R6B-1：过去这时「恢复」说「这一场不在目录里了」） */
function entrySceneSid(entry) {
  const sid = WrDocs.sceneSid(entry.sid);
  let found = null;
  try { found = WsCatalog.sceneById(sid); } catch (e) { found = null; }
  if (found || !entry.sceneId) return sid;
  let bySceneId = "";
  try { bySceneId = WsCatalog.sidForBackendId(entry.sceneId); } catch (e) { bySceneId = ""; }
  return bySceneId || sid;
}

/* 这一场已经持久地备份过的、和 html 一模一样的那份作者稿（没有就是 null） */
function sameAuthorBackup(sid, workId, html) {
  const wanted = sanitizeManuscriptHTML(html || "");
  return recoveryList().find((item) => item.type === "backup" && item.source === "author" && item.sid === sid
    && item.workId === workId && item.durable !== false && item.html === wanted) || null;
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
    const sid = entrySceneSid(entry);
    // 章已批准锁定：写作台对它只读、也不替它保存，恢复进去的字不会同步——先停下，说清要先重新打开本章（复核二 W1-R2A-2）
    if (WrDocs.locked(sid)) throw lockedRestoreError();
    // 先和服务端对齐（这一场这次还没打开过、或冲突后服务端版本还没读到时）：下面自动备份的「当前正文」
    // 就是服务端眼下那一版，不是这台电脑上可能过时的缓存；读不到服务器就停下，不盲目覆盖。
    await WrDocs.hydrate(sid);
    // 目录里没有这一场的后端 id（删到回收站了、或还没同步到后端）：没有地方可存，不动编辑器和本机缓存
    const docState = WrDocs.state(sid);
    if (!docState || !docState.draftId) {
      throw Object.assign(new Error("这一场不在目录里了（也许已移到回收站），恢复不了；这份记录还在，可以复制文字或导出。"), {
        code: "RECOVERY_SCENE_UNAVAILABLE",
      });
    }
    const current = cacheRead(sid) || "";
    const hasCurrent = countChars(htmlToParagraphs(current).join("")) > 0;
    let replacedBackup = null;
    if (hasCurrent && current !== (entry.html || "")) {
      // “恢复”本质上也是一次显式替换：先留下可撤销的当前稿，配额不足则
      // fail closed，不允许恢复工具反过来成为新的丢稿入口。一模一样的作者稿已经持久地备份过（起草台覆盖前刚备份的、
      // 上一次恢复前备份的）就沿用那一份，不再多留一份一样的（复核六 W1-R6A-1）
      replacedBackup = sameAuthorBackup(sid, entry.workId || activeWorkId(), current) || recoveryCreate({
        sid,
        html: current,
        type: "backup",
        reason: `恢复“${entry.label || sid}”前自动备份当前正文`,
        label: `场景 ${sid} · 作者稿备份`,
        source: "author",
        requireDurable: true,
      });
    }
    // 编辑器与本机缓存在同一个调用里换成恢复稿（WrDocs.replace 同步通知写作台）：之后要同步的就是作者眼前的这一稿，
    // 服务器这次没存上也一样——它停在本机，下一次保存或离开这一场时再发，不会有一份作者看不见的正文被悄悄提升或冲刷上去。
    let settled;
    try {
      settled = await WrDocs.replace(sid, entry.html || "", { reason: "restore" });
    } catch (error) {
      throw restoreFailure(error, sid);
    }
    notifyRecoveryChanged(entry, "restored");
    return { entry, replacedBackup, carried: settled.carried, state: WrDocs.state(sid) };
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


/* 目录每次装载成功后：换了名字的场（乐观新建的场建好了）让正文的状态机跟过去、不在目录里了的场没同步上的字留进同步与恢复
   （WrDocs.followCatalog，复核六 W1-R6B-1 · W1-R6B-2），再预热当前在写那一场（经 WrDocs 的属性调用：单测会 spy 它） */
WsCatalog.onLoaded((workId) => {
  try { WrDocs.followCatalog(workId); } catch (e) { console.warn("[WrDocs] 跟随目录失败:", e); }
  WrDocs.hydrateActive();
});
/* 写作台、起草台是按需加载的视图：这个模块加载时目录可能已经装载过了（上面的登记赶不上那一次）——先跟一次 */
Promise.resolve().then(() => {
  try { if (WsCatalog.ready()) WrDocs.followCatalog(activeWorkId()); } catch (e) { console.warn("[WrDocs] 跟随目录失败:", e); }
});

Object.assign(window, { WrDocs, WrDocVersions, WrRecovery });

export { WrDocs, WrDocVersions, WrRecovery };
