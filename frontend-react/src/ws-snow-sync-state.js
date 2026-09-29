import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { toStoreError } from "./lib/store-kit.js";
import { emit } from "./lib/events.js";
import { buildStepFragmentFrom, FE_BY_BE } from "./ws-snow-canon.js";

/* ==========================================================
   雪花同步 · 每部作品的内存状态与「收下服务端回包」（2026-09-29 从 ws-snow-sync.jsx 拆出）
   服务端规范草稿镜像、上行去重账、水合闸门的几张表、后端权威健康、回流 / 分诊 / 要点镜像、同步状态，
   以及把 workspace 回包收进这些表的 capture*。水合（ws-snow-hydrate.js）、上行（ws-snow-push.js）、
   分章接口（ws-snow-chapter-api.js）与门面（ws-snow-sync.jsx）都读写这里。
   ========================================================== */

const activeWork = () => { try { return (WsWorks && WsWorks.activeId()) || ""; } catch (e) { return ""; } };

/* 通知：视图一直听的 ws:snow-* 窗口事件照发（别的视图包还在听）；同一条消息也交给 SnowSync.subscribe 的订阅者
   （fn(kind, detail)，kind 是事件名去掉 ws:snow- 前缀：health / resync / brief / sync-state / hydrated / catalog-synced）。 */
const snowListeners = new Set();
function snowNotify(kind, detail) {
  snowListeners.forEach((fn) => { try { fn(kind, detail); } catch (e) { /* 订阅者出错不打断同步 */ } });
  emit("ws:snow-" + kind, detail);
}
function subscribeSnow(fn) {
  snowListeners.add(fn);
  return () => { snowListeners.delete(fn); };
}

/* 服务端规范草稿镜像（保真合并的底，见 ws-snow-canon.js 的 mergeCanon）：workId -> feKey -> 规范草稿（已剥 fe_*） */
const snowCanon = {};
/* 上行去重账：workId -> feKey -> { sig, state, approvalPending } */
const lastPushed = {};

/* 这一步的上行片段（按作品取服务端镜像）：上行、水合预填去重账、接服务端章表共用同一份计算 */
function buildStepFragment(feKey, cache, workId) {
  const serverCanon = workId ? ((snowCanon[workId] || {})[feKey] || null) : null;
  return buildStepFragmentFrom(feKey, cache, serverCanon);
}

/* 一步的服务端回包（PATCH / approve / accept-stale / generate / skip）→ 这一步的权威健康；
   回包带整份 workspace 时顺手刷新所有步骤（批准上游会让下游 stale），然后广播 health */
function recordStepHealth(workId, feKey, step, workspace) {
  (snowHealth[workId] || (snowHealth[workId] = {}))[feKey] = shapeStepHealth(step);
  if (workspace) captureWorkspaceHealth(workId, workspace);
  snowNotify("health", workId);
}

/* 上行由 ws-snow-push.js 登记（水合发现「本机已确认、服务端仍待审」时要补一次上行；水合模块不反过来 import 上行模块） */
let pushScheduler = null;
function registerPushScheduler(fn) { pushScheduler = typeof fn === "function" ? fn : null; }
function requestPush(cacheKey) { if (pushScheduler) pushScheduler(cacheKey); }

const snowHydratedOnce = {};
const snowReadyFlags = {};
const snowUnsupported = {};
/* 水合闸门：workId -> true = 本会话至少成功读到过一次服务端工作台（含「服务端还没有构思数据」）。
   没读到过就不知道本机缓存相对服务端是新是旧，此时上行等于盲写。并发的水合请求合并成一条链。 */
const snowHydrateOk = {};
const snowHydrateInflight = {};
/* 后端 per-step 权威健康（score/status/gaps/completeness）：只读后端真相，
   与前端写穿缓存分开存（避免被本地 save 覆盖）。hydrate 时全量捕获，
   每次 update_step 的 PATCH 响应里带最新 step.health → 增量更新。 */
const snowHealth = {}; // workId -> feKey -> shaped health
/* 整份 workspace 回包 → 刷新所有步骤的权威健康。approve / accept-stale 的回包都带 workspace：
   批准上游会让下游按消费字段置 stale，这里顺手把它们的 stale 状态收进来，「需复核」不必等下一次全量水合。 */
