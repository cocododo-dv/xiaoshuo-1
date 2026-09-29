import { apiPatch, apiPost } from "./lib/client.js";
import { emit } from "./lib/events.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { SNOW_STEPS, snowCacheKey, stepIsPristine, stepSig, stripFe } from "./ws-snow-canon.js";
import {
  SNOW_APPROVE_BODY, activeWork, afterApproveCatalogSync, buildStepFragment, lastPushed, readSnowSyncState,
  recordStepHealth, registerPushScheduler, setSnowSyncState, snowCanon, snowErrorShape, snowUnsupported,
} from "./ws-snow-sync-state.js";
import { ensureHydrated, snowHydrate } from "./ws-snow-hydrate.js";

/* ==========================================================
   雪花同步 · 上行（2026-09-29 从 ws-snow-sync.jsx 原样搬出）
   本机缓存按步 diff → PATCH steps/{key}（force）→ 需要时 approve；水合闸门先行（ensureHydrated），
   从没同步过也从没动过的空白步从不上行。步骤健康的回包经 recordStepHealth 收下（原来 6 处各写一遍）。
   ========================================================== */

/* ---------- 上行 ---------- */
let pushTimer = null;
let pendingKeys = new Set();
let pushChain = Promise.resolve();

async function snowPushKey(cacheKey) {
  const workId = cacheKey.split("::")[1];
  if (!workId || snowUnsupported[workId]) return;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(cacheKey)); } catch (e) {
    setSnowSyncState(workId, { phase: "error", error: snowErrorShape(e, "无法读取本机构思缓存", "local") });
  }
  if (!saved) return readSnowSyncState(workId);
  /* 水合闸门：本会话还没成功读到过服务端工作台，就不知道这份本机缓存相对服务端是新是旧——此时上行是盲写，
     新浏览器里那份从没水合过的空白默认稿会 force 覆盖十步。先等 / 补一次水合；仍读不到就停在「仅本机」，
     本机版本原样保留，等作者点重试（重试还是先走这道闸）。 */
  if (!(await ensureHydrated(workId))) {
    if (snowUnsupported[workId]) return readSnowSyncState(workId);
    const prior = readSnowSyncState(workId);
    setSnowSyncState(workId, {
      phase: "error", pendingSteps: SNOW_STEPS.map(([feKey]) => feKey).filter(feKey => !stepIsPristine(feKey, saved)),
      error: { ...snowErrorShape((prior.error && prior.error.scope === "hydrate") ? prior.error : null, "读不到服务器上的构思版本", "hydrate"),
        message: "读不到服务器上的构思版本，已暂停上行以免覆盖服务器内容；本机版本已保留" },
    });
    return readSnowSyncState(workId);
  }
  // 水合可能刚改写过本机缓存（服务端较新 / 空白步接回了服务端内容）——按最新的缓存算差异
  try { saved = JSON.parse(localStorage.getItem(cacheKey)) || saved; } catch (e) {}
  const mine = lastPushed[workId] || (lastPushed[workId] = {});
  const work = SNOW_STEPS.filter(([feKey]) => {
    // 从没同步过、也从没动过的空白步：没有可保存的东西，更不能拿去覆盖服务端（水合没认出内容的步骤也靠这条兜底）
    if (!mine[feKey] && stepIsPristine(feKey, saved)) return false;
    const fragment = buildStepFragment(feKey, saved, workId);
    const prev = mine[feKey] || {};
    return prev.sig !== stepSig(fragment) || prev.approvalPending === true;
  });
  if (!work.length) {
    setSnowSyncState(workId, { phase: "synced", pendingSteps: [], error: null, lastSyncedAt: Date.now() });
    return readSnowSyncState(workId);
  }
  setSnowSyncState(workId, { phase: "syncing", pendingSteps: work.map(([feKey]) => feKey), error: null });
  const failures = [];
  let pushedSceneish = false; // 9/10 步的改动会改变物化后的 resync_status
  for (const [feKey, beKey] of work) {
    const fragment = buildStepFragment(feKey, saved, workId);
    const sig = stepSig(fragment);
    const prev = mine[feKey] || {};
    if (prev.sig !== sig) {
      try {
        const patched = await apiPatch(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}`, { draft: fragment, force: true });
        const patchedStatus = patched && patched.step && patched.step.status;
        // 阶段 G：已确认的步骤被改动（后端 revised_after_approval）→ 待作者显式重新确认，
        // 不再在停止输入一秒后自动补批准——下游失效级联在作者点「确认本步」那一刻才发生。
        const revisedAfterApproval = !!(patched && patched.step && patched.step.revised_after_approval);
        const approvalPending = fragment.fe_state === "done"
          && patchedStatus !== "approved"
          && patchedStatus !== "skipped"
          && !revisedAfterApproval;
        mine[feKey] = { sig, state: fragment.fe_state, approvalPending };
        if (feKey === "scenes" || feKey === "planning") pushedSceneish = true;
        // update_step 回包带最新 step.health/completeness → 增量刷新后端权威评估（无需再拉全量）
        if (patched && patched.step) {
          // 服务端把 draft 过了模板归一化（可能补齐空模板键）——刷新 canon 镜像并
          // 用新镜像重算 sig 记账，否则下轮 save 会因归一化差异多推一次空转 PATCH
          (snowCanon[workId] || (snowCanon[workId] = {}))[feKey] = stripFe(patched.step.draft || {});
          mine[feKey] = {
            sig: stepSig(buildStepFragment(feKey, saved, workId)),
            state: fragment.fe_state,
            approvalPending,
          };
          recordStepHealth(workId, feKey, patched.step);
        }
      } catch (error) {
        if (error && error.status === 409 && error.code === "PROJECT_NOT_SNOWFLAKE") {
          snowUnsupported[workId] = true;
          const shaped = snowErrorShape(error, "当前作品未启用雪花工作台");
          setSnowSyncState(workId, { phase: "error", pendingSteps: [feKey], error: shaped });
          return readSnowSyncState(workId);
        }
        failures.push({ feKey, beKey, stage: "patch", error });
        continue; // PATCH 未成功，绝不能继续 approve
      }
    }

    // PATCH 回包的服务端状态优先：本机首次载入时已经是 done，也必须把 pending_review
    // 补批准；不能只依赖“本会话观察到 active → done”，否则离线/刷新后的完成态会永久卡住。
    // 批准失败会写 approvalPending，重试时即使 PATCH 已成功也会再次批准。
    const currentLedger = mine[feKey] || prev;
    const shouldApprove = fragment.fe_state === "done"
      && (currentLedger.approvalPending === true || (prev.state && prev.state !== "done"));
    if (!shouldApprove) continue;
    try {
      const appr = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/approve`, SNOW_APPROVE_BODY);
      mine[feKey] = { ...(mine[feKey] || {}), state: "done", approvalPending: false };
      if (appr && appr.step) recordStepHealth(workId, feKey, appr.step, appr.workspace); // 下游 stale 立即可见
      afterApproveCatalogSync(workId, appr);
    } catch (error) {
      mine[feKey] = { ...(mine[feKey] || {}), state: "done", approvalPending: true };
      // 「需要先确认前面的雪花步骤」不是同步故障：本步 draft 已由上面的 PATCH 存到服务器，
      // 只是「确认」被上游依赖挡住。保留 approvalPending，等作者补确认前面步骤后，下一次
      // autosave 会按序自动补批；但绝不计入 failures——否则每次编辑都把这条正常的依赖等待
      // 谎报成红色「仅本机已保存 · 服务器同步失败」，且「重试」按钮对它毫无作用（重试不会
      // 替作者确认前面的步骤）。其余 approve 失败（真·闸门/网络）照旧上报并可重试。
      if (!(error && error.code === "SNOWFLAKE_PREVIOUS_STEP_REQUIRED")) {
        failures.push({ feKey, beKey, stage: "approve", error });
      }
    }
  }
  // 物化过（目录里已有章）的作品：9/10 步保存后强制重拉一次工作台，让「N 场待同步」
  // 的回流横幅跟上。hydrate 的 _t 比较保证较新的本地草稿不会被回写覆盖。
  if (pushedSceneish) {
    try {
      const hasCatalog = !!WsCatalog.get().length;
      if (hasCatalog) await snowHydrate(workId, { force: true });
    } catch (e) {}
  }
  if (failures.length) {
    const first = failures[0];
    const stageLabel = first.stage === "approve" ? "后端批准" : "服务器保存";
    setSnowSyncState(workId, {
      phase: "error",
      pendingSteps: [...new Set(failures.map(item => item.feKey))],
      error: snowErrorShape(first.error, `${stageLabel}失败，本机版本已保留`),
      failures: failures.map(item => ({ feKey: item.feKey, beKey: item.beKey, stage: item.stage, code: item.error && (item.error.code || item.error.status) })),
    });
  } else {
    setSnowSyncState(workId, { phase: "synced", pendingSteps: [], failures: [], error: null, lastSyncedAt: Date.now() });
  }
  return readSnowSyncState(workId);
}

