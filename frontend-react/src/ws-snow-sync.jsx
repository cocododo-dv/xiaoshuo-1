import { apiGet, apiPatch, apiPost, apiPut } from "./lib/client.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { s2NormalizeState } from "./ws-snow-model.js";
import { randomSuffix } from "./lib/ids.js";
import { adoptModuleListeners, retireModuleListeners } from "./lib/events.js";
import { isRealWorkId } from "./lib/work-id.js";
import { readyWorkId } from "./lib/ready-work.js";
import { WsWorks } from "./ws-works.jsx";
import {
  BE_BY_FE, FE_BY_BE, SNOW_STEPS, applyCanonPatch, canonFromFE, canonText, feFromCanon, mergeCanon, snowCacheKey,
  stepIsPristine, stepSig, stripFe,
} from "./ws-snow-canon.js";
import {
  SNOW_APPROVE_BODY, activeWork, afterApproveCatalogSync, buildStepFragment, captureAssistantHistory,
  captureDirectionBriefs, captureResync, captureTriage, emitBrief, lastPushed, readSnowSyncState, recordStepHealth,
  setSnowSyncState, shapeStepHealth, snowAssistantHistory, snowBriefs, snowCanon, snowErrorShape, snowHealth,
  snowHydrateOk, snowNotify, snowReadyFlags, snowResync, snowSceneIds, snowTriage, subscribeSnow,
} from "./ws-snow-sync-state.js";
import { adoptServerChapters, snowHydrate } from "./ws-snow-hydrate.js";
import { flushSnowPush, retryPush, schedulePush } from "./ws-snow-push.js";
import {
  chapterPreview, chapterSuggest, chapterTitles, materialize, resolveOrphanedScene, saveChapterPlan,
} from "./ws-snow-chapter-api.js";

/* global window */
/* ==========================================================
   SnowSync — 雪花构思 ↔ snowflake-workspace v2（FE-ALIGN F3）
   ----------------------------------------------------------
   ws_snow_state_v2::<work> 退化为后端真相的写穿缓存：
   - 视图保存（ws:snow-saved）→ 按步 diff → PATCH steps/{key}
     （draft 同时带规范字段喂完备性闸门 + fe_* 键无损保存原型形状）；
     fe_state 变为 done 时顺手 POST approve（闸门不满足则静默跳过）。
   - 启动 / 进入 #construct / 切作品 → GET workspace 水合：
     fe_* 键优先（无损还原），无 fe_* 时从规范字段反推原型形状
     （真·雪花管线生成的项目）；本地 _t 不旧于
     服务端则本地为准（未上行的编辑不被覆盖）。
   - 失效真相在后端（E3 第二步已移除本地 revs/confirmRevs 图）；history（过程
     快照日志）留本地（体积大、跨会话价值低，账本记录）。
   - 水合闸门（2026-09-18）：本会话没成功读到过服务端工作台之前绝不上行；从没动过的
     空白步不是作者的编辑——水合时让位给服务端内容，上行时从不拿去覆盖服务端。
     起因：新浏览器 / 清过缓存的会话里，视图挂载 450ms 后就把空白默认稿落盘并排队上行；
     水合失败（或只是比它慢）时，这份空白稿会 force 覆盖十步——后端对 pending_review
     步是原位改写，未确认的草稿没有历史可回。
   - 07 的章表是分章的只读镜像（重评 R11）：07 上行不带章表，本机那一份总接服务端规范的 chapters；
     改章表只走分章面板（确认写入 = materialize，只保存章表 = saveChapterPlan）和章节编排的改章名。
   2026-09-29 拆分：这里只剩门面（SnowSync 对象、触发面、登记口）。
     ws-snow-canon.js       前端形状 ↔ 规范草稿（纯函数）
     ws-snow-sync-state.js  每部作品的内存表、收下回包、通知
     ws-snow-hydrate.js     水合（闸门与空白步保护）、接服务端章表
     ws-snow-push.js        上行
     ws-snow-chapter-api.js 分章与物化
   ========================================================== */

/* 模块在 HMR / 测试 resetModules 后可能重新执行：先撤掉旧实例的监听器与启动水合定时器 */
retireModuleListeners("ws-snow-sync");