function captureWorkspaceHealth(workId, ws) {
  if (!workId || !ws || !Array.isArray(ws.steps)) return false;
  const bucket = snowHealth[workId] || (snowHealth[workId] = {});
  ws.steps.forEach(step => { const feKey = FE_BY_BE[step && step.step_key]; if (feKey) bucket[feKey] = shapeStepHealth(step); });
  return true;
}
function shapeStepHealth(step) {
  const h = (step && step.health) || {};
  const comp = (step && step.completeness) || {};
  const arr = (v) => (Array.isArray(v) ? v : []);
  return {
    score: typeof h.score === "number" ? h.score : null,
    status: h.status || null,                       // pass / maybe / rewrite
    gaps: arr(h.gaps),
    nextActions: arr(h.next_actions),
    missingFields: arr(comp.missing_fields).length ? arr(comp.missing_fields) : arr(h.missing_fields),
    filled: typeof comp.filled_count === "number" ? comp.filled_count : null,
    total: typeof comp.total_count === "number" ? comp.total_count : null,
    gateSatisfied: !!(step && step.gate_satisfied),
    beStatus: (step && step.status) || null,        // draft / pending_review / approved / skipped / stale
    // 阶段 G：确认过的步骤被改动后是「待重新确认」——不再在键入后自动补批准，等作者显式点「确认本步」
    revisedAfterApproval: !!(step && step.revised_after_approval),
    // 阶段 E：失效真相来自后端——原因、是否已确认仍有效、本版与本步确认时消费的上游版本（step_run_id）
    staleReason: (step && step.stale_reason) || "",
    staleAcceptedAt: (step && step.stale_accepted_at) || null,
    version: typeof (step && step.version) === "number" ? step.version : null,
    stepRunId: (step && step.artifact && step.artifact.step_run_id) || null,
    inputRefs: (step && step.artifact && step.artifact.input_refs && typeof step.artifact.input_refs === "object")
      ? { ...step.artifact.input_refs } : {},
    // 阶段 T：这一版生成消费了哪一版作者意图要点（used / revision / sha），用于「本稿未采用最新要点」提示
    directionBrief: (h.direction_brief && typeof h.direction_brief === "object") ? { ...h.direction_brief } : null,
    // 阶段 U：这一版怎么来的——generation_source（llm / fallback / skip）与按哪个方向生成
    // （{kind: candidate|coach_reply, turn_id, candidate_index, label, sha}）；编辑页 AI 工具条据此写「本稿：按方向「X」生成」
    generationSource: h.generation_source || null,
    direction: (h.direction && typeof h.direction === "object") ? { ...h.direction } : null,
  };
}

/* 物化后回流（FE 补接 resync）：workspace 回包自带 resync_status——物化过的场，
   9/10 步再改动后与目录场景卡（SceneCard）的 diff。pendingCount>0 = 构思领先于目录，
   写作台 / AI 起草台拿到的还是旧三拍。只读后端真相，与写穿缓存分开存。 */
const snowResync = {}; // workId -> { pendingCount, pendingScenes }
const snowSyncStates = {}; // workId -> 本机缓存 / 服务端写穿的诚实状态

/* 同步失败的形状 = store 层统一的 toStoreError + 出在哪一段（scope：remote / hydrate / local） */
function snowErrorShape(error, fallback, scope = "remote") {
  return toStoreError(error, fallback || "同步失败，请稍后重试", { code: "SYNC_FAILED", scope });
}

function setSnowSyncState(workId, patch) {
  if (!workId) return;
  const previous = snowSyncStates[workId] || {
    phase: "idle", pendingSteps: [], error: null, localSavedAt: null, lastSyncedAt: null,
  };
  snowSyncStates[workId] = { ...previous, ...patch };
  snowNotify("sync-state", { workId, state: snowSyncStates[workId] });
}

function readSnowSyncState(workId) {
  return snowSyncStates[workId] || {
    phase: "idle", pendingSteps: [], error: null, localSavedAt: null, lastSyncedAt: null,
  };
}
function shapeResync(ws) {
  const rs = (ws && ws.resync_status) || {};
  const scenes = Array.isArray(rs.pending_scenes) ? rs.pending_scenes : [];
  return {
    pendingCount: typeof rs.pending_count === "number" ? rs.pending_count : scenes.length,
    pendingScenes: scenes.map(s => ({
      scenePlanId: (s && s.scene_plan_id) || "",
      sceneId: (s && s.scene_id) || "",
      title: (s && s.title) || "",
      changedFields: Array.isArray(s && s.changed_fields) ? s.changed_fields : [],
    })),
  };
}
/* 阶段 X「确认即同步」：确认 09 / 10 时带 sync_catalog，服务端把已物化的场景卡当场跟上这一版构思
   （回包 catalog_sync）。这里接住结果：待同步横幅立刻按回包更新、目录重拉（写作台 / AI 起草台读到新卡）、
   并广播给视图出一句回执——同步了几场、哪几场留给作者自己看差异。 */
