import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { apiGet } from "./lib/client.js";
import { createSubscribers, useStoreTick } from "./lib/store-utils.js";

/* ==========================================================
   WsDiagnosis — 一本书每一场 / 每一章开着的诊断发现数（2026-09-22 场景诊断统一；第三轮改成随写回传）
   ----------------------------------------------------------
   整本书的汇总只在视图挂载 / 换作品时读一次（GET /api/v1/projects/{id}/diagnosis-summary）。之后每一次会改动
   发现的写入——作者稿保存、深改面板里的忽略 / 恢复、AI 深评、AI 看这一处、成稿中心的 AI 通读——都在响应里带
   diagnosis_rollup（这一场所在那一章的章条目 + 章里每一场的条目），store 把它合进表、本地汇总 totals
   （与服务端 scene_diagnosis.summarize_counts 同一条规则），订阅的视图即得；不再按目录事件节流拉取。
   忽略 / 恢复先按面板里的清单本地记一笔（applySceneFindings），服务端的 rollup 到了再以它为准。
   起草台归档终稿（服务端改了这一场的正文）时 refreshScene 只拉这一章的 rollup；目录成员变了（场进了回收站）
   只把不在目录里的条目剪掉。读不到就当没有计数：这是角标，不是闸门。
   ESM 模块，不写 window。
   ========================================================== */

const dgSubs = createSubscribers();
const dgSummary = {};       // workId → { totals, chapters, scenes }
const dgFetching = {};      // workId → Promise
const dgFailed = {};        // workId → true
const dgSceneFetching = {}; // sceneId → Promise
const DG_COUNT_KEYS = ["open", "blocking", "revision", "taste", "info", "ignored", "stale"];

function dgWorkId() {
  try { const id = WsWorks.activeId(); return id && id !== "__loading__" ? id : null; } catch (e) { return null; }
}

const EMPTY_SCENE = { open: 0, blocking: 0, revision: 0, taste: 0, info: 0, ignored: 0, stale: 0, ai_status: "not_run", review_status: "not_run" };
const EMPTY_CHAPTER = { open: 0, blocking: 0, chapter_level: 0, chapter_level_blocking: 0, scenes: 0, scenes_with_findings: 0, ai_status: "not_run" };

/* totals 从场 / 章条目汇总：与服务端 summarize_counts 同一条规则（章级发现算开着的，章条目里带 chapter_level） */
function dgSummarize(scenes, chapters) {
  const totals = { open: 0, blocking: 0, revision: 0, taste: 0, info: 0, ignored: 0, stale: 0, scenes: 0, scenes_with_text: 0, scenes_with_findings: 0, ai_reviewed_scenes: 0, chapters_reviewed: 0 };
  Object.values(scenes || {}).forEach((entry) => {
    totals.scenes += 1;
    if (entry.text_layer && entry.text_layer !== "none") totals.scenes_with_text += 1;
    DG_COUNT_KEYS.forEach((key) => { totals[key] += Number(entry[key] || 0); });
    if (Number(entry.open || 0)) totals.scenes_with_findings += 1;
    if (entry.ai_status && entry.ai_status !== "not_run") totals.ai_reviewed_scenes += 1;
  });
  Object.values(chapters || {}).forEach((counts) => {
    if (counts.ai_status && counts.ai_status !== "not_run") totals.chapters_reviewed += 1;
    totals.open += Number(counts.chapter_level || 0);
    totals.blocking += Number(counts.chapter_level_blocking || 0);
  });
  return totals;
}

/* 一场的计数从面板里的发现清单算（忽略 / 恢复不必等往返）：与服务端 _finding_counts 同一条规则 */
function dgCountsFromFindings(findings, base) {
  const list = Array.isArray(findings) ? findings : [];
  const open = list.filter((f) => !f.ignored);
  const by = (severity) => open.filter((f) => f.severity === severity).length;
  return {
    ...EMPTY_SCENE, ...(base || {}),
    open: open.length, ignored: list.length - open.length, stale: open.filter((f) => f.stale).length,
    blocking: by("blocking"), revision: by("revision"), taste: by("taste"), info: by("info"),
  };
}

