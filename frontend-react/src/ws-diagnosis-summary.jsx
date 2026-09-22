import { WsWorks } from "./ws-works.jsx";
import { apiGet } from "./lib/client.js";
import { createSubscribers, useStoreTick } from "./lib/store-utils.js";

/* ==========================================================
   WsDiagnosis — 一本书每一场 / 每一章开着的诊断发现数（2026-09-22 场景诊断统一）
   ----------------------------------------------------------
   读 GET /api/v1/projects/{id}/diagnosis-summary：服务端把每一场的统一诊断（规则 / 节奏 / 评审 / AI 深评，
   减去作者在写作台忽略过的）数一遍。主页的章卡、成稿中心的章列表与场景拼接、章级「AI 通读」面板
   都读这一份；写作台深改面板里忽略 / 恢复 / 跑深评之后广播 ws:diagnosis-changed，这里重拉。
   ws:catalog-changed 连字数回写都会广播——事件触发的重拉节流 20 秒；视图挂载时的那次不节流。
   ESM 模块，不写 window。
   ========================================================== */

const dgSubs = createSubscribers();
const dgSummary = {};    // workId → { totals, chapters, scenes }
const dgFetching = {};
const dgFetchedAt = {};
const dgFailed = {};     // workId → true（读不到就当没有计数：这是角标，不是闸门）
const DG_EVENT_MIN_INTERVAL_MS = 20_000;

function dgWorkId() {
  try { const id = WsWorks.activeId(); return id && id !== "__loading__" ? id : null; } catch (e) { return null; }
}

function dgRefresh(workId = dgWorkId(), options = {}) {
  if (!workId) return Promise.resolve();
  if (dgFetching[workId]) return dgFetching[workId];
  if (!options.force && dgFetchedAt[workId] && Date.now() - dgFetchedAt[workId] < DG_EVENT_MIN_INTERVAL_MS) {
    return Promise.resolve();
  }
  dgFetchedAt[workId] = Date.now();
  dgFetching[workId] = apiGet(`/api/v1/projects/${encodeURIComponent(workId)}/diagnosis-summary`)
    .then((data) => {
      dgSummary[workId] = {
        totals: (data && data.totals) || {},
        chapters: (data && data.chapters) || {},
        scenes: (data && data.scenes) || {},
      };
      delete dgFailed[workId];
      dgSubs.notify();
    })
    .catch(() => {
      dgFailed[workId] = true;
      dgSubs.notify();
    })
    .finally(() => { delete dgFetching[workId]; });
  return dgFetching[workId];
}

const EMPTY_SCENE = { open: 0, blocking: 0, revision: 0, taste: 0, info: 0, ignored: 0, stale: 0, ai_status: "not_run", review_status: "not_run" };
const EMPTY_CHAPTER = { open: 0, blocking: 0, chapter_level: 0, scenes: 0, scenes_with_findings: 0, ai_status: "not_run" };

const WsDiagnosis = {
  refresh(workId) { return dgRefresh(workId || dgWorkId(), { force: true }); },
  /* 某一场（后端 scene_id）的计数；还没读到 / 读不到 → null（视图不画角标，不画 0） */
  sceneCounts(sceneId) {
    const workId = dgWorkId();
    const summary = workId ? dgSummary[workId] : null;
    if (!summary || !sceneId) return null;
    return summary.scenes[sceneId] ? { ...EMPTY_SCENE, ...summary.scenes[sceneId] } : { ...EMPTY_SCENE };
  },
  chapterCounts(chapterId) {
    const workId = dgWorkId();
    const summary = workId ? dgSummary[workId] : null;
    if (!summary || !chapterId) return null;
    return summary.chapters[chapterId] ? { ...EMPTY_CHAPTER, ...summary.chapters[chapterId] } : { ...EMPTY_CHAPTER };
  },
  totals() {
    const workId = dgWorkId();
    const summary = workId ? dgSummary[workId] : null;
    return summary ? summary.totals : null;
  },
  loaded() {
    const workId = dgWorkId();
    return !!(workId && dgSummary[workId]);
  },
  failed() {
    const workId = dgWorkId();
    return !!(workId && dgFailed[workId]);
  },
  subscribe(fn) { return dgSubs.subscribe(fn); },
  /* 测试用：清空模块级状态 */
  __reset() {
    [dgSummary, dgFetching, dgFetchedAt, dgFailed].forEach((map) => Object.keys(map).forEach((key) => delete map[key]));
  },
};

/* 写作台深改面板里的动作（忽略 / 恢复 / AI 深评 / AI 看这一处）之后广播：计数马上重拉 */
function announceDiagnosisChanged(detail) {
  try { window.dispatchEvent(new CustomEvent("ws:diagnosis-changed", { detail: detail || {} })); } catch (e) {}
}

/* hook：视图挂载时拉一次；诊断变了立刻重拉；目录 / 作品变了节流重拉 */
function useDiagnosisSummary() {
  useStoreTick((bump) => {
    const un = WsDiagnosis.subscribe(bump);
    const onChanged = () => { dgRefresh(dgWorkId(), { force: true }); };
    const onCatalog = () => { dgRefresh(); };
    window.addEventListener("ws:diagnosis-changed", onChanged);
    window.addEventListener("ws:catalog-changed", onCatalog);
    window.addEventListener("ws:work-changed", onChanged);
    dgRefresh(dgWorkId(), { force: true });
    return () => {
      un();
      window.removeEventListener("ws:diagnosis-changed", onChanged);
      window.removeEventListener("ws:catalog-changed", onCatalog);
      window.removeEventListener("ws:work-changed", onChanged);
    };
  });
  return WsDiagnosis;
}

export { WsDiagnosis, announceDiagnosisChanged, useDiagnosisSummary };
