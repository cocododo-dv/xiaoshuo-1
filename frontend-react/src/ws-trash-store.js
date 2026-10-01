import { apiDelete, apiGet, apiPost } from "./lib/client.js";
import { createSubscribers, storeAlert } from "./lib/store-utils.js";
import { createKeyedLoader } from "./lib/store-kit.js";
import { adoptModuleListeners, retireModuleListeners } from "./lib/events.js";
import { readyWorkId } from "./lib/ready-work.js";
import { WsWorks } from "./ws-works.jsx";

/* ==========================================================
   WsTrashStore — 回收站（FE-ALIGN Phase 4：接真后端统一三级列表）
   GET /api/v2/trash?project_id=… = 全局作品桶 + 当前作品的章/场景桶；
   软删端点自动产生条目，push() 退化为「触发刷新」的兼容壳。
   restore/purge 走对应端点；失败由 store 自行提示并以服务端为准刷新。
   2026-09-29 从 ws-catalog.jsx 拆出；ws-catalog.jsx 照旧转出 WsTrashStore。
   ========================================================== */
retireModuleListeners("ws-trash-store");

const trashSubs = createSubscribers();
/* 恢复了章 / 场之后要做的事（目录重读）：目录 import 本模块并在这里登记，本模块不反过来 import 目录 */
const trashRestoredHooks = new Set();
function onTrashRestored(fn) {
  trashRestoredHooks.add(fn);
  return () => { trashRestoredHooks.delete(fn); };
}
let trashCache = [];
/* 读取状态（只读，给视图区分「真的空」「还在读」「读不到」）：以前拉取失败只 console.warn，
   列表停在 []，作者看到的是「回收站是空的」——删掉的东西像是没了。 */
let trashLoad = { status: "idle", message: "" };
const TRASH_KIND_LABEL = { work: "作品", chapter: "章节", scene: "场景" };

function trashNotify() { trashSubs.notify(); }

function trashAdapt(item) {
  return {
    id: item.id,
    kind: TRASH_KIND_LABEL[item.kind] || item.kind || "内容",
    title: item.kind === "work" ? `《${item.title}》· 整部` : item.title,
    removedAt: item.removed_at ? (Date.parse(item.removed_at) || Date.now()) : Date.now(),
    restorable: item.restorable !== false,
    // 场景条目带所在章：回收站把随章一起回收的场景嵌在章下面（后端 services/trash.py 已返回）
    chapterId: item.chapter_id || "",
    payload: { type: item.kind },
  };
}

/* 回收站只存「当前作品」一份：键 = 当前作品 id（没有作品时是空串，只拿全局作品桶）。
   换作品时新作品另发请求，上一部还在飞的那次回来直接丢掉（审计 F01-05：过去并进同一个在飞请求，
   新作品的回收站显示上一部的章与场）。 */
const trashKey = () => readyWorkId(WsWorks) || "";
const trashLoader = createKeyedLoader({
  fetch(key) {
    const qs = key ? `?project_id=${encodeURIComponent(key)}` : "";
    return apiGet(`/api/v2/trash${qs}`);
  },
  isCurrent: (key) => key === trashKey(),
  apply(key, data) {
    trashCache = ((data && data.items) || []).map(trashAdapt);
    trashLoad = { status: "ready", message: "" };
    trashNotify();
  },
  onError(key, e) {
    console.warn("[WsTrashStore] 拉取回收站失败:", e);
    trashLoad = { status: "error", message: (e && e.message) || "读不到回收站。" };
    trashNotify();
  },
});

function trashLoading() {
  // 已经读到过一次时，后台刷新不把状态打回「读取中」（视图不该因此闪一下）
  if (trashLoad.status !== "ready") { trashLoad = { status: "loading", message: "" }; trashNotify(); }
}

/* 装载（这部作品的在飞请求可以复用） */
function trashFetch() {
  trashLoading();
  return trashLoader.load(trashKey());
}

/* 服务端刚变过（软删 / 恢复 / 永久删除）：在飞的那一次作废，结束后恰好再读一次 */
function trashRefetch() {
  trashLoading();
  return trashLoader.invalidate(trashKey());
}

const WsTrashStore = {
  list() { return trashCache; },
  /* { status: idle | loading | ready | error, message }——只读 */
  loadState() { return trashLoad; },
  /* 回收站打开时重拉一次：分章 / 物化可能在服务端自动移入或取回了章与场景 */
  refresh() { return trashRefetch(); },
  /* 兼容壳：各软删端点已自动产生后端条目，这里只触发刷新（旧调用点无害化） */
  push(item) {
    trashRefetch();
    return { id: "pending", removedAt: Date.now(), ...(item || {}) };
  },
  restore(id) {
    apiPost(`/api/v2/trash/${encodeURIComponent(id)}/restore`, {}).then(() => {
      trashRefetch();
      if (String(id).startsWith("work:")) WsWorks.retry("projects");
      else trashRestoredHooks.forEach((fn) => { try { fn(id); } catch (e) {} });
    }).catch((e) => {
      storeAlert(e, "恢复失败。");
      trashRefetch();
    });
    return true; // 乐观返回；失败走上面的独立提示
  },
  purge(id) {
    apiDelete(`/api/v2/trash/${encodeURIComponent(id)}`).then(() => {
      trashRefetch();
    }).catch((e) => {
      storeAlert(e, "永久删除失败。");
      trashRefetch();
    });
  },
  async clear() {
    const failures = [];
    // 子项先删、作品最后删，避免清除作品时级联删除子项后，后续请求误报 404。
    const rank = { scene: 0, chapter: 1, work: 2 };
    const items = trashCache.slice().sort((a, b) => (
      (rank[a.payload && a.payload.type] ?? 3) - (rank[b.payload && b.payload.type] ?? 3)
    ));
    for (const it of items) {
      try {
        await apiDelete(`/api/v2/trash/${encodeURIComponent(it.id)}`);
      } catch (e) {
        failures.push({ item: it, error: e });
      }
    }
    await trashRefetch();
    if (failures.length) {
      const first = failures[0].error;
      const reason = first && first.message ? `：${first.message}` : "";
      storeAlert(null, `回收站未能完全清空，${failures.length} 条仍需重试${reason}`);
      return false;
    }
    return true;
  },
  subscribe(fn) { return trashSubs.subscribe(fn); },
};

try { trashFetch(); } catch (e) {}
const trashOnWorkChanged = () => { try { trashFetch(); } catch (e) {} };
window.addEventListener("ws:work-changed", trashOnWorkChanged);
/* 软删端点完成后的精确刷新信号（WsWorks.remove / 目录删除成功时 dispatch） */
const trashOnChanged = () => { try { trashRefetch(); } catch (e) {} };
window.addEventListener("ws:trash-changed", trashOnChanged);
adoptModuleListeners("ws-trash-store", () => {
  window.removeEventListener("ws:work-changed", trashOnWorkChanged);
  window.removeEventListener("ws:trash-changed", trashOnChanged);
});

export { WsTrashStore, onTrashRestored };