const onSnowSaved = (e) => {
  const key = (e && e.detail) || (activeWork() ? snowCacheKey(activeWork()) : null);
  // 新建作品还没拿到正式 id（临时 id）：它的构思缓存先不上行，免得拿临时 id 去 GET 工作区（复核 Q1-R3）
  if (key && activeWork() && !readyWorkId(WsWorks) && key === snowCacheKey(activeWork())) return;
  if (key) schedulePush(key);
};

/* ---------- 触发面 ---------- */
const onSnowHashChange = () => {
  const h = location.hash || "";
  if (h.indexOf("snowflake") >= 0 || h.indexOf("home") >= 0) snowHydrate(readyWorkId(WsWorks) || "");
};
const onSnowWorkChanged = (e) => { if (e && e.detail) snowHydrate(e.detail); };
window.addEventListener("ws:snow-saved", onSnowSaved);
window.addEventListener("hashchange", onSnowHashChange);
window.addEventListener("ws:work-changed", onSnowWorkChanged);
// 启动水合（等待 WsWorks 就绪）。句柄必须单独保存，便于 HMR/测试模块重载时撤销旧监听器。
const snowHydrateTimer = setTimeout(() => snowHydrate(readyWorkId(WsWorks) || ""), 600);
adoptModuleListeners("ws-snow-sync", () => {
  window.removeEventListener("ws:snow-saved", onSnowSaved);
  window.removeEventListener("hashchange", onSnowHashChange);
  window.removeEventListener("ws:work-changed", onSnowWorkChanged);
  clearTimeout(snowHydrateTimer);
});

