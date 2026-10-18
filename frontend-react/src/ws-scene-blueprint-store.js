import { apiGet, apiPost, apiPut } from "./lib/client.js";
import { createSubscribers } from "./lib/store-utils.js";

/* ==========================================================
   WsSceneBlueprint — AI 起草台「AI 写前笔记」的 store（界面在 ws-scene-blueprint.jsx）

   后端契约（services/scene_blueprint_view.py，/api/v1/scenes/{sid}/blueprint）：
   · GET —— { scene_form, running, blueprint, character_pressure, plan_check }。blueprint 逐格带 origin：plan（第 10 步写下的那一格
     落地而来）/ ai（第 10 步里没有，AI 加的）/ author（我的版本）；我的版本留下来之后构思或参考书又改过时带 design_changed。
     plan_check 是最近一次准终稿评审的写后对照（services/plan_check.py：每条 { id, label, planned, grade, evidence_excerpt,
     located, note }，grade ∈ kept / mostly / partly / missed，没回为 null；没有评审或没有场景结构时为 null）。
   · PUT { fields } —— 保存成我的版本（起草优先用它，设计再变也不作废）。
   · POST restore-ai —— 我的版本让位，被它顶下去的 AI 版（设计没再变过的话）回来。
   · POST generate —— 蓝图与人物压力按现在的设计重做（要模型；没配好时后端 409，错误原样挂进桶）。
   起草任务在跑时写操作回 409 RUN_JOB_IN_PROGRESS。

   状态按后端 scene_id 分桶；写失败不做本地猜测，错误挂进桶由界面就地说明。
   ========================================================== */

const sbState = {};
const sbSubs = createSubscribers();
let sbVersionCounter = 0;

const SB_EMPTY = Object.freeze({
  status: "idle",
  data: null,
  error: null,
  action: Object.freeze({ busy: false, kind: null, error: null }),
});

function sbNotify() { sbVersionCounter += 1; sbSubs.notify(); }

function sbBucket(sid) {
  if (!sbState[sid]) sbState[sid] = { status: "idle", data: null, error: null, action: { ...SB_EMPTY.action } };
  return sbState[sid];
}

const sbPath = (sid) => `/api/v1/scenes/${encodeURIComponent(sid)}/blueprint`;

function sbError(e) {
  return { code: (e && e.code) || "REQUEST_FAILED", message: (e && e.message) || "请求失败" };
}

function sbFields(list) {
  return (Array.isArray(list) ? list : []).map((f) => ({
    key: String((f && f.key) || ""),
    value: String((f && f.value) || ""),
    origin: (f && f.origin) || "ai",
  })).filter((f) => f.key);
}

const PLAN_GRADES = ["kept", "mostly", "partly", "missed"];

function sbPlanCheck(raw) {
  if (!raw || !Array.isArray(raw.items)) return null;
  const items = raw.items.filter((it) => it && it.id).map((it) => ({
    id: String(it.id),
    label: String(it.label || it.id),
    planned: String(it.planned || ""),
    grade: PLAN_GRADES.includes(it.grade) ? it.grade : null,
    evidence: String(it.evidence_excerpt || ""),
    located: !!it.located,
    note: String(it.note || ""),
  }));
  if (!items.length) return null;
  return {
    items,
    departures: items.filter((it) => it.grade === "partly" || it.grade === "missed").length,
    missing: items.filter((it) => !it.grade).length,
    reviewedAt: String(raw.reviewed_at || ""),
  };
}

/* 后端回包 → 视图形状 */
function sbAdapt(data) {
  if (!data) return null;
  const bp = data.blueprint;
  const changed = bp && bp.design_changed && typeof bp.design_changed === "object" ? bp.design_changed : null;
  const pressure = data.character_pressure;
  return {
    sceneForm: data.scene_form === "reactive" ? "reactive" : "proactive",
    running: !!data.running,
    blueprint: bp ? {
      rowId: bp.row_id || "",
      createdAt: bp.created_at || "",
      source: bp.source === "author" ? "author" : "ai",
      variant: bp.variant === "facts" ? "facts" : "craft",
      fields: sbFields(bp.fields),
      tags: Array.isArray(bp.situation_tags) ? bp.situation_tags.map(String) : [],
      designChanged: changed ? { reason: changed.reason || "", at: changed.at || "", message: changed.message || "" } : null,
    } : null,
    pressure: pressure ? {
      rowId: pressure.row_id || "",
      createdAt: pressure.created_at || "",
      fields: sbFields(pressure.fields).map(({ key, value }) => ({ key, value })),
    } : null,
    planCheck: sbPlanCheck(data.plan_check),
  };
}

/* 同一场同一时间只跑一个写动作；失败把错误挂进桶并抛出 */
async function sbRun(sid, kind, fn) {
  const bucket = sbBucket(sid);
  if (bucket.action.busy) return null;
  bucket.action = { busy: true, kind, error: null };
  sbNotify();
  try {
    const data = await fn();
    bucket.status = "ready";
    bucket.data = sbAdapt(data);
    bucket.error = null;
    bucket.action = { busy: false, kind: null, error: null };
    sbNotify();
    return bucket.data;
  } catch (e) {
    bucket.action = { busy: false, kind, error: sbError(e) };
    sbNotify();
    throw e;
  }
}

export const WsSceneBlueprint = {
  subscribe(fn) { return sbSubs.subscribe(fn); },
  version() { return sbVersionCounter; },
  snapshot(sid) { return sid && sbState[sid] ? sbState[sid] : SB_EMPTY; },

  async load(sid) {
    if (!sid) return null;
    const bucket = sbBucket(sid);
    bucket.status = "loading";
    bucket.error = null;
    sbNotify();
    try {
      bucket.data = sbAdapt(await apiGet(sbPath(sid)));
      bucket.status = "ready";
      sbNotify();
      return bucket.data;
    } catch (e) {
      bucket.status = "error";
      bucket.error = sbError(e);
      sbNotify();
      throw e;
    }
  },

  /* fields：{ 格子键: 文字 }，只送改过的也行（后端照抄没送的格子） */
  saveMine(sid, fields) {
    if (!sid) return Promise.resolve(null);
    return sbRun(sid, "save", () => apiPut(sbPath(sid), { fields }));
  },

  restoreAi(sid) {
    if (!sid) return Promise.resolve(null);
    return sbRun(sid, "restore", () => apiPost(`${sbPath(sid)}/restore-ai`, {}));
  },

  regenerate(sid) {
    if (!sid) return Promise.resolve(null);
    return sbRun(sid, "generate", () => apiPost(`${sbPath(sid)}/generate`, {}));
  },
};
