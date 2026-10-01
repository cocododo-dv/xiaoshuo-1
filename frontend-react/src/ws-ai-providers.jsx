import React from "react";
import { apiAdminDelete, apiAdminGet, apiAdminPost, apiGet } from "./lib/client.js";
import { createSubscribers, useStoreTick } from "./lib/store-utils.js";

/* ==========================================================
   WsAiProviders — AI 模型接入 store(设置 → AI 模型)
   ----------------------------------------------------------
   后端真相:/api/v1/system-config/llm 系列接口(Provider CRUD、
   探活、模型列表、分工槽位、节点路由)。管理面板低频写,统一采用
   「写后重拉 overview」而非乐观更新;唯一本地持久化是管理令牌
   （sessionStorage novel-system-admin-token，仅在当前浏览器会话保留）。
   ========================================================== */

const ADMIN_TOKEN_KEY = "novel-system-admin-token";

let AIP = {
  loading: false,
  loaded: false,
  error: null,          // ApiRequestError | null —— overview 拉取失败
  overview: null,       // GET /llm 的 data(providers/node_routes/role_slots/readiness…)
  presets: null,        // GET /llm/provider-presets 的 data({presets, provider_catalog})
  adminConfigured: false, // 后端是否设置了 NOVEL_SYSTEM_ADMIN_TOKEN
  busy: {},             // { [动作key]: true } —— 行内按钮加载态
  probes: {},           // { [provider_id]: 最近一次探活结果 }
};

const aipSubs = createSubscribers();
function aipNotify() { aipSubs.notify(); }
function aipPatch(patch) { AIP = { ...AIP, ...patch }; aipNotify(); }
function aipBusy(key, on) {
  const busy = { ...AIP.busy };
  if (on) busy[key] = true; else delete busy[key];
  aipPatch({ busy });
}

/* busy 样板收敛:置忙 → 执行 → (成功时按 refreshAfter 重拉) → finally 复位。
   refreshAfter="always":成功后重拉 overview;
   refreshAfter="overview":返回值自带 overview 则就地覆写,否则重拉;
   省略:不重拉。失败一律原样上抛,busy 在 finally 复位。 */
async function withBusy(key, fn, refreshAfter) {
  aipBusy(key, true);
  try {
    const result = await fn();
    if (refreshAfter === "overview" && result?.overview) aipPatch({ overview: result.overview });
    else if (refreshAfter) await WsAiProviders.refresh();
    return result;
  } finally {
    aipBusy(key, false);
  }
}

/* 管理令牌只读 sessionStorage。旧版存在 localStorage 里的那一份不再搬过来（批准 #25，重评 R16），
   但照旧随手抹掉：管理口令不能留在 localStorage 里（幂等，与 setAdminToken 同一条） */
function adminToken() {
  try { localStorage.removeItem(ADMIN_TOKEN_KEY); } catch (e) { /* 存储被禁用：没有可抹的 */ }
  try { return (sessionStorage.getItem(ADMIN_TOKEN_KEY) || "").trim(); } catch (e) { return ""; }
}

const WsAiProviders = {
  subscribe(fn) { return aipSubs.subscribe(fn); },
  state: () => AIP,
  adminToken,
  /* 管理令牌缺失/错误 → 视图提示输入(ADMIN_TOKEN_REQUIRED 由调用处捕获) */
  setAdminToken(value) {
    try {
      const v = (value || "").trim();
      localStorage.removeItem(ADMIN_TOKEN_KEY);
      if (v) sessionStorage.setItem(ADMIN_TOKEN_KEY, v);
      else sessionStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch (e) {}
    aipNotify();
  },

  /* 只拉 /llm 一个接口:overview 自带 runtime.admin_configured。
     (旧实现每次都并拉全量 /system-config——它带全部历史快照,已到 MB 级,
     是「保存服务」按钮迟迟不结束的主因。) */
  async refresh() {
    aipPatch({ loading: true, error: null });
    try {
      const overview = await apiGet("/api/v1/system-config/llm");
      aipPatch({
        loading: false,
        loaded: true,
        overview,
        adminConfigured: Boolean(overview?.runtime?.admin_configured),
      });
      return overview;
    } catch (error) {
      aipPatch({ loading: false, error });
      throw error;
    }
  },

  async loadPresets() {
    if (AIP.presets) return AIP.presets;
    const presets = await apiGet("/api/v1/system-config/llm/provider-presets");
    aipPatch({ presets });
    return presets;
  },

  /* 保存(新增或编辑)一个模型服务;成功后重拉 overview */
  async saveProvider(payload) {
    return withBusy(`save:${payload.provider_id}`, () =>
      apiAdminPost("/api/v1/system-config/llm/providers", payload, adminToken()), "always");
  },

  /* 删除一个模型服务(连同后端密钥);节点路由不随删,orphaned 列表随返回值带回。
     特例:重拉前先清掉该 provider 的探活残留 */
  async deleteProvider(providerId) {
    return withBusy(`delete:${providerId}`, async () => {
      const result = await apiAdminDelete(
        `/api/v1/system-config/llm/providers/${encodeURIComponent(providerId)}`, adminToken(),
      );
      const probes = { ...AIP.probes };
      delete probes[providerId];
      aipPatch({ probes });
      return result;
    }, "always");
  },

  async setDefault(providerId) {
    return withBusy(`default:${providerId}`, () => apiAdminPost(
      `/api/v1/system-config/llm/providers/${encodeURIComponent(providerId)}/default`, {}, adminToken(),
    ), "always");
  },

  /* 已保存服务的连接测试;结果留在 probes[providerId] 供行内展示。
     特例:失败也要把 {ok:false} 写入 probes 后再上抛 */
  async probe(providerId, extra = {}) {
    return withBusy(`probe:${providerId}`, async () => {
      try {
        const result = await apiAdminPost(
          `/api/v1/system-config/llm/providers/${encodeURIComponent(providerId)}/probe`,
          { check_completion: true, ...extra },
          adminToken(),
        );
        aipPatch({ probes: { ...AIP.probes, [providerId]: result } });
        return result;
      } catch (error) {
        aipPatch({ probes: { ...AIP.probes, [providerId]: { ok: false, message: error.message } } });
        throw error;
      }
    });
  },

  /* 已保存服务的模型列表(实时拉取,失败回退预设) */
  async fetchModels(providerId) {
    return withBusy(`models:${providerId}`, () => apiAdminGet(
      `/api/v1/system-config/llm/providers/${encodeURIComponent(providerId)}/models`, adminToken(),
    ));
  },

  /* 保存前的草稿试连(添加流程用):同样能带回 available_models */
  async testDraft(payload) {
    return withBusy("draft-test", () =>
      apiAdminPost("/api/v1/system-config/test-provider", payload, adminToken()));
  },

  /* 分工槽位:{slot_id: {provider_id, model}} 批量展开为节点路由 */
  async saveRoleRoutes(assignments, activate = true) {
    return withBusy("role-routes", () => apiAdminPost(
      "/api/v1/system-config/llm/role-routes", { assignments, activate }, adminToken(),
    ), "overview");
  },

  /* 一键补齐缺失路由(默认 provider 或指定 provider/model) */
  async syncMissing(payload = {}) {
    return withBusy("sync-missing", () => apiAdminPost(
      "/api/v1/system-config/llm/node-routes/sync-missing", { activate: true, ...payload }, adminToken(),
    ), "overview");
  },
};

function useAiProviders() {
  useStoreTick((fn) => WsAiProviders.subscribe(fn));
  return AIP;
}

export { WsAiProviders, useAiProviders };
