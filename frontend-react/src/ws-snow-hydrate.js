import { apiGet } from "./lib/client.js";
import { emit } from "./lib/events.js";
import {
  BE_STATE_TO_FE, FE_BY_BE, SNOW_STEPS, canonHasContent, feFromCanon, snowCacheKey, stepIsPristine, stepSig, stripFe,
} from "./ws-snow-canon.js";
import {
  buildStepFragment, captureDirectionBriefs, captureResync, captureTriage, captureWorkspaceHealth, lastPushed,
  readSnowSyncState, requestPush, setSnowSyncState, shapeStepHealth, snowCanon, snowErrorShape, snowHealth,
  snowHydrateInflight, snowHydrateOk, snowHydratedOnce, snowNotify, snowReadyFlags, snowUnsupported,
} from "./ws-snow-sync-state.js";

/* ==========================================================
   雪花同步 · 水合（2026-09-29 从 ws-snow-sync.jsx 原样搬出）
   读服务端工作台 → 本机缓存；水合闸门（本会话没读到过服务端之前不上行）与空白步保护（2026-09-18）在这里，
   一字未改——它们的单测（ws-snow-sync.test.jsx「水合闸门与空白步保护」）就是证据。
   唯一的改动：发现「本机已确认、服务端仍待审」时经 requestPush 排一次上行（上行模块在加载时登记，
   水合模块不反过来 import 上行模块）；广播经 snowNotify（窗口事件照发）。
   ========================================================== */

/* 水合入口：去重（每个作品自动水合一次，force 强制重拉）+ 串行（同一作品的水合排成一条链，
   调用方 await 到的是「这次水合做完」）。返回 true = 成功读到了服务端工作台。永不 reject。 */
function snowHydrate(workId, opts) {
  const force = !!(opts && opts.force);
  if (!workId || snowUnsupported[workId]) return Promise.resolve(false);
  if (!force && snowHydratedOnce[workId]) return snowHydrateInflight[workId] || Promise.resolve(!!snowHydrateOk[workId]);
  snowHydratedOnce[workId] = true;
  const prior = snowHydrateInflight[workId];
  const run = (prior ? prior.catch(() => false) : Promise.resolve()).then(() => snowHydrateRun(workId)).catch(() => false);
  const tracked = run.finally(() => { if (snowHydrateInflight[workId] === tracked) delete snowHydrateInflight[workId]; });
  snowHydrateInflight[workId] = tracked;
  return tracked;
}
/* 上行前确认本会话读到过服务端：有在途的水合就等它，仍没读到再强制补一次。 */
async function ensureHydrated(workId) {
  if (snowHydrateOk[workId]) return true;
  if (snowUnsupported[workId]) return false;
  if (snowHydrateInflight[workId]) await snowHydrateInflight[workId];
  if (!snowHydrateOk[workId] && !snowUnsupported[workId]) await snowHydrate(workId, { force: true });
  return !!snowHydrateOk[workId];
}