/* 一章的 open / blocking / scenes_with_findings 由它的场条目重算（章级发现的数目留在 chapter_level 里） */
function dgRechapter(scenes, chapters, chapterId) {
  if (!chapterId) return chapters;
  const base = { ...EMPTY_CHAPTER, ...(chapters[chapterId] || {}) };
  const mine = Object.values(scenes).filter((entry) => entry.chapter_id === chapterId);
  return {
    ...chapters,
    [chapterId]: {
      ...base,
      scenes: mine.length,
      open: mine.reduce((sum, entry) => sum + Number(entry.open || 0), 0) + Number(base.chapter_level || 0),
      blocking: mine.reduce((sum, entry) => sum + Number(entry.blocking || 0), 0) + Number(base.chapter_level_blocking || 0),
      scenes_with_findings: mine.filter((entry) => Number(entry.open || 0) > 0).length,
    },
  };
}

function dgEnsure(workId) {
  if (!dgSummary[workId]) dgSummary[workId] = { totals: dgSummarize({}, {}), chapters: {}, scenes: {} };
  return dgSummary[workId];
}

function dgCommit(workId, scenes, chapters) {
  dgSummary[workId] = { totals: dgSummarize(scenes, chapters), chapters, scenes };
  delete dgFailed[workId];
  dgSubs.notify();
}