const SNOW_APPROVE_BODY = { sync_catalog: true };
function afterApproveCatalogSync(workId, res) {
  const sync = res && res.catalog_sync;
  if (!workId || !sync) return;
  if (res.workspace) captureResync(workId, res.workspace);
  if (sync.synced_count > 0) {
    try { WsCatalog.__refresh(workId); } catch (e) {}
  }
  if (sync.synced_count > 0 || sync.held_count > 0) {
    snowNotify("catalog-synced", { workId, ...sync });
  }
}

function captureResync(workId, ws) {
  if (!workId || !ws || !ws.resync_status) return;
  snowResync[workId] = shapeResync(ws);
  snowNotify("resync", workId);
}

/* 阶段 M：分诊结果随工作台回包水合——以前只活在组件内存里，一刷新就没了。
   后端条目按 scene_id 记，09 的行按 row_uid 记：用同一份工作台里的场景列表把两者对上。 */
const snowTriage = {}; // workId -> { items: rowUid -> item, at, source }
// 阶段 R：scene_id ↔ row_uid 的对照（成稿中心按 scene_id 回跳第 10 步、裁定按 scene_id 存档）
const snowSceneIds = {}; // workId -> { rowBySceneId, sceneByRow }
/* 阶段 T：作者意图要点（后端 direction_briefs 镜像）：workId -> beKey -> brief payload
   （含已撤条目供恢复、继承的上游全书级条目）。教练回包 / 生成回包 / 全量水合都会刷新它。 */
const snowBriefs = {};
function emitBrief(workId) { snowNotify("brief", workId); }
function captureDirectionBriefs(workId, ws) {
  if (!workId || !ws || !ws.direction_briefs || typeof ws.direction_briefs !== "object") return false;
  snowBriefs[workId] = { ...ws.direction_briefs };
  emitBrief(workId);
  return true;
}

function captureTriage(workId, ws) {
  if (!workId || !ws || !Array.isArray(ws.triage_items)) return;
  const rowBySceneId = {};
  const sceneByRow = {};
  (ws.steps || []).forEach(step => {
    if (!step || step.step_key !== "scene_list") return;
    ((step.draft || {}).scenes || []).forEach(s => { if (s && s.scene_id && s.row_uid) { rowBySceneId[s.scene_id] = s.row_uid; sceneByRow[s.row_uid] = s.scene_id; } });
  });
  if (Object.keys(rowBySceneId).length) snowSceneIds[workId] = { rowBySceneId, sceneByRow };
  const items = {};
  ws.triage_items.forEach(it => {
    if (!it) return;
    const key = it.row_uid || rowBySceneId[it.scene_id] || it.scene_id;
    if (!key) return;
    items[key] = {
      scene_plan_id: it.scene_plan_id || "", scene_id: it.scene_id || "", triage_id: it.triage_id || "",
      status: (it.effective_status && it.effective_status !== "unreviewed") ? it.effective_status : (it.recommended_status || it.status || ""),
      recommended_status: it.recommended_status || "", effective_status: it.effective_status || "",
      // 阶段 R：作者裁定过（manual_status 非空）才算「你的裁定」，否则显示为系统建议
      manual: !!(it.manual_status || it.status),
      score: typeof it.score === "number" ? it.score : null, notes: it.notes || "",
      missing_fields: Array.isArray(it.missing_fields) ? it.missing_fields : [],
      fix_steps: Array.isArray(it.fix_steps) ? it.fix_steps : [],
      repair_patch: (it.repair_patch && typeof it.repair_patch === "object") ? it.repair_patch : {},
      source: it.triage_source || "",
    };
  });
  snowTriage[workId] = { items, at: Date.now(), source: "workspace" };
}

export {
  activeWork, snowNotify, subscribeSnow, snowCanon, lastPushed, buildStepFragment, recordStepHealth,
  registerPushScheduler, requestPush,
  snowHydratedOnce, snowReadyFlags, snowUnsupported, snowHydrateOk, snowHydrateInflight, snowHealth,
  captureWorkspaceHealth, shapeStepHealth,
  snowResync, snowSyncStates, snowErrorShape, setSnowSyncState, readSnowSyncState, shapeResync,
  SNOW_APPROVE_BODY, afterApproveCatalogSync, captureResync,
  snowTriage, snowSceneIds, snowBriefs, emitBrief, captureDirectionBriefs, captureTriage,
};