async function snowHydrateRun(workId) {
  let ws = null;
  try {
    ws = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`);
  } catch (e) {
    if (e && (e.status === 409 || e.status === 404)) snowUnsupported[workId] = true;
    else delete snowHydratedOnce[workId]; // 网络类失败：下次再试
    if (!(e && (e.status === 409 || e.status === 404))) {
      setSnowSyncState(workId, { phase: "error", error: snowErrorShape(e, "无法读取服务器构思版本", "hydrate"), pendingSteps: [] });
    }
    return false;
  }
  snowHydrateOk[workId] = true;
  const priorSyncState = readSnowSyncState(workId);
  if (priorSyncState.phase === "idle" || (priorSyncState.phase === "error" && priorSyncState.error && priorSyncState.error.scope === "hydrate")) {
    setSnowSyncState(workId, { phase: "synced", error: null, lastSyncedAt: Date.now() });
  }
  snowReadyFlags[workId] = !!(ws && ws.ready_to_materialize);
  captureResync(workId, ws);
  captureTriage(workId, ws);
  captureDirectionBriefs(workId, ws);
  const remote = { drafts: {}, scaffolds: {}, checks: {}, states: {}, _t: 0 };
  const health = {};
  let any = false;
  const canonMine = snowCanon[workId] || (snowCanon[workId] = {});
  (ws && ws.steps ? ws.steps : []).forEach(step => {
    const feKey = FE_BY_BE[step.step_key];
    if (!feKey) return;
    health[feKey] = shapeStepHealth(step);
    const draft = step.draft || {};
    canonMine[feKey] = stripFe(draft); // 服务端规范草稿镜像：先于 lastPushed 预填，保证 sig 一致
    if (draft.fe_scaffold || draft.fe_text || draft.fe_state) {
      any = true;
      if (draft.fe_text != null) remote.drafts[feKey] = draft.fe_text;
      if (draft.fe_scaffold) remote.scaffolds[feKey] = draft.fe_scaffold;
      if (Array.isArray(draft.fe_checks)) remote.checks[feKey] = draft.fe_checks;
      if (draft.fe_state) remote.states[feKey] = draft.fe_state;
      if (draft.fe_t && draft.fe_t > remote._t) remote._t = draft.fe_t;
      if (draft.fe_meta) {
        // E3 第二步：旧写穿里的 fe_meta.revs / confirmRevs 直接忽略——失效真相在后端
        // G2：跨会话 journal（去快照、cap 20）——视图只对带 snap 的条目给回滚按钮，
        // 还原条目天然只读，不需要视图改动
        if (Array.isArray(draft.fe_meta.history)) remote.history = draft.fe_meta.history;
      }
    } else if (canonHasContent(feKey, draft)) {
      any = true;
      const fe = feFromCanon(feKey, draft);
      if (fe.text != null) remote.drafts[feKey] = fe.text;
      if (fe.scaffold) remote.scaffolds[feKey] = fe.scaffold;
      const st = BE_STATE_TO_FE[step.status];
      remote.states[feKey] = st || (step.step_key === ws.current_step_key ? "active" : "todo");
      if (remote._t < 1) remote._t = 1; // 规范字段水合：极小时间戳，本地编辑永远赢
    }
  });
  snowHealth[workId] = health;
  snowNotify("health", workId);
  if (!any) return true; // 服务端还没有构思数据：保留本地（含种子门控默认）
  // BUG-2 防回退：用后端真相预填 lastPushed（去重账本），使随后第一个 autosave 不再把这些未改动的步骤
  // 全量 re-push。否则新会话 lastPushed 为空 → snowPushKey 全量上行：后端 update_step 对非 pending_review
  // 步走 else 分支新建 pending_review 版本，把「已确认」步静默打回待审，并产生无谓写/approve 噪声。
  // 注意：仅 seed 从后端水合到内容的步骤；本地新增、后端尚无的步骤不 seed，照常上行（不丢失）。
  let approvalRetryNeeded = false;
  try {
    const mine = lastPushed[workId] || (lastPushed[workId] = {});
    const hydratedKeys = new Set([
      ...Object.keys(remote.drafts), ...Object.keys(remote.states), ...Object.keys(remote.scaffolds),
    ]);
    hydratedKeys.forEach(feKey => {
      const frag = buildStepFragment(feKey, remote, workId);
      const approvalPending = frag.fe_state === "done"
        && ((health[feKey] || {}).beStatus === "pending_review")
        && !((health[feKey] || {}).revisedAfterApproval); // 阶段 G：确认后又改的，等作者显式重新确认
      mine[feKey] = {
        sig: stepSig(frag),
        state: frag.fe_state,
        // 本机的 done 是“作者希望确认”，服务端 pending_review 才是权威未批状态。
        // 两者签名完全相同时也必须保留待批准账，否则新会话永远不会再发 approve。
        approvalPending,
      };
      approvalRetryNeeded = approvalRetryNeeded || approvalPending;
    });
  } catch (e) {}
  const key = snowCacheKey(workId);
  let local = null;
  try { local = JSON.parse(localStorage.getItem(key)); } catch (e) {}
  if (local && (local._t || 0) >= remote._t) {
    /* 本地不旧于服务端：本地为准——但只对作者真的动过的步骤成立。视图挂载 450ms 后就会把空白默认稿
       落盘（_t = 此刻，必然比服务端新），水合只要比它慢一点，「本地为准」就会让一份从没水合过的空白稿
       赢过服务端，随后的上行再把空白写回去。所以：本地从没动过的空白步，服务端有内容就接服务端的。 */
    const rescuable = (cache) => SNOW_STEPS.map(([feKey]) => feKey)
      .filter(feKey => stepIsPristine(feKey, cache) && !stepIsPristine(feKey, remote));
    if (rescuable(local).length) {
      // 先让仍挂载的视图把此刻的内存态落盘（同步事件）：胜负已定，重读不改变判定，只避免接回时吃掉最后几百毫秒的键入
      emit("ws:snow-flush-local", { workId });
      try { local = JSON.parse(localStorage.getItem(key)) || local; } catch (e) {}
      const rescued = rescuable(local);
      if (rescued.length) {
        const merged = { ...local, drafts: { ...(local.drafts || {}) }, scaffolds: { ...(local.scaffolds || {}) },
          checks: { ...(local.checks || {}) }, states: { ...(local.states || {}) } };
        rescued.forEach(feKey => {
          ["drafts", "scaffolds", "checks", "states"].forEach(part => {
            if (remote[part] && remote[part][feKey] !== undefined) merged[part][feKey] = remote[part][feKey];
          });
        });
        if (!Array.isArray(local.history) || !local.history.length) { if (Array.isArray(remote.history)) merged.history = remote.history; }
        try { localStorage.setItem(key, JSON.stringify(merged)); } catch (e) {}
        snowNotify("hydrated", workId);
      }
    }
    // 水合本身就发现“本机已确认、服务端仍待审”时主动补批，不再依赖视图恰好
    // 触发一次 autosave 或作者手点重试。尤其是十步草稿预先导入的场景，若只补第 1 步，
    // UI 会误报“服务器已同步”，真正的物化闸门却仍卡在第 2 步。
    if (approvalRetryNeeded) requestPush(key);
    return true; // 本地不旧于服务端：作者动过的步骤以本地为准
  }
  try { localStorage.setItem(key, JSON.stringify(remote)); } catch (e) {}
  snowNotify("hydrated", workId);
  if (approvalRetryNeeded) requestPush(key);
  return true;
}

/* 分章面板确认之后：章表是**服务端**改的（按场景新建、改名、拆章、并章），本机的 07 章节表与 09 行上的
   章标签必须立刻接过来。水合帮不上忙——本机缓存的 _t 总比服务端新，「本地为准」会让旧章表（真实故障里
   是两行「（待补）」）留在 07，下一次 07 上行再把它们当成作者的章表同步回去、把刚确认的分章冲掉。
   调用时机保证安全：materialize 之前已经 flushSnowPush，本机与服务端只差服务端刚改的这一块。 */
async function adoptServerChapters(workId) {
  let ws = null;
  try { ws = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`); } catch (e) { return false; }
  snowReadyFlags[workId] = !!(ws && ws.ready_to_materialize);
  captureResync(workId, ws);
  captureTriage(workId, ws);
  captureWorkspaceHealth(workId, ws);
  const stepDraft = (beKey) => (((ws && ws.steps) || []).find(s => s && s.step_key === beKey) || {}).draft || null;
  const outlineDraft = stepDraft("long_synopsis");
  const sceneDraft = stepDraft("scene_list");
  emit("ws:snow-flush-local", { workId });
  const key = snowCacheKey(workId);
  let local = null;
  try { local = JSON.parse(localStorage.getItem(key)); } catch (e) {}
  if (!local || typeof local !== "object") return false;
  const scaffolds = { ...(local.scaffolds || {}) };
  if (outlineDraft && Array.isArray(outlineDraft.chapters) && outlineDraft.chapters.length) {
    const fresh = (feFromCanon("outline", { ...outlineDraft, paragraphs: [] }).scaffold || {}).chapters || [];
    scaffolds.outline = { ...(scaffolds.outline || {}), chapters: fresh };
  }
  if (sceneDraft && Array.isArray(sceneDraft.scenes) && scaffolds.scenes && Array.isArray(scaffolds.scenes.list)) {
    const titleByRow = {};
    sceneDraft.scenes.forEach(s => { if (s && s.row_uid) titleByRow[s.row_uid] = (s.chapter_title && s.chapter_title !== s.chapter_id) ? s.chapter_title : ""; });
    scaffolds.scenes = { ...scaffolds.scenes, list: scaffolds.scenes.list.map(row => (row && row.id in titleByRow) ? { ...row, chapter: titleByRow[row.id] } : row) };
  }
  const merged = { ...local, scaffolds };
  try { localStorage.setItem(key, JSON.stringify(merged)); } catch (e) { return false; }
  // 服务端规范镜像与去重账一并对齐：这不是作者的编辑，不该引出一次 07 / 09 的上行
  const canonMine = snowCanon[workId] || (snowCanon[workId] = {});
  const mine = lastPushed[workId] || (lastPushed[workId] = {});
  [["outline", outlineDraft], ["scenes", sceneDraft], ["planning", stepDraft("scene_details")]].forEach(([feKey, draft]) => {
    if (!draft) return;
    canonMine[feKey] = stripFe(draft);
    if (mine[feKey]) mine[feKey] = { ...mine[feKey], sig: stepSig(buildStepFragment(feKey, merged, workId)) };
  });
  snowNotify("health", workId);
  snowNotify("hydrated", workId);
  return true;
}

export { snowHydrate, ensureHydrated, adoptServerChapters };