const SnowSync = {
  refetch(workId) { return snowHydrate(workId || activeWork(), { force: true }); },
  syncState(workId) { return { ...readSnowSyncState(workId || activeWork()) }; },
  retry(workId) {
    const id = workId || activeWork();
    if (!id) return Promise.reject(new Error("作品尚未就绪"));
    return retryPush(snowCacheKey(id));
  },
  markLocalFailure(error, workId) {
    const id = workId || activeWork();
    setSnowSyncState(id, {
      phase: "error",
      error: snowErrorShape(error, "本机自动保存失败，请先导出构思", "local"),
      pendingSteps: SNOW_STEPS.map(([feKey]) => feKey),
    });
    return readSnowSyncState(id);
  },
  readyToMaterialize(workId) { return !!snowReadyFlags[workId || activeWork()]; },
  /* 阶段 Z：章表在构思之外被改了（章节编排里给雪花的章改名 → 后端写穿章计划）——本机缓存接过服务端这一版 */
  async adoptServerChapters(workId) {
    const id = workId || activeWork();
    if (!id) return false;
    // 与 materialize 同一条纪律：先排空本机还没上行的编辑，本机与服务端才只差服务端刚改的这一块
    try { await flushSnowPush(id); } catch (e) {}
    return adoptServerChapters(id);
  },
  /* 后端步骤键 → 构思视图的步骤键（别的视图要带着意图跳进构思的某一步时用） */
  feStepKey(beKey) { return FE_BY_BE[beKey] || ""; },
  /* 水合闸门：本会话是否已成功读到过服务端工作台（没读到过之前一律不上行） */
  hydrated(workId) { return !!snowHydrateOk[workId || activeWork()]; },
  /* 同步层的通知：fn(kind, detail)，kind = health / resync / brief / sync-state / hydrated / catalog-synced
     （与 ws:snow-* 窗口事件同一条消息；视图改用它之后窗口事件可以收掉）。返回退订函数。 */
  subscribe(fn) { return subscribeSnow(fn); },
  /* 结构化雪花计划导入：这是作者从既有策划稿/外部大纲迁入十步工作台的正常入口。
     UI 一次提交后仍逐步走现有 PATCH + approve 契约，依赖闸门、历史版本、场景身份铸造
     和审计日志均不绕过；任一步失败立即停止，不把半成品谎称为 10/10。 */
  async importCanonicalPlan(workId, payload) {
    const id = workId || activeWork();
    if (!isRealWorkId(id)) throw new Error("请先选择一个作品再导入雪花计划。");
    const stepDrafts = payload && payload.steps && typeof payload.steps === "object" ? payload.steps : payload;
    if (!stepDrafts || typeof stepDrafts !== "object" || Array.isArray(stepDrafts)) {
      throw new Error("结构化计划必须是包含 steps 的 JSON 对象。");
    }
    const requiredKeys = SNOW_STEPS.map(([, beKey]) => beKey);
    const missing = requiredKeys.filter((key) => !stepDrafts[key] || typeof stepDrafts[key] !== "object" || Array.isArray(stepDrafts[key]));
    if (missing.length) throw new Error(`结构化计划缺少十步草稿：${missing.join("、")}`);

    const approvedStepKeys = [];
    const importedAt = Date.now();
    const local = {
      drafts: {}, scaffolds: {}, checks: {}, states: {},
      history: [{
        t: importedAt,
        who: "我",
        action: "导入结构化计划",
        note: "十步依赖顺序保存并由后端批准",
        key: "planning",
        snap: null,
      }],
      _t: importedAt,
    };
    for (const [feKey, beKey] of SNOW_STEPS) {
      const draft = stepDrafts[beKey];
      // 只读回包里的这一步：整份工作台在十步都批完之后另读一次（下面的 GET）
      const patched = await apiPatch(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}?include_workspace=false`, { draft, force: true });
      const patchStep = patched && patched.step;
      if (patchStep) {
        (snowCanon[id] || (snowCanon[id] = {}))[feKey] = stripFe(patchStep.draft || draft);
        (snowHealth[id] || (snowHealth[id] = {}))[feKey] = shapeStepHealth(patchStep);
      }
      const approved = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/approve`, {});
      const approvedStep = approved && approved.step;
      if (!approvedStep || approvedStep.status !== "approved") {
        throw new Error(`「${beKey}」未得到后端批准，导入已停止。`);
      }
      (snowCanon[id] || (snowCanon[id] = {}))[feKey] = stripFe(approvedStep.draft || draft);
      (snowHealth[id] || (snowHealth[id] = {}))[feKey] = shapeStepHealth(approvedStep);
      const fe = feFromCanon(feKey, approvedStep.draft || draft);
      if (fe && fe.text != null) local.drafts[feKey] = fe.text;
      if (fe && fe.scaffold) local.scaffolds[feKey] = fe.scaffold;
      local.states[feKey] = "done";
      approvedStepKeys.push(beKey);
    }

    const workspace = await apiGet(`/api/v2/projects/${id}/snowflake-workspace`);
    snowHydrateOk[id] = true; // 导入逐步写过、此刻又读到了服务端工作台：本机缓存就是服务端真相，水合闸门放行
    snowReadyFlags[id] = !!(workspace && workspace.ready_to_materialize);
    captureResync(id, workspace || {});
    captureDirectionBriefs(id, workspace || {});
    captureAssistantHistory(id, workspace || {});
    const normalizedLocal = s2NormalizeState(local);
    try { localStorage.setItem(snowCacheKey(id), JSON.stringify(normalizedLocal)); } catch (e) {}
    // The import already wrote and approved every step. Seed the autosave
    // dedupe ledger with that exact local snapshot, otherwise the history/local
    // state update emitted immediately after the modal closes schedules a
    // second ten-step PATCH wave which can race materialization back to 409.
    const mine = lastPushed[id] || (lastPushed[id] = {});
    for (const [feKey] of SNOW_STEPS) {
      const fragment = buildStepFragment(feKey, normalizedLocal, id);
      mine[feKey] = { sig: stepSig(fragment), state: fragment.fe_state };
    }
    snowNotify("health", id);
    snowNotify("hydrated", id);
    setSnowSyncState(id, { phase: "synced", pendingSteps: [], error: null, localSavedAt: importedAt, lastSyncedAt: Date.now() });
    return { approvedStepKeys, readyToMaterialize: snowReadyFlags[id], workspace };
  },
  /* 物化后回流状态：pending = 构思 9/10 步领先于目录场景卡的场（后端 resync_status 真相）。
     随 ws:snow-resync 事件更新（hydrate 全量 / 9-10 步保存后的强制重拉 / resync 回包）。 */
  resyncStatus(workId) { return snowResync[workId || activeWork()] || { pendingCount: 0, pendingScenes: [] }; },
  /* 把构思的改动写回目录场景卡（POST /resync：SceneCard 三拍/POV/题名 + 章 brief），
     成功后目录重拉，写作台 / AI 起草台即拿到最新场景卡。scenePlanIds 缺省 = 全部待同步场。
     返回 { synced, skipped, results, notice }；失败上抛由调用方诚实提示。
     notice = 后端「有一部分没能回流」的如实交代（目前只有一种：场要搬进的章还没被
     「整理为章节结构」写进目录，外键指不过去），调用方必须显示它 —— 只报 synced
     会让作者以为回流做完了，目录其实还停在上一版章节结构。 */
  async resync(workId, scenePlanIds) {
    const id = workId || activeWork();
    const body = Array.isArray(scenePlanIds) && scenePlanIds.length ? { scene_plan_ids: scenePlanIds } : {};
    const data = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/resync`, body);
    if (data && data.workspace) captureResync(id, data.workspace);
    try { await WsCatalog.refresh(id); } catch (e) {}
    const results = (data && data.results) || [];
    return {
      synced: results.filter(r => r && r.synced).length,
      skipped: results.filter(r => r && !r.synced).length,
      results,
      notice: (data && data.notice) || null,
    };
  },
  /* 后端 per-step 权威健康（feKey -> {score,status,gaps,nextActions,missingFields,gateSatisfied,...}）；
     视图用它显示「后端评估」区，与本地实时估算区分。随 ws:snow-health / ws:snow-hydrated 更新。 */
  health(workId) { return snowHealth[workId || activeWork()] || {}; },
  /* 阶段 G：确认过又改过的步骤（revised_after_approval）由作者显式重新确认——POST approve，
     回包刷新本步与整个工作台的权威健康（下游失效在这一刻可见）。失败上抛由调用方诚实提示。 */
  async approveStep(workId, feKey) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    if (!id || !beKey) throw new Error("步骤未知，无法确认");
    const res = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/approve`, SNOW_APPROVE_BODY);
    const mine = lastPushed[id] || (lastPushed[id] = {});
    mine[feKey] = { ...(mine[feKey] || {}), state: "done", approvalPending: false };
    if (res && res.step) recordStepHealth(id, feKey, res.step, res.workspace);
    afterApproveCatalogSync(id, res);
    return (snowHealth[id] || {})[feKey] || null;
  },
  /* 本步是否「确认过又改了、等作者重新确认」（后端 pending_review + revised_after_approval）。 */
  needsReconfirm(workId, feKey) {
    const h = ((snowHealth[workId || activeWork()] || {})[feKey]) || {};
    return h.beStatus === "pending_review" && !!h.revisedAfterApproval;
  },
  /* 阶段 E：「已复核」在服务端留痕——POST accept-stale，回包刷新权威健康。只对后端 status=stale 的步有意义。 */
  async acceptStale(workId, feKey, note) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    if (!id || !beKey) throw new Error("步骤未知，无法记录复核");
    const res = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/accept-stale`, note ? { note } : {});
    if (res && res.step) recordStepHealth(id, feKey, res.step, res.workspace); // 下游闸门随之变化
    return (snowHealth[id] || {})[feKey] || null;
  },
  /* 阶段 E：上游改了什么——按本步 artifact.input_refs（确认/写入时消费的上游 step_run_id）对照各上游
     现在的 step_run_id，变了的上游拉 history?include_draft=true，把消费版本与现在版本折成带栏名的分步文本并排返回
     （视角名、04 名册与场序取本机现在的脚手架）。没有记录（旧数据）或上游没变 → 空数组，视图据此提示。 */
  async upstreamChanges(workId, feKey) {
    const id = workId || activeWork();
    const health = snowHealth[id] || {};
    let local = null;
    try { local = JSON.parse(localStorage.getItem(snowCacheKey(id))); } catch (e) {}
    const scaffolds = (local && local.scaffolds) || {};
    const refs = ((health[feKey] || {}).inputRefs) || {};
    const changed = Object.entries(refs).map(([beKey, oldRunId]) => {
      const upFe = FE_BY_BE[beKey];
      if (!upFe) return null;
      const now = (health[upFe] || {}).stepRunId || null;
      return (oldRunId && now && oldRunId !== now) ? { feKey: upFe, beKey, oldRunId, newRunId: now } : null;
    }).filter(Boolean);
    const items = [];
    for (const c of changed) {
      const hist = await apiGet(`/api/v2/projects/${id}/snowflake-workspace/steps/${c.beKey}/history?include_draft=true`);
      const rows = (hist && Array.isArray(hist.items)) ? hist.items : [];
      const oldRow = rows.find(r => r.step_run_id === c.oldRunId) || null;
      const newRow = rows.find(r => r.step_run_id === c.newRunId) || rows[0] || null;
      items.push({
        ...c,
        oldFound: !!oldRow,
        oldVersion: oldRow ? oldRow.version : null,
        newVersion: newRow ? newRow.version : null,
        oldText: oldRow ? canonText(c.feKey, oldRow.draft, scaffolds) : "",
        newText: newRow ? canonText(c.feKey, newRow.draft, scaffolds) : "",
      });
    }
    return items;
  },
  /* 「采纳并结构化」接缝（AI 融合 F1）：generate 回包的 step 落进本地——
     刷新 canon 镜像（后续 push 在它之上保真合并）与权威健康，并把规范草稿
     反推成原型形状 {text?, scaffold?} 交视图写入 drafts/scaffolds。 */
  applyServerStep(workId, feKey, step) {
    const id = workId || activeWork();
    if (!id || !feKey || !step) return null;
    const canon = stripFe(step.draft || {});
    (snowCanon[id] || (snowCanon[id] = {}))[feKey] = canon;
    recordStepHealth(id, feKey, step);
    return feFromCanon(feKey, canon);
  },
  /* 视图把当前内存态折成本步规范草稿（如场景分诊的 draft_override）——
     避免「刚编辑还没自动保存上行」的竞态；不含 fe_* 写穿键。 */
  canonDraft(feKey, cache) { return canonFromFE(feKey, cache || {}); },
  /* structuredGenerate 的 draft_override：与上行 PATCH 完全同源的规范草稿
     （服务端 canon 镜像 ⊕ 当前脚手架，数组按 id 对位、FE 成员为准）——
     generate 的底稿据此看到「刚加的角色/场还没自动保存上行」的内容，
     且成员 id（character_id/scene_id）齐全，服务端能按 id 对位合并。 */
  pushCanon(feKey, cache, workId) {
    const id = workId || activeWork();
    const canon = canonFromFE(feKey, cache || {});
    const server = id ? ((snowCanon[id] || {})[feKey] || null) : null;
    return server ? mergeCanon(server, canon) : canon;
  },
  /* 教练 candidate_patch 落地：以「服务端 canon 镜像 ⊕ 当前脚手架」为底，
     咨询式合并补丁（空值不清空、按 id 对位、不删成员），反推回原型形状。 */
  applyCanonPatch(feKey, cache, patch, workId) {
    const id = workId || activeWork();
    const canon = canonFromFE(feKey, cache || {});
    const server = id ? ((snowCanon[id] || {})[feKey] || null) : null;
    const base = server ? mergeCanon(server, canon) : canon;
    // 07 的章表只从服务端来（重评 R11）：补丁里的章表不落进本机那一份——它也上不去，只会显示一张下一次水合就消失的表
    const advice = { ...(patch || {}) };
    if (feKey === "outline") delete advice.chapters;
    return feFromCanon(feKey, applyCanonPatch(base, advice));
  },
  /* 阶段 T：本步的作者意图要点（后端 direction_briefs 镜像）；没有 → null */
  directionBrief(workId, feKey) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    return (id && beKey && snowBriefs[id] && snowBriefs[id][beKey]) || null;
  },
  /* 生成 / 批准等回包自带整份 workspace 时顺手刷新要点镜像 */
  captureBriefs(workId, ws) { return captureDirectionBriefs(workId || activeWork(), ws); },
  /* 教练回包带本步最新要点 → 直接落镜像 */
  setDirectionBrief(workId, feKey, brief) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    if (!id || !beKey) return;
    const bucket = snowBriefs[id] || (snowBriefs[id] = {});
    if (brief) bucket[beKey] = brief; else delete bucket[beKey];
    emitBrief(id);
  },
  /* 作者编辑要点：乐观写入（本地立刻反映），失败回滚并上抛由视图诚实提示。
     lines 是作者要的完整列表（缺席的活动条目 = 撤下；带 status=active 的已撤条目 = 恢复）；
     inherit_upstream 可单独改。 */
  async saveDirectionBrief(workId, feKey, { lines, inherit_upstream } = {}) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    if (!id || !beKey) throw new Error("步骤未知，无法保存要点");
    const bucket = snowBriefs[id] || (snowBriefs[id] = {});
    const prev = bucket[beKey] ? JSON.parse(JSON.stringify(bucket[beKey])) : null;
    const body = {};
    if (Array.isArray(lines)) {
      body.lines = lines.map(l => {
        const item = { kind: l.kind, scope: l.scope, text: l.text, status: l.status || "active" };
        if (l.line_id && !String(l.line_id).startsWith("local_")) item.line_id = l.line_id;
        return item;
      });
    }
    if (inherit_upstream != null) body.inherit_upstream = !!inherit_upstream;
    const optimistic = { ...(prev || { step_key: beKey, revision: 0, inherit_upstream: true, inherited: [], lines: [], active_count: 0 }) };
    if (Array.isArray(lines)) {
      const known = new Map((prev ? prev.lines : []).map(l => [l.line_id, l]));
      const keep = new Set();
      const next = lines.map(l => {
        const base = l.line_id ? known.get(l.line_id) : null;
        if (l.line_id) keep.add(l.line_id);
        const same = !!base && base.text === l.text && base.kind === l.kind && base.scope === l.scope;
        return { ...(base || {}), line_id: l.line_id || `local_${randomSuffix(8)}`,
          kind: l.kind, scope: l.scope, text: l.text, status: l.status || "active", dismissed_by: null,
          origin: same ? (base.origin || "coach") : "author" };
      });
      (prev ? prev.lines : []).forEach(l => {
        if (keep.has(l.line_id)) return;
        next.push(l.status === "dismissed" ? l : { ...l, status: "dismissed", dismissed_by: "author" });
      });
      optimistic.lines = next;
      optimistic.active_count = next.filter(l => l.status === "active").length;
    }
    if (inherit_upstream != null) optimistic.inherit_upstream = !!inherit_upstream;
    bucket[beKey] = optimistic;
    emitBrief(id);
    try {
      const res = await apiPut(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/direction-brief`, body);
      if (res && res.direction_brief) bucket[beKey] = res.direction_brief;
      emitBrief(id);
      return bucket[beKey];
    } catch (err) {
      if (prev) bucket[beKey] = prev; else delete bucket[beKey];
      emitBrief(id);
      throw err;
    }
  },
  /* 本步最新一版生成消费了哪一版要点（health.direction_brief）：要点改过而本稿没跟上 → stale */
  briefUsage(workId, feKey) {
    const id = workId || activeWork();
    const brief = this.directionBrief(id, feKey);
    const used = (((snowHealth[id] || {})[feKey]) || {}).directionBrief || null;
    const current = brief ? (Number(brief.revision) || 0) : 0;
    const active = brief ? (Number(brief.active_count) || 0) : 0;
    const inheritedCount = brief && Array.isArray(brief.inherited) && brief.inherit_upstream !== false ? brief.inherited.length : 0;
    const hasBrief = active > 0 || inheritedCount > 0;
    const usedRevision = used && typeof used.revision === "number" ? used.revision : null;
    if (!hasBrief) return { hasBrief: false, stale: false, disabled: false, usedRevision, currentRevision: current };
    const disabled = !!used && used.used === false;
    const stale = !!used && !disabled && usedRevision != null && usedRevision < current;
    return { hasBrief: true, stale, disabled, usedRevision, currentRevision: current };
  },
  /* 阶段 M：工作台里存档的分诊（rowUid -> item），刷新后第 10 步也能看到上次的分诊。 */
  triageItems(workId) { return snowTriage[workId || activeWork()] || null; },
  /* 教练日志（后端 assistant_history）的镜像：水合时随工作台收下，教练页不必另拉一整份工作台（审计 F02-11）。
     还没收到过（这次会话还没水合成）是 null；收到过、日志是空的是 []。返回副本。 */
  assistantHistory(workId) {
    const list = snowAssistantHistory[workId || activeWork()];
    return Array.isArray(list) ? list.slice() : null;
  },
  /* 教练 / 生成的回包带着整条日志：教练页记回镜像，视图重挂载后第一次打开教练页读到的是这次会话的最新日志 */
  rememberAssistantHistory(workId, list) {
    const id = workId || activeWork();
    if (id && Array.isArray(list)) snowAssistantHistory[id] = list.slice();
  },
  /* 阶段 R：scene_id ↔ 09 row_uid 对照（来自最近一次水合的工作台） */
  rowUidForSceneId(workId, sceneId) { const m = snowSceneIds[workId || activeWork()]; return (m && m.rowBySceneId[sceneId]) || ""; },
  sceneIdForRow(workId, rowUid) { const m = snowSceneIds[workId || activeWork()]; return (m && m.sceneByRow[rowUid]) || ""; },
  /* 阶段 R：作者对某一场的分诊裁定（pass / maybe / rewrite / cut）写回服务端——原著的 Yes / No / Maybe 由作者拍板；
     cut（待删）是作者专用：不建卡、不阻断、三拍留在构思里。返回服务端的条目（含 triage_id），失败抛错。 */
  async saveTriageVerdict(workId, item) {
    const id = workId || activeWork();
    const status = String((item && item.status) || "").trim().toLowerCase();
    if (!["pass", "maybe", "rewrite", "cut"].includes(status)) throw new Error("非法的裁定");
    const sceneId = (item && item.scene_id) || this.sceneIdForRow(id, item && item.row_uid);
    if (!(item && item.scene_plan_id) && !sceneId) throw new Error("这一场还没同步到服务端，稍后再裁定");
    const data = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/scene-triage`, { items: [{
      triage_id: (item && item.triage_id) || "", scene_plan_id: (item && item.scene_plan_id) || "", scene_id: sceneId,
      status, recommended_status: (item && item.recommended_status) || "",
      notes: (item && item.notes) || "", missing_fields: (item && item.missing_fields) || [], fix_steps: (item && item.fix_steps) || [],
      repair_patch: (item && item.repair_patch) || {},
    }] });
    if (data && data.workspace) { captureTriage(id, data.workspace); snowReadyFlags[id] = !!data.workspace.ready_to_materialize; }
    const saved = ((data && data.items) || []).find(it => it && (it.scene_id === sceneId || (item && item.scene_plan_id && it.scene_plan_id === item.scene_plan_id)));
    return saved || null;
  },
  /* 阶段 M：「略过此步」写回服务端（generate skip=true，理由必填；只有 04–08 可略过）——以前只在本地，
     后端永远收不到，硬闸门于是静默卡住。回包刷新本步与整个工作台的健康。 */
  async skipStep(workId, feKey, reason) {
    const id = workId || activeWork();
    const beKey = BE_BY_FE[feKey];
    if (!id || !beKey) throw new Error("步骤未知，无法略过");
    const res = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/steps/${beKey}/generate`, { skip: true, skip_reason: String(reason || "").trim() });
    const mine = lastPushed[id] || (lastPushed[id] = {});
    mine[feKey] = { ...(mine[feKey] || {}), state: "skip", approvalPending: false };
    if (res && res.step) recordStepHealth(id, feKey, res.step, res.workspace);
    return (snowHealth[id] || {})[feKey] || null;
  },
  /* 分章：预览 / AI 建议 / AI 起章名 / 处置孤儿场 / 只保存章表 / 物化——实现在 ws-snow-chapter-api.js */
  chapterPreview,
  chapterSuggest,
  chapterTitles,
  resolveOrphanedScene,
  saveChapterPlan,
  materialize,
};

/* 台子上改的章名被后端写穿到章计划后，目录在重拉之前等本机雪花缓存接过服务端章表（登记口见 ws-catalog.jsx）。
   经 SnowSync 的属性调用：单测会替换它。 */
WsCatalog.onPlanTitlesSynced((workId) => SnowSync.adoptServerChapters(workId));

Object.assign(window, { SnowSync });

// mergeCanon / applyCanonPatch / feFromCanon / canonFromFE 一并导出：供 store 单测
// 直接验证「保真合并」「咨询式补丁」与「规范字段 ↔ 原型形状」的往返契约
export { SnowSync, mergeCanon, applyCanonPatch, feFromCanon, canonFromFE, stepIsPristine };