function schedulePush(cacheKey) {
  pendingKeys.add(cacheKey);
  const workId = String(cacheKey || "").split("::")[1];
  if (workId) setSnowSyncState(workId, { phase: "local_only", localSavedAt: Date.now(), error: null });
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    const keys = [...pendingKeys];
    pendingKeys = new Set();
    keys.forEach(k => { pushChain = pushChain.then(() => snowPushKey(k)).catch(() => {}); });
  }, 700);
}

/* 分章预览/物化是雪花流程的下一跳，必须先排空 450ms 本机保存 + 700ms 上行防抖。
   否则作者刚点“确认本步”就点“整理”，预览会抢在 PATCH/approve 前读到旧闸门。 */
async function flushSnowPush(workId) {
  const id = workId || activeWork();
  if (!id) return readSnowSyncState(id);
  const key = snowCacheKey(id);
  // 先向仍挂载的雪花视图要一份“此刻内存态”的同步落盘，跨过视图自身 450ms 的
  // localStorage 防抖。事件是同步分发的；视图写完会立刻发 ws:snow-saved，把 key
  // 放进下面要排空的队列。作者页直达等没有雪花视图的场景则只排已有队列。
  emit("ws:snow-flush-local", { workId: id });
  if (pendingKeys.has(key)) {
    pendingKeys.delete(key);
    if (!pendingKeys.size) {
      clearTimeout(pushTimer);
      pushTimer = null;
    }
    pushChain = pushChain.catch(() => {}).then(() => snowPushKey(key));
  }
  await pushChain.catch(() => {});
  return readSnowSyncState(id);
}

/* 作者点「重试」：马上把这部作品排进上行链（不等防抖），返回这一次上行的 Promise */
function retryPush(cacheKey) {
  pushChain = pushChain.catch(() => {}).then(() => snowPushKey(cacheKey));
  return pushChain;
}

/* 水合发现要补批准时经这里排一次上行（见 ws-snow-sync-state.js 的 requestPush） */
registerPushScheduler(schedulePush);

export { snowPushKey, schedulePush, flushSnowPush, retryPush };
