import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { apiGet, apiPost } from "./lib/client.js";
import { createSubscribers, storeAlert, useStoreTick } from "./lib/store-utils.js";

/* ==========================================================
   WsDesignSync — 「这张场景卡落后于已确认的构思了吗」（阶段 X）
   ----------------------------------------------------------
   物化之后构思和目录是两份数据。确认 09 / 10 时工作台带 sync_catalog，绝大多数卡当场就跟上了；
   留下来的只有需要作者看一眼的那几种（作者在台子上改过这张卡、同步会把一张写过的卡送进回收站、
   要搬去的章目录里还没有）。写作台 / AI 起草台不必为此拉整个雪花工作台——读这个轻量口。
   只认**已确认**的规划：还在改的草稿不算「待同步」，不在台子上打扰作者。
   ESM 模块，不写 window。
   ========================================================== */

const dsSubs = createSubscribers();
const dsPending = {};      // workId → { [sceneId]: { fields, deskEdited } }
const dsUnsupported = {};  // workId → true（非雪花作品：后端答 supported:false，别再问）
const dsFetching = {};
let dsBusy = {};           // sceneId → true（同步中）

function dsWorkId() {
  try { const id = WsWorks ? WsWorks.activeId() : null; return id && id !== "__loading__" ? id : null; } catch (e) { return null; }
}

/* ws:catalog-changed 连写作时的字数回写都会广播——事件触发的重拉必须节流，否则每次自动保存都多打一个请求。
   作者点的（同步 / 进台子）走 force。 */
const DS_EVENT_MIN_INTERVAL_MS = 20_000;
const dsFetchedAt = {};

function dsRefresh(workId = dsWorkId(), options = {}) {
  if (!workId || dsUnsupported[workId]) return Promise.resolve();
  if (dsFetching[workId]) return dsFetching[workId];
  if (!options.force && dsFetchedAt[workId] && Date.now() - dsFetchedAt[workId] < DS_EVENT_MIN_INTERVAL_MS) {
    return Promise.resolve();
  }
  dsFetchedAt[workId] = Date.now();
  dsFetching[workId] = apiGet(`/api/v2/projects/${workId}/snowflake-workspace/resync-status`)
    .then((data) => {
      if (data && data.supported === false) { dsUnsupported[workId] = true; dsPending[workId] = {}; dsSubs.notify(); return; }
      const next = {};
      ((data && data.pending_scenes) || []).forEach((item) => {
        if (!item || !item.scene_id || item.plan_status !== "approved") return;
        next[item.scene_id] = { fields: item.changed_fields || [], deskEdited: !!item.desk_edited };
      });
      dsPending[workId] = next;
      dsSubs.notify();
    })
    .catch((error) => {
      if (error && error.code === "PROJECT_NOT_SNOWFLAKE") dsUnsupported[workId] = true;
      // 读不到就当没有待同步：这是一条提示，不是闸门
    })
    .finally(() => { delete dsFetching[workId]; });
  return dsFetching[workId];
}

const WsDesignSync = {
  refresh(workId) { return dsRefresh(workId || dsWorkId(), { force: true }); },
  pendingFor(sceneId) {
    const workId = dsWorkId();
    return (sceneId && workId && dsPending[workId] && dsPending[workId][sceneId]) || null;
  },
  pendingCount() {
    const workId = dsWorkId();
    return workId && dsPending[workId] ? Object.keys(dsPending[workId]).length : 0;
  },
  isBusy(sceneId) { return !!dsBusy[sceneId]; },
  /* 把这几场的场景卡同步到构思的最新版（显式回流——作者点的，不是静默的）；成功后目录与状态一起重拉。 */
  async syncScenes(sceneIds) {
    const workId = dsWorkId();
    const ids = (sceneIds || []).filter(Boolean);
    if (!workId || !ids.length) return null;
    ids.forEach((id) => { dsBusy[id] = true; });
    dsSubs.notify();
    try {
      const result = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/resync`, { scene_ids: ids });
      if (WsCatalog && WsCatalog.__refresh) await WsCatalog.__refresh(workId);
      await dsRefresh(workId, { force: true });
      return result;
    } catch (error) {
      storeAlert(error, "同步这一场失败，场景卡没有改动。");
      return null;
    } finally {
      dsBusy = Object.fromEntries(Object.entries(dsBusy).filter(([id]) => !ids.includes(id)));
      dsSubs.notify();
    }
  },
  subscribe(fn) { return dsSubs.subscribe(fn); },
  /* 测试用：清空模块级状态 */
  __reset() {
    [dsPending, dsUnsupported, dsFetching, dsFetchedAt].forEach((map) => Object.keys(map).forEach((key) => delete map[key]));
    dsBusy = {};
  },
};

/* hook：台子挂载时拉一次；目录变了（物化 / 回流 / 手动改卡）再拉一次 */
function useDesignSync() {
  useStoreTick((bump) => {
    const un = WsDesignSync.subscribe(bump);
    const onChange = () => { dsRefresh(); };
    window.addEventListener("ws:catalog-changed", onChange);
    window.addEventListener("ws:work-changed", onChange);
    dsRefresh(dsWorkId(), { force: true });
    return () => {
      un();
      window.removeEventListener("ws:catalog-changed", onChange);
      window.removeEventListener("ws:work-changed", onChange);
    };
  });
  return WsDesignSync;
}

export { WsDesignSync, useDesignSync };