function dgRefresh(workId = dgWorkId()) {
  if (!workId) return Promise.resolve();
  if (dgFetching[workId]) return dgFetching[workId];
  dgFetching[workId] = apiGet(`/api/v1/projects/${encodeURIComponent(workId)}/diagnosis-summary`)
    .then((data) => {
      const scenes = (data && data.scenes) || {};
      const chapters = (data && data.chapters) || {};
      dgSummary[workId] = { totals: (data && data.totals) || dgSummarize(scenes, chapters), chapters, scenes };
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

/* 一次写入回传的 rollup：{ project_id?, chapter_id, chapters: {id: counts}, scenes: {id: counts} }。
   服务端整章算，章里每一场都在里面：先清掉这一章旧的场条目再合并（进了回收站的场随之消失）。 */
function dgApplyRollup(rollup, workId = dgWorkId()) {
  if (!rollup || typeof rollup !== "object" || !workId) return false;
  if (rollup.project_id && rollup.project_id !== workId) return false;
  const summary = dgEnsure(workId);
  const chapterIds = new Set(Object.keys(rollup.chapters || {}));
  const scenes = {};
  Object.entries(summary.scenes).forEach(([id, entry]) => { if (!chapterIds.has(entry.chapter_id)) scenes[id] = entry; });
  Object.entries(rollup.scenes || {}).forEach(([id, entry]) => { scenes[id] = { ...EMPTY_SCENE, ...entry }; });
  const chapters = { ...summary.chapters };
  Object.entries(rollup.chapters || {}).forEach(([id, counts]) => { chapters[id] = { ...EMPTY_CHAPTER, ...counts }; });
  dgCommit(workId, scenes, chapters);
  return true;
}

/* 深改面板里忽略 / 恢复了一条：按面板里的清单先记一笔，章条目按差额重算 */
function dgApplySceneFindings(sceneId, findings, workId = dgWorkId()) {
  if (!sceneId || !workId) return false;
  const summary = dgEnsure(workId);
  const previous = summary.scenes[sceneId] || null;
  const next = dgCountsFromFindings(findings, previous);
  const scenes = { ...summary.scenes, [sceneId]: next };
  dgCommit(workId, scenes, dgRechapter(scenes, summary.chapters, next.chapter_id));
  return true;
}

/* 目录成员变了：不在目录里的场 / 章剪掉（新加的场没有正文，问到就是零，不必拉） */
function dgReconcile(workId = dgWorkId()) {
  if (!workId || !dgSummary[workId]) return;
  let chapters;
  try { chapters = WsCatalog.get() || []; } catch (e) { return; }
  const sceneIds = new Set();
  const chapterIds = new Set();
  chapters.forEach((chapter) => {
    if (chapter && chapter.backendId) chapterIds.add(chapter.backendId);
    (chapter && chapter.scenes ? chapter.scenes : []).forEach((scene) => { if (scene && scene.backendId) sceneIds.add(scene.backendId); });
  });
  if (!sceneIds.size && !chapterIds.size) return;
  const summary = dgSummary[workId];
  const keptScenes = Object.keys(summary.scenes).filter((id) => sceneIds.has(id));
  const keptChapters = Object.keys(summary.chapters).filter((id) => chapterIds.has(id));
  if (keptScenes.length === Object.keys(summary.scenes).length && keptChapters.length === Object.keys(summary.chapters).length) return;
  const scenes = {};
  keptScenes.forEach((id) => { scenes[id] = summary.scenes[id]; });
  let next = {};
  keptChapters.forEach((id) => { next[id] = summary.chapters[id]; });
  keptChapters.forEach((id) => { next = dgRechapter(scenes, next, id); });
  dgCommit(workId, scenes, next);
}

function dgRefreshScene(sceneId, workId = dgWorkId()) {
  if (!sceneId || !workId) return Promise.resolve();
  if (dgSceneFetching[sceneId]) return dgSceneFetching[sceneId];
  dgSceneFetching[sceneId] = apiGet(`/api/v1/scenes/${encodeURIComponent(sceneId)}/diagnosis-rollup`)
    .then((rollup) => { dgApplyRollup(rollup, workId); })
    .catch(() => {})
    .finally(() => { delete dgSceneFetching[sceneId]; });
  return dgSceneFetching[sceneId];
}

/* 章运行（后台作业逐场归档终稿）：每完成一场就拉一次这一章的 rollup；同一章在途的只拉一次 */
function dgRefreshChapter(chapterId, workId = dgWorkId()) {
  if (!chapterId || !workId) return Promise.resolve();
  const key = `chapter:${chapterId}`;
  if (dgSceneFetching[key]) return dgSceneFetching[key];
  dgSceneFetching[key] = apiGet(`/api/v1/chapters/${encodeURIComponent(chapterId)}/diagnosis-rollup`)
    .then((rollup) => { dgApplyRollup(rollup, workId); })
    .catch(() => {})
    .finally(() => { delete dgSceneFetching[key]; });
  return dgSceneFetching[key];
}

const WsDiagnosis = {
  refresh(workId) { return dgRefresh(workId || dgWorkId()); },
  /* 写入的响应里带的 diagnosis_rollup → 合进表；返回是否用上了 */
  applyRollup(rollup) { return dgApplyRollup(rollup); },
  /* 深改面板里忽略 / 恢复后按清单记一笔（服务端的 rollup 到了再以它为准） */
  applySceneFindings(sceneId, findings) { return dgApplySceneFindings(sceneId, findings); },
  /* 服务端改了这一场的正文（起草台归档终稿）：只拉这一章 */
  refreshScene(sceneId) { return dgRefreshScene(sceneId); },
  /* 章运行的后台作业归档了几场：只拉这一章 */
  refreshChapter(chapterId) { return dgRefreshChapter(chapterId); },
  reconcile() { dgReconcile(); },
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
    [dgSummary, dgFetching, dgFailed, dgSceneFetching].forEach((map) => Object.keys(map).forEach((key) => delete map[key]));
  },
  __summarize: dgSummarize,
};

/* 写作台深改面板 / 成稿中心里的动作之后广播。detail.rollup（写入响应里的 diagnosis_rollup）或
   detail.findings（面板里的清单）带着计数一起来，别的视图不必重拉；什么都没带才重拉一次。 */
function announceDiagnosisChanged(detail) {
  try { window.dispatchEvent(new CustomEvent("ws:diagnosis-changed", { detail: detail || {} })); } catch (e) {}
}

function dgOnChanged(event) {
  const detail = (event && event.detail) || {};
  if (detail.rollup && dgApplyRollup(detail.rollup)) return;
  if (detail.sceneId && Array.isArray(detail.findings) && dgApplySceneFindings(detail.sceneId, detail.findings)) return;
  dgRefresh(dgWorkId());
}

/* hook：视图挂载 / 换作品时读一次整本书；写入随响应推送；目录成员变了只剪掉不在目录里的条目 */
function useDiagnosisSummary() {
  useStoreTick((bump) => {
    const un = WsDiagnosis.subscribe(bump);
    const onWork = () => { dgRefresh(dgWorkId()); };
    const onCatalog = () => { dgReconcile(); };
    window.addEventListener("ws:diagnosis-changed", dgOnChanged);
    window.addEventListener("ws:catalog-changed", onCatalog);
    window.addEventListener("ws:work-changed", onWork);
    dgRefresh(dgWorkId());
    return () => {
      un();
      window.removeEventListener("ws:diagnosis-changed", dgOnChanged);
      window.removeEventListener("ws:catalog-changed", onCatalog);
      window.removeEventListener("ws:work-changed", onWork);
    };
  });
  return WsDiagnosis;
}

export { WsDiagnosis, announceDiagnosisChanged, useDiagnosisSummary };
